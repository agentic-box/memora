"""X3: scripts/lp_container.sh runs the operator tool in a one-off
container of memora-all's CURRENT image, without the host's docker. It is
authoritative for memora-all's service state (checked before and after
the run) and removes only the container it created, by its captured ID
after rechecking the label (never --rm, never a volume)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "lp_container.sh"
ROUTES = {"gamma": "/data/gamma.db", "other": "d1://acct/db"}

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state = os.environ["STATE"]
st = json.load(open(state)) if os.path.exists(state) else {"running_calls": 0}
def save():
    json.dump(st, open(state, "w"))
service = os.environ.get("SERVICE", "memora-all")
if args[:2] == ["inspect", "-f"] and args[3:] == [service]:
    fmt = args[2]
    if os.environ.get("INSPECT_RC"):
        sys.exit(int(os.environ["INSPECT_RC"]))
    if fmt == "{{.State.Running}}":
        st["running_calls"] += 1
        save()
        key = "RUNNING_BEFORE" if st["running_calls"] == 1 else "RUNNING_AFTER"
        print(os.environ.get(key, os.environ.get("RUNNING_BEFORE", "false")))
    elif fmt == "{{.Image}}":
        print("sha256:1mage")
    elif fmt == "{{.Config.User}}":
        print(os.environ.get("RUN_USER", ""))
    elif "Config.Env" in fmt:
        for line in json.loads(os.environ.get("SERVICE_ENV", "[]")):
            print(line)
    elif ".Mounts" in fmt:
        print(os.environ.get("DATA_MOUNT", "memora-all-data"))
    sys.exit(0)
if args and args[0] == "create":
    st["name"] = args[args.index("--name") + 1]
    st["label"] = args[args.index("--label") + 1].split("=", 1)[1]
    save()  # on a failed create: the EXISTING container has this same name and label
    if os.environ.get("CREATE_RC"):
        print("Error: name already in use", file=sys.stderr)
        sys.exit(int(os.environ["CREATE_RC"]))
    print("cid-0123456789")
    sys.exit(0)
if args[:2] == ["start", "-a"]:
    sys.exit(int(os.environ.get("RUN_RC", "0")))
if args[:2] == ["inspect", "-f"] and "memora.lp.run" in args[2]:
    print(os.environ.get("FOREIGN_LABEL") or st.get("label", ""))
    sys.exit(0)
if args[:2] == ["rm", "-f"]:
    sys.exit(0)
sys.exit(97)
'''


@pytest.fixture
def rt(tmp_path):
    fake = tmp_path / "fake-docker"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    tok = tmp_path / "secrets"
    tok.mkdir()
    env = {**os.environ, "LP_RUNTIME": str(fake), "LP_TOKEN_DIR": str(tok), "CALL_LOG": str(tmp_path / "calls"),
           "STATE": str(tmp_path / "state.json"),
           "SERVICE_ENV": json.dumps(["PATH=/usr/bin", f"MEMORA_DATABASES={json.dumps(ROUTES)}",
                                      "MEMORA_SERVICE_LOCK=1", "OTHER=1"])}

    def run(*args, **extra):
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                           env={**env, **{k: str(v) for k, v in extra.items()}})
        log = tmp_path / "calls"
        calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
        for f in ("calls", "state.json"):
            (tmp_path / f).unlink(missing_ok=True)
        return r.returncode, calls, r.stderr

    run.tok = tok
    return run


VERIFY = ["rollback", "gamma", "--phase", "verify", "--store", "/data/gamma.db", "--lock-barrier"]
FINISH = ["rollback", "gamma", "--phase", "finish", "--store", "/data/gamma.db", "--lock-barrier"]


def _create(calls):
    creates = [c for c in calls if c[0] == "create"]
    assert len(creates) == 1
    return creates[0]


def test_it_runs_the_tool_in_memora_alls_current_image_without_docker_or_rm(rt):
    code, calls, err = rt(*VERIFY, RUN_USER="1000:1000")
    assert code == 0, err
    create = _create(calls)
    assert not [c for c in calls if c[0] == "run"], "create + start, never run"
    assert "--rm" not in create
    joined = " ".join(create)
    assert "docker.sock" not in joined and "/var/run/docker" not in joined
    vols = [create[i + 1] for i, a in enumerate(create) if a == "-v"]
    assert vols == ["memora-all-data:/data", f"{rt.tok}:/run/secrets/memora:ro"]
    envs = [create[i + 1] for i, a in enumerate(create) if a == "-e"]
    assert envs == ["MEMORA_DATA_DIR=/data", "LP_SERVICE_DATA_DIR=/data",
                    f"MEMORA_DATABASES={json.dumps(ROUTES)}"], "memora-all's routing and data dir, nothing else"
    image_at = create.index("sha256:1mage")  # the image ID memora-all runs, not a tag
    assert create[image_at + 1] == "-c" and create[image_at + 3] == "lp"
    assert create[image_at + 4:] == VERIFY, "the tool's arguments pass through unchanged"
    assert create[create.index("--user") + 1] == "1000:1000"
    name = create[create.index("--name") + 1]
    assert name.startswith("memora-lp-") and create[create.index("--label") + 1] == f"memora.lp.run={name}"
    assert ["start", "-a", "cid-0123456789"] in calls
    rms = [c for c in calls if c[:2] == ["rm", "-f"]]
    assert rms == [["rm", "-f", "cid-0123456789"]], "removes exactly the created container, by its ID"
    assert not any(c[0] in ("volume", "rmi", "system") for c in calls)


@pytest.mark.parametrize("args, required", [
    (["restore", "gamma", "--out", "/data/gamma.db", "--lock-barrier"], "false"),
    (["resume", "gamma", "--store", "/data/gamma.db", "--lock-barrier"], "false"),
    (["sequence-highwater", "gamma", "--local", "/data/gamma.db", "--lock-barrier"], "false"),
    (VERIFY, "false"),
    (FINISH, "true"),
    (["rollback", "gamma", "--phase", "drain", "--store", "/data/gamma.db"], "true"),
])
def test_the_service_state_is_checked_before_anything_runs(rt, args, required):
    wrong = "true" if required == "false" else "false"
    code, calls, err = rt(*args, RUNNING_BEFORE=wrong)
    assert code == 67 and "nothing was run" in err
    assert not [c for c in calls if c[0] in ("create", "start", "rm")]
    code, calls, err = rt(*args, RUNNING_BEFORE=required)
    assert code == 0, err


def test_a_state_change_during_the_run_fails_loudly(rt):
    code, calls, err = rt(*VERIFY, RUNNING_BEFORE="false", RUNNING_AFTER="true")
    assert code == 68 and "changed during the run (false -> true)" in err
    assert ["rm", "-f", "cid-0123456789"] in calls, "still cleaned up"


def test_other_commands_do_not_require_a_state(rt):
    for running in ("true", "false"):
        code, _, err = rt("fk-audit", "gamma", "--store", "/data/gamma.db", RUNNING_BEFORE=running)
        assert code == 0, err


def test_a_failed_create_removes_nothing(rt):
    """A name collision with an existing container carrying the SAME name and
    label (review 7778 P1-2): no ID was captured, so nothing is removed --
    not by name, not by label."""
    code, calls, err = rt(*VERIFY, CREATE_RC=125)
    assert code == 69 and "nothing to clean up" in err
    assert not [c for c in calls if c[0] in ("rm", "start")]


def test_the_tools_exit_status_is_kept_and_the_container_still_removed(rt):
    code, calls, _ = rt(*VERIFY, RUN_RC=3)
    assert code == 3 and ["rm", "-f", "cid-0123456789"] in calls


def test_a_container_that_does_not_carry_this_runs_label_is_left_alone(rt):
    code, calls, err = rt(*VERIFY, FOREIGN_LABEL="someone-else")
    assert code == 0 and not [c for c in calls if c[:2] == ["rm", "-f"]]
    assert "left in place" in err


def test_service_stopped_is_refused_inside_the_container(rt):
    code, calls, err = rt("restore", "gamma", "--service-stopped")
    assert code == 65 and "--lock-barrier" in err and calls == []


def test_rollback_without_a_phase_is_a_usage_error(rt):
    code, calls, _ = rt("rollback", "gamma", "--store", "/data/gamma.db")
    assert code == 65 and calls == []


def test_a_missing_token_dir_is_a_usage_error(rt, tmp_path):
    code, calls, _ = rt(*VERIFY, LP_TOKEN_DIR=str(tmp_path / "nope"))
    assert code == 65 and calls == []


def test_an_uninspectable_service_stops_before_any_create(rt):
    code, calls, err = rt(*VERIFY, INSPECT_RC=1)
    assert code == 66 and not [c for c in calls if c[0] == "create"] and "cannot inspect" in err


def test_no_user_flag_when_memora_all_runs_as_the_image_default(rt):
    code, calls, _ = rt(*VERIFY)
    assert code == 0 and "--user" not in _create(calls)


def test_no_routing_passed_when_memora_all_has_none(rt):
    code, calls, _ = rt(*VERIFY, SERVICE_ENV=json.dumps(["PATH=/usr/bin", "MEMORA_SERVICE_LOCK=1"]))
    assert code == 0
    envs = [c for c in _create(calls)]
    assert not any(a.startswith("MEMORA_DATABASES=") for a in envs)


def test_the_in_container_program_refuses_an_image_without_the_tool(tmp_path):
    """The sh -c program the wrapper passes: exit 64 when the image predates
    scripts/local_primary.py."""
    text = SCRIPT.read_text()
    program = text.split("-c \\\n  '", 1)[1].split("' \\\n", 1)[0]
    r = subprocess.run(["sh", "-c", program.replace("/app/scripts/local_primary.py", str(tmp_path / "none.py")),
                        "lp", "x"], capture_output=True, text=True)
    assert r.returncode == 64 and "predates the operator tool" in r.stderr


def test_the_image_carries_the_operator_tool():
    assert "COPY scripts/local_primary.py scripts/local_primary.py" in (REPO / "Dockerfile").read_text()



# ------------------------------------------------------------------ review 7823: the service lock is the proof

@pytest.mark.parametrize("env", [["PATH=/usr/bin"], ["MEMORA_SERVICE_LOCK=0"], ["MEMORA_SERVICE_LOCK=10"]])
def test_a_stopped_required_run_needs_memora_all_to_hold_the_service_lock(rt, env):
    code, calls, err = rt(*VERIFY, SERVICE_ENV=json.dumps(env))
    assert code == 70 and "MEMORA_SERVICE_LOCK=1" in err
    assert not [c for c in calls if c[0] in ("create", "start")]


def test_a_running_required_run_does_not_need_the_service_lock_setting(rt):
    code, _, err = rt(*FINISH, RUNNING_BEFORE="true", SERVICE_ENV=json.dumps(["PATH=/usr/bin"]))
    assert code == 0, err


@pytest.mark.parametrize("mount", ["", "other-volume"])
def test_memora_all_must_mount_this_data_volume(rt, mount):
    code, calls, err = rt(*VERIFY, DATA_MOUNT=mount)
    assert code == 70 and "at /data, not memora-all-data" in err
    assert not [c for c in calls if c[0] in ("create", "start")]


def test_the_image_takes_the_service_lock():
    assert "MEMORA_SERVICE_LOCK=1" in (REPO / "Dockerfile").read_text()



@pytest.mark.parametrize("value, code", [("/tmp", 70), ("/data/", 70), ("/data", 0), (None, 0)])
def test_memora_alls_data_dir_must_be_data_for_a_stopped_required_run(rt, value, code):
    env = ["PATH=/usr/bin", "MEMORA_SERVICE_LOCK=1"] + ([f"MEMORA_DATA_DIR={value}"] if value is not None else [])
    got, calls, err = rt(*VERIFY, SERVICE_ENV=json.dumps(env))
    assert got == code, err
    if code == 70:
        assert "not /data" in err and not [c for c in calls if c[0] == "create"]
    else:
        create = _create(calls)
        assert "LP_SERVICE_DATA_DIR=/data" in create
