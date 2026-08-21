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

    def test_readiness_route_returns_503_when_a_database_is_down(self, monkeypatch, tmp_path):
        """A probe reading only the status CODE must still learn the truth."""
        server = self._serve(monkeypatch, tmp_path, 8882, break_beta=True)
        try:
            live_status, _ = self._get(8882, "/health")
            ready_status, body = self._get(8882, "/health/db")
        finally:
            server.should_exit = True

        assert live_status == 200, "a down database made the server look dead"
        assert ready_status == 503
        assert body["degraded"] == ["beta"]
        assert body["databases"]["alpha"]["status"] == "ok"
