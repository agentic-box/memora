"""memora #965 phase 2: /mcp/<db> routing.

The load-bearing properties are that the selector actually binds, that an
UNKNOWN database fails closed rather than reaching the default, and that the
binding is sticky per SESSION — the last is asserted so a future SDK change to
per-request semantics is caught as a regression rather than adopted silently.
"""
import json

import pytest

from memora import storage
from memora.db_routing import make_router, parse_db_from_path
from memora.storage import CURRENT_DB


class TestPathParsing:
    @pytest.mark.parametrize("path,expected", [
        ("/mcp/ob1", "ob1"),
        ("/mcp/ob1/", "ob1"),
        ("/mcp/ob1/extra", "ob1"),
        ("/mcp", None),
        ("/mcp/", None),
        ("/other", None),
    ])
    def test_extracts_the_database_name(self, path, expected):
        assert parse_db_from_path(path) == expected


class _Recorder:
    """Stands in for FastMCP's app; records what the router bound and passed."""

    def __init__(self):
        self.seen_db = "<never called>"
        self.seen_path = None

    async def __call__(self, scope, receive, send):
        self.seen_db = CURRENT_DB.get()
        self.seen_path = scope.get("path")


def _drive(app, path):
    """Run one request through the router.

    Driven with asyncio.run rather than pytest-asyncio: the repo does not
    depend on that plugin and a routing test is not worth adding one for.
    """
    import asyncio

    sent = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(app({"type": "http", "path": path}, None, send))
    return sent


class TestRouting:
    @pytest.fixture(autouse=True)
    def _registry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "alpha.db"),
            "beta": str(tmp_path / "beta.db"),
        }))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None
        yield
        storage._registry_cache = None
        storage._registry_source = None

    def test_path_binds_the_named_database(self):
        inner = _Recorder()
        _drive(make_router(inner), "/mcp/beta")
        assert inner.seen_db == "beta"

    def test_path_is_rewritten_so_fastmcp_still_matches(self):
        """FastMCP mounts ONE route at a fixed path; /mcp/beta would 404."""
        inner = _Recorder()
        _drive(make_router(inner), "/mcp/beta")
        assert inner.seen_path == "/mcp"

    def test_bare_mcp_leaves_the_binding_unset(self):
        """Unbound means the registry default, resolved by current_backend()."""
        inner = _Recorder()
        _drive(make_router(inner), "/mcp")
        assert inner.seen_db is None

    def test_unknown_database_fails_closed_and_never_reaches_the_app(self):
        """The failure this feature must never have: serving the wrong store."""
        inner = _Recorder()
        sent = _drive(make_router(inner), "/mcp/does-not-exist")
        assert inner.seen_db == "<never called>", "request reached the app anyway"
        assert sent[0]["status"] == 404
        assert b"does-not-exist" in sent[1]["body"]

    def test_binding_is_released_after_the_request(self):
        inner = _Recorder()
        _drive(make_router(inner), "/mcp/beta")
        assert CURRENT_DB.get() is None, "binding leaked past the request"

    def test_lifespan_is_delegated_untouched(self):
        """The session manager runs in the inner app's lifespan."""
        seen = {}

        async def inner(scope, receive, send):
            seen["type"] = scope["type"]

        import asyncio
        asyncio.run(make_router(inner)({"type": "lifespan"}, None, None))
        assert seen["type"] == "lifespan"


class TestStickyPerSession:
    """The phase 0 spike found the binding is sticky to the MCP SESSION.

    A session opened on /mcp/alpha and reused against /mcp/beta still resolves
    to alpha, because streamable-http keeps a long-lived task per session id.
    That is the SAFER semantic and we want it — a client cannot half-switch
    databases mid-conversation. Asserted here so a future SDK change to
    per-request semantics is caught as a REGRESSION rather than adopted
    silently: it would open exactly the cross-database window this feature
    exists to prevent.
    """

    def test_end_to_end_session_stays_on_its_first_database(self, monkeypatch, tmp_path):
        import json as _json
        import threading
        import time
        import urllib.request

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setenv("MEMORA_DATABASES", _json.dumps({
            "alpha": str(tmp_path / "alpha.db"),
            "beta": str(tmp_path / "beta.db"),
        }))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None

        probe = FastMCP("sticky-probe", host="127.0.0.1", port=8873)

        @probe.tool()
        async def which_db() -> str:
            return CURRENT_DB.get() or "<unbound>"

        app = make_router(probe.streamable_http_app())
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8873,
                                               log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        sid = {"v": None}

        def rpc(path, method, params, notify=False):
            body = _json.dumps({"jsonrpc": "2.0", "method": method, "params": params,
                                **({} if notify else {"id": 1})}).encode()
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
            if sid["v"]:
                headers["Mcp-Session-Id"] = sid["v"]
            req = urllib.request.Request(f"http://127.0.0.1:8873{path}",
                                         data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                sid["v"] = resp.headers.get("Mcp-Session-Id") or sid["v"]
                raw = resp.read().decode()
            if notify or not raw.strip():
                return None
            for line in raw.splitlines():
                if line.startswith("data: "):
                    raw = line[6:]
                    break
            return _json.loads(raw)

        try:
            rpc("/mcp/alpha", "initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"}})
            rpc("/mcp/alpha", "notifications/initialized", {}, notify=True)
            first = rpc("/mcp/alpha", "tools/call",
                        {"name": "which_db", "arguments": {}})["result"]["content"][0]["text"]
            # SAME session id, DIFFERENT path — a client could do this by accident
            second = rpc("/mcp/beta", "tools/call",
                         {"name": "which_db", "arguments": {}})["result"]["content"][0]["text"]
        finally:
            server.should_exit = True

        assert first == "alpha"
        assert second == "alpha", (
            f"the session followed the PATH to {second!r}; the binding became "
            "per-request, which opens a cross-database window mid-conversation"
        )
