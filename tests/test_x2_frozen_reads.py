"""X2: a store that starts frozen (persisted freeze or MEMORA_READONLY_DBS)
serves reads when its schema is current -- the first connect skips the
schema pass after a read-only check -- and refuses clearly when the schema
still needs work. Thawing lets the schema pass run again."""
from __future__ import annotations

import json
import sqlite3

import pytest

from memora import admin, backends, schema, storage, write_gate
from memora.backends import LocalSQLiteBackend
from memora.write_gate import StoreReadOnlyError
from tests.test_l2_gate_journal import FakeD1Server


def _make_store(path, *, drop=None):
    conn = LocalSQLiteBackend(path).connect()
    try:
        schema.ensure_schema(conn)
        conn.execute("INSERT INTO memories (id, content) VALUES (1, 'kept')")
        if drop:
            conn.execute(drop)
        conn.commit()
    finally:
        conn.close()


def _master(path):
    db = sqlite3.connect(path)
    try:
        return sorted(db.execute("SELECT type, name, sql FROM sqlite_master").fetchall(), key=str)
    finally:
        db.close()


@pytest.fixture
def loc(tmp_path, monkeypatch):
    path = tmp_path / "loc.db"
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"loc": str(path)}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "loc")
    token = storage.CURRENT_DB.set("loc")
    yield path
    storage.CURRENT_DB.reset(token)


def _start_frozen(how, monkeypatch):
    if how == "freeze file":
        write_gate.persist_freeze("loc")
    else:
        monkeypatch.setenv("MEMORA_READONLY_DBS", "loc")
    write_gate._reset_for_tests()  # a fresh process: the gate starts frozen


@pytest.mark.parametrize("how", ["freeze file", "MEMORA_READONLY_DBS"])
def test_a_frozen_store_with_a_current_schema_serves_reads_and_refuses_writes(loc, monkeypatch, how):
    _make_store(loc)
    before = _master(loc)
    _start_frozen(how, monkeypatch)
    assert storage.backend_for("loc").write_gate().state == "frozen"
    conn = storage.connect()
    try:
        assert conn.execute("SELECT content FROM memories WHERE id = 1").fetchone()[0] == "kept"
        with pytest.raises(StoreReadOnlyError):
            conn.execute("INSERT INTO memories (content) VALUES ('no')")
    finally:
        conn.close()
    assert _master(loc) == before, "no schema statement ran"
    conn = storage.connect()  # the next connect does not re-check
    conn.close()


@pytest.mark.parametrize("drop, needle", [
    ("DROP TABLE import_lease", "table import_lease"),
    ("DELETE FROM memories_meta WHERE key = 'embedding_change_epoch'", "row memories_meta.embedding_change_epoch"),
    ("DROP INDEX idx_tombstones_hash", "index idx_tombstones_hash"),
    ("DROP TRIGGER trg_embedding_external_insert", "trigger trg_embedding_external_insert"),
])
def test_a_frozen_store_whose_schema_needs_work_refuses_with_a_clear_message(loc, monkeypatch, drop, needle):
    _make_store(loc, drop=drop)
    before = _master(loc)
    _start_frozen("freeze file", monkeypatch)
    with pytest.raises(StoreReadOnlyError, match=r"store loc is frozen; schema upgrade pending \(.*" + needle):
        storage.connect()
    assert _master(loc) == before, "nothing was created"
    with pytest.raises(StoreReadOnlyError, match="schema upgrade pending"):
        storage.connect()  # still refused: not marked as ensured


def test_a_missing_column_is_pending(loc, monkeypatch):
    path = loc
    _make_store(path)
    db = sqlite3.connect(path)  # rebuild tombstone_components without content_hash (an older schema)
    db.execute("DROP INDEX IF EXISTS idx_tombstone_components_hash")
    db.execute("ALTER TABLE tombstone_components DROP COLUMN content_hash")
    db.commit()
    db.close()
    pending = schema.schema_pending(sqlite3.connect(path))
    assert "columns tombstone_components.content_hash" in pending


def test_thaw_then_the_schema_pass_runs_and_writes_work(loc, monkeypatch):
    _make_store(loc, drop="DROP TABLE import_lease")
    _start_frozen("freeze file", monkeypatch)
    with pytest.raises(StoreReadOnlyError, match="schema upgrade pending"):
        storage.connect()
    status, body = admin.thaw_store("loc")
    assert status == 200 and body["state"] == "open"
    conn = storage.connect()
    try:
        conn.execute("INSERT INTO memories (content) VALUES ('after thaw')")
        conn.commit()
    finally:
        conn.close()
    assert schema.schema_pending(sqlite3.connect(loc)) == []


def test_an_open_store_still_runs_the_schema_pass(loc):
    _make_store(loc, drop="DROP TABLE import_lease")
    conn = storage.connect()
    conn.close()
    assert schema.schema_pending(sqlite3.connect(loc)) == []


def test_a_replicated_store_with_old_sync_triggers_is_pending(tmp_path):
    from tests.l3_fakes import local_store

    b = local_store(tmp_path / "r.db")
    conn = b.connect()
    try:
        assert schema.schema_pending(conn) == []
        conn.execute("UPDATE sync_state SET trigger_version = 0 WHERE id = 1")
        conn.commit()
        assert any(p.startswith("sync triggers v0 <") for p in schema.schema_pending(conn))
    finally:
        conn.close()


def test_a_frozen_d1_store_with_a_current_schema_serves_reads(tmp_path, monkeypatch):
    server = FakeD1Server(tmp_path / "d1.db")
    monkeypatch.setattr(backends.D1Connection, "_send", lambda self, sql, params=None: server.send(sql, params))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token")
    # A different registry string for the first phase: the second one then
    # gets a fresh backend object, as a restarted server would.
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"remote": "d1://acct/db1", "other": "d1://acct/db2"}))
    token = storage.CURRENT_DB.set("remote")
    try:
        conn = storage.connect()  # open: the schema pass builds the D1 schema
        conn.execute("INSERT INTO memories (content) VALUES ('on d1')")
        conn.close()
        write_gate.persist_freeze("remote")
        write_gate._reset_for_tests()
        monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"remote": "d1://acct/db1"}))
        assert storage.backend_for("remote").write_gate().state == "frozen"
        conn = storage.connect()
        try:
            assert conn.execute("SELECT content FROM memories").fetchone()[0] == "on d1"
            with pytest.raises(StoreReadOnlyError):
                conn.execute("INSERT INTO memories (content) VALUES ('no')")
        finally:
            conn.close()
    finally:
        storage.CURRENT_DB.reset(token)
