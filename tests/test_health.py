"""memora #965 phase 4: health of the RUNNING server.

`memora.cli health` spawns a fresh process, so it proves a config can connect —
not that the live server can. Under one-process-serves-everything that gap is
the operator's only warning before agents go quiet.
"""
import json

import pytest

from memora import health, storage
from memora.storage import CURRENT_DB


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
