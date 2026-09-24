"""What LocalSQLiteBackend.connect() actually runs on the RAW sqlite3
connection before handing it out (local-primary plan §1 "Freeze", §9 (b)).

The write gate classifies every statement on the handed-out connection; the
statements connect() itself runs never pass through it. §9 (b) holds them to
exactly PRAGMA busy_timeout on every writer, plus PRAGMA journal_mode=WAL on a
live primary (both idempotent, neither changes a row), and the touch read.

tests/test_l4_store_write.py checks the tuple writer_setup_pragmas() returns
and the resulting settings. This test traces the connection itself, so a
statement run outside writer_setup_pragmas() -- another PRAGMA, a write --
fails here even when that tuple is unchanged.
"""
import json
import re
import sqlite3

import pytest

from memora import backends

TOUCH_READ = "SELECT 1 FROM sqlite_master LIMIT 1"


def _normalise_pragma(sql):
    m = re.fullmatch(r"\s*PRAGMA\s+(\w+)\s*(?:=\s*['\"]?(\w+)['\"]?)?\s*;?\s*", sql, re.I)
    assert m, f"unparseable PRAGMA: {sql!r}"
    name, value = m.group(1).lower(), m.group(2)
    if name == "journal_mode" and value is not None:
        return f"journal_mode={value.upper()}"
    return name


@pytest.fixture
def traced(monkeypatch):
    """Record every statement run on connections sqlite3.connect creates, from
    the moment they exist (backends.sqlite3 IS the sqlite3 module)."""
    seen = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(backends.sqlite3, "connect", connect)
    return seen


def _open(backend, traced):
    traced.clear()
    conn = backend.connect()
    conn.set_trace_callback(None)  # the caller's own statements are not setup
    return conn, list(traced)


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("live_primary", [False, True])
def test_connect_runs_exactly_the_setup_statements(tmp_path, traced, monkeypatch, fresh, live_primary):
    path = tmp_path / "store.db"
    if not fresh:
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE t (x)")
        seed.commit()
        seed.close()
    backend = backends.LocalSQLiteBackend(path)
    if live_primary:
        backend.store_name = "p"
        monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://a/b"}))
        backend.fk_gate()  # the once-per-process audit (its own connection; test below)
    conn, statements = _open(backend, traced)
    try:
        pragmas = [s for s in statements if s.lstrip().upper().startswith("PRAGMA")]
        others = [s for s in statements if s not in pragmas]
        expected = ["busy_timeout"] + (["journal_mode=WAL", "foreign_keys"] if live_primary else [])
        assert sorted(_normalise_pragma(p) for p in pragmas) == sorted(expected), pragmas
        assert [_normalise_pragma(p) for p in backends.writer_setup_pragmas(live_primary)] \
            == [_normalise_pragma(p) for p in pragmas], "connect() ran PRAGMAs outside writer_setup_pragmas()"
        assert others == [TOUCH_READ], f"connect() ran more than its touch read: {others}"
    finally:
        conn.close()
        if live_primary:
            backends.release_primary_lock(backend.db_path)


def test_the_fk_audit_runs_once_per_process_on_its_own_closed_connection(tmp_path, traced, monkeypatch):
    """Plan §9 (x): before the first writer of an enforcing store, one audit
    -- reads only (sqlite_master, foreign_key_list, foreign_key_check) --
    on a separate writer connection that is closed again; later opens run
    only the setup statements."""
    path = tmp_path / "store.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    seed.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id) ON DELETE CASCADE)")
    seed.commit()
    seed.close()
    backend = backends.LocalSQLiteBackend(path)
    backend.store_name = "p"
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://a/b"}))
    try:
        conn, statements = _open(backend, traced)
        conn.close()
        audit = [s for s in statements if "foreign_key_check" in s or "foreign_key_list" in s
                 or s.startswith("SELECT name FROM sqlite_master")]
        assert audit and all(not re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)", s, re.I)
                             for s in statements), statements
        assert backends._store_lock(path).open_writers == 0, "the audit connection was closed"
        conn, again = _open(backend, traced)
        conn.close()
        assert not any("foreign_key" in s and "foreign_keys" not in s for s in again), "audited once"
    finally:
        backends.release_primary_lock(path)


def test_the_normaliser_does_not_fold_other_pragmas_into_allowed_ones():
    allowed = {"busy_timeout", "journal_mode=WAL", "foreign_keys"}
    for sql in ("PRAGMA journal_mode=DELETE", "PRAGMA wal_checkpoint(TRUNCATE)",
                "PRAGMA query_only=1", "PRAGMA synchronous=OFF", "PRAGMA optimize"):
        try:
            got = _normalise_pragma(sql)
        except AssertionError:
            continue
        assert got not in allowed, sql
