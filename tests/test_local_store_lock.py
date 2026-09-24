"""The per-store reader-writer lock that makes local read-only reads create
no files (memora.backends: LocalSQLiteBackend.connect / connect_read_only).

Phase 0 round 5 (review 7308, 7317): a pre-open stat cannot enforce "reads
never create files" -- a writer closing between the check and the open
deletes the WAL sidecars and the open recreates them. In-process writers now
open and close under the exclusive side, and a read holds the shared side
for its whole duration.
"""

import ast
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from memora import backends
from memora.backends import LocalSQLiteBackend, StoreLockedError

ROOT = Path(__file__).resolve().parents[1]


def _wal_store(tmp_path):
    """A WAL store with an IN-PROCESS writer open (memora's own writer path)."""
    db = tmp_path / "w.db"
    backend = LocalSQLiteBackend(db)
    writer = backend.connect(check_same_thread=False)  # closed from another thread in the race tests
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    writer.execute("INSERT INTO memories (content) VALUES ('committed')")
    writer.commit()
    assert sorted(os.listdir(tmp_path)) == ["w.db", "w.db-shm", "w.db-wal"]
    return backend, writer


def _started(fn):
    done = threading.Event()
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # surfaced by the test
            out["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done, out


def test_the_reviewers_race_a_writer_closing_at_the_open_window_waits(tmp_path, monkeypatch):
    """Reproduce review 7308: the writer closes exactly between the
    sidecar check and sqlite3.connect. With the lock the close waits for the
    read, which reads through the existing sidecars; nothing is created."""
    backend, writer = _wal_store(tmp_path)
    real_connect = sqlite3.connect
    state = {}

    def connect(*args, **kwargs):
        if kwargs.get("uri") and "closer" not in state:
            # The observe-then-open window: the writer tries to close now.
            state["closer"] = _started(writer.close)
            state["closer_done_during_open"] = state["closer"][0].wait(0.3)
            state["files_at_open"] = sorted(os.listdir(tmp_path))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(backends.sqlite3, "connect", connect)
    reader = backend.connect_read_only()
    monkeypatch.setattr(backends.sqlite3, "connect", real_connect)
    rows = [r[0] for r in reader.execute("SELECT content FROM memories")]
    assert rows == ["committed"]
    assert state["closer_done_during_open"] is False  # the close waited for the read
    assert state["files_at_open"] == ["w.db", "w.db-shm", "w.db-wal"]
    assert sorted(os.listdir(tmp_path)) == ["w.db", "w.db-shm", "w.db-wal"]  # still open, nothing new
    reader.close()
    closer_done, closer_out = state["closer"]
    assert closer_done.wait(5) and "error" not in closer_out
    # The writer's close (after the read) removed the sidecars; the read created none.
    assert sorted(os.listdir(tmp_path)) == ["w.db"]


def test_a_writer_open_during_an_immutable_read_blocks_until_the_read_ends(tmp_path):
    backend, writer = _wal_store(tmp_path)
    writer.close()
    assert sorted(os.listdir(tmp_path)) == ["w.db"]
    reader = backend.connect_read_only()  # no in-process writer, no sidecars: immutable
    try:
        opened, out = _started(lambda: backend.connect(check_same_thread=False))
        assert opened.wait(0.3) is False  # the writer's open waits for the read
        assert reader.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert sorted(os.listdir(tmp_path)) == ["w.db"]
    finally:
        reader.close()
    assert opened.wait(5) and "error" not in out
    out["value"].close()


def test_a_read_sees_committed_data_of_an_open_in_process_writer(tmp_path):
    backend, writer = _wal_store(tmp_path)
    writer.execute("INSERT INTO memories (content) VALUES ('second')")
    writer.commit()
    reader = backend.connect_read_only()
    try:
        assert [r[0] for r in reader.execute("SELECT content FROM memories ORDER BY id")] == [
            "committed", "second"]
    finally:
        reader.close()
        writer.close()


def test_opening_a_writer_while_holding_a_read_on_the_same_store_fails_loudly(tmp_path):
    backend, writer = _wal_store(tmp_path)
    writer.close()
    reader = backend.connect_read_only()
    try:
        with pytest.raises(RuntimeError, match="would deadlock"):
            backend.connect()
    finally:
        reader.close()


def test_rollback_journal_store_reads_create_nothing_with_a_writer_open(tmp_path):
    db = tmp_path / "r.db"
    backend = LocalSQLiteBackend(db)
    writer = backend.connect()
    writer.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
    writer.commit()
    before = sorted(os.listdir(tmp_path))
    reader = backend.connect_read_only()
    assert reader.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    reader.close()
    writer.close()
    assert sorted(os.listdir(tmp_path)) == before == ["r.db"]


def test_a_garbage_collected_connection_releases_its_side(tmp_path):
    backend, writer = _wal_store(tmp_path)
    del writer  # closed by the GC: exclusive taken and released
    import gc
    gc.collect()
    lock = backends._store_lock(tmp_path / "w.db")
    assert lock.open_writers == 0
    reader = backend.connect_read_only()
    del reader
    gc.collect()
    w2 = backend.connect()  # would block forever if the reader's shared side leaked
    w2.close()


# --- every in-process writer path goes through the lock (review 7317) -------------

# Every place in memora/ that opens a SQLite connection itself. A new one fails
# this test until it is reviewed: a local-store writer MUST go through
# LocalSQLiteBackend.connect (exclusive side), a read through
# connect_read_only (shared side).
EXPECTED_SQLITE_CONNECT_SITES = {
    # writers (connect, and the gate-exempt connect_replicator): exclusive side
    ("backends.py", "LocalSQLiteBackend._open_writer"),
    ("backends.py", "LocalSQLiteBackend.connect_read_only"),  # reader: shared side
    # S3 cloud stores: a local CACHE copy, synced from/to object storage.
    # Outside the local-store guarantee (their reads sync the cache file).
    ("backends.py", "CloudSQLiteBackend._create_and_upload_empty_database"),
    ("backends.py", "CloudSQLiteBackend._create_local_database_only"),
    ("backends.py", "CloudSQLiteBackend.connect"),
    # L5 operator tool: scratch files only (export verification, a seed
    # before it becomes a store); never a live store.
    ("local_primary.py", "_scratch_connect"),
    # X2: the in-memory reference schema (":memory:"), never a store file.
    ("schema.py", "_reference_schema"),
}
# Every storage backend class; each one's connect() is the only way a
# connection to its store is made (storage.connect, schema.connect, the graph
# server, CLI, sync, import, rebuild, backfill and absorb all call
# backend.connect() / connect_read_only()).
EXPECTED_BACKENDS = {"StorageBackend", "LocalSQLiteBackend", "CloudSQLiteBackend", "D1Backend"}


def _qualname_sites():
    sites = set()
    backends_with_connect = set()
    for path in (ROOT / "memora").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if any(isinstance(f, ast.FunctionDef) and f.name == "connect" for f in cls.body):
                backends_with_connect.add(cls.name)

        def visit(node, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, f"{prefix}.{child.name}" if prefix else child.name)
                    continue
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "connect" and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "sqlite3"):
                    sites.add((path.name, prefix))
                visit(child, prefix)

        visit(tree, "")
    return sites, backends_with_connect


def test_every_sqlite_connection_site_is_known():
    sites, backends_with_connect = _qualname_sites()
    assert sites == EXPECTED_SQLITE_CONNECT_SITES, (
        "a new sqlite3.connect site: a local-store writer must go through "
        "LocalSQLiteBackend.connect (exclusive side of the store lock)")
    # D1Backend/others define connect without sqlite3 (HTTP); a new backend fails here.
    assert backends_with_connect - {"_LockedWriterConnection", "_LockedReaderConnection"} <= EXPECTED_BACKENDS | {
        "D1Connection"}


def test_the_local_writer_connect_uses_the_locked_factory(tmp_path):
    conn = LocalSQLiteBackend(tmp_path / "x.db").connect()
    try:
        assert isinstance(conn, backends._LockedWriterConnection)
        assert conn._memora_lock is backends._store_lock(tmp_path / "x.db")
    finally:
        conn.close()


def test_a_just_opened_idle_writer_already_has_its_sidecars(tmp_path):
    """The writer's open touches the database under the exclusive side, so
    a WAL store's sidecars exist for as long as it is open -- a read can use
    them even before the writer's first statement."""
    backend, writer = _wal_store(tmp_path)
    writer.close()
    idle = backend.connect()  # no statement of its own yet
    try:
        assert sorted(os.listdir(tmp_path)) == ["w.db", "w.db-shm", "w.db-wal"]
        reader = backend.connect_read_only()
        assert reader.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        reader.close()
    finally:
        idle.close()
    assert sorted(os.listdir(tmp_path)) == ["w.db"]


def test_a_writer_collected_while_the_store_is_held_exclusive_does_not_deadlock(tmp_path):
    """Regression: the GC can finalize an unclosed writer connection while
    the same thread is inside the exclusive side (opening another one)."""
    import gc

    backend = LocalSQLiteBackend(tmp_path / "g.db")
    lock = backends._store_lock(tmp_path / "g.db")

    def scenario():
        held = [backend.connect()]  # never closed explicitly
        with lock.exclusive():
            held.clear()
            gc.collect()  # its __del__ -> close -> exclusive, re-entered by this thread
        return lock.open_writers

    done, out = _started(scenario)
    assert done.wait(5), "deadlocked"
    assert "error" not in out and out["value"] == 0


# --- round 6: bookkeeping only after a successful close (review 7345) ---------------

def test_a_failed_cross_thread_close_leaves_the_writer_open_and_locked(tmp_path, monkeypatch):
    """Review 7345: a wrong-thread close used to mark the writer closed while
    SQLite kept it open; the owner's later close then bypassed the lock and
    deleted the sidecars inside a read's pre-open window."""
    db = tmp_path / "w.db"
    backend = LocalSQLiteBackend(db)
    writer = backend.connect()  # check_same_thread=True, owned by this thread
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    writer.execute("INSERT INTO memories (content) VALUES ('committed')")
    writer.commit()
    lock = backends._store_lock(db)

    failed, out = _started(writer.close)  # wrong thread
    assert failed.wait(5) and isinstance(out.get("error"), sqlite3.ProgrammingError)
    assert lock.open_writers == 1 and writer._memora_closed is False
    assert writer.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1  # still open

    real_connect = sqlite3.connect
    in_window, go = threading.Event(), threading.Event()
    seen = {}

    def connect(*args, **kwargs):
        if kwargs.get("uri"):
            in_window.set()
            go.wait(5)
            time.sleep(0.3)  # the owner's close is attempted meanwhile
            seen["closed_during_window"] = seen.get("closed", False)
            seen["files"] = sorted(os.listdir(tmp_path))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(backends.sqlite3, "connect", connect)

    def read():
        conn = backend.connect_read_only()
        rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        return rows

    read_done, read_out = _started(read)
    assert in_window.wait(5)
    go.set()
    writer.close()  # owner thread: must take the exclusive side, i.e. wait for the read
    seen["closed"] = True
    monkeypatch.setattr(backends.sqlite3, "connect", real_connect)
    assert read_done.wait(5) and read_out.get("value") == 1, read_out
    assert seen["closed_during_window"] is False
    assert seen["files"] == ["w.db", "w.db-shm", "w.db-wal"]
    assert lock.open_writers == 0 and sorted(os.listdir(tmp_path)) == ["w.db"]


def test_a_failed_cross_thread_reader_close_keeps_the_shared_hold(tmp_path):
    backend, writer = _wal_store(tmp_path)
    writer.close()
    reader = backend.connect_read_only()  # owned by this thread
    failed, out = _started(reader.close)
    assert failed.wait(5) and isinstance(out.get("error"), sqlite3.ProgrammingError)
    assert reader.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1  # still open
    opened, wout = _started(lambda: backend.connect(check_same_thread=False))
    assert opened.wait(0.3) is False  # still protected: the writer's open waits
    reader.close()
    assert opened.wait(5) and "error" not in wout
    wout["value"].close()


def test_a_gc_close_on_a_thread_holding_a_read_is_deferred_until_the_read_ends(tmp_path):
    import gc

    backend, writer = _wal_store(tmp_path)
    lock = backends._store_lock(tmp_path / "w.db")
    reader = backend.connect_read_only()  # mode=ro through the writer's sidecars
    del writer
    gc.collect()  # this thread holds a read: the close must not run now, nor block
    assert lock.open_writers == 1
    assert sorted(os.listdir(tmp_path)) == ["w.db", "w.db-shm", "w.db-wal"]
    assert reader.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    reader.close()  # releasing the read drains the deferred close, under the exclusive side
    assert lock.open_writers == 0 and lock._deferred == []
    assert sorted(os.listdir(tmp_path)) == ["w.db"]


def test_a_gc_close_never_blocks_while_another_thread_reads(tmp_path):
    import gc

    backend, writer = _wal_store(tmp_path)
    lock = backends._store_lock(tmp_path / "w.db")
    reader_ready, release = threading.Event(), threading.Event()

    def hold_read():
        conn = backend.connect_read_only()
        reader_ready.set()
        release.wait(5)
        conn.close()

    done, _ = _started(hold_read)
    assert reader_ready.wait(5)
    t0 = time.perf_counter()
    del writer
    gc.collect()  # another thread reads: deferred, not waited for
    assert time.perf_counter() - t0 < 0.5
    assert lock.open_writers == 1 and len(lock._deferred) == 1
    release.set()
    assert done.wait(5)
    assert lock.open_writers == 0 and sorted(os.listdir(tmp_path)) == ["w.db"]


def test_a_failing_underlying_close_changes_no_bookkeeping(tmp_path, monkeypatch):
    backend, writer = _wal_store(tmp_path)
    lock = backends._store_lock(tmp_path / "w.db")
    real = backends._sqlite_close

    def boom(conn):
        raise sqlite3.OperationalError("injected close failure")

    monkeypatch.setattr(backends, "_sqlite_close", boom)
    with pytest.raises(sqlite3.OperationalError):
        writer.close()
    assert lock.open_writers == 1 and writer._memora_closed is False
    reader = backend.connect_read_only()
    with pytest.raises(sqlite3.OperationalError):
        reader.close()
    assert lock._readers  # the shared hold is kept
    monkeypatch.setattr(backends, "_sqlite_close", real)
    reader.close()
    assert not lock._readers
    writer.close()
    assert lock.open_writers == 0 and sorted(os.listdir(tmp_path)) == ["w.db"]


# --- round 7: thread checks mirror stock sqlite3; deferred closes drain promptly (review 7360) ---

def test_every_public_method_is_wrapped_on_this_python():
    """A new Python that adds an unchecked method fails here."""
    for native, wrapper, unchecked in (
        (sqlite3.Connection, backends._ThreadCheckedConnection, backends._CONNECTION_UNCHECKED),
        (sqlite3.Cursor, backends._ThreadCheckedCursor, backends._CURSOR_UNCHECKED),
    ):
        names = backends._public_callables(native)
        assert names, native
        for name in names:
            if name in unchecked:
                continue
            assert getattr(wrapper, name) is not getattr(native, name), f"{native.__name__}.{name} is not wrapped"
    if hasattr(sqlite3.Connection, "autocommit"):
        assert isinstance(backends._ThreadCheckedConnection.__dict__["autocommit"], property)


def _foreign_ops(conn, cur, dest, made):
    return {
        # Objects the OWNER thread got from the convenience methods (round 8).
        "cursor from conn.execute: execute(INSERT)": lambda: made["exec"].execute("INSERT INTO t VALUES (7)"),
        "cursor from conn.execute: fetchone": lambda: made["exec"].fetchone(),
        "cursor from conn.executemany: execute(INSERT)": lambda: made["many"].execute("INSERT INTO t VALUES (7)"),
        "cursor from conn.executescript: execute(INSERT)": lambda: made["script"].execute("INSERT INTO t VALUES (7)"),
        "blob.read": lambda: made["blob"].read(1),
        "blob.write": lambda: made["blob"].write(b"a"),
        "len(blob)": lambda: len(made["blob"]),
        "blob[0]": lambda: made["blob"][0],
        "blob.close": lambda: made["blob"].close(),
        "next(iterdump made on the owner)": lambda: next(made["dump"]),
        "cursor.execute(INSERT) from a cursor made on the owner thread":
            lambda: cur.execute("INSERT INTO t VALUES (9)"),
        "cursor.fetchall": lambda: cur.fetchall(),
        "cursor.__next__": lambda: next(cur),
        "cursor.__iter__": lambda: iter(cur),
        "cursor.setinputsizes": lambda: cur.setinputsizes([]),
        "conn.backup": lambda: conn.backup(dest),
        "conn.__exit__": lambda: conn.__exit__(None, None, None),
        "conn.__enter__": lambda: conn.__enter__(),
        "conn.execute": lambda: conn.execute("SELECT 1"),
        "conn.executescript": lambda: conn.executescript("SELECT 1;"),
        "conn.commit": lambda: conn.commit(),
        "conn.rollback": lambda: conn.rollback(),
        "conn.cursor": lambda: conn.cursor(),
        "conn.iterdump": lambda: next(conn.iterdump()),
        "conn.create_function": lambda: conn.create_function("f", 0, lambda: 1),
        "conn.blobopen": lambda: conn.blobopen("t", "x", 1),
        "conn.serialize": lambda: conn.serialize(),
        "conn.interrupt": lambda: conn.interrupt(),
        "conn.total_changes": lambda: conn.total_changes,
        "conn.autocommit": lambda: conn.autocommit,
        "conn.close": lambda: conn.close(),
    }


_HAS_BLOBOPEN = hasattr(sqlite3.Connection, "blobopen")


def _outcomes(make_conn):
    conn = make_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.execute("CREATE TABLE IF NOT EXISTS b (x BLOB)")
    conn.execute("INSERT INTO b VALUES (zeroblob(4))")
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT x FROM t")
    made = {
        "exec": conn.execute("SELECT x FROM t"),
        "many": conn.executemany("INSERT INTO t VALUES (?)", [(2,)]),
        "script": conn.executescript("SELECT 1;"),
        # Connection.blobopen is Python 3.11+; the 3.10 CI leg has no blobs.
        "blob": conn.blobopen("b", "x", 1) if _HAS_BLOBOPEN else None,
        "dump": conn.iterdump(),
    }
    conn.commit()
    dest = sqlite3.connect(":memory:", check_same_thread=False)
    ops = _foreign_ops(conn, cur, dest, made)
    result = {}

    def run():
        for name, op in ops.items():
            if not hasattr(sqlite3.Connection, "autocommit") and name == "conn.autocommit":
                continue
            if not _HAS_BLOBOPEN and "blob" in name:
                continue
            if not hasattr(sqlite3.Connection, "serialize") and name == "conn.serialize":
                continue
            try:
                op()
                result[name] = "ok"
            except sqlite3.ProgrammingError as exc:
                result[name] = "ProgrammingError" if "thread" in str(exc) else f"other: {exc}"
            except Exception as exc:
                result[name] = type(exc).__name__

    t = threading.Thread(target=run)
    t.start()
    t.join()
    conn.close()
    return result


def test_foreign_thread_behaviour_is_identical_to_native_sqlite3(tmp_path):
    native = _outcomes(lambda: sqlite3.connect(tmp_path / "native.db"))
    ours = _outcomes(lambda: LocalSQLiteBackend(tmp_path / "ours.db").connect())
    assert ours == native
    assert native["cursor.execute(INSERT) from a cursor made on the owner thread"] == "ProgrammingError"
    assert native["conn.backup"] == native["conn.__exit__"] == "ProgrammingError"
    for name, outcome in native.items():
        if name.startswith(("cursor from", "blob.", "len(blob)", "blob[0]", "next(iterdump")):
            assert outcome == "ProgrammingError", name
    with sqlite3.connect(tmp_path / "ours.db") as check:  # nothing was written from the foreign thread
        assert sorted(r[0] for r in check.execute("SELECT x FROM t")) == [1, 2]


def test_a_read_only_connection_mirrors_native_too(tmp_path):
    db = tmp_path / "ro.db"
    w = LocalSQLiteBackend(db).connect()
    w.execute("CREATE TABLE t (x)")
    w.commit()
    w.close()
    reader = LocalSQLiteBackend(db).connect_read_only()
    out = {}

    def run():
        for name, op in {"execute": lambda: reader.execute("SELECT 1"),
                         "cursor": lambda: reader.cursor(),
                         "close": lambda: reader.close()}.items():
            try:
                op()
                out[name] = "ok"
            except sqlite3.ProgrammingError:
                out[name] = "ProgrammingError"

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert out == {"execute": "ProgrammingError", "cursor": "ProgrammingError", "close": "ProgrammingError"}
    reader.close()


def test_a_deferred_close_goes_before_a_queued_writer_when_the_last_read_ends(tmp_path):
    import gc

    backend, writer = _wal_store(tmp_path)
    lock = backends._store_lock(tmp_path / "w.db")
    reader = backend.connect_read_only()
    del writer
    gc.collect()  # deferred: this thread holds a read
    assert len(lock._deferred) == 1
    opened, out = _started(lambda: backend.connect(check_same_thread=False))
    for _ in range(100):  # the writer is queued behind the read
        if lock._waiting_writers:
            break
        time.sleep(0.01)
    assert lock._waiting_writers == 1
    reader.close()
    assert opened.wait(5) and "error" not in out
    assert lock._deferred == [] and lock.open_writers == 1  # the deferred one closed; the new one open
    out["value"].close()
    assert lock.open_writers == 0


def test_a_deferred_close_completes_within_one_section_under_writer_churn(tmp_path):
    backend, writer = _wal_store(tmp_path)
    lock = backends._store_lock(tmp_path / "w.db")
    stop = threading.Event()

    def churn():
        while not stop.is_set():
            backend.connect(check_same_thread=False).close()

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        time.sleep(0.05)
        with lock._cond:
            lock._deferred.append(writer)  # as the GC would, while writers keep queueing
        deadline = time.time() + 2
        while lock._deferred and time.time() < deadline:
            time.sleep(0.005)
        assert lock._deferred == [] and writer._memora_closed is True
    finally:
        stop.set()
        t.join(5)
    assert lock.open_writers == 0



def test_convenience_methods_return_checked_cursors_and_refuse_foreign_writes(tmp_path):
    db = tmp_path / "c.db"
    conn = LocalSQLiteBackend(db).connect()
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    cursors = {
        "execute": conn.execute("SELECT 1"),
        "executemany": conn.executemany("INSERT INTO t VALUES (?)", [(1,)]),
        "executescript": conn.executescript("SELECT 1;"),
    }
    conn.commit()
    assert all(isinstance(c, backends._ThreadCheckedCursor) for c in cursors.values())
    refused = {}

    def run():
        for name, c in cursors.items():
            try:
                c.execute("INSERT INTO t VALUES (99)")
                refused[name] = False
            except sqlite3.ProgrammingError:
                refused[name] = True

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert refused == {"execute": True, "executemany": True, "executescript": True}
    assert [r[0] for r in conn.execute("SELECT x FROM t")] == [1]  # nothing written
    conn.close()


def test_every_public_blob_method_is_wrapped():
    if not hasattr(sqlite3, "Blob"):
        pytest.skip("no sqlite3.Blob on this Python")
    names = [n for n in dir(sqlite3.Blob) if not n.startswith("_") and callable(getattr(sqlite3.Blob, n))]
    names += sorted(backends._BLOB_DUNDERS)
    for name in names:
        assert name in vars(backends._ThreadCheckedBlob), f"Blob.{name} is not wrapped"
