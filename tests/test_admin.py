"""/admin/* auth (local-primary plan §9 item (a)).

The admin routes (memora/admin.py, L2) take MEMORA_ADMIN_TOKEN only, through
memora/admin_auth.py installed with admin.set_admin_auth: not the health
token, and not a loopback peer (which /health/db does trust).
"""
import json
import os
import re

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from memora import admin, storage
from memora.admin_auth import AdminConfigError, install_admin_auth
from memora.health import register_health_routes

ADMIN = "A" * 48
HEALTH = "H" * 48


@pytest.fixture(autouse=True)
def _isolate():
    # /health/db starts the module-global refresher on the TestClient's loop;
    # it must not outlive the test (see tests/test_health.py). The auth hook
    # is module-global too.
    from memora import health

    health.stop_refresher()
    saved = admin._admin_auth
    yield
    health.stop_refresher()
    admin.set_admin_auth(saved)
    storage.set_store_refusals({})


def _app(client=("198.51.100.7", 40000)):
    mcp = FastMCP("admin-test")
    register_health_routes(mcp)
    install_admin_auth(mcp)
    admin.register_admin_routes(mcp)
    return mcp, TestClient(mcp.streamable_http_app(), client=client)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
        {"local": os.path.join(os.environ["MEMORA_DATA_DIR"], "local.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "local")
    monkeypatch.setenv("MEMORA_HEALTH_TOKEN", HEALTH)
    storage.set_store_refusals({"local": "/data is not a mount point"})


def _get(client, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.get("/admin/data-volume", headers=headers)


class TestAdminAuth:
    def test_the_admin_token_is_accepted(self, registry, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
        _, client = _app()
        r = _get(client, ADMIN)
        assert r.status_code == 200
        assert r.json()["stores"]["local"] == {"needs_data_volume": True,
                                               "refused": "/data is not a mount point"}

    @pytest.mark.parametrize("presented", [None, "", "wrong" * 10, HEALTH, ADMIN + "x", ADMIN[:-1]])
    def test_anything_else_is_401_without_detail(self, registry, monkeypatch, presented):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
        _, client = _app()
        r = _get(client, presented)
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_non_ascii_header_is_401_not_500(self, registry, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
        _, client = _app()
        r = client.get("/admin/data-volume",
                       headers={"Authorization": "Bearer é".encode("latin-1")})
        assert r.status_code == 401

    def test_loopback_is_not_trusted(self, registry, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
        _, client = _app(client=("127.0.0.1", 40000))
        assert _get(client).status_code == 401
        # contrast: the health detail route does trust loopback
        assert "databases" in client.get("/health/db").json()

    def test_the_health_token_opens_health_but_not_admin(self, registry, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
        _, client = _app()
        auth = {"Authorization": f"Bearer {HEALTH}"}
        assert "databases" in client.get("/health/db", headers=auth).json()
        assert _get(client, HEALTH).status_code == 401

    def test_unset_token_disables_admin(self, registry, monkeypatch):
        monkeypatch.delenv("MEMORA_ADMIN_TOKEN", raising=False)
        _, client = _app(client=("127.0.0.1", 40000))
        for token in (None, HEALTH, ADMIN):
            r = _get(client, token)
            assert r.status_code == 403
            assert r.json() == {"error": "admin_disabled"}


class TestAdminConfig:
    def test_a_short_token_is_refused_at_startup(self, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", "short")
        with pytest.raises(AdminConfigError, match="at least"):
            install_admin_auth(FastMCP("t"))

    def test_a_non_ascii_token_is_refused(self, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", "é" * 40)
        with pytest.raises(AdminConfigError, match="ASCII"):
            install_admin_auth(FastMCP("t"))

    def test_a_token_equal_to_the_health_token_is_refused(self, monkeypatch):
        monkeypatch.setenv("MEMORA_HEALTH_TOKEN", ADMIN)
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
        with pytest.raises(AdminConfigError, match="differ"):
            install_admin_auth(FastMCP("t"))

    def test_a_refused_config_leaves_the_placeholder_in_place(self, monkeypatch):
        monkeypatch.setenv("MEMORA_ADMIN_TOKEN", "short")
        before = admin._admin_auth
        with pytest.raises(AdminConfigError):
            install_admin_auth(FastMCP("t"))
        assert admin._admin_auth is before


def _concrete(path):
    return re.sub(r"\{[^}]+\}", lambda m: "1" if "id" in m.group(0) else "local", path)


@pytest.mark.parametrize("token", [None, HEALTH, "B" * 48])
def test_every_admin_route_refuses_without_the_admin_token(registry, monkeypatch, token):
    """Walk every registered /admin/* route and method (L2's freeze, intents
    and reconcile, and L2a's data-volume): without the admin token each one
    answers 401 before doing anything. Any route added later without the
    require_admin check fails here."""
    monkeypatch.setenv("MEMORA_ADMIN_TOKEN", ADMIN)
    called = []
    for fn in ("freeze_store", "thaw_store", "list_intents", "accept_intent"):
        monkeypatch.setattr(admin, fn, lambda *a, _fn=fn, **k: called.append(_fn) or (200, {}))
    mcp, client = _app(client=("127.0.0.1", 40000))
    routes = [r for r in mcp._custom_starlette_routes if r.path.startswith("/admin/")]
    assert {r.path for r in routes} >= {"/admin/freeze/{name}", "/admin/intents/{name}",
                                        "/admin/reconcile/{name}/{intent_id}", "/admin/data-volume"}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for r in routes:
        for method in sorted(r.methods - {"HEAD"}):
            resp = client.request(method, _concrete(r.path), headers=headers, json={})
            assert resp.status_code == 401, (method, r.path, resp.status_code)
    assert called == [], f"handlers ran without the admin token: {called}"
