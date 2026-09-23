"""scripts/repoint_mcp_config.py (plan §6 F4/F5, slice L8): round trips for
each config shape, the 0600 backup, dry run, the endpoint check gate, and
that the result passes the audit."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memora import config_audit
from tests.test_l8_endpoint_check import ADMIN, HEALTH, FakeMemoraAll, _token_file

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "repoint_mcp_config.py"
D1 = "d1" + "://"
TOKEN = "cfut_" + "RepointSecret0123456789"
URL = "http://nuc8:8920/mcp/memora"

CREDENTIALS = {"mcpServers": {"memora": {
    "command": "/Users/x/.local/bin/memora-server", "args": ["--no-graph"],
    "env": {"MEMORA_STORAGE_URI": f"{D1}acct/db", "CLOUDFLARE_API_TOKEN": TOKEN,
            "OPENAI_API_KEY": "sk-x", "MEMORA_ALLOW_ANY_TAG": "1"}}}}
WORKSPACE = {"mcpServers": {
    "clmux": {"command": "clmux", "args": ["mcp"]},
    "memora": {"type": "stdio", "command": "uvx", "args": ["memora", f"--storage={D1}acct/db"],
               "env": {"CF_API_TOKEN": TOKEN}},
    "other": {"type": "http", "url": "http://elsewhere/mcp"}}}
CLAUDE_JSON = {"numStartups": 7, "projects": {"/x": {"allowedTools": []}},
               "mcpServers": {"memora": {"command": "memora-server",
                                         "env": {"MEMORA_DATABASES": json.dumps({"m": f"{D1}a/b"}),
                                                 "CLOUDFLARE_API_TOKEN": TOKEN}}},
               "zeta": True}


def _run(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=120)


def _write(tmp_path, name, doc, mode=0o644):
    p = tmp_path / name
    p.write_text(json.dumps(doc, indent=2))
    os.chmod(p, mode)
    return p


@pytest.mark.parametrize("name,doc,expect", [
    ("credentials.mcp.json", CREDENTIALS, ["memora"]),
    (".mcp.json", WORKSPACE, ["memora"]),
    (".claude.json", CLAUDE_JSON, ["memora"]),
])
def test_round_trip_for_each_shape(tmp_path, name, doc, expect):
    p = _write(tmp_path, name, doc, mode=0o640)
    original = p.read_bytes()

    dry = _run(str(p), "--url", URL)
    assert dry.returncode == 0 and json.loads(dry.stdout)["dry_run"] is True
    assert p.read_bytes() == original and not list(tmp_path.glob("*.bak-repoint-*"))
    assert TOKEN not in dry.stdout + dry.stderr, "the dry run masks the token"

    r = _run(str(p), "--url", URL, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["repointed"] == expect
    backup = Path(out["backup"])
    assert backup.read_bytes() == original and oct(backup.stat().st_mode)[-3:] == "600"
    assert oct(p.stat().st_mode)[-3:] == "640", "the file keeps its own mode"

    new = json.loads(p.read_text())
    for n in expect:
        assert new["mcpServers"][n] == {"type": "http", "url": URL}
    for n, entry in doc["mcpServers"].items():
        if n not in expect:
            assert new["mcpServers"][n] == entry, "other servers are untouched"
    assert list(new) == list(doc), "top-level key order kept"
    assert {k: v for k, v in new.items() if k != "mcpServers"} == {k: v for k, v in doc.items() if k != "mcpServers"}
    text = p.read_text()
    assert D1 not in text and TOKEN not in text

    audit_new = config_audit.audit([p], host="mac")
    assert audit_new["clean"], "the repointed file passes the audit"
    assert not config_audit.audit([tmp_path], host="mac")["clean"], "the backup still holds the token"

    again = _run(str(p), "--url", URL, "--apply")
    assert again.returncode == 3 and json.loads(again.stdout)["repointed"] == []


def test_server_selects_one_entry(tmp_path):
    doc = {"mcpServers": {"a": CREDENTIALS["mcpServers"]["memora"], "b": CREDENTIALS["mcpServers"]["memora"]}}
    p = _write(tmp_path, "x.mcp.json", doc)
    r = _run(str(p), "--url", URL, "--apply", "--server", "b")
    assert r.returncode == 0
    new = json.loads(p.read_text())
    assert new["mcpServers"]["a"] == doc["mcpServers"]["a"] and new["mcpServers"]["b"]["type"] == "http"


@pytest.mark.parametrize("args,content", [
    (["--url", "ftp://nuc8/mcp"], CREDENTIALS),
    (["--url", "http://nuc8:8920/other"], CREDENTIALS),
    (["--url", URL], {"servers": {}}),
    (["--url", URL, "--server", "missing"], CREDENTIALS),
    (["--url", URL, "--check-health-token-file", "/x"], CREDENTIALS),
])
def test_bad_input_is_refused_and_writes_nothing(tmp_path, args, content):
    p = _write(tmp_path, "c.mcp.json", content)
    before = p.read_bytes()
    r = _run(str(p), *args, "--apply")
    assert r.returncode == 2 and json.loads(r.stdout)["ok"] is False
    assert p.read_bytes() == before and not list(tmp_path.glob("*.bak-repoint-*"))


def test_a_failed_endpoint_check_refuses_the_repoint(tmp_path):
    fake = FakeMemoraAll(kind="d1")  # the scratch store is not local: check-endpoint refuses
    try:
        p = _write(tmp_path, "c.mcp.json", CREDENTIALS)
        before = p.read_bytes()
        h, a = _token_file(tmp_path, "h", HEALTH), _token_file(tmp_path, "a", ADMIN)
        r = _run(str(p), "--url", f"{fake.url}/mcp/memora", "--apply",
                 "--check-health-token-file", h, "--check-admin-token-file", a)
        assert r.returncode == 2 and "check-endpoint failed" in r.stdout
        assert p.read_bytes() == before and not list(tmp_path.glob("*.bak-repoint-*"))
    finally:
        fake.close()


def test_a_passing_endpoint_check_then_repoints(tmp_path):
    fake = FakeMemoraAll()
    try:
        p = _write(tmp_path, "c.mcp.json", CREDENTIALS)
        h, a = _token_file(tmp_path, "h", HEALTH), _token_file(tmp_path, "a", ADMIN)
        r = _run(str(p), "--url", f"{fake.url}/mcp/memora", "--apply",
                 "--check-health-token-file", h, "--check-admin-token-file", a)
        assert r.returncode == 0, r.stdout
        out = json.loads(r.stdout)
        assert out["checked"] is True and out["repointed"] == ["memora"]
        assert "memory_create" in fake.tools(), "the check wrote only through the scratch store"
        assert all(c[1].endswith("/scratch") for c in fake.calls if c[0] == "POST")
    finally:
        fake.close()
