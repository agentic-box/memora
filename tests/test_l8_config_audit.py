"""memora/config_audit.py and scripts/audit_configs.py (plan §6 F4-F6, slice
L8): fixture trees, masking, exit codes, the memora-all exception, and the
remote path through a fake ssh."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memora import config_audit

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "audit_configs.py"
MODULE = REPO / "memora" / "config_audit.py"

@pytest.fixture(autouse=True)
def _no_real_runtimes(monkeypatch):
    """No test may query the host's real docker/podman/container; the
    container tests below install fakes explicitly."""
    monkeypatch.setenv("MEMORA_AUDIT_RUNTIMES", "")


# Assembled so the D1 write guard's own scan of this repo stays quiet.
D1 = "d1" + "://"
TOKEN = "cfut_" + "S3cretTokenValue0123456789abcdef"
ACCT, DB = "a" * 32, "b" * 32


def _tree(root: Path):
    """A $HOME with one of each kind, plus decoys that must not be read."""
    stdio = {"mcpServers": {
        "memora": {"command": "memora-server", "args": ["--no-graph"],
                   "env": {"MEMORA_STORAGE_URI": f"{D1}{ACCT}/{DB}", "CLOUDFLARE_API_TOKEN": TOKEN,
                           "OPENAI_API_KEY": "sk-not-a-d1-secret"}},
        "other": {"command": "x"}}}
    (root / ".config" / "memora").mkdir(parents=True)
    (root / ".config" / "memora" / "credentials.mcp.json").write_text(json.dumps(stdio, indent=2))
    (root / "work" / "proj").mkdir(parents=True)
    (root / "work" / "proj" / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"memora": {"type": "http", "url": "http://nuc8:8920/mcp/memora"}}}))
    (root / "repo" / "instances").mkdir(parents=True)
    (root / "repo" / "instances" / "all.env").write_text(
        f"MEMORA_DATABASES='{{\"memora\":\"{D1}{ACCT}/{DB}\"}}'\n")
    (root / "repo" / "instances" / "re.env").write_text(f'STORAGE_URI="{D1}{ACCT}/{DB}"\n')
    (root / ".zshrc").write_text(f"export CF_API_TOKEN={TOKEN}\nexport CLOUDFLARE_API_TOKEN=\n")
    (root / "Library" / "LaunchAgents").mkdir(parents=True)
    (root / "Library" / "LaunchAgents" / "com.x.plist").write_text(
        f"<key>CLOUDFLARE_API_TOKEN</key><string>$CLOUDFLARE_API_TOKEN</string>\n")
    # Decoys: skipped trees and non-candidate files.
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / ".mcp.json").write_text(f'{{"x": "{D1}{ACCT}/{DB}"}}')
    (root / "Library" / "Caches").mkdir(parents=True)
    (root / "Library" / "Caches" / "x.env").write_text(f"CF_API_TOKEN={TOKEN}\n")
    (root / "notes.txt").write_text(f"{D1}{ACCT}/{DB} {TOKEN}\n")
    return root


def test_finds_every_kind_and_masks_every_value(tmp_path):
    root = _tree(tmp_path)
    result = config_audit.audit([root], host="mac")
    kinds = {(Path(f["file"]).name, f["kind"]) for f in result["findings"]}
    assert ("credentials.mcp.json", "storage_uri") in kinds
    assert ("credentials.mcp.json", "cloudflare_token") in kinds
    assert ("all.env", "memora_databases") in kinds
    assert ("re.env", "d1_uri") in kinds
    assert (".zshrc", "cloudflare_token") in kinds
    assert ("com.x.plist", "cloudflare_token") in kinds
    files = {Path(f["file"]).name for f in result["findings"]}
    assert ".mcp.json" not in files, "an http entry is not a direct-D1 client"
    assert "notes.txt" not in files, "not a configuration file"
    assert not any("node_modules" in f["file"] or "Caches" in f["file"] for f in result["findings"])
    dump = json.dumps(result)
    assert TOKEN not in dump and TOKEN[5:] not in dump and ACCT not in dump and DB not in dump
    tok = next(f for f in result["findings"] if f["kind"] == "cloudflare_token" and f["file"].endswith(".zshrc"))
    assert tok["value"] == f"{TOKEN[:4]}…({len(TOKEN)} chars)" and tok["name"] == "CF_API_TOKEN"
    ref = next(f for f in result["findings"] if f["file"].endswith(".plist"))
    assert ref["value"] == "(reference)"
    assert sum(1 for f in result["findings"] if f["file"].endswith(".zshrc")) == 1, "an empty setting is not a finding"


def test_line_numbers_point_at_the_value(tmp_path):
    root = _tree(tmp_path)
    result = config_audit.audit([root], host="mac")
    f = next(f for f in result["findings"] if f["kind"] == "storage_uri")
    lines = Path(f["file"]).read_text().splitlines()
    assert "MEMORA_STORAGE_URI" in lines[f["line"] - 1]


def test_memora_all_is_reported_but_does_not_fail(tmp_path):
    root = tmp_path / "home"
    (root / "repo" / "instances").mkdir(parents=True)
    (root / "repo" / "instances" / "all.env").write_text(f"MEMORA_DATABASES='{{\"m\":\"{D1}{ACCT}/{DB}\"}}'\n")
    result = config_audit.audit([root], host="mac")
    assert result["findings"] and all(f["memora_all"] for f in result["findings"])
    assert result["clean"] and result["blocking"] == 0


def test_nuc8_credentials_are_memora_alls_only_on_nuc8(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _tree(tmp_path)
    for f in (tmp_path / ".zshrc", tmp_path / "repo" / "instances" / "re.env",
              tmp_path / "Library" / "LaunchAgents" / "com.x.plist"):
        f.unlink()
    on_mac = config_audit.audit([tmp_path], host="mac")
    on_nuc8 = config_audit.audit([tmp_path], host="nuc8")
    assert not on_mac["clean"], "on the Mac the credential file is a direct-D1 client (F4)"
    assert on_nuc8["clean"], "on nuc8 it is memora-all's own credential source"


def test_extra_memora_all_paths(tmp_path):
    root = _tree(tmp_path)
    everything = str(tmp_path) + "/*"
    assert config_audit.audit([root], host="x", memora_all=[everything])["clean"]


def test_an_unreadable_candidate_is_not_clean(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    f = tmp_path / "x.mcp.json"
    f.write_text("{}")
    f.chmod(0)
    try:
        result = config_audit.audit([tmp_path], host="x")
    finally:
        f.chmod(0o600)
    assert not result["clean"] and result["errors"]


def test_repoint_backups_are_found(tmp_path):
    (tmp_path / ".claude.json.bak-repoint-20260924T000000Z").write_text(
        json.dumps({"mcpServers": {"m": {"env": {"CLOUDFLARE_API_TOKEN": TOKEN}}}}))
    result = config_audit.audit([tmp_path], host="x")
    assert not result["clean"], "a backup still holds the old token until it is deleted"


def _cli(*args, env=None):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          env=env or os.environ.copy(), timeout=120)


def test_cli_exit_codes_and_no_secret_on_stdout(tmp_path):
    root = _tree(tmp_path / "dirty")
    r = _cli("--local", str(root))
    assert r.returncode == 1 and "DIRECT-D1" in r.stdout and "NOT CLEAN" in r.stdout
    assert TOKEN not in r.stdout + r.stderr and ACCT not in r.stdout
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / ".mcp.json").write_text(json.dumps({"mcpServers": {"m": {"type": "http", "url": "http://nuc8:8920/mcp"}}}))
    r = _cli("--local", str(clean))
    assert r.returncode == 0 and "ALL CLEAN" in r.stdout
    assert _cli("--local", str(tmp_path / "missing")).returncode == 2


def _fake_ssh(tmp_path, body):
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/bin/sh\n" + body)
    ssh.chmod(0o755)
    return str(ssh)


def test_remote_hosts_run_the_module_over_ssh(tmp_path):
    """The fake ssh drops its options and host and runs the rest locally, so
    the module really runs from stdin with the host label."""
    remote_home = _tree(tmp_path / "remote")
    # Drops "-o X" pairs, the host and the remote "python3"; runs the rest.
    ssh = _fake_ssh(tmp_path, f'while [ "$1" = -o ]; do shift 2; done\nshift 2\ncd /\nexec {sys.executable} "$@" '
                              f'"{remote_home}"\n')
    r = _cli("--host", "ob1", "--ssh", ssh, "--json")
    out = json.loads(r.stdout)
    assert r.returncode == 1 and not out["clean"]
    host = out["hosts"][0]
    assert host["host"] == "ob1" and host["blocking"] > 0
    assert all(f["host"] == "ob1" for f in host["findings"])
    assert TOKEN not in r.stdout


@pytest.mark.parametrize("body,why", [
    ("exit 255\n", "ssh failed"),
    ("echo not json\nexit 0\n", "garbage output"),
    ('echo \'{"host": "someone-else", "clean": true, "findings": [], "blocking": 0, "errors": []}\'\nexit 0\n',
     "answer from the wrong host"),
    ('echo \'{"host": "ob1", "clean": true, "findings": [], "blocking": 0, "errors": []}\'\nexit 1\n',
     "exit status contradicts the report"),
])
def test_a_host_that_cannot_be_audited_is_not_clean(tmp_path, body, why):
    ssh = _fake_ssh(tmp_path, "cat >/dev/null\n" + body)
    r = _cli("--host", "ob1", "--ssh", ssh, "--json")
    out = json.loads(r.stdout)
    assert r.returncode == 1 and not out["clean"], why
    assert out["hosts"][0]["errors"], why


def test_the_module_runs_standalone_from_stdin(tmp_path):
    """What ssh HOST python3 - does: no memora package on the remote."""
    root = _tree(tmp_path / "h")
    r = subprocess.run([sys.executable, "-", "--json", "--host-label", "bestation", str(root)],
                       input=MODULE.read_text(), capture_output=True, text=True, cwd="/", timeout=60,
                       env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "MEMORA_AUDIT_RUNTIMES": ""})
    assert r.returncode == 1, r.stderr
    assert json.loads(r.stdout)["host"] == "bestation"


# ---------------------------------------------------------------- running containers

def _fake_runtime(bindir: Path, name: str, containers: dict, *, fail: str = ""):
    """A fake docker/podman/container. containers: {name: [env strings]}.
    fail: "list" or "inspect" makes that verb fail."""
    bindir.mkdir(exist_ok=True)
    data = bindir / f"{name}.json"
    data.write_text(json.dumps(containers))
    body = f"""#!{sys.executable}
import json, sys
data = json.load(open({str(data)!r}))
verb = sys.argv[1]
if verb in ("ps", "list"):
    if {fail!r} == "list":
        sys.exit("cannot connect to the daemon")
    if {name!r} == "container":
        print("ID  IMAGE  OS  ARCH  STATE  ADDR")
    for n in data:
        print(n)
elif verb == "inspect":
    if {fail!r} == "inspect":
        sys.exit("inspect failed")
    env = data[sys.argv[2]]
    if {name!r} == "container":
        print(json.dumps([{{"configuration": {{"id": sys.argv[2], "initProcess": {{"environment": env}}}}}}]))
    else:
        print(json.dumps([{{"Name": "/" + sys.argv[2], "Config": {{"Env": env}}}}]))
"""
    exe = bindir / name
    exe.write_text(body)
    exe.chmod(0o755)


@pytest.fixture
def runtimes(tmp_path, monkeypatch):
    bindir = tmp_path / "rtbin"

    def install(**by_runtime):
        names = []
        for rt, spec in by_runtime.items():
            containers, fail = spec if isinstance(spec, tuple) else (spec, "")
            _fake_runtime(bindir, rt, containers, fail=fail)
            names.append(rt)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
        monkeypatch.setenv("MEMORA_AUDIT_RUNTIMES", ",".join(names))
    return install


BASE_ENV = ["PATH=/usr/local/bin:/usr/bin", "HOME=/root"]


def test_a_running_container_with_the_old_token_blocks(tmp_path, runtimes):
    runtimes(container={"memora-agentic": BASE_ENV + [f"CLOUDFLARE_API_TOKEN={TOKEN}",
                                                      f"MEMORA_DATABASES={{\"m\":\"{D1}{ACCT}/{DB}\"}}",
                                                      "OPENAI_API_KEY=sk-live-not-d1"]},
             docker={"unrelated": BASE_ENV})
    empty = tmp_path / "home"
    empty.mkdir()
    result = config_audit.audit([empty], host="mac")
    assert not result["clean"] and result["blocking"] == 2
    kinds = {(f["container"], f["detail"]) for f in result["findings"]}
    assert kinds == {("memora-agentic", "cloudflare_token"), ("memora-agentic", "memora_databases")}
    assert all(f["kind"] == "runtime_env" and f["file"] == "container:memora-agentic" for f in result["findings"])
    dump = json.dumps(result)
    assert TOKEN not in dump and ACCT not in dump and "sk-live-not-d1" not in dump


def test_memora_all_on_nuc8_is_the_exception(tmp_path, runtimes):
    env = BASE_ENV + [f"CLOUDFLARE_API_TOKEN={TOKEN}", f"MEMORA_DATABASES={{\"m\":\"{D1}{ACCT}/{DB}\"}}"]
    runtimes(docker={"memora-all": env})
    empty = tmp_path / "home"
    empty.mkdir()
    assert config_audit.audit([empty], host="nuc8")["clean"]
    assert not config_audit.audit([empty], host="ob1")["clean"], "memora-all is only the exception on nuc8"


def test_a_clean_container_is_clean(tmp_path, runtimes):
    runtimes(docker={"web": BASE_ENV + ["MEMORA_URL=http://nuc8:8920/mcp"]})
    empty = tmp_path / "home"
    empty.mkdir()
    assert config_audit.audit([empty], host="mac")["clean"]


@pytest.mark.parametrize("fail", ["list", "inspect"])
def test_a_runtime_query_failure_is_not_clean(tmp_path, runtimes, fail):
    runtimes(docker=({"x": BASE_ENV}, fail))
    empty = tmp_path / "home"
    empty.mkdir()
    result = config_audit.audit([empty], host="mac")
    assert not result["clean"] and result["errors"] and result["blocking"] == 0


def test_an_inspect_without_environment_is_not_clean(tmp_path, runtimes):
    runtimes(docker={"x": []})
    empty = tmp_path / "home"
    empty.mkdir()
    assert not config_audit.audit([empty], host="mac")["clean"]


def test_containers_only_skips_files(tmp_path, runtimes):
    runtimes(docker={"web": BASE_ENV})
    root = _tree(tmp_path / "dirty")
    assert config_audit.audit([root], host="mac", files=False)["clean"]
    r = _cli("--local", str(root), "--containers-only")
    assert r.returncode == 0, r.stdout
    r = _cli("--local", str(root))
    assert r.returncode == 1


def test_containers_are_audited_on_remote_hosts_too(tmp_path, runtimes):
    """The remote run inherits the host's runtimes: here the fake ones."""
    runtimes(podman={"memora-ob1": BASE_ENV + [f"CF_API_TOKEN={TOKEN}"]})
    remote_home = tmp_path / "remote"
    remote_home.mkdir()
    ssh = _fake_ssh(tmp_path, f'while [ "$1" = -o ]; do shift 2; done\nshift 2\ncd /\nexec {sys.executable} "$@" '
                              f'"{remote_home}"\n')
    r = _cli("--host", "ob1", "--ssh", ssh, "--json", "--containers-only")
    out = json.loads(r.stdout)
    assert r.returncode == 1
    f = out["hosts"][0]["findings"][0]
    assert f["kind"] == "runtime_env" and f["container"] == "memora-ob1" and f["host"] == "ob1"
    assert TOKEN not in r.stdout
