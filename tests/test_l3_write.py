"""L3 piece (b): write mode -- batches, ack/prune, epoch preflight and
postcheck (H2), the durable in-flight marker and read-back reconciliation
(H3), foreign-writer and delete-guard halts that persist, tokens, metrics.
docs/local-primary-implementation.md §2.2-§2.7, §6.1. Offline only."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memora import replicator as R
from memora import schema, write_gate

from tests.l3_fakes import URI, FakeReplica, local_rows, local_store, sync_state

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path):
    replica = FakeReplica(tmp_path / "replica.db")
    local = local_store(tmp_path / "local.db", epoch=replica.epoch())
    return local, replica


def _rep(local, replica, broadcasts=None, **kw):
    rep = R.StoreReplicator("s1", local, URI, mode="write", writer_factory=replica.writer,
                            reader_factory=replica.reader,
                            broadcast=(lambda: broadcasts.append(1)) if broadcasts is not None else (lambda: None), **kw)
    rep._open()
    return rep


def _drain(rep, limit=50):
    outcomes = []
    for _ in range(limit):
        out = rep.run_once()
        outcomes.append(out)
        if out in ("idle", "halted"):
            return outcomes
    raise AssertionError(f"did not settle: {outcomes}")


def _write_everything(local):
    conn = local.connect()
    conn.execute("INSERT INTO memories (id, content, tags, created_at, importance) VALUES (1, 'a', '[]', 't', 1.0)")
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (2, 'b', '[]', 't')")
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, representation, dimension, encoding_source, writer_token) "
                 "VALUES (1, '{\"a\":1}', 'rep-1', 3, 'src', 'tok-1')")
    conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (1, '[]')")
    conn.execute("INSERT INTO tombstones (content_hash, memory_id, reason) VALUES ('h', 2, 'r')")
    conn.execute("INSERT INTO tombstone_components (memory_id, content_hash) VALUES (2, 'h')")
    conn.execute("INSERT INTO memories_actions (memory_id, action, summary) VALUES (1, 'add', 's')")
    conn.execute("INSERT INTO memories_meta (key, value) VALUES ('other', 'v')")
    conn.execute("UPDATE memories SET content = 'a2' WHERE id = 1")
    conn.execute("UPDATE memories_embeddings SET embedding = '{\"a\":2}', writer_token = 'tok-2' WHERE memory_id = 1")
    conn.commit()
    conn.close()


def _assert_converged(local, replica):
    for table in schema.SYNC_TABLES:
        remote = replica.rows(table)
        if table == "memories_meta":
            remote = [r for r in remote if r["key"] not in schema.SYNC_META_EXCLUDED]
        assert local_rows(local, table) == remote, table


# ------------------------------------------------------------------ happy path

def test_write_mode_converges_every_table_and_acks(env):
    local, replica = env
    _write_everything(local)
    broadcasts = []
    rep = _rep(local, replica, broadcasts)
    assert _drain(rep)[-1] == "idle"
    _assert_converged(local, replica)
    # D1's own embedding triggers did not null the replicated representation
    assert replica.rows("memories_embeddings")[0]["representation"] == "rep-1"
    st = sync_state(local)
    assert st["last_acked_seq"] > 0 and st["inflight_id"] is None and st["last_ack_at"]
    assert st["d1_epoch_expected"] == replica.epoch()  # the postcheck's value
    assert broadcasts == [1]  # one batch, one ack, one /broadcast after it
    s = rep.status()
    assert s["status"] == "running" and s["lag_rows"] == 0 and s["head_seq"] == st["last_acked_seq"]


def test_replicator_sends_no_ddl_and_only_allow_listed_statements(env):
    local, replica = env
    _write_everything(local)
    _drain(_rep(local, replica, batch_rows=10_000))
    assert replica.statements
    for sql in replica.statements:
        R._check_statement(sql)
        head = sql.split()[0].upper()
        assert head in ("INSERT", "DELETE", "SELECT") and "OR IGNORE" not in sql
    for sql in replica.reads:
        assert sql.startswith("SELECT ")


def test_a_second_drain_after_more_writes_converges(env):
    local, replica = env
    _write_everything(local)
    rep = _rep(local, replica)
    _drain(rep)
    conn = local.connect()
    conn.execute("DELETE FROM tombstones WHERE content_hash = 'h' AND memory_id = 2")
    conn.execute("DELETE FROM memories_embeddings WHERE memory_id = 1")
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (3, 'c', '[]', 't')")
    conn.commit()
    conn.close()
    # Deletes from tables under 100 rows always trip the P3 guard: an
    # operator allows the exact attempt, once.
    assert _drain(rep, limit=10)[-1] == "halted"
    reason = sync_state(local)["halted_reason"]
    assert reason.startswith("delete_guard:")
    conn = local.connect_replicator()
    R.resume(conn, allow_deletes=reason.rsplit("attempt=", 1)[1])
    conn.close()
    assert _drain(rep, limit=10)[-1] == "idle"
    _assert_converged(local, replica)
    for sql in replica.statements:
        R._check_statement(sql)  # the deletes are per-key, full primary key


def test_batch_body_rejected_falls_back_to_single_statements(env):
    local, replica = env
    replica.reject_batch_400 = True
    _write_everything(local)
    _drain(_rep(local, replica))
    _assert_converged(local, replica)


# ------------------------------------------------------------------ ack rules and H3

def test_ack_only_when_every_result_succeeds(env):
    local, replica = env
    _write_everything(local)
    replica.result_override = lambda out: [dict(out[0], success=False)] + out[1:]
    rep = _rep(local, replica)
    with pytest.raises(RuntimeError, match="every statement"):
        rep.run_once()
    st = sync_state(local)
    assert st["last_acked_seq"] == 0 and st["inflight_id"]  # no ack, marker kept


def test_partial_batch_is_reconciled_and_resent_whole(env):
    local, replica = env
    _write_everything(local)
    replica.apply_then_raise = (3, TimeoutError("reset mid-batch"))
    rep = _rep(local, replica)
    with pytest.raises(TimeoutError):
        rep.run_once()
    assert sync_state(local)["inflight_id"]
    replica.apply_then_raise = None
    assert rep.run_once() == "reconciled-resend"  # read-back shows a mismatch
    assert _drain(rep)[-1] == "idle"
    _assert_converged(local, replica)
    assert sync_state(local)["halted_reason"] is None  # relaxed preflight: our own partial writes moved the epoch


def test_applied_but_response_lost_is_acked_after_read_back(env):
    local, replica = env
    _write_everything(local)
    replica.apply_then_raise = (10_000, TimeoutError("response lost"))
    rep = _rep(local, replica)
    with pytest.raises(TimeoutError):
        rep.run_once()
    replica.apply_then_raise = None
    assert rep.run_once() == "reconciled-acked"
    st = sync_state(local)
    assert st["epoch_unverified_batches"] == 1 and st["inflight_id"] is None
    assert st["d1_epoch_expected"] == replica.epoch()
    _assert_converged(local, replica)


def test_replicator_killed_between_send_and_ack(env, tmp_path):
    local, replica = env
    _write_everything(local)
    child = f"""
import os, sys
sys.path.insert(0, {str(REPO)!r})
os.environ['MEMORA_DATA_DIR'] = {str(write_gate.data_dir())!r}
os.environ['MEMORA_TEST_KILL_AFTER_SEND'] = '1'
from memora import replicator as R
from memora.backends import LocalSQLiteBackend
from tests.l3_fakes import FakeReplica, URI
rep = FakeReplica(__import__('pathlib').Path({str(replica.path)!r}))
r = R.StoreReplicator('s1', LocalSQLiteBackend({str(local.db_path)!r}), URI, mode='write',
                      writer_factory=rep.writer, reader_factory=rep.reader, broadcast=lambda: None)
r._open(); r.run_once()
"""
    r = subprocess.run([sys.executable, "-c", child], cwd=str(REPO), timeout=60, capture_output=True, text=True)
    assert r.returncode == 137, r.stderr
    st = sync_state(local)
    assert st["inflight_id"] and st["last_acked_seq"] == 0  # D1 has it, the ack never happened
    rep = _rep(local, replica)
    assert rep.run_once() == "reconciled-acked"
    assert _drain(rep)[-1] == "idle"
    _assert_converged(local, replica)


# ------------------------------------------------------------------ H2 foreign writer

def test_foreign_writer_halts_persists_and_resumes(env):
    local, replica = env
    _write_everything(local)
    rep = _rep(local, replica)
    _drain(rep)
    db = sqlite3.connect(replica.path)  # someone else writes D1
    db.execute("INSERT INTO memories (id, content, created_at) VALUES (99, 'foreign', 't')")
    db.commit()
    db.close()
    conn = local.connect()
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (5, 'mine', '[]', 't')")
    conn.commit()
    conn.close()
    before = len(replica.statements)
    assert rep.run_once() == "halted"
    assert len(replica.statements) == before  # the preflight stopped it before any mutation
    reason = sync_state(local)["halted_reason"]
    assert reason.startswith("foreign_writer: expected")
    rep2 = _rep(local, replica)  # a restart
    assert rep2.run_once() == "halted" and rep2.status()["status"] == "halted"
    conn = local.connect_replicator()
    with pytest.raises(ValueError):
        R.resume(conn)
    R.resume(conn, accept_d1_epoch=replica.epoch())
    conn.close()
    # the halted attempt's marker is reconciled: D1 lacks row 5, so resend
    assert rep2.run_once() == "reconciled-resend"
    assert _drain(rep2)[-1] == "idle"
    assert [r["id"] for r in replica.rows("memories") if r["id"] == 5] == [5]


def test_write_mode_needs_an_expected_epoch(env):
    local, replica = env
    conn = local.connect_replicator()
    conn.execute("UPDATE sync_state SET d1_epoch_expected = NULL")
    conn.commit()
    conn.close()
    _write_everything(local)
    assert _rep(local, replica).run_once() == "halted"
    assert "d1_epoch_expected" in sync_state(local)["halted_reason"]
    assert replica.statements == []


# ------------------------------------------------------------------ P3 in write mode

def test_delete_guard_halts_write_mode_before_sending(env):
    local, replica = env
    conn = local.connect()
    conn.executemany("INSERT INTO memories (id, content, created_at) VALUES (?, 'x', 't')", [(i,) for i in range(1, 101)])
    conn.commit()
    conn.close()
    rep = _rep(local, replica, batch_rows=10_000)
    _drain(rep)
    conn = local.connect()
    conn.execute("DELETE FROM memories WHERE id <= 2")
    conn.commit()
    conn.close()
    before = len(replica.statements)
    assert rep.run_once() == "halted"
    assert len(replica.statements) == before
    assert sync_state(local)["halted_reason"].startswith("delete_guard: memories 2/100")


# ------------------------------------------------------------------ prune

def test_outbox_kept_until_compare_consumed_and_24h(env):
    local, replica = env
    _write_everything(local)
    rep = _rep(local, replica)
    _drain(rep)
    conn = local.connect_replicator()
    n = conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]
    assert n > 0  # acked, but no compare has consumed them
    conn.execute("UPDATE sync_state SET compare_consumed_seq = last_acked_seq")
    conn.commit()
    conn.close()
    conn = local.connect()
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (7, 'n', '[]', 't')")
    conn.commit()
    conn.close()
    _drain(rep)
    conn = local.connect_replicator()
    assert conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == n + 1  # younger than 24 h
    conn.execute("UPDATE sync_outbox SET created_at = julianday('now') - 2")
    conn.execute("UPDATE sync_state SET compare_consumed_seq = last_acked_seq")
    conn.commit()
    conn.close()
    conn = local.connect()
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (8, 'n', '[]', 't')")
    conn.commit()
    conn.close()
    _drain(rep)
    conn = local.connect_replicator()
    remaining = conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]
    conn.close()
    assert remaining == 1  # everything up to the consumed seq and older than a day is gone


# ------------------------------------------------------------------ tokens, gate, metrics

def test_write_mode_uses_the_replicator_token_only(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "the-app-edit-token")
    monkeypatch.delenv("MEMORA_D1_REPLICATOR_TOKEN", raising=False)
    with pytest.raises(R.ReplicatorConfigError, match="MEMORA_D1_REPLICATOR_TOKEN"):
        R._writer_for(URI)
    monkeypatch.setenv("MEMORA_D1_REPLICATOR_TOKEN", "replicator-token")
    assert R._writer_for(URI).api_token == "replicator-token"
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read-token")
    assert R._reader_for(URI)._api_token == "read-token"


def test_writer_refuses_statements_outside_the_allow_list(env):
    local, replica = env
    w = replica.writer()
    with pytest.raises(R.ReplicatorStatementError):
        w.execute_batch([("DROP TABLE memories", ())])
    assert replica.statements == []
    assert not hasattr(w, "execute") and not hasattr(w, "executescript")


def test_inflight_marker_counts_as_an_open_intent(env):
    local, replica = env
    _write_everything(local)
    replica.fail_before = TimeoutError("down")
    rep = _rep(local, replica)
    gate = local.write_gate()
    gate.journal_status = lambda: (rep.open_marker(), None)
    with pytest.raises(TimeoutError):
        rep.run_once()
    rep._refresh_metrics()
    gate.freeze(timeout_s=1)
    assert gate.state == "frozen-unsafe"
    replica.fail_before = None
    _drain(rep)
    rep._refresh_metrics()
    assert gate.state == "frozen"
    gate.thaw()


def test_health_reports_the_replication_block(env, monkeypatch):
    from memora import admin

    local, replica = env
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"s1": str(local.db_path)}))
    rep = _rep(local, replica)
    R._REPLICATORS["s1"] = rep
    try:
        _write_everything(local)
        rep._refresh_metrics()
        block = admin.gate_health("s1")["replication"]
        assert block["mode"] == "write" and block["lag_rows"] > 0 and block["oldest_unacked_age_s"] >= 0
        assert set(block) >= {"status", "head_seq", "last_acked_seq", "log_cursor_seq", "lag_rows",
                              "oldest_unacked_age_s", "last_ack_at", "last_error", "halted_reason",
                              "epoch_unverified_batches", "d1_missing_vectors"}
    finally:
        R._REPLICATORS.clear()


def test_a_thread_drains_after_a_commit(env):
    import time

    local, replica = env
    rep = R.StoreReplicator("s1", local, URI, mode="write", writer_factory=replica.writer,
                            reader_factory=replica.reader, broadcast=lambda: None, poll_s=30)
    rep.start()
    try:
        time.sleep(0.2)
        conn = local.connect()
        conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (1, 'woke', '[]', 't')")
        conn.commit()  # sets the commit event; the 30 s poll is not what wakes it
        conn.close()
        for _ in range(100):
            if replica.rows("memories"):
                break
            time.sleep(0.05)
        assert [r["content"] for r in replica.rows("memories")] == ["woke"]
    finally:
        rep.stop()


# ------------------------------------------------------------------ round 2 (review 7599)

def test_freeze_waits_for_a_replicator_send_and_never_certifies_an_empty_marker(env):
    """P1-1: a held execute_batch is in flight on the store's gate; freeze()
    waits for it, and the marker the gate sees is the durable one."""
    import threading
    import time

    local, replica = env
    _write_everything(local)
    gate = local.write_gate()
    box = {}

    def send():  # the replicator's connection belongs to the thread that opened it
        box["rep"] = _rep(local, replica)
        gate.journal_status = lambda: (box["rep"].open_marker(), None)
        box["rep"].run_once()

    replica.hold, replica.arrived = threading.Event(), threading.Event()
    sender = threading.Thread(target=send)
    sender.start()
    assert replica.arrived.wait(5)
    rep = box["rep"]
    assert rep.open_marker(), "the marker is set before the send"
    done = threading.Event()
    freezer = threading.Thread(target=lambda: (gate.freeze(timeout_s=5), done.set()))
    freezer.start()
    time.sleep(0.2)
    assert not done.is_set() and gate.state == "draining"
    replica.hold.set()
    sender.join(5)
    freezer.join(5)
    assert done.is_set() and gate.state == "frozen" and rep.open_marker() == []
    assert sync_state(local)["inflight_id"] is None
    gate.thaw()


def test_a_failed_send_leaves_the_store_frozen_unsafe(env):
    local, replica = env
    _write_everything(local)
    rep = _rep(local, replica)
    gate = local.write_gate()
    gate.journal_status = lambda: (rep.open_marker(), None)
    replica.fail_before = TimeoutError("unknown outcome")
    with pytest.raises(TimeoutError):
        rep.run_once()
    gate.freeze(timeout_s=1)
    assert gate.state == "frozen-unsafe" and rep.open_marker()
    rep2 = _rep(local, replica)  # a new process sees the durable marker at once
    assert rep2.open_marker() == rep.open_marker()
    gate.thaw()


def test_foreign_keys_parent_before_child_reviewers_sequence(env):
    """P1-2: INSERT memories 1, INSERT embedding 1, UPDATE memories 1 --
    coalescing puts the embedding (seq 2) before its parent (seq 3)."""
    local, replica = env
    conn = local.connect()
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (1, 'a', '[]', 't')")
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, writer_token) VALUES (1, '{}', 'w')")
    conn.execute("UPDATE memories SET content = 'a2' WHERE id = 1")
    conn.commit()
    conn.close()
    assert _drain(_rep(local, replica))[-1] == "idle"
    _assert_converged(local, replica)


def test_foreign_keys_across_a_batch_boundary(env):
    """The child's outbox row comes first and the batch holds one row: the
    parent's current row travels with it."""
    local, replica = env
    conn = local.connect()
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, writer_token) VALUES (1, '{}', 'w')")
    conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (1, '[]')")
    conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (1, 'a', '[]', 't')")
    conn.commit()
    conn.close()
    rep = _rep(local, replica, batch_rows=1)
    assert rep.run_once() == "sent"
    assert [r["id"] for r in replica.rows("memories")] == [1]
    assert _drain(rep)[-1] == "idle"
    _assert_converged(local, replica)


def test_foreign_keys_parent_delete_with_children(env):
    local, replica = env
    conn = local.connect()
    for i in range(1, 121):  # enough rows that 1 delete stays under the P3 guard
        conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (?, 'x', '[]', 't')", (i,))
        conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, writer_token) VALUES (?, '{}', 'w')", (i,))
        conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (?, '[]')", (i,))
    conn.commit()
    conn.close()
    rep = _rep(local, replica, batch_rows=10_000)
    _drain(rep)
    conn = local.connect()
    conn.execute("DELETE FROM memories WHERE id = 7")  # the parent's outbox row comes FIRST
    conn.execute("DELETE FROM memories_embeddings WHERE memory_id = 7")
    conn.execute("DELETE FROM memories_crossrefs WHERE memory_id = 7")
    conn.commit()
    conn.close()
    assert _drain(rep)[-1] == "idle"
    _assert_converged(local, replica)
    order = [s.split(" WHERE")[0] for s in replica.statements[-4:-1]]
    assert order[-1] == "DELETE FROM memories"  # the parent is deleted last


def test_a_result_without_success_is_not_acked(env):
    """P1-4: every result must say success: true."""
    local, replica = env
    _write_everything(local)
    replica.result_override = lambda out: [{k: v for k, v in out[0].items() if k != "success"}] + out[1:]
    rep = _rep(local, replica)
    with pytest.raises(RuntimeError, match="every statement"):
        rep.run_once()
    st = sync_state(local)
    assert st["last_acked_seq"] == 0 and st["inflight_id"]
    replica.result_override = None
    assert rep.run_once() == "reconciled-acked"  # the H3 path, not a silent ack


def test_live_d1_guard_refuses_a_database_that_is_not_the_named_throwaway():
    """P1-3: the live module checks the API-reported name before any write."""
    from tests.live_d1_guard import verify_throwaway

    def fetch(name):
        return lambda account, database, token: {"name": name}

    ok, why = verify_throwaway("a", "d", "t", "my-throwaway-db", fetch=fetch("production-memora"))
    assert not ok and "production-memora" in why
    ok, why = verify_throwaway("a", "d", "t", "my-throwaway-db", fetch=fetch("other-throwaway-db"))
    assert not ok
    ok, why = verify_throwaway("a", "d", "t", "prod", fetch=fetch("prod"))
    assert not ok and "throwaway" in why
    ok, _ = verify_throwaway("a", "d", "t", "my-throwaway-db", fetch=fetch("my-throwaway-db"))
    assert ok

    def broken(account, database, token):
        raise OSError("network down")

    ok, why = verify_throwaway("a", "d", "t", "my-throwaway-db", fetch=broken)
    assert not ok and "lookup failed" in why


def test_marker_stays_visible_until_the_ack_commits(env, monkeypatch):
    local, replica = env
    _write_everything(local)
    rep = _rep(local, replica)
    seen = []
    real = rep._ack_locked

    def spy(*a, **k):
        seen.append(list(rep.open_marker()))
        real(*a, **k)
        seen.append(list(rep.open_marker()))

    monkeypatch.setattr(rep, "_ack_locked", spy)
    assert rep.run_once() == "sent"
    assert seen[0] and seen[1], "cleared before the ack committed"
    assert rep.open_marker() == []
