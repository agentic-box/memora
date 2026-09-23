"""scripts/deploy-memora-all.sh: the named /data volume, its migration, and
the admin token (local-primary plan §8 L2a, §9 (a); review 7626).

The script is run for real with ssh, git and curl replaced by fakes and
docker by tests/fake_container_runtime.py: ssh runs the remote heredoc
locally, docker records every call and keeps volumes as directories, and the
/data migration program really runs against them. Nothing touches nuc8 or a
real runtime. The script's final smoke check talks HTTP to 127.0.0.1:8920
and fails here (no server); every assertion is about what happened before.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "deploy-memora-all.sh")
FAKE_RUNTIME = os.path.join(REPO, "tests", "fake_container_runtime.py")
ANON = "3f" * 32
REGISTRY = {"memora": "d1://acct/db1", "ob1": "d1://acct/db2"}
MARKER = ".memora-volume-source"


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()
            and p.name != MARKER and not p.relative_to(root).as_posix().startswith(".memora-")}


@pytest.fixture
def deploy(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "instances").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "deploy-memora-all.sh")
    shutil.copy(os.path.join(REPO, "scripts", "migrate_data_volume.sh"), repo / "scripts")
    (repo / "instances" / "all.env").write_text(f"MEMORA_DATABASES='{json.dumps(REGISTRY)}'\n")

    home = tmp_path / "home"
    (home / "repos" / "agentic-box" / "memora").mkdir(parents=True)
    cfg = home / ".config" / "memora"
    cfg.mkdir(parents=True)
    (cfg / "all.health-token").write_text("h" * 48)
    os.chmod(cfg / "all.health-token", 0o600)

    volroot = tmp_path / "volumes"
    old = volroot / ANON
    (old / "intent").mkdir(parents=True)
    (old / "intent" / "memora.jsonl").write_text('{"type":"intent","id":3}\n')
    (old / "freeze").mkdir()
    (old / "freeze" / "ob1").write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir / "ssh", '#!/bin/bash\nshift\nexec "$@"\n')
    os.symlink(FAKE_RUNTIME, bin_dir / "docker")
    _exe(bin_dir / "git", "#!/bin/bash\nexit 0\n")
    _exe(bin_dir / "curl", "#!/bin/bash\nexit 0\n")
    log = tmp_path / "calls.txt"

    def run(cred_env=None, current=ANON, admin_token=None, admin_mode=0o600, runtime_env=None):
        (cfg / "credentials.mcp.json").write_text(json.dumps({"mcpServers": {"memora": {
            "env": cred_env or {"CLOUDFLARE_API_TOKEN": "tok"}}}}))
        if admin_token is not None:
            (cfg / "all.admin-token").write_text(admin_token)
            os.chmod(cfg / "all.admin-token", admin_mode)
        if log.exists():
            log.unlink()
        env = dict(os.environ, HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}",
                   CALL_LOG=str(log), CURRENT_MOUNT=current, VOLROOT=str(volroot),
                   ARGV_OUT=str(tmp_path / "argv.txt"), **(runtime_env or {}))
        proc = subprocess.run(["bash", str(repo / "scripts" / "deploy-memora-all.sh")],
                              env=env, capture_output=True, text=True, timeout=120)
        raw = log.read_text() if log.exists() else ""
        calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
        return proc, calls, cfg

    run.old = old
    run.new = volroot / "memora-all-data"
    return run


def _new_container_run(calls):
    runs = [c for c in calls if c[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one container run, got {runs}"
    return runs[0]


def _index(calls, pred):
    return next(i for i, c in enumerate(calls) if pred(c))


def _is_copy(c):
    return c[:2] == ["run", "--rm"] and "migrate_data_volume" in c


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_first_deploy_copies_the_anonymous_volume_into_the_named_one(deploy):
    proc, calls, cfg = deploy()
    run = _new_container_run(calls)
    assert _flag_values(run, "-v") == ["memora-all-data:/data"]
    envs = _flag_values(run, "-e")
    assert "MEMORA_DATA_VOLUME=memora-all-data" in envs
    admin = [e for e in envs if e.startswith("MEMORA_ADMIN_TOKEN=")]
    assert len(admin) == 1
    token = admin[0].split("=", 1)[1]
    assert len(token) == 48 and token.isalnum() and token != "h" * 48
    assert (cfg / "all.admin-token").read_text() == token
    assert oct(os.stat(cfg / "all.admin-token").st_mode)[-3:] == "600"

    create = _index(calls, lambda c: c[:3] == ["volume", "create", "memora-all-data"])
    stop = _index(calls, lambda c: c == ["stop", "memora-all"])
    copy = _index(calls, _is_copy)
    rename = _index(calls, lambda c: c[0] == "rename")
    new = calls.index(run)
    assert create < stop < copy < rename < new, \
        "the volume is made before the stop; the copy runs while memora-all is stopped"
    assert f"{ANON}:/from:ro" in calls[copy] and "memora-all-data:/to" in calls[copy]
    assert _files(deploy.new) == _files(deploy.old)
    assert f"source={ANON}" in (deploy.new / MARKER).read_text()


def test_an_unchanged_source_is_not_copied_again(deploy):
    """A rerun after a completed copy of the same, unchanged source: the
    migration program reports skip and the live named volume is untouched."""
    deploy()
    (deploy.new / "written-after-the-switch").write_text("x")
    proc, calls, _ = deploy()
    assert any(_is_copy(c) for c in calls)  # checked, then skipped
    assert "skip " in proc.stdout
    assert (deploy.new / "written-after-the-switch").exists()
    _new_container_run(calls)


def test_rollback_then_writes_then_redeploy_recopies(deploy):
    """Review 7626 P1-2: after a rollback memora-all runs on the OLD volume
    again and accrues writes; the next deploy must bring them over."""
    deploy()
    (deploy.old / "intent" / "memora.jsonl").write_text('{"type":"intent","id":3}\n{"type":"intent","id":4}\n')
    (deploy.old / "re.db").write_bytes(b"written during the rollback window")
    proc, calls, _ = deploy()
    assert "copied " in proc.stdout
    assert _files(deploy.new) == _files(deploy.old)


def test_a_partial_copy_is_reset_on_the_rerun(deploy):
    deploy.new.mkdir(parents=True)
    staging = deploy.new / ".memora-staging"
    staging.mkdir()
    (staging / "half-copied").write_text("junk from a failed run")
    deploy()
    assert not (deploy.new / "half-copied").exists()
    assert not staging.exists()
    assert _files(deploy.new) == _files(deploy.old)


def test_no_copy_once_memora_all_mounts_the_named_volume(deploy):
    deploy.new.mkdir(parents=True)
    _, calls, _ = deploy(current="memora-all-data")
    assert not any(_is_copy(c) for c in calls)
    assert _flag_values(_new_container_run(calls), "-v") == ["memora-all-data:/data"]


def test_a_source_still_in_use_is_refused(deploy):
    proc, calls, _ = deploy(runtime_env={"PS_RUNNING": ANON})
    assert proc.returncode != 0 and "still uses" in proc.stderr
    assert not any(_is_copy(c) for c in calls)
    assert not any(c[0] == "rename" for c in calls)


def test_a_failed_in_use_check_is_refused(deploy):
    """Review 7637 P1-2: a FAILED `docker ps` must refuse, not read as
    "nothing uses the volume"."""
    proc, calls, _ = deploy(runtime_env={"PS_RC": "1"})
    assert proc.returncode != 0 and "cannot tell whether a container uses" in proc.stderr
    assert not any(_is_copy(c) for c in calls)
    assert not any(c[0] == "rename" for c in calls)


def test_a_successful_empty_in_use_check_means_unused(deploy):
    proc, calls, _ = deploy()
    assert any(_is_copy(c) for c in calls)
    _new_container_run(calls)


def test_a_failed_copy_stops_before_rename_and_run(deploy):
    proc, calls, _ = deploy(runtime_env={"COPY_RC": "1"})
    assert proc.returncode != 0
    assert "copy" in proc.stderr and "docker start memora-all" in proc.stderr
    assert not any(c[0] == "rename" for c in calls)
    assert not any(c[:2] == ["run", "-d"] for c in calls)


def test_a_volume_that_does_not_resolve_refuses_before_stop(deploy, tmp_path):
    """A runtime that cannot create or name the volume must fail with the old
    container still serving."""
    wrapper = tmp_path / "bin" / "docker"
    wrapper.unlink()
    _exe(wrapper, f'#!/bin/bash\nif [ "$1 $2" = "volume create" ]; then exit 0; fi\nexec "{FAKE_RUNTIME}" "$@"\n')
    proc, calls, _ = deploy()
    assert proc.returncode != 0
    assert not any(c[0] == "stop" for c in calls)


class TestAdminTokenFile:
    """Review 7626 P1-3: an existing admin-token file must be a regular file,
    owned by the deploying user, mode 0600; anything else is refused before
    the live container is touched, and never chmod-ed into shape."""

    def test_0600_is_accepted(self, deploy):
        proc, calls, _ = deploy(admin_token="k" * 48)
        envs = _flag_values(_new_container_run(calls), "-e")
        assert "MEMORA_ADMIN_TOKEN=" + "k" * 48 in envs

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
    def test_a_readable_token_is_refused_and_left_as_is(self, deploy, mode):
        proc, calls, cfg = deploy(admin_token="k" * 48, admin_mode=mode)
        assert proc.returncode != 0 and "mode 0600" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)
        assert oct(os.stat(cfg / "all.admin-token").st_mode)[-3:] == oct(mode)[-3:]

    def test_a_symlink_is_refused(self, deploy, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_text("k" * 48)
        os.chmod(target, 0o600)
        cfg = tmp_path / "home" / ".config" / "memora"
        (cfg / "all.admin-token").symlink_to(target)
        proc, calls, _ = deploy()
        assert proc.returncode != 0 and "regular file" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)

    def test_a_file_owned_by_someone_else_is_refused(self, deploy, tmp_path):
        wrap = tmp_path / "pywrap"
        wrap.mkdir()
        (wrap / "sitecustomize.py").write_text("import os\nos.getuid = lambda: 424242\n")
        proc, calls, _ = deploy(admin_token="k" * 48, runtime_env={"PYTHONPATH": str(wrap)})
        assert proc.returncode != 0 and "owned by" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)

    @pytest.mark.parametrize("mode", [0o644, 0o640])
    def test_a_readable_health_token_is_refused(self, deploy, tmp_path, mode):
        """Review 7636: the health token gets the same rule."""
        f = tmp_path / "home" / ".config" / "memora" / "all.health-token"
        os.chmod(f, mode)
        proc, calls, _ = deploy()
        assert proc.returncode != 0 and "all.health-token must be a regular file" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)
        assert oct(os.stat(f).st_mode)[-3:] == oct(mode)[-3:]

    def test_a_symlinked_health_token_is_refused(self, deploy, tmp_path):
        f = tmp_path / "home" / ".config" / "memora" / "all.health-token"
        target = tmp_path / "elsewhere-health"
        target.write_text("h" * 48)
        os.chmod(target, 0o600)
        f.unlink()
        f.symlink_to(target)
        proc, calls, _ = deploy()
        assert proc.returncode != 0 and "regular file" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)

    def test_equal_to_the_health_token_refuses_before_stop(self, deploy):
        proc, calls, _ = deploy(admin_token="h" * 48)
        assert proc.returncode != 0
        assert "equals the health token" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)

    def test_an_unusable_token_is_refused_not_replaced(self, deploy):
        proc, calls, cfg = deploy(admin_token="short")
        assert proc.returncode != 0
        assert (cfg / "all.admin-token").read_text() == "short"
        assert not any(c[0] == "stop" for c in calls)


def test_credentials_cannot_override_the_marker_or_admin_token(deploy):
    _, calls, _ = deploy(cred_env={"CLOUDFLARE_API_TOKEN": "tok",
                                   "MEMORA_DATA_VOLUME": ANON,
                                   "MEMORA_ADMIN_TOKEN": "stale-token-from-a-credential-file"})
    envs = _flag_values(_new_container_run(calls), "-e")
    assert [e for e in envs if e.startswith("MEMORA_DATA_VOLUME=")] == ["MEMORA_DATA_VOLUME=memora-all-data"]
    admin = [e for e in envs if e.startswith("MEMORA_ADMIN_TOKEN=")]
    assert len(admin) == 1 and "stale" not in admin[0]
    assert "CLOUDFLARE_API_TOKEN=tok" in envs


def test_memory_limit_is_the_measured_gate(deploy):
    from tests.test_instance_script import MEASURED_DEFAULT_MEMORY

    _, calls, _ = deploy()
    assert _flag_values(_new_container_run(calls), "--memory") == [MEASURED_DEFAULT_MEMORY.lower()]
