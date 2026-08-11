"""Regression tests for core storage operations."""

import logging

import pytest

import memora
import memora.storage as storage


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
    caplog.set_level(logging.INFO)
    with storage.connect() as conn:
        _leaves, _active, _stale = _seed_depleted_lineage(conn)
        monkeypatch.setattr(storage, "_SCAN_CAP", 3)

        results = storage.list_memories(conn, limit=2, follow="active")

        assert results == []
        assert "candidate scan reached cap=3" in caplog.text
        assert "requested=2 delivered=0 candidate_window=3" in caplog.text


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


def test_tag_whitelist_enforcement(local_db, monkeypatch):
    """Adding memory with invalid tag should raise when whitelist is active."""
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"allowed-tag"})

    with storage.connect() as conn:
        try:
            storage.add_memory(conn, content="Memory with blocked tag content here", tags=["not-allowed"])
            assert False, "Expected ValueError for invalid tag"
        except ValueError as e:
            assert "not-allowed" in str(e).lower() or "whitelist" in str(e).lower() or "allowed" in str(e).lower()
