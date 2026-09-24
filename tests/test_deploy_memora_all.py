"""scripts/deploy-memora-all.sh: the named /data volume, its migration, and
the admin token (local-primary plan §8 L2a, §9 (a); review 7626).

The script is run for real with ssh, git and curl replaced by fakes and
docker by tests/fake_container_runtime.py: ssh runs the remote heredoc
locally, docker records every call and keeps volumes as directories, and the
/data migration program really runs against them. Nothing touches deploy-host or a
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
REGISTRY = {"memora": "d1://acct/db1", "alpha": "d1://acct/db2"}
# The operator's deploy configuration (CFG1), as the fixture writes it into
# the copied repo's git-ignored instances/deploy.env.
PROJECTS = {"memora": ["memora", "project-a"], "alpha": ["alpha"]}
DEPLOY_CFG = {"DEPLOY_HOST": "deploy-host", "DEPLOY_GRAPH_BIND": "100.64.0.10",
              "DEPLOY_REPO": "~/repos/agentic-box/memora", "MEMORA_PROJECTS": json.dumps(PROJECTS)}


def _deploy_env_text(cfg):
    return "".join(f"{k}='{v}'\n" for k, v in cfg.items())
MARKER = ".memora-volume-source"
# Distinctive token values (REL1): a leak anywhere is found by substring.
CF_TOKEN = "cfTOKEN" + "x9" * 20
D1_READ_TOKEN = "d1READ" + "y8" * 20
CRED_CF_TOKEN = "credCF" + "z7" * 20   # the old plain value in credentials.mcp.json
SECRETS_MOUNT = "/run/secrets/memora"


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
    shutil.copy(os.path.join(REPO, "scripts", "deploy_config.py"), repo / "scripts")
    (repo / "instances" / "deploy.env").write_text(_deploy_env_text(DEPLOY_CFG))
    (repo / "instances" / "all.env").write_text(f"MEMORA_DATABASES='{json.dumps(REGISTRY)}'\n")

    home = tmp_path / "home"
    (home / "repos" / "agentic-box" / "memora").mkdir(parents=True)
    cfg = home / ".config" / "memora"
    cfg.mkdir(parents=True)
    (cfg / "all.health-token").write_text("h" * 48)
    os.chmod(cfg / "all.health-token", 0o600)
    secrets = home / ".config" / "memora-lp"
    secrets.mkdir(mode=0o700)
    for name, value in (("cloudflare-api.token", CF_TOKEN), ("d1-read.token", D1_READ_TOKEN)):
        (secrets / name).write_text(value + "\n")
        os.chmod(secrets / name, 0o600)

    volroot = tmp_path / "volumes"
    old = volroot / ANON
    (old / "intent").mkdir(parents=True)
    (old / "intent" / "memora.jsonl").write_text('{"type":"intent","id":3}\n')
    (old / "freeze").mkdir()
    (old / "freeze" / "alpha").write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool_log = tmp_path / "tools.txt"
    # Like real ssh (REL2): the arguments after the host are JOINED with
    # spaces into one command line, which the remote shell re-splits -- an
    # empty argument vanishes and one with a space splits. The joined line is
    # recorded too.
    _exe(bin_dir / "ssh", f'#!/bin/bash\necho "ssh $1" >> "{tool_log}"\nshift\n'
                          f'printf "%s\\n" "$*" > "{tmp_path / "ssh-command.txt"}"\nexec sh -c "$*"\n')
    os.symlink(FAKE_RUNTIME, bin_dir / "docker")
    os.symlink(FAKE_RUNTIME, bin_dir / "podman")
    _exe(bin_dir / "git", f'#!/bin/bash\necho "git $*" >> "{tool_log}"\nexit 0\n')
    _exe(bin_dir / "curl", "#!/bin/bash\nexit 0\n")
    # host-independent: the rehearsal host (Fedora) really is SELinux-enforcing
    _exe(bin_dir / "getenforce", "#!/bin/bash\necho Disabled\n")
    log = tmp_path / "calls.txt"

    def run(cred_env=None, current=ANON, admin_token=None, admin_mode=0o600, runtime_env=None):
        (cfg / "credentials.mcp.json").write_text(json.dumps({"mcpServers": {"memora": {
            "env": cred_env or {"CLOUDFLARE_API_TOKEN": CRED_CF_TOKEN, "OPENAI_API_KEY": "k"}}}}))
        if admin_token is not None:
            (cfg / "all.admin-token").write_text(admin_token)
            os.chmod(cfg / "all.admin-token", admin_mode)
        if log.exists():
            log.unlink()
        env = dict(os.environ, HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}",
                   CALL_LOG=str(log), CURRENT_MOUNT=current, VOLROOT=str(volroot),
                   ARGV_OUT=str(tmp_path / "argv.txt"), INSPECT_FROM_RUN="1", **(runtime_env or {}))
        proc = subprocess.run(["bash", str(repo / "scripts" / "deploy-memora-all.sh")],
                              env=env, capture_output=True, text=True, timeout=120)
        raw = log.read_text() if log.exists() else ""
        calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
        return proc, calls, cfg

    run.old = old
    run.new = volroot / "memora-all-data"
    run.volroot = volroot
    run.tools = tool_log
    run.home = home
    run.bin = bin_dir
    run.secrets = secrets
    run.env_file = repo / "instances" / "all.env"
    run.config_file = repo / "instances" / "deploy.env"
    return run


def _new_container_run(calls):
    runs = [c for c in calls if c[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one container run, got {runs}"
    return runs[0]


def _index(calls, pred):
    return next(i for i, c in enumerate(calls) if pred(c))


def _is_copy(c):
    return c[0] == "run" and "migrate_data_volume" in c


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_first_deploy_copies_the_anonymous_volume_into_the_named_one(deploy):
    proc, calls, cfg = deploy()
    run = _new_container_run(calls)
    assert _flag_values(run, "-v") == ["memora-all-data:/data", f"{deploy.secrets}:{SECRETS_MOUNT}:ro"]
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
    assert "--rm" not in calls[copy], "podman's --rm deletes an anonymous volume mounted with -v (R1)"
    migrator = calls[copy][calls[copy].index("--name") + 1]
    assert calls[copy + 1] == ["rm", migrator], "the migration container is removed with a plain rm"
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
    (deploy.old / "gamma.db").write_bytes(b"written during the rollback window")
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
    assert _flag_values(_new_container_run(calls), "-v") == ["memora-all-data:/data",
                                                             f"{deploy.secrets}:{SECRETS_MOUNT}:ro"]


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
    _, calls, _ = deploy(cred_env={"CLOUDFLARE_API_TOKEN": CRED_CF_TOKEN,
                                   "MEMORA_DATA_VOLUME": ANON,
                                   "MEMORA_ADMIN_TOKEN": "stale-token-from-a-credential-file"})
    envs = _flag_values(_new_container_run(calls), "-e")
    assert [e for e in envs if e.startswith("MEMORA_DATA_VOLUME=")] == ["MEMORA_DATA_VOLUME=memora-all-data"]
    admin = [e for e in envs if e.startswith("MEMORA_ADMIN_TOKEN=")]
    assert len(admin) == 1 and "stale" not in admin[0]
    assert not [e for e in envs if e.startswith("CLOUDFLARE_API_TOKEN=")]  # REL1: only the mounted file


def test_memory_limit_is_the_measured_gate(deploy):
    from tests.test_instance_script import MEASURED_DEFAULT_MEMORY

    _, calls, _ = deploy()
    assert _flag_values(_new_container_run(calls), "--memory") == [MEASURED_DEFAULT_MEMORY.lower()]


# ---------------------------------------------------------------- R1: rehearsal parameters

def test_the_defaults_are_the_production_deploy(deploy):
    """Unparameterised: ssh to deploy-host, docker, the v0.5.2 checkout, memora-all
    on 8920 with memora-all-data and memora:latest, the tokens from
    ~/.config/memora-lp."""
    proc, calls, cfg = deploy()
    tools = deploy.tools.read_text().splitlines()
    assert tools[0] == "ssh deploy-host"
    assert ("deploy target: host=deploy-host runtime=docker container=memora-all volume=memora-all-data "
            "image=memora:latest port=8920 graph=100.64.0.10:8766 tag=v0.5.2 secrets=~/.config/memora-lp") in proc.stdout
    assert any(t.startswith("git ") and "checkout v0.5.2" in t for t in tools)
    run = _new_container_run(calls)
    assert run[run.index("--name") + 1] == "memora-all" and run[-1] == "memora:latest"
    assert "0.0.0.0:8920:8000" in _flag_values(run, "-p")


def test_a_rehearsal_runs_locally_on_another_runtime_with_its_own_names(deploy, tmp_path):
    """DEPLOY_HOST=localhost runs the same steps without ssh; RUNTIME picks
    the binary (every call goes through it); the names, port, image, config
    dir and checkout are parameters."""
    (deploy.bin / "docker").unlink()  # RUNTIME=podman: any docker call is recorded and fails
    _exe(deploy.bin / "docker", f'#!/bin/bash\necho "docker $*" >> "{deploy.tools}"\nexit 97\n')
    rcfg = tmp_path / "rehearsal-config"
    shutil.copytree(deploy.home / ".config" / "memora", rcfg)
    (rcfg / "credentials.mcp.json").write_text(json.dumps({"mcpServers": {"memora": {"env": {"X": "1"}}}}))
    rsec = tmp_path / "rehearsal-secrets"
    shutil.copytree(deploy.secrets, rsec)
    proc, calls, cfg = deploy(runtime_env={
        "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(tmp_path),
        "DEPLOY_LABELS": "memora.rehearsal=rh-run-7",
        "DEPLOY_HOST": "localhost", "RUNTIME": "podman", "DEPLOY_CONTAINER": "memora-rh",
        "DEPLOY_DATA_VOLUME": "memora-rh-data", "DEPLOY_IMAGE": "memora-rh:latest", "DEPLOY_PORT": "18920",
        "DEPLOY_GRAPH_BIND": "127.0.0.1", "DEPLOY_GRAPH_PORT": "18766",
        "DEPLOY_CONFIG_DIR": str(rcfg), "DEPLOY_SECRETS_DIR": str(rsec),
        "DEPLOY_SKIP_CHECKOUT": "1", "DEPLOY_SMOKE_ABSORB": "0",
        "DEPLOY_REPO": str(deploy.home / "repos" / "agentic-box" / "memora"), "DEPLOY_TAG": "v9.9.9"})
    tools = deploy.tools.read_text().splitlines() if deploy.tools.exists() else []
    assert calls, proc.stderr[-2000:]
    assert not [t for t in tools if t.startswith(("ssh", "git", "docker"))], tools
    assert ["inspect", "memora-rh", "--format"] == calls[_index(calls, lambda c: c[0] == "inspect")][:3]
    assert ["stop", "memora-rh"] in calls
    b = calls[_index(calls, lambda c: c[0] == "build")]
    assert b[0] == "build" and b[b.index("-t") + 1] == "memora-rh:latest"
    run = _new_container_run(calls)
    assert run[run.index("--name") + 1] == "memora-rh" and run[-1] == "memora-rh:latest"
    assert _flag_values(run, "-v") == ["memora-rh-data:/data", f"{rsec}:{SECRETS_MOUNT}:ro"]
    assert "0.0.0.0:18920:8000" in _flag_values(run, "-p")
    assert "MEMORA_DATA_VOLUME=memora-rh-data" in _flag_values(run, "-e")
    rename = calls[_index(calls, lambda c: c[0] == "rename")]
    assert rename[1] == "memora-rh" and rename[2].startswith("memora-rh-grok-")
    assert (rcfg / "all.admin-token").exists() and not (cfg / "all.admin-token").exists()
    assert _files(deploy.volroot / "memora-rh-data") == _files(deploy.old)
    # 7729: everything the deploy creates carries the rehearsal run's label
    create = calls[_index(calls, lambda c: c[:2] == ["volume", "create"])]
    copy = calls[_index(calls, _is_copy)]
    build = calls[_index(calls, lambda c: c[0] == "build")]
    for made in (create, copy, run, build):
        assert _flag_values(made, "--label") == ["memora.rehearsal=rh-run-7"], made


REHEARSAL = {"DEPLOY_HOST": "localhost", "RUNTIME": "podman", "DEPLOY_CONTAINER": "memora-rh",
             "DEPLOY_DATA_VOLUME": "memora-rh-data", "DEPLOY_IMAGE": "memora-rh:latest", "DEPLOY_PORT": "18920",
             "DEPLOY_GRAPH_BIND": "127.0.0.1", "DEPLOY_GRAPH_PORT": "18766",
             "DEPLOY_SKIP_CHECKOUT": "1", "DEPLOY_SMOKE_ABSORB": "0"}


def _nothing_done(deploy, proc, calls):
    assert proc.returncode != 0
    assert calls == [], "no runtime call"
    assert not deploy.tools.exists() or not deploy.tools.read_text(), "no ssh or git"


@pytest.mark.parametrize("override", [
    {"RUNTIME": "podman"}, {"DEPLOY_PORT": "18920"}, {"DEPLOY_CONTAINER": "memora-rh"},
    {"DEPLOY_HOST": "localhost"}, {"DEPLOY_SKIP_CHECKOUT": "1"}, {"DEPLOY_TAG": "v9"},
    {"DEPLOY_LABELS": "memora.rehearsal=x"}, {"DEPLOY_SECRETS_DIR": "/tmp/tokens"}, {"DEPLOY_STORE_WAIT_S": "1"},
])
def test_an_override_without_the_rehearsal_sentinel_is_refused(deploy, override):
    """7725 P1: a stray variable cannot re-target the production deploy."""
    proc, calls, cfg = deploy(runtime_env=override)
    _nothing_done(deploy, proc, calls)
    assert "overrides are for rehearsals only" in proc.stderr and next(iter(override)) in proc.stderr


@pytest.mark.parametrize("change, match", [
    ({"DEPLOY_CONTAINER": "memora-all"}, "DEPLOY_CONTAINER 'memora-all' is not rehearsal-scoped"),
    ({"DEPLOY_DATA_VOLUME": "memora-all-data"}, "DEPLOY_DATA_VOLUME 'memora-all-data' is not rehearsal-scoped"),
    ({"DEPLOY_IMAGE": "memora:latest"}, "DEPLOY_IMAGE 'memora:latest' is not rehearsal-scoped"),
    ({"DEPLOY_PORT": "8920"}, "8920 is production's"),
    ({"DEPLOY_HOST": "deploy-host"}, "DEPLOY_HOST must be localhost"),
    ({"DEPLOY_CONFIG_DIR": "/etc"}, "DEPLOY_CONFIG_DIR is not under"),
    ({"DEPLOY_SECRETS_DIR": "/etc"}, "DEPLOY_SECRETS_DIR is not under"),
    ({"DEPLOY_REHEARSAL_ROOT": ""}, "DEPLOY_REHEARSAL_ROOT must name an existing directory"),
    ({"DEPLOY_LABELS": ""}, "DEPLOY_LABELS must carry memora.rehearsal=<run-id>"),
    ({"DEPLOY_LABELS": "memora.rehearsal="}, "DEPLOY_LABELS must carry memora.rehearsal=<run-id>"),
])
def test_a_rehearsal_with_a_production_like_target_is_refused(deploy, tmp_path, change, match):
    rcfg = tmp_path / "rcfg"
    rcfg.mkdir()
    env = {**REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(tmp_path),
           "DEPLOY_LABELS": "memora.rehearsal=rh-run-7", "DEPLOY_CONFIG_DIR": str(rcfg),
           "DEPLOY_SECRETS_DIR": str(tmp_path / "rsec"), **change}
    proc, calls, cfg = deploy(runtime_env=env)
    _nothing_done(deploy, proc, calls)
    assert match in proc.stderr


def test_a_rehearsal_env_file_outside_the_root_is_refused(deploy, tmp_path):
    root = tmp_path / "rh"
    (root / "cfg").mkdir(parents=True)
    proc, calls, cfg = deploy(runtime_env={**REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(root), "DEPLOY_LABELS": "memora.rehearsal=r",
                                           "DEPLOY_CONFIG_DIR": str(root / "cfg")})
    _nothing_done(deploy, proc, calls)
    assert "DEPLOY_ENV_FILE is not under" in proc.stderr


# ---------------------------------------------------------------- REL1: tokens as mounted files

def _all_argv(calls):
    return "\n".join("\x1f".join(c) for c in calls)


def _secrets_refused(deploy, proc, calls, match):
    """Refused in the remote script's first step: nothing fetched, built or stopped."""
    assert proc.returncode != 0
    assert match in proc.stderr, proc.stderr[-2000:]
    assert "nothing was stopped" in proc.stderr
    assert calls == [], "no runtime call at all"
    assert not [t for t in deploy.tools.read_text().splitlines() if t.startswith("git ")]


class TestTokenFiles:
    """Review 7758: the Cloudflare tokens reach the container only as files
    of a read-only mount; the container gets the *_FILE paths, never a value."""

    def test_mounted_read_only_and_only_the_paths_are_set(self, deploy):
        proc, calls, _ = deploy()
        run = _new_container_run(calls)
        assert f"{deploy.secrets}:{SECRETS_MOUNT}:ro" in _flag_values(run, "-v")
        envs = _flag_values(run, "-e")
        assert f"CLOUDFLARE_API_TOKEN_FILE={SECRETS_MOUNT}/cloudflare-api.token" in envs
        assert f"MEMORA_D1_READ_TOKEN_FILE={SECRETS_MOUNT}/d1-read.token" in envs
        assert f"MEMORA_D1_REPLICATOR_TOKEN_FILE={SECRETS_MOUNT}/d1-read.token" in envs
        for plain in ("CLOUDFLARE_API_TOKEN=", "CF_API_TOKEN=", "MEMORA_D1_READ_TOKEN=", "MEMORA_D1_REPLICATOR_TOKEN="):
            assert not [e for e in envs if e.startswith(plain)], plain
        assert "OPENAI_API_KEY=k" in envs  # the other credentials still pass through
        assert "container env carries no token value; /run/secrets/memora is mounted read-only" in proc.stdout

    def test_no_token_value_in_any_argv_or_output(self, deploy):
        proc, calls, _ = deploy()
        seen = _all_argv(calls) + proc.stdout + proc.stderr
        for value in (CF_TOKEN, D1_READ_TOKEN, CRED_CF_TOKEN):
            assert value not in seen

    def test_the_new_image_reads_the_files_before_the_stop(self, deploy):
        proc, calls, _ = deploy()
        assert "token files readable by the new image through the read-only mount" in proc.stdout
        pre = _index(calls, lambda c: c[:2] == ["run", "--rm"] and "check_secret_files" in c[-1])
        assert pre < _index(calls, lambda c: c[0] == "stop")
        assert f"{deploy.secrets}:{SECRETS_MOUNT}:ro" in _flag_values(calls[pre], "-v")

    def test_an_unreadable_mount_in_the_new_image_refuses_before_the_stop(self, deploy):
        proc, calls, _ = deploy(runtime_env={"SECRETS_UNREADABLE": "1"})
        assert proc.returncode != 0 and "token-file preflight failed" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)
        assert CF_TOKEN not in proc.stderr + proc.stdout

    @pytest.mark.parametrize("name", ["cloudflare-api.token", "d1-read.token"])
    def test_a_missing_file_refuses_before_anything(self, deploy, name):
        (deploy.secrets / name).unlink()
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, f"token file {deploy.secrets / name} is missing")
        assert "umask 077" in proc.stderr  # the one command to write it

    def test_a_missing_directory_refuses_before_anything(self, deploy):
        shutil.rmtree(deploy.secrets)
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, "is missing")

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o400, 0o700])
    def test_a_file_not_0600_is_refused_and_left_as_is(self, deploy, mode):
        p = deploy.secrets / "d1-read.token"
        os.chmod(p, mode)
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, "mode 0600")
        assert stat.S_IMODE(os.stat(p).st_mode) == mode

    def test_a_symlinked_file_is_refused(self, deploy, tmp_path):
        target = tmp_path / "elsewhere.token"
        target.write_text(CF_TOKEN)
        os.chmod(target, 0o600)
        (deploy.secrets / "cloudflare-api.token").unlink()
        (deploy.secrets / "cloudflare-api.token").symlink_to(target)
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, "regular file")

    def test_a_symlinked_directory_is_refused(self, deploy, tmp_path):
        real = tmp_path / "real-secrets"
        deploy.secrets.rename(real)
        deploy.secrets.symlink_to(real)
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, "must be a directory (not a symlink)")

    def test_a_file_owned_by_someone_else_is_refused(self, deploy, tmp_path):
        wrap = tmp_path / "pywrap"
        wrap.mkdir()
        (wrap / "sitecustomize.py").write_text("import os\nos.getuid = lambda: 424242\n")
        # the directory is the user's (its own check passes); the FILE is refused
        (wrap / "sitecustomize.py").write_text(
            "import os, stat\n_l = os.lstat\n"
            "def lstat(p, *a, **k):\n    st = _l(p, *a, **k)\n"
            "    if stat.S_ISREG(st.st_mode):\n"
            "        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 424242) + tuple(st)[5:10])\n"
            "    return st\nos.lstat = lstat\n")
        proc, calls, _ = deploy(runtime_env={"PYTHONPATH": str(wrap)})
        _secrets_refused(deploy, proc, calls, "must be a regular file owned by this user with mode 0600")

    def test_an_empty_file_is_refused(self, deploy):
        (deploy.secrets / "d1-read.token").write_text("\n")
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, "is empty")

    def test_a_leaked_value_in_the_new_container_env_fails_the_deploy(self, deploy):
        proc, calls, _ = deploy(runtime_env={"RUN_INSPECT_EXTRA_ENV": f"SOMETHING={D1_READ_TOKEN}"})
        assert proc.returncode != 0 and "exposes a token" in proc.stderr
        assert "a token value is in the container's environment" in proc.stderr
        assert D1_READ_TOKEN not in proc.stderr + proc.stdout

    def test_a_writable_token_mount_fails_the_deploy(self, deploy):
        proc, calls, _ = deploy(runtime_env={"RUN_INSPECT_RW": "1"})
        assert proc.returncode != 0 and "is not mounted exactly once read-only" in proc.stderr

    def test_selinux_enforcing_relabels_the_mount_shared_and_read_only(self, deploy):
        _exe(deploy.bin / "getenforce", "#!/bin/bash\necho Enforcing\n")
        proc, calls, _ = deploy()
        run = _new_container_run(calls)
        assert f"{deploy.secrets}:{SECRETS_MOUNT}:ro,z" in _flag_values(run, "-v")
        pre = calls[_index(calls, lambda c: c[:2] == ["run", "--rm"] and "check_secret_files" in c[-1])]
        assert f"{deploy.secrets}:{SECRETS_MOUNT}:ro,z" in _flag_values(pre, "-v")
        assert "mounted :ro,z" in proc.stdout
        assert "/run/secrets/memora is mounted read-only" in proc.stdout

    def test_selinux_permissive_keeps_plain_ro(self, deploy):
        _exe(deploy.bin / "getenforce", "#!/bin/bash\necho Permissive\n")
        proc, calls, _ = deploy()
        assert f"{deploy.secrets}:{SECRETS_MOUNT}:ro" in _flag_values(_new_container_run(calls), "-v")

    def test_file_names_are_documented_production_overrides(self, deploy):
        repl = deploy.secrets / "d1-replicator.token"
        repl.write_text("r" * 40)
        os.chmod(repl, 0o600)
        proc, calls, _ = deploy(runtime_env={"DEPLOY_D1_REPLICATOR_TOKEN_FILE": "d1-replicator.token"})
        assert "overrides are for rehearsals only" not in proc.stderr
        envs = _flag_values(_new_container_run(calls), "-e")
        assert f"MEMORA_D1_REPLICATOR_TOKEN_FILE={SECRETS_MOUNT}/d1-replicator.token" in envs
        assert f"MEMORA_D1_READ_TOKEN_FILE={SECRETS_MOUNT}/d1-read.token" in envs

    def test_the_cloudflare_file_name_is_an_override_too(self, deploy):
        cf = deploy.secrets / "cf-other.token"
        cf.write_text("c" * 40)
        os.chmod(cf, 0o600)
        proc, calls, _ = deploy(runtime_env={"DEPLOY_CLOUDFLARE_TOKEN_FILE": "cf-other.token"})
        envs = _flag_values(_new_container_run(calls), "-e")
        assert f"CLOUDFLARE_API_TOKEN_FILE={SECRETS_MOUNT}/cf-other.token" in envs

    @pytest.mark.parametrize("bad", ["../d1-read.token", "/etc/passwd", "sub/x.token", ".hidden", ""])
    def test_a_file_name_that_is_not_a_plain_name_is_refused(self, deploy, bad):
        proc, calls, _ = deploy(runtime_env={"DEPLOY_D1_READ_TOKEN_FILE": bad} if bad else
                                {"DEPLOY_CLOUDFLARE_TOKEN_FILE": ""})
        if bad == "":
            # empty means the default (bash ${VAR:-default})
            assert f"CLOUDFLARE_API_TOKEN_FILE={SECRETS_MOUNT}/cloudflare-api.token" in _flag_values(
                _new_container_run(calls), "-e")
            return
        _nothing_done(deploy, proc, calls)
        assert "must be a plain file name inside DEPLOY_SECRETS_DIR" in proc.stderr

    def test_credential_token_file_variables_are_not_passed_through(self, deploy):
        _, calls, _ = deploy(cred_env={"CF_API_TOKEN": CRED_CF_TOKEN, "MEMORA_D1_READ_TOKEN": "v",
                                       "MEMORA_D1_READ_TOKEN_FILE": "/elsewhere", "MEMORA_REPLICATION": "write",
                                       "OPENAI_API_KEY": "k"})
        envs = _flag_values(_new_container_run(calls), "-e")
        assert not [e for e in envs if e.startswith(("CF_API_TOKEN=", "MEMORA_D1_READ_TOKEN=", "MEMORA_REPLICATION="))]
        assert [e for e in envs if e.startswith("MEMORA_D1_READ_TOKEN_FILE=")] == [
            f"MEMORA_D1_READ_TOKEN_FILE={SECRETS_MOUNT}/d1-read.token"]


# ---------------------------------------------------------------- REL1: local-primary switches from all.env

LOCAL_REGISTRY = {**REGISTRY, "gamma": "/data/gamma.db"}


def _env_file(deploy, **extra):
    lines = [f"MEMORA_DATABASES='{json.dumps(LOCAL_REGISTRY)}'"]
    lines += [f"{k}='{v}'" if k == "MEMORA_REPLICAS" else f"{k}={v}" for k, v in extra.items()]
    deploy.env_file.write_text("\n".join(lines) + "\n")


class TestReplicationPassthrough:
    def test_absent_is_dark(self, deploy):
        _env_file(deploy)
        proc, calls, _ = deploy()
        envs = _flag_values(_new_container_run(calls), "-e")
        assert not [e for e in envs if e.startswith(("MEMORA_REPLICAS=", "MEMORA_REPLICATION="))]
        assert "MEMORA_REPLICAS / MEMORA_REPLICATION absent (dark)" in proc.stdout

    def test_present_is_passed_through(self, deploy):
        replicas = json.dumps({"gamma": "d1://acct/db3"})
        _env_file(deploy, MEMORA_REPLICAS=replicas, MEMORA_REPLICATION="write")
        proc, calls, _ = deploy()
        envs = _flag_values(_new_container_run(calls), "-e")
        assert f"MEMORA_REPLICAS={replicas}" in envs and "MEMORA_REPLICATION=write" in envs
        assert "local-primary: MEMORA_REPLICAS=['gamma'] MEMORA_REPLICATION=write" in proc.stdout

    def test_file_uri_registry_entry_counts_as_local(self, deploy):
        deploy.env_file.write_text(
            f"MEMORA_DATABASES='{json.dumps({**REGISTRY, 'gamma': 'file:///data/gamma.db'})}'\n"
            f"MEMORA_REPLICAS='{json.dumps({'gamma': 'd1://acct/db3'})}'\nMEMORA_REPLICATION=log\n")
        proc, calls, _ = deploy()
        assert "MEMORA_REPLICATION=log" in _flag_values(_new_container_run(calls), "-e")

    @pytest.mark.parametrize("extra, match", [
        ({"MEMORA_REPLICATION": "on"}, "must be log or write"),
        ({"MEMORA_REPLICAS": '{"nope": "d1://a/b"}'}, "not a store of MEMORA_DATABASES"),
        ({"MEMORA_REPLICAS": '{"memora": "d1://acct/db1"}'}, "a replicated store must be a local path"),
        ({"MEMORA_REPLICAS": '{"gamma": "gamma"}'}, "must be d1://account/database"),
        ({"MEMORA_REPLICAS": '"gamma"'}, "must be a non-empty JSON object"),
        ({"MEMORA_REPLICAS": "{}"}, "must be a non-empty JSON object"),
        ({"MEMORA_REPLICAS": "gamma"}, "Expecting value"),
    ])
    def test_a_bad_value_refuses_before_anything(self, deploy, extra, match):
        _env_file(deploy, **extra)
        proc, calls, _ = deploy()
        _nothing_done(deploy, proc, calls)
        assert match in proc.stderr and "nothing was done" in proc.stderr


class TestReplicationTimingPassthrough:
    def test_present_values_are_passed_through(self, deploy):
        _env_file(deploy, MEMORA_REPLICAS=json.dumps({"gamma": "d1://acct/db3"}), MEMORA_REPLICATION="write",
                  MEMORA_REPLICATION_INTERVAL_S="60", MEMORA_REPLICATION_POLL_S="2.5",
                  MEMORA_REPLICATION_BATCH_ROWS="250")
        proc, calls, _ = deploy()
        envs = _flag_values(_new_container_run(calls), "-e")
        for kv in ("MEMORA_REPLICATION_INTERVAL_S=60", "MEMORA_REPLICATION_POLL_S=2.5",
                   "MEMORA_REPLICATION_BATCH_ROWS=250"):
            assert kv in envs, kv
            assert f"local-primary: {kv}" in proc.stdout

    def test_absent_values_are_not_set(self, deploy):
        _env_file(deploy)
        _, calls, _ = deploy(cred_env={"MEMORA_REPLICATION_INTERVAL_S": "5", "OPENAI_API_KEY": "k"})
        envs = _flag_values(_new_container_run(calls), "-e")
        assert not [e for e in envs if e.startswith("MEMORA_REPLICATION_")]  # never from credentials

    @pytest.mark.parametrize("var, value", [
        ("MEMORA_REPLICATION_INTERVAL_S", "-1"), ("MEMORA_REPLICATION_INTERVAL_S", "soon"),
        ("MEMORA_REPLICATION_POLL_S", "0"), ("MEMORA_REPLICATION_BATCH_ROWS", "1001"),
        ("MEMORA_REPLICATION_BATCH_ROWS", "1.5"),
    ])
    def test_an_invalid_value_refuses_before_anything(self, deploy, var, value):
        _env_file(deploy, **{var: value})
        proc, calls, _ = deploy()
        _nothing_done(deploy, proc, calls)
        assert var in proc.stderr and "nothing was done" in proc.stderr


# ---------------------------------------------------------------- REL1 review 7787 P1: replicated stores after the start

class _FakeMemora:
    """The HTTP surface the deploy's post-start check talks to: /health,
    /health/db/<store>, /mcp/<store> (initialize, memory_semantic_search,
    memory_stats) and /api/v1 (404). health_db maps a store to its body."""

    def __init__(self, health_db):
        import http.server
        import threading

        outer = self
        self.health_db = health_db
        self.tool_calls = []
        self.graph_token = None

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=()):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/databases":  # the graph UI (G1), on the same fake port
                    if self.headers.get("Authorization", "") != f"Bearer {outer.graph_token}":
                        return self._send(401, {"error": "unauthorized", "memora_graph": True})
                    return self._send(200, {"databases": sorted(LOCAL_REGISTRY), "default": "memora"})
                if self.path == "/health":
                    return self._send(200, {"status": "ok", "version": "0.5.0"})
                if self.path.startswith("/health/db/"):
                    store = self.path.rsplit("/", 1)[1]
                    body = outer.health_db.get(store, {})
                    if isinstance(body, list):  # a sequence of answers; the last one repeats
                        body = body.pop(0) if len(body) > 1 else body[0]
                    return self._send(200, {"status": "ok", "stale": False, **body})
                return self._send(404, {})

            def do_POST(self):
                store = self.path.rsplit("/", 1)[1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body.get("method") == "initialize":
                    return self._send(200, {"jsonrpc": "2.0", "id": 1, "result": {}}, [("mcp-session-id", "s1")])
                if body.get("method") == "tools/call":
                    name = body["params"]["name"]
                    outer.tool_calls.append((store, name))
                    out = {"results": [], "profile": {"total_requests": 1, "total_seconds": 0}}
                    if name == "memory_stats":
                        out = {**out, "database": store, "import_pending": 0, "total_memories": 1}
                    return self._send(200, {"jsonrpc": "2.0", "id": body["id"],
                                            "result": {"content": [{"type": "text", "text": json.dumps(out)}]}})
                return self._send(202, {})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


GRAPH_TOKEN = "G" * 48
REPL_OK = {"mode": "write", "status": "running", "replica_uri": "d1://acct/db3", "head_seq": 0,
           "last_acked_seq": 0, "lag_rows": 0, "trigger_version": 2, "trigger_version_expected": 2}
FROZEN = {"freeze": {"state": "frozen", "in_flight": 0}}


@pytest.fixture
def poststart(deploy, tmp_path):
    """A rehearsal-mode deploy against _FakeMemora: `gamma` replicated (write)."""
    rcfg = tmp_path / "rcfg"
    shutil.copytree(deploy.home / ".config" / "memora", rcfg)
    (rcfg / "credentials.mcp.json").write_text(json.dumps({"mcpServers": {"memora": {"env": {"X": "1"}}}}))
    rsec = tmp_path / "rsec"
    shutil.copytree(deploy.secrets, rsec)
    (rsec / "graph.token").write_text(GRAPH_TOKEN)
    os.chmod(rsec / "graph.token", 0o600)
    envf = tmp_path / "rh.env"
    envf.write_text(f"MEMORA_DATABASES='{json.dumps(LOCAL_REGISTRY)}'\n"
                    f"MEMORA_REPLICAS='{json.dumps({'gamma': 'd1://acct/db3'})}'\nMEMORA_REPLICATION=write\n")
    servers = []

    def run(health_db):
        fake = _FakeMemora(health_db)
        fake.graph_token = GRAPH_TOKEN
        servers.append(fake)
        proc, calls, _ = deploy(runtime_env={
            **REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(tmp_path),
            "DEPLOY_LABELS": "memora.rehearsal=rh-t", "DEPLOY_CONFIG_DIR": str(rcfg), "DEPLOY_SECRETS_DIR": str(rsec),
            "DEPLOY_ENV_FILE": str(envf), "DEPLOY_PORT": str(fake.port), "DEPLOY_TAG": "v0.5.0",
            "DEPLOY_GRAPH_PORT": str(fake.port),
            "DEPLOY_REPO": str(deploy.home / "repos" / "agentic-box" / "memora"), "DEPLOY_STORE_WAIT_S": "2"})
        return proc, fake

    yield run
    for s in servers:
        s.close()


class TestReplicatedStoreAfterStart:
    def test_a_frozen_replicating_store_passes(self, poststart):
        proc, fake = poststart({"gamma": {**FROZEN, "replication": REPL_OK}})
        assert "store gamma: replicating (write) to d1://acct/db3, status running, trigger version 2" in proc.stdout
        assert "store gamma: /health/db 200 ok, FROZEN" in proc.stdout
        assert "all 3 stores verified" in proc.stdout, proc.stderr[-1500:]
        assert ("gamma", "memory_stats") in fake.tool_calls  # X2: a frozen store serves reads

    def test_a_thawed_replicating_store_is_checked_and_called(self, poststart):
        proc, fake = poststart({"gamma": {"replication": REPL_OK}})
        assert "all 3 stores verified" in proc.stdout, proc.stderr[-1500:]
        assert ("gamma", "memory_stats") in fake.tool_calls

    @pytest.mark.parametrize("body, reason", [
        ({"replication": {"status": "refused", "error": "ReplicatorConfigError: gamma: no sync_state (install_sync has not run)"}},
         "replication refused: ReplicatorConfigError: gamma: no sync_state"),
        ({"replication": {**REPL_OK, "status": "halted", "halted_reason": "foreign_writer"}}, "replication halted: foreign_writer"),
        ({"replication": {**REPL_OK, "mode": "log"}}, "replication mode 'log', configured 'write'"),
        ({"replication": {**REPL_OK, "replica_uri": "d1://acct/other"}}, "replica_uri 'd1://acct/other', configured 'd1://acct/db3'"),
        ({"replication": {**REPL_OK, "trigger_version": 1}}, "sync trigger version 1, this build expects 2"),
        ({"refused": "the /data check refused it", "replication": REPL_OK}, "the store is refused"),
    ])
    def test_a_frozen_store_fails_on_its_replication(self, poststart, body, reason):
        proc, fake = poststart({"gamma": {**FROZEN, **body}})
        assert proc.returncode != 0
        assert "STORE CHECK FAILED — gamma: " + reason in proc.stderr, proc.stderr[-2000:]
        assert "L6 runbook" in proc.stderr and "all 3 stores verified" not in proc.stdout

    @pytest.mark.parametrize("body, reason", [
        ({}, "no replication block (the replicator has not started)"),
        ({"replication": {"mode": "write", "status": "disabled"}}, "the replicator has not read its sync state yet"),
    ])
    def test_http_200_alone_is_not_enough_for_a_frozen_replicated_store(self, poststart, body, reason):
        # not terminal: waited for (DEPLOY_STORE_WAIT_S, 90 s in production), then failed
        proc, _ = poststart({"gamma": {**FROZEN, **body}})
        assert proc.returncode != 0 and f"STORE CHECK FAILED — gamma: {reason}" in proc.stderr, proc.stderr[-1500:]

    def test_a_persistent_backoff_fails_after_the_bounded_wait(self, poststart):
        # review 7841 P1: D1 auth/network trouble after the sync state was read
        repl = {**REPL_OK, "status": "backoff", "last_error": "D1 403 Forbidden", "lag_rows": 1}
        proc, _ = poststart({"gamma": {**FROZEN, "replication": repl}})
        assert proc.returncode != 0
        assert ("STORE CHECK FAILED — gamma: replication status 'backoff', not running "
                "(last_error 'D1 403 Forbidden', lag_rows 1)") in proc.stderr, proc.stderr[-1500:]
        assert "all 3 stores verified" not in proc.stdout

    def test_a_backoff_that_recovers_within_the_wait_passes(self, poststart):
        repl = {**REPL_OK, "status": "backoff", "last_error": "D1 503", "lag_rows": 1}
        proc, _ = poststart({"gamma": [{**FROZEN, "replication": repl}, {**FROZEN, "replication": REPL_OK}]})
        assert "all 3 stores verified" in proc.stdout, proc.stderr[-1500:]

    def test_a_store_not_replicated_keeps_the_plain_check(self, poststart):
        proc, fake = poststart({"gamma": {"replication": REPL_OK}, "alpha": {**FROZEN}})
        assert "store alpha: /health/db 200 ok, FROZEN" in proc.stdout
        assert "store alpha: replicating" not in proc.stdout
        assert "all 3 stores verified" in proc.stdout, proc.stderr[-1500:]


def test_a_frozen_unsafe_store_fails_the_deploy(poststart):
    proc, _ = poststart({"gamma": {"replication": REPL_OK}, "alpha": {"freeze": {"state": "frozen-unsafe", "in_flight": 1}}})
    assert proc.returncode != 0 and "STORE CHECK FAILED — alpha: frozen-unsafe after the restart" in proc.stderr


class TestDataDirPinned:
    """Leader 7834: MEMORA_DATA_DIR is /data in the container, whatever the
    sources say; a source naming another dir is refused (X3's service lock)."""

    def _data_dirs(self, calls):
        return [e for e in _flag_values(_new_container_run(calls), "-e") if e.startswith("MEMORA_DATA_DIR=")]

    def test_set_to_data(self, deploy):
        _, calls, _ = deploy()
        assert self._data_dirs(calls) == ["MEMORA_DATA_DIR=/data"]

    def test_credentials_saying_data_are_not_duplicated(self, deploy):
        _, calls, _ = deploy(cred_env={"MEMORA_DATA_DIR": "/data", "OPENAI_API_KEY": "k"})
        assert self._data_dirs(calls) == ["MEMORA_DATA_DIR=/data"]

    def test_credentials_naming_another_dir_are_refused_before_the_stop(self, deploy):
        proc, calls, _ = deploy(cred_env={"MEMORA_DATA_DIR": "/elsewhere", "OPENAI_API_KEY": "k"})
        assert proc.returncode != 0 and "sets MEMORA_DATA_DIR=/elsewhere" in proc.stderr
        assert "pinned to /data" in proc.stderr
        assert not any(c[0] in ("stop", "rename") or c[:2] == ["run", "-d"] for c in calls)

    def test_all_env_naming_another_dir_is_refused_before_anything(self, deploy):
        deploy.env_file.write_text(deploy.env_file.read_text() + "MEMORA_DATA_DIR=/srv/memora\n")
        proc, calls, _ = deploy()
        _nothing_done(deploy, proc, calls)
        assert "sets MEMORA_DATA_DIR=/srv/memora" in proc.stderr

    def test_all_env_saying_data_is_accepted(self, deploy):
        deploy.env_file.write_text(deploy.env_file.read_text() + "MEMORA_DATA_DIR=/data\n")
        _, calls, _ = deploy()
        assert self._data_dirs(calls) == ["MEMORA_DATA_DIR=/data"]


# ---------------------------------------------------------------- REL2: the remote arguments survive ssh

N_REMOTE_ARGS = 25


def _decode_blob(line):
    import base64
    import hashlib

    words = line.split()
    assert words[:3] == ["bash", "-s", "--"] and len(words) == 5, words
    assert words[3] == hashlib.sha256(words[4].encode()).hexdigest()
    raw = base64.b64decode(words[4])
    assert raw.endswith(b"\0")
    return [v.decode() for v in raw[:-1].split(b"\0")]


class TestRemoteArguments:
    """Production died with `$18: unbound variable` (leader 7885): ssh joins
    its arguments into one command line, so empty ones vanished. The fake ssh
    now does the same joining (fixture); these pin the transport itself."""

    def test_the_remote_command_is_one_word_carrying_every_parameter(self, deploy, tmp_path):
        deploy()
        line = (tmp_path / "ssh-command.txt").read_text().strip()
        params = _decode_blob(line)
        assert len(params) == N_REMOTE_ARGS
        assert params[0] == "v0.5.2" and params[3] == "docker" and params[4] == "memora-all"
        assert params[12] == ""                      # DEPLOY_LABELS: empty in production
        assert params[13] == "~/.config/memora-lp"
        assert params[17] == ""                      # MEMORA_REPLICAS_B64: empty (dark)
        assert params[18] == "" and params[19] == ""  # MEMORA_REPLICATION, the timing
        assert params[20] == "90"
        assert params[21:24] == ["100.64.0.10", "8766", "graph.token"]  # G1: the graph publish
        import base64
        assert json.loads(base64.b64decode(params[24])) == PROJECTS    # CFG1: from deploy.env

    def test_spaces_survive_the_transport(self, deploy, tmp_path):
        root = tmp_path / "rh root"
        rcfg = root / "c f g"
        shutil.copytree(deploy.home / ".config" / "memora", rcfg)
        (rcfg / "credentials.mcp.json").write_text(json.dumps({"mcpServers": {"memora": {"env": {"X": "1"}}}}))
        rsec = root / "s e c"
        shutil.copytree(deploy.secrets, rsec)
        envf = root / "all env"
        envf.write_text(f"MEMORA_DATABASES='{json.dumps(REGISTRY)}'\n")
        dcfg = root / "deploy env"
        dcfg.write_text(deploy.config_file.read_text())
        proc, calls, _ = deploy(runtime_env={
            **REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(root),
            "DEPLOY_LABELS": "memora.rehearsal=rh-t extra=x", "DEPLOY_CONFIG_DIR": str(rcfg),
            "DEPLOY_SECRETS_DIR": str(rsec), "DEPLOY_ENV_FILE": str(envf), "DEPLOY_CONFIG_FILE": str(dcfg),
            "DEPLOY_REPO": str(deploy.home / "repos" / "agentic-box" / "memora")})
        run = _new_container_run(calls)
        assert f"{rsec}:{SECRETS_MOUNT}:ro" in _flag_values(run, "-v"), proc.stderr[-1500:]
        assert _flag_values(run, "--label") == ["memora.rehearsal=rh-t", "extra=x"]
        assert (rcfg / "all.admin-token").exists()

    @pytest.mark.parametrize("mangle, match", [
        ("drop-last-word", "arrived with 1 words, not 2"),
        ("truncate-blob", "the parameters' sha256 does not match"),
        # review 7889: one base64 character changed -- still 21 NUL-terminated
        # fields, a different value (inside DEPLOY_IMAGE's or another field)
        ("same-count-change", "the parameters' sha256 does not match"),
        ("digest-changed", "the parameters' sha256 does not match"),
    ])
    def test_a_broken_transport_refuses_before_anything(self, deploy, mangle, match):
        flip = ('python3 -c \'import base64,sys; b=bytearray(base64.b64decode(sys.argv[1])); '
                'i=b.index(b"memora:latest"); b[i]=ord("n"); print(base64.b64encode(bytes(b)).decode())\' "$5"')
        body = {"drop-last-word": 'set -- $*; n=$#; a=""; i=1; for w in "$@"; do [ $i -lt $n ] && a="$a $w"; i=$((i+1)); done; exec sh -c "$a"',
                "truncate-blob": 'set -- $*; b="$5"; exec sh -c "$1 $2 $3 $4 ${b%????????????????}"',
                "same-count-change": f'set -- $*; exec sh -c "$1 $2 $3 $4 $({flip})"',
                "digest-changed": 'set -- $*; exec sh -c "$1 $2 $3 0000$4 $5"'}[mangle]
        _exe(deploy.bin / "ssh", f'#!/bin/bash\nshift\n{body}\n')
        proc, calls, _ = deploy()
        assert proc.returncode != 0 and match in proc.stderr, proc.stderr[-800:]
        assert "argument transport broken; nothing was done" in proc.stderr
        assert calls == []


def _transport_block(script):
    """The encode line and the decode block, as the deploy script has them."""
    lines = script.splitlines()
    a = next(k for k, l in enumerate(lines) if l.startswith("PARAMS_B64="))
    b = next(k for k, l in enumerate(lines) if l.startswith("REMOTE_CMD="))
    i = next(k for k, l in enumerate(lines) if l.startswith("broken() {"))
    j = next(k for k, l in enumerate(lines) if l == 'set -- "${P[@]}"')
    return "\n".join(lines[a:b]), "\n".join(lines[i:j + 1])


@pytest.mark.parametrize("pos", range(N_REMOTE_ARGS))
def test_every_position_survives_empty_and_spaced_values(tmp_path, pos):
    """The script's own encode/decode, through `sh -c` (what ssh's remote
    shell does): an empty value and values with spaces, quotes, $, newlines
    and globs at every position arrive in place."""
    enc, dec = _transport_block(open(SCRIPT).read())
    values = [f"v{i}" for i in range(N_REMOTE_ARGS)]
    values[pos] = ""
    values[(pos + 1) % N_REMOTE_ARGS] = "a b  c"
    values[(pos + 2) % N_REMOTE_ARGS] = "q'uo\"te $HOME *\nline2"
    harness = tmp_path / "t.sh"
    harness.write_text(
        "set -euo pipefail\nREMOTE_ARGS=(\"$@\")\n" + enc + "\n"
        'sh -c "bash -s -- $PARAMS_SHA $PARAMS_B64" <<\'REMOTE\'\n'
        "set -euo pipefail\n" + dec + "\n"
        'printf "%s\\0" "$@"\nREMOTE\n')
    out = subprocess.run(["bash", str(harness), *values], capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.decode().split("\0")[:-1] == values


# ---------------------------------------------------------------- G1: the graph UI's publish and token

class TestGraphPublish:
    def test_published_only_on_the_tailscale_address(self, deploy):
        proc, calls, _ = deploy()
        run = _new_container_run(calls)
        ports = _flag_values(run, "-p")
        assert "100.64.0.10:8766:8765" in ports
        assert not [p for p in ports if p.endswith(":8765") and not p.startswith("100.64.0.10:")]
        assert "graph=100.64.0.10:8766" in proc.stdout

    def test_the_graph_token_is_minted_once_0600_and_passed_as_a_file(self, deploy):
        proc, calls, _ = deploy()
        tok = deploy.secrets / "graph.token"
        assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600
        value = tok.read_text()
        assert len(value) == 48 and value.isalnum()
        envs = _flag_values(_new_container_run(calls), "-e")
        assert f"MEMORA_GRAPH_TOKEN_FILE={SECRETS_MOUNT}/graph.token" in envs
        assert not [e for e in envs if e.startswith("MEMORA_GRAPH_TOKEN=")]
        assert value not in _all_argv(calls) + proc.stdout + proc.stderr
        deploy()
        assert tok.read_text() == value  # never re-minted

    def test_a_graph_token_equal_to_the_health_token_is_refused_before_the_stop(self, deploy):
        tok = deploy.secrets / "graph.token"
        tok.write_text("h" * 48)
        os.chmod(tok, 0o600)
        proc, calls, _ = deploy()
        assert proc.returncode != 0 and "the graph token equals the health or admin token" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)

    def test_a_group_readable_graph_token_is_refused_before_anything(self, deploy):
        tok = deploy.secrets / "graph.token"
        tok.write_text("q" * 48)
        os.chmod(tok, 0o640)
        proc, calls, _ = deploy()
        _secrets_refused(deploy, proc, calls, "mode 0600")

    @pytest.mark.parametrize("env", [{"DEPLOY_GRAPH_BIND": "127.0.0.1"}, {"DEPLOY_GRAPH_PORT": "9999"}])
    def test_the_graph_publish_is_not_overridable_without_the_sentinel(self, deploy, env):
        proc, calls, _ = deploy(runtime_env=env)
        _nothing_done(deploy, proc, calls)
        assert "overrides are for rehearsals only" in proc.stderr

    @pytest.mark.parametrize("bind, match", [("0.0.0.0", "every interface"), ("deploy-host", "not an IPv4 address"),
                                             ("", "not an IPv4 address")])
    def test_the_graph_is_never_published_on_all_interfaces(self, deploy, tmp_path, bind, match):
        rcfg = tmp_path / "rcfg"
        rcfg.mkdir()
        env = {**REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(tmp_path),
               "DEPLOY_LABELS": "memora.rehearsal=rh-t", "DEPLOY_CONFIG_DIR": str(rcfg),
               "DEPLOY_SECRETS_DIR": str(tmp_path / "rsec"), "DEPLOY_ENV_FILE": str(deploy.env_file),
               "DEPLOY_GRAPH_BIND": bind, "DEPLOY_GRAPH_PORT": "18766"}
        if bind == "":
            env["DEPLOY_GRAPH_BIND"] = " "
        proc, calls, _ = deploy(runtime_env=env)
        _nothing_done(deploy, proc, calls)
        assert match in proc.stderr or "DEPLOY_ENV_FILE is not under" in proc.stderr

    def test_a_rehearsal_may_not_use_the_production_graph_port(self, deploy, tmp_path):
        rcfg = tmp_path / "rcfg"
        rcfg.mkdir()
        proc, calls, _ = deploy(runtime_env={**REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(tmp_path),
                                             "DEPLOY_LABELS": "memora.rehearsal=rh-t", "DEPLOY_CONFIG_DIR": str(rcfg),
                                             "DEPLOY_GRAPH_BIND": "127.0.0.1", "DEPLOY_GRAPH_PORT": "8766"})
        _nothing_done(deploy, proc, calls)
        assert "DEPLOY_GRAPH_PORT 8766 is production's" in proc.stderr


# ---------------------------------------------------------------- E1b: Preflight 3, the embedding preflight

class TestEmbeddingPreflight:
    def _create(self, calls):
        creates = [c for c in calls if c[0] == "create"]
        assert len(creates) == 1, creates
        return creates[0]

    def test_it_runs_in_the_new_image_with_the_new_env_before_the_stop(self, deploy):
        proc, calls, _ = deploy()
        c = self._create(calls)
        assert c[-3:] == ["python", "-m", "memora.embedding_preflight"] and c[-4] == "memora:latest"
        assert c[c.index("--name") + 1].startswith("memora-all-embedpf-")
        assert f"{ANON}:/data" in _flag_values(c, "-v")  # the RUNNING container's volume, read-write
        assert not [v for v in _flag_values(c, "-v") if v.endswith("/data:ro")]
        assert f"{deploy.secrets}:{SECRETS_MOUNT}:ro" in _flag_values(c, "-v")
        assert "--rm" not in c
        # exactly the new container's environment
        assert sorted(_flag_values(c, "-e")) == sorted(_flag_values(_new_container_run(calls), "-e"))
        i_create = _index(calls, lambda x: x[0] == "create")
        i_start = _index(calls, lambda x: x[:2] == ["start", "-a"])
        i_rm = _index(calls, lambda x: x == ["rm", "pf0123456789"])
        i_stop = _index(calls, lambda x: x[0] == "stop")
        assert i_create < i_start < i_rm < i_stop
        assert calls[i_start] == ["start", "-a", "pf0123456789"]

    def test_the_preflight_cannot_swallow_the_rest_of_the_deploy(self, deploy):
        """podman's `start -a` reads stdin; the remote script IS bash's stdin
        (rehearsal on server2): without `< /dev/null` the deploy ended there."""
        proc, calls, _ = deploy()
        assert any(c[:2] == ["run", "-d"] for c in calls), proc.stderr[-800:]
        assert ["rm", "pf0123456789"] in calls

    def test_a_store_that_would_refuse_searches_refuses_before_the_stop(self, deploy):
        proc, calls, _ = deploy(runtime_env={"EMBED_PF_RC": "2"})
        assert proc.returncode != 0 and "embedding preflight refused (exit 2)" in proc.stderr
        assert "would refuse semantic search" in proc.stderr
        assert not any(c[0] in ("stop", "rename") or c[:2] == ["run", "-d"] for c in calls)
        assert ["rm", "pf0123456789"] in calls  # removed by its ID even on a refusal

    @pytest.mark.parametrize("rc", ["1", "127"])
    def test_any_other_failure_refuses_too(self, deploy, rc):
        proc, calls, _ = deploy(runtime_env={"EMBED_PF_RC": rc})
        assert proc.returncode != 0 and f"embedding preflight refused (exit {rc})" in proc.stderr
        assert not any(c[0] == "stop" for c in calls)

    def test_a_failed_create_refuses_before_the_stop(self, deploy):
        proc, calls, _ = deploy(runtime_env={"CREATE_RC": "125"})
        assert proc.returncode != 0 and "cannot create the embedding preflight container" in proc.stderr
        assert not any(c[0] in ("start", "stop") for c in calls)

# ---------------------------------------------------------------- CFG1: infrastructure values come from deploy.env

def test_the_production_values_come_from_the_deploy_configuration(deploy, tmp_path):
    """The host, the graph address and the checkout are the configured ones,
    and a different configured host is what ssh is called with."""
    cfg = dict(DEPLOY_CFG, DEPLOY_HOST="other-host", DEPLOY_GRAPH_BIND="100.64.0.77")
    deploy.config_file.write_text(_deploy_env_text(cfg))
    proc, calls, _ = deploy()
    assert "deploy target: host=other-host " in proc.stdout, proc.stderr[-800:]
    assert "graph=100.64.0.77:8766" in proc.stdout
    assert deploy.tools.read_text().splitlines()[0] == "ssh other-host"
    params = _decode_blob((tmp_path / "ssh-command.txt").read_text().strip())
    assert params[9] == "~/repos/agentic-box/memora" and params[21] == "100.64.0.77"


@pytest.mark.parametrize("problem, match", [
    ("missing", "is missing or incomplete"),
    ("no-host", "is missing or incomplete"),
    ("no-bind", "is missing or incomplete"),
    ("no-repo", "is missing or incomplete"),
    ("no-projects", "is missing or incomplete"),
    ("empty-host", "is missing or incomplete"),
    ("unknown-key", "is missing or incomplete"),
    ("not-key-value", "is missing or incomplete"),
    ("bad-projects", "MEMORA_PROJECTS in"),
])
def test_the_deploy_refuses_without_a_complete_configuration(deploy, problem, match):
    """CFG1: no guessed default -- a missing file, a missing, empty or unknown
    key, or a malformed line refuses before anything runs."""
    cfg = dict(DEPLOY_CFG)
    text = None
    if problem == "missing":
        deploy.config_file.unlink()
    elif problem.startswith("no-"):
        cfg.pop({"no-host": "DEPLOY_HOST", "no-bind": "DEPLOY_GRAPH_BIND", "no-repo": "DEPLOY_REPO",
                 "no-projects": "MEMORA_PROJECTS"}[problem])
    elif problem == "empty-host":
        cfg["DEPLOY_HOST"] = ""
    elif problem == "unknown-key":
        text = _deploy_env_text(cfg) + "DEPLOY_HOTS=typo\n"
    elif problem == "not-key-value":
        text = _deploy_env_text(cfg) + "export DEPLOY_HOST\n"
    elif problem == "bad-projects":
        cfg["MEMORA_PROJECTS"] = '["memora"]'
    if problem != "missing":
        deploy.config_file.write_text(text if text is not None else _deploy_env_text(cfg))
    proc, calls, _ = deploy()
    _nothing_done(deploy, proc, calls)
    assert match in proc.stderr and "nothing was done" in proc.stderr, proc.stderr[-800:]


def test_a_config_file_override_needs_the_rehearsal_sentinel(deploy, tmp_path):
    other = tmp_path / "elsewhere.env"
    other.write_text(deploy.config_file.read_text())
    proc, calls, _ = deploy(runtime_env={"DEPLOY_CONFIG_FILE": str(other)})
    _nothing_done(deploy, proc, calls)
    assert "overrides are for rehearsals only" in proc.stderr and "DEPLOY_CONFIG_FILE" in proc.stderr


def test_a_rehearsal_config_file_outside_the_root_is_refused(deploy, tmp_path):
    root = tmp_path / "rh"
    (root / "cfg").mkdir(parents=True)
    envf = root / "all.env"
    envf.write_text(f"MEMORA_DATABASES='{json.dumps(REGISTRY)}'\n")
    proc, calls, _ = deploy(runtime_env={**REHEARSAL, "DEPLOY_REHEARSAL": "1", "DEPLOY_REHEARSAL_ROOT": str(root),
                                         "DEPLOY_LABELS": "memora.rehearsal=r", "DEPLOY_CONFIG_DIR": str(root / "cfg"),
                                         "DEPLOY_SECRETS_DIR": str(root / "sec"), "DEPLOY_ENV_FILE": str(envf)})
    _nothing_done(deploy, proc, calls)
    assert "DEPLOY_CONFIG_FILE is not under" in proc.stderr
