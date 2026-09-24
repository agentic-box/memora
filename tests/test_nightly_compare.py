"""scripts/nightly_compare.sh (NC1): scheduled compares of the live local
primaries, run inside memora-all through a fake docker (tests/fake_nightly_docker.py)."""
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "nightly_compare.sh"
FAKE = REPO / "tests" / "fake_nightly_docker.py"


@pytest.fixture
def nc(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(FAKE, bin_dir / "docker")
    log = tmp_path / "calls.txt"
    logs = tmp_path / "logs"

    def run(*args, env=None):
        if log.exists():
            log.unlink()
        e = {k: v for k, v in os.environ.items() if k not in ("DEPLOY_HOST", "RUNTIME", "DEPLOY_CONTAINER")}
        e.update(PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(log), NC_LOG_DIR=str(logs), **(env or {}))
        proc = subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=60)
        raw = log.read_text() if log.exists() else ""
        calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
        return proc, calls

    run.logs = logs
    run.tmp = tmp_path
    return run


def _compares(calls):
    return [c for c in calls if c[2:5] == ["python", "/app/scripts/local_primary.py", "compare"]]


def _log_lines(nc):
    files = list(nc.logs.glob("compare-*.log"))
    assert len(files) == 1
    return files[0].read_text().splitlines()


class TestPlan:
    def test_dry_run_prints_the_plan_and_runs_nothing(self, nc):
        proc, calls = nc("--dry-run", "--mode", "nightly")
        assert proc.returncode == 0, proc.stderr
        assert "nightly_compare plan: mode nightly, container memora-all on localhost" in proc.stdout
        assert ("alpha: docker exec memora-all python /app/scripts/local_primary.py compare alpha --mode nightly "
                "--store /data/alpha.db --account acct-a --database-id db-a") in proc.stdout
        assert "dry run: nothing was run" in proc.stdout
        assert _compares(calls) == [] and not [c for c in calls if c[2:4] == ["sh", "-c"]]
        assert not nc.logs.exists()

    def test_sunday_default_is_the_barrier_with_a_brief_freeze(self, nc):
        proc, _ = nc("--dry-run", "--mode", "barrier")
        assert "--mode barrier" in proc.stdout and proc.stdout.count("--brief-freeze") == 2

    def test_a_bad_mode_is_refused(self, nc):
        proc, calls = nc("--mode", "weekly")
        assert proc.returncode == 2 and calls == []


class TestRun:
    def test_every_replicated_store_is_compared_in_the_container_and_logged(self, nc):
        proc, calls = nc("--mode", "nightly")
        assert proc.returncode == 0, proc.stderr
        cmp_calls = _compares(calls)
        assert [c[5] for c in cmp_calls] == ["alpha", "beta"]
        a = cmp_calls[0]
        assert a[:2] == ["exec", "memora-all"]
        assert a[a.index("--admin-token-file") + 1] == "/dev/shm/memora-nightly-compare/admin.token"
        assert a[a.index("--store") + 1] == "/data/alpha.db" and "--brief-freeze" not in a
        lines = _log_lines(nc)
        assert len(lines) == 2 and all(" mode=nightly result=clean exit=0 " in l for l in lines)
        assert ["exec", "memora-all", "rm", "-rf", "/dev/shm/memora-nightly-compare"] in calls

    def test_the_weekly_run_is_a_barrier_compare_with_a_brief_freeze(self, nc):
        proc, calls = nc("--mode", "barrier")
        assert proc.returncode == 0
        assert all(c[c.index("--mode") + 1] == "barrier" and c[-1] == "--brief-freeze" for c in _compares(calls))

    def test_tokens_reach_the_tool_as_container_side_files(self, nc):
        _, calls = nc("--mode", "nightly")
        setup = [c for c in calls if c[2:4] == ["sh", "-c"]]
        assert len(setup) == 1 and '"$MEMORA_ADMIN_TOKEN"' in setup[0][4] and "umask 077" in setup[0][4]

    @pytest.mark.parametrize("rc, result", [(5, "diff"), (6, "skipped"), (2, "refused"), (3, "halted"), (1, "error")])
    def test_a_non_clean_compare_fails_the_run_and_the_rest_still_run(self, nc, rc, result):
        proc, calls = nc("--mode", "nightly", env={"NC_RC": json.dumps({"alpha": rc})})
        assert proc.returncode == 1
        assert [c[5] for c in _compares(calls)] == ["alpha", "beta"]
        lines = _log_lines(nc)
        assert f" alpha mode=nightly result={result} exit={rc} " in lines[0]
        assert " beta mode=nightly result=clean exit=0 " in lines[1]

    def test_missing_vectors_alert(self, nc):
        out = {"alpha": {"ok": True, "clean": True, "diff_count": 0, "d1_missing_vectors": 4, "recorded": True}}
        proc, _ = nc("--mode", "nightly", env={"NC_OUT": json.dumps(out)})
        assert proc.returncode == 1 and "problems=missing_vectors=4" in _log_lines(nc)[0]

    def test_a_halted_replicator_alerts(self, nc):
        repl = {"beta": {"status": "halted", "halted_reason": "foreign_writer", "would_halt_count": 0}}
        proc, _ = nc("--mode", "nightly", env={"NC_REPL": json.dumps(repl)})
        assert proc.returncode == 1 and "replicator_halted='foreign_writer'" in _log_lines(nc)[1]

    def test_new_would_halt_events_alert_once(self, nc):
        repl = json.dumps({"alpha": {"status": "running", "would_halt_count": 2}})
        proc, _ = nc("--mode", "nightly", env={"NC_REPL": repl})
        assert proc.returncode == 1 and "would_halt_events=+2" in _log_lines(nc)[0]
        proc, _ = nc("--mode", "nightly", env={"NC_REPL": repl})  # the same count: no new events
        assert proc.returncode == 0
        assert (nc.logs / "would-halt-alpha.count").read_text() == "2"

    def test_logs_older_than_30_days_are_removed(self, nc):
        nc.logs.mkdir()
        old = nc.logs / "compare-2000-01-01.log"
        old.write_text("x\n")
        past = time.time() - 40 * 86400
        os.utime(old, (past, past))
        keep = nc.logs / "compare-2000-01-02.log"
        keep.write_text("y\n")
        nc("--mode", "nightly")
        assert not old.exists() and keep.exists()

    def test_compares_never_overlap(self, nc):
        running = nc.tmp / "running"
        proc, calls = nc("--mode", "nightly", env={"NC_RUNNING": str(running), "NC_SLEEP": "0.2"})
        assert proc.returncode == 0 and "OVERLAP" not in proc.stdout

    def test_a_run_while_another_holds_the_lock_logs_and_exits_0(self, nc):
        import fcntl

        nc.logs.mkdir()
        fd = os.open(nc.logs / "nightly_compare.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another run holds it
        try:
            proc, calls = nc("--mode", "nightly")
        finally:
            os.close(fd)
        assert proc.returncode == 0 and _compares(calls) == []
        assert "result=skipped-locked" in _log_lines(nc)[0]

    def test_concurrent_runs_one_runs_the_other_logs_and_exits_0(self, nc):
        """Reviews 8111/8122: two simultaneous starts can never both run."""
        e = {k: v for k, v in os.environ.items() if k not in ("DEPLOY_HOST", "RUNTIME", "DEPLOY_CONTAINER")}
        e.update(PATH=f"{nc.tmp / 'bin'}:{os.environ['PATH']}", CALL_LOG=str(nc.tmp / "calls.txt"),
                 NC_LOG_DIR=str(nc.logs), NC_SLEEP="1", NC_RUNNING=str(nc.tmp / "running"))
        procs = [subprocess.Popen(["bash", str(SCRIPT), "--mode", "nightly"], env=e, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        assert [p.returncode for p in procs] == [0, 0], outs
        lines = _log_lines(nc)
        assert sum("result=skipped-locked" in l for l in lines) == 1
        assert sum(" result=clean " in l for l in lines) == 2  # one run, both stores
        assert not any("OVERLAP" in o for o, _ in outs)
        raw = (nc.tmp / "calls.txt").read_text()
        assert raw.count("local_primary.py\x1fcompare") == 2

    def test_a_killed_run_leaves_no_lock_behind(self, nc):
        """SIGKILL mid-run: the kernel drops the lock; the next run proceeds."""
        e = {k: v for k, v in os.environ.items() if k not in ("DEPLOY_HOST", "RUNTIME", "DEPLOY_CONTAINER")}
        e.update(PATH=f"{nc.tmp / 'bin'}:{os.environ['PATH']}", CALL_LOG=str(nc.tmp / "calls.txt"),
                 NC_LOG_DIR=str(nc.logs), NC_SLEEP="30")
        victim = subprocess.Popen(["bash", str(SCRIPT), "--mode", "nightly"], env=e, start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline and "local_primary.py" not in ((nc.tmp / "calls.txt").read_text()
                                                                   if (nc.tmp / "calls.txt").exists() else ""):
            time.sleep(0.1)
        import signal

        os.killpg(victim.pid, signal.SIGKILL)  # the run and its children, mid-compare
        victim.wait()
        time.sleep(0.2)
        proc, calls = nc("--mode", "nightly")
        assert proc.returncode == 0, proc.stderr
        assert [c[5] for c in _compares(calls)] == ["alpha", "beta"]
        assert not any("skipped-locked" in l for l in _log_lines(nc))

    def test_the_lock_is_released_after_a_run(self, nc):
        import fcntl

        nc("--mode", "nightly")
        fd = os.open(nc.logs / "nightly_compare.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again
        finally:
            os.close(fd)

    def test_no_replicated_store_is_a_clean_no_op(self, nc):
        proc, calls = nc("--mode", "nightly", env={"NC_STORES": "{}"})
        assert proc.returncode == 0 and _compares(calls) == []
        assert "no store in MEMORA_REPLICAS" in _log_lines(nc)[0]

    def test_an_unusable_store_config_is_refused(self, nc):
        bad = {"alpha": {"uri": "not-d1", "store": "/data/alpha.db"}}
        proc, calls = nc("--mode", "nightly", env={"NC_STORES": json.dumps(bad)})
        assert proc.returncode == 1 and "cannot use uri" in proc.stderr and _compares(calls) == []

    def test_runtime_and_container_come_from_the_environment(self, nc):
        bin_dir = nc.tmp / "bin"
        os.symlink(FAKE, bin_dir / "podman")
        proc, calls = nc("--mode", "nightly", env={"RUNTIME": "podman", "DEPLOY_CONTAINER": "memora-x"})
        assert proc.returncode == 0 and all(c[1] == "memora-x" for c in calls if c[0] == "exec")

    def test_no_infra_identifier_in_the_script(self):
        text = SCRIPT.read_text()
        for word in ("nuc8", "100.104", "bestation", "ob1", "cloudflare-strategic"):
            assert word not in text, word
