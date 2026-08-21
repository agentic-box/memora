"""memora #997: a session must be able to say WHICH database it is bound to.

A valid-but-wrong database name in a workspace's .mcp.json is otherwise
undetectable -- every tool works, reads succeed, and writes land silently in
another project's store. These tests are written from the WRONG-binding
direction on purpose: asserting that the right store reports the right name
passes even if the field is hardcoded, and would prove nothing.
"""
import json

import pytest

from memora import storage
from memora.storage import CURRENT_DB, bound_database


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({
        "alpha": str(tmp_path / "alpha.db"),
        "beta": str(tmp_path / "beta.db"),
    }))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    storage._registry_cache = None
    storage._registry_source = None
    yield
    storage._registry_cache = None
    storage._registry_source = None


class TestBoundDatabaseReportsTheTruth:
    def test_a_session_bound_to_the_WRONG_store_says_so(self, registry):
        """The drift case. A workspace that believes it is 'beta' but was
        configured onto 'alpha' must get 'alpha' back, so the mismatch with
        its expectation is visible. If this returned the expectation, or a
        constant, drift would stay silent -- which is the bug."""
        # Bind to the NON-DEFAULT store on purpose. An earlier version of this
        # test bound to "alpha", which is also MEMORA_DEFAULT_DB, so a bug that
        # returned the default instead of the real binding passed it -- the
        # exact silent-drift bug the test exists to catch.
        expected_by_the_workspace = "alpha"
        token = CURRENT_DB.set("beta")            # what the .mcp.json actually did
        try:
            reported = bound_database()
        finally:
            CURRENT_DB.reset(token)

        assert reported["database"] == "beta", (
            "reported the expected/default store instead of the real binding"
        )
        assert reported["database"] != expected_by_the_workspace, (
            "a misconfigured workspace could not detect it was on the wrong store"
        )
        assert reported["database_source"] == "path"

    def test_each_binding_reports_its_own_name(self, registry):
        """Guards against a constant: two different bindings must differ."""
        seen = {}
        for name in ("alpha", "beta"):
            token = CURRENT_DB.set(name)
            try:
                seen[name] = bound_database()["database"]
            finally:
                CURRENT_DB.reset(token)
        assert seen == {"alpha": "alpha", "beta": "beta"}

    def test_the_default_is_reported_as_a_DIFFERENT_KIND_of_fact(self, registry):
        """"I asked for alpha" and "I said nothing and got alpha" are not the
        same claim, and only the first is an assertion a workspace can make."""
        explicit = None
        token = CURRENT_DB.set("alpha")
        try:
            explicit = bound_database()
        finally:
            CURRENT_DB.reset(token)
        defaulted = bound_database()          # bare /mcp: nothing bound

        assert explicit["database"] == defaulted["database"] == "alpha"
        assert explicit["database_source"] == "path"
        assert defaulted["database_source"] == "registry_default"

    def test_unconfigured_single_store_reports_no_name(self, monkeypatch):
        monkeypatch.delenv("MEMORA_DATABASES", raising=False)
        storage._registry_cache = None
        storage._registry_source = None
        assert bound_database() == {"database": None, "database_source": "unconfigured"}

    def test_it_does_not_enumerate_the_other_databases(self, registry):
        """#985/#996: naming the caller's own store leaks nothing it did not
        already spell in its URL. Naming the OTHERS would be inventory."""
        token = CURRENT_DB.set("alpha")
        try:
            payload = json.dumps(bound_database())
        finally:
            CURRENT_DB.reset(token)
        assert "beta" not in payload


class TestIdentityReachesTheToolResult:
    """Through the real route, not just the helper: a field that never makes
    it into memory_stats is invisible to the agent that needs it."""

    def test_memory_stats_carries_the_bound_identity(self, registry, tmp_path):
        import asyncio

        from memora import server

        for name in ("alpha", "beta"):
            token = CURRENT_DB.set(name)
            try:
                with storage.connect() as c:
                    c.commit()
            finally:
                CURRENT_DB.reset(token)

        async def stats_for(name):
            token = CURRENT_DB.set(name)
            try:
                return await server.memory_stats()
            finally:
                CURRENT_DB.reset(token)

        a = asyncio.run(stats_for("alpha"))
        b = asyncio.run(stats_for("beta"))
        assert a["database"] == "alpha" and a["database_source"] == "path"
        assert b["database"] == "beta"
        # the statistics themselves must still be there
        assert "total_memories" in a
