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
        entry = new["mcpServers"][n]
        assert entry["type"] == "http" and entry["url"] == URL
        assert "command" not in entry and "args" not in entry
        old_env = doc["mcpServers"][n].get("env", {})
        kept = {k: v for k, v in old_env.items()
                if k not in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN", "MEMORA_STORAGE_URI", "MEMORA_DATABASES")}
        assert entry.get("env", {}) == kept, "only the routing changes; every other env key stays"
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


SECRETS = {"OPENAI_API_KEY": "sk-proj-" + "LeakCheck0001", "MEMORA_EMBEDDING_API_KEY": "emb-" + "LeakCheck0002",
           "AWS_SECRET_ACCESS_KEY": "aws-" + "LeakCheck0003", "AWS_ACCESS_KEY_ID": "AKIA" + "LEAKCHECK0004"}


def test_no_value_is_ever_printed(tmp_path):
    """Review 7667 P1-1: the preview shows keys and routing only."""
    doc = {"mcpServers": {"memora": {"command": "/secret/path/memora-server",
                                     "args": ["--no-graph", f"--storage={D1}acct/db", "positional-secret"],
                                     "env": {"MEMORA_STORAGE_URI": f"{D1}acct/db", "CLOUDFLARE_API_TOKEN": TOKEN,
                                             **SECRETS}}}}
    p = _write(tmp_path, "credentials.mcp.json", doc)
    for extra in ([], ["--apply"]):
        r = _run(str(p), "--url", URL, *extra)
        out = r.stdout + r.stderr
        for value in [TOKEN, "acct/db", "positional-secret", "/secret/path", *SECRETS.values()]:
            assert value not in out, (extra, value)
        for key in SECRETS:
            assert key in out, "keys are shown so the operator sees what is kept"
        assert "<redacted:" in out and "--no-graph" in out and "http://nuc8:8920/<redacted path:2 segments>" in out
        if extra:
            break
        p.write_text(json.dumps(doc))


def test_instance_credentials_shape_keeps_what_cred_args_reads(tmp_path):
    """Review 7667 P1-2b: memora-instance.sh's cred_args reads
    mcpServers.memora.env of credentials.mcp.json; after the repoint it must
    still deliver every non-D1 key, and nothing that reaches D1."""
    env = {"MEMORA_STORAGE_URI": f"{D1}acct/db", "CLOUDFLARE_API_TOKEN": TOKEN,
           "MEMORA_DATABASES": json.dumps({"memora": f"{D1}a/b", "scratch": "/data/scratch.db"}),
           "MEMORA_LLM_MODEL": "openai/gpt-4o-mini", "MEMORA_EMBEDDING_MODEL": "openai", **SECRETS}
    p = _write(tmp_path, "credentials.mcp.json",
               {"mcpServers": {"memora": {"command": "/Users/x/.local/bin/memora-server",
                                          "args": ["--no-graph"], "env": env}}})
    assert _run(str(p), "--url", URL, "--apply").returncode == 0
    new_env = json.loads(p.read_text())["mcpServers"]["memora"]["env"]
    assert json.loads(new_env["MEMORA_DATABASES"]) == {"scratch": "/data/scratch.db"}, "only the d1 entries leave"
    # The exact reader memora-instance.sh uses (cred_args, python part).
    script = (REPO / "scripts" / "memora-instance.sh").read_text()
    reader = script.split("python3 - \"$CRED_SOURCE\" <<'PYEOF'\n", 1)[1].split("PYEOF", 1)[0]
    r = subprocess.run([sys.executable, "-c", reader, str(p)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    delivered = [a for a in r.stdout.split("\0") if a and a != "-e"]
    keys = {a.split("=", 1)[0] for a in delivered}
    assert {"MEMORA_LLM_MODEL", "MEMORA_EMBEDDING_MODEL", *SECRETS} <= keys
    assert not any(D1 in a or TOKEN in a for a in delivered)
    assert "CLOUDFLARE_API_TOKEN" not in keys


def test_drop_env_removes_the_env(tmp_path):
    p = _write(tmp_path, ".mcp.json", {"mcpServers": {"memora": {"command": "x", "env": {
        "CF_API_TOKEN": TOKEN, **SECRETS}}}})
    assert _run(str(p), "--url", URL, "--apply", "--drop-env").returncode == 0
    assert json.loads(p.read_text())["mcpServers"]["memora"] == {"type": "http", "url": URL}



def test_a_url_with_userinfo_or_query_is_never_printed(tmp_path):
    """Review 7680 P1-2a."""
    doc = {"mcpServers": {
        "other": {"type": "http", "url": "https://admin:hunter2secret@example.com:8443/mcp?token=q-" + "Leak9"},
        "memora": {"command": "x", "env": {"CF_API_TOKEN": TOKEN}}}}
    p = _write(tmp_path, ".mcp.json", doc)
    doc["mcpServers"]["third"] = {"type": "http", "url": "https://h.example/api/pathsecret-" + "Leak5/mcp#frag-" + "Leak6"}
    p.write_text(json.dumps(doc))
    for url, extra in ((URL, ["--apply"]), ("http://user:pw-" + "Leak7@nuc8:8920/mcp/memora", []),
                       ("ftp://x/path-" + "Leak8", [])):
        before = p.read_text()
        r = _run(str(p), "--url", url, *extra)
        out = r.stdout + r.stderr
        for secret in ("hunter2secret", "q-Leak9", "pw-Leak7", "pathsecret-Leak5", "frag-Leak6", "path-Leak8", "/mcp/memora"):
            assert secret not in out, (url, secret)
        assert TOKEN not in out
        if extra:
            p.write_text(before)
    from scripts.repoint_mcp_config import safe_url
    assert safe_url("https://a:b@h.example:8443/p/q?x=1#f") == \
        "https://h.example:8443/<redacted path:2 segments> (userinfo/query/fragment withheld)"
    assert safe_url("http://nuc8:8920/mcp") == "http://nuc8:8920/<redacted path:1 segment>"
    assert safe_url("http://nuc8:8920") == "http://nuc8:8920"
    assert safe_url("not a url").startswith("<redacted:")
