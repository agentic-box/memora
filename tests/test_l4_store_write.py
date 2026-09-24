"""L4 piece (a): store_write, the writer setup PRAGMAs (§9 b) and the
exempt-caller allow-list (§9 o). docs/local-primary-implementation.md §3."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from memora import backends
from memora.backends import LocalSQLiteBackend, StoreWriteAborted, in_store_write, store_write


# ------------------------------------------------------------------ store_write

def _store(tmp_path, name="s.db"):
    b = LocalSQLiteBackend(tmp_path / name)
    with b.connect() as c:
        c.execute("CREATE TABLE t (a INTEGER)")
    return b


def _count(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        db.close()


def test_store_write_defers_inner_commits_to_its_end(tmp_path):
    b = _store(tmp_path)
    conn = b.connect()
    with store_write(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()  # an inner commit: deferred
        assert _count(b.db_path) == 0
        conn.execute("INSERT INTO t VALUES (2)")
    assert _count(b.db_path) == 2
    conn.close()


def test_inner_rollback_or_error_aborts_everything(tmp_path):
    b = _store(tmp_path)
    conn = b.connect()
    raised = []
    with pytest.raises(StoreWriteAborted):
        with store_write(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            try:
                conn.rollback()
            except StoreWriteAborted:
                raised.append(True)
                raise
    assert raised, "rollback() inside store_write must raise at the call site"
    with pytest.raises(ZeroDivisionError):
        with store_write(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            with conn:  # the connection's own context manager neither commits nor rolls back here
                conn.execute("INSERT INTO t VALUES (2)")
            1 / 0
    assert _count(b.db_path) == 0
    assert not conn.in_transaction
    conn.close()


def test_after_commit_callbacks_run_after_commit_outside_the_lock_and_not_on_rollback(tmp_path):
    b = _store(tmp_path)
    conn = b.connect()
    ran = []
    with store_write(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        backends.after_store_write(lambda: ran.append((in_store_write(), _count(b.db_path))))
    assert ran == [(False, 1)]
    with pytest.raises(RuntimeError):
        with store_write(conn):
            backends.after_store_write(lambda: ran.append("rolled back"))
            raise RuntimeError("x")
    assert ran == [(False, 1)]
    conn.close()


def test_nested_store_write_joins_the_outer_transaction(tmp_path):
    b = _store(tmp_path)
    conn = b.connect()
    with store_write(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        with store_write(conn):
            conn.execute("INSERT INTO t VALUES (2)")
        assert _count(b.db_path) == 0
    assert _count(b.db_path) == 2
    conn.close()


def test_store_write_serialises_writers_in_process(tmp_path):
    b = _store(tmp_path)
    order = []
    inside = threading.Event()

    def first():
        c = b.connect()
        with store_write(c):
            order.append("first-in")
            inside.set()
            time.sleep(0.3)
            order.append("first-out")
        c.close()

    def second():
        inside.wait(5)
        c = b.connect()
        c.execute("PRAGMA busy_timeout = 0")  # only the process lock can make it wait
        try:
            with store_write(c):
                order.append("second-in")
        except sqlite3.OperationalError as exc:
            order.append(f"second-failed: {exc}")
        c.close()

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start(); t2.start(); t1.join(5); t2.join(5)
    assert order == ["first-in", "first-out", "second-in"]


def test_store_write_refuses_a_non_transactional_connection(tmp_path):
    with pytest.raises(TypeError):
        with store_write(sqlite3.connect(tmp_path / "x.db")):
            pass


# ------------------------------------------------------------------ §9 (b) setup PRAGMAs

def test_writer_setup_pragmas_are_exactly_busy_timeout_and_wal_for_a_live_primary(tmp_path, monkeypatch):
    assert backends.writer_setup_pragmas(False) == ("PRAGMA busy_timeout = 5000",)
    assert backends.writer_setup_pragmas(True) == ("PRAGMA busy_timeout = 5000", "PRAGMA journal_mode = WAL",
                                                   "PRAGMA foreign_keys = ON")  # plan §9 x
    assert backends.writer_setup_pragmas(False, True) == ("PRAGMA busy_timeout = 5000", "PRAGMA foreign_keys = ON")
    plain = LocalSQLiteBackend(tmp_path / "plain.db")
    with plain.connect() as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # unchanged
    primary = LocalSQLiteBackend(tmp_path / "primary.db")
    primary.store_name = "p"
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://a/b"}))
    c = primary.connect()
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        c.close()
        backends.release_primary_lock(primary.db_path)


def test_a_reader_sees_the_pre_transaction_snapshot_on_a_live_primary(tmp_path, monkeypatch):
    primary = LocalSQLiteBackend(tmp_path / "wal.db")
    primary.store_name = "p"
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://a/b"}))
    w = primary.connect()
    try:
        w.execute("CREATE TABLE t (a INTEGER)")
        w.execute("INSERT INTO t VALUES (1)")
        w.commit()
        with store_write(w):
            w.execute("INSERT INTO t VALUES (2)")
            reader = sqlite3.connect(primary.db_path, timeout=0.1)
            assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1  # no wait, old snapshot
            reader.close()
    finally:
        w.close()
        backends.release_primary_lock(primary.db_path)


def test_exempt_gate_entries_are_for_the_replicator_only(tmp_path):
    """§9 (o): enter(exempt=True) refuses any caller module but the replicator."""
    gate = LocalSQLiteBackend(tmp_path / "g.db").write_gate()
    with pytest.raises(PermissionError, match="replicator only"):
        gate.enter("not the replicator", exempt=True)
    assert gate.status()["in_flight"] == 0



def test_a_caught_inner_rollback_still_refuses_the_commit(tmp_path):
    """§9 (r): catching StoreWriteAborted does not make the commit possible."""
    b = _store(tmp_path)
    conn = b.connect()
    with pytest.raises(StoreWriteAborted, match="caught"):
        with store_write(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            try:
                conn.rollback()
            except StoreWriteAborted:
                pass
            conn.execute("INSERT INTO t VALUES (2)")
    assert _count(b.db_path) == 0
    with store_write(conn):  # the next transaction is clean
        conn.execute("INSERT INTO t VALUES (3)")
    assert _count(b.db_path) == 1
    conn.close()
