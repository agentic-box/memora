"""L5 piece c: restore (default full re-seed; --from-r2 with conflict
groups, an approve file bound to the conflicts file, preimage revalidation,
the P3 guard), reconcile show/accept, resume (docs/local-primary-
implementation.md §4, §1 "Reconciliation", §2.6, §0 P3/P7).

Offline: D1 is a FakeReplica (foreign keys on, D1's triggers), the
operator's D1 writer runs statements on it (or rejects them), R2 is FsR2,
memora-all's admin routes are FreezeServer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memora import local_primary as lp
from memora.backends import D1DefiniteError, LocalSQLiteBackend
from memora.replicator import _check_statement
from tests.l3_fakes import FakeReplica
from tests.test_l5_export import DB, FakeBarrier, FreezeServer, make_deps, replica_exec, seed_replica, token_args
from tests.test_l5_seed import ReplicaSend, URI, d1_now

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "l5_cli_harness.py"


def _rows(path, sql, params=()):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def _data_stats(stats):
    return {t: v for t, v in stats.items() if t != "sqlite_sequence"}


@pytest.fixture
def replica(tmp_path):
    r = FakeReplica(tmp_path / "d1.db")
    seed_replica(r)
    return r


def _export(replica, tmp_path):
    return lp.export(DB, make_deps(replica, tmp_path), tmp_path / "exports")


class Scenario:
    """A snapshot of the store taken while it equalled D1, then an operator
    error on D1: memory 2 edited, memory 3 deleted (its embedding cascades),
    memory 4 added, meta 'other' changed."""

    def __init__(self, replica, tmp_path):
        self.replica, self.tmp = replica, tmp_path
        self.r2 = lp.FsR2(tmp_path / "r2")
        store = tmp_path / "store" / f"{DB}.db"
        lp.seed(DB, str(_export(replica, tmp_path)), store, make_deps(replica, tmp_path), tmp_path / "exports")
        self.store = store
        self.key = lp.snapshot(DB, store, self.r2, tmp_path / "work")["key"]
        self.before = d1_now(replica)
        replica_exec(replica, "UPDATE memories SET content = 'overwritten by mistake' WHERE id = 2")
        replica_exec(replica, "DELETE FROM memories WHERE id = 3")
        replica_exec(replica, "INSERT INTO memories (content, created_at) VALUES ('new on D1', '2026-09-02')")
        replica_exec(replica, "UPDATE memories_meta SET value = 'changed' WHERE key = 'other'")
        self.receipt = _export(replica, tmp_path)

    def deps(self, send=None, barrier=None):
        deps = make_deps(self.replica, self.tmp, barrier, r2=self.r2)
        if send is not None:
            deps.writer_factory = lambda: lp.OperatorD1Writer(send, allow_restore=True)
        return deps

    def prepare(self, **kw):
        return lp.restore_prepare(DB, self.key, str(self.receipt), self.deps(**kw), self.tmp / "exports")

    def approve(self, conflicts, selections, *, sha=None):
        path = self.tmp / "approve.json"
        path.write_text(json.dumps({"conflicts_sha256": sha or lp._sha256_file(Path(conflicts)),
                                    "selections": selections}))
        return path


@pytest.fixture
def sc(replica, tmp_path):
    return Scenario(replica, tmp_path)


SELECT = {"memory:2": "snapshot", "memory:3": "snapshot", "memory:4": "d1", "meta:other": "snapshot"}


# ---------------------------------------------------------------- prepare

def test_prepare_groups_every_difference_with_both_versions_and_writes_nothing(sc):
    rep = sc.prepare()
    doc = json.loads(Path(rep["conflicts"]).read_text())
    groups = {g["group"]: g for g in doc["groups"]}
    assert sorted(groups) == ["memory:2", "memory:3", "memory:4", "meta:other"]
    g3 = groups["memory:3"]
    assert {r["table"] for r in g3["snapshot_rows"]} == {"memories", "memories_embeddings"}
    assert g3["d1_rows"] == []  # present only in the snapshot (the embedding cascaded on D1)
    g2 = groups["memory:2"]
    snap2 = [r for r in g2["snapshot_rows"] if r["table"] == "memories"][0]["row"]
    d12 = [r for r in g2["d1_rows"] if r["table"] == "memories"][0]["row"]
    assert snap2["content"] == "memory 2 it's quoted" and d12["content"] == "overwritten by mistake"
    # the unchanged embedding of memory 2 is in the group too (every row of the group)
    assert {r["table"] for r in g2["d1_rows"]} == {"memories", "memories_embeddings"}
    assert groups["memory:4"]["snapshot_rows"] == []
    assert doc["snapshot_key"] == sc.key and doc["database_id"] == "replica-db"
    assert rep["conflicts_sha256"] == lp._sha256_file(Path(rep["conflicts"]))
    assert d1_now(sc.replica) == d1_now(sc.replica)  # (no writer exists in prepare)


def test_prepare_excludes_the_meta_keys_that_are_never_replicated(sc):
    replica_exec(sc.replica, "UPDATE memories_meta SET value = value + 5 WHERE key = 'embedding_change_epoch'")
    sc.receipt = _export(sc.replica, sc.tmp)
    doc = json.loads(Path(sc.prepare()["conflicts"]).read_text())
    assert not [g for g in doc["groups"] if g["group"] == "meta:embedding_change_epoch"]


def test_prepare_needs_the_freeze(sc):
    with pytest.raises(lp.L5Refused, match="not frozen before the recheck"):
        sc.prepare(barrier=FakeBarrier(frozen=False))


def test_prepare_refuses_a_corrupt_snapshot(sc):
    (sc.tmp / "r2" / sc.key).write_bytes(b"not gzip")
    with pytest.raises(lp.L5Refused, match="not a readable gzip"):
        sc.prepare()


# ---------------------------------------------------------------- approve file

@pytest.mark.parametrize("mutate, match", [
    (lambda sel: sel, None),
    (lambda sel: {k: v for k, v in sel.items() if k != "meta:other"}, "missing=\\['meta:other'\\]"),
    (lambda sel: {**sel, "memory:99": "d1"}, "unknown=\\['memory:99'\\]"),
    (lambda sel: {**sel, "memory:2": "both"}, "invalid=\\['memory:2'\\]"),
])
def test_the_approve_file_must_choose_for_every_group(sc, mutate, match):
    c = sc.prepare()["conflicts"]
    a = sc.approve(c, mutate(dict(SELECT)))
    if match is None:
        conflicts, sel, sha = lp.load_approval(c, str(a), DB, sc.deps())
        assert sel == SELECT and sha == lp._sha256_file(Path(c))
        return
    with pytest.raises(lp.L5Refused, match=match):
        lp.load_approval(c, str(a), DB, sc.deps())


def test_the_approve_file_is_bound_to_the_conflicts_file(sc):
    c = sc.prepare()["conflicts"]
    a = sc.approve(c, SELECT, sha="0" * 64)
    with pytest.raises(lp.L5Refused, match="does not quote this conflicts file"):
        lp.load_approval(c, str(a), DB, sc.deps())
    a = sc.approve(c, SELECT)
    doc = json.loads(Path(c).read_text())
    doc["groups"][0]["snapshot_rows"] = []  # edited after approval
    Path(c).write_text(json.dumps(doc))
    with pytest.raises(lp.L5Refused, match="does not quote this conflicts file"):
        lp.load_approval(c, str(a), DB, sc.deps())


# ---------------------------------------------------------------- apply

def _apply(sc, conflicts, approve, send, **kw):
    out = kw.pop("out", sc.store)
    return lp.restore_apply(DB, conflicts, str(approve), str(sc.receipt), sc.deps(send=send), out,
                            sc.tmp / "exports", **kw)


def test_apply_writes_only_snapshot_groups_then_rebuilds_the_local_store_from_d1(sc):
    c = sc.prepare()["conflicts"]
    send = Recorder(sc.replica)
    rep = _apply(sc, c, sc.approve(c, SELECT), send)
    assert rep["applied"] == ["memory:2", "memory:3", "meta:other"] and rep["aborted"] == []
    assert rep["d1_groups"] == ["memory:4"]
    # every statement is a P2 shape; nothing touched memory 4 (the d1 choice)
    assert all(_check_statement(sql) in ("upsert", "insert", "delete") for sql, _ in send.sent)
    assert not [p for _s, p in send.sent if p and p[0] == 4]
    # parents first: memory 3's row before its embedding
    idx = [i for i, (sql, p) in enumerate(send.sent) if p and p[0] == 3]
    assert send.sent[idx[0]][0].startswith("INSERT INTO memories (")
    d1 = sqlite3.connect(sc.replica.path)
    assert d1.execute("SELECT content FROM memories WHERE id = 2").fetchone() == ("memory 2 it's quoted",)
    assert d1.execute("SELECT COUNT(*) FROM memories_embeddings WHERE memory_id = 3").fetchone() == (1,)
    assert d1.execute("SELECT content FROM memories WHERE id = 4").fetchone() == ("new on D1",)
    assert d1.execute("SELECT value FROM memories_meta WHERE key = 'other'").fetchone() == ("v",)
    d1.close()
    # the local store is D1 now (a fresh verified export), the old one kept aside
    local = rep["local"]
    now = d1_now(sc.replica)
    data = sorted(_data_stats(now))
    assert lp.local_stats(sc.store, data) == {t: now[t] for t in data}
    assert local["moved_aside"] and (Path(local["moved_aside"]) / sc.store.name).is_file()
    assert _rows(sc.store, "SELECT last_acked_seq FROM sync_state") == [(0,)]


class Recorder(ReplicaSend):
    def __init__(self, replica, mode="apply"):
        super().__init__(replica, mode)
        self.sent = []

    def _send(self, sql, params):
        self.sent.append((sql, tuple(params)))
        return super()._send(sql, params)


def test_apply_dry_run_prints_the_statements_and_writes_nothing(sc):
    c = sc.prepare()["conflicts"]
    before = d1_now(sc.replica)
    store_bytes = sc.store.read_bytes()
    send = Recorder(sc.replica)
    rep = _apply(sc, c, sc.approve(c, SELECT), send, dry_run=True)
    assert send.sent == [] and d1_now(sc.replica) == before and sc.store.read_bytes() == store_bytes
    assert set(rep["statements"]) == {"memory:2", "memory:3", "meta:other"}
    assert all(_check_statement(sql) for g in rep["statements"].values() for sql, _ in g)


def test_a_group_whose_d1_rows_changed_since_prepare_is_aborted_the_others_apply(sc):
    c = sc.prepare()["conflicts"]
    replica_exec(sc.replica, "UPDATE memories SET content = 'changed again' WHERE id = 2")  # after prepare
    sc.receipt = _export(sc.replica, sc.tmp)
    store_bytes = sc.store.read_bytes()
    with pytest.raises(lp.L5Refused) as exc:
        _apply(sc, c, sc.approve(c, SELECT), Recorder(sc.replica))
    rep = json.loads(str(exc.value))
    assert rep["aborted"] == ["memory:2"] and rep["applied"] == ["memory:3", "meta:other"]
    assert _rows(sc.replica.path, "SELECT content FROM memories WHERE id = 2") == [("changed again",)]
    assert sc.store.read_bytes() == store_bytes  # the local store is not rebuilt


def test_the_delete_guard_refuses_snapshot_deletes_until_that_attempt_is_allowed(sc):
    """P3 for restore replay: choosing the snapshot for memory 4 (absent
    there) DELETEs it on D1 -- 1 of its 3 rows, over 1%."""
    c = sc.prepare()["conflicts"]
    sel = {**SELECT, "memory:4": "snapshot"}
    a = sc.approve(c, sel)
    send = Recorder(sc.replica)
    with pytest.raises(lp.L5Refused, match=r"delete_guard: memories 1/3 attempt=(\w+)") as exc:
        _apply(sc, c, a, send)
    assert send.sent == []
    attempt = exc.value.args[0].split("attempt=")[1].split(":")[0]
    rep = _apply(sc, c, a, send, allow_deletes=attempt)
    assert "memory:4" in rep["applied"]
    assert _rows(sc.replica.path, "SELECT COUNT(*) FROM memories WHERE id = 4") == [(0,)]
    with pytest.raises(lp.L5Refused, match="delete_guard"):
        _apply(sc, c, a, send, allow_deletes="another-attempt")


def test_a_rejected_restore_statement_halts(sc):
    c = sc.prepare()["conflicts"]
    with pytest.raises(lp.L5Halt, match="group memory:2: D1 send failed"):
        _apply(sc, c, sc.approve(c, SELECT), Recorder(sc.replica, "reject"))


def test_a_restore_group_that_does_not_read_back_halts(sc):
    c = sc.prepare()["conflicts"]
    with pytest.raises(lp.L5Halt, match="does not read back as the snapshot"):
        _apply(sc, c, sc.approve(c, SELECT), Recorder(sc.replica, "accept-only"))


def test_the_restore_writer_accepts_only_p2_shapes(replica):
    w = lp.OperatorD1Writer(ReplicaSend(replica), allow_restore=True)
    for sql in ("UPDATE memories SET content = ? WHERE id = ?", "DROP TABLE memories",
                "DELETE FROM memories WHERE content = ?", "DELETE FROM sqlite_sequence WHERE name = ?",
                "INSERT INTO memories (content) VALUES (?); DELETE FROM memories WHERE id = ?"):
        with pytest.raises(lp.L5Refused, match="allow-list"):
            w.send(sql, (1,))
    w.send("DELETE FROM memories_meta WHERE key = ?", ("nothing",))
    plain = lp.OperatorD1Writer(ReplicaSend(replica))
    with pytest.raises(lp.L5Refused, match="allow-list"):
        plain.send("DELETE FROM memories_meta WHERE key = ?", ("nothing",))


# ---------------------------------------------------------------- default restore

def test_default_restore_reseeds_and_keeps_the_old_store_aside(sc):
    (sc.store.parent / f"{sc.store.name}-wal").write_bytes(b"")  # a sidecar moves with it
    rep = lp.restore(DB, str(sc.receipt), sc.store, sc.deps(), sc.tmp / "exports")
    now = d1_now(sc.replica)
    data = sorted(_data_stats(now))
    assert lp.local_stats(sc.store, data) == {t: now[t] for t in data}
    aside = Path(rep["moved_aside"])
    assert sorted(p.name for p in aside.iterdir()) == [sc.store.name, f"{sc.store.name}-wal"]


def test_a_failed_default_restore_puts_the_old_store_back(sc):
    old = sc.store.read_bytes()
    with pytest.raises(lp.L5Refused, match="not frozen after the recheck"):
        lp.restore(DB, str(sc.receipt), sc.store, sc.deps(barrier=FakeBarrier(frozen=True, bad_at="after the recheck")),
                   sc.tmp / "exports")
    assert sc.store.read_bytes() == old
    assert not list(sc.store.parent.glob("*.pre-restore-*"))


def test_default_restore_refuses_while_memora_all_holds_the_store(sc):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, time\n"
         f"fd = os.open({str(sc.store) + '.primary-lock'!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        old = sc.store.read_bytes()
        with pytest.raises(lp.L5Refused, match="stop memora-all first"):
            lp.restore(DB, str(sc.receipt), sc.store, sc.deps(), sc.tmp / "exports")
        assert sc.store.read_bytes() == old
    finally:
        holder.kill()
        holder.wait()


# ---------------------------------------------------------------- reconcile (§1)

@pytest.fixture
def admin(tmp_path):
    srv = FreezeServer()
    srv.intents = {"state": "frozen-unsafe", "open_intents": [
        {"id": 7, "sql": "UPDATE memories SET tags = ? WHERE id = ?", "evidence": {"status": "found"},
         "evidence_sha256": "e" * 64}]}
    tokens = token_args(tmp_path)
    client = lp.AdminClient(srv.url, "admin-tok", DB, health_token="health-tok")
    yield srv, client, tokens
    srv.close()


def test_reconcile_accept_sends_exactly_the_l2_accept_body(replica, tmp_path, admin):
    srv, client, _ = admin
    receipt = _export(replica, tmp_path)
    rep = lp.reconcile_accept(DB, client, intent_id=7, receipt_path=str(receipt), operator="spok",
                              decision="not-applied", evidence_sha256="e" * 64,
                              account_id="acct", database_id="replica-db")
    assert srv.reconcile_bodies == [("7", {"receipt": str(receipt), "operator": "spok", "intent_id": 7,
                                           "decision": "not-applied", "evidence_sha256": "e" * 64})]
    assert rep["response"]["outcome"] == "operator-accepted"


@pytest.mark.parametrize("kw, match", [
    ({"evidence_sha256": "f" * 64}, "evidence changed since you read it"),
    ({"intent_id": 8}, "intent 8 is not open"),
    ({"decision": "maybe"}, "decision must be one of"),
    ({"operator": "  "}, "--operator is required"),
    ({"database_id": "other-db"}, "another D1 database"),
])
def test_reconcile_accept_refuses_before_posting(replica, tmp_path, admin, kw, match):
    srv, client, _ = admin
    receipt = _export(replica, tmp_path)
    args = dict(intent_id=7, receipt_path=str(receipt), operator="spok", decision="applied",
                evidence_sha256="e" * 64, account_id="acct", database_id="replica-db")
    args.update(kw)
    with pytest.raises(lp.L5Refused, match=match):
        lp.reconcile_accept(DB, client, **args)
    assert srv.reconcile_bodies == []


def test_reconcile_accept_reports_a_server_refusal(replica, tmp_path, admin):
    srv, client, _ = admin
    srv.reconcile_status = 409
    with pytest.raises(lp.L5Refused, match=r"refused \(409\)"):
        lp.reconcile_accept(DB, client, intent_id=7, receipt_path=str(_export(replica, tmp_path)), operator="o",
                            decision="applied", evidence_sha256="e" * 64, account_id="acct",
                            database_id="replica-db")


# ---------------------------------------------------------------- resume (§2.6, P3)

def _halted_store(tmp_path, reason):
    path = tmp_path / "halted" / f"{DB}.db"
    from memora import schema

    conn = LocalSQLiteBackend(path).connect()
    try:
        schema.ensure_schema(conn)
        schema.install_sync(conn, URI, 0)
        conn.execute("UPDATE sync_state SET halted_reason = ?, halted_at = 'x' WHERE id = 1", (reason,))
        conn.commit()
    finally:
        conn.close()
    return path


def test_resume_accepts_d1s_current_epoch_only(replica, tmp_path):
    store = _halted_store(tmp_path, "foreign_writer: expected 0 got 3")
    reader = lp.D1Reader(replica.reader())
    now = reader.epoch()
    with pytest.raises(lp.L5Refused, match="is not D1's epoch now"):
        lp.resume_store(store, reader, accept_d1_epoch=now + 1)
    rep = lp.resume_store(store, reader, accept_d1_epoch=now)
    assert rep["cleared"].startswith("foreign_writer")
    assert _rows(store, "SELECT halted_reason, d1_epoch_expected FROM sync_state") == [(None, now)]


def test_resume_a_delete_guard_halt_for_that_attempt_only(replica, tmp_path):
    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    reader = lp.D1Reader(replica.reader())
    with pytest.raises(lp.L5Refused, match="allow_deletes=abc123"):
        lp.resume_store(store, reader, allow_deletes="other")
    lp.resume_store(store, reader, allow_deletes="abc123")
    assert _rows(store, "SELECT halted_reason, allow_deletes_attempt FROM sync_state") == [(None, "abc123")]
    with pytest.raises(lp.L5Refused, match="not halted"):
        lp.resume_store(store, reader, allow_deletes="abc123")


def test_resume_refuses_while_memora_all_holds_the_store(replica, tmp_path):
    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, time\n"
         f"fd = os.open({str(store) + '.primary-lock'!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(lp.L5Refused, match="stop memora-all first"):
            lp.resume_store(store, lp.D1Reader(replica.reader()), allow_deletes="abc123")
        assert _rows(store, "SELECT halted_reason FROM sync_state")[0][0].startswith("delete_guard")
    finally:
        holder.kill()
        holder.wait()


# ---------------------------------------------------------------- CLI, in subprocesses

def _cli(replica, *args, env_extra=None):
    env = {**os.environ, "L5_TEST_FAKE_D1": str(replica.path), "MEMORA_D1_READ_TOKEN": "read-token"}
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(HARNESS), *args], capture_output=True, text=True,
                       timeout=120, env=env, cwd=str(REPO))
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    return r.returncode, out, r.stderr


def test_cli_restore_from_r2_prepare_then_apply(sc):
    srv = FreezeServer(already_frozen=True)
    try:
        tokens = token_args(sc.tmp)
        cred = sc.tmp / "operator.tok"
        cred.write_text("operator-edit-token")
        cred.chmod(0o600)
        common = [DB, "--account", "acct", "--database-id", "replica-db", "--memora-url", srv.url, *tokens,
                  "--r2-dir", str(sc.tmp / "r2"), "--out-dir", str(sc.tmp / "exports"),
                  "--from-r2", sc.key, "--receipt", str(sc.receipt)]
        code, out, err = _cli(sc.replica, "restore", *common)
        assert code == 0 and out["groups"] == 4, err
        approve = sc.approve(out["conflicts"], SELECT)
        apply_args = [*common, "--conflicts", out["conflicts"], "--approve", str(approve), "--out", str(sc.store),
                      "--credential-file", str(cred)]
        code, dry, err = _cli(sc.replica, "restore", *apply_args, "--dry-run")
        assert code == 0 and dry["dry_run"] is True, err
        code, out, err = _cli(sc.replica, "restore", *apply_args, env_extra={"L5_TEST_D1_WRITES": "apply"})
        assert code == 0 and out["applied"] == ["memory:2", "memory:3", "meta:other"], err
        assert _rows(sc.replica.path, "SELECT content FROM memories WHERE id = 2") == [("memory 2 it's quoted",)]
        assert srv.state == "frozen" and ("DELETE", f"/admin/freeze/{DB}") not in srv.methods()
    finally:
        srv.close()


def test_cli_reconcile_show_and_accept(replica, tmp_path, admin):
    srv, _client, tokens = admin
    receipt = _export(replica, tmp_path)
    base = ["reconcile", DB, "--account", "acct", "--database-id", "replica-db", "--memora-url", srv.url, *tokens]
    code, out, err = _cli(replica, *base)
    assert code == 0 and out["open_intents"][0]["id"] == 7, err
    code, out, err = _cli(replica, *base, "--accept", "7", "--receipt", str(receipt), "--operator", "spok",
                          "--decision", "applied", "--evidence-sha256", "e" * 64)
    assert code == 0 and srv.reconcile_bodies[0][1]["decision"] == "applied", err
    code, out, _ = _cli(replica, *base, "--accept", "7", "--operator", "spok")
    assert code == 2 and "--receipt" in out["refused"]


def test_cli_resume(replica, tmp_path):
    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    code, out, err = _cli(replica, "resume", DB, "--store", str(store), "--account", "acct",
                          "--database-id", "replica-db", "--allow-deletes", "abc123")
    assert code == 0 and out["cleared"].startswith("delete_guard"), err


def test_a_group_no_allowed_statement_can_restore_is_refused_before_any_write(sc):
    """A BLOB value cannot be sent as a D1 parameter: the apply refuses
    before writing anything instead of failing midway."""
    c = sc.prepare()["conflicts"]
    doc = json.loads(Path(c).read_text())
    g2 = [g for g in doc["groups"] if g["group"] == "memory:2"][0]
    [r for r in g2["snapshot_rows"] if r["table"] == "memories"][0]["row"]["content"] = {"$hex": "00ff"}
    Path(c).write_text(json.dumps(doc))
    send = Recorder(sc.replica)
    with pytest.raises(lp.L5Refused, match="group memory:2: no allowed statement can restore it"):
        _apply(sc, c, sc.approve(c, SELECT), send)
    assert send.sent == []


def test_moving_aside_twice_in_one_second_keeps_both(tmp_path):
    out = tmp_path / "memora-v2.db"
    for n in range(2):
        out.write_bytes(f"store {n}".encode())
        lp._move_aside(out, lambda: 0.0)
    kept = sorted(p.name for p in tmp_path.iterdir())
    assert kept == ["memora-v2.db.pre-restore-19700101T000000Z", "memora-v2.db.pre-restore-19700101T000000Z-2"]
    assert (tmp_path / kept[1] / "memora-v2.db").read_bytes() == b"store 1"


def test_a_conflicts_file_of_another_d1_database_is_refused(sc):
    c = sc.prepare()["conflicts"]
    doc = json.loads(Path(c).read_text())
    doc["database_id"] = "other-db"
    Path(c).write_text(json.dumps(doc))
    with pytest.raises(lp.L5Refused, match="is for another D1 database"):
        lp.load_approval(c, str(sc.approve(c, SELECT)), DB, sc.deps())


def test_apply_needs_the_freeze_and_writes_nothing_without_it(sc):
    c = sc.prepare()["conflicts"]
    send = Recorder(sc.replica)
    deps = sc.deps(send=send, barrier=FakeBarrier(frozen=False))
    with pytest.raises(lp.L5Refused, match="not frozen before the recheck"):
        lp.restore_apply(DB, c, str(sc.approve(c, SELECT)), str(sc.receipt), deps, sc.store, sc.tmp / "exports")
    assert send.sent == []


def test_an_action_without_a_memory_is_its_own_group(sc):
    replica_exec(sc.replica, "INSERT INTO memories_actions (memory_id, action, summary) VALUES (NULL, 'sweep', 's')")
    replica_exec(sc.replica, "INSERT INTO memories_actions (memory_id, action, summary) VALUES (2, 'edit', 's')")
    sc.receipt = _export(sc.replica, sc.tmp)
    doc = json.loads(Path(sc.prepare()["conflicts"]).read_text())
    groups = {g["group"]: g for g in doc["groups"]}
    assert "action:1" in groups and groups["action:1"]["keys"] == [{"table": "memories_actions", "pk": [1]}]
    assert {"table": "memories_actions", "pk": [2]} in groups["memory:2"]["keys"]


def test_cli_apply_refuses_a_conflicts_file_of_another_snapshot(sc):
    srv = FreezeServer(already_frozen=True)
    try:
        c = sc.prepare()["conflicts"]
        cred = sc.tmp / "operator.tok"
        cred.write_text("operator-edit-token")
        cred.chmod(0o600)
        code, out, _ = _cli(sc.replica, "restore", DB, "--account", "acct", "--database-id", "replica-db",
                            "--memora-url", srv.url, *token_args(sc.tmp), "--r2-dir", str(sc.tmp / "r2"),
                            "--out-dir", str(sc.tmp / "exports"), "--from-r2", f"{DB}/other.db.gz",
                            "--receipt", str(sc.receipt), "--conflicts", c, "--approve", str(sc.approve(c, SELECT)),
                            "--out", str(sc.store), "--credential-file", str(cred))
        assert code == 2 and "was prepared for" in out["refused"]
    finally:
        srv.close()
