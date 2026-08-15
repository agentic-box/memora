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
            "_search_by_vector",
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
            "_search_by_vector",
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
            "_search_by_vector",
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
    with storage.connect() as conn:
        orig, left, right = _seed_fork(conn)
        monkeypatch.setattr(
            storage,
            "_search_by_vector",
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
            "_search_by_vector",
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
