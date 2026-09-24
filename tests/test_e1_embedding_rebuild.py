"""E1 (production, nuc8 v0.5.0): the embedding fingerprint identifies the
MODEL and REPRESENTATION only -- backend|model|repr, never the endpoint host
-- and old host-bearing stamps compare equal when those match; a search
never rebuilds embeddings on any backend (a repairable mismatch is logged
once, shown in /health/db and repaired only by the explicit
memory_rebuild_embeddings tool)."""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import pytest

import memora
from memora import admin, embeddings, storage

OLD_HOST_TFIDF = "tfidf|tfidf|100.85.27.63:11434|sparse"  # the pre-E1 format, the old M1's host


def _stamp_value(fingerprint, audit):
    return embeddings.integrity_stamp_value(embeddings.integrity_stamp(audit, fingerprint))


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "re.db"
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"re": str(path)}))
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    storage._EMBEDDING_REPAIR_NEEDED.clear()
    token = storage.CURRENT_DB.set("re")
    conn = storage.connect()
    for i in range(3):
        storage.add_memory(conn, content=f"apples and pears number {i}", metadata={}, tags=[])
    conn.commit()
    conn.close()
    yield path
    storage.CURRENT_DB.reset(token)


def _meta(path, key, value):
    db = sqlite3.connect(path)
    if value is None:
        db.execute("DELETE FROM memories_meta WHERE key = ?", (key,))
    else:
        db.execute("INSERT INTO memories_meta (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()
    db.close()


def _rebuild_spy(monkeypatch):
    calls = []
    real = embeddings.rebuild_all_embeddings
    monkeypatch.setattr(storage, "_rebuild_all_embeddings", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    return calls


def _search(n=1):
    out = []
    for _ in range(n):
        conn = storage.connect()
        try:
            embeddings.invalidate_embedding_integrity_cache(conn)
            out.append(storage.semantic_search(conn, "apples", top_k=2))
        finally:
            conn.close()
    return out


def _audit(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return embeddings.audit_embedding_integrity(db)
    finally:
        db.close()


# ------------------------------------------------------------------ the fingerprint

@pytest.mark.parametrize("fp, expected", [
    ("openai|bge-m3|100.85.27.63:11434|dense:1024", "openai|bge-m3|dense:1024"),
    ("openai|bge-m3|dense:1024", "openai|bge-m3|dense:1024"),
    ("tfidf|tfidf|default-host|sparse", "tfidf|tfidf|sparse"),
    ("tfidf", "tfidf"),
    (None, None),
])
def test_normalize_fingerprint_drops_only_the_host(fp, expected):
    assert embeddings.normalize_fingerprint(fp) == expected


def test_the_current_fingerprint_carries_no_host(monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://100.118.64.23:11434/v1")
    assert embeddings.current_embedding_fingerprint("openai", observed_dim=1024) == "openai|bge-m3|dense:1024"
    assert embeddings.current_embedding_fingerprint("tfidf") == "tfidf|tfidf|sparse"


def test_a_host_move_alone_is_not_a_mismatch_for_dense_vectors(tmp_path, monkeypatch):
    """The production case: bge-m3 stamped via the OLD M1, served via the new one."""
    from memora import schema

    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://100.118.64.23:11434/v1")
    db = sqlite3.connect(tmp_path / "m.db")
    db.row_factory = sqlite3.Row
    schema.ensure_schema(db)
    for i in (1, 2):
        db.execute("INSERT INTO memories (id, content) VALUES (?, 'x')", (i,))
        embeddings.upsert_embedding(db, i, {str(j): 0.1 for j in range(8)})
    embeddings.set_stored_embedding_model(db, "openai|bge-m3|100.85.27.63:11434|dense:8")
    embeddings.verify_embedding_integrity(db)
    db.commit()
    assert embeddings.check_embedding_model_mismatch(db, "openai") is False
    embeddings.set_stored_embedding_model(db, "openai|text-embedding-3-small|100.118.64.23:11434|dense:8")
    embeddings.verify_embedding_integrity(db)
    db.commit()
    assert embeddings.check_embedding_model_mismatch(db, "openai") is True, "a MODEL change still is one"


# ------------------------------------------------------------------ symptom 1: the seeded local store

def test_a_seeded_store_with_old_host_stamps_neither_warns_nor_rebuilds(store, monkeypatch, caplog):
    """`re` after the cutover: embedding_model from the new host, D1's
    integrity stamp still from the old one -- as the seed copied them."""
    _meta(store, "embedding_model", "tfidf|tfidf|100.118.64.23:11434|sparse")
    _meta(store, "embedding_integrity", _stamp_value(OLD_HOST_TFIDF, _audit(store)))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"re": "d1://acct/db"}))
    calls = _rebuild_spy(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="memora.storage"):
        results = _search(3)
    assert calls == [] and all(len(r) == 2 for r in results)
    assert not [r for r in caplog.records if "embedding integrity" in r.getMessage()]
    assert "embeddings" not in (admin.gate_health("re") or {})


# ------------------------------------------------------------------ no implicit rebuild, anywhere

@pytest.mark.parametrize("replicated", [True, False])
def test_a_real_model_mismatch_is_reported_once_and_never_rebuilt_from_a_search(store, monkeypatch, caplog,
                                                                                  replicated):
    real_mismatch = "openai|text-embedding-3-small|dense:1536"
    _meta(store, "embedding_model", real_mismatch)
    _meta(store, "embedding_integrity", _stamp_value(real_mismatch, _audit(store)))
    if replicated:
        monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"re": "d1://acct/db"}))
    calls = _rebuild_spy(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="memora.storage"):
        for _ in range(3):
            with pytest.raises(storage.SearchUnavailable, match="model_mismatch"):
                _search()
    assert calls == [], "never from a search"
    notes = [r.getMessage() for r in caplog.records if "NOT rebuilt from a search" in r.getMessage()]
    assert len(notes) == 1 and "memory_rebuild_embeddings" in notes[0] and "replicates to D1" in notes[0]
    health = admin.gate_health("re")["embeddings"]["repair_needed"]
    assert health["reason"] == "model_or_representation_mismatch" and "memory_rebuild_embeddings" in health["action"]


def test_the_explicit_rebuild_repairs_and_clears_the_health_note(store, monkeypatch):
    _meta(store, "embedding_model", "openai|text-embedding-3-small|dense:1536")
    with pytest.raises(storage.SearchUnavailable):
        _search()
    assert "embeddings" in admin.gate_health("re")
    conn = storage.connect()
    try:
        assert storage.rebuild_embeddings(conn) == 3  # what memory_rebuild_embeddings runs
    finally:
        conn.close()
    assert "embeddings" not in admin.gate_health("re")
    assert all(len(r) == 2 for r in _search(2))


def test_a_store_never_audited_searches_its_comparable_vectors_without_a_rebuild(store, monkeypatch):
    _meta(store, "embedding_integrity", None)
    _meta(store, "embedding_model", embeddings.current_embedding_fingerprint("tfidf"))
    calls = _rebuild_spy(monkeypatch)
    assert all(len(r) == 2 for r in _search(2)) and calls == []


# ------------------------------------------------------------------ symptom 2: the D1 stores

@pytest.fixture
def d1(tmp_path, monkeypatch):
    from memora import backends
    from tests.l3_fakes import FakeReplica
    from tests.test_l9a_shadow import D1Send

    replica = FakeReplica(tmp_path / "d1.db")
    send = D1Send(replica)
    monkeypatch.setattr(backends.D1Connection, "_send", lambda self, sql, params=None: send(self, sql, params))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"bestation": "d1://acct/db1"}))
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    storage._EMBEDDING_REPAIR_NEEDED.clear()
    token = storage.CURRENT_DB.set("bestation")
    conn = storage.connect()
    for i in range(3):
        storage.add_memory(conn, content=f"apples and pears number {i}", metadata={}, tags=[])
    conn.commit()
    conn.close()
    yield replica
    storage.CURRENT_DB.reset(token)


def _d1_meta(replica, key, value):
    db = replica._db()
    db.execute("INSERT INTO memories_meta (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()
    db.close()


def _d1_meta_get(replica, key):
    db = replica._db()
    try:
        row = db.execute("SELECT value FROM memories_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def test_bestation_old_host_stamp_and_a_leftover_lease_compare_equal_no_rebuild(d1, monkeypatch, caplog):
    """bestation after the M1 move: embedding_model and the stamp from the old
    host, a rebuild lease left by the 13:17 search. It must simply compare
    equal: searches work, nothing is written, nothing is logged."""
    _d1_meta(d1, "embedding_model", OLD_HOST_TFIDF)
    _d1_meta(d1, "embedding_integrity", _stamp_value(OLD_HOST_TFIDF, _audit(d1.path)))
    lease = f"1f6469d5-leftover|{int(time.time())}"
    _d1_meta(d1, "embedding_rebuild_lease", lease)
    calls = _rebuild_spy(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="memora.storage"):
        results = _search(3)
    assert calls == [] and all(len(r) == 2 for r in results)
    assert _d1_meta_get(d1, "embedding_model") == OLD_HOST_TFIDF, "no mass rewrite of the stamp"
    assert _d1_meta_get(d1, "embedding_rebuild_lease") == lease, "the lease is not touched"
    assert not [r for r in caplog.records if "embedding integrity" in r.getMessage()]


def test_a_d1_store_with_a_real_mismatch_is_not_rebuilt_on_d1_by_a_search(d1, monkeypatch, caplog):
    """ob1's 13:21 rebuild of 615 embeddings ON D1 from one search: never again."""
    real_mismatch = "openai|text-embedding-3-small|dense:1536"
    _d1_meta(d1, "embedding_model", real_mismatch)
    _d1_meta(d1, "embedding_integrity", _stamp_value(real_mismatch, _audit(d1.path)))
    calls = _rebuild_spy(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="memora.storage"):
        for _ in range(3):
            with pytest.raises(storage.SearchUnavailable, match="model_mismatch"):
                _search()
    assert calls == []
    assert len([r for r in caplog.records if "NOT rebuilt from a search" in r.getMessage()]) == 1
    assert admin.gate_health("bestation")["embeddings"]["repair_needed"]["reason"] == \
        "model_or_representation_mismatch"


# ------------------------------------------------------------------ the write path records the model once

def test_a_fresh_store_records_its_model_on_the_first_write_and_searches(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"fresh": str(path)}))
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    token = storage.CURRENT_DB.set("fresh")
    try:
        conn = storage.connect()
        storage.add_memory(conn, content="apples and pears", metadata={}, tags=[])
        conn.commit()
        assert embeddings.get_stored_embedding_model(conn) == "tfidf|tfidf|sparse"
        conn.close()
        calls = _rebuild_spy(monkeypatch)
        assert len(_search()[0]) == 1 and calls == []
    finally:
        storage.CURRENT_DB.reset(token)


def test_a_fallback_vector_never_records_a_model(tmp_path):
    from memora import schema

    db = sqlite3.connect(tmp_path / "m.db")
    db.row_factory = sqlite3.Row
    schema.ensure_schema(db)
    embeddings.record_embedding_model_once(db, {"word": 1.0}, "openai")  # a sparse vector under a dense backend
    assert embeddings.get_stored_embedding_model(db) is None
    embeddings.record_embedding_model_once(db, {"0": 0.1, "1": 0.2}, "tfidf")  # dense under tfidf
    assert embeddings.get_stored_embedding_model(db) is None


def test_a_store_with_memories_but_no_vector_searches_instead_of_refusing(store, monkeypatch):
    db = sqlite3.connect(store)
    db.execute("DELETE FROM memories_embeddings")
    db.execute("DELETE FROM memories_meta WHERE key IN ('embedding_model', 'embedding_integrity')")
    db.commit()
    db.close()
    calls = _rebuild_spy(monkeypatch)
    conn = storage.connect()
    try:
        embeddings.invalidate_embedding_integrity_cache(conn)
        assert storage.semantic_search(conn, "apples", top_k=2) is not None
    finally:
        conn.close()
    assert calls == []
