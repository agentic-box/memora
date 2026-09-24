"""v0.5.1 hotfix (leader 7945): the deploy's import_attempt preflight reads
every store READ-ONLY. It runs as a second process next to the running
memora-all, which holds a live local primary's writer lock (primary lock)
and its WAL; a writer open there is refused (StoreLockedError), which
aborted the production deploy. The preflight program is taken verbatim from
scripts/deploy-memora-all.sh and run against a real local primary whose lock
another process holds."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "deploy-memora-all.sh"


def _preflight_program(script_text: str) -> str:
    """The python program the deploy pipes into `$RT exec -i $CONTAINER python -`."""
    lines = script_text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith('"$RT" exec -i "$CONTAINER" python - <<\'PY\''))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "PY")
    return "\n".join(lines[start + 1:end]) + "\n"


HOLDER = """
import sys, time
from memora import schema, storage
other = storage.backend_for("other").connect()
schema.ensure_schema(other)
other.commit()
other.close()
conn = storage.backend_for("gamma").connect()        # memora-all's writer: primary lock + WAL
schema.ensure_schema(conn)
conn.commit()
conn.execute("INSERT INTO memories (content, metadata) VALUES ('held', ?)", (sys.argv[1],))
conn.commit()
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture
def live_primary(tmp_path):
    """`gamma` as a live local primary (in MEMORA_REPLICAS) plus a plain local
    store; a holder process owns gamma's primary lock and keeps its WAL open."""
    reg = {"gamma": str(tmp_path / "gamma.db"), "other": str(tmp_path / "other.db")}
    env = dict(os.environ, MEMORA_DATABASES=json.dumps(reg), MEMORA_DEFAULT_DB="other",
               MEMORA_REPLICAS=json.dumps({"gamma": "d1://acct/gamma"}), MEMORA_DATA_DIR=str(tmp_path / "data"),
               PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    env.pop("MEMORA_REPLICATION", None)
    procs = []

    def start(holder_metadata="{}"):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, holder_metadata], env=env, cwd=str(tmp_path),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(p)
        assert p.stdout.readline().strip() == "ready", p.stderr.read()
        return p

    def preflight(program):
        return subprocess.run([sys.executable, "-"], input=program, env=env, cwd=str(tmp_path),
                              capture_output=True, text=True, timeout=60)

    live_primary.env = env
    yield start, preflight, tmp_path
    for p in procs:
        p.kill()
        p.wait()


def test_a_live_primary_is_inspected_without_its_writer_lock(live_primary):
    start, preflight, tmp = live_primary
    start()
    assert Path(f"{tmp / 'gamma.db'}.primary-lock").exists()
    out = preflight(_preflight_program(SCRIPT.read_text()))
    assert out.returncode == 0, out.stderr
    assert "gamma: 0 row(s) with import_attempt in metadata" in out.stdout
    assert "other: 0 row(s) with import_attempt in metadata" in out.stdout


def test_an_import_attempt_row_in_the_live_primary_is_still_refused(live_primary):
    start, preflight, _ = live_primary
    start(json.dumps({"import_attempt": {"id": "x"}}))
    out = preflight(_preflight_program(SCRIPT.read_text()))
    assert out.returncode != 0
    assert "gamma: 1 row(s) with import_attempt in metadata" in out.stdout
    assert "rows the startup sweep could complete or remove" in out.stderr


def test_the_old_writer_open_is_what_production_hit(live_primary):
    """Negative control: the v0.5.1 program's writer open, on the same setup,
    fails with StoreLockedError -- the production abort."""
    start, preflight, _ = live_primary
    start()
    old = _preflight_program(SCRIPT.read_text()).replace(
        '    conn = getattr(backend, "connect_read_only", backend.connect)()',
        "    conn = backend.connect()")
    assert "backend.connect()" in old
    out = preflight(old)
    assert out.returncode != 0 and "StoreLockedError" in out.stderr


def test_the_preflight_opens_no_writer_anywhere():
    program = _preflight_program(SCRIPT.read_text())
    assert "connect_read_only" in program
    assert ".connect()" not in program.replace("backend.connect)()", "")
