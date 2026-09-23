"""L3 piece (a): statement builder and P2 allow-list, outbox reader, P3
deletion guard, log mode (P4) with the fsync-before-cursor JSONL log, and the
dark-by-construction startup rules. docs/local-primary-implementation.md §2,
§0 P2-P4. Offline only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memora import replicator as R
from memora import schema, write_gate
from memora.backends import commit_event

from tests.l3_fakes import URI, FakeReplica, local_store, sync_state

REPO = Path(__file__).resolve().parent.parent
COLS = {t: None for t in schema.SYNC_TABLES}


def _cols(backend, tbl):
    conn = backend.connect()
    try:
        return R._table_columns(conn, tbl)
    finally:
        conn.close()


# ------------------------------------------------------------------ builder + P2

def test_build_statements_shapes(tmp_path):
    b = local_store(tmp_path / "l.db")
    mem = R._build_statements("memories", [1], {"id": 1, "content": "c", "metadata": None, "tags": "[]",
                                                "created_at": "t", "updated_at": None, "importance": 1.0,
                                                "last_accessed": None, "access_count": 0}, _cols(b, "memories"))
    assert mem == [(
        "INSERT INTO memories (id, content, metadata, tags, created_at, updated_at, importance, last_accessed, "
        "access_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
        "metadata = excluded.metadata, tags = excluded.tags, created_at = excluded.created_at, "
        "updated_at = excluded.updated_at, importance = excluded.importance, last_accessed = excluded.last_accessed, "
        "access_count = excluded.access_count",
        (1, "c", None, "[]", "t", None, 1.0, None, 0))]
    emb = R._build_statements("memories_embeddings", [1], {"memory_id": 1, "embedding": "{}", "representation": "r",
                                                          "dimension": 3, "encoding_source": "s", "writer_token": "w"},
                              _cols(b, "memories_embeddings"))
    assert [s for s, _ in emb] == [
        "DELETE FROM memories_embeddings WHERE memory_id = ?",
        "INSERT INTO memories_embeddings (memory_id, embedding, representation, dimension, encoding_source, "
        "writer_token) VALUES (?, ?, ?, ?, ?, ?)"]
    assert R._build_statements("tombstones", ["h", 3], None, _cols(b, "tombstones")) == [
        ("DELETE FROM tombstones WHERE content_hash = ? AND memory_id = ?", ("h", 3))]
    with pytest.raises(R.ReplicatorStatementError):
        R._build_statements("memories_crossrefs", [1], {"memory_id": 1, "related": b"bytes"}, ["memory_id", "related"])
    with pytest.raises(R.ReplicatorStatementError):
        R._build_statements("memories_events", [1], None, ["id"])
    with pytest.raises(R.ReplicatorStatementError):
        R._build_statements("tombstones", [3], None, ["content_hash", "memory_id"])  # partial key


def test_check_statement_accepts_every_builder_shape(tmp_path):
    b = local_store(tmp_path / "l.db")
    for tbl, pk in schema.SYNC_TABLES.items():
        cols = _cols(b, tbl)
        row = {c: (1 if c in ("id", "memory_id", "dimension", "access_count") else "x") for c in cols}
        for sql, _ in R._build_statements(tbl, [row[c] for c in pk], row, cols):
            assert R._check_statement(sql) in ("upsert", "insert", "delete")
        for sql, _ in R._build_statements(tbl, [row[c] for c in pk], None, cols):
            assert R._check_statement(sql) == "delete"
        where = " AND ".join(f"{c} = ?" for c in pk)
        assert R._check_statement(f"SELECT * FROM {tbl} WHERE {where}") == "readback"
    assert R._check_statement(R.EPOCH_SQL) == "epoch"


@pytest.mark.parametrize("sql", [
    "DROP TABLE memories",
    "DELETE FROM memories",
    "DELETE FROM memories WHERE content = ?",
    "DELETE FROM tombstones WHERE memory_id = ?",
    "DELETE FROM tombstones WHERE memory_id = ? AND content_hash = ?",
    "UPDATE memories SET content = ? WHERE id = ?",
    "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
    "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
    "INSERT INTO memories_events (id) VALUES (?)",
    "DELETE FROM memories_events WHERE id = ?",
    "INSERT INTO memories (id, content) VALUES (?, ?)",
    "INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?) ON CONFLICT(memory_id) DO UPDATE SET embedding = excluded.embedding",
    "INSERT INTO memories (id, content) VALUES (?, ?) ON CONFLICT(content) DO UPDATE SET id = excluded.id",
    "INSERT INTO memories (id, content) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET content = excluded.tags",
    "INSERT INTO memories (id, content, tags) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET content = excluded.content, tags = excluded.tags",
    "DELETE FROM memories WHERE id = ?; DROP TABLE memories",
    "SELECT * FROM memories",
    "SELECT * FROM memories WHERE id = ? OR 1 = 1",
    "SELECT value FROM memories_meta",
    "PRAGMA table_info(memories)",
    "delete from memories where id = ?",
    "",
])
def test_check_statement_rejects(sql):
    with pytest.raises(R.ReplicatorStatementError):
        R._check_statement(sql)


# ------------------------------------------------------------------ outbox reader

def test_outbox_reader_coalesces_in_seq_order_and_current_row_decides(tmp_path):
    b = local_store(tmp_path / "l.db")
    conn = b.connect()
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.execute("INSERT INTO memories (id, content) VALUES (2, 'b')")
    conn.execute("UPDATE memories SET content = 'a2' WHERE id = 1")
    conn.execute("DELETE FROM memories WHERE id = 2")
    conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (1, '[]')")
    conn.commit()
    batch = R.read_batch(conn, 0, 100)
    conn.close()
    keys = [(k.tbl, k.pk, None if k.row is None else k.row.get("content", k.row.get("related"))) for k in batch.keys]
    # coalesced to each key's HIGHEST seq; ordered parent upserts, other
    # tables, then parent deletes (D1 foreign keys), by seq within each
    assert keys == [("memories", [1], "a2"), ("memories_crossrefs", [1], "[]"), ("memories", [2], None)]
    assert [k.seq for k in batch.keys] == [3, 5, 4]
    assert batch.lo == 1 and batch.hi == 5 and batch.deletes == {"memories": 1}


# ------------------------------------------------------------------ log mode

class _NoD1:
    def __getattr__(self, name):
        raise AssertionError(f"log mode touched D1 ({name})")


def _rep(b, mode="log", **kw):
    r = R.StoreReplicator("s1", b, URI, mode=mode, writer_factory=lambda u: _NoD1(),
                          reader_factory=lambda u: _NoD1(), broadcast=lambda: None, **kw)
    r._open()
    return r


def test_log_mode_logs_statements_advances_the_cursor_and_sends_nothing(tmp_path):
    b = local_store(tmp_path / "l.db")
    conn = b.connect()
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.execute("INSERT INTO memories_meta (key, value) VALUES ('other', 'v')")
    conn.commit()
    conn.close()
    rep = _rep(b)
    assert rep.run_once() == "logged"
    assert rep.run_once() == "idle"
    recs = list(R.iter_log("s1"))
    assert [(r["tbl"], r["pk"]) for r in recs] == [("memories", [1]), ("memories_meta", ["other"])]
    assert all(R._check_statement(r["sql"]) for r in recs)
    st = sync_state(b)
    assert st["log_cursor_seq"] == 2 and st["last_acked_seq"] == 0
    assert rep.status()["lag_rows"] == 0 and rep.status()["mode"] == "log"


def test_log_key_set_equals_outbox_key_set(tmp_path):
    b = local_store(tmp_path / "l.db")
    conn = b.connect()
    for i in range(1, 6):
        conn.execute("INSERT INTO memories (id, content) VALUES (?, 'x')", (i,))
        conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, writer_token) VALUES (?, '{}', 'w')", (i,))
    conn.commit()
    outbox = {(t, pk) for t, pk in conn.execute("SELECT tbl, pk FROM sync_outbox")}
    conn.close()
    rep = _rep(b, batch_rows=3)
    while rep.run_once() == "logged":
        pass
    logged = {(r["tbl"], json.dumps(r["pk"], separators=(",", ":"))) for r in R.iter_log("s1")}
    assert logged == {(t, json.dumps(json.loads(pk), separators=(",", ":"))) for t, pk in outbox}


def test_log_crash_between_fsync_and_cursor(tmp_path):
    """A real process dies after the log fsync and before the cursor commit;
    the next cycle appends the same range again and readers deduplicate."""
    db = tmp_path / "l.db"
    b = local_store(db)
    conn = b.connect()
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.commit()
    conn.close()
    child = f"""
import os, sys
sys.path.insert(0, {str(REPO)!r})
os.environ['MEMORA_DATA_DIR'] = {str(write_gate.data_dir())!r}
os.environ['MEMORA_TEST_KILL_AFTER_LOG_FSYNC'] = '1'
from memora import replicator as R
from memora.backends import LocalSQLiteBackend
r = R.StoreReplicator('s1', LocalSQLiteBackend({str(db)!r}), {URI!r}, mode='log', broadcast=lambda: None)
r._open(); r.run_once()
"""
    assert subprocess.run([sys.executable, "-c", child], timeout=60).returncode == 137
    assert sync_state(b)["log_cursor_seq"] == 0
    raw_lines = sum(1 for p in R.log_dir("s1").glob("*.jsonl") for _ in p.read_text().splitlines())
    assert raw_lines == 1
    rep = _rep(b)
    assert rep.run_once() == "logged"
    raw_lines = sum(1 for p in R.log_dir("s1").glob("*.jsonl") for _ in p.read_text().splitlines())
    assert raw_lines == 2 and len(list(R.iter_log("s1"))) == 1  # appended again, read once
    assert sync_state(b)["log_cursor_seq"] == 1


def test_torn_final_log_line_is_dropped(tmp_path):
    b = local_store(tmp_path / "l.db")
    conn = b.connect()
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    conn.commit()
    conn.close()
    _rep(b).run_once()
    path = next(R.log_dir("s1").glob("*.jsonl"))
    with open(path, "a") as fh:
        fh.write('{"attempt_id":"x","seq":9')
    assert [r["seq"] for r in R.iter_log("s1")] == [1]


def test_meta_exclusions_never_logged(tmp_path):
    b = local_store(tmp_path / "l.db")
    conn = b.connect()
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'bumps the epoch')")
    for key in schema.SYNC_META_EXCLUDED:
        conn.execute("INSERT OR REPLACE INTO memories_meta (key, value) VALUES (?, 'x')", (key,))
    conn.commit()
    conn.close()
    _rep(b).run_once()
    assert [(r["tbl"], r["pk"]) for r in R.iter_log("s1")] == [("memories", [1])]


# ------------------------------------------------------------------ P3 guard

def _seed_rows(b, n):
    conn = b.connect()
    conn.executemany("INSERT INTO memories (id, content) VALUES (?, 'x')", [(i,) for i in range(1, n + 1)])
    conn.commit()
    conn.close()


@pytest.mark.parametrize("rows,deletes,halts", [
    (200, 51, True),    # both rules
    (6000, 51, True),   # the 50-row rule alone (1% of 6000 is 60)
    (6000, 50, False),
    (100, 2, True),     # the 1% rule alone
    (99, 1, True),      # any delete from a table under 100 rows
    (200, 2, False),
])
def test_delete_guard(tmp_path, rows, deletes, halts):
    b = local_store(tmp_path / "l.db")
    _seed_rows(b, rows)
    rep = _rep(b, batch_rows=10_000)
    assert rep.run_once() == "logged"  # the inserts
    conn = b.connect()
    conn.execute("DELETE FROM memories WHERE id <= ?", (deletes,))
    conn.commit()
    conn.close()
    if not halts:
        assert rep.run_once() == "logged"
        return
    assert rep.run_once() == "halted"
    reason = sync_state(b)["halted_reason"]
    assert reason.startswith(f"delete_guard: memories {deletes}/{rows}")
    assert R._REPLICATORS == {} and rep.run_once() == "halted"  # persists
    attempt = reason.rsplit("attempt=", 1)[1]
    conn = b.connect_replicator()
    with pytest.raises(ValueError):
        R.resume(conn, allow_deletes="wrong")
    R.resume(conn, allow_deletes=attempt)
    conn.close()
    assert rep.run_once() == "logged"
    assert sync_state(b)["allow_deletes_attempt"] is None  # one attempt only


def test_statement_outside_the_allow_list_halts(tmp_path, monkeypatch):
    b = local_store(tmp_path / "l.db")
    _seed_rows(b, 1)
    monkeypatch.setattr(R, "_build_statements", lambda *a: [("DROP TABLE memories", ())])
    rep = _rep(b)
    assert rep.run_once() == "halted"
    assert sync_state(b)["halted_reason"].startswith("statement_rejected:")


# ------------------------------------------------------------------ startup rules

def test_start_replicators_is_dark_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORA_REPLICATION", raising=False)
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"s1": URI}))
    assert R.start_replicators(start=False) == {}
    monkeypatch.setenv("MEMORA_REPLICATION", "log")
    monkeypatch.delenv("MEMORA_REPLICAS")
    assert R.start_replicators(start=False) == {}


def test_shadow_store_is_forced_to_log_and_write_refuses(tmp_path, monkeypatch):
    with pytest.raises(R.ReplicatorConfigError, match="shadow"):
        R.StoreReplicator("s1", local_store(tmp_path / "x.db"), URI, mode="write", shadow=True)
    shadow = tmp_path / "shadow.db"
    local_store(shadow)
    monkeypatch.setenv("MEMORA_REPLICATION", "write")
    monkeypatch.setenv("MEMORA_SHADOW_LOCAL", json.dumps({"s1": str(shadow)}))
    try:
        assert R.start_replicators(start=False) == {"s1": {"mode": "log", "shadow": True}}
    finally:
        R._REPLICATORS.clear()


def test_replicated_store_must_match_sync_state_and_be_local(tmp_path, monkeypatch):
    local_store(tmp_path / "a.db")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db"), "r": "d1://x/y"}))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("MEMORA_REPLICATION", "log")
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"a": "d1://acct/other", "r": URI}))
    try:
        out = R.start_replicators(start=False)
    finally:
        R._REPLICATORS.clear()
    assert "!= sync_state" in out["a"]["error"] and "must be local" in out["r"]["error"]


def test_a_commit_wakes_the_replicator(tmp_path):
    b = local_store(tmp_path / "l.db")
    ev = commit_event(b.db_path)
    ev.clear()
    conn = b.connect()
    conn.execute("INSERT INTO memories (id, content) VALUES (1, 'a')")
    assert not ev.is_set()
    conn.commit()
    assert ev.is_set()
    conn.close()
