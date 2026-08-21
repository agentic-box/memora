"""memora #999: a request the server refuses must not cost a session.

These tests drive a REAL StreamableHTTPSessionManager and assert on
len(session_manager._server_instances) -- the retained session count itself.
An earlier version used a fake inner app and asserted only delegation and
status codes; it passed while the DoS primitive was fully intact behind a
POST, and it blessed an empty-body POST as a legitimate "initialize". A test
that cannot observe the leak cannot prove it is gone.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from memora import session_guard, storage


def _post(port, path, body, headers=None, timeout=10):
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    hdrs.update(headers or {})
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def _request(port, path, method="GET", headers=None, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "leaktest", "version": "0"}}}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A real FastMCP streamable-http app behind the real guard."""
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    storage._registry_cache = None
    storage._registry_source = None
    session_guard._reset_admissions()

    mcp = FastMCP("leak-probe")

    @mcp.tool()
    async def ping() -> str:
        return "pong"

    from memora.health import register_health_routes
    register_health_routes(mcp)

    app = session_guard.guard_sessions(mcp.streamable_http_app())
    # A FRESH port per test. Reusing one fixed port made later tests hammer a
    # socket the previous server had not released yet, turning every request
    # into a 10s timeout -- the suite hung rather than failing, which is the
    # worst way for a test to be wrong.
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                        log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(80):
        if getattr(srv, "started", False):
            break
        time.sleep(0.1)
    yield mcp, port
    srv.should_exit = True
    for _ in range(50):
        if not getattr(srv, "started", False):
            break
        time.sleep(0.1)
    storage._registry_cache = None
    storage._registry_source = None


def _sessions(mcp):
    return len(mcp.session_manager._server_instances)


class TestRefusedRequestsCostNothing:
    def test_a_bare_GET_retains_no_session(self, server):
        mcp, port = server
        assert _sessions(mcp) == 0
        assert _request(port, "/mcp") == 406
        assert _sessions(mcp) == 0

    @pytest.mark.parametrize("body,label", [
        (b"", "empty body"),
        (b"not json at all", "malformed json"),
        ({"jsonrpc": "2.0", "method": "initialize"}, "notification, no id"),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "not an initialize"),
        ([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}], "a batch"),
        ({"jsonrpc": "2.0", "id": 1}, "no method"),
        # codex round 3: these LOOK like an initialize and the SDK rejects
        # them on typed validation -- after allocating.
        ({"method": "initialize", "id": 1}, "no jsonrpc member"),
        ({"jsonrpc": "2.0", "id": None, "method": "initialize", "params": {}}, "id is null"),
        ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "empty params"),
        ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": 5}}}, "malformed clientInfo"),
    ])
    def test_a_sessionless_POST_that_is_not_an_initialize_retains_no_session(
            self, server, body, label):
        """codex P0: the first fix allowed ANY session-less POST through, so
        the exhaustion primitive simply moved from GET to POST. The SDK
        allocates before it validates Accept, Content-Type, JSON or method."""
        mcp, port = server
        assert _sessions(mcp) == 0
        status = _post(port, "/mcp", body)
        assert status == 400, f"{label} was not refused (got {status})"
        assert _sessions(mcp) == 0, f"{label} left a session behind"

    def test_a_flood_of_bad_POSTs_does_not_accumulate_sessions(self, server):
        """The DoS itself, at small scale. O(1), not O(N)."""
        mcp, port = server
        for _ in range(200):
            _post(port, "/mcp", b"{}")
        assert _sessions(mcp) == 0, (
            f"200 refused POSTs retained {_sessions(mcp)} sessions"
        )

    def test_an_oversized_body_is_refused_without_buffering_further(self, server):
        mcp, port = server
        huge = b"x" * (session_guard.MAX_INIT_BODY_BYTES + 1024)
        assert _post(port, "/mcp", huge) == 413
        assert _sessions(mcp) == 0


class TestLegitimateTrafficStillWorks:
    def test_a_real_initialize_DOES_create_exactly_one_session(self, server):
        """The guard must not be an outage: this is the request whose job is
        to create a session, and it is also the control proving the session
        counter can go UP -- without it every assertion above is vacuous."""
        mcp, port = server
        assert _sessions(mcp) == 0
        assert _post(port, "/mcp", INITIALIZE) == 200
        assert _sessions(mcp) == 1

    def test_health_is_never_guarded(self, server):
        """A bare GET with no session by design; the watchdog depends on it."""
        mcp, port = server
        assert _request(port, "/health") == 200
        assert _sessions(mcp) == 0

    def test_a_request_carrying_a_session_id_is_passed_through(self, server):
        """Even an unknown id must reach the SDK, which owns that decision --
        the guard only refuses requests with NO session at all."""
        mcp, port = server
        before = _sessions(mcp)
        status = _request(port, "/mcp", method="DELETE",
                          headers={"mcp-session-id": "does-not-exist"})
        assert status in (400, 404, 405), f"unexpected {status}"
        assert _sessions(mcp) == before


class TestAdmissionBound:
    """codex P0: prevalidation cannot stop a flood of VALID initializes --
    each one legitimately creates a session, so the bound is what limits them.

    Driven through the guard with a stub inner app rather than a live server:
    a real initialize opens an SSE stream that stays open, so a live-server
    version of this test spends its time timing out on held connections
    instead of measuring admission. The RETENTION property is covered against
    the real session manager above; this covers the BOUND.
    """

    @staticmethod
    async def _drive(app, body):
        sent = []
        delivered = {"done": False}

        async def receive():
            if not delivered["done"]:
                delivered["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "headers": [(b"content-type", b"application/json"),
                             # the guard now mirrors the SDK's Accept check,
                             # so the request has to be a legitimate one for
                             # this test to be measuring ADMISSION at all
                             (b"accept", b"application/json, text/event-stream")]}
        await app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    def test_valid_initializes_are_bounded_and_the_rest_are_refused(self, monkeypatch):
        import asyncio

        reached = []

        async def inner(scope, receive, send):
            reached.append(1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 5)
        session_guard._reset_admissions()
        app = session_guard.guard_sessions(inner)
        body = json.dumps(INITIALIZE).encode()

        codes = [asyncio.run(self._drive(app, body)) for _ in range(9)]

        assert codes[:5] == [200] * 5, f"the bound refused legitimate traffic: {codes}"
        assert codes[5:] == [429] * 4, f"the bound did not engage: {codes}"
        assert len(reached) == 5, (
            f"{len(reached)} requests reached the session manager, bound was 5"
        )

    def test_the_bound_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 0)
        session_guard._reset_admissions()
        assert all(session_guard._admit() for _ in range(500))

    def test_the_window_slides_rather_than_latching(self, monkeypatch):
        """A bound that never releases would be an outage after one burst."""
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 3)
        session_guard._reset_admissions()
        assert [session_guard._admit() for _ in range(4)] == [True, True, True, False]
        # age the recorded admissions past the window instead of sleeping 60s
        session_guard._admissions[:] = [t - 61 for t in session_guard._admissions]
        assert session_guard._admit() is True, "the window latched shut"


class TestHeaderPrevalidation:
    """codex round 3: the SDK validates Accept (:452) and Content-Type (:456)
    AFTER allocating. A structurally perfect initialize with the wrong headers
    therefore still leaked a session."""

    @pytest.mark.parametrize("headers,label", [
        ({"Accept": "application/json"}, "no text/event-stream"),
        ({"Accept": "text/event-stream"}, "no application/json"),
        ({"Accept": "*/*"}, "wildcard only"),
        ({"Content-Type": "text/plain"}, "wrong content-type"),
    ])
    def test_bad_headers_retain_no_session(self, server, headers, label):
        mcp, port = server
        base = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        base.update(headers)
        status = _post(port, "/mcp", INITIALIZE, headers=base)
        assert status in (406, 415), f"{label}: unexpected {status}"
        assert _sessions(mcp) == 0, f"{label} left a session behind"

    def test_accept_with_parameters_is_still_accepted(self, server):
        """q-values are legal and must not be mistaken for a refusal."""
        mcp, port = server
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "Accept": "application/json;q=0.9, text/event-stream;q=1.0"}
        assert _post(port, "/mcp", INITIALIZE, headers=headers) == 200
        assert _sessions(mcp) == 1


class TestReceiveDelegation:
    """codex round 3 P1: after replaying the buffered body the wrapper
    returned a synthetic empty http.request FOREVER. SSE's disconnect
    listener calls receive again to wait for http.disconnect, so it would
    hot-loop on immediately-ready messages and never see the real one."""

    def test_the_second_receive_is_the_real_disconnect(self):
        import asyncio

        seen = []

        async def inner(scope, receive, send):
            seen.append(await receive())      # the replayed body
            seen.append(await receive())      # must be the UNDERLYING event
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        body = json.dumps(INITIALIZE).encode()
        events = [{"type": "http.request", "body": body, "more_body": False},
                  {"type": "http.disconnect"}]

        async def receive():
            return events.pop(0) if events else {"type": "http.disconnect"}

        async def send(msg):
            pass

        session_guard._reset_admissions()
        app = session_guard.guard_sessions(inner)
        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "headers": [(b"content-type", b"application/json"),
                             (b"accept", b"application/json, text/event-stream")]}
        asyncio.run(app(scope, receive, send))

        assert seen[0]["type"] == "http.request"
        assert seen[1]["type"] == "http.disconnect", (
            "the wrapper swallowed the disconnect; SSE would hot-loop"
        )


class TestAdmissionIsChargedOnlyWhenEligible:
    """codex round 3 P1: charging before route eligibility meant 120 requests
    to an unknown database could exhaust the window and block every real
    workspace for a minute while creating zero sessions."""

    def test_an_unknown_database_does_not_consume_the_budget(self, monkeypatch, tmp_path):
        import asyncio

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 3)
        session_guard._reset_admissions()

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        # Build the app the way PRODUCTION does, not by hand: an earlier
        # version composed make_router(guard(inner)) inside the test, so
        # reversing the order in db_routing left this test green while the
        # deployed server charged admission for unknown databases.
        from memora import db_routing

        class _FakeMCP:
            def streamable_http_app(self):
                return inner

        app = db_routing.routed_streamable_http_app(_FakeMCP())
        body = json.dumps(INITIALIZE).encode()

        async def drive(path):
            sent = []
            done = {"v": False}

            async def receive():
                if not done["v"]:
                    done["v"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(msg):
                sent.append(msg)

            scope = {"type": "http", "method": "POST", "path": path,
                     "headers": [(b"content-type", b"application/json"),
                                 (b"accept", b"application/json, text/event-stream")]}
            await app(scope, receive, send)
            return next(m["status"] for m in sent if m["type"] == "http.response.start")

        # Ten attempts at a database that does not exist.
        for _ in range(10):
            assert asyncio.run(drive("/mcp/nosuch")) == 404
        # The budget must be untouched: three real initializes still succeed.
        assert [asyncio.run(drive("/mcp/alpha")) for _ in range(3)] == [200, 200, 200], (
            "requests to an unknown database consumed the admission budget"
        )


class TestGuardConfigFailsClosed:
    @pytest.mark.parametrize("value", ["-1", "abc", "1.5"])
    def test_bad_rate_bound_is_refused(self, monkeypatch, value):
        monkeypatch.setenv("MEMORA_MAX_INIT_PER_MIN", value)
        with pytest.raises(session_guard.SessionGuardConfigError):
            session_guard._int_env("MEMORA_MAX_INIT_PER_MIN", "120", minimum=0)

    @pytest.mark.parametrize("value", ["nan", "-5", "inf", "abc"])
    def test_bad_idle_timeout_is_refused(self, monkeypatch, value):
        """NaN silently disabled reaping while reading as configured."""
        monkeypatch.setenv("MEMORA_SESSION_IDLE_TIMEOUT", value)
        with pytest.raises(session_guard.SessionGuardConfigError):
            session_guard.idle_timeout_seconds()

    def test_zero_idle_timeout_disables_reaping(self, monkeypatch):
        monkeypatch.setenv("MEMORA_SESSION_IDLE_TIMEOUT", "0")
        assert session_guard.idle_timeout_seconds() == 0


class TestIdleReapingIsRealAndWired:
    """codex round 3: idle reaping is one of the two controls claimed to bound
    valid abandoned sessions, and NOTHING tested it -- a mutation deleting the
    wiring left the whole suite green."""

    def test_the_real_manager_actually_drops_an_idle_session(self, monkeypatch, tmp_path):
        """Not just that the attribute is set -- that a session goes away."""
        import socket
        import threading
        import time as _t

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        session_guard._reset_admissions()

        mcp = FastMCP("reap-probe")
        app = session_guard.guard_sessions(mcp.streamable_http_app())
        mcp.session_manager.session_idle_timeout = 2.0

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(80):
            if getattr(srv, "started", False):
                break
            _t.sleep(0.1)

        try:
            assert _post(port, "/mcp", INITIALIZE) == 200
            assert _sessions(mcp) == 1, "the control failed: no session was created"
            for _ in range(60):
                if _sessions(mcp) == 0:
                    break
                _t.sleep(0.25)
            assert _sessions(mcp) == 0, (
                "an abandoned session was never reaped despite the idle timeout"
            )
        finally:
            srv.should_exit = True
            for _ in range(50):
                if not getattr(srv, "started", False):
                    break
                _t.sleep(0.1)


class TestNamedDatabasePathsAreGuardedToo:
    """The composition test above only proved an UNKNOWN database is not
    charged. It did NOT prove a request to a REAL one is guarded at all --
    and with the guard composed outside the router it is not: /mcp/alpha
    never matches the exact /mcp the guard watches, so it sails past into the
    session manager. Production traffic is almost entirely named paths, so
    that hole would have left the leak effectively unfixed."""

    @pytest.fixture
    def routed_server(self, monkeypatch, tmp_path):
        import socket
        import threading
        import time as _t

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        from memora.db_routing import routed_streamable_http_app

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        session_guard._reset_admissions()

        mcp = FastMCP("routed-leak-probe")
        app = routed_streamable_http_app(mcp)      # exactly what production serves

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(80):
            if getattr(srv, "started", False):
                break
            _t.sleep(0.1)
        yield mcp, port
        srv.should_exit = True
        for _ in range(50):
            if not getattr(srv, "started", False):
                break
            _t.sleep(0.1)
        storage._registry_cache = None
        storage._registry_source = None

    def test_a_bad_POST_to_a_NAMED_database_retains_no_session(self, routed_server):
        mcp, port = routed_server
        for _ in range(50):
            _post(port, "/mcp/alpha", b"{}")
        assert _sessions(mcp) == 0, (
            f"named-path requests bypassed the guard: {_sessions(mcp)} sessions"
        )

    def test_a_bare_GET_to_a_NAMED_database_retains_no_session(self, routed_server):
        """This is the exact request clmux's sidebar sends."""
        mcp, port = routed_server
        assert _request(port, "/mcp/alpha") == 406
        assert _sessions(mcp) == 0

    def test_a_real_initialize_to_a_NAMED_database_still_works(self, routed_server):
        """Control: the guard must not break the deployment's main path."""
        mcp, port = routed_server
        assert _post(port, "/mcp/alpha", INITIALIZE) == 200
        assert _sessions(mcp) == 1
