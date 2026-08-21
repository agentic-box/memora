"""memora #965 phase 2: /mcp/<db> routing.

The load-bearing properties are that the selector actually binds, that an
UNKNOWN database fails closed rather than reaching the default, and that the
binding is sticky per SESSION — the last is asserted so a future SDK change to
per-request semantics is caught as a regression rather than adopted silently.
"""
import json

import pytest

from memora import storage
from memora.db_routing import NotAnMcpPath, make_router, parse_db_from_path
from memora.storage import CURRENT_DB


class TestPathParsing:
    @pytest.mark.parametrize("path,expected", [
        ("/mcp/ob1", "ob1"),
        ("/mcp/ob1/", "ob1"),
        ("/mcp", None),
        ("/mcp/", None),
    ])
    def test_extracts_the_database_name(self, path, expected):
        assert parse_db_from_path(path) == expected

    @pytest.mark.parametrize("path", [
        "/mcpbeta",       # prefix-matched to "beta" before the boundary fix
        "/mcp-other",     # prefix-matched to "-other"
        "/other",
        "/mcp/ob1/extra",  # extra segments are NOT an alias for ob1
    ])
    def test_non_route_paths_are_rejected(self, path):
        """A string prefix made any /mcp* URL an alternate database alias.

        That bypasses proxy or routing rules written for the canonical path,
        so matching is on a path-COMPONENT boundary and anything else is
        delegated unchanged rather than rewritten.
        """
        with pytest.raises(NotAnMcpPath):
            parse_db_from_path(path)


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
        # GENERIC body: this router runs OUTSIDE the inner app's auth, so
        # echoing the registry error would let an unauthenticated caller
        # enumerate database names and read backend configuration detail.
        body = sent[1]["body"]
        assert b"does-not-exist" not in body, "404 leaked the requested name"
        assert b"alpha" not in body and b"beta" not in body, "404 leaked the registry"

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


class TestProductionWiring:
    """codex: all routing tests called make_router directly, so a typo in
    server.main() would leave every one of them green and Phase 2 inert in
    production — the same integration gap as #969's discovery bug."""

    def _stub(self, monkeypatch):
        from memora import server

        calls = {"uvicorn_app": None, "mcp_run": False}
        monkeypatch.setattr(server, "start_graph_server", lambda *a, **k: None)
        monkeypatch.setattr(server, "connect", lambda *a, **k: _FakeConn())
        monkeypatch.setattr(server.mcp, "run",
                            lambda *a, **k: calls.__setitem__("mcp_run", True))
        import uvicorn
        monkeypatch.setattr(uvicorn, "run",
                            lambda app, **k: calls.__setitem__("uvicorn_app", app))
        return server, calls

    def test_configured_http_serves_the_routed_app(self, monkeypatch, tmp_path):
        server, calls = self._stub(monkeypatch)
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
        # MEMORA_TRANSPORT is read at MODULE IMPORT into DEFAULT_TRANSPORT, so
        # setting it here would not reach args.transport. Pass it explicitly.
        server.main(["--transport", "streamable-http"])
        assert calls["uvicorn_app"] is not None, "routed app was never served"
        assert calls["mcp_run"] is False, "fell through to mcp.run; routing is inert"

    def test_unconfigured_http_stays_on_mcp_run(self, monkeypatch):
        server, calls = self._stub(monkeypatch)
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        # MEMORA_TRANSPORT is read at MODULE IMPORT into DEFAULT_TRANSPORT, so
        # setting it here would not reach args.transport. Pass it explicitly.
        server.main(["--transport", "streamable-http"])
        assert calls["mcp_run"] is True
        assert calls["uvicorn_app"] is None

    def test_configured_stdio_stays_on_mcp_run(self, monkeypatch, tmp_path):
        server, calls = self._stub(monkeypatch)
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
        server.main(["--transport", "stdio"])
        assert calls["mcp_run"] is True
        assert calls["uvicorn_app"] is None


class _FakeConn:
    def close(self):
        pass


class TestRouteSafeNames:
    """codex: Phase 1 accepted names the router cannot address."""

    @pytest.mark.parametrize("name", ["with/slash", "..", ".", "has space", ""])
    def test_unroutable_names_are_rejected_at_validation(self, monkeypatch, name):
        from memora.storage import DatabaseRegistryError, database_registry

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({name: "/tmp/x.db"}))
        with pytest.raises(DatabaseRegistryError):
            database_registry()

    def test_ordinary_names_are_accepted(self, monkeypatch):
        from memora.storage import database_registry

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "ob1": "/tmp/a.db", "my-db_2.x": "/tmp/b.db"}))
        assert set(database_registry()) == {"ob1", "my-db_2.x"}

    def test_bare_initialize_then_named_call_uses_the_default(self, monkeypatch, tmp_path):
        """codex: the most surprising disagreement, and it was unasserted.

        FastMCP creates the long-lived session task while handling INITIALIZE,
        and AnyIO copies that task's context into it. So a session that
        initializes on bare /mcp captures CURRENT_DB=None, and a later
        /mcp/beta call on the same session still executes tools unbound —
        resolving the registry DEFAULT, not beta. "First session path wins" is
        the rule; this pins the case where that surprises someone.
        """
        first, second = self._run_session(monkeypatch, tmp_path,
                                          "/mcp", "/mcp/beta", port=8874)
        assert first == "<unbound>"
        assert second == "<unbound>", (
            f"a later /mcp/beta call switched the session to {second!r}; "
            "the binding became per-request"
        )

    def test_two_concurrent_sessions_do_not_cross_talk(self, monkeypatch, tmp_path):
        """Cross-session isolation is the primary safety property here."""
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

        probe = FastMCP("conc-probe", host="127.0.0.1", port=8875)

        @probe.tool()
        async def which_db() -> str:
            return CURRENT_DB.get() or "<unbound>"

        app = make_router(probe.streamable_http_app())
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8875,
                                               log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        results = {}
        ready = threading.Barrier(2, timeout=10)

        def session(db):
            sid = {"v": None}

            def rpc(method, params, notify=False):
                body = _json.dumps({"jsonrpc": "2.0", "method": method,
                                    "params": params,
                                    **({} if notify else {"id": 1})}).encode()
                h = {"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"}
                if sid["v"]:
                    h["Mcp-Session-Id"] = sid["v"]
                req = urllib.request.Request(
                    f"http://127.0.0.1:8875/mcp/{db}", data=body, headers=h)
                with urllib.request.urlopen(req, timeout=20) as r:
                    sid["v"] = r.headers.get("Mcp-Session-Id") or sid["v"]
                    raw = r.read().decode()
                if notify or not raw.strip():
                    return None
                for line in raw.splitlines():
                    if line.startswith("data: "):
                        raw = line[6:]
                        break
                return _json.loads(raw)

            rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": db, "version": "0"}})
            rpc("notifications/initialized", {}, notify=True)
            ready.wait()                       # overlap the tool calls
            results[db] = rpc("tools/call", {"name": "which_db", "arguments": {}}
                              )["result"]["content"][0]["text"]

        threads = [threading.Thread(target=session, args=(d,)) for d in ("alpha", "beta")]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            server.should_exit = True

        assert results == {"alpha": "alpha", "beta": "beta"}, (
            f"sessions crossed databases: {results}"
        )

    def _run_session(self, monkeypatch, tmp_path, first_path, second_path, port):
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

        probe = FastMCP("path-probe", host="127.0.0.1", port=port)

        @probe.tool()
        async def which_db() -> str:
            return CURRENT_DB.get() or "<unbound>"

        app = make_router(probe.streamable_http_app())
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
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
            h = {"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"}
            if sid["v"]:
                h["Mcp-Session-Id"] = sid["v"]
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                         data=body, headers=h)
            with urllib.request.urlopen(req, timeout=20) as r:
                sid["v"] = r.headers.get("Mcp-Session-Id") or sid["v"]
                raw = r.read().decode()
            if notify or not raw.strip():
                return None
            for line in raw.splitlines():
                if line.startswith("data: "):
                    raw = line[6:]
                    break
            return _json.loads(raw)

        try:
            rpc(first_path, "initialize", {"protocolVersion": "2024-11-05",
                                           "capabilities": {},
                                           "clientInfo": {"name": "t", "version": "0"}})
            rpc(first_path, "notifications/initialized", {}, notify=True)
            a = rpc(first_path, "tools/call",
                    {"name": "which_db", "arguments": {}})["result"]["content"][0]["text"]
            b = rpc(second_path, "tools/call",
                    {"name": "which_db", "arguments": {}})["result"]["content"][0]["text"]
        finally:
            server.should_exit = True
        return a, b
