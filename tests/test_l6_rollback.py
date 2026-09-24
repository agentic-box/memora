"""L6 piece b: the §5.3 rollback runbook (drain / verify / finish), restamp
(write path 4), the drain under MEMORA_READONLY_DBS, and §9 (w) (inbound
crossref dependencies in the conflicts file).

Offline: D1 is a FakeReplica (the SELECT-only reader, and a D1Connection
whose send runs on it for the read-only integrity audit); memora-all's
admin routes are an in-process fake or FreezeServer; "stopped" is a fake
barrier or a docker recorder.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memora import backends
from memora import local_primary as lp
from memora import replicator as R
from memora import rollback as rb
from tests.l3_fakes import URI, FakeReplica, local_store, sync_state
from tests.test_l5_export import DB as L5_DB
from tests.test_l5_export import FakeBarrier, FreezeServer, replica_exec, token_args
from tests.test_l5_seed import ReplicaSend
from tests.test_l6_compare import DB, drain, raw_local, write

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "l5_cli_harness.py"


class FakeAdmin(FakeBarrier):
    """memora-all live: freeze/check/thaw and /health/db's body."""

    def __init__(self, frozen=False, health=None):
        super().__init__(frozen=frozen)
        self.db = DB
        self.health = health or {}

    def _request(self, method, path):
        return 200, {"status": "ok", **self.health, "freeze": {"state": "frozen" if self.frozen else "open"}}


class Stopped(FakeBarrier):
    """memora-all stopped (docker State.Running=false), unless `running`."""
    stopped_service = True

    def __init__(self, running=False):
        super().__init__(frozen=True)
        self.running = running

    def check(self, where):
        self.calls.append(f"check {where}")
        if self.running:
            raise lp.L5Refused(f"memora-all must be stopped {where} (State.Running=true)")

    def freeze(self):  # like lp.ServiceStopped: nothing to place, only a check
        self.check("at the start")


def _d1_send_on(replica):
    def send(self, sql, params=None):
        db = replica._db()
        try:
            cur = db.execute(sql, tuple(params or ()))
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            db.commit()
            return {"success": True, "result": [{"results": rows, "success": True,
                                                  "meta": {"changes": max(cur.rowcount, 0), "served_by_primary": True}}]}
        finally:
            db.close()
    return send


@pytest.fixture
def pair(tmp_path, monkeypatch):
    replica = FakeReplica(tmp_path / "d1.db")
    local = local_store(tmp_path / "local.db", epoch=replica.epoch())
    conn = local.connect()
    try:
        for i in (1, 2, 3):
            conn.execute("INSERT INTO memories (id, content, metadata, tags, created_at) VALUES (?, ?, '{}', '[]', 't')",
                         (i, f"memory {i}"))
            conn.execute("INSERT INTO memories_embeddings (memory_id, embedding, representation, dimension, "
                         "encoding_source, writer_token) VALUES (?, '[0.1, 0.2]', 'r1', 2, 'local', 'w')", (i,))
        conn.execute("INSERT INTO memories_meta (key, value) VALUES ('other', 'v')")
        conn.commit()
    finally:
        conn.close()
    drain(local, replica)
    monkeypatch.setattr(backends.D1Connection, "_send", _d1_send_on(replica))
    return local, replica


def deps(pair, tmp_path, *, admin=None, stopped=None, send=None, **kw):
    local, replica = pair
    return rb.RollbackDeps(
        db=DB, store=local.db_path, account_id="acct", database_id="replica-db",
        reader=lp.D1Reader(replica.reader()), admin=admin or FakeAdmin(), stopped=stopped or Stopped(),
        r2=lp.FsR2(tmp_path / "r2"), out_dir=tmp_path / "out",
        d1_audit_conn=lambda: rb.d1_audit_connection("acct", "replica-db", "read-token"),
        writer_factory=lambda: lp.OperatorD1Writer(send or ReplicaSend(replica), allow_restamp=True),
        sleep=kw.pop("sleep", lambda s: None), poll_s=0, **kw)


def _d1_seq(replica, name="memories"):
    db = sqlite3.connect(replica.path)
    try:
        row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


# ---------------------------------------------------------------- the whole runbook

def test_the_rollback_runbook_end_to_end(pair, tmp_path):
    local, replica = pair
    raw_local(local, "UPDATE sqlite_sequence SET seq = 10 WHERE name = 'memories'")  # ids used and gone locally
    write(local, "UPDATE memories SET content = 'pending at the freeze' WHERE id = 1")
    admin = FakeAdmin()
    d = deps(pair, tmp_path, admin=admin, sleep=lambda s: drain(local, replica))
    out = rb.phase_drain(d)
    assert out["drained_head"] == sync_state(local)["last_acked_seq"] and admin.frozen
    assert admin.calls[0] == "freeze" and "check after the drain" in admin.calls
    stopped = Stopped()
    d.stopped = stopped
    out = rb.phase_verify(d)
    assert out["compare_clean"] is True and out["integrity"]["differs"] == []
    assert out["sequence"]["statements"] == [(lp.SEQ_UPDATE_SQL, (10, "memories", 10))] and _d1_seq(replica) == 10
    assert out["repoint"] == {"MEMORA_DATABASES": {DB: URI}, "MEMORA_REPLICAS": f"remove {DB!r}"}
    assert Path(out["receipt"]).is_file() and Path(out["compare_report"]).is_file()
    assert all(c.startswith("check") for c in stopped.calls) and len(stopped.calls) >= 6
    assert sync_state(local)["last_compare_clean"] == 1
    admin.health = {"journal": {"path": "/data/intent/s1"}}  # repointed: a d1:// store, no replicator
    out = rb.phase_finish(d)
    assert out["thawed"] is True and not admin.frozen
    st = rb.load_state(d, need="verify")
    assert set(st["phases"]) == {"drain", "verify", "finish"}


# ---------------------------------------------------------------- boundary refusals

def test_drain_refuses_when_the_replicator_does_not_drain_and_keeps_the_freeze(pair, tmp_path):
    local, replica = pair
    write(local, "UPDATE memories SET content = 'stuck' WHERE id = 1")
    admin = FakeAdmin()
    clock = iter(range(0, 10_000, 100))
    d = deps(pair, tmp_path, admin=admin, clock=lambda: float(next(clock)), drain_timeout_s=300)
    with pytest.raises(lp.L5Refused, match="not drained after 300 s"):
        rb.phase_drain(d)
    assert admin.frozen and "thaw" not in admin.calls
    assert not rb.load_state(d)  # nothing recorded


def test_drain_rechecks_the_freeze_while_waiting(pair, tmp_path):
    local, replica = pair
    write(local, "UPDATE memories SET content = 'pending' WHERE id = 1")
    admin = FakeAdmin()
    admin.bad_at = "while draining"
    with pytest.raises(lp.L5Refused, match="while draining"):
        rb.phase_drain(deps(pair, tmp_path, admin=admin))


def test_verify_needs_the_drain_phase(pair, tmp_path):
    with pytest.raises(lp.L5Refused, match="phase 'drain' has not completed|no rollback in progress"):
        rb.phase_verify(deps(pair, tmp_path))


def test_verify_refuses_while_memora_all_runs_and_writes_nothing(pair, tmp_path):
    d = deps(pair, tmp_path)
    rb.phase_drain(d)
    d.stopped = Stopped(running=True)
    with pytest.raises(lp.L5Refused, match="must be stopped before the rollback verify"):
        rb.phase_verify(d)
    assert not list((tmp_path / "out").rglob("*.receipt.json"))


def test_an_unclean_rollback_compare_stops_and_changes_nothing_on_d1(pair, tmp_path):
    """P7: a D1-only row is reported as a deletion for a human; nothing is
    deleted, and the sequence step never runs."""
    local, replica = pair
    raw_local(local, "UPDATE sqlite_sequence SET seq = 10 WHERE name = 'memories'")
    replica_exec(replica, "INSERT INTO memories (id, content, created_at) VALUES (8, 'foreign', 't')")
    d = deps(pair, tmp_path)
    rb.phase_drain(d)
    with pytest.raises(lp.L5Halt, match=r"Keys present only on D1 -- deletions a human must decide: \{'memories': \[\[8\]\]\}"):
        rb.phase_verify(d)
    assert _d1_seq(replica) != 10
    assert sqlite3.connect(replica.path).execute("SELECT content FROM memories WHERE id = 8").fetchone() == ("foreign",)
    st = rb.load_state(d)
    assert "verify" not in st["phases"] and st["phases"]["verify_failed"]["compare_clean"] is False


def test_a_rejected_sequence_update_stops_the_rollback(pair, tmp_path):
    local, replica = pair
    raw_local(local, "UPDATE sqlite_sequence SET seq = 10 WHERE name = 'memories'")
    d = deps(pair, tmp_path, send=ReplicaSend(replica, "reject"))
    rb.phase_drain(d)
    with pytest.raises(lp.L5Halt, match="D1 rejected"):
        rb.phase_verify(d)
    assert "verify" not in rb.load_state(d)["phases"]


def test_an_integrity_audit_that_differs_stops_the_rollback(pair, tmp_path, monkeypatch):
    real = rb._audit_d1
    monkeypatch.setattr(rb, "_audit_d1", lambda d: {**real(d), "missing_ids": [2]})
    d = deps(pair, tmp_path)
    rb.phase_drain(d)
    with pytest.raises(lp.L5Halt, match=r"integrity audit differs from the local store's in \['missing_ids'\]"):
        rb.phase_verify(d)


def test_the_integrity_audit_is_read_only_on_d1(pair, tmp_path):
    conn = rb.d1_audit_connection("acct", "replica-db", "read-token")
    with pytest.raises(backends.StoreReadOnlyError):
        conn.execute("UPDATE memories SET content = 'x' WHERE id = 1")
    audit = rb._audit_d1(deps(pair, tmp_path))
    assert audit["memory_count"] == 3


def test_a_d1_change_beyond_the_sequence_step_stops_the_rollback(pair, tmp_path):
    local, replica = pair
    raw_local(local, "UPDATE sqlite_sequence SET seq = 10 WHERE name = 'memories'")

    class Sneaky(ReplicaSend):
        def _send(self, sql, params):
            out = super()._send(sql, params)
            replica_exec(self.replica, "UPDATE memories_meta SET value = 'moved' WHERE key = 'other'")
            return out

    d = deps(pair, tmp_path, send=Sneaky(replica))
    rb.phase_drain(d)
    with pytest.raises(lp.L5Halt, match=r"D1 changed during the rollback beyond the sequence step: \['memories_meta', 'sqlite_sequence'\]"):
        rb.phase_verify(d)


def test_finish_needs_the_repoint(pair, tmp_path):
    admin = FakeAdmin()
    d = deps(pair, tmp_path, admin=admin)
    with pytest.raises(lp.L5Refused, match="phase 'verify' has not completed|no rollback in progress"):
        rb.phase_finish(d)
    rb.phase_drain(d)
    rb.phase_verify(d)
    admin.health = {"replication": {"status": "running"}}  # still the local primary
    with pytest.raises(lp.L5Refused, match="is not served from D1 yet"):
        rb.phase_finish(d)
    assert admin.frozen and "thaw" not in admin.calls


def test_rollback_state_is_bound_to_the_store_and_d1_identity(pair, tmp_path):
    d = deps(pair, tmp_path)
    rb.phase_drain(d)
    d.database_id = "other-db"
    with pytest.raises(lp.L5Refused, match="no rollback in progress"):
        rb.phase_verify(d)


# ---------------------------------------------------------------- restamp (write path 4)

def _rolled_back(pair, tmp_path):
    admin = FakeAdmin()
    d = deps(pair, tmp_path, admin=admin)
    rb.phase_drain(d)
    rb.phase_verify(d)
    admin.health = {"journal": {}}
    rb.phase_finish(d)
    return d, admin


def test_restamp_writes_exactly_one_integrity_row_and_reads_it_back(pair, tmp_path):
    from memora.embeddings import integrity_stamp_value

    local, replica = pair
    d, admin = _rolled_back(pair, tmp_path)
    admin.freeze()  # restamp runs under a freeze
    receipt = lp.export(DB, d.lp_deps(admin), d.out_dir)
    sent = []

    class Recording(ReplicaSend):
        def _send(self, sql, params):
            sent.append((sql, params))
            return super()._send(sql, params)

    d.writer_factory = lambda: lp.OperatorD1Writer(Recording(replica), allow_restamp=True)
    out = rb.restamp(d, str(receipt))
    assert len(sent) == 1 and sent[0][0] == lp.RESTAMP_SQL and sent[0][1][0] == "embedding_integrity"
    value = sqlite3.connect(replica.path).execute(
        "SELECT value FROM memories_meta WHERE key = 'embedding_integrity'").fetchone()[0]
    stamp = json.loads(value)
    assert value == sent[0][1][1] == integrity_stamp_value(stamp)
    assert stamp["state"] == "initialized" and stamp["generation"] == out["generation"]
    assert stamp["memory_count"] == 3


def test_restamp_needs_the_rollback_a_freeze_and_a_usable_receipt(pair, tmp_path):
    d = deps(pair, tmp_path)
    with pytest.raises(lp.L5Refused, match="no rollback in progress"):
        rb.restamp(d, "whatever.json")
    d, admin = _rolled_back(pair, tmp_path)
    receipt = lp.export(DB, d.lp_deps(admin), d.out_dir)  # (export freezes)
    admin.thaw()
    with pytest.raises(lp.L5Refused, match="not frozen before the recheck"):
        rb.restamp(d, str(receipt))
    admin.freeze()
    r = json.loads(Path(receipt).read_text())
    r["verified_at_epoch"] = 0
    Path(receipt).write_text(json.dumps(r))
    with pytest.raises(lp.L5Refused, match="older than 24 h"):
        rb.restamp(d, str(receipt))


def test_the_restamp_writer_accepts_nothing_else(pair):
    local, replica = pair
    w = lp.OperatorD1Writer(ReplicaSend(replica), allow_restamp=True)
    for sql, params in ((lp.RESTAMP_SQL, ("other", "x")), (lp.RESTAMP_SQL, ("embedding_integrity",)),
                        ("DELETE FROM memories_meta WHERE key = ?", ("embedding_integrity",)),
                        ("INSERT INTO memories_meta (key, value) VALUES (?, ?)", ("embedding_integrity", "x"))):
        with pytest.raises(lp.L5Refused, match="allow-list"):
            w.send(sql, params)
    plain = lp.OperatorD1Writer(ReplicaSend(replica))
    with pytest.raises(lp.L5Refused, match="allow-list"):
        plain.send(lp.RESTAMP_SQL, ("embedding_integrity", "x"))


def test_a_rejected_restamp_halts(pair, tmp_path):
    local, replica = pair
    d, admin = _rolled_back(pair, tmp_path)
    admin.freeze()
    receipt = lp.export(DB, d.lp_deps(admin), d.out_dir)
    d.writer_factory = lambda: lp.OperatorD1Writer(ReplicaSend(replica, "reject"), allow_restamp=True)
    with pytest.raises(lp.L5Halt, match="D1 rejected the restamp"):
        rb.restamp(d, str(receipt))


# ---------------------------------------------------------------- drain under MEMORA_READONLY_DBS (§5.3 step 1)

def test_the_replicator_drains_a_store_named_read_only(tmp_path, monkeypatch):
    from memora.write_gate import StoreReadOnlyError

    replica = FakeReplica(tmp_path / "d1.db")
    path = tmp_path / "ro.db"
    local = local_store(path, epoch=replica.epoch())
    write(local, "INSERT INTO memories (id, content, created_at) VALUES (1, 'before the freeze', 't')")
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"ro": str(path)}))
    monkeypatch.setenv("MEMORA_REPLICAS", json.dumps({"ro": URI}))
    monkeypatch.setenv("MEMORA_READONLY_DBS", "ro")
    from memora import storage

    backend = storage.backend_for("ro")
    assert backend.write_gate().state.startswith("frozen")
    conn = backend.connect()
    try:
        with pytest.raises(StoreReadOnlyError):
            conn.execute("INSERT INTO memories (id, content, created_at) VALUES (2, 'refused', 't')")
    finally:
        conn.close()
    rep = R.StoreReplicator("ro", backend, URI, mode="write", writer_factory=replica.writer,
                            reader_factory=replica.reader, broadcast=lambda: None)
    rep._open()
    try:
        assert rep.run_once() == "sent"
        assert rep.run_once() == "idle"
    finally:
        rep._conn.close()
    assert sync_state(backend)["last_acked_seq"] >= 1
    assert sqlite3.connect(replica.path).execute("SELECT content FROM memories WHERE id = 1").fetchone() == (
        "before the freeze",)


# ---------------------------------------------------------------- §9 (w): inbound crossref dependencies

def test_conflicts_show_inbound_crossrefs_and_the_apply_warns_of_dangling_ones(tmp_path):
    from tests.test_l5_restore import Scenario

    replica = FakeReplica(tmp_path / "d1r.db")
    from tests.test_l5_export import seed_replica

    seed_replica(replica)
    sc = Scenario(replica, tmp_path)
    replica_exec(replica, "UPDATE memories_crossrefs SET related = '[{\"id\": 3, \"score\": 0.4}]' WHERE memory_id = 1")
    sc.receipt = lp.export(L5_DB, sc.deps(), sc.tmp / "exports")
    doc = json.loads(Path(sc.prepare()["conflicts"]).read_text())
    groups = {g["group"]: g for g in doc["groups"]}
    assert {"from": "memory:1", "side": "d1", "from_is_conflict": True} in groups["memory:3"]["inbound_refs"]
    assert {"from": "memory:1", "side": "snapshot", "from_is_conflict": True} in groups["memory:2"]["inbound_refs"]
    base = {"memory:1": "d1", "memory:2": "snapshot", "memory:4": "d1", "meta:other": "snapshot"}
    assert lp.dangling_references(doc, {**base, "memory:3": "d1"}) == [{"from": "memory:1", "to": "memory:3"}]
    assert lp.dangling_references(doc, {**base, "memory:3": "snapshot"}) == []
    assert lp.dangling_references(doc, {**base, "memory:1": "snapshot", "memory:3": "d1"}) == []


# ---------------------------------------------------------------- CLI, in subprocesses

def _cli(replica, *args, env_extra=None):
    e = {**os.environ, "L5_TEST_FAKE_D1": str(replica.path), "MEMORA_D1_READ_TOKEN": "read-token"}
    e.update(env_extra or {})
    r = subprocess.run([sys.executable, str(HARNESS), *args], capture_output=True, text=True, timeout=120,
                       env=e, cwd=str(REPO))
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    return r.returncode, out, r.stderr


def _docker(tmp_path, running):
    bin_dir = tmp_path / f"bin-{running}"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(f"#!/bin/sh\necho {running}\n")
    (bin_dir / "docker").chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_cli_rollback_phases_and_restamp(pair, tmp_path):
    local, replica = pair
    raw_local(local, "UPDATE sqlite_sequence SET seq = 10 WHERE name = 'memories'")
    srv = FreezeServer(db=DB)
    try:
        cred = tmp_path / "operator.tok"
        cred.write_text("operator-edit-token")
        cred.chmod(0o600)
        common = [DB, "--account", "acct", "--database-id", "replica-db", "--memora-url", srv.url,
                  *token_args(tmp_path), "--r2-dir", str(tmp_path / "r2"), "--out-dir", str(tmp_path / "out"),
                  "--store", str(local.db_path)]
        code, out, err = _cli(replica, "rollback", *common, "--phase", "drain")
        assert code == 0 and srv.state == "frozen", err
        code, out, _ = _cli(replica, "rollback", *common, "--phase", "verify", "--credential-file", str(cred),
                            env_extra=_docker(tmp_path, "true"))
        assert code == 2 and "must be stopped" in out["refused"] and out["recovery"].startswith("to abandon")
        code, out, err = _cli(replica, "rollback", *common, "--phase", "verify", "--credential-file", str(cred),
                              env_extra={**_docker(tmp_path, "false"), "L5_TEST_D1_WRITES": "apply"})
        assert code == 0 and out["compare_clean"] and _d1_seq(replica) == 10, err
        code, out, _ = _cli(replica, "rollback", *common, "--phase", "finish")
        assert code == 2 and "not served from D1" in out["refused"]
        srv.health_extra = {"journal": {}}
        code, out, err = _cli(replica, "rollback", *common, "--phase", "finish")
        assert code == 0 and out["thawed"] and srv.state == "open", err
        code, out, err = _cli(replica, "freeze", DB, "--memora-url", srv.url, *token_args(tmp_path))
        code, out, err = _cli(replica, "export", *[a for a in common if a not in ("--store", str(local.db_path))])
        assert code == 0, err
        code, out, err = _cli(replica, "restamp", *common, "--receipt", out["receipt"], "--credential-file", str(cred),
                              env_extra={"L5_TEST_D1_WRITES": "apply"})
        assert code == 0 and out["restamped"], err
    finally:
        srv.close()


def test_verify_refuses_a_store_that_is_not_drained(pair, tmp_path):
    local, replica = pair
    d = deps(pair, tmp_path)
    rb.phase_drain(d)
    write(local, "UPDATE memories SET content = 'written after the drain' WHERE id = 1")  # (a bypass)
    with pytest.raises(lp.L5Refused, match="the store is not drained"):
        rb.phase_verify(d)
    assert not list((tmp_path / "out").rglob("*.receipt.json"))


def test_a_restamp_d1_accepts_but_does_not_apply_halts(pair, tmp_path):
    local, replica = pair
    d, admin = _rolled_back(pair, tmp_path)
    admin.freeze()
    receipt = lp.export(DB, d.lp_deps(admin), d.out_dir)
    d.writer_factory = lambda: lp.OperatorD1Writer(ReplicaSend(replica, "accept-only"), allow_restamp=True)
    with pytest.raises(lp.L5Halt, match="does not read back the restamp"):
        rb.restamp(d, str(receipt))


def test_inbound_refs_skip_self_references_and_count_non_conflict_referrers(tmp_path):
    from tests.test_l5_export import seed_replica
    from tests.test_l5_restore import Scenario

    replica = FakeReplica(tmp_path / "d1r.db")
    seed_replica(replica)
    # before the snapshot: memory 1 points at 3, memory 3 at itself -- on both sides
    replica_exec(replica, "UPDATE memories_crossrefs SET related = '[3]' WHERE memory_id = 1")
    replica_exec(replica, "INSERT INTO memories_crossrefs (memory_id, related) VALUES (3, '[3]')")
    sc = Scenario(replica, tmp_path)  # then memory 3 is deleted on D1
    doc = json.loads(Path(sc.prepare()["conflicts"]).read_text())
    groups = {g["group"]: g for g in doc["groups"]}
    refs = groups["memory:3"]["inbound_refs"]
    assert {r["from"] for r in refs} == {"memory:1"} and not any(r["from_is_conflict"] for r in refs)
    base = {"memory:2": "snapshot", "memory:4": "d1", "meta:other": "snapshot"}
    assert lp.dangling_references(doc, {**base, "memory:3": "d1"}) == [{"from": "memory:1", "to": "memory:3"}]
    assert lp.dangling_references(doc, {**base, "memory:3": "snapshot"}) == []
