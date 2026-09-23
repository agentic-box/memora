"""scripts/deploy-memora-all.sh: the named /data volume and the admin token
(local-primary plan §8 L2a, §9 (a)).

The script is run for real with ssh, docker, git and curl replaced by fakes on
PATH: ssh runs the remote heredoc locally, docker records every call. Nothing
touches nuc8 or a real runtime. The script's final smoke check talks HTTP to
127.0.0.1:8920 and fails here (no server); every assertion is about the
docker calls made before it.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "deploy-memora-all.sh")
ANON = "3f" * 32
REGISTRY = {"memora": "d1://acct/db1", "ob1": "d1://acct/db2"}

FAKE_DOCKER = r"""#!/bin/bash
# One record per call: args separated by \x1f, records by \x1e (argv may
# hold newlines, e.g. a python -c body).
{ printf '%s\x1f' "$@"; printf '\x1e'; } >> "$CALL_LOG"
case "$1 $2" in
  "inspect memora-all") echo "$OLD_VOLUME"; exit 0 ;;
  "volume inspect")
    [ -e "$VOL_STATE" ] || exit 1
    [ "${4:-}" = --format ] && echo "$3"
    exit 0 ;;
  "volume create") touch "$VOL_STATE"; echo "$3"; exit 0 ;;
  "exec -i") cat >/dev/null; exit 0 ;;
esac
if [ "$1" = run ] && [ "$2" = --rm ]; then
  case "$*" in
    *"test -e /to/.memora-copied-from-previous-volume"*) exit "${COPY_DONE_RC:-1}" ;;
    *"cp -a /from/. /to/"*) exit "${COPY_RC:-0}" ;;
  esac
fi
exit 0
"""


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def deploy(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "instances").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "deploy-memora-all.sh")
    (repo / "instances" / "all.env").write_text(f"MEMORA_DATABASES='{json.dumps(REGISTRY)}'\n")

    home = tmp_path / "home"
    (home / "repos" / "agentic-box" / "memora").mkdir(parents=True)
    cfg = home / ".config" / "memora"
    cfg.mkdir(parents=True)
    (cfg / "all.health-token").write_text("h" * 48)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _exe(bin_dir / "ssh", '#!/bin/bash\nshift\nexec "$@"\n')
    _exe(bin_dir / "docker", FAKE_DOCKER)
    _exe(bin_dir / "git", "#!/bin/bash\nexit 0\n")
    _exe(bin_dir / "curl", "#!/bin/bash\nexit 0\n")

    def run(cred_env=None, old_volume=ANON, volume_exists=False, copy_done=False,
            copy_rc=0, admin_token=None):
        (cfg / "credentials.mcp.json").write_text(json.dumps({"mcpServers": {"memora": {
            "env": cred_env or {"CLOUDFLARE_API_TOKEN": "tok"}}}}))
        if admin_token is not None:
            (cfg / "all.admin-token").write_text(admin_token)
        state = tmp_path / "volume-state"
        if volume_exists:
            state.touch()
        log = tmp_path / "calls.txt"
        env = dict(os.environ, HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}",
                   CALL_LOG=str(log), OLD_VOLUME=old_volume, VOL_STATE=str(state),
                   COPY_DONE_RC="0" if copy_done else "1", COPY_RC=str(copy_rc))
        proc = subprocess.run(["bash", str(repo / "scripts" / "deploy-memora-all.sh")],
                              env=env, capture_output=True, text=True, timeout=120)
        raw = log.read_text() if log.exists() else ""
        calls = [r.split("\x1f")[:-1] for r in raw.split("\x1e") if r]
        return proc, calls, cfg

    return run


def _new_container_run(calls):
    runs = [c for c in calls if c[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one container run, got {runs}"
    return runs[0]


def _index(calls, pred):
    return next(i for i, c in enumerate(calls) if pred(c))


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

    stop = _index(calls, lambda c: c == ["stop", "memora-all"])
    create = _index(calls, lambda c: c[:3] == ["volume", "create", "memora-all-data"])
    copy = _index(calls, lambda c: c[:2] == ["run", "--rm"] and "cp -a /from/. /to/" in " ".join(c))
    rename = _index(calls, lambda c: c[0] == "rename")
    new = calls.index(run)
    assert create < stop < copy < rename < new, \
        "the volume is made before the stop; the copy runs while memora-all is stopped"
    assert f"{ANON}:/from:ro" in calls[copy] and "memora-all-data:/to" in calls[copy]


def test_no_copy_once_memora_all_mounts_the_named_volume(deploy):
    _, calls, _ = deploy(old_volume="memora-all-data", volume_exists=True)
    assert not any("cp -a" in " ".join(c) for c in calls)
    assert _flag_values(_new_container_run(calls), "-v") == ["memora-all-data:/data"]


def test_no_second_copy_when_an_earlier_copy_completed(deploy):
    _, calls, _ = deploy(volume_exists=True, copy_done=True)
    assert not any("cp -a" in " ".join(c) for c in calls)
    _new_container_run(calls)


def test_a_failed_copy_stops_before_rename_and_run(deploy):
    proc, calls, _ = deploy(copy_rc=1)
    assert proc.returncode != 0
    assert "copy" in proc.stderr and "docker start memora-all" in proc.stderr
    assert not any(c[0] == "rename" for c in calls)
    assert not any(c[:2] == ["run", "-d"] for c in calls)


def test_admin_token_equal_to_health_token_refuses_before_stop(deploy):
    proc, calls, _ = deploy(admin_token="h" * 48)
    assert proc.returncode != 0
    assert "equals the health token" in proc.stderr
    assert not any(c[0] == "stop" for c in calls)


def test_an_unusable_admin_token_is_refused_not_replaced(deploy):
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


def test_a_volume_that_does_not_resolve_refuses_before_stop(deploy, tmp_path):
    """A runtime that cannot create or name the volume must fail with the old
    container still serving."""
    import pathlib
    fake = pathlib.Path(tmp_path / "bin" / "docker")
    fake.write_text(fake.read_text().replace('"volume create") touch "$VOL_STATE"',
                                             '"volume create") :'))
    proc, calls, _ = deploy()
    assert proc.returncode != 0
    assert not any(c[0] == "stop" for c in calls)
