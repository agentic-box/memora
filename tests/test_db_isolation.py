"""memora #965 phase 3: databases must not leak into each other.

THE LOAD-BEARING PHASE. Separation is currently GUARANTEED by four separate
processes with four separate configs — nothing in the code has to be correct for
ob1 to stay out of bestation. A single routed server trades that for separation
guaranteed only by CODE, so a leak here is worse than the limitation it
replaces.

These assert leakage is IMPOSSIBLE across every subsystem that could carry an id
or a row from one store to another, including a MIXED pair (local SQLite +
FakeD1) because the two backends have genuinely different semantics — D1
autocommits every statement and has no real transactions.
"""
import json

import pytest

from memora import storage
from memora.storage import CURRENT_DB

from .conftest import FakeD1Backend


@pytest.fixture
def two_databases(monkeypatch, tmp_path):
    """A registry with a LOCAL and a FAKE-D1 entry, both schema-initialised."""
    from memora.backends import LocalSQLiteBackend

    local = LocalSQLiteBackend(tmp_path / "alpha.db")
    faked1 = FakeD1Backend(tmp_path / "beta.db")
    backends = {"alpha": local, "beta": faked1}

    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(storage, "backend_for", lambda name: backends[name])
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(
        {"alpha": str(tmp_path / "alpha.db"), "beta": str(tmp_path / "beta.db")}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "alpha")
    import memora
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())

    for name in backends:
        token = CURRENT_DB.set(name)
        try:
            with storage.connect() as c:
                c.commit()
        finally:
            CURRENT_DB.reset(token)
    return backends


def _in(name):
    """Open a connection bound to `name`."""
    class _Ctx:
        def __enter__(self):
            self.token = CURRENT_DB.set(name)
            self.conn = storage.connect()
            return self.conn

        def __exit__(self, *exc):
            self.conn.close()
            CURRENT_DB.reset(self.token)
    return _Ctx()


class TestRowsDoNotLeak:
    def test_a_write_to_alpha_is_invisible_from_beta(self, two_databases):
        with _in("alpha") as c:
            storage.add_memory(c, content="alpha only secret content", tags=["x"])
        with _in("beta") as c:
            rows = storage.list_memories(c, limit=-1)
        assert rows == [], f"beta saw alpha's rows: {rows}"

    def test_each_database_sees_only_its_own(self, two_databases):
        with _in("alpha") as c:
            storage.add_memory(c, content="written into alpha", tags=["x"])
        with _in("beta") as c:
            storage.add_memory(c, content="written into beta", tags=["x"])
        with _in("alpha") as c:
            a = [r["content"] for r in storage.list_memories(c, limit=-1)]
        with _in("beta") as c:
            b = [r["content"] for r in storage.list_memories(c, limit=-1)]
        assert a == ["written into alpha"]
        assert b == ["written into beta"]


class TestSearchDoesNotLeak:
    def test_semantic_search_never_returns_another_database(self, two_databases):
        with _in("alpha") as c:
            storage.add_memory(c, content="quantum entanglement research notes", tags=["x"])
        with _in("beta") as c:
            hits = storage.semantic_search(c, "quantum entanglement", top_k=10)
        assert hits == [], f"beta's search returned alpha rows: {hits}"

    def test_text_query_never_returns_another_database(self, two_databases):
        with _in("alpha") as c:
            storage.add_memory(c, content="distinctive alpha phrase zebra", tags=["x"])
        with _in("beta") as c:
            hits = storage.list_memories(c, query="zebra", limit=-1)
        assert hits == []


class TestLinksAndLineageDoNotLeak:
    def test_a_link_cannot_target_an_id_from_another_database(self, two_databases):
        """The dangerous shape: ids are small integers and BOTH stores use them.

        Memory 1 exists in alpha AND in beta. A crossref or supersedes edge
        written in one must never resolve against the other.
        """
        with _in("alpha") as c:
            storage.add_memory(c, content="alpha memory number one", tags=["x"])
            storage.add_memory(c, content="alpha memory number two", tags=["x"])
            storage.add_link(c, 2, 1, edge_type="supersedes")
        with _in("beta") as c:
            storage.add_memory(c, content="beta memory number one", tags=["x"])
            refs = storage.get_crossrefs(c, 1)
        assert refs == [], f"beta's memory 1 inherited alpha's edges: {refs}"

    def test_supersession_filtering_is_per_database(self, two_databases):
        with _in("alpha") as c:
            storage.add_memory(c, content="alpha memory number one", tags=["x"])
            storage.add_memory(c, content="alpha memory number two", tags=["x"])
            storage.add_link(c, 2, 1, edge_type="supersedes")
            active_a = storage.list_memories(c, limit=-1, follow="active")
        with _in("beta") as c:
            storage.add_memory(c, content="beta memory number one", tags=["x"])
            active_b = storage.list_memories(c, limit=-1, follow="active")
        assert len(active_a) == 1, "alpha's own supersession did not apply"
        assert len(active_b) == 1, "beta's row was filtered by alpha's edge"


class TestRetirementDoesNotLeak:
    def test_tombstones_are_per_database(self, two_databases):
        """A retirement in alpha must not retire beta's memory 1.

        The first version of this test asserted BOTH sets were empty, which is
        true whether or not isolation works -- it survived the broken-routing
        mutation untouched. It now actually retires something in alpha and
        asserts the sets DIFFER.
        """
        with _in("alpha") as c:
            storage.add_memory(c, content="alpha memory to retire", tags=["x"])
            c.execute(
                "INSERT INTO tombstone_components (memory_id, content_hash) VALUES (?, ?)",
                (1, "hash-for-alpha-1"),
            )
            c.commit()
            retired_a = storage.retired_memory_ids(c)
        with _in("beta") as c:
            storage.add_memory(c, content="beta memory stays live", tags=["x"])
            retired_b = storage.retired_memory_ids(c)
        assert retired_a == {1}, "alpha's own tombstone did not register"
        assert retired_b == set(), (
            f"beta's memory 1 was retired by alpha's tombstone: {retired_b}"
        )


class TestDedupDoesNotLeak:
    def test_identical_content_in_another_database_is_not_a_duplicate(self, two_databases):
        """Absorb must not dedup against a store the caller is not using."""
        text = "the same fact written into both databases verbatim"
        with _in("alpha") as c:
            storage.add_memory(c, content=text, tags=["x"])
        with _in("beta") as c:
            result = storage.absorb_memory(c, [text], source="manual",
                                           confidence=1.0, context=None,
                                           metadata=None, tags=["x"], dry_run=False)
            rows = storage.list_memories(c, limit=-1)

        # Asserting only len(rows) == 1 was VACUOUS: under broken routing both
        # writes land in one store, absorb DEDUPS against the existing copy,
        # and the count is 1 either way. Assert absorb actually CREATED, which
        # is the behaviour that differs.
        created = result.get("created", 0) if isinstance(result, dict) else 0
        skipped = result.get("skipped", 0) if isinstance(result, dict) else 0
        assert created == 1, (
            f"absorb did not create in beta (created={created}, skipped={skipped}); "
            f"it deduped against alpha's copy: {result}"
        )
        assert len(rows) == 1
