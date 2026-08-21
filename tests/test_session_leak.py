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

    app = session_guard.guard_sessions(mcp.streamable_http_app(),
                                       **session_guard.session_wiring(mcp))
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
            # Must carry mcp-session-id: the guard now attributes the outcome
            # from the response, and a 200 without it means "no session was
            # created", which is correctly refunded.
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"mcp-session-id", b"stub")]})
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
        assert all(session_guard._admit() is not None for _ in range(500))

    def test_the_window_slides_rather_than_latching(self, monkeypatch):
        """A bound that never releases would be an outage after one burst."""
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 3)
        session_guard._reset_admissions()
        # _admit returns a request-scoped TOKEN now, or None when full
        assert [session_guard._admit() is not None for _ in range(4)] == [True, True, True, False]
        # age the recorded admissions past the window instead of sleeping 60s
        # admissions are (timestamp, sequence) tokens now
        session_guard._admissions[:] = [(t - 61, n) for t, n in session_guard._admissions]
        assert session_guard._admit() is not None, "the window latched shut"


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
        # 400 comes from the SDK's own TransportSecurityMiddleware, which
        # validates Content-Type BEFORE the transport's Accept check; 406 is
        # the Accept refusal. Mirroring the SDK means inheriting its statuses,
        # and the property under test is the session count either way.
        assert status in (400, 406, 415), f"{label}: unexpected {status}"
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

        class _FakeManager:
            _server_instances: dict = {}

        class _FakeMCP:
            # session_wiring reads these; a fake without them silently broke
            # this test when the guard grew a ceiling and a security mirror.
            session_manager = _FakeManager()
            settings = type("S", (), {"transport_security": None})()

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
        app = session_guard.guard_sessions(mcp.streamable_http_app(),
                                           **session_guard.session_wiring(mcp))
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


class TestHardSessionCeiling:
    """codex round 4 P0: a creation RATE plus an idle timeout does not BOUND
    retained sessions. The manager refreshes a known session's idle deadline
    BEFORE validating the request, so an attacker who keeps the ids can hold
    every session alive with cheap rejected requests -- about two per second
    for a thousand sessions -- and keep creating more at the rate limit. Only
    a ceiling checked before allocation bounds it."""

    @pytest.fixture
    def capped(self, monkeypatch, tmp_path):
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
        monkeypatch.setattr(session_guard, "MAX_SESSIONS", 3)

        mcp = FastMCP("cap-probe")
        app = session_guard.guard_sessions(mcp.streamable_http_app(),
                                           **session_guard.session_wiring(mcp))
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

    def test_sessions_stop_at_the_ceiling_however_many_are_attempted(self, capped):
        mcp, port = capped
        codes = [_post(port, "/mcp", INITIALIZE) for _ in range(8)]
        assert codes[:3] == [200, 200, 200], f"the ceiling refused real traffic: {codes}"
        assert codes[3:] == [503] * 5, f"the ceiling did not hold: {codes}"
        assert _sessions(mcp) == 3, (
            f"retained {_sessions(mcp)} sessions against a ceiling of 3"
        )

    def test_keeping_old_sessions_alive_does_not_buy_new_capacity(self, capped):
        """The attack itself: hold the existing sessions active and try again."""
        mcp, port = capped
        for _ in range(3):
            assert _post(port, "/mcp", INITIALIZE) == 200
        held = list(mcp.session_manager._server_instances.keys())
        assert len(held) == 3

        for sid in held:                      # cheap keepalives on known ids
            _request(port, "/mcp", method="GET", headers={"mcp-session-id": sid})
        assert _post(port, "/mcp", INITIALIZE) == 503
        assert _sessions(mcp) == 3, "keepalives bought new session capacity"

    # test_capacity_returns_when_a_session_goes_away was REMOVED, not fixed.
    # It popped mcp.session_manager._server_instances by hand, which codex
    # correctly called out as hiding the real behaviour: an actual DELETE
    # terminates the transport and the SDK then leaves the entry in the map,
    # so the hand-popped version passed while ordinary clients could still
    # wedge the server at 503. TestDeleteReturnsCapacityForReal covers this
    # with a real DELETE against a real manager.

    def test_a_zero_ceiling_disables_the_cap(self, capped, monkeypatch):
        mcp, port = capped
        monkeypatch.setattr(session_guard, "MAX_SESSIONS", 0)
        codes = [_post(port, "/mcp", INITIALIZE) for _ in range(5)]
        assert codes == [200] * 5, f"the disabled cap still refused: {codes}"


class TestTransportSecurityIsMirrored:
    """codex round 4 P1: TransportSecurityMiddleware.validate_request runs at
    streamable_http.py:382 -- AFTER allocation -- and enforces Host/Origin
    when DNS-rebinding protection is on, which is FastMCP's default. A fully
    typed initialize with a forged Host allocated a session and then took a
    421, staying retained until idle expiry."""

    @pytest.mark.parametrize("headers,label", [
        ({"Host": "evil.example"}, "forged Host"),
        ({"Origin": "http://evil.example"}, "disallowed Origin"),
    ])
    def test_a_rejected_host_or_origin_retains_no_session(self, server, headers, label):
        mcp, port = server
        base = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        base.update(headers)
        status = _post(port, "/mcp", INITIALIZE, headers=base)
        assert status in (400, 403, 421), f"{label}: unexpected {status}"
        assert _sessions(mcp) == 0, f"{label} left a session behind"


class TestAdmissionIsRefundedWhenNoSessionResults:
    """codex round 4 P1: admission was charged before FastMCP authentication,
    so unauthenticated-but-well-formed initializes could consume the whole
    minute budget and lock out authorised clients while creating zero
    sessions. Charging optimistically and refunding when no session appears
    covers auth and every other downstream refusal without the guard having to
    know what they are."""

    def test_a_downstream_refusal_gives_the_slot_back(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 2)
        session_guard._reset_admissions()
        count = {"n": 0}

        async def refusing_inner(scope, receive, send):
            # stands in for auth rejecting before the manager allocates
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        app = session_guard.guard_sessions(refusing_inner,
                                           session_count=lambda: count["n"])
        body = json.dumps(INITIALIZE).encode()

        async def drive():
            sent = []
            done = {"v": False}

            async def receive():
                if not done["v"]:
                    done["v"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(msg):
                sent.append(msg)

            scope = {"type": "http", "method": "POST", "path": "/mcp",
                     "headers": [(b"content-type", b"application/json"),
                                 (b"accept", b"application/json, text/event-stream")]}
            await app(scope, receive, send)
            return next(m["status"] for m in sent if m["type"] == "http.response.start")

        # Ten refusals against a budget of two: without refunding, the budget
        # is gone after two and everything later is 429 instead of 401.
        codes = [asyncio.run(drive()) for _ in range(10)]
        assert codes == [401] * 10, f"the budget was consumed by refusals: {codes}"


class TestTheCeilingHoldsUnderConcurrency:
    """codex round 5 P0, reproduced by them with cap=1 and two requests: the
    ceiling read session_count() and then AWAITED before anything was
    reserved, so N simultaneous initializes all saw the same pre-burst count
    and all allocated. A serial test cannot catch this."""

    def test_a_concurrent_burst_cannot_exceed_the_ceiling(self, monkeypatch, tmp_path):
        import asyncio

        monkeypatch.setattr(session_guard, "MAX_SESSIONS", 1)
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 0)
        session_guard._reset_admissions()

        created = {"n": 0}
        start = None

        async def slow_inner(scope, receive, send):
            # Allocation is not instantaneous in the SDK either; the await is
            # exactly the window the unreserved check left open.
            await asyncio.sleep(0.05)
            created["n"] += 1
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"mcp-session-id", b"x")]})
            await send({"type": "http.response.body", "body": b""})

        app = session_guard.guard_sessions(slow_inner,
                                           session_count=lambda: created["n"])
        body = json.dumps(INITIALIZE).encode()

        async def one():
            sent = []
            done = {"v": False}

            async def receive():
                if not done["v"]:
                    done["v"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(msg):
                sent.append(msg)

            await start.wait()
            scope = {"type": "http", "method": "POST", "path": "/mcp",
                     "headers": [(b"content-type", b"application/json"),
                                 (b"accept", b"application/json, text/event-stream")]}
            await app(scope, receive, send)
            return next(m["status"] for m in sent if m["type"] == "http.response.start")

        async def burst():
            nonlocal start
            start = asyncio.Event()
            tasks = [asyncio.create_task(one()) for _ in range(8)]
            await asyncio.sleep(0.05)
            start.set()                      # release them together
            return await asyncio.gather(*tasks)

        codes = asyncio.run(burst())
        assert codes.count(200) == 1, f"the ceiling was exceeded by a burst: {codes}"
        assert created["n"] == 1, f"{created['n']} sessions created against a cap of 1"
        assert session_guard._pending == 0, "a reservation leaked"


class TestDeleteReturnsCapacityForReal:
    """codex round 5 P1: the earlier capacity test popped the private map by
    hand, which hid the actual behaviour. On DELETE the SDK terminates the
    transport but the manager only deletes the map entry when it is NOT
    terminated -- so a cleanly closed session was counted forever and ordinary
    clients could drive the service to a permanent 503."""

    @pytest.fixture
    def capped(self, monkeypatch, tmp_path):
        import socket
        import threading
        import time as _t

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        session_guard._reset_admissions()
        monkeypatch.setattr(session_guard, "MAX_SESSIONS", 1)

        mcp = FastMCP("delete-probe")
        app = session_guard.guard_sessions(mcp.streamable_http_app(),
                                           **session_guard.session_wiring(mcp))
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

    def test_an_explicit_DELETE_frees_a_slot(self, capped):
        import time as _t

        mcp, port = capped
        assert _post(port, "/mcp", INITIALIZE) == 200
        sid = next(iter(mcp.session_manager._server_instances))
        assert _post(port, "/mcp", INITIALIZE) == 503, "the ceiling did not engage"

        assert _request(port, "/mcp", method="DELETE",
                        headers={"mcp-session-id": sid}) in (200, 204)

        for _ in range(40):
            if _post(port, "/mcp", INITIALIZE) == 200:
                return
            _t.sleep(0.1)
        pytest.fail("DELETE never returned capacity; clients can wedge the server at 503")


class TestRefundIsAttributedToTheRightRequest:
    """codex round 5 P1: refunding by count delta credited whoever happened to
    finish, and popping 'the newest admission' refunded somebody else's slot.
    Under concurrency a refused request could keep its charge while a
    successful one gave its own back."""

    def test_concurrent_success_and_refusal_each_settle_correctly(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(session_guard, "MAX_SESSIONS", 0)
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 50)
        session_guard._reset_admissions()

        async def mixed_inner(scope, receive, send):
            # odd requests succeed with a session, even ones are refused
            n = scope["headers"][-1][1]
            if n == b"ok":
                await asyncio.sleep(0.02)
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"mcp-session-id", b"s")]})
            else:
                await asyncio.sleep(0.01)
                await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        charged = {"ok": [], "no": []}
        real_admit = session_guard._admit
        current = {"kind": None}

        def recording_admit():
            token = real_admit()
            if token is not None:
                charged[current["kind"]].append(token)
            return token

        monkeypatch.setattr(session_guard, "_admit", recording_admit)

        app = session_guard.guard_sessions(mixed_inner)
        body = json.dumps(INITIALIZE).encode()

        async def one(kind):
            done = {"v": False}

            async def receive():
                if not done["v"]:
                    done["v"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(msg):
                pass

            scope = {"type": "http", "method": "POST", "path": "/mcp",
                     "headers": [(b"content-type", b"application/json"),
                                 (b"accept", b"application/json, text/event-stream"),
                                 (b"x-kind", kind)]}
            current["kind"] = "ok" if kind == b"ok" else "no"
            await app(scope, receive, send)

        async def run():
            kinds = [b"ok" if i % 2 else b"no" for i in range(20)]
            await asyncio.gather(*(one(k) for k in kinds))

        asyncio.run(run())
        # COUNTING IS NOT ENOUGH: refunding "the newest admission" also leaves
        # exactly 10 entries, so a count assertion passes with the attribution
        # bug fully present -- it did, under mutation. The surviving tokens
        # must be the ones the SUCCESSFUL requests charged.
        assert len(session_guard._admissions) == 10, (
            f"charges settled wrong: {len(session_guard._admissions)} held, expected 10"
        )
        assert set(session_guard._admissions) == set(charged["ok"]), (
            "the wrong requests' slots survived: refunds were not attributable"
        )
        assert not (set(session_guard._admissions) & set(charged["no"])), (
            "a refused request kept its charge"
        )


class TestTerminatedTransportsArePurged:
    """codex round 6 P0: excluding terminated transports from the COUNT left
    them in the map. Every initialize+DELETE cycle strongly referenced another
    transport, so a client could grow the map for the life of the process --
    the original OOM class, just paced by the rate limit instead of the probe
    rate. The earlier DELETE test asserted only that capacity came back; it
    never looked at the retained map size, which is the thing that OOMs."""

    @pytest.fixture
    def live(self, monkeypatch, tmp_path):
        import socket
        import threading
        import time as _t

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        session_guard._reset_admissions()
        monkeypatch.setattr(session_guard, "MAX_INIT_PER_MIN", 0)

        mcp = FastMCP("purge-probe")
        app = session_guard.guard_sessions(mcp.streamable_http_app(),
                                           **session_guard.session_wiring(mcp))
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

    def test_repeated_initialize_and_DELETE_does_not_grow_the_map(self, live):
        import time as _t

        mcp, port = live
        instances = mcp.session_manager._server_instances

        for cycle in range(12):
            assert _post(port, "/mcp", INITIALIZE) == 200, f"cycle {cycle} failed"
            sid = next(s for s, t in instances.items()
                       if not getattr(t, "is_terminated", False))
            assert _request(port, "/mcp", method="DELETE",
                            headers={"mcp-session-id": sid}) in (200, 204)
            _t.sleep(0.05)

        # One more initialize forces the purge, then nothing terminated may
        # remain: 12 cycles must not leave 12 corpses referenced.
        assert _post(port, "/mcp", INITIALIZE) == 200
        assert len(instances) <= 2, (
            f"{len(instances)} transports retained after 12 initialize+DELETE "
            "cycles -- terminated sessions are still being kept"
        )

    def test_the_purge_runs_even_when_the_ceiling_is_disabled(self, live, monkeypatch):
        """The garbage must not depend on the cap being switched on."""
        import time as _t

        monkeypatch.setattr(session_guard, "MAX_SESSIONS", 0)
        mcp, port = live
        instances = mcp.session_manager._server_instances

        for _ in range(6):
            assert _post(port, "/mcp", INITIALIZE) == 200
            sid = next(s for s, t in instances.items()
                       if not getattr(t, "is_terminated", False))
            _request(port, "/mcp", method="DELETE", headers={"mcp-session-id": sid})
            _t.sleep(0.05)

        assert _post(port, "/mcp", INITIALIZE) == 200
        assert len(instances) <= 2, (
            f"{len(instances)} retained with the ceiling disabled -- the purge "
            "is gated on the cap"
        )
