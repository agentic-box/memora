"""Shadow-local mode, piece (a): the connection hook, the applier and the
dirty rule (docs/local-primary-implementation.md §2.9). Offline: "D1" is a
SQLite file with memora's D1 schema behind a patched D1Connection._send, and
the applier reads it through the REAL D1SelectOnlyConnection (its HTTP post
patched), exactly as tests/l3_fakes.py does for the replicator."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from memora import backends, schema, shadow, storage
from memora.backends import D1Connection, LocalSQLiteBackend
from memora.shadow import ShadowingD1Connection, ShadowItem, init_shadow_file
from memora.write_gate import StoreReadOnlyError
from tests.l3_fakes import FakeReplica

REPO = Path(__file__).resolve().parent.parent
URI = "d1" + "://acct/replica-db"


class D1Send:
    """The app's D1 requests, answered from the fake D1 file (D1-shaped
    results with meta.changes and last_row_id)."""

    def __init__(self, replica: FakeReplica):
        self.replica = replica
        self.fail_when = None     # (sql, params) -> exception or None
        self.calls = []

    def __call__(self, conn, sql, params=None):
        self.calls.append(sql)
        if self.fail_when is not None:
            exc = self.fail_when(sql, params)
            if exc is not None:
                raise exc
        db = self.replica._db()
        try:
            cur = db.execute(sql, tuple(params or ()))
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            db.commit()
            meta = {"changes": max(cur.rowcount, 0), "last_row_id": cur.lastrowid or 0, "served_by_primary": True}
        finally:
            db.close()
        return {"success": True, "result": [{"results": rows, "meta": meta}]}


def _seed_shadow(d1_path: Path, shadow_path: Path) -> None:
    """What `local_primary.py seed` + `shadow-init` produce: a copy of D1
    with the local schema, the sync outbox and a clean shadow_state."""
    shutil.copy(d1_path, shadow_path)
    b = LocalSQLiteBackend(shadow_path)
    conn = b.connect()
    try:
        schema.ensure_schema(conn)
        conn.commit()
        schema.install_sync(conn, URI, 0)
    finally:
        conn.close()
    init_shadow_file(str(shadow_path))


@pytest.fixture
def world(tmp_path, monkeypatch):
    replica = FakeReplica(tmp_path / "d1.db")
    send = D1Send(replica)
    monkeypatch.setattr(backends.D1Connection, "_send", lambda self, sql, params=None: send(self, sql, params))
    shadow_path = tmp_path / "shadow" / "s1.db"
    shadow_path.parent.mkdir()
    _seed_shadow(replica.path, shadow_path)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"s1": URI}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "s1")
    monkeypatch.setenv("MEMORA_SHADOW_LOCAL", json.dumps({"s1": str(shadow_path)}))
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read-token")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token")
    sleeps = []
    shadow._reset_for_tests()
    started = shadow.start_shadow_appliers(reader_factory=lambda a, d, t: replica.reader(),
                                           replication_mode=lambda *a: "disabled")
    assert started == {"s1": {"started": True}}
    app = shadow.applier_for("s1")
    app.sleep = sleeps.append
    yield SimpleNamespace(replica=replica, send=send, shadow_path=shadow_path, app=app, sleeps=sleeps,
                          backend=storage.backend_for("s1"), tmp=tmp_path)
    shadow._reset_for_tests()


def _drain(app, timeout=10):
    """Every item replayed AND the quiescent copy-back done (or the applier
    gave up on a key and marked the shadow dirty)."""
    deadline = time.time() + timeout
    while (app.queue.unfinished_tasks or app._pending_keys) and app.alive and time.time() < deadline:
        time.sleep(0.01)
    assert not app.queue.unfinished_tasks, "the applier did not drain"


def _rows(path_or_replica, table):
    if isinstance(path_or_replica, FakeReplica):
        rows = path_or_replica.rows(table)
    else:
        db = sqlite3.connect(path_or_replica)
        db.row_factory = sqlite3.Row
        pk = ", ".join(schema.SYNC_TABLES[table])
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {pk}")]
        db.close()
    if table == "memories_meta":
        rows = [r for r in rows if r["key"] not in schema.SYNC_META_EXCLUDED]
    return rows


def _state(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return dict(db.execute("SELECT * FROM shadow_state").fetchone())
    finally:
        db.close()


def _assert_mirrored(w):
    for t in schema.SYNC_TABLES:
        assert _rows(w.shadow_path, t) == _rows(w.replica, t), t


# ------------------------------------------------------------------ mirroring

def test_mirror_every_replicated_table(world):
    w = world
    conn = w.backend.connect()
    assert type(conn) is ShadowingD1Connection
    cur = conn.execute("INSERT INTO memories (content, tags, metadata) VALUES (?, ?, ?)", ("one", '["a"]', "{}"))
    mid = cur.lastrowid
    conn.execute("UPDATE memories SET content = ?, tags = ? WHERE id = ?", ("one!", '["b"]', mid))
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?)", (mid, "[[\"0\", 1.0]]"))
    conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (mid,))
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?)", (mid, "[[\"1\", 0.5]]"))
    conn.execute("INSERT INTO memories_actions (memory_id, action, summary) VALUES (?, ?, ?)", (mid, "create", "s"))
    conn.execute("INSERT INTO memories_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 ("embedding_model", "m1"))
    conn.executemany("INSERT INTO memories (content) VALUES (?)", [("two",), ("three",)])
    conn.execute("DELETE FROM memories WHERE content = ?", ("two",))
    conn.close()
    _drain(w.app)
    _assert_mirrored(w)
    st = _state(w.shadow_path)
    assert st["dirty"] == 0 and w.app.dirty_reason is None


def test_reads_are_not_mirrored(world):
    conn = world.backend.connect()
    conn.execute("SELECT * FROM memories").fetchall()
    conn.close()
    assert world.app.queue.qsize() == 0 and world.app.dirty_reason is None


def test_a_different_local_id_is_corrected_by_copy_back(world):
    """D1 assigns id 100 (its sequence is higher); the local replay assigns
    another id; copy-back deletes the local one and writes D1's row."""
    db = world.replica._db()
    db.execute("INSERT INTO memories (id, content) VALUES (99, 'x')")
    db.execute("DELETE FROM memories WHERE id = 99")
    db.commit()
    db.close()
    conn = world.backend.connect()
    assert conn.execute("INSERT INTO memories (content) VALUES (?)", ("late",)).lastrowid == 100
    conn.close()
    _drain(world.app)
    _assert_mirrored(world)
    assert world.app.dirty_reason is None


def test_a_copy_back_interrupted_by_a_new_mutation_is_redone(world, monkeypatch):
    """The reads are taken only at a quiet point; a mutation that starts
    while they run voids them, and they are taken again later -- without a
    local equalisation from the voided reads."""
    from memora import replicator

    real_build = replicator._build_statements
    built = []
    monkeypatch.setattr(replicator, "_build_statements", lambda *a, **kw: (built.append(a[:2]), real_build(*a, **kw))[1])
    real = world.app.reader.execute
    calls = []

    def reader(sql, params=None):
        calls.append(sql)
        if len(calls) == 1:  # a write starts on another connection meanwhile
            world.app.begin_mutation()
            world.app.end_mutation()
        return real(sql, params)
    world.app.reader.execute = reader
    _write(world)
    _drain(world.app)
    assert len(calls) >= 2, "the first reads were discarded and taken again"
    assert len(built) == len({(t, tuple(pk)) for t, pk in built}), "no equalisation from the voided reads"
    assert world.app.dirty_reason is None
    _assert_mirrored(world)


def test_embedding_delete_insert_pairs_replay_cleanly(world):
    """The app's own DELETE+INSERT of an embedding, repeated quickly: the
    replays never meet a row copy-back installed from a later state."""
    conn = world.backend.connect()
    mid = conn.execute("INSERT INTO memories (content) VALUES (?)", ("e",)).lastrowid
    for i in range(5):
        conn.execute("DELETE FROM memories_embeddings WHERE memory_id = ?", (mid,))
        conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?)",
                     (mid, json.dumps([[str(i), 1.0]])))
    conn.close()
    _drain(world.app)
    assert world.app.dirty_reason is None
    _assert_mirrored(world)


def test_non_replicated_targets_are_ignored(world):
    db = world.replica._db()
    db.execute("CREATE TABLE side (a)")
    db.commit()
    db.close()
    conn = world.backend.connect()
    conn.execute("INSERT INTO side (a) VALUES (?)", (1,))
    conn.close()
    assert world.app.queue.qsize() == 0 and world.app.dirty_reason is None


def test_the_log_mode_replicator_sees_the_mirrored_writes(world, monkeypatch):
    """§2.9 + L3: the shadow's outbox feeds a LOG-mode replicator."""
    from memora import replicator

    monkeypatch.setenv("MEMORA_REPLICATION", "log")
    conn = world.backend.connect()
    mid = conn.execute("INSERT INTO memories (content) VALUES (?)", ("logged",)).lastrowid
    conn.close()
    _drain(world.app)
    out = replicator.start_replicators(start=False)
    try:
        assert out["s1"] == {"mode": "log", "shadow": True}
        rep = replicator._REPLICATORS["s1"]
        rep._open()
        assert rep.run_once() == "logged"
        logged = {(r["tbl"], tuple(r["pk"])) for r in replicator.iter_log("s1")}
        assert ("memories", (mid,)) in logged
        assert all(not r["sql"].upper().startswith("UPDATE") or "ON CONFLICT" in r["sql"]
                   for r in replicator.iter_log("s1"))
    finally:
        replicator.stop_replicators()


# ------------------------------------------------------------------ app-visible identity

class Canned:
    """A _send that returns fixed D1 answers (or raises), for comparing the
    plain and the shadowed connection on identical responses."""

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, conn, sql, params=None):
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def _cursor_view(cur):
    return (cur.lastrowid, cur.rowcount, [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()])


def _ans(rows=(), **meta):
    return {"success": True, "result": [{"results": list(rows), "meta": meta}]}


@pytest.mark.parametrize("label,call,answers", [
    ("absent meta.changes", lambda c: c.execute("INSERT INTO memories (content) VALUES (?)", ("x",)),
     [_ans(last_row_id=7)]),
    ("empty params", lambda c: c.execute("UPDATE memories SET content = 'y' WHERE id = 1"),
     [_ans(changes=1)]),
    ("select rows", lambda c: c.execute("SELECT id FROM memories"), [_ans([{"id": 1}, {"id": 2}])]),
    ("executemany", lambda c: c.executemany("INSERT INTO memories (content) VALUES (?)", [("a",), ("b",)]),
     [_ans(changes=1, last_row_id=3), _ans(changes=1, last_row_id=4)]),
    ("executescript", lambda c: c.executescript("DELETE FROM memories WHERE id = 1; DELETE FROM memories WHERE id = 2"),
     [_ans(changes=1), _ans()]),
])
def test_the_app_sees_exactly_the_plain_result(world, monkeypatch, label, call, answers):
    views = []
    for cls in (D1Connection, ShadowingD1Connection):
        monkeypatch.setattr(backends.D1Connection, "_send", Canned([dict(a) for a in answers]))
        conn = cls("acct", "replica-db", "tok")
        conn._backend = world.backend
        views.append(_cursor_view(call(conn)))
    assert views[0] == views[1], label


@pytest.mark.parametrize("label,call,answers,fails", [
    ("unknown outcome", lambda c: c.execute("INSERT INTO memories (content) VALUES (?)", ("x",)),
     [TimeoutError("read timed out")], "row 1/2"),
    ("partial then fail", lambda c: c.executemany("INSERT INTO memories (content) VALUES (?)", [("a",), ("b",)]),
     [_ans(changes=1, last_row_id=3), RuntimeError("D1 API error (500)")], "row 1/2"),
])
def test_the_same_exception_object_reaches_the_app_and_the_shadow_goes_dirty(world, monkeypatch, label, call,
                                                                               answers, fails):
    exc = next(a for a in answers if isinstance(a, BaseException))
    monkeypatch.setattr(backends.D1Connection, "_send", Canned(answers))
    conn = world.backend.connect()
    with pytest.raises(type(exc)) as got:
        call(conn)
    assert got.value is exc, "the parent's exception object, unchanged"
    assert world.app.dirty_reason.startswith(fails)
    assert _state(world.shadow_path)["dirty"] == 1


def test_execute_batch_is_refused(world):
    with pytest.raises(StoreReadOnlyError):
        world.backend.connect().execute_batch([("SELECT 1", ())])


def test_a_write_refused_by_the_freeze_is_not_dirty(world):
    """Nothing reached D1: the gate refused before the journal and the send."""
    gate = world.backend.write_gate()
    gate.freeze(timeout_s=1)
    try:
        with pytest.raises(StoreReadOnlyError):
            world.backend.connect().execute("INSERT INTO memories (content) VALUES (?)", ("x",))
    finally:
        gate.thaw()
    assert world.app.dirty_reason is None


# ------------------------------------------------------------------ the dirty table, one test per row

def _write(w, sql="INSERT INTO memories (content) VALUES (?)", params=("x",)):
    conn = w.backend.connect()
    try:
        return conn.execute(sql, params)
    finally:
        conn.close()


def test_row_1_a_failed_mutating_request(world):
    world.send.fail_when = lambda sql, p: RuntimeError("D1 API error (503)") if sql.startswith("INSERT") else None
    with pytest.raises(RuntimeError):
        _write(world)
    assert world.app.dirty_reason.startswith("row 1/2")


def test_row_2_a_failed_element_after_earlier_ones(world):
    seen = []
    world.send.fail_when = lambda sql, p: (seen.append(1), RuntimeError("boom") if len(seen) == 2 else None)[1]
    conn = world.backend.connect()
    with pytest.raises(RuntimeError):
        conn.executemany("INSERT INTO memories (content) VALUES (?)", [("a",), ("b",)])
    _drain(world.app)
    assert world.app.dirty_reason.startswith("row 1/2")
    assert [r["content"] for r in _rows(world.shadow_path, "memories")] == ["a"], "the first element was enqueued"


def test_row_3_the_applier_is_not_running(world):
    assert world.app.stop()
    _write(world)
    assert world.app.dirty_reason.startswith("row 3") and "not running" in world.app.dirty_reason


def test_row_3_enqueue_fails(world, monkeypatch):
    def boom(item):
        raise RuntimeError("queue broke")
    monkeypatch.setattr(world.app.queue, "put_nowait", boom)
    cur = _write(world)
    assert cur.lastrowid, "the app still gets D1's answer"
    assert world.app.dirty_reason.startswith("row 3")


def test_row_4_the_local_replay_raises_and_rolls_back(world):
    db = sqlite3.connect(world.shadow_path)
    db.execute("CREATE TRIGGER no_insert BEFORE INSERT ON memories BEGIN SELECT RAISE(ABORT, 'refused'); END")
    db.commit()
    db.close()
    _write(world)
    _drain(world.app)
    assert world.app.dirty_reason.startswith("row 4")
    assert _rows(world.shadow_path, "memories") == []


@pytest.mark.parametrize("fault,needle", [
    ("read fails", "read failed"),
    ("replica served", "served by a replica"),
    ("written value stale", "written values"),
])
def test_row_5_copy_back_fails_after_the_retries(world, fault, needle):
    real = world.app.reader.execute
    calls = []

    def reader(sql, params=None):
        calls.append(sql)
        if fault == "read fails":
            raise RuntimeError("D1 API error (500)")
        rows, meta = real(sql, params)
        if fault == "replica served":
            return rows, {**meta, "served_by_primary": False}
        return [{**r, "content": "stale"} for r in rows], meta
    world.app.reader.execute = reader
    _write(world, "UPDATE memories SET content = ? WHERE id = ?", ("new", 1)) if False else None
    conn = world.backend.connect()
    mid = conn.execute("INSERT INTO memories (content) VALUES (?)", ("fresh",)).lastrowid
    conn.close()
    _drain(world.app)
    assert world.app.dirty_reason.startswith("row 5") and needle in world.app.dirty_reason, world.app.dirty_reason
    assert len(calls) == 5 and world.sleeps == [0.2] * 4, "5 attempts, 200 ms apart"
    assert mid


def test_row_5_a_stale_replica_read_is_retried_then_accepted(world):
    """Replica-served twice, then the primary: clean, after 2 sleeps."""
    real = world.app.reader.execute
    n = []

    def reader(sql, params=None):
        rows, meta = real(sql, params)
        n.append(1)
        return rows, {**meta, "served_by_primary": len(n) > 2}
    world.app.reader.execute = reader
    _write(world)
    _drain(world.app)
    assert world.app.dirty_reason is None and world.sleeps == [0.2, 0.2]
    _assert_mirrored(world)


def test_a_mutation_starting_during_the_local_equalisation_rolls_it_back_and_retries(world, monkeypatch):
    """Review 7701 P2: the generation is rechecked, under the applier lock,
    after the local writes and before the commit; a shadowed mutation that
    started meanwhile rolls the equalisation back and the keys stay pending."""
    from memora import replicator

    real = replicator._build_statements
    calls = []

    def build(*a, **kw):
        calls.append(a[0])
        if len(calls) == 1:
            world.app.begin_mutation()
            world.app.end_mutation()
        return real(*a, **kw)
    monkeypatch.setattr(replicator, "_build_statements", build)
    seen_pending = []
    real_copy = world.app._copy_back_if_quiet

    def copy(conn):
        n = len(calls)
        real_copy(conn)
        if n == 0 and calls:
            seen_pending.append(len(world.app._pending_keys))
    monkeypatch.setattr(world.app, "_copy_back_if_quiet", copy)
    _write(world)
    _drain(world.app)
    assert seen_pending and seen_pending[0] > 0, "the first equalisation was discarded, its keys kept"
    assert len(calls) > len(set(calls)), "the equalisation ran again"
    assert world.app.dirty_reason is None
    _assert_mirrored(world)


def test_row_6_the_local_row_differs_after_copy_back(world):
    db = sqlite3.connect(world.shadow_path)
    db.execute("CREATE TRIGGER skew AFTER UPDATE ON memories BEGIN "
               "UPDATE memories SET tags = 'skewed' WHERE id = NEW.id AND tags IS NOT 'skewed'; END")
    db.commit()
    db.close()
    _write(world)
    _drain(world.app)
    assert world.app.dirty_reason.startswith("row 6")


def test_row_7_an_unknown_statement(world, monkeypatch):
    """A multi-statement body classifies as unknown; D1 answered it."""
    monkeypatch.setattr(backends.D1Connection, "_send", Canned([_ans(changes=1)]))
    _write(world, "UPDATE memories SET tags = NULL WHERE id = 1; DELETE FROM memories WHERE id = 2", ())
    assert world.app.dirty_reason.startswith("row 7:")


def test_row_7_a_mutation_without_a_target(world):
    from memora.sql_classify import Classified, MUTATION
    conn = world.backend.connect()
    conn._after_mutation(Classified(MUTATION, "INSERT", None), "INSERT INTO", (), _ans())
    assert world.app.dirty_reason.startswith("row 7:") and "target" in world.app.dirty_reason


def test_row_7b_ddl(world):
    _write(world, "CREATE TABLE extra (a INTEGER)", ())
    assert world.app.dirty_reason.startswith("row 7b")


def test_row_8_the_applier_thread_dies(world, monkeypatch):
    def die(conn, item):
        raise SystemExit("killed")
    monkeypatch.setattr(world.app, "_apply", die)
    _write(world)
    deadline = time.time() + 5
    while world.app.alive and time.time() < deadline:
        time.sleep(0.01)
    assert not world.app.alive and world.app.dirty_reason.startswith("row 8")
    assert world.app.status()["applier_alive"] is False
    _write(world)  # a later write: row 3 is already covered by the first reason
    assert world.app.dirty_reason.startswith("row 8")


def test_row_9_restart_after_a_clean_stop_is_clean(world):
    assert world.app.stop()
    assert _state(world.shadow_path)["clean_shutdown"] == 1
    again = shadow.ShadowApplier("s1", str(world.shadow_path), world.replica.reader())
    again.start()
    try:
        assert again.dirty_reason is None and _state(world.shadow_path)["clean_shutdown"] == 0
    finally:
        again.stop()


SUBPROCESS = r'''
import json, os, sys, threading
sys.path.insert(0, sys.argv[1])
from memora import shadow
from memora.shadow import ShadowItem
from tests.l3_fakes import FakeReplica
replica = FakeReplica(sys.argv[3])
app = shadow.ShadowApplier("s1", sys.argv[2], replica.reader())
app.start()
hold = threading.Event()
app._apply = lambda conn, item: hold.wait(60)   # the item never finishes
app.enqueue(ShadowItem("INSERT INTO memories (content) VALUES (?)", ("x",), {}, "memories", "INSERT"))
print("queued", flush=True)
os._exit(0)   # the process dies with the item still in memory
'''


def test_row_9_a_process_exit_with_a_queued_item_marks_dirty_on_restart(world):
    assert world.app.stop()
    r = subprocess.run([sys.executable, "-c", SUBPROCESS, str(REPO), str(world.shadow_path), str(world.replica.path)],
                       capture_output=True, text=True, timeout=60, cwd=str(REPO))
    assert r.returncode == 0 and "queued" in r.stdout, r.stderr
    assert _state(world.shadow_path)["clean_shutdown"] == 0
    again = shadow.ShadowApplier("s1", str(world.shadow_path), world.replica.reader())
    again.start()
    try:
        assert again.dirty_reason.startswith("row 9")
        assert _state(world.shadow_path)["dirty"] == 1
    finally:
        again.stop()


def test_the_first_reason_is_kept(world):
    world.app.mark_dirty("first")
    world.app.mark_dirty("second")
    assert _state(world.shadow_path)["dirty_reason"] == "first"


def test_the_first_reason_survives_another_instance(world):
    """A reason already persisted (by an earlier run or another process) is
    kept when a fresh applier -- one that never read the state -- marks the
    shadow dirty again."""
    world.app.mark_dirty("persisted first")
    fresh = shadow.ShadowApplier("s1", str(world.shadow_path))
    fresh.mark_dirty("second")
    assert _state(world.shadow_path)["dirty_reason"] == "persisted first"


def test_dirty_restarts_the_clean_night_count(world):
    db = sqlite3.connect(world.shadow_path)
    db.execute("UPDATE shadow_state SET clean_nights = 5")
    db.commit()
    db.close()
    world.app.mark_dirty("x")
    assert _state(world.shadow_path)["clean_nights"] == 0


# ------------------------------------------------------------------ no network under store_write

def test_no_network_while_store_write_is_held(world):
    real = world.app.reader.execute
    depths = []

    def reader(sql, params=None):
        depths.append(getattr(backends._STORE_WRITE_TLS, "depth", 0))
        return real(sql, params)
    world.app.reader.execute = reader
    conn = world.backend.connect()
    for i in range(3):
        conn.execute("INSERT INTO memories (content) VALUES (?)", (f"n{i}",))
    conn.close()
    _drain(world.app)
    assert depths and all(d == 0 for d in depths), depths


# ------------------------------------------------------------------ startup

@pytest.mark.parametrize("env,mode,needle", [
    ({"MEMORA_D1_READ_TOKEN": ""}, "disabled", "MEMORA_D1_READ_TOKEN"),
    ({}, "auto", "read replication"),
])
def test_the_applier_refuses_to_start(tmp_path, monkeypatch, env, mode, needle):
    replica = FakeReplica(tmp_path / "d1.db")
    send = D1Send(replica)
    monkeypatch.setattr(backends.D1Connection, "_send", lambda self, sql, params=None: send(self, sql, params))
    sp = tmp_path / "s1.db"
    _seed_shadow(replica.path, sp)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"s1": URI}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "s1")
    monkeypatch.setenv("MEMORA_SHADOW_LOCAL", json.dumps({"s1": str(sp)}))
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read-token")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    shadow._reset_for_tests()
    try:
        out = shadow.start_shadow_appliers(reader_factory=lambda a, d, t: replica.reader(),
                                           replication_mode=lambda *a: mode)
        assert out["s1"]["started"] is False and needle in out["s1"]["refused"]
        conn = storage.backend_for("s1").connect()
        assert conn.execute("INSERT INTO memories (content) VALUES (?)", ("x",)).lastrowid, "D1 is unaffected"
        conn.close()
        assert shadow.applier_for("s1").dirty_reason.startswith("row 3")
    finally:
        shadow._reset_for_tests()


def test_a_malformed_shadow_config_is_fatal(monkeypatch):
    monkeypatch.setenv("MEMORA_SHADOW_LOCAL", "{not json")
    with pytest.raises(shadow.ShadowConfigError):
        shadow.shadow_config()
    # ...while the request path keeps plain D1 connections working
    assert shadow.shadow_connection_class("s1") is D1Connection


def test_init_shadow_file_refuses_an_unseeded_file(tmp_path):
    p = tmp_path / "plain.db"
    b = LocalSQLiteBackend(p)
    c = b.connect()
    schema.ensure_schema(c)
    c.close()
    with pytest.raises(shadow.ShadowConfigError, match="sync outbox"):
        init_shadow_file(str(p))
    with pytest.raises(shadow.ShadowConfigError, match="does not exist"):
        init_shadow_file(str(tmp_path / "missing.db"))


def test_shadow_init_cli(tmp_path):
    replica = FakeReplica(tmp_path / "d1.db")
    p = tmp_path / "s.db"
    shutil.copy(replica.path, p)
    b = LocalSQLiteBackend(p)
    c = b.connect()
    schema.ensure_schema(c)
    c.commit()
    schema.install_sync(c, URI, 0)
    c.close()
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "local_primary.py"), "shadow-init", "--shadow", str(p)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    st = json.loads(r.stdout)["shadow_state"]
    assert st["clean_shutdown"] == 1 and st["dirty"] == 0
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "local_primary.py"), "shadow-init", "--shadow",
                        str(tmp_path / "none.db")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and json.loads(r.stdout)["ok"] is False
