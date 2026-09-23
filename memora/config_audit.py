#!/usr/bin/env python3
"""Find every memora client configuration that can reach Cloudflare D1
directly (docs/local-primary-implementation.md §6 F4-F6, §6.1, slice L8).

After the writer freeze, memora-all on nuc8 is the only process allowed to
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

memora-all's own configuration is reported but does not fail the audit:
instances/all.env (the deploy's registry source, on any host) and, when the
host label is "nuc8", ~/.config/memora/credentials.mcp.json and
~/.config/memora/all.* (memora-all's credential source on nuc8). More
paths can be marked with --memora-all PATH.

Standard library only: scripts/audit_configs.py pipes this file to
`ssh HOST python3 -` to audit other hosts.

Exit: 0 clean (only memora-all findings, or none), 1 a non-memora-all
direct-D1 client remains, 2 usage error.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional

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


def _memora_all(path: Path, host: str, extra: List[str]) -> bool:
    s = path.as_posix()
    if s.endswith("/instances/all.env"):
        return True
    if host == "nuc8":
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


def audit(roots: List[Path], *, host: str = "local", memora_all: Optional[List[str]] = None,
          max_depth: int = MAX_DEPTH) -> Dict[str, object]:
    findings: List[Dict[str, object]] = []
    errors: List[str] = []
    seen = set()
    for root in roots:
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
            owner_is_all = _memora_all(path, host, memora_all or [])
            for f in scan_text(text):
                findings.append({"host": host, "file": str(path), **f, "memora_all": owner_is_all})
    blocking = [f for f in findings if not f["memora_all"]]
    # An unreadable candidate was not audited: that is not clean either.
    return {"host": host, "clean": not blocking and not errors, "findings": findings,
            "blocking": len(blocking), "errors": errors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", help="directories or files to scan (default: $HOME)")
    ap.add_argument("--host-label", default=os.uname().nodename.split(".")[0])
    ap.add_argument("--memora-all", action="append", default=[], metavar="PATH",
                    help="a file (glob) that belongs to memora-all itself")
    ap.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    roots = [Path(r) for r in args.roots] or [Path.home()]
    missing = [str(r) for r in roots if not r.expanduser().exists()]
    if missing:
        print(f"config_audit: no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    result = audit(roots, host=args.host_label, memora_all=args.memora_all, max_depth=args.max_depth)
    if args.json:
        print(json.dumps(result))
    else:
        for f in result["findings"]:
            tag = "memora-all" if f["memora_all"] else "DIRECT-D1"
            name = f" {f['name']}" if "name" in f else ""
            print(f"{tag:10} {f['host']}:{f['file']}:{f['line']}: {f['kind']}{name} {f['value']}")
        for e in result["errors"]:
            print(f"ERROR      {result['host']}: {e}")
        print(f"{result['host']}: {'clean' if result['clean'] else 'NOT clean'} "
              f"({result['blocking']} direct-D1 finding(s), {len(result['errors'])} error(s))")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
