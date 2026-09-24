"""L6 piece a: the §5.2 compare (barrier, nightly, log), its report, and
the recorded outcome (compare_consumed_seq only on a clean run; health
fields; §2.7 d1_missing_vectors).

Offline: D1 is a FakeReplica read through the real D1SelectOnlyConnection;
the local store is a real local primary (sync installed) drained into it
by the real write-mode replicator. Diffs are injected on either side
directly (a raw connection for the local side, so no outbox row records
them -- a lost write).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memora import compare as cmp
from memora import local_primary as lp
from memora import replicator as R
from memora.backends import LocalSQLiteBackend
from tests.l3_fakes import URI, FakeReplica, local_store, sync_state
from tests.test_l5_export import FakeBarrier, replica_exec

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "l5_cli_harness.py"
DB = "s1"


# ---------------------------------------------------------------- fixtures

def _rep(local, replica):
    rep = R.StoreReplicator(DB, local, URI, mode="write", writer_factory=replica.writer,
                            reader_factory=replica.reader, broadcast=lambda: None)
    rep._open()
    return rep


def drain(local, replica):
    rep = _rep(local, replica)
    try:
        for _ in range(50):
            if rep.run_once() == "idle":
                return
        raise AssertionError("did not drain")
    finally:
        rep.stop() if hasattr(rep, "stop") else None
        if rep._conn is not None:
            rep._conn.close()
            rep._conn = None


def write(local, sql, params=()):
    conn = local.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def raw_local(local, sql, params=()):
    """A lost write: a raw connection with the sync triggers dropped for the
    statement, so nothing reaches the outbox."""
    db = sqlite3.connect(local.db_path)
    try:
        trig = db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_sync_%'").fetchall()
        for name, _ in trig:
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute(sql, params)
        for _name, ddl in trig:
            db.execute(ddl)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def pair(tmp_path):
    """A drained local primary and its D1: 3 memories with embeddings, a
    crossref, an action, a tombstone, meta keys."""
    replica = FakeReplica(tmp_path / "d1.db")
    local = local_store(tmp_path / "local.db", epoch=replica.epoch())
    conn = local.connect()
    try:
        for i in (1, 2, 3):
            conn.execute("INSERT INTO memories (id, content, metadata, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                         (i, f"memory {i}", json.dumps({"n": i}), '["t"]', "2026-09-01"))
            conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, representation, dimension, "
                         "encoding_source, writer_token) VALUES (?, ?, 'r1', 2, 'local', 'w')",
                         (i, json.dumps([0.1 * i, 0.2])))
        conn.execute("INSERT INTO memories_crossrefs (memory_id, related) VALUES (1, '[2]')")
        conn.execute("INSERT INTO memories_actions (memory_id, action, summary) VALUES (1, 'create', 's')")
        conn.execute("INSERT INTO tombstones (content_hash, memory_id, reason) VALUES ('h', 9, 'gone')")
        conn.execute("INSERT INTO memories_meta (key, value) VALUES ('other', 'v')")
        conn.commit()
    finally:
        conn.close()
    drain(local, replica)
    return local, replica


def env(tmp_path, local, replica, barrier=None, **kw):
    return cmp.Env(store=local.db_path, reader=lp.D1Reader(replica.reader()), work=tmp_path / "work",
                   barrier=barrier or FakeBarrier(frozen=True), sleep=kw.pop("sleep", lambda s: None),
                   poll_s=0, **kw)


def diffs(report, table):
    t = report["tables"][table]
    return {k: t[k] for k in ("only_local", "only_d1", "changed", "provenance_mismatch",
                              "columns_only_local", "columns_only_d1") if t[k]}


# ---------------------------------------------------------------- barrier

def test_a_drained_pair_compares_clean_under_the_barrier(pair, tmp_path):
    local, replica = pair
    barrier = FakeBarrier(frozen=True)
    rep = cmp.barrier_compare(env(tmp_path, local, replica, barrier))
    assert rep["clean"] and rep["diff_count"] == 0 and rep["d1_missing_vectors"] == 0
    assert rep["consumed_seq"] == rep["snapshot_head"] == sync_state(local)["last_acked_seq"] > 0
    assert rep["tables"]["memories"]["local"] == rep["tables"]["memories"]["d1"] == 3
    assert barrier.calls == ["check before the barrier compare", "check after the drain",
                             "check after the snapshot", "check after reading D1"]


@pytest.mark.parametrize("inject, table, expect", [
    (("d1", "INSERT INTO memories (id, content, created_at) VALUES (9, 'foreign', 't')"), "memories",
     {"only_d1": [[9]]}),
    (("d1", "DELETE FROM memories_crossrefs WHERE memory_id = 1"), "memories_crossrefs", {"only_local": [[1]]}),
    (("local", "DELETE FROM memories_actions WHERE id = 1"), "memories_actions", {"only_d1": [[1]]}),
    (("d1", "UPDATE memories SET content = 'edited on D1' WHERE id = 2"), "memories",
     {"changed": [{"pk": [2], "columns": ["content"]}]}),
    (("local", "UPDATE tombstones SET reason = 'x' WHERE memory_id = 9"), "tombstones",
     {"changed": [{"pk": ["h", 9], "columns": ["reason"]}]}),
    (("d1", "UPDATE memories_meta SET value = 'w' WHERE key = 'other'"), "memories_meta",
     {"changed": [{"pk": ["other"], "columns": ["value"]}]}),
    (("d1", "UPDATE memories_embeddings SET encoding_source = 'unknown', representation = NULL WHERE memory_id = 3"),
     "memories_embeddings",
     {"changed": [{"pk": [3], "columns": ["encoding_source", "representation"]}],
      "provenance_mismatch": [{"pk": [3], "columns": ["encoding_source", "representation"]}]}),
    (("d1", "ALTER TABLE tombstone_components ADD COLUMN extra TEXT"), "tombstone_components",
     {"columns_only_d1": ["extra"]}),
    (("local", "ALTER TABLE tombstone_components ADD COLUMN mine TEXT"), "tombstone_components",
     {"columns_only_local": ["mine"]}),
])
def test_every_kind_of_diff_is_reported_and_blocks(pair, tmp_path, inject, table, expect):
    local, replica = pair
    side, sql = inject
    replica_exec(replica, sql) if side == "d1" else raw_local(local, sql)
    rep = cmp.barrier_compare(env(tmp_path, local, replica))
    assert not rep["clean"] and rep["consumed_seq"] is None
    assert diffs(rep, table) == expect
    assert rep["diff_count"] == sum(len(v) for v in expect.values() if v) - len(expect.get("provenance_mismatch", []))


@pytest.mark.parametrize("sql, missing", [
    ("DELETE FROM memories_embeddings WHERE memory_id = 2", 1),
    ("UPDATE memories_embeddings SET embedding = NULL WHERE memory_id = 2", 1),
    ("UPDATE memories_embeddings SET embedding = '[9]' WHERE memory_id = 2", 0),
])
def test_missing_vectors_are_counted_for_the_degraded_mirror(pair, tmp_path, sql, missing):
    """§2.7: an embedding the local store has and D1 lacks (row gone, or its
    vector NULL) is a d1_missing_vector; a different vector is only a diff."""
    local, replica = pair
    replica_exec(replica, sql)
    rep = cmp.barrier_compare(env(tmp_path, local, replica))
    assert not rep["clean"] and rep["d1_missing_vectors"] == missing


def test_a_change_to_an_excluded_meta_key_is_no_diff(pair, tmp_path):
    local, replica = pair
    for key in ("embedding_change_epoch", "embedding_rebuild_lease", "embedding_integrity"):
        replica_exec(replica, "INSERT OR REPLACE INTO memories_meta (key, value) VALUES (?, 'd1-only')", (key,))
    rep = cmp.barrier_compare(env(tmp_path, local, replica))
    assert rep["clean"], diffs(rep, "memories_meta")


def test_barrier_refuses_without_the_freeze(pair, tmp_path):
    local, replica = pair
    with pytest.raises(lp.L5Refused, match="not frozen before the barrier compare"):
        cmp.barrier_compare(env(tmp_path, local, replica, FakeBarrier(frozen=False)))


def test_barrier_refuses_when_not_drained(pair, tmp_path):
    local, replica = pair
    write(local, "UPDATE memories SET content = 'pending' WHERE id = 1")
    waits = []
    with pytest.raises(cmp.CompareRefused, match="not drained after"):
        cmp.barrier_compare(env(tmp_path, local, replica, sleep=waits.append, clock=_ticking()),
                            drain_timeout_s=3)
    assert waits  # it waited for the replicator before refusing


def test_barrier_waits_for_the_drain(pair, tmp_path):
    local, replica = pair
    write(local, "UPDATE memories SET content = 'pending' WHERE id = 1")
    rep = cmp.barrier_compare(env(tmp_path, local, replica, sleep=lambda s: drain(local, replica),
                                  clock=_ticking()), drain_timeout_s=10)
    assert rep["clean"] and rep["consumed_seq"] == sync_state(local)["last_acked_seq"]


@pytest.mark.parametrize("where", ["after the drain", "after the snapshot", "after reading D1"])
def test_barrier_rechecks_the_freeze_at_every_boundary(pair, tmp_path, where):
    local, replica = pair
    with pytest.raises(lp.L5Refused, match=where):
        cmp.barrier_compare(env(tmp_path, local, replica, FakeBarrier(frozen=True, bad_at=where)))


def _ticking(step=1.0):
    t = [0.0]

    def clock():
        t[0] += step
        return t[0]

    return clock


# ---------------------------------------------------------------- nightly

def test_nightly_excludes_keys_written_after_the_snapshot_and_reports_them_the_second_night(pair, tmp_path):
    """K: memory 3 is written (and drained) after S -- S and D1 differ on it,
    but it is excluded. The same key excluded the next night is reported."""
    local, replica = pair
    write(local, "UPDATE memories SET content = 'before S' WHERE id = 2")  # unacked when S is taken

    def during_wait(_s):
        drain(local, replica)  # the acks pass H ...
        write(local, "UPDATE memories SET content = 'after S' WHERE id = 3")  # ... then a hot write
        drain(local, replica)

    state = tmp_path / "nightly.json"
    rep = cmp.nightly_compare(env(tmp_path, local, replica, sleep=during_wait, clock=_ticking()),
                              state_path=state, night="2026-09-20")
    assert rep["clean"] and rep["tables"]["memories"]["excluded"] == 1
    assert ["memories", [3]] in rep["excluded_keys"] and rep["hot_keys"] == []
    assert rep["consumed_seq"] == rep["snapshot_head"]
    write(local, "UPDATE memories SET content = 'before S again' WHERE id = 2")
    rep2 = cmp.nightly_compare(env(tmp_path, local, replica, sleep=during_wait, clock=_ticking()),
                               state_path=state, night="2026-09-21")
    assert rep2["hot_keys"] == [["memories", [3]]]
    # a rerun the same night still compares with the night before
    write(local, "UPDATE memories SET content = 'rerun' WHERE id = 2")
    rep3 = cmp.nightly_compare(env(tmp_path, local, replica, sleep=during_wait, clock=_ticking()),
                               state_path=state, night="2026-09-21")
    assert rep3["hot_keys"] == [["memories", [3]]]


class FlakyReader(lp.D1Reader):
    """The first full read of memories sees a foreign edit that is gone by
    the retry; between the two, the store moves on (so S/H/K change)."""

    def __init__(self, replica, local):
        super().__init__(replica.reader())
        self.replica, self.local, self.reads = replica, local, 0

    def all_rows(self, table):
        rows = list(super().all_rows(table))
        if table == "memories":
            self.reads += 1
            if self.reads == 1:
                rows = [{**r, "content": "transient"} if r["id"] == 1 else r for r in rows]
                write(self.local, "INSERT INTO memories (id, content, created_at) VALUES (7, 'new', 't')")
                drain(self.local, self.replica)
        return iter(rows)


def test_nightly_retries_once_retaking_s_h_and_k(pair, tmp_path):
    local, replica = pair
    e = env(tmp_path, local, replica)
    e.reader = FlakyReader(replica, local)
    rep = cmp.nightly_compare(e, state_path=tmp_path / "n.json", night="n1")
    assert rep["clean"] and len(rep["attempts"]) == 2
    first, second = rep["attempts"]
    assert first["diff_count"] == 1 and second["diff_count"] == 0
    assert second["snapshot_head"] > first["snapshot_head"]  # a NEW snapshot and head
    assert rep["tables"]["memories"]["local"] == 4  # S2 has memory 7


def test_a_persistent_nightly_diff_fails_after_one_retry(pair, tmp_path):
    local, replica = pair
    replica_exec(replica, "UPDATE memories SET content = 'foreign' WHERE id = 1")
    rep = cmp.nightly_compare(env(tmp_path, local, replica), state_path=tmp_path / "n.json", night="n1")
    assert not rep["clean"] and len(rep["attempts"]) == 2 and rep["consumed_seq"] is None


def test_nightly_skips_when_the_acks_do_not_reach_h(pair, tmp_path):
    local, replica = pair
    write(local, "UPDATE memories SET content = 'never drained' WHERE id = 1")
    state = tmp_path / "n.json"
    rep = cmp.nightly_compare(env(tmp_path, local, replica, clock=_ticking(600)), state_path=state,
                              wait_timeout_s=1800, night="n1")
    assert rep["skipped"].startswith("the acks did not reach H=") and rep["consumed_seq"] is None
    assert rep["clean"] is False and not state.exists()


# ---------------------------------------------------------------- recording

def genuine(pair, tmp_path, conn, *, mode="barrier", mutate=None):
    """A real compare report, registered as a run on the store: (path, sha)."""
    local, replica = pair
    run_id = R.begin_compare(conn)["run_id"]
    e = env(tmp_path, local, replica)
    rep = cmp.barrier_compare(e) if mode == "barrier" else cmp.nightly_compare(
        e, state_path=tmp_path / "n.json", night="n1")
    rep.update({"run_id": run_id, "db": DB, "d1_uri": URI, "account_id": "acct", "database_id": "replica-db"})
    if mutate:
        mutate(rep)
    return cmp.write_report(rep, tmp_path / "reports", DB)


def _st(conn):
    return dict(conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone())


def test_a_genuine_clean_report_advances_the_cursor_to_its_h(pair, tmp_path):
    local, _ = pair
    acked = sync_state(local)["last_acked_seq"]
    conn = local.connect()
    try:
        path, sha = genuine(pair, tmp_path, conn)
        out = R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)
        st = _st(conn)
        assert out["compare_consumed_seq"] == st["compare_consumed_seq"] == acked
        assert st["last_compare_clean"] == 1 and st["last_compare_report"] == sha and json.loads(st["compare_runs"]) == {}
        with pytest.raises(R.CompareNotRecorded, match="not registered"):  # a run records once
            R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)
    finally:
        conn.close()


def _forge(field, value):
    def mutate(rep):
        rep[field] = value
    return mutate


@pytest.mark.parametrize("case, match", [
    ("made-up hash", "sha256 is"),
    ("no file", "unreadable|no report file"),
    ("another store", "is for 'other'"),
    ("another D1", "this store replicates to"),
    ("H past acked", "past the acked head"),
    ("stale run", "a compare must record within 12 h"),
    ("tampered file", "sha256 is"),
    ("unregistered run", "was not registered"),
    ("snapshot missing", "snapshot is missing"),
    ("snapshot tampered", "snapshot is missing or does not match"),
    ("not a mode", "not a compare mode"),
])
def test_a_forged_report_cannot_advance_the_cursor(pair, tmp_path, case, match):
    """7695 P1: nothing the caller asserts is trusted; nothing is recorded."""
    local, _ = pair
    acked = sync_state(local)["last_acked_seq"]
    conn = local.connect()
    try:
        mutate = {"another store": _forge("db", "other"), "another D1": _forge("d1_uri", "d1://acct/other-db"),
                  "H past acked": _forge("snapshot_head", acked + 5), "unregistered run": _forge("run_id", "f" * 32),
                  "not a mode": _forge("mode", "weekly")}.get(case)
        path, sha = genuine(pair, tmp_path, conn, mutate=mutate)
        kw = {"report_path": str(path), "report_sha256": sha}
        if case == "made-up hash":
            kw["report_sha256"] = "0" * 64
        elif case == "no file":
            kw["report_path"] = str(tmp_path / "nothing.json")
        elif case == "tampered file":
            rep = json.loads(path.read_text())
            rep["clean"] = True
            rep["snapshot_head"] = acked
            path.write_text(json.dumps(rep) + " ")
        elif case == "stale run":
            st = _st(conn)
            runs = {k: v - 13 / 24 for k, v in json.loads(st["compare_runs"]).items()}
            raw = sqlite3.connect(local.db_path)
            raw.execute("UPDATE sync_state SET compare_runs = ?", (json.dumps(runs),))
            raw.commit()
            raw.close()
        elif case in ("snapshot missing", "snapshot tampered"):
            snap = Path(json.loads(path.read_text())["snapshot_path"])
            snap.unlink() if case == "snapshot missing" else snap.write_bytes(b"x")
        before = _st(conn)
        with pytest.raises(R.CompareNotRecorded, match=match):
            R.record_compare(conn, db=DB, **kw)
        after = _st(conn)
        assert after["compare_consumed_seq"] == 0 and after["last_compare_at"] is None
        assert after["compare_runs"] == before["compare_runs"]
    finally:
        conn.close()


def test_an_unclean_or_log_report_records_health_only(pair, tmp_path):
    local, replica = pair
    replica_exec(replica, "DELETE FROM memories_embeddings WHERE memory_id = 1")
    conn = local.connect()
    try:
        path, sha = genuine(pair, tmp_path, conn)
        out = R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)
        st = _st(conn)
        assert out["compare_consumed_seq"] == st["compare_consumed_seq"] == 0
        assert st["last_compare_clean"] == 0 and st["d1_missing_vectors"] == 1
    finally:
        conn.close()


def test_the_cursor_never_moves_backwards(pair, tmp_path):
    local, _ = pair
    acked = sync_state(local)["last_acked_seq"]
    conn = local.connect()
    try:
        path, sha = genuine(pair, tmp_path, conn)
        R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)
        path, sha = genuine(pair, tmp_path, conn, mutate=_forge("snapshot_head", acked - 1))
        R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)
        assert _st(conn)["compare_consumed_seq"] == acked
    finally:
        conn.close()


def _outbox_count(local):
    db = sqlite3.connect(local.db_path)
    try:
        return db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]
    finally:
        db.close()


def _age_outbox(local, days):
    raw = sqlite3.connect(local.db_path)
    raw.execute("UPDATE sync_outbox SET created_at = julianday('now') - ?", (days,))
    raw.commit()
    raw.close()


def test_the_outbox_is_pruned_only_after_a_clean_compare_consumed_it(pair, tmp_path):
    local, replica = pair
    _age_outbox(local, 2)
    write(local, "UPDATE memories SET content = 'x' WHERE id = 1")
    drain(local, replica)
    assert _outbox_count(local) > 1  # not consumed: kept
    conn = local.connect()
    try:
        path, sha = genuine(pair, tmp_path, conn)
        R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)
    finally:
        conn.close()
    write(local, "UPDATE memories SET content = 'y' WHERE id = 1")
    drain(local, replica)
    assert _outbox_count(local) == 2  # the old rows pruned; the last two are younger than 24 h


def test_pruning_never_removes_a_row_newer_than_an_in_progress_compare(pair, tmp_path, monkeypatch):
    """7695 P2b. With the shipped bounds (runs stale after 24 h, rows kept
    24 h) a prunable row can never be newer than a live run's start, so the
    rule is defence in depth; a longer stale bound shows it working."""
    local, replica = pair
    monkeypatch.setattr(R, "COMPARE_RUN_STALE_S", 72 * 3600)
    conn = local.connect()
    try:
        path, sha = genuine(pair, tmp_path, conn)
        R.record_compare(conn, db=DB, report_path=str(path), report_sha256=sha)  # everything consumed
        R.begin_compare(conn)
    finally:
        conn.close()
    raw = sqlite3.connect(local.db_path)
    runs = {k: v - 2.5 for k, v in json.loads(raw.execute("SELECT compare_runs FROM sync_state").fetchone()[0]).items()}
    raw.execute("UPDATE sync_state SET compare_runs = ?", (json.dumps(runs),))  # started 2.5 days ago
    raw.execute("UPDATE sync_outbox SET created_at = julianday('now') - 2")      # 2 days old: newer than it
    raw.commit()
    raw.close()
    kept = _outbox_count(local)
    write(local, "UPDATE memories SET content = 'z' WHERE id = 1")
    drain(local, replica)
    assert _outbox_count(local) == kept + 1  # nothing pruned while that compare runs
    raw = sqlite3.connect(local.db_path)
    raw.execute("UPDATE sync_state SET compare_runs = '{}'")
    raw.commit()
    raw.close()
    write(local, "UPDATE memories SET content = 'w' WHERE id = 1")
    drain(local, replica)
    assert _outbox_count(local) == 2  # the run gone: the old rows are pruned


def test_begin_drops_stale_runs_and_abort_removes_one(pair):
    local, _ = pair
    conn = local.connect()
    try:
        a = R.begin_compare(conn)["run_id"]
        raw = sqlite3.connect(local.db_path)
        runs = {a: json.loads(_st(conn)["compare_runs"])[a] - 2}
        raw.execute("UPDATE sync_state SET compare_runs = ?", (json.dumps(runs),))
        raw.commit()
        raw.close()
        b = R.begin_compare(conn)["run_id"]
        assert list(json.loads(_st(conn)["compare_runs"])) == [b]
        R.abort_compare(conn, b)
        assert json.loads(_st(conn)["compare_runs"]) == {}
    finally:
        conn.close()


def test_the_admin_route_begins_verifies_and_records(pair, tmp_path, monkeypatch):
    from memora import admin

    local, replica = pair
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({DB: str(local.db_path)}))
    acked = sync_state(local)["last_acked_seq"]
    status, body = admin.compare_action(DB, "begin", {})
    assert status == 200 and len(body["run_id"]) == 32
    rep = cmp.barrier_compare(env(tmp_path, local, replica))
    rep.update({"run_id": body["run_id"], "db": DB, "d1_uri": URI})
    path, sha = cmp.write_report(rep, tmp_path / "reports", DB)
    status, out = admin.compare_action(DB, "record", {"report": str(path), "report_sha256": "0" * 64})
    assert status == 409 and out["error"] == "compare_not_recorded"
    status, out = admin.compare_action(DB, "record", {"report": str(path), "report_sha256": sha})
    assert status == 200 and out["compare_consumed_seq"] == acked
    status, body = admin.compare_action(DB, "begin", {})
    assert admin.compare_action(DB, "abort", {"run_id": body["run_id"]})[0] == 200
    assert admin.compare_action(DB, "weird", {})[0] == 404
    r = _rep(local, replica)
    try:
        r._refresh_metrics()
        st = r.status()
    finally:
        r._conn.close()
        r._conn = None
    assert st["d1_missing_vectors"] == 0 and st["last_compare_clean"] is True
    assert st["last_compare_mode"] == "barrier" and st["compare_consumed_seq"] == acked


def test_the_admin_route_needs_the_admin_token():
    from memora import admin

    calls = []
    admin.set_admin_auth(lambda req: calls.append(req) or (403, {"error": "admin_disabled"}))
    try:
        assert admin.require_admin(object()) == (403, {"error": "admin_disabled"})
    finally:
        admin.set_admin_auth(admin._refuse_by_default)


# ---------------------------------------------------------------- log mode (§2.9 (b))

@pytest.fixture
def logged(tmp_path, monkeypatch):
    """A store seeded from a verified export, then written and logged by a
    log-mode replicator (it never touches D1)."""
    from tests.test_l5_export import make_deps, seed_replica

    replica = FakeReplica(tmp_path / "d1.db")
    seed_replica(replica)
    receipt = lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")
    store = tmp_path / "shadow" / f"{DB}.db"
    lp.seed(DB, str(receipt), store, make_deps(replica, tmp_path), tmp_path / "exports")
    local = LocalSQLiteBackend(store)
    write(local, "UPDATE memories SET content = 'logged edit' WHERE id = 2")
    write(local, "INSERT INTO memories (content, created_at) VALUES ('logged insert', 't')")
    write(local, "INSERT INTO memories_embeddings (memory_id, embedding, writer_token) VALUES (4, '[1]', 'w')")
    write(local, "UPDATE memories_crossrefs SET related = '[3]' WHERE memory_id = 1")
    write(local, "UPDATE memories_meta SET value = 'logged meta' WHERE key = 'other'")
    rep = R.StoreReplicator(DB, local, URI, mode="log", writer_factory=lambda u: None,
                            reader_factory=lambda u: None, broadcast=lambda: None)
    rep._open()
    try:
        status = "logged"
        while status == "logged":
            status = rep.run_once()
        assert status == "idle", status  # (a delete of a small table would halt on P3)
    finally:
        rep._conn.close()
    return local, replica, receipt


def _log_env(tmp_path, local, replica):
    return env(tmp_path, local, replica, clock=_ticking())


def _log_compare(tmp_path, local, replica, receipt, records=None):
    return cmp.log_compare(_log_env(tmp_path, local, replica), db=DB, receipt_path=str(receipt),
                           account_id="acct", database_id="replica-db",
                           log_records=records or (lambda: R.iter_log(DB)))


def test_the_log_replayed_into_the_seed_export_equals_the_store(logged, tmp_path):
    local, replica, receipt = logged
    rep = _log_compare(tmp_path, local, replica, receipt)
    assert rep["clean"], (rep["keys_missing_from_log"], rep["unexpected_log_keys"], rep["replay_error"],
                          {t: diffs(rep, t) for t in rep["tables"]})
    assert rep["log_records"] > 0 and rep["log_cursor_seq"] == rep["snapshot_head"]
    assert rep["consumed_seq"] is None


def test_a_wrong_logged_statement_is_a_builder_diff(logged, tmp_path):
    local, replica, receipt = logged

    def tampered():
        for r in R.iter_log(DB):
            if r["tbl"] == "memories" and r["pk"] == [2]:
                r = {**r, "params": [("tampered" if p == "logged edit" else p) for p in r["params"]]}
            yield r

    rep = _log_compare(tmp_path, local, replica, receipt, tampered)
    assert not rep["clean"] and diffs(rep, "memories") == {"changed": [{"pk": [2], "columns": ["content"]}]}


def test_a_missing_log_line_is_reported(logged, tmp_path):
    local, replica, receipt = logged
    rep = _log_compare(tmp_path, local, replica, receipt,
                       lambda: (r for r in R.iter_log(DB) if r["tbl"] != "memories_meta"))
    assert not rep["clean"] and rep["keys_missing_from_log"] == [["memories_meta", ["other"]]]


def test_an_unexpected_log_key_is_reported(logged, tmp_path):
    local, replica, receipt = logged

    def extra():
        yield from R.iter_log(DB)
        yield {"seq": 1, "index": 9, "tbl": "tombstones", "pk": ["zz", 5],
               "sql": "DELETE FROM tombstones WHERE content_hash = ? AND memory_id = ?", "params": ["zz", 5]}

    rep = _log_compare(tmp_path, local, replica, receipt, extra)
    assert not rep["clean"] and rep["unexpected_log_keys"] == [["tombstones", ["zz", 5]]]


def _parent_and_child(recs):
    """The memories parent the replicator added for the crossrefs update of
    memory 1 (no memories outbox row), and that child record."""
    child = next(r for r in recs if r["tbl"] == "memories_crossrefs" and r["pk"] == [1])
    parent = next(r for r in recs if r["tbl"] == "memories" and r["pk"] == [1])
    assert (parent["attempt_id"], parent["seq"]) == (child["attempt_id"], child["seq"])
    return parent, child


def test_the_added_parent_is_the_only_extra_memories_key_allowed(logged, tmp_path):
    """Review 7721 P1: the parent is allowed because it is the replicator's
    own UPSERT with a real child upsert; a key merely named memories is not."""
    local, replica, receipt = logged
    recs = list(R.iter_log(DB))
    parent, _child = _parent_and_child(recs)
    conn = local.connect()
    try:
        cols = R.added_parent_columns(conn)
    finally:
        conn.close()
    assert R.is_added_parent(("memories", (1,)), recs, cols)
    assert not R.is_added_parent(("memories", (1,)), recs, {}), "no schema: nothing passes"
    assert not R.is_added_parent(("memories", (1,)), recs, {**cols, "memories": cols["memories"][:2]})
    rep = _log_compare(tmp_path, local, replica, receipt)
    assert rep["clean"] and rep["unexpected_log_keys"] == []


@pytest.mark.parametrize("variant", ["no-op delete of an absent id", "no-op update on the parent",
                                     "parent without its child", "partial upsert as the parent"])
def test_an_extra_memories_key_that_is_not_an_added_parent_is_reported(logged, tmp_path, variant):
    local, replica, receipt = logged
    recs = list(R.iter_log(DB))
    parent, child = _parent_and_child(recs)
    key = [1]
    if variant == "no-op delete of an absent id":
        key = [99999]
        extra = [{**parent, "index": 0, "pk": key, "sql": "DELETE FROM memories WHERE id = ?", "params": key}]
    elif variant == "no-op update on the parent":
        extra = [{**parent, "index": 7, "sql": "UPDATE memories SET content = content WHERE id = ?", "params": [1]}]
    elif variant == "partial upsert as the parent":  # review 7733 P1: id + content only, in place of the real one
        recs = [r for r in recs if r is not parent]
        content = parent["params"][parent["sql"].split("(", 1)[1].split(")", 1)[0].split(", ").index("content")]
        extra = [{**parent, "sql": "INSERT INTO memories (id, content) VALUES (?, ?) "
                                   "ON CONFLICT(id) DO UPDATE SET content = excluded.content",
                  "params": [1, content]}]
    else:
        recs = [r for r in recs if r is not child]
        extra = []
    rep = _log_compare(tmp_path, local, replica, receipt, lambda: iter(recs + extra))
    assert not rep["clean"] and ["memories", key] in rep["unexpected_log_keys"], rep["unexpected_log_keys"]


def test_log_mode_accepts_the_old_seed_receipt_but_not_another_database(logged, tmp_path):
    local, replica, receipt = logged
    r = json.loads(receipt.read_text())
    r["verified_at_epoch"] = 0  # a seed export is days old
    receipt.write_text(json.dumps(r))
    with pytest.raises(lp.L5Refused, match="missing or changed|another D1 database|not verified") as exc:
        cmp.log_compare(_log_env(tmp_path, local, replica), db=DB, receipt_path=str(receipt), account_id="acct",
                        database_id="other-db", log_records=lambda: R.iter_log(DB))
    assert "another D1 database" in str(exc.value)
    assert _log_compare(tmp_path, local, replica, receipt)["clean"]


# ---------------------------------------------------------------- CLI, in a subprocess

def _cli(replica, *args, env_extra=None):
    e = {**os.environ, "L5_TEST_FAKE_D1": str(replica.path), "MEMORA_D1_READ_TOKEN": "read-token"}
    e.update(env_extra or {})
    r = subprocess.run([sys.executable, str(HARNESS), *args], capture_output=True, text=True, timeout=120,
                       env=e, cwd=str(REPO))
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    return r.returncode, out, r.stderr


def _stopped(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/bin/sh\necho false\n")
    (bin_dir / "docker").chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _args(local, tmp_path, mode, *extra):
    return ["compare", DB, "--mode", mode, "--store", str(local.db_path), "--account", "acct",
            "--database-id", "replica-db", "--out-dir", str(tmp_path / "compare"), *extra]


def test_cli_barrier_compare_with_the_service_stopped_records_directly(pair, tmp_path):
    local, replica = pair
    code, out, err = _cli(replica, *_args(local, tmp_path, "barrier", "--service-stopped"),
                          env_extra=_stopped(tmp_path))
    assert code == 0 and out["clean"] and out["recorded"]["compare_consumed_seq"] == out["consumed_seq"], err
    report = json.loads(Path(out["report"]).read_text())
    assert report["mode"] == "barrier" and report["db"] == DB
    st = sync_state(local)
    assert st["last_compare_clean"] == 1 and st["last_compare_report"] == out["report_sha256"]
    assert not list((tmp_path / "compare" / "work").glob("compare-*"))  # the snapshot removed once recorded


def test_cli_nightly_diff_exits_5_and_records_unclean(pair, tmp_path):
    local, replica = pair
    replica_exec(replica, "DELETE FROM memories_embeddings WHERE memory_id = 1")
    code, out, err = _cli(replica, *_args(local, tmp_path, "nightly", "--service-stopped"),
                          env_extra=_stopped(tmp_path))
    assert code == 5 and out["clean"] is False and out["d1_missing_vectors"] == 1, err
    st = sync_state(local)
    assert st["last_compare_clean"] == 0 and st["d1_missing_vectors"] == 1 and st["compare_consumed_seq"] == 0


def test_cli_nightly_skip_exits_6_and_records_nothing(pair, tmp_path):
    local, replica = pair
    write(local, "UPDATE memories SET content = 'never drained' WHERE id = 1")
    code, out, _ = _cli(replica, *_args(local, tmp_path, "nightly", "--service-stopped", "--wait-timeout", "0"),
                        env_extra=_stopped(tmp_path))
    assert code == 6 and out["skipped"] and out["recorded"] is None
    assert sync_state(local)["last_compare_at"] is None


def test_cli_compare_needs_a_way_to_record(pair, tmp_path):
    local, replica = pair
    code, out, _ = _cli(replica, *_args(local, tmp_path, "nightly"))
    assert code == 2 and "--no-record" in out["refused"]
    code, out, _ = _cli(replica, *_args(local, tmp_path, "nightly", "--no-record"))
    assert code == 0 and out["recorded"] is None


def test_iter_log_keeps_a_child_that_shares_its_seq_with_the_added_parent(tmp_path):
    """Found by the log-mode compare: the replicator logs an FK parent it
    added for a child under the CHILD's seq, so (seq, index) is not unique;
    a crash's re-append (all fields equal) is still dropped."""
    lines = [
        {"attempt_id": "a", "seq": 4, "index": 0, "tbl": "memories", "pk": [1], "sql": "P", "params": []},
        {"attempt_id": "a", "seq": 4, "index": 0, "tbl": "memories_crossrefs", "pk": [1], "sql": "C", "params": []},
        {"attempt_id": "b", "seq": 4, "index": 0, "tbl": "memories_crossrefs", "pk": [1], "sql": "C", "params": []},
    ]
    (tmp_path / "2026-09-24.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    got = [(r["tbl"], r["sql"]) for r in R.iter_log("x", tmp_path)]
    assert got == [("memories", "P"), ("memories_crossrefs", "C")]


def test_cli_weekly_brief_freeze_lifts_only_its_own_freeze(pair, tmp_path):
    from tests.test_l5_export import FreezeServer, token_args

    local, replica = pair
    for already in (False, True):
        srv = FreezeServer(db=DB, already_frozen=already)
        srv.intents = {}
        try:
            code, out, err = _cli(replica, *_args(local, tmp_path, "barrier", "--brief-freeze", "--no-record",
                                                  "--memora-url", srv.url, *token_args(tmp_path)))
            assert code == 0 and out["clean"], err
            methods = [m for m, _p in srv.methods() if m != "GET"]
            assert methods == ([] if already else ["POST", "DELETE"])
            assert srv.state == ("frozen" if already else "open")
        finally:
            srv.close()


def test_the_head_survives_a_fully_pruned_outbox(pair, tmp_path):
    """H is the highest seq ever ASSIGNED (sqlite_sequence), not MAX(seq) of
    what pruning left -- else a clean compare would consume 0."""
    local, replica = pair
    acked = sync_state(local)["last_acked_seq"]
    raw = sqlite3.connect(local.db_path)
    raw.execute("DELETE FROM sync_outbox")
    raw.commit()
    raw.close()
    rep = cmp.barrier_compare(env(tmp_path, local, replica))
    assert rep["clean"] and rep["snapshot_head"] == rep["consumed_seq"] == acked


def test_a_same_night_rerun_compares_with_the_night_before_not_itself(pair, tmp_path):
    local, replica = pair
    state = tmp_path / "n.json"

    def hot(mid):
        def during_wait(_s):
            drain(local, replica)
            write(local, f"UPDATE memories SET content = 'hot {mid}' WHERE id = {mid}")
            drain(local, replica)
        return during_wait

    for night, mid in (("n1", 3), ("n2", 2), ("n2", 2)):
        write(local, "UPDATE memories SET content = content || '.' WHERE id = 1")
        rep = cmp.nightly_compare(env(tmp_path, local, replica, sleep=hot(mid), clock=_ticking()),
                                  state_path=state, night=night)
        assert rep["clean"] and rep["hot_keys"] == [], (night, rep["hot_keys"])


def test_the_log_replay_enforces_foreign_keys_like_d1(logged, tmp_path):
    local, replica, receipt = logged

    def orphan():
        yield from R.iter_log(DB)
        yield {"seq": 1, "index": 5, "tbl": "memories_embeddings", "pk": [99],
               "sql": "INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?)", "params": [99, "[1]"]}

    rep = _log_compare(tmp_path, local, replica, receipt, orphan)
    assert not rep["clean"] and "FOREIGN KEY" in (rep["replay_error"] or "")


def test_the_admin_route_refuses_a_store_that_is_not_local(monkeypatch):
    from memora import admin

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "edit-token")  # the registry builds a D1 backend (no call is made)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"remote": "d1://acct/remote-db"}))
    status, body = admin.compare_action("remote", "begin", {})
    assert (status, body["error"]) == (400, "not_a_local_store")
