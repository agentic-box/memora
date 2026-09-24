"""X3: scripts/lp_container.sh runs the operator tool in a one-off
container of memora-all's CURRENT image, without the host's docker, and
removes only its own named container (never --rm, never a volume)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "lp_container.sh"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ["CALL_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps(args) + "\n")
state = os.environ["STATE"]
if args[:2] == ["inspect", "-f"] and args[3:] == [os.environ.get("SERVICE", "memora-all")]:
    if os.environ.get("INSPECT_RC"):
        sys.exit(int(os.environ["INSPECT_RC"]))
    print({"{{.Image}}": "sha256:1mage", "{{.Config.User}}": os.environ.get("RUN_USER", "")}[args[2]])
    sys.exit(0)
if args[:2] == ["inspect", "-f"] and "memora.lp.run" in args[2]:
    if not os.path.exists(state):
        sys.exit(1)
    label = os.environ.get("FOREIGN_LABEL") or json.load(open(state))["label"]
    print(label)
    sys.exit(0)
if args and args[0] == "run":
    name = args[args.index("--name") + 1]
    label = args[args.index("--label") + 1].split("=", 1)[1]
    json.dump({"name": name, "label": label}, open(state, "w"))
    sys.exit(int(os.environ.get("RUN_RC", "0")))
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
           "STATE": str(tmp_path / "state.json")}

    def run(*args, **extra):
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                           env={**env, **{k: str(v) for k, v in extra.items()}})
        log = tmp_path / "calls"
        calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
        return r.returncode, calls, r.stderr

    run.tok = tok
    return run


TOOL = ["rollback", "re", "--phase", "verify", "--store", "/data/re.db", "--lock-barrier"]


def _run_call(calls):
    runs = [c for c in calls if c[0] == "run"]
    assert len(runs) == 1
    return runs[0]


def test_it_runs_the_tool_in_memora_alls_current_image_without_docker_or_rm(rt):
    code, calls, err = rt(*TOOL, RUN_USER="1000:1000")
    assert code == 0, err
    run = _run_call(calls)
    assert "--rm" not in run
    joined = " ".join(run)
    assert "docker.sock" not in joined and "/var/run/docker" not in joined
    vols = [run[i + 1] for i, a in enumerate(run) if a == "-v"]
    assert vols == ["memora-all-data:/data", f"{rt.tok}:/run/secrets/memora:ro"]
    image_at = run.index("sha256:1mage")  # the image ID memora-all runs, not a tag
    assert run[image_at + 1:image_at + 3][0] == "-c" and run[image_at + 3] == "lp"
    assert run[image_at + 4:] == TOOL, "the tool's arguments pass through unchanged"
    assert run[run.index("--user") + 1] == "1000:1000"
    assert run[run.index("--entrypoint") + 1] == "sh"
    name = run[run.index("--name") + 1]
    assert name.startswith("memora-lp-") and run[run.index("--label") + 1] == f"memora.lp.run={name}"
    rms = [c for c in calls if c[:2] == ["rm", "-f"]]
    assert rms == [["rm", "-f", name]], "removes exactly its own container, by name"
    assert not any(c[0] in ("volume", "rmi", "system") for c in calls)


def test_the_tools_exit_status_is_kept_and_the_container_still_removed(rt):
    code, calls, _ = rt(*TOOL, RUN_RC=3)
    assert code == 3
    name = _run_call(calls)[_run_call(calls).index("--name") + 1]
    assert ["rm", "-f", name] in calls


def test_a_container_that_does_not_carry_this_runs_label_is_left_alone(rt):
    code, calls, err = rt(*TOOL, FOREIGN_LABEL="someone-else")
    assert code == 0 and not [c for c in calls if c[:2] == ["rm", "-f"]]
    assert "left in place" in err


def test_service_stopped_is_refused_inside_the_container(rt):
    code, calls, err = rt("restore", "re", "--service-stopped")
    assert code == 65 and "--lock-barrier" in err and calls == []


def test_a_missing_token_dir_is_a_usage_error(rt, tmp_path):
    code, calls, _ = rt(*TOOL, LP_TOKEN_DIR=str(tmp_path / "nope"))
    assert code == 65 and calls == []


def test_an_unreadable_service_image_stops_before_any_run(rt):
    code, calls, err = rt(*TOOL, INSPECT_RC=1)
    assert code == 66 and not [c for c in calls if c[0] == "run"] and "cannot read the image" in err


def test_no_user_flag_when_memora_all_runs_as_the_image_default(rt):
    code, calls, _ = rt(*TOOL)
    assert code == 0 and "--user" not in _run_call(calls)


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
