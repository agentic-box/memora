"""X3: --lock-barrier -- the store's primary lock as the barrier, held for
the whole run (inside scripts/lp_container.sh there is no docker, so
--service-stopped cannot work). memora-all holds that lock while it serves
the store, so acquiring it proves memora-all is not serving it."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memora import backends
from memora import local_primary as lp
from tests.test_l5_export import DB
from tests.test_l5_restore import SELECT, _cli, _halted_store, replica, sc  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parent.parent


def _routes(store, db=DB):
    """memora-all's MEMORA_DATABASES as lp_container.sh passes it."""
    import json

    return {"MEMORA_DATABASES": json.dumps({db: str(store)})}


class Holder:
    """Another process holding the store's primary lock (as memora-all does)."""

    def __init__(self, store: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, sys.argv[2]); from memora import backends; "
             "backends.acquire_primary_lock(sys.argv[1]); print('held', flush=True); time.sleep(120)",
             str(store), str(REPO)], stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "held"

    def stop(self):
        self.proc.kill()
        self.proc.wait()


def _can_take(store: Path) -> bool:
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, sys.argv[2]); from memora import backends\n"
                        "try:\n backends.acquire_primary_lock(sys.argv[1]); print('took')\n"
                        "except backends.StoreLockedError: print('held')",
                        str(store), str(REPO)], capture_output=True, text=True, timeout=60)
    return r.stdout.strip() == "took"


def _no_docker(tmp_path):
    """A `docker` on PATH that records any call: the lock barrier never uses it."""
    bin_dir = tmp_path / "bin-nodocker"
    bin_dir.mkdir(exist_ok=True)
    marker = tmp_path / "docker-called"
    (bin_dir / "docker").write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\nexit 99\n")
    (bin_dir / "docker").chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}, marker


# ------------------------------------------------------------------ the barrier

def test_the_barrier_is_refused_while_another_process_holds_the_lock(tmp_path):
    store = tmp_path / "s.db"
    store.write_bytes(b"")
    h = Holder(store)
    try:
        b = lp.LockBarrier(store)
        with pytest.raises(lp.L5Refused, match=r"--lock-barrier at the start: .*held by another process"):
            b.freeze()
        assert not b.placed
    finally:
        h.stop()


def test_the_barrier_holds_the_lock_for_the_whole_run(tmp_path):
    store = tmp_path / "s.db"
    store.write_bytes(b"")
    b = lp.LockBarrier(store)
    b.check("at the start")
    try:
        # a step inside takes and releases the same lock (restore, resume):
        backends.acquire_primary_lock(store)
        backends.release_primary_lock(store)
        b.check("after the step")
        assert not _can_take(store), "the step's release did not open a window"
    finally:
        b.release()
    assert _can_take(store), "released when the run ends"


def test_a_replaced_or_removed_lock_file_is_detected_at_the_next_boundary(tmp_path):
    store = tmp_path / "s.db"
    store.write_bytes(b"")
    lock = backends.primary_lock_path(store)
    b = lp.LockBarrier(store)
    b.check("at the start")
    try:
        os.replace(lock, str(lock) + ".old")
        lock.write_text("")
        with pytest.raises(lp.L5Refused, match="--lock-barrier lost before group 1: .*replaced"):
            b.check("before group 1")
        os.remove(lock)
        with pytest.raises(lp.L5Refused, match="was removed"):
            b.check("before group 2")
    finally:
        b.release()


def test_every_boundary_rechecks(tmp_path, monkeypatch):
    store = tmp_path / "s.db"
    store.write_bytes(b"")
    b = lp.LockBarrier(store)
    seen = []
    monkeypatch.setattr(backends, "primary_lock_problem", lambda p: seen.append(str(p)) or None)
    b.freeze()
    b.require("x")
    b.check("y")
    b.release()
    assert len(seen) == 3


# ------------------------------------------------------------------ the CLI

@pytest.mark.parametrize("cmd", ["restore", "resume", "sequence-highwater", "rollback"])
def test_a_held_lock_refuses_before_any_d1_call(sc, cmd):
    store = sc.store
    store.parent.mkdir(parents=True, exist_ok=True)
    if not store.exists():
        store.write_bytes(b"")
    calls = sc.tmp / f"d1-calls-{cmd}"
    env, docker_marker = _no_docker(sc.tmp)
    env["L5_TEST_D1_CALLS"] = str(calls)
    env.update(_routes(store))
    base = [DB, "--account", "acct", "--database-id", "replica-db", "--lock-barrier"]
    common = [*base, "--r2-dir", str(sc.tmp / "r2"), "--out-dir", str(sc.tmp / "exports")]
    args = {
        "restore": ["restore", *common, "--from-r2", sc.key, "--receipt", str(sc.receipt), "--conflicts", "c.json",
                    "--approve", "a.json", "--out", str(store)],
        "resume": ["resume", DB, "--store", str(store), "--account", "acct", "--database-id", "replica-db",
                   "--lock-barrier", "--accept-d1-epoch", "0"],
        "sequence-highwater": ["sequence-highwater", *common, "--receipt", str(sc.receipt), "--local", str(store)],
        "rollback": ["rollback", *common, "--phase", "verify", "--store", str(store),
                     "--admin-token-file", "x", "--health-token-file", "y"],
    }[cmd]
    h = Holder(store)
    try:
        code, out, err = _cli(sc.replica, *args, env_extra=env)
    finally:
        h.stop()
    assert code == 2 and "--lock-barrier at the start, before any D1 call" in out["refused"], (out, err)
    assert not calls.exists() or calls.read_text() == "", "a D1 call was made before the barrier"
    assert not docker_marker.exists()


def test_restore_apply_runs_under_the_lock_barrier_without_docker(sc):
    env, docker_marker = _no_docker(sc.tmp)
    cred = sc.tmp / "operator.tok"
    cred.write_text("operator-edit-token")
    cred.chmod(0o600)
    c = sc.prepare()["conflicts"]
    args = [DB, "--account", "acct", "--database-id", "replica-db", "--lock-barrier", "--r2-dir",
            str(sc.tmp / "r2"), "--out-dir", str(sc.tmp / "exports"), "--from-r2", sc.key,
            "--receipt", str(sc.receipt), "--conflicts", c, "--approve", str(sc.approve(c, SELECT)),
            "--out", str(sc.store), "--credential-file", str(cred)]
    code, out, err = _cli(sc.replica, "restore", *args,
                          env_extra={**env, **_routes(sc.store), "L5_TEST_D1_WRITES": "apply"})
    assert code == 0 and out["applied"] == ["memory:2", "memory:3", "meta:other"], err
    assert not docker_marker.exists(), "the lock barrier never calls docker"
    assert _can_take(sc.store), "released when the run ended"


def test_resume_runs_under_the_lock_barrier(replica, tmp_path):
    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    code, out, err = _cli(replica, "resume", DB, "--store", str(store), "--account", "acct",
                          "--database-id", "replica-db", "--allow-deletes", "abc123", "--lock-barrier",
                          env_extra=_routes(store))
    assert code == 0 and out["cleared"].startswith("delete_guard") and out["lock_barrier"] is True, err
    assert _can_take(store)


@pytest.mark.parametrize("extra, needle", [
    (["rollback", "--phase", "drain"], "the drain runs while memora-all serves the store"),
    (["export"], "--lock-barrier applies to"),
    (["restore", "--service-stopped"], "two barriers"),
])
def test_the_lock_barrier_is_refused_where_it_does_not_apply(sc, extra, needle):
    cmd, rest = extra[0], extra[1:]
    args = [cmd, DB, "--account", "acct", "--database-id", "replica-db", "--lock-barrier",
            "--r2-dir", str(sc.tmp / "r2"), "--out-dir", str(sc.tmp / "exports"), *rest]
    if cmd == "rollback":
        args += ["--store", str(sc.store), "--admin-token-file", "x", "--health-token-file", "y"]
    if cmd == "restore":
        args += ["--receipt", str(sc.receipt), "--out", str(sc.store)]
    code, out, err = _cli(sc.replica, *args)
    assert code == 2 and needle in out["refused"], (out, err)


# ------------------------------------------------------------------ boundaries inside the steps

class _RecordingBarrier(lp.LockBarrier):
    def __init__(self, store, fail_at=None):
        super().__init__(store)
        self.seen, self.fail_at = [], fail_at

    def check(self, where):
        self.seen.append(where)
        if where == self.fail_at:
            raise lp.L5Refused(f"--lock-barrier lost {where}: test")


def test_resume_rechecks_before_reading_d1_and_before_clearing(replica, tmp_path):
    store = _halted_store(tmp_path, "foreign_writer: expected 0 got 3")
    reads = []
    reader = lp.D1Reader(replica.reader())
    real = reader.epoch
    reader.epoch = lambda: (reads.append("epoch"), real())[1]
    b = _RecordingBarrier(store, fail_at="before reading D1's epoch")
    with pytest.raises(lp.L5Refused, match="before reading D1's epoch"):
        lp.resume_store(store, reader, accept_d1_epoch=0, barrier=b)
    assert reads == [], "no D1 read after a lost barrier"
    b = _RecordingBarrier(store, fail_at="before clearing the halt")
    with pytest.raises(lp.L5Refused, match="before clearing the halt"):
        lp.resume_store(store, reader, accept_d1_epoch=real(), barrier=b)
    import sqlite3

    db = sqlite3.connect(store)
    assert db.execute("SELECT halted_reason FROM sync_state").fetchone()[0].startswith("foreign_writer")
    db.close()


def test_rollback_finish_rechecks_the_lock_at_every_boundary(tmp_path):
    from memora import rollback as rb

    class Admin:
        calls = []

        def check(self, where):
            self.calls.append(where)

    b = _RecordingBarrier(tmp_path / "s.db", fail_at="before the finish")
    deps = rb.RollbackDeps(db=DB, store=tmp_path / "s.db", account_id="a", database_id="d", reader=None,
                           admin=Admin(), stopped=b, r2=None, out_dir=tmp_path, d1_audit_conn=lambda: None)
    with pytest.raises(lp.L5Refused, match="before the finish"):
        rb._finish(deps, {"phases": {}})
    assert Admin.calls == [], "refused before touching memora-all"


def test_main_releases_the_lock_barrier_when_it_returns(tmp_path, monkeypatch):
    """In-process: after main() returns (success or refusal) this process no
    longer holds the store's primary lock -- a later run in the same process,
    or memora-all, can take it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("lp_cli_x3", REPO / "scripts" / "local_primary.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("MEMORA_D1_READ_TOKEN", "read-token")
    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    for k, v in _routes(store).items():
        monkeypatch.setenv(k, v)
    args = ["resume", DB, "--store", str(store), "--account", "acct", "--database-id", "replica-db",
            "--lock-barrier"]
    assert cli.main([*args, "--allow-deletes", "abc123"]) == 0
    assert backends.primary_lock_problem(store) is not None, "released after a successful run"
    assert cli.main([*args, "--allow-deletes", "wrong"]) == 2
    assert backends.primary_lock_problem(store) is not None, "released after a refusal"
    assert _can_take(store)



# ------------------------------------------------------------------ review 7778 P1-1: memora-all's routing

@pytest.mark.parametrize("routes, needle", [
    (None, "needs memora-all's MEMORA_DATABASES"),
    ({DB: "d1://acct/replica-db"}, "serves 'l5' from d1://"),
    ({DB: "s3://bucket/x.db"}, "from s3://"),
    ({DB: "/data/elsewhere.db"}, "routes 'l5' to /data/elsewhere.db"),
    ({"another": "/x.db"}, "does not route 'l5'"),
    ("{not json", "MEMORA_DATABASES is unusable"),
])
def test_a_stopped_required_run_refuses_unless_memora_all_routes_the_store_to_that_file(
        replica, tmp_path, routes, needle):
    import json

    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    calls = tmp_path / "d1-calls"
    env = {"L5_TEST_D1_CALLS": str(calls), "MEMORA_DATABASES": ""}
    if routes is not None:
        env["MEMORA_DATABASES"] = routes if isinstance(routes, str) else json.dumps(routes)
    code, out, err = _cli(replica, "resume", DB, "--store", str(store), "--account", "acct",
                          "--database-id", "replica-db", "--allow-deletes", "abc123", "--lock-barrier",
                          env_extra=env)
    assert code == 2 and needle.replace("'l5'", repr(DB)) in out["refused"], (out, err)
    assert not calls.exists() or calls.read_text() == ""
    assert _can_take(store), "the lock was never taken"


def test_a_file_uri_route_to_the_same_file_is_accepted(replica, tmp_path):
    import json

    store = _halted_store(tmp_path, "delete_guard: memories 60/100 attempt=abc123")
    code, out, err = _cli(replica, "resume", DB, "--store", str(store), "--account", "acct",
                          "--database-id", "replica-db", "--allow-deletes", "abc123", "--lock-barrier",
                          env_extra={"MEMORA_DATABASES": json.dumps({DB: f"file://{store}"})})
    assert code == 0, (out, err)


def test_rollback_finish_needs_no_local_route(sc):
    """finish runs with memora-all up and serving D1 (its routing is d1:// by
    then); it proves the D1 identity through /admin/data-volume instead."""
    import json

    args = ["rollback", DB, "--account", "acct", "--database-id", "replica-db", "--lock-barrier",
            "--r2-dir", str(sc.tmp / "r2"), "--out-dir", str(sc.tmp / "exports"), "--phase", "finish",
            "--store", str(sc.store), "--admin-token-file", str(sc.tmp / "no-admin"),
            "--health-token-file", str(sc.tmp / "no-health")]
    code, out, err = _cli(sc.replica, *args, env_extra={"MEMORA_DATABASES": json.dumps({DB: "d1://acct/replica-db"})})
    assert code == 2 and "MEMORA_DATABASES" not in out["refused"] and "memora-all serves" not in out["refused"], out
