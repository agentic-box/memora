#!/usr/bin/env python3
"""Find every memora client configuration that can reach Cloudflare D1
directly (docs/local-primary-implementation.md §6 F4-F6, §6.1, slice L8).

After the writer freeze, memora-all on its deploy host is the only process allowed to
talk to D1. Every other client must go through memora-all's HTTP endpoint.
This scan finds what still does not:

  d1_uri             a d1://account/database URI (MCP args, env, anywhere)
  storage_uri        MEMORA_STORAGE_URI set to a d1:// URI
  memora_databases   a MEMORA_DATABASES registry with a d1:// entry
  cloudflare_token   CLOUDFLARE_API_TOKEN or CF_API_TOKEN with a value (or a
                     reference to one, e.g. "$CLOUDFLARE_API_TOKEN")

Each finding names the file, the line, the kind, a MASKED value (first four
characters, then the length) and whether the file belongs to memora-all
itself. Token values are never printed.

Which files: under each root (default $HOME), *.mcp.json, .mcp.json,
credentials*.mcp.json, .claude.json, *.env / .env, instances/*.env,
launchd plists (~/Library/LaunchAgents, launchd/), shell rc files
(.zshrc .zshenv .zprofile .bashrc .bash_profile .profile), ~/.codex/*.toml
and ~/.config/memora/*. Heavy or irrelevant trees (node_modules, .git,
caches, virtualenvs, most of ~/Library) are skipped.

Containers (review 7667 P1-2a, 7680 P1-1): a container keeps the environment
it was created with, so a repointed file does not help until the container
is recreated -- and a STOPPED container still holds it and can be started
again. Every persisted container, running or stopped, of every runtime found
on the host (docker/podman `ps -a`, Apple's `container list --all`;
MEMORA_AUDIT_RUNTIMES overrides the list) is inspected, and its environment
is scanned like a file: kind "runtime_env", with the container's name, its
state and the underlying kind in "detail". Any such finding blocks, stopped
or not, until the container is recreated or removed. A runtime that is
installed but cannot list or inspect its containers makes the host NOT
clean. --containers-only skips the files.

Coverage limits (the first line of every text report): only this user's
files under the given roots, and only the containers this user's runtimes
can see -- another user's rootless docker/podman is invisible. An explicitly
empty MEMORA_AUDIT_RUNTIMES, or one that leaves out a runtime installed on
the host, is NOT clean ("no container runtime audited").

Nothing a runtime or ssh prints is ever copied into a report: failures
carry the command name and exit status only (review 7680 P1-2b).

memora-all's own configuration is reported but does not fail the audit:
instances/all.env (the deploy's registry source, on any host) and, when the
host label equals --deploy-host (memora-all's host, DEPLOY_HOST in the
operator's git-ignored instances/deploy.env; never hard-coded here),
~/.config/memora/credentials.mcp.json and ~/.config/memora/all.*
(memora-all's credential source there), and the running container named
memora-all there. Without --deploy-host no host is memora-all's, so those
files and that container count as ordinary direct-D1 clients (fail-safe).
More paths can be marked with --memora-all PATH.

Standard library only: scripts/audit_configs.py pipes this file to
`ssh HOST python3 -` to audit other hosts.

Exit: 0 clean (only memora-all findings, or none), 1 a non-memora-all
direct-D1 client remains (a file or a running container) or something could
not be audited, 2 usage error.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", ".npm", ".cargo",
    ".rustup", ".pyenv", ".nvm", ".gradle", ".m2", ".docker", ".wrangler", ".Trash",
    "site-packages", "dist-packages", "Caches", "Containers", "Group Containers",
    "Application Support", "Logs", "Mail", "Photos Library.photoslibrary", "Pictures",
    "Movies", "Music", "Downloads",
}
NAME_PATTERNS = (
    "*.mcp.json", ".mcp.json", "credentials*.mcp.json", ".claude.json", "*.env", ".env",
    ".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile",
    # repoint_mcp_config.py backups: they hold the old token until deleted.
    "*.bak-repoint-*",
)
# Parent-directory based: every file directly in these directories.
DIR_PATTERNS = (".config/memora", ".codex", "LaunchAgents", "launchd", "launchd/generated", "instances")
MAX_DEPTH = 8
MAX_BYTES = 8 * 1024 * 1024

D1_URI = re.compile(r"d1://[^\s\"'`,;}\])]+")
# NAME=value, "NAME": "value", NAME = "value" (TOML), and the launchd plist
# form <key>NAME</key><string>value</string>.
TOKEN = re.compile(
    r"\b(CLOUDFLARE_API_TOKEN|CF_API_TOKEN)\b(?:[\"']?\s*(?:=|:)\s*[\"']?|</key>\s*<string>)([^\s\"',;}<]*)"
)
STORAGE = re.compile(r"\bMEMORA_STORAGE_URI\b")
REGISTRY = re.compile(r"\bMEMORA_DATABASES\b")


def mask(value: str) -> str:
    """First four characters, then the length. Never the whole value."""
    if not value:
        return "(empty)"
    return f"{value[:4]}…({len(value)} chars)"


def _is_candidate(path: Path) -> bool:
    if any(fnmatch.fnmatch(path.name, pat) for pat in NAME_PATTERNS):
        return True
    parent = path.parent.as_posix()
    return any(parent.endswith("/" + d) or parent == d for d in DIR_PATTERNS)


def candidate_files(root: Path, *, max_depth: int = MAX_DEPTH) -> Iterator[Path]:
    root = root.expanduser()
    if root.is_file():
        yield root
        return
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        depth = len(here.parts) - base_depth
        keep = []
        for d in dirnames:
            if d in SKIP_DIRS:
                continue
            if depth >= max_depth:
                continue
            # ~/Library: only LaunchAgents is relevant.
            if here.name == "Library" and d != "LaunchAgents":
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            p = here / f
            if _is_candidate(p):
                yield p


def _memora_all(path: Path, host: str, extra: List[str], deploy_host: Optional[str] = None) -> bool:
    s = path.as_posix()
    if s.endswith("/instances/all.env"):
        return True
    if deploy_host and host == deploy_host:
        home = str(Path.home())
        if s == f"{home}/.config/memora/credentials.mcp.json" or fnmatch.fnmatch(s, f"{home}/.config/memora/all.*"):
            return True
    return any(fnmatch.fnmatch(s, str(Path(e).expanduser())) for e in extra)


def scan_text(text: str) -> List[Dict[str, object]]:
    """Findings in one file's text, without the file-level fields."""
    out: List[Dict[str, object]] = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in D1_URI.finditer(line):
            prefix = line[: m.start()]
            if STORAGE.search(prefix):
                kind = "storage_uri"
            elif REGISTRY.search(prefix):
                kind = "memora_databases"
            else:
                kind = "d1_uri"
            out.append({"line": n, "kind": kind, "value": mask(m.group(0))})
        for m in TOKEN.finditer(line):
            value = m.group(2)
            if not value:
                continue  # an empty setting grants nothing
            out.append({"line": n, "kind": "cloudflare_token", "name": m.group(1),
                        "value": "(reference)" if value.startswith("$") else mask(value)})
    return out


RUNTIMES = ("docker", "podman", "container")
RUNTIME_TIMEOUT = 60


class RuntimeQueryFailed(RuntimeError):
    pass


def _runtimes() -> List[str]:
    """The container runtimes to query: MEMORA_AUDIT_RUNTIMES (comma-separated,
    empty = none), else every known one installed on this host."""
    raw = os.environ.get("MEMORA_AUDIT_RUNTIMES")
    if raw is not None:
        return [r for r in (x.strip() for x in raw.split(",")) if r]
    return [r for r in RUNTIMES if shutil.which(r)]


def _run(cmd: List[str]) -> str:
    """Run one runtime command. A failure is reported by command name and
    exit status only: what a runtime prints may contain a token."""
    what = " ".join(cmd[:2])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=RUNTIME_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeQueryFailed(f"{what}: timed out after {RUNTIME_TIMEOUT}s") from exc
    except OSError as exc:
        raise RuntimeQueryFailed(f"{what}: could not run ({exc.strerror or type(exc).__name__})") from exc
    if r.returncode != 0:
        raise RuntimeQueryFailed(f"{what}: exited {r.returncode} (output withheld)")
    return r.stdout


def all_containers(runtime: str) -> List[Tuple[str, str]]:
    """(name, state) of EVERY persisted container of one runtime, running
    or stopped."""
    if runtime == "container":  # Apple: header row, then ID (= name) ... STATE ...
        lines = [ln for ln in _run([runtime, "list", "--all"]).splitlines() if ln.strip()]
        if not lines:
            return []
        head = lines[0].split()
        state_col = head.index("STATE") if "STATE" in head else None
        out = []
        for ln in lines[1:] if head and head[0] == "ID" else lines:
            parts = ln.split()
            state = parts[state_col] if state_col is not None and len(parts) > state_col else "unknown"
            out.append((parts[0], state))
        return out
    out = []
    for ln in _run([runtime, "ps", "-a", "--format", "{{.Names}}\t{{.State}}"]).splitlines():
        if ln.strip():
            name, _, state = ln.partition("\t")
            out.append((name.strip(), state.strip() or "unknown"))
    return out


def _env_lists(obj: object) -> List[str]:
    """Every "KEY=value" string in env-like lists of an inspect document
    (docker/podman Config.Env; Apple's environment lists)."""
    out: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("env", "environment") and isinstance(v, list):
                out += [e for e in v if isinstance(e, str) and "=" in e]
            else:
                out += _env_lists(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _env_lists(v)
    return out


def container_env(runtime: str, name: str) -> List[str]:
    raw = _run([runtime, "inspect", name])
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise RuntimeQueryFailed(f"{runtime} inspect {name}: output is not JSON (withheld)") from exc
    env = _env_lists(doc)
    if not env:
        # Every container has at least PATH; none found means we could not read it.
        raise RuntimeQueryFailed(f"{runtime} inspect {name}: no environment found")
    return env


def audit_containers(host: str, deploy_host: Optional[str] = None) -> Dict[str, List]:
    findings: List[Dict[str, object]] = []
    errors: List[str] = []
    runtimes = _runtimes()
    # Review 7689 P1-2: an audit that queried no runtime is not evidence.
    # An explicitly empty MEMORA_AUDIT_RUNTIMES, or a runtime installed on
    # this host but left out of it, makes the host NOT clean.
    present = [r for r in RUNTIMES if shutil.which(r)]
    if os.environ.get("MEMORA_AUDIT_RUNTIMES") is not None and not runtimes:
        errors.append("no container runtime audited: MEMORA_AUDIT_RUNTIMES is set and empty")
    skipped = [r for r in present if r not in runtimes]
    if skipped:
        errors.append(f"no container runtime audited for {', '.join(skipped)}: installed here but not queried")
    for runtime in runtimes:
        try:
            listed = all_containers(runtime)
        except RuntimeQueryFailed as exc:
            errors.append(f"runtime {runtime}: cannot list containers: {exc}")
            continue
        for name, state in listed:
            try:
                env = container_env(runtime, name)
            except RuntimeQueryFailed as exc:
                errors.append(f"runtime {runtime}: {exc}")
                continue
            owner_is_all = bool(deploy_host) and host == deploy_host and name == "memora-all"
            for f in scan_text("\n".join(env)):
                entry = {"host": host, "file": f"{runtime}:{name}", "line": 0, "kind": "runtime_env",
                         "detail": f["kind"], "container": name, "state": state, "value": f["value"],
                         "memora_all": owner_is_all}
                if "name" in f:
                    entry["name"] = f["name"]
                findings.append(entry)
    return {"findings": findings, "errors": errors, "runtimes": _runtimes()}


def coverage(roots: List[Path], files: bool, runtimes: List[str]) -> str:
    parts = [f"files under {', '.join(str(r) for r in roots)} (this user's view)" if files else "no files"]
    if runtimes:
        parts.append(f"every container of {', '.join(runtimes)} visible to this user "
                     "(another user's rootless runtime is not visible)")
    elif os.environ.get("MEMORA_AUDIT_RUNTIMES") is not None:
        parts.append("NO containers: MEMORA_AUDIT_RUNTIMES is set and empty (not clean)")
    else:
        parts.append("no container runtime installed")
    return "; ".join(parts)


def audit(roots: List[Path], *, host: str = "local", memora_all: Optional[List[str]] = None,
          max_depth: int = MAX_DEPTH, files: bool = True, containers: bool = True,
          deploy_host: Optional[str] = None) -> Dict[str, object]:
    findings: List[Dict[str, object]] = []
    errors: List[str] = []
    seen = set()
    for root in (roots if files else []):
        for path in candidate_files(root, max_depth=max_depth):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                if path.stat().st_size > MAX_BYTES:
                    errors.append(f"{path}: larger than {MAX_BYTES} bytes, not read")
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                errors.append(f"{path}: {exc.strerror or exc}")
                continue
            owner_is_all = _memora_all(path, host, memora_all or [], deploy_host)
            for f in scan_text(text):
                findings.append({"host": host, "file": str(path), **f, "memora_all": owner_is_all})
    runtimes: List[str] = []
    if containers:
        c = audit_containers(host, deploy_host)
        findings += c["findings"]
        errors += c["errors"]
        runtimes = c["runtimes"]
    blocking = [f for f in findings if not f["memora_all"]]
    # An unreadable candidate was not audited: that is not clean either.
    return {"host": host, "clean": not blocking and not errors, "findings": findings,
            "blocking": len(blocking), "errors": errors,
            "coverage": coverage(roots, files, runtimes if containers else [])}


def format_finding(f: Dict[str, object]) -> str:
    tag = "memora-all" if f["memora_all"] else "DIRECT-D1"
    name = f" {f['name']}" if "name" in f else ""
    detail = f" ({f['detail']}, {f['state']})" if "state" in f else (f" ({f['detail']})" if "detail" in f else "")
    return f"{tag:10} {f['host']}:{f['file']}:{f['line']}: {f['kind']}{detail}{name} {f['value']}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", help="directories or files to scan (default: $HOME)")
    ap.add_argument("--host-label", default=os.uname().nodename.split(".")[0])
    ap.add_argument("--deploy-host", default=None, metavar="LABEL",
                    help="the host label that runs memora-all (DEPLOY_HOST)")
    ap.add_argument("--memora-all", action="append", default=[], metavar="PATH",
                    help="a file (glob) that belongs to memora-all itself")
    ap.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--containers-only", action="store_true", help="only the running containers")
    args = ap.parse_args(argv)
    roots = [Path(r) for r in args.roots] or [Path.home()]
    missing = [str(r) for r in roots if not r.expanduser().exists()]
    if missing:
        print(f"config_audit: no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    result = audit(roots, host=args.host_label, memora_all=args.memora_all, max_depth=args.max_depth,
                   files=not args.containers_only, deploy_host=args.deploy_host)
    if args.json:
        print(json.dumps(result))
    else:
        print(f"coverage {result['host']}: {result['coverage']}")
        for f in result["findings"]:
            print(format_finding(f))
        for e in result["errors"]:
            print(f"ERROR      {result['host']}: {e}")
        print(f"{result['host']}: {'clean' if result['clean'] else 'NOT clean'} "
              f"({result['blocking']} direct-D1 finding(s), {len(result['errors'])} error(s))")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
