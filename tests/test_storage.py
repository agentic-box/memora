"""Regression tests for core storage operations."""

import memora
import memora.storage as storage


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
            conn, content="Metadata reindex test memory content",
            tags=["meta"], metadata={"section": "docs"}
        )
        mid = mem["id"]

        old_emb = conn.execute(
            "SELECT embedding FROM memories_embeddings WHERE memory_id = ?", (mid,)
        ).fetchone()

        storage.update_memory(conn, mid, metadata={"section": "api-reference"})

        new_emb = conn.execute(
            "SELECT embedding FROM memories_embeddings WHERE memory_id = ?", (mid,)
        ).fetchone()

        assert old_emb is not None and new_emb is not None
        assert old_emb[0] != new_emb[0]


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


def test_tag_whitelist_enforcement(local_db, monkeypatch):
    """Adding memory with invalid tag should raise when whitelist is active."""
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"allowed-tag"})

    with storage.connect() as conn:
        try:
            storage.add_memory(conn, content="Memory with blocked tag content here", tags=["not-allowed"])
            assert False, "Expected ValueError for invalid tag"
        except ValueError as e:
            assert "not-allowed" in str(e).lower() or "whitelist" in str(e).lower() or "allowed" in str(e).lower()
