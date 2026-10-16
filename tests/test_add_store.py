"""Item 95: scripts/add_store.sh and scripts/add_store_config.py.

add_store.sh runs for real with ssh, scp and docker replaced by fakes
(tests/fake_cutover_docker.py stands in for memora-all and the operator tool
inside it) and the deploy replaced by a logging stub. Nothing touches
deploy-host, D1 or a runtime."""
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "add_store.sh")
CONFIG = os.path.join(REPO, "scripts", "add_store_config.py")
FAKE_DOCKER = os.path.join(REPO, "tests", "fake_cutover_docker.py")

ACCT = "acct00000001"
DBID = "newdb0000001"
URI = f"d1://{ACCT}/{DBID}"
REGISTRY = {"memora": "/data/memora.db", "alpha": "/data/alpha.db", "gamma": "/data/gamma.db", "beta": "/data/beta.db"}
REPLICAS = {"memora": "d1://acct00000001/id-memora-01", "alpha": "d1://acct00000001/id-alpha-001",
            "gamma": "d1://acct00000001/id-gamma-001", "beta": "d1://acct00000001/id-beta-0001"}
PROJECTS = {"memora": ["memora"], "alpha": ["alpha"]}
ALL_ENV = (f"# instance config\nOTHER=kept\nMEMORA_DATABASES='{json.dumps(REGISTRY)}'\n"
           f"MEMORA_REPLICAS='{json.dumps(REPLICAS)}'\nMEMORA_REPLICATION=write\n"
           "MEMORA_REPLICATION_INTERVAL_S=60\nTRAILER=also kept\n")
DEPLOY_ENV = ("DEPLOY_HOST=deploy-host\n# a comment\n"
              f"MEMORA_PROJECTS='{json.dumps(PROJECTS)}'\nDEPLOY_GRAPH_BIND=100.64.0.10\n")


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def add(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "instances").mkdir()
    for name in ("add_store.sh", "add_store_config.py", "deploy_config.py"):
        shutil.copy(os.path.join(REPO, "scripts", name), repo / "scripts" / name)
    env_file = repo / "instances" / "all.env"
    deploy_env = repo / "instances" / "deploy.env"
    env_file.write_text(ALL_ENV)
    deploy_env.write_text(DEPLOY_ENV + f"DEPLOY_BACKUP_DIR={tmp_path / 'backups'}\n")
    os.chmod(env_file, 0o600)
    os.chmod(deploy_env, 0o600)
    tools = tmp_path / "tools.txt"
    _exe(repo / "scripts" / "deploy-memora-all.sh",
         f'#!/bin/bash\necho "deploy $*" >> "{tools}"\nexit "${{DEPLOY_RC:-0}}"\n')
    home = tmp_path / "home"
    home.mkdir()
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
                 STATE=str(state), CUTOVER_OFFHOST_DIR=str(tmp_path / "offhost"), REPL="null", REG_ENTRY=URI)
        e.update(env or {})
        proc = subprocess.run(["bash", str(repo / "scripts" / "add_store.sh"), *args],
                              env=e, capture_output=True, text=True, timeout=60)
        raw = log.read_text() if log.exists() else ""
        calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
        return proc, calls

    run.repo, run.env_file, run.deploy_env, run.tools, run.state = repo, env_file, deploy_env, tools, state
    run.backups = tmp_path / "backups"
    run.offhost = tmp_path / "offhost"
    run.home = home
    return run


COMMON = ["acebar", "--d1-account", ACCT, "--d1-id", DBID, "--d1-name", "acebar-graph",
          "--credential-file", "/run/secrets/memora/edit.token"]


def _tool_cmds(calls):
    return [c[4] for c in calls if c[:4] == ["exec", "memora-all", "python", "/app/scripts/local_primary.py"]]


def _deployed(add):
    return add.tools.exists() and any(t.startswith("deploy") for t in add.tools.read_text().splitlines())


def _json(path, key):
    import re
    m = re.search(rf"^{key}='(.*)'$", path.read_text(), re.M)
    return json.loads(m.group(1))


# ---------------------------------------------------------------- the plan

class TestPlan:
    def test_default_is_a_dry_run_that_touches_nothing(self, add):
        proc, calls = add(*COMMON)
        assert proc.returncode == 0, proc.stderr
        assert "dry run: nothing was done" in proc.stdout
        assert calls == [] and not add.tools.exists(), "no ssh, docker or deploy"
        assert add.env_file.read_text() == ALL_ENV and not add.backups.exists()

    def test_the_plan_names_every_step_the_handoff_and_the_non_advertising_rule(self, add):
        proc, _ = add(*COMMON)
        out = proc.stdout
        for n in "12345":
            assert f"\n  {n}  " in out, n
        assert "bootstrap-d1 acebar --account acct00000001 --database-id newdb0000001 --d1-name acebar-graph" in out
        assert "pre-freeze acebar" in out
        assert f"MEMORA_DATABASES[acebar]={URI}" in out
        assert 'MEMORA_PROJECTS[acebar]=["acebar"]' in out
        assert "scripts/cutover_store.sh acebar --execute" in out
        assert "NOT advertised" in out

    def test_a_store_already_configured_is_refused_even_in_the_dry_run(self, add):
        proc, calls = add("gamma", *COMMON[1:])
        assert proc.returncode == 2 and "already configured" in proc.stderr
        assert calls == []

    def test_a_d1_id_that_another_store_uses_is_refused(self, add):
        proc, calls = add("acebar", "--d1-account", ACCT, "--d1-id", "id-gamma-001", "--d1-name", "x",
                          "--credential-file", "/c")
        assert proc.returncode == 2 and "already used by another store" in proc.stderr
        assert calls == []

    @pytest.mark.parametrize("args,msg", [
        (["acebar", "--d1-id", DBID, "--d1-name", "n", "--credential-file", "/c"], "--d1-account"),
        (["acebar", "--d1-account", ACCT, "--d1-id", DBID, "--d1-name", "n"], "--credential-file"),
        (["acebar", "--d1-account", "short", "--d1-id", DBID, "--d1-name", "n", "--credential-file", "/c"], "not Cloudflare ids"),
        (["Bad Name", "--d1-account", ACCT, "--d1-id", DBID, "--d1-name", "n", "--credential-file", "/c"], "usage"),
    ])
    def test_missing_or_bad_inputs_are_refused(self, add, args, msg):
        proc, calls = add(*args)
        assert proc.returncode == 2 and msg in proc.stderr
        assert calls == []

    def test_replication_must_already_be_write_mode(self, add):
        add.env_file.write_text(ALL_ENV.replace("MEMORA_REPLICATION=write", "MEMORA_REPLICATION=log"))
        proc, _ = add(*COMMON)
        assert proc.returncode == 2 and "MEMORA_REPLICATION must be 'write'" in proc.stderr


# ---------------------------------------------------------------- steps 1 and 2

class TestBootstrapAndPrefreeze:
    def test_execute_runs_bootstrap_then_prefreeze_and_stops_before_the_env_edits(self, add):
        proc, calls = add(*COMMON, "--execute")
        assert proc.returncode == 0, proc.stderr
        cmds = _tool_cmds(calls)
        assert cmds.index("bootstrap-d1") < cmds.index("pre-freeze")
        boot = next(c for c in calls if len(c) > 4 and c[4] == "bootstrap-d1" and "--d1-name" in c)
        assert boot[5:] == ["acebar", "--account", ACCT, "--database-id", DBID, "--d1-name", "acebar-graph",
                            "--credential-file", "/run/secrets/memora/edit.token"]
        assert "stopped before the env edits" in proc.stdout
        assert add.env_file.read_text() == ALL_ENV and not _deployed(add)

    def test_a_running_image_without_the_tools_is_refused_before_any_step(self, add):
        proc, calls = add(*COMMON, "--execute", env={"TOOL_RC_BOOTSTRAP_D1_HELP": "2"})
        assert proc.returncode == 2 and "predates Item 95" in proc.stderr
        assert "pre-freeze" not in _tool_cmds(calls) and not _deployed(add)
        assert not any(len(c) > 5 and c[4] == "bootstrap-d1" and c[5] == "acebar" for c in calls)

    def test_a_failed_bootstrap_stops_before_the_prefreeze(self, add):
        proc, calls = add(*COMMON, "--execute", env={"TOOL_RC_BOOTSTRAP_D1": "2"})
        assert proc.returncode == 1 and "STOPPED at step bootstrap" in proc.stderr
        assert "pre-freeze" not in _tool_cmds(calls) and not _deployed(add)

    def test_no_token_value_is_on_any_command_line(self, add):
        proc, calls = add(*COMMON, "--execute", "--from", "export", env={})
        for c in calls:
            for a in c:
                assert "Bearer" not in a and "token=" not in a.lower()


# ---------------------------------------------------------------- steps 3-5

class TestEnvDeployExport:
    def test_without_apply_env_it_stops_before_the_edits(self, add):
        proc, calls = add(*COMMON, "--execute", "--from", "env")
        assert proc.returncode == 0, proc.stderr
        assert "stopped before the env edits" in proc.stdout
        assert add.env_file.read_text() == ALL_ENV and not _deployed(add)

    def test_apply_env_edits_only_the_two_keys_and_everything_else_is_byte_identical(self, add, tmp_path):
        add.state.write_text(json.dumps({"freeze": "frozen", "compare": None}))   # pre-frozen earlier
        proc, calls = add(*COMMON, "--execute", "--from", "env", "--apply-env")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        registry = _json(add.env_file, "MEMORA_DATABASES")
        assert registry == {**REGISTRY, "acebar": URI}                       # the four unchanged, acebar added
        assert _json(add.env_file, "MEMORA_REPLICAS") == REPLICAS            # no replica entry yet (the cutover adds it)
        assert _json(add.deploy_env, "MEMORA_PROJECTS") == {**PROJECTS, "acebar": ["acebar"]}
        before = ALL_ENV.split("\n")
        after = add.env_file.read_text().split("\n")
        assert [l for l in before if not l.startswith("MEMORA_DATABASES=")] == \
               [l for l in after if not l.startswith("MEMORA_DATABASES=")]
        assert _deployed(add)
        assert "interim stage ok (frozen, 0 in flight, served from D1, no replication yet)" in proc.stdout

    def test_backups_are_dated_0600_and_outside_the_repo(self, add):
        add.state.write_text(json.dumps({"freeze": "frozen", "compare": None}))
        add(*COMMON, "--execute", "--from", "env", "--apply-env")
        names = sorted(p.name for p in add.backups.iterdir())
        assert len(names) == 2 and all("bak-addstore-acebar-" in n for n in names)
        for p in add.backups.iterdir():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
            assert not str(p).startswith(str(add.repo))
        assert not [p for p in add.repo.rglob("*bak*")]

    def test_a_backup_directory_inside_the_repo_is_refused(self, add):
        add.deploy_env.write_text(DEPLOY_ENV + f"DEPLOY_BACKUP_DIR={add.repo}/instances\n")
        add.state.write_text(json.dumps({"freeze": "frozen", "compare": None}))
        proc, _ = add(*COMMON, "--execute", "--from", "env", "--apply-env")
        assert proc.returncode != 0 and "inside the repo" in proc.stdout + proc.stderr
        assert add.env_file.read_text() == ALL_ENV and not _deployed(add)

    def test_the_first_export_is_copied_off_host_and_the_cutover_handoff_is_printed(self, add):
        add.state.write_text(json.dumps({"freeze": "frozen", "compare": None}))
        proc, calls = add(*COMMON, "--execute", "--from", "env", "--apply-env")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert "export" in _tool_cmds(calls)
        host_dir = add.home / "memora-lp" / "exports" / "acebar"
        assert sorted(p.suffix for p in host_dir.iterdir()) == [".json", ".sql"]
        assert sorted(p.suffix for p in (add.offhost / "acebar").iterdir()) == [".json", ".sql"]
        assert "scripts/cutover_store.sh acebar --execute --from h --thaw" in proc.stdout
        assert "Nothing may point at it yet" in proc.stdout

    def test_an_interim_store_that_is_not_frozen_stops_the_run_after_the_deploy(self, add):
        add.state.write_text(json.dumps({"freeze": "open", "compare": None}))
        proc, calls = add(*COMMON, "--execute", "--from", "env", "--apply-env")
        assert proc.returncode == 1 and "STOPPED at step deploy" in proc.stderr and "not frozen" in proc.stderr
        assert "export" not in _tool_cmds(calls)

    def test_an_interim_store_with_a_replication_block_is_refused(self, add):
        add.state.write_text(json.dumps({"freeze": "frozen", "compare": None}))
        proc, _ = add(*COMMON, "--execute", "--from", "env", "--apply-env",
                      env={"REPL": json.dumps({"mode": "write"})})
        assert proc.returncode == 1 and "replication block is present before the cutover" in proc.stderr

    def test_a_failed_deploy_names_the_rollback(self, add):
        proc, _ = add(*COMMON, "--execute", "--from", "env", "--apply-env", env={"DEPLOY_RC": "1"})
        assert proc.returncode == 1 and "STOPPED at step deploy" in proc.stderr
        assert "S3" in proc.stderr and "--rollback-env" in proc.stderr


# ---------------------------------------------------------------- rollback

class TestRollback:
    def _interim(self, add):
        add.env_file.write_text(ALL_ENV.replace(json.dumps(REGISTRY), json.dumps({**REGISTRY, "acebar": URI})))
        add.deploy_env.write_text(add.deploy_env.read_text().replace(
            json.dumps(PROJECTS), json.dumps({**PROJECTS, "acebar": ["acebar"]})))

    def test_rollback_env_dry_run_changes_nothing(self, add):
        self._interim(add)
        before = add.env_file.read_text()
        proc, calls = add("acebar", "--rollback-env")
        assert proc.returncode == 0 and "dry run" in proc.stdout and calls == []
        assert add.env_file.read_text() == before

    def test_rollback_env_removes_only_the_acebar_keys_from_the_current_files(self, add):
        self._interim(add)
        # a concurrent, unrelated edit made after the interim edits must survive
        add.env_file.write_text(add.env_file.read_text() + "LATER_EDIT=kept\n")
        proc, _ = add("acebar", "--execute", "--rollback-env")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert _json(add.env_file, "MEMORA_DATABASES") == REGISTRY
        assert _json(add.deploy_env, "MEMORA_PROJECTS") == PROJECTS
        assert "LATER_EDIT=kept" in add.env_file.read_text()          # no stale backup restored over it
        assert "redeploy" in proc.stdout and "--clear-freeze" in proc.stdout

    def test_rollback_env_is_refused_once_the_store_is_local(self, add):
        add.env_file.write_text(ALL_ENV.replace(json.dumps(REGISTRY), json.dumps({**REGISTRY, "acebar": "/data/acebar.db"})))
        before = add.env_file.read_text()
        proc, _ = add("acebar", "--execute", "--rollback-env")
        assert proc.returncode == 2 and "past the interim stage" in proc.stdout
        assert add.env_file.read_text() == before

    def test_clear_freeze_runs_the_tool_and_refusal_is_reported(self, add):
        proc, calls = add("acebar", "--execute", "--clear-freeze")
        assert proc.returncode == 0 and "clear-freeze" in _tool_cmds(calls)
        proc, _ = add("acebar", "--execute", "--clear-freeze", env={"TOOL_RC_CLEAR_FREEZE": "2"})
        assert proc.returncode == 1 and "the freeze was kept" in proc.stderr

    def test_the_two_rollback_steps_cannot_be_combined(self, add):
        proc, _ = add("acebar", "--execute", "--rollback-env", "--clear-freeze")
        assert proc.returncode == 2 and "two steps" in proc.stderr


# ---------------------------------------------------------------- add_store_config.py directly

def _cfg(add, *args):
    proc = subprocess.run([sys.executable, CONFIG, *args, "--all-env", str(add.env_file),
                           "--deploy-env", str(add.deploy_env), "--repo", str(add.repo)],
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


class TestConfigTool:
    def test_check_accepts_a_new_store_and_lists_the_existing_ones(self, add):
        rc, out = _cfg(add, "check", "--db", "acebar", "--uri", URI)
        assert rc == 0 and out["stores"] == sorted(REGISTRY) and out["replicated"] == sorted(REPLICAS)

    @pytest.mark.parametrize("env_change,msg", [
        (lambda t: t.replace("MEMORA_REPLICATION_INTERVAL_S=60\n", ""), "MEMORA_REPLICATION_INTERVAL_S"),
        (lambda t: t.replace("MEMORA_DATABASES='", "MEMORA_DATABASES_X='"), "no MEMORA_DATABASES"),
        (lambda t: t + "MEMORA_DATABASES='{}'\n", "MEMORA_DATABASES 2 times"),
        (lambda t: t.replace("OTHER=kept", "MEMORA_REPLICAS='[]'"), "2 times"),
    ])
    def test_check_refuses_a_broken_configuration(self, add, env_change, msg):
        add.env_file.write_text(env_change(ALL_ENV))
        rc, out = _cfg(add, "check", "--db", "acebar", "--uri", URI)
        assert rc == 2 and msg in out["refused"]

    def test_check_refuses_a_bad_uri_and_name(self, add):
        assert _cfg(add, "check", "--db", "acebar", "--uri", "d1://only-one")[0] == 2
        assert _cfg(add, "check", "--db", "Bad", "--uri", URI)[0] == 2

    def test_apply_is_atomic_with_respect_to_a_changed_file(self, add):
        # the file changed between the operator's plan and the apply: the guards re-run on the CURRENT file
        add.env_file.write_text(ALL_ENV.replace(json.dumps(REGISTRY), json.dumps({**REGISTRY, "acebar": "/data/x.db"})))
        rc, out = _cfg(add, "apply", "--db", "acebar", "--uri", URI, "--backup-dir", str(add.backups))
        assert rc == 2 and "already configured" in out["refused"]
        assert not add.backups.exists()

    def test_apply_then_remove_round_trips_to_the_original_files(self, add):
        rc, _ = _cfg(add, "apply", "--db", "acebar", "--uri", URI, "--backup-dir", str(add.backups))
        assert rc == 0
        rc, out = _cfg(add, "remove", "--db", "acebar", "--backup-dir", str(add.backups))
        assert rc == 0 and out["removed"]["MEMORA_DATABASES"] == URI
        assert _json(add.env_file, "MEMORA_DATABASES") == REGISTRY
        assert _json(add.deploy_env, "MEMORA_PROJECTS") == PROJECTS

    def test_remove_is_refused_when_replicas_name_the_store(self, add):
        add.env_file.write_text(ALL_ENV.replace(json.dumps(REGISTRY), json.dumps({**REGISTRY, "acebar": URI})).replace(
            json.dumps(REPLICAS), json.dumps({**REPLICAS, "acebar": URI})))
        rc, out = _cfg(add, "remove", "--db", "acebar", "--backup-dir", str(add.backups))
        assert rc == 2 and "cutover has started" in out["refused"]

    def test_remove_of_a_store_that_is_not_there_is_a_no_op(self, add):
        rc, out = _cfg(add, "remove", "--db", "acebar", "--backup-dir", str(add.backups))
        assert rc == 0 and out["removed"] == {}
        assert add.env_file.read_text() == ALL_ENV
