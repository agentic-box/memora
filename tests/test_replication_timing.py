"""REL1 (leader 7762): the replicator's timing from the environment --
MEMORA_REPLICATION_INTERVAL_S, MEMORA_REPLICATION_POLL_S,
MEMORA_REPLICATION_BATCH_ROWS. Offline: a local store and FakeReplica."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from memora import replicator as R

from tests.l3_fakes import URI, FakeReplica, local_store, sync_state

TIMING_VARS = ("MEMORA_REPLICATION_INTERVAL_S", "MEMORA_REPLICATION_POLL_S", "MEMORA_REPLICATION_BATCH_ROWS")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for v in TIMING_VARS:
        monkeypatch.delenv(v, raising=False)
    replica = FakeReplica(tmp_path / "replica.db")
    local = local_store(tmp_path / "local.db", epoch=replica.epoch())
    sends = []  # (monotonic start, statements in the batch)
    post = replica.post_json

    def counting(body):
        if "batch" in body:
            sends.append((time.monotonic(), len(body["batch"]) - 1))  # minus the epoch postcheck
        return post(body)

    replica.post_json = counting
    return local, replica, sends


def _rep(local, replica, **kw):
    return R.StoreReplicator("s1", local, URI, mode="write", writer_factory=replica.writer,
                             reader_factory=replica.reader, broadcast=lambda: None, **kw)


def _insert(local, ids, *, raw=False):
    """Commit memories. raw=True writes through a plain sqlite3 connection:
    the outbox triggers fire, but no commit event wakes the replicator."""
    conn = sqlite3.connect(local.db_path) if raw else local.connect()
    for i in ids:
        conn.execute("INSERT INTO memories (id, content, tags, created_at) VALUES (?, 'x', '[]', 't')", (i,))
        conn.commit()
    conn.close()


def _wait(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _head(local):
    conn = sqlite3.connect(local.db_path)
    try:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_outbox").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- the interval

def test_the_interval_batches_the_commits_made_meanwhile_into_one_send(env):
    local, replica, sends = env
    rep = _rep(local, replica, interval_s=0.8, poll_s=0.05)
    rep.start()
    try:
        _insert(local, [1])
        assert _wait(lambda: len(sends) == 1, 5), "the first commit is sent at once"
        _insert(local, [2, 3, 4])  # three commits within the interval
        assert _wait(lambda: sync_state(local)["last_acked_seq"] == _head(local), 5)
    finally:
        rep.stop()
    assert [n for _, n in sends] == [1, 3], sends  # one send for the three commits
    assert sends[1][0] - sends[0][0] >= 0.8 - 0.01, "the second send starts one interval after the first"


def test_zero_interval_sends_on_commit_as_before(env):
    local, replica, sends = env
    rep = _rep(local, replica, interval_s=0.0, poll_s=30.0)
    rep.start()
    try:
        _insert(local, [1])
        assert _wait(lambda: len(sends) == 1, 5)
        _insert(local, [2])
        assert _wait(lambda: len(sends) == 2, 1.0), "no interval: the next commit is sent at once"
    finally:
        rep.stop()


def test_a_stop_is_not_delayed_by_a_pending_interval(env):
    local, replica, sends = env
    rep = _rep(local, replica, interval_s=30.0, poll_s=0.05)
    rep.start()
    _insert(local, [1])
    assert _wait(lambda: len(sends) == 1, 5)
    _insert(local, [2])
    time.sleep(0.2)  # now inside the 30 s wait
    t0 = time.monotonic()
    rep.stop(timeout=5)
    assert time.monotonic() - t0 < 2 and not rep._thread.is_alive()
    assert len(sends) == 1


# ---------------------------------------------------------------- poll and batch size

def test_the_poll_is_the_fallback_wake(env):
    local, replica, sends = env
    rep = _rep(local, replica, poll_s=0.1)
    rep.start()
    try:
        time.sleep(0.2)
        _insert(local, [1], raw=True)  # no commit event
        assert _wait(lambda: len(sends) == 1, 3), "the poll found the row"
    finally:
        rep.stop()


def test_without_an_event_a_long_poll_does_not_send(env):
    local, replica, sends = env
    rep = _rep(local, replica, poll_s=30.0)
    rep.start()
    try:
        time.sleep(0.2)
        _insert(local, [1], raw=True)
        assert not _wait(lambda: len(sends) == 1, 0.6)
    finally:
        rep.stop()


def test_the_batch_size_caps_one_send(env):
    local, replica, sends = env
    _insert(local, [1, 2, 3, 4, 5])
    rep = _rep(local, replica, batch_rows=2)
    rep._open()
    assert rep.run_once() == "sent"
    assert sends[-1][1] == 2 and sync_state(local)["last_acked_seq"] == 2


# ---------------------------------------------------------------- the environment

@pytest.mark.parametrize("env_vals, want", [
    ({}, {"interval_s": 0.0, "poll_s": 5.0, "batch_rows": 100}),
    ({"MEMORA_REPLICATION_INTERVAL_S": "60"}, {"interval_s": 60.0, "poll_s": 5.0, "batch_rows": 100}),
    ({"MEMORA_REPLICATION_POLL_S": "0.5", "MEMORA_REPLICATION_BATCH_ROWS": "1000"},
     {"interval_s": 0.0, "poll_s": 0.5, "batch_rows": 1000}),
    ({"MEMORA_REPLICATION_INTERVAL_S": " "}, {"interval_s": 0.0, "poll_s": 5.0, "batch_rows": 100}),
])
def test_valid_values(monkeypatch, env_vals, want):
    for v in TIMING_VARS:
        monkeypatch.delenv(v, raising=False)
    for k, v in env_vals.items():
        monkeypatch.setenv(k, v)
    assert R.replication_timing() == want


@pytest.mark.parametrize("var, value", [
    ("MEMORA_REPLICATION_INTERVAL_S", "-1"), ("MEMORA_REPLICATION_INTERVAL_S", "abc"),
    ("MEMORA_REPLICATION_INTERVAL_S", "nan"), ("MEMORA_REPLICATION_INTERVAL_S", "inf"),
    ("MEMORA_REPLICATION_POLL_S", "0"), ("MEMORA_REPLICATION_POLL_S", "-2"), ("MEMORA_REPLICATION_POLL_S", "x"),
    ("MEMORA_REPLICATION_BATCH_ROWS", "0"), ("MEMORA_REPLICATION_BATCH_ROWS", "1001"),
    ("MEMORA_REPLICATION_BATCH_ROWS", "2.5"), ("MEMORA_REPLICATION_BATCH_ROWS", "-3"),
    ("MEMORA_REPLICATION_BATCH_ROWS", "ten"),
])
def test_invalid_values_are_refused_never_defaulted(monkeypatch, var, value):
    for v in TIMING_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(var, value)
    with pytest.raises(R.ReplicatorConfigError, match=var):
        R.replication_timing()


@pytest.fixture()
def configured(env, monkeypatch):
    """start_replicators over the local store, named in MEMORA_REPLICAS."""
    import json

    from memora import storage

    local, replica, _ = env
    monkeypatch.setattr(storage, "backend_for", lambda name: local)
    monkeypatch.setenv("MEMORA_REPLICATION", "log")
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"s1": URI}))
    monkeypatch.delenv("MEMORA_SHADOW_LOCAL", raising=False)
    R.stop_replicators()
    yield local
    R.stop_replicators()


def test_start_replicators_applies_the_values_and_health_shows_them(configured, monkeypatch):
    monkeypatch.setenv("MEMORA_REPLICATION_INTERVAL_S", "60")
    monkeypatch.setenv("MEMORA_REPLICATION_POLL_S", "2.5")
    monkeypatch.setenv("MEMORA_REPLICATION_BATCH_ROWS", "250")
    out = R.start_replicators(start=False)
    assert out == {"s1": {"mode": "log", "shadow": False}}
    rep = R.replicator_for("s1")
    assert (rep.interval_s, rep.poll_s, rep.batch_rows) == (60.0, 2.5, 250)
    rep._open()
    status = rep.status()
    assert (status["interval_s"], status["poll_s"], status["batch_rows"]) == (60.0, 2.5, 250)
    assert R.start_refusal("s1") is None


def test_an_invalid_value_refuses_the_store_with_the_reason_in_health(configured, monkeypatch):
    from memora import admin

    monkeypatch.setenv("MEMORA_REPLICATION_BATCH_ROWS", "5000")
    out = R.start_replicators(start=False)
    assert "MEMORA_REPLICATION_BATCH_ROWS='5000'" in out["s1"]["error"]
    assert R.replicator_for("s1") is None  # not started with a default
    assert "MEMORA_REPLICATION_BATCH_ROWS" in R.start_refusal("s1")

    class Gate:
        def status(self):
            return {"state": "open"}

    class Backend:
        def write_gate(self):
            return Gate()

    monkeypatch.setattr(admin, "_backend", lambda name: (Backend(), None))
    body = admin.gate_health("s1")
    assert body["replication"]["status"] == "refused"
    assert "MEMORA_REPLICATION_BATCH_ROWS" in body["replication"]["error"]


def test_a_later_valid_start_clears_the_refusal(configured, monkeypatch):
    monkeypatch.setenv("MEMORA_REPLICATION_POLL_S", "0")
    R.start_replicators(start=False)
    assert R.start_refusal("s1")
    monkeypatch.setenv("MEMORA_REPLICATION_POLL_S", "1")
    R.start_replicators(start=False)
    assert R.start_refusal("s1") is None and R.replicator_for("s1") is not None
