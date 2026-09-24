"""scripts/deploy_config.py and the scripts that need it (CFG1): the real
infrastructure values come only from the git-ignored instances/deploy.env,
and a script refuses rather than guess when a value is missing."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "scripts" / "deploy_config.py"
sys.path.insert(0, str(REPO / "scripts"))
import deploy_config  # noqa: E402


def _run(path, *keys):
    return subprocess.run([sys.executable, str(TOOL), str(path), *keys], capture_output=True)


def test_values_come_back_in_order_nul_terminated(tmp_path):
    f = tmp_path / "deploy.env"
    f.write_text("# a comment\n\nDEPLOY_HOST=host-a\nDEPLOY_GRAPH_BIND='100.64.0.10'\n"
                 "MEMORA_PROJECTS='{\"memora\": [\"memora\"]}'\n")
    r = _run(f, "DEPLOY_GRAPH_BIND", "DEPLOY_HOST", "MEMORA_PROJECTS")
    assert r.returncode == 0
    assert r.stdout.split(b"\0") == [b"100.64.0.10", b"host-a", b'{"memora": ["memora"]}', b""]


@pytest.mark.parametrize("text, why", [
    (None, "cannot read"),
    ("DEPLOY_HOST=\n", "missing DEPLOY_HOST"),
    ("DEPLOY_GRAPH_BIND=1.2.3.4\n", "missing DEPLOY_HOST"),
    ("DEPLOY_HOST=a\nDEPLOY_HOST=b\n", "set twice"),
    ("DEPLOY_HOTS=a\n", "unknown key"),
    ("export DEPLOY_HOST=a\n", "not KEY=VALUE"),
])
def test_anything_wrong_prints_nothing_and_exits_2(tmp_path, text, why):
    f = tmp_path / "deploy.env"
    if text is not None:
        f.write_text(text)
    r = _run(f, "DEPLOY_HOST")
    assert r.returncode == 2 and r.stdout == b"" and why in r.stderr.decode()


def test_nothing_is_expanded_or_executed(tmp_path):
    f = tmp_path / "deploy.env"
    marker = tmp_path / "ran"
    f.write_text(f"DEPLOY_HOST=$(touch {marker})\n")
    r = _run(f, "DEPLOY_HOST")
    assert r.stdout == f"$(touch {marker})\0".encode() and not marker.exists()


def test_an_unknown_requested_key_is_refused(tmp_path):
    f = tmp_path / "deploy.env"
    f.write_text("DEPLOY_HOST=a\n")
    assert _run(f, "DEPLOY_HOST", "NOPE").returncode == 2


def test_the_example_lists_every_key():
    cfg = deploy_config.load(REPO / "instances" / "deploy.env.example")
    assert set(cfg) == set(deploy_config.KEYS)


def test_switch_embedding_host_refuses_without_the_configuration(tmp_path):
    """No ssh, no credential edit: without DEPLOY_HOST / EMBEDDING_* the
    one-shot switch refuses before anything else."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "instances").mkdir()
    shutil.copy(REPO / "scripts" / "switch-embedding-host.sh", repo / "scripts")
    shutil.copy(TOOL, repo / "scripts")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "ssh-called"
    (bin_dir / "ssh").write_text(f"#!/bin/sh\ntouch {called}\nexit 0\n")
    (bin_dir / "ssh").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    for text in (None, "DEPLOY_HOST=h\n", "DEPLOY_HOST=h\nEMBEDDING_OLD_URL=http://a/v1\n"):
        f = repo / "instances" / "deploy.env"
        if text is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(text)
        r = subprocess.run(["bash", str(repo / "scripts" / "switch-embedding-host.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and "refused: instances/deploy.env lacks" in r.stderr, r.stderr
        assert not called.exists()
