"""memora #965 phase 1: named database registry + per-context backend.

Phase 1 is plumbing only — nothing selects a database yet. The load-bearing
properties are therefore (a) it is a NO-OP when unconfigured, and (b) every bad
configuration fails CLOSED rather than quietly using one store for everything.
"""
import json

import pytest

from memora import storage
from memora.storage import (
    CURRENT_DB,
    DatabaseRegistryError,
    backend_for,
    current_backend,
    database_registry,
    default_database_name,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MEMORA_DATABASES", raising=False)
    monkeypatch.delenv("MEMORA_DEFAULT_DB", raising=False)
    storage._registry_cache = None
    storage._registry_source = None
    yield
    storage._registry_cache = None
    storage._registry_source = None


class TestUnconfiguredIsANoOp:
    def test_registry_empty_and_default_none(self):
        assert database_registry() == {}
        assert default_database_name() is None

    def test_current_backend_is_the_module_default(self):
        assert current_backend() is storage.STORAGE_BACKEND

    def test_monkeypatched_storage_backend_still_wins(self, monkeypatch):
        """The compatibility contract: every existing test patches this."""
        sentinel = object()
        monkeypatch.setattr(storage, "STORAGE_BACKEND", sentinel)
        assert current_backend() is sentinel


class TestRegistryParsing:
    def test_parses_names_to_uris(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"memora": "d1://acct/aaa", "scratch": "/tmp/s.db"}))
        assert database_registry() == {"memora": "d1://acct/aaa", "scratch": "/tmp/s.db"}

    def test_mixed_local_and_remote_is_supported(self, monkeypatch, tmp_path):
        """Backend-agnostic by construction — a stated requirement, not luck.

        parse_backend_uri dispatches on SCHEME, so a registry may mix a local
        path with a d1:// entry. The D1 branch demands a Cloudflare token, so
        the test supplies a fake one — the point is which BACKEND CLASS each
        URI resolves to, not that the credential works.
        """
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-token-for-parsing-only")
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"remote": "d1://acct/aaa", "local": str(tmp_path / "local.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "local")
        local = backend_for("local")
        remote = backend_for("remote")
        assert type(local) is not type(remote), (
            f"both resolved to {type(local).__name__}; mixed backends not routable"
        )


class TestBadConfigurationFailsClosed:
    def test_malformed_json_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", "{not json")
        with pytest.raises(DatabaseRegistryError):
            database_registry()

    def test_non_object_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", '["a","b"]')
        with pytest.raises(DatabaseRegistryError):
            database_registry()

    def test_empty_uri_raises(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": "   "}))
        with pytest.raises(DatabaseRegistryError):
            database_registry()

    def test_unknown_default_raises_and_names_the_known_set(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": "/tmp/a.db"}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "typo")
        with pytest.raises(DatabaseRegistryError) as exc:
            default_database_name()
        assert "typo" in str(exc.value) and "a" in str(exc.value)

    def test_ambiguous_default_raises(self, monkeypatch):
        """Two databases and no MEMORA_DEFAULT_DB must not silently pick one."""
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"a": "/tmp/a.db", "b": "/tmp/b.db"}))
        with pytest.raises(DatabaseRegistryError):
            default_database_name()

    def test_single_entry_needs_no_explicit_default(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"only": "/tmp/only.db"}))
        assert default_database_name() == "only"

    def test_unknown_name_never_falls_through_to_the_default(self, monkeypatch):
        """The failure this feature must never have: serving the wrong store."""
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": "/tmp/a.db"}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
        with pytest.raises(DatabaseRegistryError):
            backend_for("does-not-exist")


class TestContextBinding:
    def test_bound_database_overrides_the_module_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"bound": str(tmp_path / "bound.db")}))
        token = CURRENT_DB.set("bound")
        try:
            picked = current_backend()
            assert picked is not storage.STORAGE_BACKEND
            assert picked is backend_for("bound")
        finally:
            CURRENT_DB.reset(token)

    def test_binding_is_released_and_default_returns(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"bound": str(tmp_path / "bound.db")}))
        token = CURRENT_DB.set("bound")
        CURRENT_DB.reset(token)
        assert current_backend() is storage.STORAGE_BACKEND

    def test_backend_is_cached_per_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"x": str(tmp_path / "x.db")}))
        assert backend_for("x") is backend_for("x")

    def test_cache_does_not_survive_a_registry_change(self, monkeypatch, tmp_path):
        """A stale cache would serve the OLD database after a reconfiguration."""
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"x": str(tmp_path / "one.db")}))
        first = backend_for("x")
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"x": str(tmp_path / "two.db")}))
        assert backend_for("x") is not first
