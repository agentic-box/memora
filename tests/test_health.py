"""memora #965 phase 4: health of the RUNNING server.

`memora.cli health` spawns a fresh process, so it proves a config can connect —
not that the live server can. Under one-process-serves-everything that gap is
the operator's only warning before agents go quiet.
"""
import json
import os

import pytest

from memora import health, storage
from memora.storage import CURRENT_DB


@pytest.fixture(autouse=True)
def _reset_health_state():
    """codex P1: module-global snapshot leaked BETWEEN tests and servers.

    Without this, the per-database 503 test could inherit the preceding
    degraded-beta snapshot and pass without probing its own server at all.
    """
    health._snapshot = None
    health._snapshot_at = 0.0
    health._refreshing = False
    # The refresher is a module global bound to whichever loop created it.
    # Leaking one between tests made an unrelated readiness test fail 2 runs
    # in 3 -- the same defect production would hit on a loop replacement.
    health.stop_refresher()
    yield
    health._snapshot = None
    health._snapshot_at = 0.0
    health._refreshing = False
    health.stop_refresher()
    health.stop_refresher()


class TestLivenessNeverTouchesADatabase:
    """Collapsing liveness into readiness makes a supervisor restart a healthy
    process because one remote store is slow — and a restart takes memory from
    EVERY workspace at once."""

    def test_liveness_is_ok_even_when_every_database_is_broken(self, monkeypatch):
        def explode(*a, **k):
            raise RuntimeError("every store is down")

        monkeypatch.setattr(storage, "connect", explode)
        assert health.liveness_payload()["status"] == "ok"

    def test_liveness_does_not_call_connect_at_all(self, monkeypatch):
        called = []
        monkeypatch.setattr(storage, "connect", lambda *a, **k: called.append(1))
        health.liveness_payload()
        assert called == [], "liveness opened a database connection"

    def test_liveness_reports_the_version(self):
        from memora import __version__
        assert health.liveness_payload()["version"] == __version__

    def test_uptime_is_monotonic_not_wall_clock(self):
        """codex P2: pid_uptime_hint was time.time() -- current wall clock, not
        uptime, and different on every call."""
        a = health.liveness_payload()["uptime_seconds"]
        b = health.liveness_payload()["uptime_seconds"]
        assert 0 <= a < 10 ** 6, f"looks like wall clock: {a}"
        assert b >= a


class TestReadinessIsPerDatabase:
    @pytest.fixture(autouse=True)
    def _registry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "alpha.db"),
            "beta": str(tmp_path / "beta.db"),
        }))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        for name in ("alpha", "beta"):
            token = CURRENT_DB.set(name)
            try:
                with storage.connect() as c:
                    c.commit()
            finally:
                CURRENT_DB.reset(token)
        yield
        storage._registry_cache = None
        storage._registry_source = None

    def test_every_registered_database_is_reported(self):
        payload = health.readiness_payload()
        assert set(payload["databases"]) == {"alpha", "beta"}
        assert payload["status"] == "ok"
        assert payload["default_database"] == "alpha"

    def test_probe_does_not_count_rows(self, monkeypatch):
        """codex: COUNT(*) is costlier AND unnecessary inventory leakage."""
        seen = []
        real = storage.connect

        class _Spy:
            def __init__(self, inner):
                self._i = inner

            def execute(self, sql, *a, **k):
                seen.append(sql)
                return self._i.execute(sql, *a, **k)

            def __getattr__(self, k):
                return getattr(self._i, k)

        monkeypatch.setattr(storage, "connect", lambda *a, **k: _Spy(real(*a, **k)))
        health.readiness_payload()
        assert seen, "no statement issued"
        assert not any("COUNT" in s.upper() for s in seen), seen

    def test_one_broken_database_does_not_hide_the_healthy_ones(self, monkeypatch):
        """The whole point: a slow D1 must show as THAT database degraded."""
        real = storage.connect

        def selective(*a, **k):
            if CURRENT_DB.get() == "beta":
                raise RuntimeError("beta is unreachable")
            return real(*a, **k)

        monkeypatch.setattr(storage, "connect", selective)
        payload = health.readiness_payload()

        assert payload["status"] == "degraded"
        assert payload["degraded"] == ["beta"]
        assert payload["databases"]["alpha"]["status"] == "ok", (
            "one broken store hid a healthy one"
        )
        assert payload["databases"]["beta"]["status"] == "error"

    def test_a_broken_database_does_not_raise_out_of_readiness(self, monkeypatch):
        def explode(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(storage, "connect", explode)
        payload = health.readiness_payload()          # must not raise
        assert payload["status"] == "degraded"
        assert set(payload["degraded"]) == {"alpha", "beta"}

    def test_a_broken_registry_reports_rather_than_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", "{bad")
        storage._registry_cache = None
        storage._registry_source = None
        payload = health.readiness_payload()
        assert payload["status"] == "error"


class TestUnconfiguredStillWorks:
    def test_single_database_reports_one_entry(self, monkeypatch):
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        storage._registry_cache = None
        storage._registry_source = None
        payload = health.readiness_payload()
        assert list(payload["databases"]) == ["(default)"]


class TestEndpointsOverHttp:
    """Through the real routes, not just the payload functions — a route that
    is never registered leaves every payload test green (the #969 lesson)."""

    def _serve(self, monkeypatch, tmp_path, port, break_beta=False):
        import threading
        import time

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "alpha.db"),
            "beta": str(tmp_path / "beta.db"),
        }))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        for name in ("alpha", "beta"):
            token = CURRENT_DB.set(name)
            try:
                with storage.connect() as c:
                    c.commit()
            finally:
                CURRENT_DB.reset(token)

        if break_beta:
            real = storage.connect

            def selective(*a, **k):
                if CURRENT_DB.get() == "beta":
                    raise RuntimeError("beta is unreachable")
                return real(*a, **k)

            monkeypatch.setattr(storage, "connect", selective)

        app = FastMCP("health-probe", host="127.0.0.1", port=port)
        health.register_health_routes(app)
        from memora.db_routing import make_router

        server = uvicorn.Server(uvicorn.Config(
            make_router(app.streamable_http_app()),
            host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(60):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)
        return server

    def _get(self, port, path):
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_liveness_route_answers_200(self, monkeypatch, tmp_path):
        server = self._serve(monkeypatch, tmp_path, 8881)
        try:
            status, body = self._get(8881, "/health")
        finally:
            server.should_exit = True
        assert status == 200 and body["status"] == "ok"

    def test_aggregate_readiness_is_200_even_when_degraded(self, monkeypatch, tmp_path):
        """codex P1: a 503 here makes a load balancer withdraw the WHOLE
        process because one store is degraded, taking healthy databases down
        with the broken one -- the exact blast radius the split exists to
        avoid. Aggregate /health/db is an ALERT surface, always 200."""
        server = self._serve(monkeypatch, tmp_path, 8882, break_beta=True)
        try:
            live_status, _ = self._get(8882, "/health")
            agg_status, body = self._get(8882, "/health/db")
        finally:
            server.should_exit = True

        assert live_status == 200, "a down database made the server look dead"
        assert agg_status == 200, "aggregate readiness withdrew the whole process"
        assert body["status"] == "degraded"
        assert body["degraded"] == ["beta"]
        assert body["databases"]["alpha"]["status"] == "ok"

    def test_per_database_readiness_does_503(self, monkeypatch, tmp_path):
        """Withdrawing on THIS affects only the workspaces bound to beta."""
        server = self._serve(monkeypatch, tmp_path, 8883, break_beta=True)
        try:
            ok_status, _ = self._get(8883, "/health/db/alpha")
            bad_status, _ = self._get(8883, "/health/db/beta")
            missing_status, _ = self._get(8883, "/health/db/nosuch")
        finally:
            server.should_exit = True

        assert ok_status == 200
        assert bad_status == 503
        assert missing_status == 404

    def test_unauthorised_caller_gets_no_names_counts_or_messages(self, monkeypatch, tmp_path):
        """codex P0: custom_route() is unauthenticated even when MCP auth is
        configured. Names, counts and backend exception text are inventory and
        configuration -- the same boundary Phase 2 fixed for the 404 body.

        Drives the ROUTE with a non-loopback peer. The first version of this
        test called health._redact() directly, so removing the authorisation
        branch from the route left it GREEN -- it tested the redactor, not the
        decision to use it.
        """
        import asyncio

        from mcp.server.fastmcp import FastMCP

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "alpha.db"), "beta": str(tmp_path / "beta.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        health._snapshot = None
        health._snapshot_at = 0.0

        app = FastMCP("redact-probe")
        health.register_health_routes(app)
        route = next(r for r in app._custom_starlette_routes if r.path == "/health/db")

        class _RemoteReq:
            headers = {}
            client = type("C", (), {"host": "10.0.0.7"})()

        resp = asyncio.run(route.endpoint(_RemoteReq()))
        blob = resp.body.decode()

        assert "alpha" not in blob and "beta" not in blob, f"leaked names: {blob}"
        assert "latency_ms" not in blob, f"leaked per-store detail: {blob}"
        assert "\"status\"" in blob

    def test_loopback_is_authorised_and_a_remote_peer_is_not(self):
        class _Req:
            def __init__(self, host):
                self.headers = {}
                self.client = type("C", (), {"host": host})()

        assert health._is_authorised(_Req("127.0.0.1"))
        assert not health._is_authorised(_Req("10.0.0.7"))

    def test_bearer_token_authorises_a_remote_peer(self, monkeypatch):
        monkeypatch.setenv("MEMORA_HEALTH_TOKEN", "s3cret")

        class _Req:
            def __init__(self, header):
                self.headers = {"authorization": header}
                self.client = type("C", (), {"host": "10.0.0.7"})()

        assert health._is_authorised(_Req("Bearer s3cret"))
        assert not health._is_authorised(_Req("Bearer wrong"))


class TestProductionRegistration:
    """codex P1: deleting the block from server.main() left all 10 tests green.

    Same integration gap Phase 2 fixed — and the same one that let #969's
    discovery bug survive a full review.
    """

    def _stub(self, monkeypatch):
        from memora import server

        registered = []
        monkeypatch.setattr(server, "start_graph_server", lambda *a, **k: None)
        monkeypatch.setattr(server, "connect", lambda *a, **k: type("C", (), {"close": lambda s: None})())
        monkeypatch.setattr(server.mcp, "run", lambda *a, **k: None)
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda app, **k: None)
        import memora.health as h
        monkeypatch.setattr(h, "register_health_routes",
                            lambda mcp: registered.append(mcp))
        return server, registered

    def test_streamable_http_registers_health_routes(self, monkeypatch):
        server, registered = self._stub(monkeypatch)
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        server.main(["--transport", "streamable-http"])
        assert registered, "health routes were never registered in production"

    def test_sse_registers_health_routes(self, monkeypatch):
        server, registered = self._stub(monkeypatch)
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        server.main(["--transport", "sse"])
        assert registered

    def test_stdio_does_not_register_them(self, monkeypatch):
        server, registered = self._stub(monkeypatch)
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        server.main(["--transport", "stdio"])
        assert registered == [], "stdio has no HTTP surface to register on"


class TestAsyncPropertiesActuallyHold:
    """codex P1: none of the earlier tests exercised TTL, single-flight,
    timeout, event-loop responsiveness or max staleness — the properties the
    module's docstring claims."""

    def test_one_hung_store_does_not_hide_a_healthy_one(self, monkeypatch, tmp_path):
        """The core claim. Sequential probing made a hung alpha hide beta."""
        import time as _t

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "a.db"), "beta": str(tmp_path / "b.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        monkeypatch.setattr(health, "REFRESH_DEADLINE_S", 0.5)

        real = storage.connect

        def selective(*a, **k):
            if CURRENT_DB.get() == "alpha":
                _t.sleep(5)          # hangs well past the deadline
            return real(*a, **k)

        monkeypatch.setattr(storage, "connect", selective)
        payload = health.readiness_payload()

        assert payload["databases"]["beta"]["status"] == "ok", (
            f"a hung alpha hid beta: {payload['databases']}"
        )
        assert payload["databases"]["alpha"]["status"] == "unknown"
        assert payload["databases"]["alpha"]["reason"] == "probe_timeout"

    def test_simultaneous_callers_trigger_one_refresh(self, monkeypatch, tmp_path):
        import asyncio

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
        storage._registry_cache = None
        storage._registry_source = None

        builds = []
        monkeypatch.setattr(health, "_build_snapshot",
                            lambda: builds.append(1) or {"status": "ok", "databases": {},
                                                         "degraded": [], "default_database": "a"})

        async def main():
            await asyncio.gather(*[health.readiness_payload_async() for _ in range(5)])

        asyncio.run(main())
        assert len(builds) == 1, f"single-flight broken: {len(builds)} refreshes"

    def test_unauthorised_caller_never_triggers_a_refresh(self, monkeypatch, tmp_path):
        """Single-flight caps concurrency; it does not stop an anonymous
        caller forcing one N-database fanout per TTL forever."""
        import asyncio

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
        storage._registry_cache = None
        storage._registry_source = None

        builds = []
        monkeypatch.setattr(health, "_build_snapshot", lambda: builds.append(1) or {})

        asyncio.run(health.readiness_payload_async(may_refresh=False))
        assert builds == [], "an unauthorised caller triggered a probe fanout"

    def test_a_stale_snapshot_is_never_reported_ready(self, monkeypatch):
        import asyncio
        import time as _t

        health._snapshot = {"status": "ok", "databases": {"a": {"status": "ok"}},
                            "degraded": [], "default_database": "a"}
        health._snapshot_at = _t.monotonic() - (health.MAX_STALENESS_S + 10)
        payload = asyncio.run(health.readiness_payload_async(may_refresh=False))
        assert payload["too_stale"] is True
        assert payload["status"] == "unknown", (
            "a cached OK past max staleness was still reported ready"
        )

    def test_probing_does_not_block_the_event_loop(self, monkeypatch, tmp_path):
        """A sleeping probe must not stop other coroutines running."""
        import asyncio
        import time as _t

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
        storage._registry_cache = None
        storage._registry_source = None
        monkeypatch.setattr(health, "REFRESH_DEADLINE_S", 2.0)
        monkeypatch.setattr(health, "_build_snapshot",
                            lambda: _t.sleep(0.6) or {"status": "ok", "databases": {},
                                                      "degraded": [], "default_database": "a"})

        ticks = []

        async def heartbeat():
            for _ in range(5):
                ticks.append(1)
                await asyncio.sleep(0.05)

        async def main():
            await asyncio.gather(health.readiness_payload_async(), heartbeat())

        asyncio.run(main())
        assert len(ticks) == 5, f"the event loop stalled during probing: {ticks}"


class TestConfigValidation:
    @pytest.mark.parametrize("value", ["0", "-5", "notanumber", "1e9", ""])
    def test_bad_ttl_fails_closed(self, monkeypatch, value):
        """A zero or negative TTL makes every request trigger a refresh —
        the unbounded fanout this design removes.

        Tests the validator directly rather than reloading the module:
        importlib.reload leaves `health` half-initialised when the raise fires,
        which broke every LATER test in the file. A test must not damage the
        module it is checking.
        """
        monkeypatch.setenv("MEMORA_HEALTH_TTL", value)
        if value == "":
            # empty falls back to the default and must be valid
            assert health._positive_seconds("MEMORA_HEALTH_TTL", "10", cap=3600) == 10
            return
        with pytest.raises(health.HealthConfigError):
            health._positive_seconds("MEMORA_HEALTH_TTL", "10", cap=3600)

    def test_malformed_bearer_is_refused_not_a_500(self, monkeypatch):
        monkeypatch.setenv("MEMORA_HEALTH_TOKEN", "s3cret")

        class _Req:
            headers = {"authorization": "Bearer ÿþ"}
            client = type("C", (), {"host": "10.0.0.7"})()

        assert health._is_authorised(_Req()) is False


class TestPerDatabaseRouteHonoursStaleness:
    """The payload-level staleness test did NOT red when the ROUTE's staleness
    check was removed — it tested the payload, not the decision that uses it.
    Third instance of that pattern in this phase, so it gets its own test."""

    def _route(self, name):
        from mcp.server.fastmcp import FastMCP

        app = FastMCP("stale-probe")
        health.register_health_routes(app)
        return next(r for r in app._custom_starlette_routes
                    if r.path == "/health/db/{name}")

    class _Req:
        def __init__(self, name):
            self.headers = {}
            self.path_params = {"name": name}
            self.client = type("C", (), {"host": "127.0.0.1"})()

    def test_a_stale_healthy_entry_returns_503_not_200(self, monkeypatch, tmp_path):
        import asyncio
        import time as _t

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None

        health._snapshot = {"status": "ok", "default_database": "alpha", "degraded": [],
                            "databases": {"alpha": {"status": "ok"}}}
        health._snapshot_at = _t.monotonic() - (health.MAX_STALENESS_S + 10)
        # may_refresh False so the stale snapshot is what the route sees
        monkeypatch.setattr(health, "readiness_payload_async",
                            health.readiness_payload_async)

        route = self._route("alpha")
        with monkeypatch.context() as m:
            m.setattr(health, "_is_authorised", lambda r: False)
            resp = asyncio.run(route.endpoint(self._Req("alpha")))
        assert resp.status_code == 503, (
            "a formerly healthy store stayed 200 forever behind a hung refresh"
        )

    def test_a_configured_but_unproven_name_is_503_not_404(self, monkeypatch, tmp_path):
        """404 must mean NOT CONFIGURED. Conflating it with 'not yet probed'
        tells an operator the database does not exist."""
        import asyncio

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None
        health._snapshot = {"status": "unknown", "databases": {}, "degraded": [],
                            "default_database": "alpha"}
        health._snapshot_at = 0.0

        route = self._route("alpha")
        with monkeypatch.context() as m:
            m.setattr(health, "_is_authorised", lambda r: False)
            known = asyncio.run(route.endpoint(self._Req("alpha")))
            missing = asyncio.run(route.endpoint(self._Req("nosuch")))
        assert known.status_code == 503
        assert missing.status_code == 404


class TestTimedOutProbesAreTrulyAbandoned:
    """codex P0: the previous version WAITED for the probe it had just labelled
    timed-out, because `with ThreadPoolExecutor(...)` calls shutdown(wait=True)
    on exit. A 0.5s deadline took 5.03s to return, and my test asserted the
    payload but not the ELAPSED TIME — vacuous for the property claimed."""

    def _slow_alpha(self, monkeypatch, tmp_path, sleep_s):
        import time as _t

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "a.db"), "beta": str(tmp_path / "b.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        monkeypatch.setattr(health, "REFRESH_DEADLINE_S", 0.4)
        health._inflight.clear()

        real = storage.connect

        def selective(*a, **k):
            if CURRENT_DB.get() == "alpha":
                _t.sleep(sleep_s)
            return real(*a, **k)

        monkeypatch.setattr(storage, "connect", selective)

    def test_publication_happens_at_the_deadline_not_after_the_hang(
            self, monkeypatch, tmp_path):
        import time as _t

        self._slow_alpha(monkeypatch, tmp_path, 4.0)
        started = _t.monotonic()
        payload = health.readiness_payload()
        elapsed = _t.monotonic() - started

        assert payload["databases"]["beta"]["status"] == "ok"
        assert payload["databases"]["alpha"]["reason"] == "probe_timeout"
        assert elapsed < 2.0, (
            f"returned in {elapsed:.2f}s with a 0.4s deadline: the hung probe "
            "was waited on, so a healthy store cannot be published on time"
        )
        _t.sleep(4.2)          # let the abandoned probe finish before teardown

    def test_a_hung_name_is_not_resubmitted_by_the_next_refresh(
            self, monkeypatch, tmp_path):
        """shutdown(wait=False) alone would leak one thread per refresh
        against a permanently hung store."""
        import time as _t

        self._slow_alpha(monkeypatch, tmp_path, 3.0)
        submitted = []
        real_submit = health._probe_pool.submit

        def counting(fn, name, *a, **k):
            submitted.append(name)
            return real_submit(fn, name, *a, **k)

        monkeypatch.setattr(health._probe_pool, "submit", counting)

        health.readiness_payload()          # alpha times out, future retained
        health.readiness_payload()          # second refresh

        assert submitted.count("alpha") == 1, (
            f"alpha was probed {submitted.count('alpha')} times while its "
            "previous probe was still running"
        )
        _t.sleep(3.2)


class TestTuningRelationship:
    def test_max_stale_below_ttl_is_refused(self):
        """codex P1: that window reports 503 'too stale' while no authorised
        request will schedule a refresh yet — unactionable and unclearable."""
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-c", "import memora.health"],
            env={**os.environ, "MEMORA_HEALTH_TTL": "30",
                 "MEMORA_HEALTH_MAX_STALE": "10"},
            capture_output=True, text=True,
        )
        assert out.returncode != 0
        assert "must be >=" in out.stderr, out.stderr


class TestRegistryFailureIsNotAMissingName:
    def test_broken_registry_returns_503_not_404(self, monkeypatch):
        """codex P1: 404 tells an operator the NAME is wrong when the registry
        itself is broken."""
        import asyncio

        from mcp.server.fastmcp import FastMCP

        monkeypatch.setenv("MEMORA_DATABASES", "{bad")
        storage._registry_cache = None
        storage._registry_source = None

        app = FastMCP("registry-error-probe")
        health.register_health_routes(app)
        route = next(r for r in app._custom_starlette_routes
                     if r.path == "/health/db/{name}")

        class _Req:
            headers = {}
            path_params = {"name": "alpha"}
            client = type("C", (), {"host": "10.0.0.7"})()

        resp = asyncio.run(route.endpoint(_Req()))
        assert resp.status_code == 503, "a broken registry reported a missing name"
        assert b"registry_error" in resp.body
        assert b"bad" not in resp.body, "leaked registry detail to a remote peer"


class TestReadinessRefreshesWithoutAnAuthorisedCaller:
    """memora #996: readiness refreshed ONLY for an authorised caller, and in
    the deployed shape there is none.

    Clients reach the container through the host proxy, so no peer address is
    loopback, and no MEMORA_HEALTH_TOKEN was deployed. `may_refresh=authorised`
    therefore evaluated False for every caller that exists, nothing ever
    scheduled a probe, and /health/db reported status "unknown" with zero
    databases indefinitely -- identical to what it reports when every database
    is unreachable, so the one signal it exists to give was unreadable.
    """

    def _routes(self, monkeypatch, tmp_path):
        from mcp.server.fastmcp import FastMCP

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
        storage._registry_cache = None
        storage._registry_source = None
        app = FastMCP("refresh-probe")
        health.register_health_routes(app)
        by_path = {r.path: r for r in app._custom_starlette_routes}
        return by_path["/health"], by_path["/health/db"]

    class _RemoteReq:
        """A caller the server must NOT treat as privileged: not loopback, no
        token. This is every real client in the deployed topology."""
        headers = {}
        path_params = {}
        client = type("C", (), {"host": "10.0.0.7"})()

    @staticmethod
    def _body(response):
        return json.loads(bytes(response.body).decode())

    def test_unauthorised_polling_alone_makes_readiness_report_real_databases(
            self, monkeypatch, tmp_path):
        """The bug, stated as a test: poll ONLY as an unprivileged caller and
        readiness must still stop saying "unknown"."""
        import asyncio

        _, db_route = self._routes(monkeypatch, tmp_path)
        monkeypatch.setattr(health, "REFRESH_INTERVAL_S", 0.05)

        async def scenario():
            first = self._body(await db_route.endpoint(self._RemoteReq()))
            # Nothing has been probed yet, so this call legitimately knows
            # nothing -- that part was never the defect.
            assert first["database_count"] == 0
            for _ in range(100):
                await asyncio.sleep(0.05)
                body = self._body(await db_route.endpoint(self._RemoteReq()))
                if body["database_count"]:
                    return body
            return body

        try:
            body = asyncio.run(scenario())
        finally:
            health.stop_refresher()
        assert body["database_count"] == 1, (
            "readiness never refreshed for an unprivileged caller -- the #996 bug"
        )
        assert body["status"] == "ok"
        # Still redacted: the fix is about FRESHNESS, not about widening who
        # may see names and error text.
        assert "databases" not in body

    def test_liveness_polling_alone_is_enough_to_start_it(self, monkeypatch, tmp_path):
        """The watchdog only ever polls /health. If that did not start the
        refresher, production would depend on a human curling /health/db."""
        import asyncio

        live_route, db_route = self._routes(monkeypatch, tmp_path)
        monkeypatch.setattr(health, "REFRESH_INTERVAL_S", 0.05)

        async def scenario():
            await live_route.endpoint(self._RemoteReq())
            # Observe the SNAPSHOT, never /health/db: that route starts the
            # refresher itself, so polling it would keep this test green even
            # when /health does nothing -- which is exactly how the first
            # version of this test passed under mutation.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if health._snapshot is not None:
                    return health._snapshot
            return health._snapshot

        try:
            snap = asyncio.run(scenario())
        finally:
            health.stop_refresher()
        assert snap is not None, "polling /health alone never started the refresher"
        assert set(snap["databases"]) == {"alpha"}

    def test_a_dead_refresher_is_replaced_not_inherited(self, monkeypatch):
        """A crashed task left in place would silently restore the bug: the
        flag says "running", nothing refreshes, readiness freezes."""
        import asyncio

        monkeypatch.setattr(health, "REFRESH_INTERVAL_S", 0.05)

        async def scenario():
            async def boom():
                raise RuntimeError("refresher died")

            dead = asyncio.ensure_future(boom())
            await asyncio.sleep(0.05)
            assert dead.done()
            dead.exception()  # retrieve, so the loop does not warn
            health._refresher_task = dead

            assert health.ensure_refresher() is True
            replacement = health._refresher_task
            assert replacement is not dead
            assert not replacement.done()
            replacement.cancel()

        try:
            asyncio.run(scenario())
        finally:
            health.stop_refresher()

    def test_zero_interval_disables_the_refresher(self, monkeypatch):
        """Poll-only behaviour stays available; it must not start a task."""
        import asyncio

        monkeypatch.setattr(health, "REFRESH_INTERVAL_S", 0)

        async def scenario():
            assert health.ensure_refresher() is False
            assert health._refresher_task is None

        try:
            asyncio.run(scenario())
        finally:
            health.stop_refresher()

    def test_no_running_loop_is_not_an_error(self, monkeypatch):
        """readiness_payload() is called from the CLI, with no loop at all."""
        monkeypatch.setattr(health, "REFRESH_INTERVAL_S", 15)
        assert health.ensure_refresher() is False


class TestProbeDeadlineClearsARealColdConnect:
    """memora #996 second cause: MEMORA_HEALTH_TIMEOUT defaulted to 5s while a
    COLD probe measured 6.48s in the deployed container (warm 0.20s -- the cost
    is backend construction, not the SELECT 1). Every database therefore
    published unknown/probe_timeout on a fresh process."""

    def test_the_default_deadline_clears_the_measured_cold_connect(self):
        # Measured 2026-08-21 inside the running container: 6.48s cold.
        # This is the constant the outage turned on, so it is asserted
        # directly rather than left to a comment.
        assert health.REFRESH_DEADLINE_S >= 10

    def test_a_probe_slower_than_the_deadline_is_reported_timed_out(
            self, monkeypatch, tmp_path):
        import time as _t

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None
        monkeypatch.setattr(health, "REFRESH_DEADLINE_S", 0.5)
        monkeypatch.setattr(health, "_probe_one", lambda name: (_t.sleep(2.0), {"status": "ok"})[1])

        started = _t.time()
        payload = health._build_snapshot()
        elapsed = _t.time() - started

        assert payload["databases"]["alpha"]["reason"] == "probe_timeout"
        # The deadline must actually BOUND the call. An earlier version of this
        # module waited on the very probe it had just labelled timed-out.
        assert elapsed < 1.5, f"deadline did not bound the refresh: {elapsed:.2f}s"

    def test_a_probe_inside_the_deadline_is_reported_ok(self, monkeypatch, tmp_path):
        import time as _t

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"alpha": str(tmp_path / "a.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
        storage._registry_cache = None
        storage._registry_source = None
        monkeypatch.setattr(health, "REFRESH_DEADLINE_S", 2.0)
        monkeypatch.setattr(health, "_probe_one",
                            lambda name: (_t.sleep(0.3), {"status": "ok", "latency_ms": 300.0})[1])

        payload = health._build_snapshot()
        assert payload["status"] == "ok"
        assert payload["databases"]["alpha"]["status"] == "ok"


class TestRefreshIntervalConfig:
    @pytest.mark.parametrize("value", ["-1", "notanumber", "1e9"])
    def test_bad_interval_fails_closed(self, monkeypatch, value):
        monkeypatch.setenv("MEMORA_HEALTH_REFRESH_INTERVAL", value)
        with pytest.raises(health.HealthConfigError):
            health._interval_seconds("MEMORA_HEALTH_REFRESH_INTERVAL", "15", cap=3600)

    def test_zero_is_legal_and_means_disabled(self, monkeypatch):
        monkeypatch.setenv("MEMORA_HEALTH_REFRESH_INTERVAL", "0")
        assert health._interval_seconds("MEMORA_HEALTH_REFRESH_INTERVAL", "15", cap=3600) == 0

    def test_an_interval_that_cannot_beat_max_staleness_is_refused(self):
        """A refresher slower than MAX_STALE can never keep a verdict usable:
        readiness would report "unknown" on a schedule while healthy. Refusing
        the config is better than running a refresher that cannot work."""
        import subprocess
        import sys

        env = dict(os.environ)
        env["MEMORA_HEALTH_MAX_STALE"] = "30"
        env["MEMORA_HEALTH_REFRESH_INTERVAL"] = "30"
        env.pop("MEMORA_HEALTH_TTL", None)
        proc = subprocess.run(
            [sys.executable, "-c", "import memora.health"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0
        assert "MEMORA_HEALTH_REFRESH_INTERVAL" in proc.stderr
