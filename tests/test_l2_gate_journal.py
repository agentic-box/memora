"""L2 piece (a): statement classifier, write gate (freeze barrier), and the
D1 write-ahead intent journal. docs/local-primary-implementation.md §1, §2.9.

Offline only. D1 is a local SQLite file behind a patched D1Connection._send
(the raw HTTP call), so the real D1Backend.connect, D1Connection.execute and
the gated/journaled _execute_api are exercised; the fake can hold a request
after admission, commit then fail with an unknown outcome, or fail
definitely. Kill tests run a real subprocess.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from memora import backends, intent_journal, write_gate
from memora.backends import (D1Backend, D1DefiniteError, D1SelectOnlyConnection, LocalSQLiteBackend,
                             StoreLockedError)
from memora.intent_journal import IntentJournalError
from memora.sql_classify import classify_statement, derive_effect, is_select_only
from memora.write_gate import FreezeTimeout, StoreReadOnlyError

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ helpers

class FakeD1Server:
    """What D1 would do with one statement, on a local SQLite file."""

    def __init__(self, path: Path):
        self.path = path
        self.calls: list = []
        self.hold: threading.Event | None = None
        self.entered = threading.Event()
        self.raise_before: BaseException | None = None
        self.raise_after_commit: BaseException | None = None
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, tags TEXT)")
        db.commit()
        db.close()

    def send(self, sql, params=None):
        self.calls.append(sql)
        self.entered.set()
        if self.hold is not None:
            assert self.hold.wait(10), "test hold never released"
        if self.raise_before is not None:
            raise self.raise_before
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        cur = db.execute(sql, tuple(params or ()))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        db.commit()
        meta = {"changes": cur.rowcount if cur.rowcount >= 0 else 0, "last_row_id": cur.lastrowid or 0,
                "served_by_primary": True}
        db.close()
        if self.raise_after_commit is not None:
            raise self.raise_after_commit
        return {"success": True, "result": [{"results": rows, "meta": meta}]}

    def rows(self):
        db = sqlite3.connect(self.path)
        try:
            return db.execute("SELECT id, content FROM memories ORDER BY id").fetchall()
        finally:
            db.close()


@pytest.fixture()
def d1(tmp_path, monkeypatch):
    server = FakeD1Server(tmp_path / "d1.db")
    monkeypatch.setattr(backends.D1Connection, "_send", lambda self, sql, params=None: server.send(sql, params))
    backend = D1Backend("acct", "db1", "token")
    backend.store_name = "s1"
    return server, backend


def _journal_path() -> Path:
    return write_gate.data_dir() / "intent" / "s1.jsonl"


def _records():
    return [json.loads(line) for line in _journal_path().read_text().splitlines()]


def _restart():
    """A fresh process's view: new gate and journal registries (the journal
    is reopened and replayed from disk)."""
    write_gate._reset_for_tests()
    intent_journal._reset_for_tests()


# ------------------------------------------------------------------ classifier

RETIREMENT_QUERY = """
            WITH ids(id) AS (SELECT value FROM json_each(?))
            SELECT c.memory_id AS id, 'superseded' AS kind
              FROM memories_crossrefs c, json_each(c.related) j
             WHERE c.memory_id IN (SELECT id FROM ids)
            UNION
            SELECT memory_id, 'retired' FROM tombstone_components
             WHERE memory_id IN (SELECT id FROM ids)
            UNION
            SELECT memory_id, 'retired' FROM tombstones
             WHERE memory_id IN (SELECT id FROM ids)
"""


@pytest.mark.parametrize("sql,kind,target", [
    (RETIREMENT_QUERY, "read", None),
    ("-- lead\n/* block */ SELECT 1", "read", None),
    ("WITH x AS (SELECT 1) SELECT * FROM x", "read", None),
    ("WITH x AS (SELECT 1) INSERT INTO memories(content) SELECT * FROM x", "mutation", "memories"),
    ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r LIMIT 3) DELETE FROM memories WHERE id IN r",
     "mutation", "memories"),
    ("INSERT OR IGNORE INTO memories_meta(key, value) VALUES ('a', '0')", "mutation", "memories_meta"),
    ("UPDATE OR REPLACE memories SET tags = ? WHERE id = ?", "mutation", "memories"),
    ("REPLACE INTO memories (id) VALUES (?)", "mutation", "memories"),
    ('insert into "memories" (content) values (?)', "mutation", "memories"),
    ("SELECT 'a;b DELETE FROM x' AS s", "read", None),
    ("SELECT 1; DELETE FROM memories", "unknown", None),
    ("CREATE TRIGGER t AFTER INSERT ON memories BEGIN UPDATE memories_meta SET value = 1; END", "ddl", None),
    ("CREATE TABLE IF NOT EXISTS x (a)", "ddl", None),
    ("PRAGMA table_info(memories)", "read", None),
    ("PRAGMA database_list", "read", None),
    ("PRAGMA journal_mode", "read", None),
    ("PRAGMA journal_mode=WAL", "ddl", None),
    ("PRAGMA optimize", "ddl", None),
    ("PRAGMA wal_checkpoint(TRUNCATE)", "ddl", None),
    ("PRAGMA incremental_vacuum", "ddl", None),
    ("PRAGMA shrink_memory", "ddl", None),
    ("BEGIN IMMEDIATE", "txn", None),
    ("VACUUM", "ddl", None),
    ("FROBNICATE", "unknown", None),
])
def test_classify_statement(sql, kind, target):
    c = classify_statement(sql)
    assert (c.kind, c.target) == (kind, target), c


def test_select_only_accepts_only_select():
    assert is_select_only("SELECT 1") and is_select_only(RETIREMENT_QUERY)
    for sql in ("PRAGMA table_info(memories)", "EXPLAIN SELECT 1", "VALUES (1)", "SELECT 1; SELECT 2",
                "DELETE FROM memories WHERE id = ?", "INSERT INTO memories(id) VALUES (?) ON CONFLICT(id) DO UPDATE SET content=excluded.content",
                "WITH x AS (SELECT 1) DELETE FROM memories", "SELECT 1 RETURNING id", "CREATE TABLE t (a)"):
        assert not is_select_only(sql), sql


def test_derive_effect_shapes():
    assert derive_effect("INSERT INTO memories (content, tags) VALUES (?, ?)", ["c", "[]"]) == (
        "memories", None, {"content": "c", "tags": "[]"})
    assert derive_effect("UPDATE memories SET content = ?, tags = ? WHERE id = ?", ["c", "[]", 5]) == (
        "memories", {"id": 5}, {"content": "c", "tags": "[]"})
    assert derive_effect("DELETE FROM tombstones WHERE content_hash = ? AND memory_id = ?", ["h", 3]) == (
        "tombstones", {"content_hash": "h", "memory_id": 3}, None)
    assert derive_effect("DELETE FROM memories WHERE id IN (SELECT 1)", []) == ("memories", None, None)


# ------------------------------------------------------------------ local gate

def _local(tmp_path):
    backend = LocalSQLiteBackend(tmp_path / "local.db")
    conn = backend.connect()
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    conn.commit()
    conn.close()
    return backend


def test_capabilities():
    assert backends._LockedWriterConnection.supports_transactions is True
    assert backends._LockedReaderConnection.supports_transactions is False
    assert backends.D1Connection.supports_transactions is False


def test_freeze_prior_open_writer_refused_on_next_mutation(tmp_path):
    backend = _local(tmp_path)
    conn = backend.connect()  # opened BEFORE the freeze
    gate = backend.write_gate()
    gate.freeze(timeout_s=1)
    assert gate.state == "frozen"
    with pytest.raises(StoreReadOnlyError):
        conn.execute("INSERT INTO memories (content) VALUES ('x')")
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0  # reads continue
    with pytest.raises(StoreReadOnlyError):
        conn.cursor().execute("INSERT INTO memories (content) VALUES ('x')")  # the raw cursor too
    with pytest.raises(StoreReadOnlyError):
        conn.executescript("INSERT INTO memories (content) VALUES ('x');")
    gate.thaw()
    conn.execute("INSERT INTO memories (content) VALUES ('x')")
    conn.commit()
    conn.close()


def test_freeze_waits_for_an_admitted_local_transaction(tmp_path):
    backend = _local(tmp_path)
    gate = backend.write_gate()
    a = backend.connect()
    a.execute("INSERT INTO memories (content) VALUES ('in-flight')")  # token held (open transaction)
    done = threading.Event()
    t = threading.Thread(target=lambda: (gate.freeze(timeout_s=5), done.set()))
    t.start()
    time.sleep(0.2)
    assert not done.is_set() and gate.state == "draining"
    # the admitted transaction may continue, through a cursor too
    a.cursor().execute("INSERT INTO memories (content) VALUES ('same txn')")
    b = backend.connect()
    with pytest.raises(StoreReadOnlyError):
        b.execute("INSERT INTO memories (content) VALUES ('new')")
    a.commit()
    t.join(5)
    assert done.is_set() and gate.state == "frozen"
    assert b.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    a.close()
    b.close()


def test_context_manager_exit_releases_the_token(tmp_path):
    backend = _local(tmp_path)
    conn = backend.connect()
    with conn:
        conn.execute("INSERT INTO memories (content) VALUES ('x')")
    backend.write_gate().freeze(timeout_s=0.5)  # nothing in flight any more
    conn.close()


def test_close_releases_the_token(tmp_path):
    backend = _local(tmp_path)
    conn = backend.connect()
    conn.execute("INSERT INTO memories (content) VALUES ('x')")
    conn.close()  # uncommitted transaction discarded with the connection
    backend.write_gate().freeze(timeout_s=0.5)


def test_freeze_timeout_aborts_and_reopens(tmp_path):
    backend = _local(tmp_path)
    gate = backend.write_gate()
    a = backend.connect()
    a.execute("INSERT INTO memories (content) VALUES ('stuck')")
    with pytest.raises(FreezeTimeout) as exc:
        gate.freeze(timeout_s=0.2)
    assert exc.value.in_flight and exc.value.in_flight[0]["statement"] == "INSERT"
    assert gate.state == "open"
    a.commit()  # the aborted freeze did not disturb the admitted transaction
    b = backend.connect()
    b.execute("INSERT INTO memories (content) VALUES ('after abort')")  # admission reopened
    b.commit()
    a.close()
    b.close()


def test_replicator_connection_is_exempt(tmp_path):
    backend = _local(tmp_path)
    backend.write_gate().freeze(timeout_s=1)
    r = backend.connect_replicator()
    assert isinstance(r, backends._ReplicatorConnection)
    r.execute("INSERT INTO memories (content) VALUES ('drain')")
    r.commit()
    r._memora_gate = backend.write_gate()  # exempt by class, even with a gate attached
    r.cursor().execute("INSERT INTO memories (content) VALUES ('drain 2')")
    r.commit()
    r.close()


def test_connect_replicator_has_no_callers_outside_backends():
    offenders = []
    for path in (REPO / "memora").rglob("*.py"):
        if path.name == "backends.py":
            continue
        if "connect_replicator(" in path.read_text():
            offenders.append(str(path))
    assert offenders == []  # L3's memora/replicator.py will be the only caller


def test_registry_store_starts_frozen_from_freeze_file_and_readonly_env(tmp_path, monkeypatch):
    from memora import storage

    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db"), "b": str(tmp_path / "b.db")}))
    write_gate.persist_freeze("a")
    monkeypatch.setenv("MEMORA_READONLY_DBS", "b")
    assert write_gate.write_gate("a").state == "frozen"
    assert write_gate.write_gate("b").state == "frozen"
    assert storage.backend_for("a").store_name == "a"
    write_gate.remove_freeze("a")
    assert not write_gate.freeze_file("a").exists()


def test_live_primary_never_immutable(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
    db.commit()
    db.close()  # last close removes the sidecars
    assert not Path(f"{path}-wal").exists()
    backend = LocalSQLiteBackend(path)
    backend.store_name = "p"
    reader = backend.connect_read_only()  # not a live primary: immutable read allowed
    reader.close()
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"p": "d1://a/b"}))
    assert backend.live_primary
    with pytest.raises(StoreLockedError, match="live_primary"):
        backend.connect_read_only()


def test_primary_lock_is_exclusive(tmp_path):
    fd = backends.acquire_primary_lock(tmp_path / "x.db")
    assert backends.acquire_primary_lock(tmp_path / "x.db") == fd  # idempotent within a process
    try:
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from memora import backends\n"
                "try:\n    backends.acquire_primary_lock(%r)\nexcept backends.StoreLockedError:\n    sys.exit(3)\n"
                ) % (str(REPO), str(tmp_path / "x.db"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert r.returncode == 3, r.stderr
    finally:
        backends.release_primary_lock(tmp_path / "x.db")


# ------------------------------------------------------------------ D1 gate + journal

def test_reads_are_neither_gated_nor_journaled(d1):
    server, backend = d1
    conn = backend.connect()
    backend.write_gate().freeze(timeout_s=1)
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0
    assert [r for r in _records() if r["type"] == "intent"] == []


def test_write_is_journaled_then_resolved(d1):
    server, backend = d1
    conn = backend.connect()
    conn.execute("INSERT INTO memories (content, tags) VALUES (?, ?)", ("hello", "[]"))
    recs = _records()
    assert recs[0]["type"] == "intent" and recs[0]["target"] == "memories"
    assert recs[0]["post_state"] == {"content": "hello", "tags": "[]"}
    assert recs[1] == {**recs[1], "type": "resolved", "id": recs[0]["id"], "outcome": "ok"}
    assert backend.write_gate().status()["open_intents"] == []


def test_definite_failure_is_resolved_failed(d1):
    server, backend = d1
    server.raise_before = D1DefiniteError("D1 query failed: constraint")
    with pytest.raises(D1DefiniteError):
        backend.connect().execute("INSERT INTO memories (content) VALUES ('x')")
    assert _records()[-1]["outcome"] == "failed"
    assert backend.write_gate().status()["open_intents"] == []


def test_unknown_outcome_leaves_the_intent_open_and_freeze_unsafe(d1):
    server, backend = d1
    server.raise_after_commit = TimeoutError("read timed out")
    with pytest.raises(TimeoutError):
        backend.connect().execute("INSERT INTO memories (content) VALUES ('maybe')")
    gate = backend.write_gate()
    assert gate.status()["open_intents"] == [1] and gate.status()["in_flight"] == 0
    gate.freeze(timeout_s=1)
    assert gate.state == "frozen-unsafe"
    assert backend.journal().resolve(1, "operator-accepted", durable=True)
    assert gate.state == "frozen"


def test_freeze_waits_for_admitted_d1_write(d1):
    server, backend = d1
    server.hold = threading.Event()
    conn = backend.connect()
    writer = threading.Thread(target=lambda: conn.execute("INSERT INTO memories (content) VALUES ('held')"))
    writer.start()
    assert server.entered.wait(5)
    gate = backend.write_gate()
    frozen = threading.Event()
    t = threading.Thread(target=lambda: (gate.freeze(timeout_s=5), frozen.set()))
    t.start()
    time.sleep(0.2)
    assert not frozen.is_set() and gate.state == "draining"
    with pytest.raises(StoreReadOnlyError):
        backend.connect().execute("INSERT INTO memories (content) VALUES ('late')")
    server.hold.set()
    writer.join(5)
    t.join(5)
    assert frozen.is_set() and gate.state == "frozen"
    assert [r[1] for r in server.rows()] == ["held"]


def test_intent_fsync_failure_blocks_send(d1, monkeypatch):
    server, backend = d1
    conn = backend.connect()

    real_fsync = intent_journal.os.fsync
    calls = {"n": 0}

    def boom_once(fd):  # the intent's fsync fails; the repair's succeed
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "EIO")
        return real_fsync(fd)

    monkeypatch.setattr(intent_journal.os, "fsync", boom_once)
    with pytest.raises(IntentJournalError):
        conn.execute("INSERT INTO memories (content) VALUES ('x')")
    monkeypatch.undo()
    assert server.calls == []
    assert backend.write_gate().status() == {"state": "open", "in_flight": 0, "open_intents": []}


def test_partial_append_then_send_survives_restart(d1, monkeypatch):
    server, backend = d1
    conn = backend.connect()
    real_write = intent_journal._write_all
    calls = {"n": 0}

    def half_then_fail(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            os.write(fd, data[: len(data) // 2])
            raise OSError(28, "ENOSPC")
        return real_write(fd, data)

    monkeypatch.setattr(intent_journal, "_write_all", half_then_fail)
    with pytest.raises(IntentJournalError):
        conn.execute("INSERT INTO memories (content) VALUES ('never sent')")
    assert server.calls == [] and _journal_path().read_bytes() == b""  # repaired to the last newline
    server.raise_after_commit = ConnectionResetError("reset after send")
    with pytest.raises(ConnectionResetError):
        conn.execute("INSERT INTO memories (content) VALUES (?)", ('sent, outcome unknown',))
    _restart()
    j = backend.journal()
    assert j.fatal is None and [r["post_state"]["content"] for r in j.open_intents()] == ["sent, outcome unknown"]


def test_repair_failure_keeps_gate_refusing(d1, monkeypatch):
    server, backend = d1
    conn = backend.connect()
    monkeypatch.setattr(intent_journal, "_write_all", lambda fd, data: (_ for _ in ()).throw(OSError(28, "ENOSPC")))
    monkeypatch.setattr(intent_journal.os, "ftruncate", lambda fd, n: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(IntentJournalError):
        conn.execute("INSERT INTO memories (content) VALUES ('x')")
    monkeypatch.undo()
    gate = backend.write_gate()
    assert gate.state == "frozen-unsafe" and "repair" in gate.status()["journal_error"]
    with pytest.raises(StoreReadOnlyError):
        conn.execute("INSERT INTO memories (content) VALUES ('y')")
    assert server.calls == []


@pytest.mark.parametrize("middle", ["NOT JSON", '{"type":"intent","sql":"no id"}', '{"type":"resolved","id":"1","outcome":"ok"}',
                                    '{"type":"meta","next_id":0}', '{"type":"bogus","id":1}'])
def test_malformed_middle_record_refuses_writes_keeps_reads(d1, middle):
    """A malformed middle record -- unparseable, or a record replay could not
    apply (review 7584 P2) -- makes the journal unusable: this process gets a
    READ-ONLY connection (leader 7583); nothing is discarded."""
    server, backend = d1
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"intent","id":1,"sql":"x"}\n' + middle + '\n{"type":"resolved","id":1,"outcome":"ok"}\n')
    conn = backend.connect()
    assert conn.read_only and "offset 35" in conn.read_only_reason
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0
    with pytest.raises(StoreReadOnlyError):
        conn.execute("INSERT INTO memories (content) VALUES (?)", ("x",))
    assert server.calls == ["SELECT COUNT(*) AS n FROM memories"]
    assert path.read_text().count("\n") == 3  # nothing discarded


def test_torn_final_line_dropped_and_logged(d1, caplog):
    server, backend = d1
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"intent","id":4,"sql":"x"}\n{"type":"resol')
    with caplog.at_level("WARNING"):
        backend.connect()
    assert "torn final line" in caplog.text
    assert path.read_text() == '{"type":"intent","id":4,"sql":"x"}\n'
    assert backend.journal().status()[0] == [4]
    backend.connect().execute("INSERT INTO memories (content) VALUES ('next')")
    assert [r["id"] for r in _records() if r["type"] == "intent"] == [4, 5]


def test_compaction_serialized_with_append(d1, monkeypatch):
    server, backend = d1
    monkeypatch.setenv("MEMORA_INTENT_COMPACT_BYTES", "1")
    server.raise_after_commit = TimeoutError("unknown")
    with pytest.raises(TimeoutError):
        backend.connect().execute("INSERT INTO memories (content) VALUES (?)", ('open one',))  # compacts too
    server.raise_after_commit = None
    j = backend.journal()
    paused, go = threading.Event(), threading.Event()
    real_replace = intent_journal.os.replace

    def slow_replace(a, b):
        paused.set()
        assert go.wait(5)
        return real_replace(a, b)

    monkeypatch.setattr(intent_journal.os, "replace", slow_replace)
    compactor = threading.Thread(target=j.compact)
    compactor.start()
    assert paused.wait(5)
    writer_done = threading.Event()
    writer = threading.Thread(target=lambda: (backend.connect().execute(
        "INSERT INTO memories (content) VALUES (?)", ('during compaction',)), writer_done.set()))
    writer.start()
    time.sleep(0.2)
    assert not writer_done.is_set() and len(server.calls) == 1  # its intent waits on the journal mutex
    go.set()
    compactor.join(5)
    writer.join(5)
    assert writer_done.is_set()
    monkeypatch.setattr(intent_journal.os, "replace", real_replace)
    contents = [r.get("post_state") for r in _records() if r["type"] == "intent"]
    assert {"content": "during compaction"} in contents and {"content": "open one"} in contents


def test_compaction_then_write_survives_restart(d1, monkeypatch):
    server, backend = d1
    backend.connect().execute("INSERT INTO memories (content) VALUES ('a')")
    backend.journal().compact()
    server.raise_after_commit = TimeoutError("unknown")
    with pytest.raises(TimeoutError):
        backend.connect().execute("INSERT INTO memories (content) VALUES (?)", ('after compaction',))
    _restart()
    assert [r["post_state"]["content"] for r in backend.journal().open_intents()] == ["after compaction"]


def test_lock_after_compaction_refused_via_lockfile(d1):
    server, backend = d1
    backend.connect().execute("INSERT INTO memories (content) VALUES ('a')")
    backend.journal().compact()
    lock = write_gate.data_dir() / "intent" / "s1.lock"
    code = ("import fcntl, os, sys\nfd = os.open(%r, os.O_RDWR)\n"
            "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError:\n    sys.exit(3)\n") % str(lock)
    r = subprocess.run([sys.executable, "-c", code], timeout=60)
    assert r.returncode == 3


def test_second_process_journal_is_refused(d1):
    server, backend = d1
    backend.connect()
    lock = write_gate.data_dir() / "intent" / "s1.lock"
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import os; os.environ['MEMORA_DATA_DIR'] = %r\n"
            "from memora.intent_journal import IntentJournal\n"
            "j = IntentJournal('s1').open()\n"
            "sys.exit(3 if j.fatal and 'held by another process' in j.fatal else 0)\n"
            ) % (str(REPO), str(write_gate.data_dir()))
    assert lock.exists()
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 3


def test_stale_fd_refuses_send(d1):
    server, backend = d1
    conn = backend.connect()
    conn.execute("INSERT INTO memories (content) VALUES ('a')")
    path = _journal_path()
    other = path.with_suffix(".other")
    other.write_bytes(path.read_bytes())
    os.replace(other, path)  # the path now names a different inode
    with pytest.raises(IntentJournalError, match="inode mismatch"):
        conn.execute("INSERT INTO memories (content) VALUES ('b')")
    assert server.calls[-1] != "INSERT INTO memories (content) VALUES ('b')"
    assert backend.write_gate().state == "frozen-unsafe"


def test_resolution_append_failure_is_extra_unsafe(d1, monkeypatch):
    server, backend = d1
    real_write = intent_journal._write_all

    def fail_resolutions(fd, data):
        if b'"type":"resolved"' in data:
            raise OSError(28, "ENOSPC")
        return real_write(fd, data)

    monkeypatch.setattr(intent_journal, "_write_all", fail_resolutions)
    backend.connect().execute("INSERT INTO memories (content) VALUES ('ok but unrecorded')")
    assert [r[1] for r in server.rows()] == ["ok but unrecorded"]
    assert backend.write_gate().status()["open_intents"] == [1]
    backend.write_gate().freeze(timeout_s=1)
    assert backend.write_gate().state == "frozen-unsafe"


def test_intent_kill_after_send_before_response(tmp_path):
    """A real process dies after D1 committed and before the response was
    handled; the restarted process sees the intent open."""
    data = tmp_path / "data"
    child = f"""
import os, sys, sqlite3
sys.path.insert(0, {str(REPO)!r})
os.environ['MEMORA_DATA_DIR'] = {str(data)!r}
from memora import backends
db = {str(tmp_path / 'd1.db')!r}
c = sqlite3.connect(db); c.execute('CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, content TEXT)'); c.commit(); c.close()
def send(self, sql, params=None):
    c = sqlite3.connect(db); c.execute(sql, tuple(params or ())); c.commit(); c.close()
    os._exit(137)   # killed between the server-side commit and the response
backends.D1Connection._send = send
b = backends.D1Backend('acct', 'db1', 'tok'); b.store_name = 's1'
b.connect().execute("INSERT INTO memories (content) VALUES (?)", ('committed, response lost',))
"""
    r = subprocess.run([sys.executable, "-c", child], timeout=60)
    assert r.returncode == 137
    os.environ["MEMORA_DATA_DIR"] = str(data)
    try:
        _restart()
        backend = D1Backend("acct", "db1", "tok")
        backend.store_name = "s1"
        gate = backend.write_gate()
        assert [i["post_state"]["content"] for i in backend.journal().open_intents()] == ["committed, response lost"]
        gate.freeze(timeout_s=1)
        assert gate.state == "frozen-unsafe"
        assert backend.journal().resolve(1, "operator-accepted", durable=True)
        assert gate.state == "frozen"
    finally:
        _restart()


def test_select_only_connection_rejects_everything_but_select(monkeypatch):
    conn = D1SelectOnlyConnection("acct", "db1", "read-token")
    posted = []
    monkeypatch.setattr(conn, "_post", lambda body: posted.append(body) or (
        200, None, b'{"success":true,"result":[{"results":[{"n":1}],"meta":{"served_by_primary":true}}]}'))
    assert conn.execute("SELECT 1 AS n") == ([{"n": 1}], {"served_by_primary": True})
    for sql in ("INSERT INTO memories(id, content) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET content=excluded.content",
                "DELETE FROM memories WHERE id = ?", "PRAGMA table_info(memories)", "EXPLAIN SELECT 1",
                "VALUES (1)", "SELECT 1; SELECT 2", "CREATE TABLE t (a)", "SELECT 1 RETURNING id",
                "WITH x AS (SELECT 1) DELETE FROM memories"):
        with pytest.raises(ValueError):
            conn.execute(sql, (1,))
    assert len(posted) == 1
    for name in ("executemany", "executescript", "commit", "execute_batch"):
        assert not hasattr(conn, name)


def test_cursor_is_gated(tmp_path):
    """Plan §9 item (c): the raw cursor is not a bypass, and an admitted
    transaction's cursor shares the connection's token."""
    backend = _local(tmp_path)
    conn = backend.connect()
    cur = conn.cursor()
    assert isinstance(cur, backends._GatedCursor)
    cur.execute("INSERT INTO memories (content) VALUES ('a')")  # takes the token
    gate = backend.write_gate()
    t = threading.Thread(target=lambda: gate.freeze(timeout_s=5))
    t.start()
    time.sleep(0.1)
    conn.cursor().executemany("INSERT INTO memories (content) VALUES (?)", [("b",), ("c",)])  # same txn
    conn.commit()
    t.join(5)
    assert gate.state == "frozen"
    with pytest.raises(StoreReadOnlyError):
        conn.cursor().execute("DELETE FROM memories")
    conn.close()


def test_setup_statements_are_not_gated(tmp_path, monkeypatch):
    """Plan §9 item (b): connect() arms the gate only after its own setup on
    the raw connection (today one read; L4 adds WAL/busy_timeout there)."""
    backend = _local(tmp_path)
    entered = []
    real_enter = write_gate._WriteGate.enter
    monkeypatch.setattr(write_gate._WriteGate, "enter", lambda self, desc: entered.append(desc) or real_enter(self, desc))
    conn = backend.connect()
    assert entered == [] and conn._memora_gate is backend.write_gate()
    conn.close()



# ------------------------------------------------------------------ round 2 (review 7584)

def test_no_writable_data_dir_gives_a_read_only_connection(d1, tmp_path, monkeypatch):
    server, backend = d1
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the data dir should be")
    monkeypatch.setenv("MEMORA_DATA_DIR", str(blocker))
    _restart()
    conn = backend.connect()
    assert conn.read_only and "cannot open" in conn.read_only_reason
    conn.execute("SELECT 1")
    for attempt in (
        lambda: conn.execute("INSERT INTO memories (content) VALUES (?)", ("x",)),
        lambda: conn.executemany("INSERT INTO memories (content) VALUES (?)", [("x",)]),
        lambda: conn.executescript("DELETE FROM memories; DELETE FROM memories"),
        lambda: conn.execute("PRAGMA journal_mode=WAL"),
        lambda: conn.execute("CREATE TABLE t (a)"),
        lambda: conn.execute("PRAGMA optimize"),
    ):
        # refused by the connection itself, before the gate is consulted
        with pytest.raises(StoreReadOnlyError, match="read-only D1 connection"):
            attempt()
    with pytest.raises(StoreReadOnlyError):
        conn.execute_batch([("DELETE FROM memories WHERE id = ?", (1,))])
    assert server.calls == ["SELECT 1"]


def test_schema_connect_runs_no_ddl_on_a_read_only_connection(d1, tmp_path, monkeypatch):
    from memora import schema

    server, backend = d1
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("MEMORA_DATA_DIR", str(blocker))
    _restart()
    conn = schema.connect(backend)
    assert conn.read_only and server.calls == []  # no CREATE TABLE IF NOT EXISTS, no INSERT OR IGNORE


def test_second_process_gets_read_only_and_writer_is_unaffected(d1):
    """Process A (this test) holds the journal; process B connects: reads
    work, every mutation is refused, schema setup sends nothing."""
    server, backend = d1
    backend.connect().execute("INSERT INTO memories (content) VALUES (?)", ("from A",))
    child = f"""
import os, sys, sqlite3, json
sys.path.insert(0, {str(REPO)!r})
os.environ['MEMORA_DATA_DIR'] = {str(write_gate.data_dir())!r}
from memora import backends, schema
from memora.write_gate import StoreReadOnlyError
sent = []
def send(self, sql, params=None):
    sent.append(sql)
    c = sqlite3.connect({str(server.path)!r}); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, tuple(params or ())).fetchall()]; c.close()
    return {{"success": True, "result": [{{"results": rows, "meta": {{}}}}]}}
backends.D1Connection._send = send
b = backends.D1Backend('acct', 'db1', 'tok'); b.store_name = 's1'
conn = schema.connect(b)
out = {{"read_only": conn.read_only, "rows": conn.execute("SELECT content FROM memories").fetchall()[0]["content"]}}
try:
    conn.execute("INSERT INTO memories (content) VALUES (?)", ("from B",)); out["write"] = "sent"
except StoreReadOnlyError:
    out["write"] = "refused"
out["sent"] = sent
print(json.dumps(out))
"""
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"read_only": True, "rows": "from A", "write": "refused",
                   "sent": ["SELECT content FROM memories"]}
    backend.connect().execute("INSERT INTO memories (content) VALUES (?)", ("A again",))  # writer unaffected
    assert [r[1] for r in server.rows()] == ["from A", "A again"]


def test_existing_connection_refuses_once_the_journal_breaks(d1):
    server, backend = d1
    conn = backend.connect()
    conn.execute("INSERT INTO memories (content) VALUES (?)", ("ok",))
    backend.journal().broken = "simulated: failed repair"
    with pytest.raises(StoreReadOnlyError):
        conn.execute("INSERT INTO memories (content) VALUES (?)", ("refused",))
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1
    assert backend.connect().read_only  # new connections are read-only too


def test_failed_compaction_does_not_send_the_triggering_write(d1, monkeypatch):
    """Review 7584 P1-3, the reviewer's reproduction: threshold 1 and a
    directory fsync that fails after the compaction's rename."""
    server, backend = d1
    conn = backend.connect()  # journal open before the fault is injected
    monkeypatch.setenv("MEMORA_INTENT_COMPACT_BYTES", "1")

    def eio(path):
        raise OSError(5, "EIO")

    monkeypatch.setattr(intent_journal, "_fsync_dir", eio)
    with pytest.raises(IntentJournalError, match="not sent"):
        conn.execute("INSERT INTO memories (content) VALUES (?)", ("x",))
    ids, broken = backend.journal().status()
    assert server.calls == [] and broken and ids == []


def test_journal_rechecked_immediately_before_send(d1, monkeypatch):
    server, backend = d1
    conn = backend.connect()
    j = backend.journal()
    real = j.append_intent

    def append_then_break(*a, **k):
        iid = real(*a, **k)
        j.broken = "broke between the append and the send"
        return iid

    monkeypatch.setattr(j, "append_intent", append_then_break)
    with pytest.raises(IntentJournalError, match="not sent"):
        conn.execute("INSERT INTO memories (content) VALUES (?)", ("x",))
    assert server.calls == [] and j.status()[0] == []


def _db_digest(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_second_local_primary_process_is_fenced(tmp_path):
    """Review 7584 P1-1: process A holds the primary lock; process B starts
    (fence before prewarm), and no mutation -- not even schema setup --
    reaches the SQLite file."""
    path = tmp_path / "p.db"
    b0 = LocalSQLiteBackend(path)
    with b0.connect() as c:
        c.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
        c.execute("INSERT INTO memories (content) VALUES ('a')")
    fd = backends.acquire_primary_lock(path)  # A: memora-all
    before = _db_digest(path)
    child = f"""
import os, sys, json
sys.path.insert(0, {str(REPO)!r})
os.environ['MEMORA_DATA_DIR'] = {str(write_gate.data_dir())!r}
os.environ['MEMORA_DATABASES'] = json.dumps({{"p": {str(path)!r}, "q": {str(tmp_path / 'q.db')!r}}})
os.environ['MEMORA_REPLICAS'] = json.dumps({{"p": "d1://acct/db"}})
os.environ['MEMORA_DEFAULT_DB'] = sys.argv[1]
from memora import server, storage, backends
try:
    server._fence_live_primaries_or_exit()
except SystemExit as e:
    print(json.dumps({{"exit": e.code}})); sys.exit(0)
def _fresh():
    b = backends.LocalSQLiteBackend({str(path)!r}); b.store_name = "p"; return b
out = {{"refused": bool(storage.backend_for("p").refused_reason)}}
for label, fn in (("storage.connect", lambda: (storage.CURRENT_DB.set("p"), storage.connect())),
                  ("fresh backend writer", lambda: _fresh().connect())):
    try:
        fn(); out[label] = "opened"
    except backends.StoreLockedError:
        out[label] = "refused"
print(json.dumps(out))
"""
    try:
        r = subprocess.run([sys.executable, "-c", child, "p"], capture_output=True, text=True, timeout=60)
        assert json.loads(r.stdout.strip().splitlines()[-1]) == {"exit": 2}, r.stderr
        r = subprocess.run([sys.executable, "-c", child, "q"], capture_output=True, text=True, timeout=60)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out == {"refused": True, "storage.connect": "refused", "fresh backend writer": "refused"}, r.stderr
        assert _db_digest(path) == before  # no row, no schema, nothing
        assert not Path(f"{path}-wal").exists()
    finally:
        backends.release_primary_lock(path)


def test_append_intent_itself_refuses_after_its_compaction_breaks(tmp_path, monkeypatch):
    """The journal's own check (independent of _execute_api's re-check)."""
    j = intent_journal.IntentJournal("direct", tmp_path / "intent").open()
    monkeypatch.setenv("MEMORA_INTENT_COMPACT_BYTES", "1")
    monkeypatch.setattr(intent_journal, "_fsync_dir", lambda p: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(IntentJournalError, match="not sent"):
        j.append_intent("INSERT INTO memories (content) VALUES (?)", ["x"], target="memories", keys=None,
                        post_state={"content": "x"})
    assert j.status()[0] == [] and j.status()[1]
    j.close()


# ------------------------------------------------------------------ round 3 (review 7588)

def _aliases(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    db = real_dir / "primary.db"
    sqlite3.connect(db).close()
    file_alias = tmp_path / "alias.db"
    file_alias.symlink_to(db)
    dir_alias = tmp_path / "dirlink"
    dir_alias.symlink_to(real_dir)
    return db, file_alias, dir_alias / "primary.db"


def test_symlink_aliases_name_the_same_lock(tmp_path):
    db, file_alias, dir_alias = _aliases(tmp_path)
    assert backends.primary_lock_path(file_alias) == backends.primary_lock_path(db) \
        == backends.primary_lock_path(dir_alias)
    fd = backends.acquire_primary_lock(db)
    try:
        assert backends.acquire_primary_lock(file_alias) == fd  # same process, same lock
        assert backends.acquire_primary_lock(dir_alias) == fd
    finally:
        backends.release_primary_lock(file_alias)  # released through the alias: same identity
    assert not backends._PRIMARY_LOCKS


def test_second_process_through_a_symlink_is_refused(tmp_path):
    db, file_alias, dir_alias = _aliases(tmp_path)
    backends.acquire_primary_lock(db)  # process A, direct path
    try:
        for alias in (file_alias, dir_alias):
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from memora import backends\n"
                    "try:\n    backends.acquire_primary_lock(%r)\nexcept backends.StoreLockedError:\n    sys.exit(3)\n"
                    ) % (str(REPO), str(alias))
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
            assert r.returncode == 3, (alias, r.stderr)
    finally:
        backends.release_primary_lock(db)


@pytest.mark.parametrize("second", ["file_alias", "dir_alias", "dotdot"])
def test_registry_with_two_aliases_of_one_file_refuses_to_start(tmp_path, monkeypatch, second):
    from memora import server

    db, file_alias, dir_alias = _aliases(tmp_path)
    other = {"file_alias": file_alias, "dir_alias": dir_alias,
             "dotdot": tmp_path / "real" / ".." / "real" / "primary.db"}[second]
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(db), "b": str(other)}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "a")
    with pytest.raises(write_gate.RegistryAliasError, match="'a' and 'b'"):
        write_gate.check_registry_aliases()
    with pytest.raises(SystemExit) as exc:
        server._fence_live_primaries_or_exit()
    assert exc.value.code == 2


def test_registry_with_two_names_for_one_d1_database_refuses(monkeypatch):
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": "d1://acct/db1", "b": "d1://acct/db1", "c": "d1://acct/db2"}))
    with pytest.raises(write_gate.RegistryAliasError):
        write_gate.check_registry_aliases()


def test_distinct_stores_pass_the_alias_check(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"a": str(tmp_path / "a.db"), "b": str(tmp_path / "b.db"),
                                                         "c": "d1://acct/db1"}))
    write_gate.check_registry_aliases()
