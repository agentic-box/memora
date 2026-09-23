"""Slice L1b: retired D1-writing tools are inert, and the D1 write guard works.

docs/local-primary-implementation.md §0 P6, §6 F2/F3. Every test is offline:
the external commands a script could reach (npx, wrangler, curl, node, npm,
python for sync.sh) are replaced on PATH by recorders that log their argv and
exit 0, so a test can prove a refused path never reached them.

Strings that the guard itself looks for are assembled from pieces in this
file, so the repo-wide guard run does not flag the test's own source.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GRAPH = REPO / "memora-graph"
GUARD = REPO / "scripts" / "d1_write_guard.py"
POINTER = "docs/local-primary-implementation.md"

WR = "wrangler"
D1 = " d1 "
REMOTE = "--" + "remote"


def _recorders(tmp_path: Path, names) -> tuple[Path, Path]:
    """A bin dir whose commands append their argv to a log and exit 0."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")
    for name in names:
        exe = bindir / name
        exe.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
                       'case "$*" in *"d1 list"*) echo memora-graph ;; esac\nexit 0\n')
        exe.chmod(0o755)
    return bindir, log


def _env(bindir: Path, *, real_python: bool = True) -> dict:
    parts = [str(bindir)]
    if real_python:
        parts.append(str(Path(sys.executable).parent))
    parts += ["/usr/bin", "/bin"]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLOUDFLARE", "CF_", "AWS_", "MEMORA_"))}
    env["PATH"] = os.pathsep.join(parts)
    return env


def _imported(stderr: str, module: str) -> bool:
    # -X importtime lines end with "| <module>" (indented for submodules).
    return any(line.rsplit("|", 1)[-1].strip() == module for line in stderr.splitlines())


# ---------------------------------------------------------------- F3 tools

@pytest.mark.parametrize("args", [[REMOTE], [REMOTE, "--replace"], ["--replace", REMOTE, "--database", "x"]])
def test_sync_to_d1_remote_refused_before_anything(tmp_path, args):
    bindir, log = _recorders(tmp_path, ["npx", WR])
    r = subprocess.run([sys.executable, "-X", "importtime", str(GRAPH / "scripts" / "sync-to-d1.py"), *args],
                       env=_env(bindir), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert POINTER in r.stderr
    assert log.read_text() == ""
    assert not _imported(r.stderr, "memora"), "refusal must come before memora is imported"


def test_sync_to_d1_rejects_abbreviated_remote(tmp_path):
    bindir, log = _recorders(tmp_path, ["npx", WR])
    r = subprocess.run([sys.executable, str(GRAPH / "scripts" / "sync-to-d1.py"), "--rem"],
                       env=_env(bindir), capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and log.read_text() == ""


def test_sync_to_d1_local_command_is_local_only():
    src = (GRAPH / "scripts" / "sync-to-d1.py").read_text()
    assert '"--local"]' in src
    assert f'"{REMOTE}")' not in src.replace("cmd.append(", "")  # no remote append left


@pytest.mark.parametrize("args", [[REMOTE], ["--source", "x", REMOTE], [REMOTE + "=1"]])
def test_sync_sh_remote_refused_before_anything(tmp_path, args):
    bindir, log = _recorders(tmp_path, ["npx", WR, "curl", "python", "python3"])
    r = subprocess.run(["/bin/bash", str(GRAPH / "scripts" / "sync.sh"), *args],
                       env=_env(bindir, real_python=False), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert POINTER in r.stderr
    assert log.read_text() == "", "no python, curl or wrangler call may happen"
    assert "/broadcast" not in (GRAPH / "scripts" / "sync.sh").read_text()


@pytest.mark.parametrize("args", [[], ["--dry-run"], ["--bucket", "ob1", "--d1-id", "x"], ["--bucket", "ob1", "--d1-id", "x", "--dry-run"]])
def test_link_r2_images_always_refused(tmp_path, args):
    bindir, _ = _recorders(tmp_path, [])
    r = subprocess.run([sys.executable, "-X", "importtime", str(GRAPH / "scripts" / "link-r2-images.py"), *args],
                       env=_env(bindir), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert POINTER in r.stderr
    assert not _imported(r.stderr, "requests") and not _imported(r.stderr, "boto3")


def test_setup_cloudflare_stops_at_remote_migration(tmp_path):
    bindir, log = _recorders(tmp_path, ["node", "npm", "npx", WR])
    r = subprocess.run(["/bin/bash", str(GRAPH / "scripts" / "setup-cloudflare.sh")],
                       env=_env(bindir), capture_output=True, text=True, timeout=120)
    calls = log.read_text()
    assert r.returncode == 1
    assert POINTER in r.stdout + r.stderr
    assert "d1 execute" not in calls
    assert "pages deploy" not in calls and "deploy" not in calls.replace("d1 list", "")
    assert "d1 list" in calls  # it got as far as the D1 step, and no further


def _deploy_pages_harness(tmp_path: Path) -> Path:
    """The script's functions without its trailing `main "$@"`."""
    src = (GRAPH / "scripts" / "setup-cloudflare.sh").read_text()
    body = src[: src.rindex('main "$@"')]
    lib = tmp_path / "setup-lib.sh"
    lib.write_text(body)
    return lib


def test_setup_cloudflare_pages_deploy_runs_guard_first(tmp_path):
    bindir, log = _recorders(tmp_path, ["npx", WR])
    lib = _deploy_pages_harness(tmp_path)
    script = f'source "{lib}"; PROJECT_DIR="{GRAPH}"; deploy_pages; echo reached-after'
    r = subprocess.run(["/bin/bash", "-c", script], env=_env(bindir), capture_output=True, text=True, timeout=120)
    # The guard's handler scope fails until slice L7, so the deploy must refuse.
    assert r.returncode == 1
    assert "reached-after" not in r.stdout
    assert log.read_text() == "", "wrangler must not run when the guard fails"


def _package_scripts() -> dict:
    return json.loads((GRAPH / "package.json").read_text())["scripts"]


def test_package_deploy_runs_guard_first_and_refuses(tmp_path):
    deploy = _package_scripts()["deploy"]
    assert deploy.startswith("python3 ../scripts/d1_write_guard.py --scope all && ")
    bindir, log = _recorders(tmp_path, [WR, "npx"])
    r = subprocess.run(["/bin/sh", "-c", deploy], cwd=GRAPH, env=_env(bindir),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert log.read_text() == ""


def test_package_d1_migrate_refused(tmp_path):
    scripts = _package_scripts()
    assert WR not in scripts["d1:migrate"]
    assert scripts["d1:migrate-local"].endswith("--local")
    if shutil.which("node") is None:
        pytest.skip("node not installed; the static check above still ran")
    bindir, log = _recorders(tmp_path, [WR, "npx"])
    node_dir = str(Path(shutil.which("node")).parent)
    env = _env(bindir)
    env["PATH"] = env["PATH"] + os.pathsep + node_dir
    r = subprocess.run(["/bin/sh", "-c", scripts["d1:migrate"]], cwd=GRAPH, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert POINTER in r.stderr
    assert log.read_text() == ""


# ---------------------------------------------------------------- F2 guard

def _guard(root: Path, scope: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD), "--scope", scope, "--root", str(root)],
                          capture_output=True, text=True, timeout=60)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


POST = "requests." + "post("
D1URL = "https://api.cloudflare.com/client/v4/accounts/a/d1/" + "database/b/query"

POSITIVE = {
    "T1": ("tools/link.py", f"import requests\n{POST}'{D1URL}', json={{}})\n"),
    "T2": ("tools/mig.sh", f"npx {WR}{D1}execute db {REMOTE} --file=x.sql\n"),
    "T3": ("tools/sync.py", 'cmd = ["npx", "' + WR + '", "d1", ' + '"execute", db, "--file=x"]\n'),
    "T4": ("tools/package.json", '{"scripts": {"m": "' + WR + D1 + 'migrations apply db"}}\n'),
    "T5": ("tools/deploy.sh", f"npx {WR} pages" + " deploy public\n"),
    "H1": ("memora-graph/functions/api/x.ts",
           'export const f = (env) => env.DB_MEMORA.prepare("INS' + 'ERT INTO memories (content) VALUES (?)").run();\n'),
}

NEGATIVE = {
    "local execute": ("tools/ok.sh", f"npx {WR}{D1}execute db --local --file=x.sql\n"),
    "local list": ("tools/ok.py", 'cmd = ["npx", "' + WR + '", "d1", "execute", db, "--local"]\n'),
    "local migrations": ("tools/ok.json", '{"s": "' + WR + D1 + 'migrations apply db --local"}\n'),
    "guarded deploy": ("tools/ok2.sh", f"python3 scripts/d1_write_guard.py --scope all && {WR} pages deploy public\n"),
    "allow-listed backends": ("memora/backends.py", f"import requests\n{POST}'{D1URL}')\n"),
    "prose ignored": ("docs/notes.py", f"npx {WR}{D1}execute db {REMOTE}\n"),
    "tool description": ("memora-graph/functions/api/ok.ts",
                         'const d = "Update an existing memory by ID."; const q = db.prepare("SELECT * FROM memories");\n'),
}


@pytest.mark.parametrize("rule", sorted(POSITIVE))
def test_guard_catches_each_rule(tmp_path, rule):
    rel, text = POSITIVE[rule]
    _write(tmp_path, rel, text)
    scope = "handlers" if rule.startswith("H") else "tools"
    r = _guard(tmp_path, scope)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f" {rule} " in r.stdout and rel in r.stdout
    assert _guard(tmp_path, "all").returncode == 1
    other = "tools" if scope == "handlers" else "handlers"
    assert _guard(tmp_path, other).returncode == 0, "each rule belongs to exactly one scope"


def test_guard_negatives_are_clean(tmp_path):
    for rel, text in NEGATIVE.values():
        _write(tmp_path, rel, text)
    r = _guard(tmp_path, "all")
    assert r.returncode == 0, r.stdout


def test_guard_handler_finding_names_bindings(tmp_path):
    rel, text = POSITIVE["H1"]
    _write(tmp_path, rel, text)
    assert "bindings: DB_MEMORA" in _guard(tmp_path, "handlers").stdout


def test_guard_usage_error(tmp_path):
    assert _guard(tmp_path / "missing", "tools").returncode == 2


def test_repo_tools_scope_clean():
    """Enforced on every push: clean-install.yml runs this suite."""
    r = _guard(REPO, "tools")
    assert r.returncode == 0, r.stdout + r.stderr


def test_repo_handlers_scope_reports_viewer_writers_until_l7():
    """The viewer still writes D1 until slice L7 (plan §6 F1). L7 flips this
    test to expect a clean run and makes the CI handler step blocking."""
    r = _guard(REPO, "handlers")
    assert r.returncode == 1
    assert "memora-graph/functions/api/chat.ts" in r.stdout
    assert "memora-graph/functions/api/memories/[id].ts" in r.stdout
    assert all(" H1 " in line for line in r.stdout.splitlines())


H1_VERBS = [
    "INSERT INTO memories (content) VALUES (?)",
    "INSERT OR REPLACE INTO memories_embeddings (memory_id) VALUES (?)",
    "REPLACE INTO memories (id) VALUES (?)",
    "UPDATE memories SET tags = ? WHERE id = ?",
    "UPDATE OR IGNORE memories SET tags = ? WHERE id = ?",
    "DELETE FROM memories_crossrefs WHERE memory_id = ?",
    "CREATE TABLE IF NOT EXISTS memories_embeddings (memory_id INTEGER)",
    "CREATE INDEX i ON memories(id)",
    "CREATE TRIGGER t AFTER INSERT ON memories BEGIN SELECT 1; END",
    "DROP TABLE memories",
    "ALTER TABLE memories ADD COLUMN x",
]


@pytest.mark.parametrize("sql", H1_VERBS)
@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_guard_h1_every_write_verb(tmp_path, sql, quote):
    _write(tmp_path, "memora-graph/worker/src/w.ts",
           f"await db.prepare(\n  {quote}  {sql}{quote}\n).run();\n")
    r = _guard(tmp_path, "handlers")
    assert r.returncode == 1 and " H1 " in r.stdout, (sql, r.stdout)
