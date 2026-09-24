"""E1b (leader 7981): a store with NO recorded embedding model (written
before E1, e.g. by v0.4.6) whose vectors are what the current model
produces is searched (reported "model unrecorded"; an explicit
memory_verify_integrity(record_model=true) records it); an incompatible one
is a genuine mismatch. The deploy preflight (python -m memora.embedding_preflight)
refuses before the old container stops when any store would refuse searches."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import memora
from memora import admin, backends, embeddings, embedding_preflight as ep, storage

REPO = Path(__file__).resolve().parent.parent


def _mkstore(path, name, monkeypatch, *, dense_dim=None, record=None):
    """A store as v0.4.6 left it: vectors, NO embedding_model row (unless
    record is given)."""
    reset = storage.CURRENT_DB.set(name)
    try:
        conn = storage.connect()
        try:
            for i in range(3):
                storage.add_memory(conn, content=f"apples and pears number {i}", metadata={}, tags=[])
            if dense_dim:
                for row in conn.execute("SELECT id FROM memories").fetchall():
                    embeddings.upsert_embedding(conn, row[0], {str(j): 0.1 + j / 100 for j in range(dense_dim)})
            conn.execute("DELETE FROM memories_meta WHERE key IN ('embedding_model', 'embedding_integrity')")
            if record:
                conn.execute("INSERT INTO memories_meta (key, value) VALUES ('embedding_model', ?)", (record,))
            conn.commit()
        finally:
            conn.close()
    finally:
        storage.CURRENT_DB.reset(reset)
    embeddings._MODEL_RECORDED.clear()


@pytest.fixture
def reg(tmp_path, monkeypatch):
    names = {n: str(tmp_path / f"{n}.db") for n in ("old", "dense", "other", "fine")}
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps(names))
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    storage._EMBEDDING_REPAIR_NEEDED.clear()
    storage._EMBEDDING_MODEL_UNRECORDED.clear()
    _mkstore(tmp_path / "old.db", "old", monkeypatch)                          # v0.4.6 tfidf, unrecorded
    _mkstore(tmp_path / "dense.db", "dense", monkeypatch, dense_dim=8)         # unrecorded dense vectors
    _mkstore(tmp_path / "other.db", "other", monkeypatch, record="openai|bge-m3|dense:1024")  # recorded, other
    _mkstore(tmp_path / "fine.db", "fine", monkeypatch, record="tfidf|tfidf|sparse")
    return names


def _search(name, **kw):
    token = storage.CURRENT_DB.set(name)
    try:
        conn = storage.connect()
        try:
            embeddings.invalidate_embedding_integrity_cache(conn)
            return storage.semantic_search(conn, "apples", top_k=2, **kw)
        finally:
            conn.close()
    finally:
        storage.CURRENT_DB.reset(token)


def _vector_state(path):
    db = sqlite3.connect(path)
    try:
        return (db.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0],
                db.execute("SELECT value FROM memories_meta WHERE key = 'embedding_change_epoch'").fetchone()[0],
                db.execute("SELECT value FROM memories_meta WHERE key = 'embedding_model'").fetchone())
    finally:
        db.close()


# ------------------------------------------------------------------ (A) search

def test_a_v046_store_with_compatible_vectors_is_searched_and_reported(reg, caplog):
    before = _vector_state(reg["old"])
    with caplog.at_level(logging.WARNING, logger="memora.storage"):
        for _ in range(3):
            assert len(_search("old")) == 2
    assert _vector_state(reg["old"]) == before, "a search records nothing"
    notes = [r.getMessage() for r in caplog.records if "no recorded embedding model" in r.getMessage()]
    assert len(notes) == 1 and "record_model=true" in notes[0]
    health = admin.gate_health("old")["embeddings"]["model_unrecorded"]
    assert "record_model=true" in health["action"]


def test_the_read_only_json_path_serves_it_too(reg):
    assert len(_search("old", read_only=True)) == 2


def test_incompatible_unrecorded_vectors_are_a_genuine_mismatch(reg):
    with pytest.raises(storage.SearchUnavailable, match="embedding_model_unrecorded"):
        _search("dense")  # dense vectors, tfidf server


def test_a_dense_unrecorded_store_needs_the_query_dimension(reg, monkeypatch):
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "openai")
    monkeypatch.setattr(storage, "_query_embedding", lambda q: {str(j): 0.1 for j in range(8)})
    assert len(_search("dense")) == 2
    monkeypatch.setattr(storage, "_query_embedding", lambda q: {str(j): 0.1 for j in range(1024)})
    with pytest.raises(storage.SearchUnavailable, match="embedding_model_unrecorded"):
        _search("dense")


# ------------------------------------------------------------------ the explicit record step

def _verify(name, **kw):
    from memora import server

    token = storage.CURRENT_DB.set(name)
    try:
        return asyncio.run(server.memory_verify_integrity(**kw))
    finally:
        storage.CURRENT_DB.reset(token)


def test_record_model_is_explicit_and_clears_the_note(reg):
    _search("old")
    assert "embeddings" in admin.gate_health("old")
    plain = _verify("old")
    assert "record_model" not in plain and _vector_state(reg["old"])[2] is None, "only when asked"
    out = _verify("old", record_model=True)
    assert out["record_model"] == {"recorded": True, "model": "tfidf|tfidf|sparse", "vectors": "vectors ['sparse'] for tfidf"}
    assert _vector_state(reg["old"])[2] == ("tfidf|tfidf|sparse",)
    assert "embeddings" not in (admin.gate_health("old") or {})
    _search("old")
    assert "embeddings" not in (admin.gate_health("old") or {})


def test_record_model_refuses_incompatible_vectors(reg):
    out = _verify("dense", record_model=True)
    assert out["record_model"]["recorded"] is False and "memory_rebuild_embeddings" in out["record_model"]["reason"]
    assert _vector_state(reg["dense"])[2] is None


# ------------------------------------------------------------------ (B) the deploy preflight

def test_the_preflight_names_each_store_that_would_refuse_searches(reg, monkeypatch):
    def no_connect(self, *a, **kw):
        raise AssertionError("the preflight must never open a writer (connect())")
    monkeypatch.setattr(backends.LocalSQLiteBackend, "connect", no_connect)
    report = ep.run("tfidf")
    s = report["stores"]
    assert report["ok"] is False
    assert s["old"]["ok"] and s["old"]["state"] == "model unrecorded (compatible)"
    assert s["fine"]["ok"] and s["fine"]["state"] == "recorded model matches"
    assert not s["dense"]["ok"] and "memory_rebuild_embeddings" in s["dense"]["fix"]
    assert not s["other"]["ok"] and s["other"]["state"].startswith("search would refuse: model_mismatch")


def test_the_preflight_probes_the_dense_dimension(reg, monkeypatch):
    monkeypatch.setattr(embeddings, "compute_embedding", lambda *a: {str(j): 0.1 for j in range(8)})
    s = ep.run("openai", registry={"dense": reg["dense"]})["stores"]["dense"]
    assert s["ok"] and s["state"] == "model unrecorded (compatible)"
    monkeypatch.setattr(embeddings, "compute_embedding", lambda *a: {str(j): 0.1 for j in range(1024)})
    s = ep.run("openai", registry={"dense": reg["dense"]})["stores"]["dense"]
    assert not s["ok"] and "now produces dense:1024" in s["state"]


def test_the_preflight_reads_a_live_primary_whose_lock_another_process_holds(reg, monkeypatch):
    """memora-all holds re's primary lock while the preflight runs beside it."""
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; sys.path.insert(0, sys.argv[2]); from memora import backends; "
         "backends.acquire_primary_lock(sys.argv[1]); print('held', flush=True); time.sleep(60)",
         reg["fine"], str(REPO)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"fine": "d1://acct/db"}))
        assert ep.run("tfidf", registry={"fine": reg["fine"]})["stores"]["fine"]["ok"]
    finally:
        holder.kill()
        holder.wait()


def test_the_preflight_takes_no_service_lock(reg, monkeypatch):
    monkeypatch.setenv("MEMORA_SERVICE_LOCK", "1")
    ep.run("tfidf", registry={"fine": reg["fine"]})
    assert backends._SERVICE_LOCKS == {}


def test_the_preflight_reads_a_d1_store_with_the_read_token(tmp_path, monkeypatch):
    from tests.l3_fakes import FakeReplica

    replica = FakeReplica(tmp_path / "d1.db")
    db = replica._db()
    db.execute("INSERT INTO memories (id, content) VALUES (1, 'x')")
    # writer_token set: an unset one makes trg_embedding_external_insert null the representation
    db.execute("INSERT INTO memories_embeddings (memory_id, embedding, representation, dimension, writer_token) "
               "VALUES (1, ?, 'sparse', NULL, 'w')", ('{"w": 1.0}',))
    db.execute("INSERT INTO memories_meta (key, value) VALUES ('embedding_model', 'tfidf|tfidf|old-host:1|sparse')")
    db.commit()
    db.close()
    monkeypatch.setattr(backends.D1SelectOnlyConnection, "_post", lambda self, body: replica.reader_post(body))
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read-token")
    s = ep.run("tfidf", registry={"bestation": "d1://acct/db1"})["stores"]["bestation"]
    assert s["ok"] and s["state"] == "recorded model matches", s  # the old host is ignored (E1)


def test_an_unreadable_store_fails_closed(tmp_path):
    report = ep.run("tfidf", registry={"gone": str(tmp_path / "missing.db")})
    assert report["ok"] is False and "could not be checked" in report["stores"]["gone"]["state"]


def test_the_cli_contract(reg, tmp_path):
    env = {"MEMORA_DATABASES": json.dumps({"old": reg["old"], "fine": reg["fine"]}),
           "MEMORA_EMBEDDING_MODEL": "tfidf", "MEMORA_DATA_DIR": str(tmp_path / "data"), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-m", "memora.embedding_preflight"], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1])["ok"] is True
    env["MEMORA_DATABASES"] = json.dumps({"old": reg["old"], "other": reg["other"]})
    r = subprocess.run([sys.executable, "-m", "memora.embedding_preflight"], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "store 'other' would refuse semantic search" in r.stderr



# ------------------------------------------------------------------ review 8010

def _raw(path, *sql):
    db = sqlite3.connect(path)
    try:
        for stmt, params in sql:
            db.execute(stmt, params)
        db.commit()
    finally:
        db.close()


def test_an_unrecorded_store_with_an_unknown_encoding_is_refused_not_served(reg):
    """P1-1: the unrecorded exception only covers the comparable reasons."""
    _raw(reg["old"], ("INSERT INTO memories (id, content) VALUES (99, 'legacy')", ()),
         ("INSERT INTO memories_embeddings (memory_id, embedding, representation, writer_token) "
          "VALUES (99, ?, NULL, 'w')", ('{"w": 1.0}',)))
    with pytest.raises(storage.SearchUnavailable):
        _search("old")
    assert ep.run("tfidf", registry={"old": reg["old"]})["stores"]["old"]["ok"] is False


@pytest.mark.parametrize("backend", ["local", "d1"])
def test_the_preflight_is_never_green_where_the_search_refuses_orphans(reg, tmp_path, monkeypatch, backend):
    """P1-2: a recorded tfidf store plus an orphan sparse embedding row."""
    orphan = ("INSERT INTO memories_embeddings (memory_id, embedding, representation, writer_token) "
              "VALUES (4242, ?, 'sparse', 'w')", ('{"w": 1.0}',))
    if backend == "local":
        _raw(reg["fine"], ("PRAGMA foreign_keys = OFF", ()), orphan)
        with pytest.raises((storage.SearchUnavailable, embeddings.EmbeddingIntegrityFault)):
            _search("fine")
        s = ep.run("tfidf", registry={"fine": reg["fine"]})["stores"]["fine"]
    else:
        from tests.l3_fakes import FakeReplica

        replica = FakeReplica(tmp_path / "d1.db")
        db = replica._db()
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("INSERT INTO memories (id, content) VALUES (1, 'x')")
        db.execute("INSERT INTO memories_embeddings (memory_id, embedding, representation, writer_token) "
                   "VALUES (1, ?, 'sparse', 'w')", ('{"w": 1.0}',))
        db.execute(orphan[0], orphan[1])
        db.execute("INSERT INTO memories_meta (key, value) VALUES ('embedding_model', 'tfidf|tfidf|sparse')")
        db.commit()
        db.close()
        monkeypatch.setattr(backends.D1SelectOnlyConnection, "_post", lambda self, body: replica.reader_post(body))
        monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read-token")
        s = ep.run("tfidf", registry={"bestation": "d1://acct/db1"})["stores"]["bestation"]
    assert s["ok"] is False and s["integrity"] == "orphan_embeddings" and "search would refuse" in s["state"], s


def test_a_blank_legacy_embedding_row_does_not_make_the_preflight_refuse_a_searchable_store(reg):
    """P2: the preflight uses the audit's own vector predicate."""
    _raw(reg["fine"], ("INSERT INTO memories (id, content) VALUES (77, 'blank')", ()),
         ("INSERT INTO memories_embeddings (memory_id, embedding, representation, dimension, writer_token) "
          "VALUES (77, '', 'dense', 1024, 'w')", ()))
    searchable = True
    try:
        _search("fine")
    except (storage.SearchUnavailable, embeddings.EmbeddingIntegrityFault):
        searchable = False
    assert ep.run("tfidf", registry={"fine": reg["fine"]})["stores"]["fine"]["ok"] is searchable


# ------------------------------------------------------------------ post-PASS mutation checks

def test_the_note_clears_once_a_write_records_the_model(reg):
    _search("old")
    assert "model_unrecorded" in admin.gate_health("old")["embeddings"]
    token = storage.CURRENT_DB.set("old")
    try:
        conn = storage.connect()
        storage.add_memory(conn, content="a new apple", metadata={}, tags=[])  # the write path records it
        conn.commit()
        embeddings.verify_embedding_integrity(conn, stamp=True)  # an explicit audit: now no mismatch at all
        conn.commit()
        conn.close()
    finally:
        storage.CURRENT_DB.reset(token)
    assert _vector_state(reg["old"])[2] == ("tfidf|tfidf|sparse",)
    _search("old")
    assert "embeddings" not in (admin.gate_health("old") or {})


@pytest.mark.parametrize("reps, model, ok, dim", [
    ({"sparse": 3}, "tfidf", True, None),
    ({"sparse": 3, "empty": 1}, "tfidf", True, None),
    ({"dense:8": 3}, "tfidf", False, None),
    ({"dense:8": 3}, "openai", True, 8),
    ({"dense:8": 2, "dense:16": 1}, "openai", False, None),
    ({"dense": 3}, "openai", False, None),
    ({"sparse": 3}, "openai", False, None),
    ({}, "openai", True, None),
])
def test_unrecorded_compatibility(reps, model, ok, dim):
    got_ok, got_dim, _why = embeddings.unrecorded_compatibility(reps, model)
    assert (got_ok, got_dim) == (ok, dim)
