"""scripts/cutover_store.sh (REL1): one store's local-primary cutover.

The script runs for real with ssh, scp and docker replaced by fakes
(tests/fake_cutover_docker.py stands in for memora-all and the operator tool
inside it) and the deploy replaced by a logging stub. Nothing touches nuc8,
D1 or a runtime.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "cutover_store.sh")
FAKE_DOCKER = os.path.join(REPO, "tests", "fake_cutover_docker.py")
REGISTRY = {"memora": "d1://acct/db1", "ob1": "d1://acct/db2", "re": "d1://acct/db3"}
ENV_TEXT = f"# instance config\nMEMORA_DATABASES='{json.dumps(REGISTRY)}'\nOTHER=kept\n"
EDITED = {**REGISTRY, "re": "/data/re.db"}
ROLLBACK = "rollback: docs/cutover-runbook.md"


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def cut(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "instances").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "cutover_store.sh")
    env_file = repo / "instances" / "all.env"
    env_file.write_text(ENV_TEXT)
    os.chmod(env_file, 0o600)
    tools = tmp_path / "tools.txt"
    _exe(repo / "scripts" / "deploy-memora-all.sh",
         f'#!/bin/bash\necho "deploy $*" >> "{tools}"\nexit "${{DEPLOY_RC:-0}}"\n')

    home = tmp_path / "home"
    exports = home / "memora-lp" / "exports" / "re"
    exports.mkdir(parents=True)
    for stamp in ("20260920T000000Z", "20260923T000000Z"):  # the newest one is used
        (exports / f"{stamp}.sql").write_text("-- sql\n")
        (exports / f"{stamp}.receipt.json").write_text(json.dumps({"sql_path": f"/elsewhere/{stamp}.sql"}))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir / "ssh", f'#!/bin/bash\necho "ssh $1" >> "{tools}"\nshift\nexec bash -c "$*"\n')
    _exe(bin_dir / "scp", f'#!/bin/bash\necho "scp $*" >> "{tools}"\nshift\ncp "${{1#*:}}" "$2"\n')
    os.symlink(FAKE_DOCKER, bin_dir / "docker")
    log = tmp_path / "calls.txt"
    state = tmp_path / "state.json"

    def run(*args, env=None):
        if log.exists():
            log.unlink()
        e = dict(os.environ, HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(log),
                 STATE=str(state), CUTOVER_HEALTH_TRIES="1", CUTOVER_OFFHOST_DIR=str(tmp_path / "offhost"),
                 **(env or {}))
        proc = subprocess.run(["bash", str(repo / "scripts" / "cutover_store.sh"), *args],
                              env=e, capture_output=True, text=True, timeout=60)
        raw = log.read_text() if log.exists() else ""
        calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
        return proc, calls

    run.env_file = env_file
    run.tools = tools
    run.state = state
    run.exports = exports
    run.offhost = tmp_path / "offhost"
    run.repo = repo
    return run


def _tool_cmds(calls):
    return [c[4] for c in calls if c[:4] == ["exec", "memora-all", "python", "/app/scripts/local_primary.py"]]


def _deployed(cut):
    return cut.tools.exists() and any(t.startswith("deploy") for t in cut.tools.read_text().splitlines())


def _edit_env(cut):
    cut.env_file.write_text(f"MEMORA_DATABASES='{json.dumps(EDITED)}'\n"
                            f"MEMORA_REPLICAS='{json.dumps({'re': 'd1://acct/db3'})}'\nMEMORA_REPLICATION=write\n"
                            "MEMORA_REPLICATION_INTERVAL_S=60\n")


def _frozen(cut):
    cut.state.write_text(json.dumps({"freeze": "frozen", "compare": None}))


# ---------------------------------------------------------------- the dry-run plan

class TestPlan:
    def test_default_is_a_dry_run_that_touches_nothing(self, cut):
        proc, calls = cut("re")
        assert proc.returncode == 0, proc.stderr
        assert "dry run: nothing was done" in proc.stdout
        assert calls == [] and not cut.tools.exists(), "no ssh, docker or deploy"
        assert cut.env_file.read_text() == ENV_TEXT

    def test_the_plan_names_every_step_and_the_exact_edits(self, cut):
        proc, _ = cut("re", "--dry-run")
        out = proc.stdout
        for step in "abcdefgh":
            assert f"\n  {step}  " in out, step
        assert "cutover plan for store re (D1 d1://acct/db3) on nuc8:memora-all, from step a" in out
        assert f"    MEMORA_DATABASES='{json.dumps(EDITED)}'" in out
        assert """    MEMORA_REPLICAS='{"re": "d1://acct/db3"}'""" in out
        assert "    MEMORA_REPLICATION=write" in out
        assert "    MEMORA_REPLICATION_INTERVAL_S=60" in out  # the re pilot (leader 7763)
        assert "interval_s 60" in out and "within 150 s" in out and "--drain-timeout 600" in out
        assert "sqlite:" not in out  # the registry takes a path, not a sqlite:// URI
        assert "python /app/scripts/local_primary.py seed re" in out
        assert "the tool runs INSIDE memora-all" in out
        assert "--out /data/re.db" in out and "--mode barrier" in out
        assert "only with --apply-env" in out and "only with --thaw" in out

    def test_the_replicas_map_is_merged(self, cut):
        cut.env_file.write_text(ENV_TEXT.replace("OTHER=kept", "MEMORA_REPLICAS='{\"x\": \"d1://a/b\"}'"))
        proc, _ = cut("re")
        assert """MEMORA_REPLICAS='{"x": "d1://a/b", "re": "d1://acct/db3"}'""" in proc.stdout

    def test_from_e_plans_only_the_later_steps(self, cut):
        _edit_env(cut)
        proc, _ = cut("re", "--from", "e")
        assert proc.returncode == 0, proc.stderr
        assert "\n  a  " not in proc.stdout and "\n  d  " not in proc.stdout and "\n  e  " in proc.stdout


class TestRefusals:
    @pytest.mark.parametrize("args, match", [
        (["nope"], "'nope' is not a store of MEMORA_DATABASES"),
        (["Re;rm"], "usage:"),
        ([], "usage:"),
        (["re", "--from", "b"], "--from takes a, d, e, f, g or h"),
        (["re", "--from", "c"], "--from takes a, d, e, f, g or h"),
        (["re", "--from", "e"], "--from e needs the env edits (step d) made"),
        (["re", "--from", "h", "--thaw", "--execute"], "--from h needs the env edits"),
    ])
    def test_refused_before_anything(self, cut, args, match):
        proc, calls = cut(*args)
        assert proc.returncode == 2 and match in proc.stderr, proc.stderr
        assert calls == [] and not cut.tools.exists()
        assert ROLLBACK in proc.stderr

    def test_a_store_already_local_is_not_cut_again(self, cut):
        _edit_env(cut)
        proc, calls = cut("re", "--execute")
        assert proc.returncode == 2 and "not d1://<account>/<database-id>" in proc.stderr
        assert calls == []

    def test_a_store_already_replicated_is_refused(self, cut):
        cut.env_file.write_text(ENV_TEXT + "MEMORA_REPLICAS='{\"re\": \"d1://acct/db3\"}'\n")
        proc, calls = cut("re", "--execute")
        assert proc.returncode == 2 and "MEMORA_REPLICAS already names 're'" in proc.stderr

    def test_from_e_without_write_mode_is_refused(self, cut):
        _edit_env(cut)
        cut.env_file.write_text(cut.env_file.read_text().replace("MEMORA_REPLICATION=write", "MEMORA_REPLICATION=log"))
        proc, calls = cut("re", "--execute", "--from", "e")
        assert proc.returncode == 2 and "needs MEMORA_REPLICATION=write" in proc.stderr and calls == []


# ---------------------------------------------------------------- a..d

class TestFreezeSeed:
    def test_a_to_c_then_stops_before_the_env_edits(self, cut):
        proc, calls = cut("re", "--execute")
        assert proc.returncode == 0, proc.stderr
        assert _tool_cmds(calls) == ["freeze", "recheck", "seed", "fk-audit"]
        recheck = next(c for c in calls if c[4:5] == ["recheck"])
        assert recheck[recheck.index("--receipt") + 1] == "/data/exports/re/20260923T000000Z.receipt.json"
        cps = [c for c in calls if c[0] == "cp"]
        assert [os.path.basename(c[1]) for c in cps] == ["20260923T000000Z.receipt.json", "20260923T000000Z.sql"]
        assert all(c[2] == "memora-all:/data/exports/re/" for c in cps)
        seed = next(c for c in calls if c[4:5] == ["seed"])
        assert seed[seed.index("--out") + 1] == "/data/re.db"
        assert seed[seed.index("--receipt") + 1] == "/data/exports/re/20260923T000000Z.receipt.json"
        assert "next: " in proc.stdout and "--from d --apply-env" in proc.stdout
        assert cut.env_file.read_text() == ENV_TEXT and not _deployed(cut)
        assert "thaw" not in _tool_cmds(calls)

    def test_the_tool_gets_token_files_written_inside_the_container(self, cut):
        proc, calls = cut("re", "--execute")
        setup = [c for c in calls if c[2:4] == ["sh", "-c"]]
        assert len(setup) == 1
        prog = setup[0][4]
        # the container's own shell expands its env; no value is on any argv
        assert '"$MEMORA_ADMIN_TOKEN"' in prog and '"$MEMORA_HEALTH_TOKEN"' in prog and "umask 077" in prog
        freeze = next(c for c in calls if c[4:5] == ["freeze"])
        assert freeze[freeze.index("--admin-token-file") + 1] == "/dev/shm/memora-cutover/admin.token"
        assert ["exec", "memora-all", "rm", "-rf", "/dev/shm/memora-cutover"] in calls

    def test_an_old_receipt_takes_a_fresh_export_copied_off_host(self, cut):
        proc, calls = cut("re", "--execute", env={
            "TOOL_RC_RECHECK": "2",
            "TOOL_OUT_RECHECK": json.dumps({"ok": False, "refused": "receipt x is older than 24 h (90000 s)"})})
        assert proc.returncode == 0, proc.stderr
        assert _tool_cmds(calls) == ["freeze", "recheck", "export", "seed", "fk-audit"]
        seed = next(c for c in calls if c[4:5] == ["seed"])
        assert seed[seed.index("--receipt") + 1] == "/data/exports/re/20990101T000000Z.receipt.json"
        out = [c for c in calls if c[0] == "cp" and c[1].startswith("memora-all:")]
        assert [c[1] for c in out] == ["memora-all:/data/exports/re/20990101T000000Z.receipt.json",
                                       "memora-all:/data/exports/re/20990101T000000Z.sql"]
        assert "scp" in cut.tools.read_text()
        assert sorted(p.name for p in (cut.offhost / "re").iterdir()) == [
            "20990101T000000Z.receipt.json", "20990101T000000Z.sql"]
        assert (cut.exports / "20990101T000000Z.sql").exists()

    @pytest.mark.parametrize("knobs, step, match, done", [
        ({"TOOL_RC_FREEZE": "2"}, "a", "the freeze was refused", ["freeze"]),
        ({"FREEZE_STAYS_OPEN": "1"}, "a", "not frozen with 0 in flight", ["freeze"]),
        ({"TOOL_RC_RECHECK": "2"}, "b", "no usable receipt", ["freeze", "recheck"]),
        ({"TOOL_RC_SEED": "2"}, "c", "the seed failed", ["freeze", "recheck", "seed"]),
        ({"TOOL_RC_FK_AUDIT": "5"}, "c", "fk audit of /data/re.db is not clean", ["freeze", "recheck", "seed", "fk-audit"]),
    ])
    def test_a_failed_boundary_stops_with_the_rollback_pointer(self, cut, knobs, step, match, done):
        proc, calls = cut("re", "--execute", env=knobs)
        assert proc.returncode == 1
        assert f"cutover re STOPPED at step {step}: " in proc.stderr and match in proc.stderr, proc.stderr
        assert ROLLBACK in proc.stderr and "local_primary.py rollback re --phase drain|verify|finish" in proc.stderr
        assert _tool_cmds(calls) == done
        assert ["exec", "memora-all", "rm", "-rf", "/dev/shm/memora-cutover"] in calls  # removed on failure too
        assert cut.env_file.read_text() == ENV_TEXT and not _deployed(cut)

    def test_no_receipt_on_the_host_stops_at_b(self, cut):
        shutil.rmtree(cut.exports)
        cut.exports.mkdir()
        proc, calls = cut("re", "--execute")
        assert proc.returncode == 1 and "STOPPED at step b: no receipt" in proc.stderr
        assert _tool_cmds(calls) == ["freeze"]


class TestEnvEdits:
    def test_apply_env_backs_up_edits_and_redeploys(self, cut):
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "d", "--apply-env")
        assert proc.returncode == 0, proc.stderr
        backups = list(cut.env_file.parent.glob("all.env.bak-cutover-re-*"))
        assert len(backups) == 1 and backups[0].read_text() == ENV_TEXT
        assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(cut.env_file).st_mode) == 0o600
        text = cut.env_file.read_text()
        assert f"MEMORA_DATABASES='{json.dumps(EDITED)}'" in text
        assert """MEMORA_REPLICAS='{"re": "d1://acct/db3"}'""" in text
        assert "MEMORA_REPLICATION=write" in text and "OTHER=kept" in text and "# instance config" in text
        assert "MEMORA_REPLICATION_INTERVAL_S=60" in text
        assert _deployed(cut)
        assert _tool_cmds(calls) == ["compare"]
        cmp = next(c for c in calls if c[4:5] == ["compare"])
        assert cmp[cmp.index("--mode") + 1] == "barrier" and cmp[cmp.index("--store") + 1] == "/data/re.db"
        assert cmp[cmp.index("--drain-timeout") + 1] == "600"
        assert "stopped before the thaw" in proc.stdout and "--from h --thaw" in proc.stdout

    def test_without_apply_env_nothing_is_edited(self, cut):
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "d")
        assert proc.returncode == 0 and "--from d --apply-env" in proc.stdout
        assert cut.env_file.read_text() == ENV_TEXT and not _deployed(cut)
        assert not list(cut.env_file.parent.glob("all.env.bak-*"))

    def test_a_failed_redeploy_stops_at_e(self, cut):
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "d", "--apply-env", env={"DEPLOY_RC": "1"})
        assert proc.returncode == 1 and "STOPPED at step e" in proc.stderr and ROLLBACK in proc.stderr
        assert "compare" not in _tool_cmds(calls)


# ---------------------------------------------------------------- f..h

class TestAfterRedeploy:
    @pytest.mark.parametrize("knobs, match", [
        ({"REPL": json.dumps({"mode": "log", "status": "running", "lag_rows": 0})}, "replication mode 'log', not write"),
        ({"REPL": "null"}, "no replication block"),
        ({"REPL": json.dumps({"mode": "write", "status": "halted", "halted_reason": "x", "lag_rows": 0})},
         "replication halted"),
        ({"REPL": json.dumps({"mode": "write", "status": "running", "interval_s": 60.0, "lag_rows": 3,
                              "last_acked_seq": 4, "head_seq": 7})}, "last_acked_seq 4 has not reached head 7"),
        ({"FAKE_INTERVAL_S": "0"}, "replication interval_s 0.0, not the configured 60.0"),
        ({"REG_ENTRY": "d1://acct/db3"}, "not the local /data/re.db"),
        ({"ENV_REPLICATION": "log"}, "lacks MEMORA_REPLICATION=write"),
    ])
    def test_health_must_show_a_live_write_mode_store(self, cut, knobs, match):
        _edit_env(cut)
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "f", env=knobs)
        assert proc.returncode == 1 and "STOPPED at step f" in proc.stderr and match in proc.stderr, proc.stderr
        assert _tool_cmds(calls) == [] and ROLLBACK in proc.stderr

    def test_health_must_still_be_frozen(self, cut):
        _edit_env(cut)
        proc, calls = cut("re", "--execute", "--from", "f")
        assert proc.returncode == 1 and "not frozen" in proc.stderr

    @pytest.mark.parametrize("rc, match", [("5", "found differences"), ("6", "was skipped"), ("2", "failed")])
    def test_an_unclean_compare_stops_and_never_thaws(self, cut, rc, match):
        _edit_env(cut)
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "g", "--thaw", env={"TOOL_RC_COMPARE": rc})
        assert proc.returncode == 1 and "STOPPED at step g" in proc.stderr and match in proc.stderr
        assert _tool_cmds(calls) == ["compare"]

    def test_the_thaw_needs_the_flag(self, cut):
        _edit_env(cut)
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "g")
        assert proc.returncode == 0 and _tool_cmds(calls) == ["compare"]
        assert json.loads(cut.state.read_text())["freeze"] == "frozen"

    def test_the_thaw_needs_a_recorded_clean_barrier_compare(self, cut):
        _edit_env(cut)
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "h", "--thaw")  # no compare recorded
        assert proc.returncode == 1 and "STOPPED at step h" in proc.stderr
        assert "not a clean barrier compare" in proc.stderr and "thaw" not in _tool_cmds(calls)

    def test_the_thaw_refuses_rows_written_after_the_compare(self, cut):
        _edit_env(cut)
        _frozen(cut)
        repl = {"mode": "write", "status": "running", "lag_rows": 0, "head_seq": 9, "last_acked_seq": 9,
                "interval_s": 60.0, "compare_consumed_seq": 4,
                "last_compare_mode": "barrier", "last_compare_clean": True}
        proc, calls = cut("re", "--execute", "--from", "h", "--thaw", env={"REPL": json.dumps(repl)})
        assert proc.returncode == 1 and "rows were written after the compare" in proc.stderr
        assert "thaw" not in _tool_cmds(calls)

    def test_compare_then_thaw(self, cut):
        _edit_env(cut)
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "g", "--thaw")
        assert proc.returncode == 0, proc.stderr
        assert _tool_cmds(calls) == ["compare", "thaw"]
        assert "cutover of re done" in proc.stdout
        assert json.loads(cut.state.read_text())["freeze"] == "open"

    def test_the_whole_run_from_d(self, cut):
        _frozen(cut)
        proc, calls = cut("re", "--execute", "--from", "d", "--apply-env", "--thaw")
        assert proc.returncode == 0, proc.stderr
        assert _deployed(cut) and _tool_cmds(calls) == ["compare", "thaw"]


# ---------------------------------------------------------------- a receipt copied with its export

class TestReceiptBesideItsExport:
    """Step b copies a host receipt and its .sql into /data/exports/<db>/:
    the receipt's absolute sql_path is the host's, so the SQL is found
    beside the receipt; its sha256 still binds the content."""

    def _receipt(self, tmp_path, sql_text="-- rows\n"):
        import hashlib
        import time

        d = tmp_path / "moved"
        d.mkdir()
        (d / "s.sql").write_text(sql_text)
        sha = hashlib.sha256(b"-- rows\n").hexdigest()
        r = {"version": 1, "db": "re", "account_id": "acct", "database_id": "db3", "d1_uri": "d1://acct/db3",
             "verified_at": "now", "verified_at_epoch": time.time(), "sql_path": "/home/elsewhere/s.sql",
             "sql_sha256": sha, "r2_sha256": sha, "epoch": 1, "tables": {}}
        (d / "s.receipt.json").write_text(json.dumps(r))
        return d

    def test_found_beside_the_receipt(self, tmp_path):
        from memora.local_primary import load_receipt

        d = self._receipt(tmp_path)
        r = load_receipt(str(d / "s.receipt.json"), "re", account_id="acct", database_id="db3")
        assert r["sql_path"] == str(d / "s.sql")

    def test_a_changed_copy_is_still_refused(self, tmp_path):
        from memora.local_primary import L5Refused, load_receipt

        d = self._receipt(tmp_path, sql_text="-- other rows\n")
        with pytest.raises(L5Refused, match="missing or changed"):
            load_receipt(str(d / "s.receipt.json"), "re", account_id="acct", database_id="db3")

    def test_the_recorded_path_wins_when_it_exists(self, tmp_path):
        from memora.local_primary import load_receipt

        d = self._receipt(tmp_path)
        real = tmp_path / "real.sql"
        real.write_text("-- rows\n")
        r = json.loads((d / "s.receipt.json").read_text())
        r["sql_path"] = str(real)
        (d / "s.receipt.json").write_text(json.dumps(r))
        assert load_receipt(str(d / "s.receipt.json"), "re", account_id="acct", database_id="db3")["sql_path"] == str(real)


class TestInterval:
    def test_a_custom_interval_sets_the_edit_and_the_allowances(self, cut):
        proc, _ = cut("re", "--interval", "400")
        assert proc.returncode == 0, proc.stderr
        assert "    MEMORA_REPLICATION_INTERVAL_S=400" in proc.stdout
        assert "within 830 s" in proc.stdout and "--drain-timeout 830" in proc.stdout

    def test_zero_interval_sends_on_commit(self, cut):
        proc, _ = cut("re", "--interval", "0")
        assert "    MEMORA_REPLICATION_INTERVAL_S=0" in proc.stdout and "--drain-timeout 600" in proc.stdout

    @pytest.mark.parametrize("bad", ["-5", "soon", "", "1e3"])
    def test_a_bad_interval_is_refused(self, cut, bad):
        proc, calls = cut("re", "--interval", bad)
        assert proc.returncode == 2 and "--interval takes seconds" in proc.stderr and calls == []

    def test_after_the_edits_the_interval_comes_from_all_env(self, cut):
        _edit_env(cut)
        cut.env_file.write_text(cut.env_file.read_text().replace("INTERVAL_S=60", "INTERVAL_S=5"))
        proc, _ = cut("re", "--from", "f", "--interval", "999")
        assert "interval_s 5," in proc.stdout and "within 40 s" in proc.stdout

    def test_the_applied_interval_is_read_back(self, cut):
        _frozen(cut)
        proc, _ = cut("re", "--execute", "--from", "d", "--apply-env", "--interval", "30",
                      env={"FAKE_INTERVAL_S": "30"})
        assert proc.returncode == 0, proc.stderr
        assert "MEMORA_REPLICATION_INTERVAL_S=30" in cut.env_file.read_text()
