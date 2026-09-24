"""CFG1: nothing in the tracked tree identifies the operator's infrastructure.

The real host names, Tailscale addresses, Cloudflare account subdomain, D1
database ids and names, R2 buckets and store names live only in git-ignored
configuration (instances/deploy.env, instances/*.env,
memora-graph/wrangler.toml, instances/identifiers.local). This test scans
EVERY tracked text file:

- Generic checks that need no local configuration and so also run in CI:
  no Tailscale (100.64.0.0/10) address outside the documentation range
  100.64.0.0/24, no *.workers.dev host other than a placeholder, no
  non-placeholder D1 database_id in a tracked wrangler file, and no tracked
  copy of a git-ignored configuration file.
- Operator checks: every identifier found in the local configuration files
  above (the test never contains them itself). Skipped when none of those
  files exists, e.g. in CI; run it on the operator's checkout.

A finding names the file, the line and which configuration key the
identifier came from -- never the identifier itself.
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

REPO = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = {
    "deploy": REPO / "instances" / "deploy.env",
    "wrangler": REPO / "memora-graph" / "wrangler.toml",
    "identifiers": REPO / "instances" / "identifiers.local",
}
# Words that are never identifiers: the project's own name and placeholders.
NEUTRAL = {"memora", "localhost", "deploy-host", "build-host", "alpha", "beta", "gamma",
           "memora-graph", "memora-all", "127.0.0.1", "0.0.0.0"}
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".gz", ".db", ".sqlite", ".pdf")


def tracked_texts() -> List[Tuple[str, str]]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    files = []
    for name in out.decode().split("\0"):
        if not name or name.endswith(SKIP_SUFFIX):
            continue
        p = REPO / name
        if p.is_symlink() or not p.is_file():
            continue
        try:
            files.append((name, p.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return files


def _hits(pattern: re.Pattern, files) -> List[str]:
    found = []
    for name, text in files:
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                found.append(f"{name}:{n}")
    return found


# ------------------------------------------------------------ generic (CI)

CGNAT = ipaddress.ip_network("100.64.0.0/10")
DOC_RANGE = ipaddress.ip_network("100.64.0.0/24")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")


def test_no_tailscale_address_outside_the_documentation_range():
    bad = []
    for name, text in tracked_texts():
        for n, line in enumerate(text.splitlines(), 1):
            for m in IPV4.finditer(line):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                if ip in CGNAT and ip not in DOC_RANGE:
                    bad.append(f"{name}:{n}")
    assert not bad, f"Tailscale addresses outside 100.64.0.0/24 (use a placeholder): {bad}"


WORKERS = re.compile(r"[A-Za-z0-9-]+\.([A-Za-z0-9<>_-]+)\.workers\.dev")
WORKER_PLACEHOLDERS = {"xxx", "YOUR-SUBDOMAIN", "example-account", "<your-subdomain>", "<account-subdomain>"}


def test_no_real_workers_dev_subdomain():
    bad = []
    for name, text in tracked_texts():
        for n, line in enumerate(text.splitlines(), 1):
            for m in WORKERS.finditer(line):
                if m.group(1) not in WORKER_PLACEHOLDERS:
                    bad.append(f"{name}:{n}")
    assert not bad, f"a workers.dev account subdomain is tracked (use a placeholder): {bad}"


D1_ID = re.compile(r'database_id\s*=\s*"([^"]*)"')


def test_no_real_d1_database_id_in_a_tracked_wrangler_file():
    bad = [f"{name}:{n}" for name, text in tracked_texts() if "wrangler" in name
           for n, line in enumerate(text.splitlines(), 1)
           for m in D1_ID.finditer(line) if not re.fullmatch(r"0{8}-0{4}-0{4}-0{4}-0{11}\d", m.group(1))]
    assert not bad, f"a D1 database id is tracked (the template uses 00000000-…): {bad}"


def test_the_local_configuration_files_are_not_tracked():
    names = {name for name, _ in tracked_texts()}
    tracked = [str(p.relative_to(REPO)) for p in LOCAL_CONFIG.values() if str(p.relative_to(REPO)) in names]
    tracked += [n for n in names if re.fullmatch(r"instances/(?!example\.env$)[^/]+\.env", n)]
    assert not tracked, f"git-ignored configuration is tracked: {tracked}"


def test_the_ignore_rules_cover_the_local_configuration():
    for p in (*LOCAL_CONFIG.values(), REPO / "instances" / "all.env"):
        rel = str(p.relative_to(REPO))
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=REPO)
        assert r.returncode == 0, f"{rel} is not git-ignored"


# ------------------------------------------------------------ operator (local)

def _env_values(path: Path) -> Dict[str, str]:
    out = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _hosts_in(value: str) -> List[str]:
    """Host names / addresses inside a URL or bare host value."""
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", value)
    return [m.group(1)] if m else [value]


def _workspace_paths(value: str) -> List[str]:
    """The workspace part of a configured path: "~/repos/<ws>/memora" or
    "$HOME/repos/<ws>/.mcp.json" -> "repos/<ws>". A path that names no
    directory of its own under the home directory yields nothing."""
    v = re.sub(r"^(~|\$HOME|\$\{HOME\}|/(?:Users|home)/[^/]+)/", "", value.strip())
    parts = [p for p in v.split("/") if p]
    while parts and parts[-1] in ("memora", ".mcp.json", "credentials.mcp.json"):
        parts.pop()
    # A dot-directory (~/.config/memora, the documented default) is generic.
    return ["/".join(parts)] if len(parts) >= 2 and not parts[0].startswith(".") else []


def operator_identifiers() -> List[Tuple[str, str]]:
    """(source key, identifier) pairs from the local configuration."""
    found: List[Tuple[str, str]] = []
    dep = LOCAL_CONFIG["deploy"]
    if dep.exists():
        vals = _env_values(dep)
        for key in ("DEPLOY_HOST", "DEPLOY_GRAPH_BIND", "EMBEDDING_OLD_URL", "EMBEDDING_NEW_URL"):
            for h in _hosts_in(vals.get(key, "")):
                found.append((f"deploy.env {key}", h))
        found += [("deploy.env DEPLOY_REPO", p) for p in _workspace_paths(vals.get("DEPLOY_REPO", ""))]
        try:
            projects = json.loads(vals.get("MEMORA_PROJECTS", "{}"))
            # Store names identify D1 databases; project names are ordinary
            # words (list a sensitive one in identifiers.local).
            found += [("deploy.env MEMORA_PROJECTS store", store) for store in projects]
        except (ValueError, AttributeError):
            pass
    for env in sorted((REPO / "instances").glob("*.env")):
        if env.name in ("example.env", "deploy.env"):
            continue
        env_vals = _env_values(env)
        found += [(f"{env.name} CRED_SOURCE", p) for p in _workspace_paths(env_vals.get("CRED_SOURCE", ""))]
        reg = env_vals.get("MEMORA_DATABASES")
        if not reg:
            continue
        try:
            for store, uri in json.loads(reg).items():
                found.append((f"{env.name} MEMORA_DATABASES store", store))
                m = re.match(r"d1://([^/]+)/(.+)", str(uri))
                if m:
                    found += [(f"{env.name} d1 account", m.group(1)), (f"{env.name} d1 database", m.group(2))]
        except ValueError:
            pass
    wr = LOCAL_CONFIG["wrangler"]
    if wr.exists():
        text = wr.read_text()
        for key in ("database_id", "database_name", "bucket_name"):
            found += [(f"wrangler.toml {key}", v) for v in re.findall(rf'{key}\s*=\s*"([^"]+)"', text)]
        for url in re.findall(r'WS_WORKER_URL\s*=\s*"([^"]+)"', text):
            for h in _hosts_in(url):
                found.append(("wrangler.toml WS_WORKER_URL", h))
                if h.endswith(".workers.dev") and h.count(".") >= 3:
                    found.append(("wrangler.toml account subdomain", h.split(".")[-3]))
    ids = LOCAL_CONFIG["identifiers"]
    if ids.exists():
        found += [("identifiers.local", line.strip()) for line in ids.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
    # A value the tracked template itself carries (a copy of
    # wrangler.toml.example, say) is public by definition.
    template = (REPO / "memora-graph" / "wrangler.toml.example").read_text()
    return [(src, v) for src, v in found
            if v and v.lower() not in NEUTRAL and not identifier_pattern(v).search(template)]


def identifier_pattern(value: str) -> re.Pattern:
    """Word-bounded; a short name (under 4 characters, e.g. a store called
    "io") only in identifier contexts, so an ordinary word or a module
    reference (io.open, io-bound) never matches."""
    v = re.escape(value)
    if len(value) >= 4:
        return re.compile(rf"(?<![A-Za-z0-9_]){v}(?![A-Za-z0-9_])", re.IGNORECASE)
    end = r"(?![\w.-])"   # not followed by a word character, a dot or a hyphen (`re.compile`, `re-run`)
    ctx = [rf"[\"'`]{v}[\"'`]", rf"/{v}(?:\.db|\.env|/|{end})", rf"(?<![\w.]){v}\.(?:db|env|credentials)\b",
           rf"(?:--host|--store|freeze|thaw|seed|rollback|restore|cutover_store\.sh) {v}{end}",
           rf"\bstore {v}{end}", rf"=\s*{v}{end}"]
    return re.compile("|".join(ctx))


def test_no_operator_identifier_is_tracked():
    idents = operator_identifiers()
    if not idents:
        pytest.skip("no local configuration (instances/deploy.env, instances/*.env, "
                    "memora-graph/wrangler.toml, instances/identifiers.local): nothing to compare")
    files = tracked_texts()
    bad = []
    for source, value in idents:
        for hit in _hits(identifier_pattern(value), files):
            bad.append(f"{hit} [{source}]")
    assert not bad, "operator identifiers are tracked (values withheld):\n" + "\n".join(sorted(set(bad)))


def test_the_identifier_pattern_is_word_bounded_and_context_aware():
    long = identifier_pattern("hostname1")
    assert long.search("ssh hostname1 'ls'") and long.search("HOSTNAME1") and not long.search("hostname12")
    short = identifier_pattern("io")
    assert short.search('{"io": "/data/io.db"}') and short.search("store io: ok")
    assert short.search("--host io") and not short.search("an io-bound step, io:")
    assert not short.search("x = io.open(p)") and not short.search("import json, io, sys")
    assert not short.search("disk/cpu/io-wait") and not short.search("a store io-runs it")


def test_workspace_paths_are_derived_from_configured_paths():
    assert _workspace_paths("~/repos/ws-one/memora") == ["repos/ws-one"]
    assert _workspace_paths("$HOME/repos/ws-one/.mcp.json") == ["repos/ws-one"]
    assert _workspace_paths("/Users/someone/repos/ws-one/memora") == ["repos/ws-one"]
    assert _workspace_paths("~/.config/memora/credentials.mcp.json") == []
    assert _workspace_paths("~/memora") == [] and _workspace_paths("") == []
