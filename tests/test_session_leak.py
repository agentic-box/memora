"""memora #999: a rejected request must not cost a session.

The SDK's session manager creates AND REGISTERS a transport for any request
without an Mcp-Session-Id header BEFORE validating it, then rejects a bare GET
with 406 and keeps the session. Measured at 41.3 kB per probe, that exhausted
the 768 MB container in ~3.2 hours under clmux's sidebar liveness check.

These tests assert on RETAINED SESSION STATE, not on the status code: a test
that only checks for 406 passes with the leak fully present, which is exactly
how this survived to production.
"""
import json

import pytest

from memora import storage
from memora.db_routing import make_router


class _Recorder:
    """An inner app that records whether the router delegated to it."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope.get("path"))
        body = b'{"delegated": true}'
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


async def _run(app, method, path, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app({"type": "http", "method": method, "path": path,
               "headers": list(headers)}, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
    storage._registry_cache = None
    storage._registry_source = None
    yield
    storage._registry_cache = None
    storage._registry_source = None


class TestARejectedRequestNeverReachesTheSessionManager:
    """The invariant: the SDK must not SEE a request it would allocate for."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["/mcp", "/mcp/alpha"])
    async def test_a_bare_GET_is_not_delegated(self, registry, path):
        inner = _Recorder()
        status, _ = await _run(make_router(inner), "GET", path)
        assert status == 406
        assert inner.calls == [], (
            "the bare GET reached the session manager, which allocates before "
            "it rejects -- the #999 leak"
        )

    @pytest.mark.anyio
    async def test_the_status_stays_406_so_the_sidebar_probe_still_works(self, registry):
        """clmux #969 reads 406 as 'MCP is answering'. Fixing the leak must
        not break a working liveness check."""
        status, _ = await _run(make_router(_Recorder()), "GET", "/mcp/alpha")
        assert status == 406

    @pytest.mark.anyio
    @pytest.mark.parametrize("method", ["GET", "DELETE", "HEAD", "PUT"])
    async def test_no_session_id_and_not_a_POST_is_always_refused(self, registry, method):
        inner = _Recorder()
        await _run(make_router(inner), method, "/mcp/alpha")
        assert inner.calls == [], f"{method} without a session id was delegated"


class TestLegitimateTrafficIsUntouched:
    """The guard must not become an outage of its own."""

    @pytest.mark.anyio
    async def test_a_session_less_POST_still_reaches_the_server(self, registry):
        """This is `initialize` -- the one request whose job IS to create a
        session. Refusing it would break every client."""
        inner = _Recorder()
        status, _ = await _run(make_router(inner), "POST", "/mcp/alpha")
        assert inner.calls == ["/mcp"], "initialize was refused; no client could connect"
        assert status == 200

    @pytest.mark.anyio
    async def test_a_GET_WITH_a_session_id_still_reaches_the_server(self, registry):
        """The SSE stream of an established session."""
        inner = _Recorder()
        headers = [(b"mcp-session-id", b"deadbeef"), (b"accept", b"text/event-stream")]
        status, _ = await _run(make_router(inner), "GET", "/mcp/alpha", headers)
        assert inner.calls == ["/mcp"], "an established session's stream was refused"
        assert status == 200

    @pytest.mark.anyio
    async def test_the_header_match_is_case_insensitive(self, registry):
        """HTTP headers are case-insensitive; a client sending Mcp-Session-Id
        in any casing must not be refused."""
        inner = _Recorder()
        await _run(make_router(inner), "GET", "/mcp/alpha", [(b"MCP-Session-Id", b"x")])
        assert inner.calls == ["/mcp"]

    @pytest.mark.anyio
    async def test_non_mcp_paths_are_never_guarded(self, registry):
        """/health is a bare GET with no session id BY DESIGN, and the
        watchdog depends on it."""
        inner = _Recorder()
        status, _ = await _run(make_router(inner), "GET", "/health")
        assert inner.calls == ["/health"], "the guard swallowed the liveness endpoint"
        assert status == 200

    @pytest.mark.anyio
    async def test_an_unknown_database_still_fails_closed_before_anything_else(self, registry):
        inner = _Recorder()
        headers = [(b"mcp-session-id", b"x")]
        status, body = await _run(make_router(inner), "POST", "/mcp/nosuch", headers)
        assert status == 404
        assert inner.calls == []
        assert b"nosuch" not in body


@pytest.fixture
def anyio_backend():
    return "asyncio"
