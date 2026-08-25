"""Regression tests for core storage operations."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import memora
import memora.storage as storage


def test_llm_client_passes_explicit_timeout(monkeypatch):
    """OpenAI client must be constructed with MEMORA_LLM_TIMEOUT (not SDK 600s)."""
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai

    storage._llm_client_cache.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-timeout")
    monkeypatch.setenv("MEMORA_LLM_TIMEOUT", "45")
    monkeypatch.setattr(storage, "LLM_ENABLED", True)
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

    client = storage._get_llm_client()
    assert client is not None
    assert captured.get("timeout") == 45.0, (
        "mutation: drop timeout= from OpenAI() kwargs and this assertion goes red"
    )
    storage._llm_client_cache.clear()


def test_classify_timeout_is_named_failure(monkeypatch):
    """Strict/measurement mode: timeout is a named failure, not []."""

    class Completions:
        def create(self, *args, **kwargs):
            raise TimeoutError("simulated hang")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(storage, "_get_llm_client", lambda: fake_client)
    monkeypatch.setattr(storage, "_LLM_TIMEOUT_STRICT", True)

    with pytest.raises(storage.LLMTimeoutError, match="timed out"):
        storage._classify_fact_against_matches(
            "Deployment uses version three",
            [{"id": 1, "content": "old version", "score": 0.5, "tags": []}],
        )


def test_classify_timeout_falls_back_outside_strict_mode(monkeypatch):
    """Production absorb: timeout degrades to empty classification, does not raise."""

    class Completions:
        def create(self, *args, **kwargs):
            raise TimeoutError("simulated hang")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(storage, "_get_llm_client", lambda: fake_client)
    monkeypatch.setattr(storage, "_LLM_TIMEOUT_STRICT", False)

    result = storage._classify_fact_against_matches(
        "Deployment uses version three",
        [{"id": 1, "content": "old version", "score": 0.5, "tags": []}],
    )
    assert result == ([], []), (
        "mutation: re-raise timeout unconditionally and this assert/absorb test goes red"
    )


def test_absorb_timeout_falls_back_instead_of_raising(local_db, monkeypatch):
    """Runtime absorb_memory must not raise LLMTimeoutError on a hung provider."""

    class Completions:
        def create(self, *args, **kwargs):
            raise TimeoutError("simulated hang")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(storage, "_get_llm_client", lambda: fake_client)
    monkeypatch.setattr(storage, "_LLM_TIMEOUT_STRICT", False)
    monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})

    with storage.connect() as conn:
        existing = storage.add_memory(conn, content="Deployment uses version one")
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *a, **k: [{"score": 0.5, "memory": existing}],
        )
        result = storage.absorb_memory(conn, ["Deployment uses version three"])
    assert isinstance(result, dict) and "decisions" in result
    # Timeout degraded: fact is preserved as a create/link, not an exception.


def test_resolve_follow_defaults_and_all_escape_hatch():
    assert storage.resolve_follow(None, default=storage.DEFAULT_FOLLOW_LIST) == "active"
    assert storage.resolve_follow(None, default=storage.DEFAULT_FOLLOW_GET, for_get=True) == "latest"
    assert storage.resolve_follow("all", default="active") is None
    assert storage.resolve_follow("full_history", default="active") == "full_history"
    with pytest.raises(ValueError):
        storage.resolve_follow("active", default="latest", for_get=True)
    with pytest.raises(ValueError):
        storage.resolve_follow("bogus", default="active")


def test_find_duplicate_pairs_uses_canonical_filters(local_db):
    """Duplicate counts should ignore structural memories and non-cosine links."""
    import json

    with storage.connect() as conn:
        a = storage.add_memory(conn, content="Alpha duplicate memory one", tags=["test"])
        b = storage.add_memory(conn, content="Alpha duplicate memory two", tags=["test"])
        typed = storage.add_memory(conn, content="Typed relation memory", tags=["test"])
        absorb = storage.add_memory(conn, content="Absorb link memory", tags=["test"])
        root = storage.add_memory(
            conn,
            content="Document root memory",
            tags=["test"],
            metadata={"type": "document_root"},
        )
        section = storage.add_memory(
            conn,
            content="Section placeholder memory",
            tags=["test"],
            metadata={"type": "section"},
        )

        conn.execute("DELETE FROM memories_crossrefs")
        conn.execute(
            "INSERT INTO memories_crossrefs(memory_id, related) VALUES (?, ?)",
            (
                a["id"],
                json.dumps([
                    {"id": b["id"], "score": 0.90, "edge_type": "related_to"},
                    {"id": typed["id"], "score": 0.97, "edge_type": "supersedes"},
                    {"id": absorb["id"], "score": 1.0, "edge_type": "related_to"},
                    {"id": root["id"], "score": 0.96, "edge_type": "related_to"},
                    {"id": section["id"], "score": 0.96, "edge_type": "related_to"},
                ]),
            ),
        )
        conn.execute(
            "INSERT INTO memories_crossrefs(memory_id, related) VALUES (?, ?)",
            (
                b["id"],
                json.dumps([
                    {"id": a["id"], "score": 0.91},
                ]),
            ),
        )

        result = storage.find_duplicate_pairs(conn, min_similarity=0.85, limit=None)

        assert result["total_pairs"] == 1
        assert result["affected_node_count"] == 2
        assert result["pairs"] == [{
            "memory_a_id": min(a["id"], b["id"]),
            "memory_b_id": max(a["id"], b["id"]),
            "similarity_score": 0.91,
        }]


def test_add_memory_crud(local_db):
    """Basic create/read/update/delete cycle."""
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Test CRUD memory content here", tags=["test"])
        assert mem["id"] is not None
        mid = mem["id"]

        fetched = storage.get_memory(conn, mid)
        assert fetched is not None
        assert fetched["content"] == "Test CRUD memory content here"
        assert "test" in fetched["tags"]

        updated = storage.update_memory(conn, mid, content="Updated CRUD memory content here")
        assert updated is not None
        assert updated["content"] == "Updated CRUD memory content here"

        storage.delete_memory(conn, mid)
        assert storage.get_memory(conn, mid) is None


def test_update_tags_recomputes_fts(local_db):
    """Updating tags should refresh the FTS index."""
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="FTS reindex test memory content", tags=["old-tag"])
        mid = mem["id"]

        storage.update_memory(conn, mid, tags=["new-tag-fts"])

        row = conn.execute(
            "SELECT tags FROM memories_fts WHERE rowid = ?", (mid,)
        ).fetchone()
        assert row is not None
        assert "new-tag-fts" in row[0].lower()


def test_update_tags_recomputes_embedding(local_db):
    """Updating tags should refresh the embedding."""
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Embedding reindex test memory content", tags=["alpha"])
        mid = mem["id"]

        old_emb = conn.execute(
            "SELECT embedding FROM memories_embeddings WHERE memory_id = ?", (mid,)
        ).fetchone()

        storage.update_memory(conn, mid, tags=["completely-different-tag"])

        new_emb = conn.execute(
            "SELECT embedding FROM memories_embeddings WHERE memory_id = ?", (mid,)
        ).fetchone()

        assert old_emb is not None and new_emb is not None
        assert old_emb[0] != new_emb[0]


def test_update_metadata_recomputes_embedding(local_db):
    """Updating metadata should refresh the embedding."""
    with storage.connect() as conn:
        mem = storage.add_memory(
            conn,
            content="Metadata reindex test memory content",
            tags=["meta"],
            metadata={"section": "docs"},
        )
        mid = mem["id"]

        old_emb = conn.execute(
            "SELECT embedding FROM memories_embeddings WHERE memory_id = ?", (mid,)
        ).fetchone()

        storage.update_memory(conn, mid, metadata={"section": "api-reference"})

        new_emb = conn.execute(
            "SELECT embedding FROM memories_embeddings WHERE memory_id = ?", (mid,)
        ).fetchone()
        updated = storage.get_memory(conn, mid)

        assert old_emb is not None and new_emb is not None
        assert old_emb[0] != new_emb[0]
        assert updated is not None
        assert updated["metadata"]["section"] == "api-reference"


def test_update_metadata_merges_existing_keys(local_db):
    """Partial metadata updates should preserve omitted metadata keys."""
    with storage.connect() as conn:
        mem = storage.add_memory(
            conn,
            content="Metadata merge safety test memory content",
            tags=["meta"],
            metadata={
                "type": "todo",
                "status": "open",
                "priority": "high",
                "category": "release",
            },
        )
        mid = mem["id"]

        updated = storage.update_memory(conn, mid, metadata={"status": "closed"})

        assert updated is not None
        assert updated["metadata"]["type"] == "todo"
        assert updated["metadata"]["status"] == "closed"
        assert updated["metadata"]["priority"] == "high"
        assert updated["metadata"]["category"] == "release"


def test_update_metadata_null_deletes_key(local_db):
    """Patch-style metadata updates should delete keys explicitly set to None."""
    with storage.connect() as conn:
        mem = storage.add_memory(
            conn,
            content="Metadata delete safety test memory content",
            tags=["meta"],
            metadata={"status": "closed", "closed_reason": "done"},
        )
        mid = mem["id"]

        updated = storage.update_memory(conn, mid, metadata={"closed_reason": None})

        assert updated is not None
        assert updated["metadata"]["status"] == "closed"
        assert "closed_reason" not in updated["metadata"]


def test_update_metadata_replace_option_allows_full_replacement(local_db):
    """Explicit replacement remains available for callers that need it."""
    with storage.connect() as conn:
        mem = storage.add_memory(
            conn,
            content="Metadata replace safety test memory content",
            tags=["meta"],
            metadata={"type": "todo", "status": "open", "priority": "high"},
        )
        mid = mem["id"]

        updated = storage.update_memory(
            conn,
            mid,
            metadata={"status": "closed"},
            replace_metadata=True,
        )

        assert updated is not None
        assert updated["metadata"] == {"status": "closed"}


def test_update_content_validates(local_db):
    """Updating with too-short content should raise ValueError."""
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Valid content for update validation test", tags=["test"])
        mid = mem["id"]

        try:
            storage.update_memory(conn, mid, content="hi")
            assert False, "Expected ValueError for short content"
        except ValueError:
            pass


def test_semantic_search_basic(local_db):
    """Basic semantic search should find relevant memories."""
    with storage.connect() as conn:
        storage.add_memory(conn, content="Python programming language tutorial guide", tags=["code"])
        storage.add_memory(conn, content="Recipe for chocolate cake baking dessert", tags=["cooking"])

        results = storage.semantic_search(conn, "python programming")
        assert len(results) > 0
        assert any("python" in r["memory"]["content"].lower() for r in results)


@pytest.mark.parametrize("matched_version", ["stale", "leaf"])
def test_absorb_update_supersedes_current_leaf(
    local_db, monkeypatch, caplog, matched_version
):
    """Absorb UPDATEs must extend the leaf, even when retrieval matched history."""
    with storage.connect() as conn:
        original = storage.add_memory(conn, content="Deployment uses version one")
        current = storage.add_memory(conn, content="Deployment uses version two")
        storage.add_link(conn, current["id"], original["id"], edge_type="supersedes")

        matched = original if matched_version == "stale" else current
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *args, **kwargs: [{"score": 0.5, "memory": matched}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": matched["id"],
                "relationship": "UPDATE",
                "reason": "new deployment version",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *args, **kwargs: {"x": 1.0})

        result = storage.absorb_memory(conn, ["Deployment uses version three"])

        decision = next(d for d in result["decisions"] if d["action"] == "superseded")
        new_id = decision["memory_id"]
        assert decision["target_id"] == current["id"]
        assert any(
            ref["id"] == current["id"] and ref["edge_type"] == "supersedes"
            for ref in storage.get_crossrefs(conn, new_id)
        )
        assert not any(
            ref["id"] == original["id"] and ref["edge_type"] == "supersedes"
            for ref in storage.get_crossrefs(conn, new_id)
        )
        assert ("target #" in caplog.text) is (matched_version == "stale")


def _seed_fork(conn):
    orig = storage.add_memory(conn, content="Deployment uses version one extra words")
    left = storage.add_memory(conn, content="Deployment uses version two left extra")
    right = storage.add_memory(conn, content="Deployment uses version two right extra")
    storage.add_link(conn, left["id"], orig["id"], edge_type="supersedes")
    storage.add_link(conn, right["id"], orig["id"], edge_type="supersedes")
    return orig, left, right


def test_absorb_update_collapses_fork_to_one_leaf(local_db, monkeypatch):
    with storage.connect() as conn:
        orig, left, right = _seed_fork(conn)
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *args, **kwargs: [{"score": 0.5, "memory": left}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": left["id"],
                "relationship": "UPDATE",
                "reason": "version three",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})

        preview = storage.absorb_memory(conn, ["Deployment uses version three extra"], dry_run=True)
        dec = next(d for d in preview["decisions"] if d["action"] == "supersede")
        assert set(dec["target_ids"]) == {left["id"], right["id"]}
        assert set(dec["fork_collapsed"]) == {left["id"], right["id"]}

        result = storage.absorb_memory(conn, ["Deployment uses version three extra"])
        decision = next(d for d in result["decisions"] if d["action"] == "superseded")
        new_id = decision["memory_id"]
        assert set(decision["fork_collapsed"]) == {left["id"], right["id"]}
        assert set(decision["target_ids"]) == {left["id"], right["id"]}

        latest = storage.get_memory(conn, orig["id"], follow="latest")
        assert latest["id"] == new_id
        active = storage.list_memories(conn, follow="active")
        active_ids = {m["id"] for m in active}
        assert new_id in active_ids
        assert left["id"] not in active_ids and right["id"] not in active_ids
        hist = storage.get_memory(conn, new_id, follow="full_history")
        hist_ids = {m["id"] for m in hist.get("history") or [hist]}
        assert {orig["id"], left["id"], right["id"], new_id} <= hist_ids


def test_absorb_contradict_does_not_collapse_fork(local_db, monkeypatch):
    """LOCKING INTENDED BEHAVIOR — not an accident.

    Storage follow=active / digest keep showing pre-existing fork leaves
    until the next absorb UPDATE heals them. Graph-only quarantine
    (authority_unknown on multi-leaf tips) is the approved middle scope.
    CONTRADICT never collapses.
    """
    with storage.connect() as conn:
        orig, left, right = _seed_fork(conn)
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *args, **kwargs: [{"score": 0.5, "memory": left}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": left["id"],
                "relationship": "CONTRADICT",
                "reason": "opposing claim",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        result = storage.absorb_memory(conn, ["Deployment never uses versions extra"])
        assert any(d["action"] == "contradicted" for d in result["decisions"])
        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
        assert left["id"] in active and right["id"] in active


def test_absorb_write_boundary_reresolve_prevents_refork(local_db, monkeypatch):
    with storage.connect() as conn:
        orig, left, right = _seed_fork(conn)
        real_add = storage.add_memory
        injected = {}

        def racing_add(*args, **kwargs):
            rec = real_add(*args, **kwargs)
            if kwargs.get("absorb_nonce") and "competitor" not in injected:
                competitor = real_add(
                    conn,
                    content="Competing absorb already collapsed the fork extra",
                    tags=["test"],
                )
                storage.add_link(conn, competitor["id"], left["id"], edge_type="supersedes", commit=False)
                storage.add_link(conn, competitor["id"], right["id"], edge_type="supersedes", commit=False)
                injected["competitor"] = competitor["id"]
            return rec

        monkeypatch.setattr(storage, "add_memory", racing_add)
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *args, **kwargs: [{"score": 0.5, "memory": left}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": left["id"],
                "relationship": "UPDATE",
                "reason": "version three",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        result = storage.absorb_memory(conn, ["Deployment uses version three extra"])
        decision = next(d for d in result["decisions"] if d["action"] == "superseded")
        new_id = decision["memory_id"]
        leaves, cycle = storage._component_live_leaves(conn, orig["id"])
        assert not cycle
        assert leaves == [new_id], (
            f"re-forked leaves={leaves} (mutation: skip write-boundary resolve)"
        )
        assert injected["competitor"] != new_id
        assert injected["competitor"] not in leaves


def _tombstone_rows(conn):
    return conn.execute(
        "SELECT content_hash, memory_id, reason FROM tombstones ORDER BY memory_id"
    ).fetchall()


def test_content_tombstone_hash_normalizes():
    a = storage.content_tombstone_hash("  Hello\n\tWorld  ")
    b = storage.content_tombstone_hash("hello world")
    c = storage.content_tombstone_hash("HELLO   WORLD")
    d = storage.content_tombstone_hash("hello worlds")
    assert a == b == c, (
        "mutation: drop strip/collapse-whitespace/casefold and this goes red"
    )
    assert a != d
    assert len(a) == 64


def test_delete_memory_writes_tombstone(local_db):
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Tombstone probe ALPHA extra words")
        mid = mem["id"]
        digest = storage.content_tombstone_hash(mem["content"])
        assert storage.delete_memory(conn, mid, reason="user-retracted") is True
        rows = _tombstone_rows(conn)
        assert len(rows) == 1, "mutation: skip _write_tombstone in delete_memory"
        assert rows[0]["content_hash"] == digest
        assert rows[0]["memory_id"] == mid
        assert rows[0]["reason"] == "user-retracted"
        leftover = conn.execute(
            "SELECT 1 FROM tombstones WHERE memory_id = ?", (mid,)
        ).fetchone()
        assert leftover is not None, "tombstones must survive the deleted row (no FK)"


def test_delete_retires_whole_component(local_db):
    with storage.connect() as conn:
        orig = storage.add_memory(conn, content="Component retire root extra words")
        leaf = storage.add_memory(conn, content="Component retire leaf extra words")
        storage.add_link(conn, leaf["id"], orig["id"], edge_type="supersedes")
        sibling = storage.add_memory(conn, content="Unrelated live memory extra words")

        assert storage.delete_memory(conn, leaf["id"]) is True
        tomb_ids = {row["memory_id"] for row in _tombstone_rows(conn)}
        assert {orig["id"], leaf["id"]} <= tomb_ids, (
            "mutation: tombstone only the deleted id and ancestor becomes current"
        )

        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
        assert orig["id"] not in active
        assert leaf["id"] not in active
        assert sibling["id"] in active

        assert storage.get_memory(conn, orig["id"], follow="latest") is None
        assert storage.get_memory(conn, orig["id"]) is not None
        hist = storage.get_memory(conn, orig["id"], follow="full_history")
        hist_ids = {m["id"] for m in (hist.get("history") or [hist])}
        assert orig["id"] in hist_ids
        leaves, cycle = storage._component_live_leaves(conn, orig["id"])
        assert not cycle
        assert leaves == []


def test_absorb_skips_tombstoned_hash(local_db, monkeypatch):
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Retired absorb fact extra words")
        storage.delete_memory(conn, mem["id"], reason="user-retracted")
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        preview = storage.absorb_memory(
            conn, ["  RETIRED   absorb fact extra words "], dry_run=True
        )
        preview_dec = next(d for d in preview["decisions"] if d["action"] == "tombstoned")
        assert preview_dec["reason"] == "user-retracted"
        result = storage.absorb_memory(conn, ["  RETIRED   absorb fact extra words "])
        decision = next(d for d in result["decisions"] if d["action"] == "tombstoned")
        assert decision["reason"] == "user-retracted", (
            "mutation: skip absorb hash consult and this goes red"
        )
        assert result["tombstoned"] >= 1
        created = [d for d in result["decisions"] if d.get("memory_id")]
        assert created == []
        remaining = storage.list_memories(conn, query="Retired absorb fact", follow="all")
        assert remaining == [] or all(m["id"] != mem["id"] for m in remaining)


def test_absorb_new_content_after_tombstone_creates_root(local_db, monkeypatch):
    with storage.connect() as conn:
        orig = storage.add_memory(conn, content="Retired chain root extra words")
        leaf = storage.add_memory(conn, content="Retired chain leaf extra words")
        storage.add_link(conn, leaf["id"], orig["id"], edge_type="supersedes")
        storage.delete_memory(conn, leaf["id"])
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *a, **k: [{"score": 0.5, "memory": storage.get_memory(conn, orig["id"])}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: (_ for _ in ()).throw(
                AssertionError("tombstoned match must not reach the classifier")
            ),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        result = storage.absorb_memory(conn, ["Brand new fact after retirement extra"])
        decision = next(d for d in result["decisions"] if d["action"] == "created")
        new_id = decision["memory_id"]
        assert storage.get_memory(conn, new_id) is not None
        refs = storage.get_crossrefs(conn, new_id)
        assert not any(r.get("edge_type") == "supersedes" for r in refs)


def test_absorb_write_boundary_tombstone_wins(local_db, monkeypatch):
    with storage.connect() as conn:
        target = storage.add_memory(conn, content="Race tombstone target extra words")
        real_add = storage.add_memory

        def racing_add(*args, **kwargs):
            rec = real_add(*args, **kwargs)
            if kwargs.get("absorb_nonce") and storage.get_memory(conn, target["id"]):
                storage.delete_memory(conn, target["id"], reason="raced-delete")
            return rec

        monkeypatch.setattr(storage, "add_memory", racing_add)
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *a, **k: [{"score": 0.5, "memory": target}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": target["id"],
                "relationship": "UPDATE",
                "reason": "should not land",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        result = storage.absorb_memory(conn, ["Race tombstone incoming extra words"])
        assert any(d["action"] == "tombstoned" for d in result["decisions"]), (
            "mutation: skip write-boundary tombstoned refuse and this resurrects"
        )
        assert not any(d.get("memory_id") for d in result["decisions"] if d["action"] != "tombstoned")
        assert storage.get_memory(conn, target["id"]) is None
        incoming = storage.list_memories(
            conn, query="Race tombstone incoming", follow="all"
        )
        assert incoming == [], (
            "mutation: leave the absorb insert linked and a new current appears"
        )


def test_import_skips_tombstoned_hash(local_db):
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Imported retired content extra")
        storage.delete_memory(conn, mem["id"])
        result = storage.import_memories(
            conn,
            [{"content": "imported   RETIRED content extra"}],
            strategy="append",
        )
        assert result["imported"] == 0, "mutation: skip import hash consult"
        assert result["skipped"] == 1


def test_memory_create_allowed_after_tombstone(local_db):
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content="Create after tombstone extra words")
        storage.delete_memory(conn, mem["id"])
        created = storage.add_memory(conn, content="Create after tombstone extra words")
        assert created["id"] != mem["id"]
        fetched = storage.get_memory(conn, created["id"])
        assert fetched is not None
        assert fetched["content"] == "Create after tombstone extra words"


def test_compensate_delete_skips_tombstone(local_db):
    with storage.connect() as conn:
        mem = storage.add_memory(
            conn,
            content="Compensating absorb orphan extra words",
            metadata={"absorb_nonce": "nonce-abc"},
        )
        assert storage.delete_memory(
            conn, mem["id"], require_absorb_nonce="nonce-abc"
        ) is True
        rows = _tombstone_rows(conn)
        assert rows == [], (
            "mutation: write tombstones on require_absorb_nonce deletes"
        )


def test_merge_source_writes_tombstone(local_db):
    import asyncio
    import memora.server as server

    with storage.connect() as conn:
        src = storage.add_memory(conn, content="Merge source body extra words")
        tgt = storage.add_memory(conn, content="Merge target body extra words")
        src_id, tgt_id = src["id"], tgt["id"]
    result = asyncio.run(server.memory_merge(src_id, tgt_id))
    assert result.get("merged") is True
    with storage.connect() as conn:
        rows = [
            r for r in _tombstone_rows(conn) if r["memory_id"] == src_id
        ]
        assert rows, "mutation: merge source delete skips tombstone"
        assert rows[0]["reason"] == "merged"
        assert storage.get_memory(conn, src_id) is None
        assert storage.get_memory(conn, tgt_id) is not None


def test_delete_memories_writes_tombstones(local_db):
    with storage.connect() as conn:
        a = storage.add_memory(conn, content="Batch tombstone alpha extra")
        b = storage.add_memory(conn, content="Batch tombstone beta extra")
        deleted = storage.delete_memories(conn, [a["id"], b["id"]], reason="batch-clear")
        assert deleted == 2
        ids = {row["memory_id"] for row in _tombstone_rows(conn)}
        assert {a["id"], b["id"]} <= ids
        assert all(row["reason"] == "batch-clear" for row in _tombstone_rows(conn))


def _reciprocal_supersedes(conn, newer, older):
    fwd = storage.get_crossrefs(conn, newer)
    rev = storage.get_crossrefs(conn, older)
    return (
        any(r.get("id") == older and r.get("edge_type") == "supersedes" for r in fwd)
        and any(r.get("id") == newer and r.get("edge_type") == "superseded_by" for r in rev)
    )


def test_d1_dual_writer_links_converge_to_one_leaf(fake_d1_backend):
    """Two FakeD1 connections resolve the same leaf, then both link.

    Sequential add_link + heal: must go red if heal is a no-op (two live
    leaves). Reverse-blob lost-update is covered by
    test_d1_reverse_crossref_cas_keeps_both_edges.
    """
    backend = fake_d1_backend
    with storage.connect() as setup:
        leaf = storage.add_memory(setup, content="Shared absorb leaf extra words")
        a = storage.add_memory(setup, content="Writer A new version extra words")
        b = storage.add_memory(setup, content="Writer B new version extra words")
        lid, aid, bid = leaf["id"], a["id"], b["id"]

    c1 = backend.connect()
    c2 = backend.connect()
    plan1 = storage._resolve_absorb_supersedes_target(c1, lid)
    plan2 = storage._resolve_absorb_supersedes_target(c2, lid)
    assert plan1["targets"] == [lid]
    assert plan2["targets"] == [lid]
    storage.add_link(c1, aid, lid, edge_type="supersedes", commit=False)
    storage.add_link(c2, bid, lid, edge_type="supersedes", commit=False)
    storage._heal_supersession_fork(c1, aid)
    storage._heal_supersession_fork(c2, bid)

    with storage.connect() as conn:
        leaves, cycle = storage._component_live_leaves(conn, lid)
        assert not cycle
        assert leaves == [max(aid, bid)], (
            f"mutation: skip post-link heal and both writers stay leaves={leaves}"
        )
        winner, loser = max(aid, bid), min(aid, bid)
        assert _reciprocal_supersedes(conn, winner, loser), (
            "mutation: reverse-crossref lost-update drops a reciprocal edge"
        )
        assert _reciprocal_supersedes(conn, aid, lid)
        assert _reciprocal_supersedes(conn, bid, lid)
    c1.close()
    c2.close()


def test_d1_reverse_crossref_cas_keeps_both_edges(fake_d1_backend):
    """Read/read/write/write on the contested leaf's reverse blob.

    Both writers snapshot L.related, then both write. Without the UPDATE
    guard (OR 1=1), the second overwrite drops the first writer's edge.
    Heal cannot save this: it only links winner→loser, it does not
    restore L's reverse blob. CAS must reject the stale write so the
    loser retries.
    """
    backend = fake_d1_backend
    with storage.connect() as setup:
        leaf = storage.add_memory(setup, content="CAS leaf extra words enough")
        a = storage.add_memory(setup, content="CAS writer A extra words enough")
        b = storage.add_memory(setup, content="CAS writer B extra words enough")
        lid, aid, bid = leaf["id"], a["id"], b["id"]
        # Force the UPDATE path (not INSERT): L already has a related row.
        storage._store_crossrefs(setup, lid, [])
        storage._upsert_crossref_edge(setup, aid, lid, "supersedes")
        storage._upsert_crossref_edge(setup, bid, lid, "supersedes")

    c1 = backend.connect()
    c2 = backend.connect()
    exists1, raw1, refs1 = storage._load_crossrefs_raw(c1, lid)
    exists2, raw2, refs2 = storage._load_crossrefs_raw(c2, lid)
    assert exists1 and exists2
    merged1 = [r for r in refs1 if r.get("id") != aid]
    merged1.append({"id": aid, "score": 1.0, "edge_type": "superseded_by"})
    merged2 = [r for r in refs2 if r.get("id") != bid]
    merged2.append({"id": bid, "score": 1.0, "edge_type": "superseded_by"})

    assert storage._cas_store_crossrefs(c1, lid, exists1, raw1, merged1) is True
    stale_ok = storage._cas_store_crossrefs(c2, lid, exists2, raw2, merged2)
    assert stale_ok is False, (
        "mutation: OR 1=1 on the UPDATE guard lets the stale write succeed"
    )
    storage._upsert_crossref_edge(c2, lid, bid, "superseded_by")

    with storage.connect() as conn:
        assert _reciprocal_supersedes(conn, aid, lid), (
            "mutation: OR 1=1 lost-update drops writer A's reverse edge on L"
        )
        assert _reciprocal_supersedes(conn, bid, lid), (
            "mutation: retry path never landed writer B's reverse edge"
        )
        rev_ids = {
            r["id"]
            for r in storage.get_crossrefs(conn, lid)
            if r.get("edge_type") == "superseded_by"
        }
        assert {aid, bid} <= rev_ids
    c1.close()
    c2.close()


def test_absorb_import_blocked_when_legacy_hash_insert_fails(fake_d1_backend, monkeypatch):
    """Legacy tombstones INSERT can fail; the atomic marker must still block resurrect."""
    content = "Durable marker hash probe extra words"
    with storage.connect() as conn:
        mem = storage.add_memory(conn, content=content)

        def fail_legacy_hash(sql, params):
            upper = " ".join(sql.split()).upper()
            return (
                upper.startswith("INSERT")
                and "INTO TOMBSTONES" in upper
                and "TOMBSTONE_COMPONENTS" not in upper
            )

        conn.fail_when = fail_legacy_hash
        assert storage.delete_memory(conn, mem["id"]) is True
        conn.fail_when = None
        legacy = conn.execute(
            "SELECT 1 FROM tombstones WHERE memory_id = ?", (mem["id"],)
        ).fetchone()
        assert legacy is None, "legacy hash row must be absent for this probe"
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        result = storage.absorb_memory(conn, [content])
        assert any(d["action"] == "tombstoned" for d in result["decisions"]), (
            "mutation: hash consult ignores tombstone_components.content_hash"
        )
        imported = storage.import_memories(
            conn, [{"content": content}], strategy="append"
        )
        assert imported["imported"] == 0, (
            "mutation: import still resurrects when only the marker carries the hash"
        )


def test_delete_absorb_interleave_new_leaf_not_current(fake_d1_backend):
    """Delete snapshot then absorb link: N must be retired or compensated."""
    with storage.connect() as conn:
        ancestor = storage.add_memory(conn, content="Interleave ancestor extra words")
        leaf = storage.add_memory(conn, content="Interleave leaf extra words")
        storage.add_link(conn, leaf["id"], ancestor["id"], edge_type="supersedes")
        new = storage.add_memory(conn, content="Interleave absorb new extra words")

        def after_snap(comp):
            assert leaf["id"] in comp
            assert new["id"] not in comp
            storage.add_link(conn, new["id"], leaf["id"], edge_type="supersedes")

        storage._after_component_snapshot = after_snap
        try:
            storage.delete_memory(conn, leaf["id"])
        finally:
            storage._after_component_snapshot = None

        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
        assert new["id"] not in active, (
            "mutation: no delete rewalk; absorb leaf N stays current"
        )
        leaves, _ = storage._component_live_leaves(conn, ancestor["id"])
        assert new["id"] not in leaves
        retired = storage.retired_memory_ids(conn)
        still = storage.get_memory(conn, new["id"])
        assert new["id"] in retired or still is None


def test_absorb_postlink_recheck_compensates_after_delete_markers(fake_d1_backend, monkeypatch):
    """Delete marks+rewalks complete BEFORE absorb links (target row still there).

    D1 commits the marker statement before edge-clear. Resolve-time and
    pre-link checks have already passed. The post-link recheck is the only
    defense: it must compensate N (action=tombstoned, row absent).
    Mutation: `if False:` on that recheck leaves N current.
    """
    with storage.connect() as conn:
        ancestor = storage.add_memory(conn, content="Postcheck ancestor extra words")
        leaf = storage.add_memory(conn, content="Postcheck leaf extra words")
        storage.add_link(conn, leaf["id"], ancestor["id"], edge_type="supersedes")
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *a, **k: [{"score": 0.5, "memory": leaf}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": leaf["id"],
                "relationship": "UPDATE",
                "reason": "stale resolve then delete marked",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})

        def before_link(new_id, targets):
            assert leaf["id"] in targets
            assert not storage._is_tombstoned_id(conn, leaf["id"])
            # Just-before-edge-clear: markers + rewalks, leaf row still exists
            # so add_link can succeed and the post-link recheck is reachable.
            storage._tombstone_component(
                conn, leaf["id"], reason="raced-delete",
                content_by_id={leaf["id"]: leaf["content"]},
            )
            assert storage._is_tombstoned_id(conn, leaf["id"])
            assert storage.get_memory(conn, leaf["id"]) is not None

        storage._before_absorb_supersede_links = before_link
        try:
            result = storage.absorb_memory(conn, ["Postcheck incoming extra words"])
        finally:
            storage._before_absorb_supersede_links = None

        assert any(d["action"] == "tombstoned" for d in result["decisions"]), (
            "mutation: if False on post-link recheck reports superseded"
        )
        created = [d.get("memory_id") for d in result["decisions"] if d.get("memory_id")]
        assert created == []
        leftover = storage.list_memories(
            conn, query="Postcheck incoming", follow="all"
        )
        assert leftover == [], (
            "mutation: if False on post-link recheck leaves N in the store"
        )
        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
        incoming = [m for m in storage.list_memories(conn, follow="all")
                    if "Postcheck incoming" in (m.get("content") or "")]
        assert incoming == []
        assert all("Postcheck incoming" not in (m.get("content") or "") for m in
                   storage.list_memories(conn, follow="active"))


def test_retired_memory_ids_fail_closed_on_operational_error(fake_d1_backend):
    with storage.connect() as conn:
        storage.add_memory(conn, content="Fail-closed retirement extra words")

        def fail_components(sql, params):
            return "from tombstone_components" in sql.lower()

        conn.fail_when = fail_components
        with pytest.raises(storage.RetirementIntegrityError):
            storage.retired_memory_ids(conn)
        with pytest.raises(storage.RetirementIntegrityError):
            storage.list_memories(conn, follow="active")
        conn.fail_when = None


def test_losing_absorb_reports_winner_current_id(fake_d1_backend, monkeypatch):
    backend = fake_d1_backend
    with storage.connect() as setup:
        leaf = storage.add_memory(setup, content="Concurrent absorb leaf extra words")
        lid = leaf["id"]

    monkeypatch.setattr(
        storage,
        "_search_snapshot_full",
        lambda *a, **k: [{"score": 0.5, "memory": {"id": lid, "content": "Concurrent absorb leaf extra words", "metadata": None, "tags": []}}],
    )
    monkeypatch.setattr(
        storage,
        "_classify_fact_against_matches",
        lambda fact, matches: ([{
            "memory_id": lid,
            "relationship": "UPDATE",
            "reason": "concurrent update",
        }], []),
    )
    monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})

    c1 = backend.connect()
    c2 = backend.connect()
    nested = {"done": False}
    second = {}

    def after_resolve(plan):
        if nested["done"]:
            return
        nested["done"] = True
        second["result"] = storage.absorb_memory(
            c2, ["Second concurrent absorb fact extra words"]
        )

    storage._after_absorb_resolve = after_resolve
    try:
        first = storage.absorb_memory(c1, ["First concurrent absorb fact extra words"])
    finally:
        storage._after_absorb_resolve = None

    loser = next(
        d for d in first["decisions"]
        if d["action"] in {"superseded", "concurrency_resolved"}
    )
    winner = next(
        d for d in second["result"]["decisions"]
        if d["action"] in {"superseded", "concurrency_resolved"}
    )
    winner_id = winner.get("current_id") or winner["memory_id"]
    assert loser["action"] == "concurrency_resolved", (
        "mutation: loser still reports action=superseded with its own id"
    )
    assert loser["current_id"] == winner_id
    assert loser["canonical"] == winner_id
    assert storage.get_memory(c1, loser["memory_id"]) is not None
    c1.close()
    c2.close()


def test_absorb_second_link_failure_compensates(fake_d1_backend, monkeypatch):
    """Nth add_link failure on a 3-leaf fork must not leave a partial collapse."""
    with storage.connect() as conn:
        orig = storage.add_memory(conn, content="Compensate root extra words")
        leaves = []
        for label in ("one", "two", "three"):
            leaf = storage.add_memory(conn, content=f"Compensate leaf {label} extra words")
            storage.add_link(conn, leaf["id"], orig["id"], edge_type="supersedes")
            leaves.append(leaf)
        before = {orig["id"], *(lf["id"] for lf in leaves)}
        real_add_link = storage.add_link
        seen = {"n": 0}

        def flaky_add_link(*args, **kwargs):
            edge = kwargs.get("edge_type")
            if edge is None and len(args) >= 4:
                edge = args[3]
            if edge == "supersedes":
                seen["n"] += 1
                if seen["n"] == 2:
                    raise RuntimeError("injected link fail")
            return real_add_link(*args, **kwargs)

        monkeypatch.setattr(storage, "add_link", flaky_add_link)
        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *a, **k: [{"score": 0.5, "memory": leaves[0]}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": leaves[0]["id"],
                "relationship": "UPDATE",
                "reason": "collapse three",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})
        with pytest.raises(RuntimeError, match="fork collapse failed"):
            storage.absorb_memory(conn, ["Compensate incoming extra words"])
        remaining = {m["id"] for m in storage.list_memories(conn, follow="all")}
        assert remaining == before, (
            "mutation: skip compensation and the new memory / partial edges remain"
        )
        live, cycle = storage._component_live_leaves(conn, orig["id"])
        assert not cycle
        assert set(live) == {lf["id"] for lf in leaves}


def test_component_marker_survives_nth_member_insert_failure(fake_d1_backend):
    """Nth per-member tombstone INSERT fails: every member must still be non-current."""
    with storage.connect() as conn:
        orig = storage.add_memory(conn, content="Nth fail root extra words")
        mid = storage.add_memory(conn, content="Nth fail mid extra words")
        leaf = storage.add_memory(conn, content="Nth fail leaf extra words")
        storage.add_link(conn, mid["id"], orig["id"], edge_type="supersedes")
        storage.add_link(conn, leaf["id"], mid["id"], edge_type="supersedes")
        hash_inserts = {"n": 0}

        def fail_when(sql, params):
            upper = sql.lstrip().upper()
            if "TOMBSTONE_COMPONENTS" in upper:
                return False
            if upper.startswith("INSERT") and "TOMBSTONES" in upper:
                hash_inserts["n"] += 1
                return hash_inserts["n"] >= 3
            return False

        conn.fail_when = fail_when
        try:
            storage.delete_memory(conn, leaf["id"])
        except RuntimeError as exc:
            assert "injected" in str(exc)
        conn.fail_when = None

        retired = storage.retired_memory_ids(conn)
        assert {orig["id"], mid["id"], leaf["id"]} <= retired, (
            "mutation: skip component marker; leftover ancestor stays current"
        )
        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
        assert orig["id"] not in active
        assert mid["id"] not in active
        assert leaf["id"] not in active


def test_list_active_tombstone_queries_are_per_window(fake_d1_backend):
    """FakeD1 statement count for tombstone consults is O(windows), not O(rows)."""
    n = 40
    with storage.connect() as conn:
        for i in range(n):
            storage.add_memory(conn, content=f"Windowed tombstone probe {i} extra words")
        conn.statement_count = 0
        listed = storage.list_memories(conn, follow="active")
        assert len(listed) == n
        tombstone_sql = []
        # Re-run with a wrapper that records tombstone SQL only.
        real_execute = conn.execute

        def counting_execute(sql, params=None):
            if "tombstone" in sql.lower():
                tombstone_sql.append(sql)
            return real_execute(sql, params)

        conn.execute = counting_execute
        storage.list_memories(conn, follow="active")
        assert len(tombstone_sql) <= 6, (
            f"mutation: per-row _is_tombstoned_id => {len(tombstone_sql)} queries "
            f"for {n} rows (must be O(windows))"
        )


def test_tombstone_hash_lookup_tiebreaks_on_memory_id(local_db):
    with storage.connect() as conn:
        content = "Tiebreak same hash extra words"
        digest = storage.content_tombstone_hash(content)
        conn.execute(
            "INSERT INTO tombstones(content_hash, memory_id, reason, created_at) "
            "VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            (digest, 1, "older-id", "2026-01-01 00:00:00",
             digest, 9, "newer-id", "2026-01-01 00:00:00"),
        )
        conn.commit()
        reason = storage._lookup_tombstone_by_hash(conn, content)
        assert reason == "newer-id", (
            "mutation: drop memory_id DESC tiebreak and this is non-deterministic"
        )


def test_hybrid_semantic_list_honor_requested_limit(local_db):
    token = "storelimit-zzxq"
    with storage.connect() as conn:
        for i in range(12):
            storage.add_memory(conn, content=f"{token} matching document number {i} extra words")
        hybrid = storage.hybrid_search(conn, token, top_k=3, follow="active")
        semantic = storage.semantic_search(conn, token, top_k=3, follow="active")
        listed = storage.list_memories(conn, query=token, limit=3, follow="active")
        assert len(hybrid) == 3
        assert len(semantic) == 3
        assert len(listed) == 3
        few_token = "qzxplmvw"
        storage.add_memory(
            conn, content=f"{few_token} only one extra words", tags=["rare-probe"]
        )
        few = storage.hybrid_search(
            conn, few_token, top_k=5, follow="active", tags_all=["rare-probe"]
        )
        assert len(few) == 1


def test_hybrid_search_tags_all_filters_semantic_leg(local_db):
    """Phase 0 regression: tags_all must filter both legs of hybrid_search.

    Before the fix, the semantic leg only honored metadata_filters, so fused
    results could include rows that violated tags_all.
    """
    with storage.connect() as conn:
        storage.add_memory(conn, content="Python programming language overview", tags=["a", "b"])
        storage.add_memory(conn, content="Python programming basics intro", tags=["a"])
        storage.add_memory(conn, content="Python programming advanced patterns", tags=["b"])

        results = storage.hybrid_search(conn, "python programming", tags_all=["a", "b"])

        assert len(results) >= 1
        for entry in results:
            memory = entry.get("memory", entry)
            tags = set(memory.get("tags") or [])
            assert {"a", "b"}.issubset(tags), (
                f"hybrid_search returned row with tags {tags} violating tags_all=['a','b']"
            )


def test_hybrid_search_selective_tag_filter_surfaces_matches(local_db):
    """Phase 0 regression: selective filters must still surface the matching row
    from the semantic leg even when it would lie outside the unfiltered top-k.
    """
    with storage.connect() as conn:
        # Populate many non-matching rows to push the needle below the default
        # top_k * 3 window for semantic search.
        for i in range(20):
            storage.add_memory(
                conn,
                content=f"Distractor document about python programming number {i}",
                tags=["distract"],
            )
        # The single row that should match the filter.
        storage.add_memory(
            conn,
            content="Rare needle memory for python programming query",
            tags=["needle"],
        )

        results = storage.hybrid_search(
            conn, "python programming", tags_all=["needle"], top_k=5
        )

        assert len(results) == 1, (
            f"Expected 1 needle row, got {len(results)} — filter did not push "
            f"into semantic leg"
        )
        memory = results[0].get("memory", results[0])
        assert "needle" in (memory.get("tags") or [])


def test_hybrid_search_date_filter_applies_to_semantic_leg(local_db):
    """Phase 0 regression: date_from/date_to must filter the semantic leg."""
    import sqlite3 as _sqlite3

    with storage.connect() as conn:
        old = storage.add_memory(
            conn, content="Python programming early historical note", tags=["old"]
        )
        new = storage.add_memory(
            conn, content="Python programming recent current note", tags=["new"]
        )

        # Force the old row's created_at backward so date_from filters it out.
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", old["id"]),
        )
        conn.commit()

        results = storage.hybrid_search(
            conn, "python programming", date_from="2024-01-01T00:00:00"
        )

        ids = {entry.get("memory", entry)["id"] for entry in results}
        assert new["id"] in ids
        assert old["id"] not in ids, (
            "hybrid_search returned a row older than date_from — semantic leg "
            "ignored the date filter"
        )


def test_list_memories_filtered_pagination(local_db):
    """Phase 1 regression: offset/limit must apply to filtered results, not raw SQL rows.

    Before the fix, SQL LIMIT/OFFSET ran before Python-side tag filtering,
    so filtered pagination underfilled and offset skipped wrong rows.
    """
    with storage.connect() as conn:
        # Create 20 memories: 10 matching (tags=["match"]) and 10 non-matching
        for i in range(20):
            tag = "match" if i % 2 == 0 else "nomatch"
            storage.add_memory(
                conn,
                content=f"Filtered pagination test memory number {i:02d}",
                tags=[tag],
            )

        # Without filters: 20 total. With tags_all=["match"]: 10 matching.
        all_matching = storage.list_memories(conn, tags_all=["match"], limit=-1)
        assert len(all_matching) == 10

        # Page 1: first 5 filtered results
        page1 = storage.list_memories(conn, tags_all=["match"], limit=5, offset=0)
        assert len(page1) == 5, f"Page 1 expected 5 rows, got {len(page1)}"

        # Page 2: next 5 filtered results
        page2 = storage.list_memories(conn, tags_all=["match"], limit=5, offset=5)
        assert len(page2) == 5, f"Page 2 expected 5 rows, got {len(page2)}"

        # Pages should not overlap and should cover all 10 matching rows
        page1_ids = {r["id"] for r in page1}
        page2_ids = {r["id"] for r in page2}
        assert page1_ids.isdisjoint(page2_ids), "Pages overlap"
        assert len(page1_ids | page2_ids) == 10, "Pages don't cover all matches"

        # All returned rows must have the "match" tag
        for r in page1 + page2:
            assert "match" in r["tags"], f"Row {r['id']} missing 'match' tag"


def _seed_depleted_lineage(conn):
    leaves = [
        storage.add_memory(conn, content=f"Current lineage leaf {i}")
        for i in range(1)
    ]
    active = [
        storage.add_memory(conn, content=f"Active deeper result {i}")
        for i in range(3)
    ]
    stale = [
        storage.add_memory(conn, content=f"Stale ranked result {i}")
        for i in range(3)
    ]
    for old in stale:
        storage.add_link(conn, leaves[0]["id"], old["id"], edge_type="supersedes")

    for rank, memory in enumerate(leaves):
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (f"2010-01-0{3 - rank} 00:00:00", memory["id"]),
        )
    for rank, memory in enumerate(active):
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (f"2020-01-0{3 - rank} 00:00:00", memory["id"]),
        )
    for rank, memory in enumerate(stale):
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (f"2030-01-0{3 - rank} 00:00:00", memory["id"]),
        )
    conn.commit()
    return leaves, active, stale


@pytest.mark.parametrize("follow", ["active", "latest"])
def test_list_follow_fills_limit_without_duplicates_in_rank_order(local_db, follow):
    with storage.connect() as conn:
        leaves, active, _stale = _seed_depleted_lineage(conn)

        results = storage.list_memories(conn, limit=3, follow=follow)

        ids = [memory["id"] for memory in results]
        expected = (
            [memory["id"] for memory in active]
            if follow == "active"
            else [leaves[0]["id"], active[0]["id"], active[1]["id"]]
        )
        assert len(ids) == 3, "lineage filtering depleted the requested list page"
        assert ids == expected, "list lineage processing changed stable rank order"
        assert len(ids) == len(set(ids)), "list lineage processing returned duplicates"


def test_list_follow_logs_candidate_cap_and_shortfall(
    local_db, monkeypatch, caplog
):
    """Windowed scan refills a page the old single-cap path used to empty.

    Genuine shortfall (limit > remaining followed rows) still logs.
    """
    caplog.set_level(logging.INFO)
    with storage.connect() as conn:
        _leaves, _active, _stale = _seed_depleted_lineage(conn)
        monkeypatch.setattr(storage, "_SCAN_WINDOW", 3)
        monkeypatch.setattr(storage, "_SCAN_HARD_CAP", 100)

        filled = storage.list_memories(conn, limit=2, follow="active")
        assert len(filled) == 2, "windowed continuation should refill past a 3-row window"

        short = storage.list_memories(conn, limit=20, follow="active")
        assert len(short) < 20
        assert "requested=20 delivered=" in caplog.text


@pytest.mark.parametrize("path", ["semantic", "hybrid"])
@pytest.mark.parametrize("follow", ["active", "latest"])
def test_ranked_follow_overfetch_fills_limit_without_duplicates(
    local_db, monkeypatch, path, follow
):
    with storage.connect() as conn:
        leaves, active, stale = _seed_depleted_lineage(conn)
        ranked = stale + active + leaves
        envelopes = [
            {"score": 1.0 - rank / 100, "memory": memory}
            for rank, memory in enumerate(ranked)
        ]
        requested_limits = []

        if path == "semantic":
            def fake_vector_search(*args, top_k=None, **kwargs):
                requested_limits.append(top_k)
                return envelopes[:top_k]

            monkeypatch.setattr(storage, "_search_by_vector", fake_vector_search)
            monkeypatch.setattr(storage, "_compute_embedding", lambda *args, **kwargs: {"x": 1.0})
            results = storage.semantic_search(conn, "ranked", top_k=3, follow=follow)
        else:
            def fake_semantic(*args, top_k=None, **kwargs):
                requested_limits.append(top_k)
                return envelopes[:top_k]

            monkeypatch.setattr(storage, "semantic_search", fake_semantic)
            monkeypatch.setattr(storage, "list_memories", lambda *args, **kwargs: [])
            results = storage.hybrid_search(
                conn, "ranked", semantic_weight=1.0, top_k=3, follow=follow
            )

        ids = [entry["memory"]["id"] for entry in results]
        expected = (
            [memory["id"] for memory in active]
            if follow == "active"
            else [leaves[0]["id"], active[0]["id"], active[1]["id"]]
        )
        assert requested_limits == [9], f"{path} did not over-fetch a bounded 3x pool"
        assert len(ids) == 3, f"lineage filtering depleted {path} below top_k"
        assert ids == expected, f"{path} lineage processing changed stable rank order"
        assert len(ids) == len(set(ids)), f"{path} lineage processing returned duplicates"


def _seed_superseded_head_active_tail(conn, n_stale: int, n_active: int):
    """Newest rows are superseded; current rows sit past that head."""
    leaf = storage.add_memory(conn, content="Current lineage leaf for windowed list")
    stale = [
        storage.add_memory(conn, content=f"Stale window-head memory {i} extra words")
        for i in range(n_stale)
    ]
    for old in stale:
        storage.add_link(conn, leaf["id"], old["id"], edge_type="supersedes")
    active = [
        storage.add_memory(conn, content=f"Active window-tail memory {i} extra words")
        for i in range(n_active)
    ]
    for rank, memory in enumerate(stale):
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (f"2030-01-01 00:00:{rank:02d}", memory["id"]),
        )
    conn.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?",
        ("2025-01-01 00:00:00", leaf["id"]),
    )
    for rank, memory in enumerate(active):
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (f"2020-01-01 00:00:{rank:02d}", memory["id"]),
        )
    conn.commit()
    return leaf, stale, active


@pytest.mark.parametrize("follow", ["active", "latest"])
def test_list_follow_windows_past_old_scan_cap(local_db, monkeypatch, follow):
    """Deep pages must keep scanning windows; mutation: single 5000 cap under-fills."""
    monkeypatch.setattr(storage, "_SCAN_WINDOW", 4)
    monkeypatch.setattr(storage, "_SCAN_HARD_CAP", 100)
    with storage.connect() as conn:
        leaf, _stale, active = _seed_superseded_head_active_tail(conn, n_stale=8, n_active=3)
        page = storage.list_memories(conn, limit=3, offset=0, follow=follow)
        ids = [row["id"] for row in page]
        assert len(ids) == 3, (
            "windowed follow scan failed to refill past a superseded-heavy head "
            "(mutation: restore single-window _SCAN_CAP fetch and this goes red)"
        )
        assert leaf["id"] in ids or all(i in {m["id"] for m in active} | {leaf["id"]} for i in ids)
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("follow", ["active", "latest"])
def test_list_follow_page_boundaries_do_not_repeat_or_skip(local_db, monkeypatch, follow):
    monkeypatch.setattr(storage, "_SCAN_WINDOW", 4)
    monkeypatch.setattr(storage, "_SCAN_HARD_CAP", 100)
    with storage.connect() as conn:
        _seed_superseded_head_active_tail(conn, n_stale=8, n_active=3)
        page1 = storage.list_memories(conn, limit=2, offset=0, follow=follow)
        page2 = storage.list_memories(conn, limit=2, offset=2, follow=follow)
        full = storage.list_memories(conn, limit=4, offset=0, follow=follow)
        ids1 = [row["id"] for row in page1]
        ids2 = [row["id"] for row in page2]
        ids_full = [row["id"] for row in full]
        assert ids1 + ids2 == ids_full
        assert set(ids1).isdisjoint(ids2)


def test_list_follow_fts_window_exhaustion_does_not_switch_to_like(
    local_db, monkeypatch
):
    """Empty later FTS windows are exhaustion, not a LIKE semantic switch."""
    monkeypatch.setattr(storage, "_SCAN_WINDOW", 4)
    monkeypatch.setattr(storage, "_SCAN_HARD_CAP", 100)
    token = "uniquefstokenzz"
    like_only = f"prefix{token}suffix"
    with storage.connect() as conn:
        assert storage._fts_enabled(conn)
        fts_rows = []
        for i in range(8):
            fts_rows.append(
                storage.add_memory(
                    conn,
                    content=f"Document {i} mentions {token} explicitly as a word",
                )
            )
        like_rows = []
        for i in range(3):
            like_rows.append(
                storage.add_memory(
                    conn,
                    content=f"Substring trap {i} {like_only} should not appear via FTS",
                )
            )
        windowed = storage.list_memories(
            conn, query=token, follow="active", limit=20
        )
        monkeypatch.setattr(storage, "_SCAN_WINDOW", 100)
        full = storage.list_memories(conn, query=token, follow="active", limit=20)
        windowed_ids = [row["id"] for row in windowed]
        full_ids = [row["id"] for row in full]
        like_ids = {row["id"] for row in like_rows}
        fts_ids = {row["id"] for row in fts_rows}
        assert like_ids.isdisjoint(windowed_ids), (
            "LIKE-only rows leaked into a later FTS window "
            "(mutation: restore empty-page LIKE fallback and this goes red)"
        )
        assert set(windowed_ids) == fts_ids
        assert windowed_ids == full_ids


def test_list_follow_hard_cap_errors_loudly(local_db, monkeypatch):
    monkeypatch.setattr(storage, "_SCAN_WINDOW", 3)
    monkeypatch.setattr(storage, "_SCAN_HARD_CAP", 6)
    with storage.connect() as conn:
        _seed_superseded_head_active_tail(conn, n_stale=9, n_active=1)
        with pytest.raises(storage.LineageScanLimitError, match="hard cap"):
            storage.list_memories(conn, limit=1, offset=0, follow="active")


def test_tag_whitelist_enforcement(local_db, monkeypatch):
    """Adding memory with invalid tag should raise when whitelist is active."""
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"allowed-tag"})

    with storage.connect() as conn:
        try:
            storage.add_memory(conn, content="Memory with blocked tag content here", tags=["not-allowed"])
            assert False, "Expected ValueError for invalid tag"
        except ValueError as e:
            assert "not-allowed" in str(e).lower() or "whitelist" in str(e).lower() or "allowed" in str(e).lower()


def _tag_conformance_cases():
    fixture = Path(__file__).parent / "fixtures" / "tag_policy_conformance.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _tag_conformance_cases(), ids=lambda c: c["id"])
def test_tag_policy_conformance_fixture(case):
    if case.get("check") == "length":
        length = storage.tag_code_point_length(case["tag"])
        if case["expected"]:
            assert length <= storage.MAX_TAG_LENGTH
            assert storage._validate_tags([case["tag"]]) == [case["tag"]], (
                f"{case['id']}: 100 code-point tag must be accepted "
                "(mutation: measure UTF-16 units and this goes red)"
            )
        else:
            assert length > storage.MAX_TAG_LENGTH
            with pytest.raises(ValueError, match="maximum length"):
                storage._validate_tags([case["tag"]])
        return
    allowed = storage.tag_matches_policy(case["tag"], case["policy"])
    assert allowed is case["expected"], (
        f"{case['id']}: policy={case['policy']!r} tag={case['tag']!r} "
        f"expected {case['expected']} got {allowed} "
        "(mutation: revert matcher to dot-only .* and slash cases go red)"
    )


def test_tag_length_cap_rejects_overlong(monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    too_long = "x" * (storage.MAX_TAG_LENGTH + 1)
    with pytest.raises(ValueError, match="maximum length"):
        storage._validate_tags([too_long])


def test_slash_namespace_wildcard_allows_memora_family(local_db, monkeypatch):
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"memora/*"})
    with storage.connect() as conn:
        row = storage.add_memory(
            conn, content="Slash namespace tag should pass memora/*", tags=["memora/issues"]
        )
        assert "memora/issues" in row["tags"]
