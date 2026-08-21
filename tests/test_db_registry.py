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

    def test_releasing_a_binding_returns_the_REGISTRY_default(self, monkeypatch, tmp_path):
        """With a registry configured, unbound means the registry's default.

        NOT the legacy module backend: falling back to it while a registry is
        configured is the fail-open codex found — a broken or ambiguous
        MEMORA_DATABASES would silently open the legacy database.
        """
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"bound": str(tmp_path / "bound.db")}))
        token = CURRENT_DB.set("bound")
        CURRENT_DB.reset(token)
        assert current_backend() is backend_for("bound")
        assert current_backend() is not storage.STORAGE_BACKEND

    def test_unconfigured_release_returns_the_legacy_default(self):
        """With NO registry, the legacy path is untouched — the no-op contract."""
        token = CURRENT_DB.set(None)
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


class TestFailClosedOnTheRealPath:
    """codex P1: the contract must hold where connections are MADE.

    Before this, connect()/current_backend() never called the validation
    helpers, so malformed or ambiguous MEMORA_DATABASES silently opened the
    LEGACY database instead of failing.
    """

    def test_malformed_registry_cannot_open_the_legacy_db(self, monkeypatch):
        monkeypatch.setenv("MEMORA_DATABASES", "{bad")
        with pytest.raises(DatabaseRegistryError):
            current_backend()

    def test_ambiguous_default_cannot_open_the_legacy_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"a": str(tmp_path / "a.db"), "b": str(tmp_path / "b.db")}))
        with pytest.raises(DatabaseRegistryError):
            current_backend()

    def test_duplicate_database_names_fail_closed(self, monkeypatch):
        """json last-wins would silently pick ONE store for an ambiguous map."""
        monkeypatch.setenv("MEMORA_DATABASES", '{"x":"/tmp/a.db","x":"/tmp/b.db"}')
        with pytest.raises(DatabaseRegistryError) as exc:
            database_registry()
        assert "more than once" in str(exc.value)


class TestConcurrentResolution:
    def test_first_resolution_returns_one_instance(self, monkeypatch, tmp_path):
        """codex P1: two threads missing the cache each built a backend.

        Duplicate D1 backends split the shared latest-bookmark state, so a write
        through the discarded instance need not advance the one later calls use.

        The barrier aligns the two threads BEFORE the call and the constructor
        is slowed, rather than barriering inside the constructor: under the
        lock only one thread ever reaches the constructor, so an in-constructor
        barrier deadlocks waiting for a second party that correctly never
        arrives. (It did, on the first version of this test.)
        """
        import threading
        import time

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"x": str(tmp_path / "x.db")}))
        real = storage.parse_backend_uri
        built = []
        built_lock = threading.Lock()

        def slow(uri):
            time.sleep(0.25)          # widen the unlocked race window
            obj = real(uri)
            with built_lock:
                built.append(obj)
            return obj

        monkeypatch.setattr(storage, "parse_backend_uri", slow)

        ready = threading.Barrier(2, timeout=5)
        out = {}

        def go(i):
            ready.wait()
            out[i] = backend_for("x")

        threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert out[0] is out[1], f"two instances for one name: {out}"
        assert len(built) == 1, f"constructed {len(built)} backends for one name"


class TestBindingSurvivesTheWorkerOffload:
    """codex: this is the only real execution path for the new ContextVar.

    Every storage call runs on a worker thread since #968. contextvars are
    copied on the async caller and replayed with ctx.run inside the worker, so
    the binding should propagate — but 'should' is not evidence, and Phase 2
    will depend on it entirely.
    """

    def test_bound_backend_is_observed_inside_in_worker(self, monkeypatch, tmp_path):
        import asyncio

        from memora import server

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
            {"bound": str(tmp_path / "bound.db")}))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "bound")

        def observe():
            return current_backend()

        async def main():
            token = CURRENT_DB.set("bound")
            try:
                return await server._in_worker(observe)
            finally:
                CURRENT_DB.reset(token)

        seen = asyncio.run(main())
        assert seen is backend_for("bound"), (
            "the CURRENT_DB binding did not reach the worker thread; every "
            "storage call runs there, so Phase 2 routing would silently use "
            "the wrong database"
        )


class TestEmbeddingCacheKeyFollowsTheBinding:
    """codex P1: two bound local databases must not share an integrity cache key.

    _store_cache_key read storage.STORAGE_BACKEND, so under CURRENT_DB=alpha and
    =beta both keyed from the SAME legacy module backend — equal epochs could
    then reuse another database's cached audit result. Reached by semantic
    search and integrity verification the moment Phase 2 binds a session.
    """

    def test_two_bound_databases_get_distinct_cache_keys(self, monkeypatch, tmp_path):
        from memora import embeddings

        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
            "alpha": str(tmp_path / "alpha.db"),
            "beta": str(tmp_path / "beta.db"),
        }))
        monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")

        class _NoPragma:
            def execute(self, *a, **kw):
                raise AssertionError("must key from the bound backend, not a PRAGMA")

        keys = {}
        for name in ("alpha", "beta"):
            token = CURRENT_DB.set(name)
            try:
                keys[name] = embeddings._store_cache_key(_NoPragma())
            finally:
                CURRENT_DB.reset(token)

        assert keys["alpha"] != keys["beta"], (
            f"both databases share cache key {keys['alpha']!r}; one could reuse "
            "the other's cached integrity result"
        )
