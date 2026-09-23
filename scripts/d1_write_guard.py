#!/usr/bin/env python3
"""Fail when anything in the repo can write Cloudflare D1 outside memora.

docs/local-primary-implementation.md §0 P6 and §6 F2: D1 is the only complete
copy of every store, so the only code allowed to write it is memora itself
(memora/backends.py). This guard finds the other write paths.

Scopes:
  tools     retired scripts and deploy/migration commands (slice L1b). CI
            blocks on this scope.
  handlers  write SQL in the memora-graph Pages functions and worker (slice
            L7). Until L7 makes the viewer read-only this scope FAILS by
            design; CI reports it without blocking, and every scripted Pages
            deploy runs `--scope all`, so no scripted deploy can republish the
            viewer's write handlers.
  all       both (what a deploy runs).

Rules (tools):
  T1  `requests.post` / `requests.request` in a file that names a D1 REST
      endpoint (`/d1/database/`).
  T2  a wrangler D1 `execute` with the remote flag on the same line.
  T3  a Python list that runs a wrangler D1 `execute` or `migrations`
      without a `--local` element.
  T4  a wrangler D1 `migrations apply` without `--local` on the same line.
  T5  a wrangler Pages `deploy` on a line that does not run this guard
      (`d1_write_guard.py`) before it.
Rules (handlers):
  H1  a string literal that starts with write SQL (INSERT … INTO, REPLACE
      INTO, UPDATE … SET, DELETE FROM, CREATE/DROP/ALTER …) in a file under
      memora-graph/functions/ or memora-graph/worker/. The finding names the
      D1 binding names the file uses (DB_MEMORA/DB_OB1/DB_BESTATION/DB_RE, or
      the dynamic `DB_${…}` lookup).

Only code and config are scanned (*.py *.sh *.ts *.js *.mjs *.json *.toml
*.yml *.yaml); prose (docs, CHANGELOG, READMEs) is not executable. The
allow-list is memora/backends.py and nothing else.

Usage: python3 scripts/d1_write_guard.py [--scope tools|handlers|all] [--root DIR]
Exit status: 0 clean, 1 findings, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

POINTER = "docs/local-primary-implementation.md §0 P6, §6 F2/F3"

ALLOW = {"memora/backends.py"}
SUFFIXES = {".py", ".sh", ".ts", ".js", ".mjs", ".json", ".toml", ".yml", ".yaml"}
SKIP_DIRS = {".git", "node_modules", ".wrangler", ".venv", "venv", "__pycache__", "docs", "plans"}
HANDLER_DIRS = ("memora-graph/functions/", "memora-graph/worker/")

_I = re.IGNORECASE
T1_POST = re.compile(r"\brequests\.(post|request)\s*\(")
T1_URL = re.compile(r"/d1/database/")
T2 = re.compile(r"wrangler\s+d1\s+execute\b[^\n]*--remote\b")
T3_LIST = re.compile(r"\[[^\[\]]*?[\"']wrangler[\"']\s*,\s*[\"']d1[\"']\s*,\s*[\"'](execute|migrations)[\"'][^\[\]]*\]", re.S)
T4 = re.compile(r"wrangler\s+d1\s+migrations\s+apply\b[^\n]*")
T5 = re.compile(r"wrangler\s+pages\s+deploy\b")
H1 = re.compile(
    r"([\"'`])\s*("
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\b|REPLACE\s+INTO\b|UPDATE\s+(?:OR\s+\w+\s+)?[\w\"`\[\]]+\s+SET\b|"
    r"DELETE\s+FROM\b|CREATE\s+(?:TEMP\w*\s+|UNIQUE\s+|VIRTUAL\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\b|"
    r"DROP\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b|ALTER\s+TABLE\b)",
    _I,
)
BINDINGS = re.compile(r"\bDB_(MEMORA|OB1|BESTATION|RE)\b|DB_\$\{")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_file() and path.suffix in SUFFIXES and rel not in ALLOW:
            yield rel, path


def scan(root: Path, scope: str) -> list[str]:
    findings: list[str] = []
    for rel, path in _files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if scope in ("tools", "all"):
            if T1_POST.search(text) and T1_URL.search(text):
                m = T1_POST.search(text)
                findings.append(f"{rel}:{_line(text, m.start())}: T1 requests call to a D1 REST endpoint")
            for m in T2.finditer(text):
                findings.append(f"{rel}:{_line(text, m.start())}: T2 remote wrangler D1 execute")
            for m in T3_LIST.finditer(text):
                if not re.search(r"[\"']--local[\"']", m.group(0)):
                    findings.append(f"{rel}:{_line(text, m.start())}: T3 wrangler D1 command list without --local")
            for m in T4.finditer(text):
                if "--local" not in m.group(0):
                    findings.append(f"{rel}:{_line(text, m.start())}: T4 wrangler D1 migrations apply without --local")
            for m in T5.finditer(text):
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                line = text[start: end if end != -1 else len(text)]
                if "d1_write_guard.py" not in line[: m.start() - start]:
                    findings.append(f"{rel}:{_line(text, m.start())}: T5 Pages deploy not preceded by the guard")
        if scope in ("handlers", "all") and rel.startswith(HANDLER_DIRS):
            names = sorted({b.group(0) for b in BINDINGS.finditer(text)})
            for m in H1.finditer(text):
                sql = " ".join(m.group(2).split())
                via = f" (bindings: {', '.join(names)})" if names else ""
                findings.append(f"{rel}:{_line(text, m.start())}: H1 write SQL `{sql}`{via}")
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scope", choices=("tools", "handlers", "all"), default="all")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"d1_write_guard: no such directory: {root}", file=sys.stderr)
        return 2
    findings = scan(root, args.scope)
    for f in findings:
        print(f)
    if findings:
        print(f"d1_write_guard: {len(findings)} finding(s) in scope '{args.scope}'; see {POINTER}", file=sys.stderr)
        return 1
    print(f"d1_write_guard: clean (scope '{args.scope}')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
