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
    (old / "freeze" / "ob1").write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool_log = tmp_path / "tools.txt"
    _exe(bin_dir / "ssh", f'#!/bin/bash\necho "ssh $1" >> "{tool_log}"\nshift\nexec "$@"\n')
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
    """Unparameterised: ssh to nuc8, docker, the v0.5.0 checkout, memora-all
    on 8920 with memora-all-data and memora:latest, the tokens from
    ~/.config/memora-lp."""
    proc, calls, cfg = deploy()
    tools = deploy.tools.read_text().splitlines()
    assert tools[0] == "ssh nuc8"
    assert ("deploy target: host=nuc8 runtime=docker container=memora-all volume=memora-all-data "
            "image=memora:latest port=8920 tag=v0.5.0 secrets=~/.config/memora-lp") in proc.stdout
    assert any(t.startswith("git ") and "checkout v0.5.0" in t for t in tools)
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
             "DEPLOY_SKIP_CHECKOUT": "1", "DEPLOY_SMOKE_ABSORB": "0"}


def _nothing_done(deploy, proc, calls):
    assert proc.returncode != 0
    assert calls == [], "no runtime call"
    assert not deploy.tools.exists() or not deploy.tools.read_text(), "no ssh or git"


@pytest.mark.parametrize("override", [
    {"RUNTIME": "podman"}, {"DEPLOY_PORT": "18920"}, {"DEPLOY_CONTAINER": "memora-rh"},
    {"DEPLOY_HOST": "localhost"}, {"DEPLOY_SKIP_CHECKOUT": "1"}, {"DEPLOY_TAG": "v9"},
    {"DEPLOY_LABELS": "memora.rehearsal=x"}, {"DEPLOY_SECRETS_DIR": "/tmp/tokens"},
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
    ({"DEPLOY_HOST": "nuc8"}, "DEPLOY_HOST must be localhost"),
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

LOCAL_REGISTRY = {**REGISTRY, "re": "/data/re.db"}


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
        replicas = json.dumps({"re": "d1://acct/db3"})
        _env_file(deploy, MEMORA_REPLICAS=replicas, MEMORA_REPLICATION="write")
        proc, calls, _ = deploy()
        envs = _flag_values(_new_container_run(calls), "-e")
        assert f"MEMORA_REPLICAS={replicas}" in envs and "MEMORA_REPLICATION=write" in envs
        assert "local-primary: MEMORA_REPLICAS=['re'] MEMORA_REPLICATION=write" in proc.stdout

    def test_file_uri_registry_entry_counts_as_local(self, deploy):
        deploy.env_file.write_text(
            f"MEMORA_DATABASES='{json.dumps({**REGISTRY, 're': 'file:///data/re.db'})}'\n"
            f"MEMORA_REPLICAS='{json.dumps({'re': 'd1://acct/db3'})}'\nMEMORA_REPLICATION=log\n")
        proc, calls, _ = deploy()
        assert "MEMORA_REPLICATION=log" in _flag_values(_new_container_run(calls), "-e")

    @pytest.mark.parametrize("extra, match", [
        ({"MEMORA_REPLICATION": "on"}, "must be log or write"),
        ({"MEMORA_REPLICAS": '{"nope": "d1://a/b"}'}, "not a store of MEMORA_DATABASES"),
        ({"MEMORA_REPLICAS": '{"memora": "d1://acct/db1"}'}, "a replicated store must be a local path"),
        ({"MEMORA_REPLICAS": '{"re": "re"}'}, "must be d1://account/database"),
        ({"MEMORA_REPLICAS": '"re"'}, "must be a non-empty JSON object"),
        ({"MEMORA_REPLICAS": "{}"}, "must be a non-empty JSON object"),
        ({"MEMORA_REPLICAS": "re"}, "Expecting value"),
    ])
    def test_a_bad_value_refuses_before_anything(self, deploy, extra, match):
        _env_file(deploy, **extra)
        proc, calls, _ = deploy()
        _nothing_done(deploy, proc, calls)
        assert match in proc.stderr and "nothing was done" in proc.stderr


class TestReplicationTimingPassthrough:
    def test_present_values_are_passed_through(self, deploy):
        _env_file(deploy, MEMORA_REPLICAS=json.dumps({"re": "d1://acct/db3"}), MEMORA_REPLICATION="write",
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
