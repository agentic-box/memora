#!/usr/bin/env python3
"""Fail when anything in the repo can write Cloudflare D1 outside memora.

docs/local-primary-implementation.md §0 P6 and §6 F2: D1 is the only complete
copy of every store, so the only code allowed to write it is memora itself
(memora/backends.py). This guard finds the other write paths.

Scopes:
  tools     retired scripts and deploy/migration commands (slice L1b). CI
            blocks on this scope.
  handlers  write SQL in the memora-graph Pages functions and worker. Since
            slice L7 made the viewer read-only this scope is clean, and CI
            blocks on it too.
  all       both (what a deploy runs).

Rules (tools):
  T1  `requests.post` / `requests.request` in a file that names a D1 REST
      endpoint (`/d1/database/`).
  T2  a wrangler D1 `execute` with the remote flag.
  T3  a Python list that runs a wrangler D1 `execute` or `migrations`
      without a `--local` element.
  T4  a wrangler D1 `migrations apply` without `--local`.
  T5  a wrangler Pages `deploy` not directly preceded, in the same command
      line, by an EXECUTED run of this guard with `--scope all`:
      `python3 …/d1_write_guard.py --scope all && … deploy`, or
      `… --scope all || { …; exit 1; }; … deploy`. The guard's name in a
      comment, an `echo` or a string does not count.
  T6  a wrangler D1 `execute` or `migrations apply` whose arguments come
      from a shell variable (`$X`, `${X}`) and that has no literal
      `--local`: the remote flag may be in the variable.
  T2, T4, T5 and T6 read LOGICAL lines: a line ending in a backslash is
  joined with the next, so a flag on a continuation line is seen.
Rules (handlers), in files under memora-graph/functions/ or
memora-graph/worker/; findings name the D1 binding names the file uses
(any DB_<NAME> binding, e.g. DB_MEMORA, except the DB_CONFIG var; or the
dynamic `DB_${…}` lookup -- the store set is configuration, CFG1):
  H1  a string literal that starts with write SQL (INSERT … INTO, REPLACE
      INTO, UPDATE … SET, DELETE FROM, CREATE/DROP/ALTER …), including a
      template literal whose table is interpolated
      (`UPDATE ${table} SET`).
  H2  a string literal or template fragment that starts with an UPPERCASE
      write verb (INSERT, REPLACE, UPDATE, DELETE, CREATE, DROP, ALTER)
      followed by a space or the end of the literal: the head of write SQL
      built by concatenation (`"UPDATE " + table + " SET …"`). Uppercase
      only, so prose ("Update an existing memory") is not a finding.

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
T6 = re.compile(r"wrangler\s+d1\s+(?:execute|migrations\s+apply)\b[^\n]*")
T6_VAR = re.compile(r"\$\{?[A-Za-z_]\w*")
# The prefix of a deploy line must END with an executed guard run.
T5_GUARD = re.compile(
    r"(?:^|[;&|({]|\$\(|\":\s*\")\s*python3?\s+\"?[^\s\"]*d1_write_guard\.py\"?\s+--scope\s+all\s*"
    r"(?:&&\s*|\|\|\s*\{[^{}]*\bexit\s+1\s*;?\s*\}\s*;\s*)"
    r"(?:[A-Za-z_]\w*=\$\(\s*)?(?:npx\s+)?$"
)
T3_LIST = re.compile(r"\[[^\[\]]*?[\"']wrangler[\"']\s*,\s*[\"']d1[\"']\s*,\s*[\"'](execute|migrations)[\"'][^\[\]]*\]", re.S)
T4 = re.compile(r"wrangler\s+d1\s+migrations\s+apply\b[^\n]*")
T5 = re.compile(r"wrangler\s+pages\s+deploy\b")
H1 = re.compile(
    r"([\"'`])\s*("
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\b|REPLACE\s+INTO\b|UPDATE\s+(?:OR\s+\w+\s+)?[\w\"`\[\]]+\s+SET\b|"
    r"DELETE\s+FROM\b|CREATE\s+(?:TEMP\w*\s+|UNIQUE\s+|VIRTUAL\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\b|"
    r"DROP\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b|ALTER\s+TABLE\b|"
    r"UPDATE\s+(?:OR\s+\w+\s+)?\$\{[^}]*\}\s*SET\b)",
    _I,
)
# Case-SENSITIVE on purpose: the head of concatenated write SQL.
H2 = re.compile(r"([\"'`]|\})\s*(INSERT|REPLACE|UPDATE|DELETE|CREATE|DROP|ALTER)(?=\s|[\"'`]|\$\{)")
BINDINGS = re.compile(r"\bDB_(?!CONFIG\b)[A-Z][A-Z0-9_]*\b|DB_\$\{")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _logical_lines(text: str):
    """(first line number, text) per logical line: a physical line ending in
    a backslash is joined with the next one."""
    start, buf = 1, []
    for n, line in enumerate(text.split("\n"), 1):
        if not buf:
            start = n
        if line.rstrip().endswith("\\"):
            buf.append(line.rstrip()[:-1])
            continue
        buf.append(line)
        yield start, " ".join(buf)
        buf = []
    if buf:
        yield start, " ".join(buf)


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
            for m in T3_LIST.finditer(text):
                if not re.search(r"[\"']--local[\"']", m.group(0)):
                    findings.append(f"{rel}:{_line(text, m.start())}: T3 wrangler D1 command list without --local")
            for n, line in _logical_lines(text):
                if T2.search(line):
                    findings.append(f"{rel}:{n}: T2 remote wrangler D1 execute")
                for m in T4.finditer(line):
                    if "--local" not in m.group(0):
                        findings.append(f"{rel}:{n}: T4 wrangler D1 migrations apply without --local")
                for m in T6.finditer(line):
                    if T6_VAR.search(m.group(0)) and "--local" not in m.group(0) and not T2.search(line):
                        findings.append(f"{rel}:{n}: T6 wrangler D1 command with flags from a variable and no --local")
                for m in T5.finditer(line):
                    if not T5_GUARD.search(line[: m.start()]):
                        findings.append(f"{rel}:{n}: T5 Pages deploy not directly preceded by an executed guard run (--scope all)")
        if scope in ("handlers", "all") and rel.startswith(HANDLER_DIRS):
            names = sorted({b.group(0) for b in BINDINGS.finditer(text)})
            via = f" (bindings: {', '.join(names)})" if names else ""
            h1_at = set()
            for m in H1.finditer(text):
                sql = " ".join(m.group(2).split())
                h1_at.add(m.start(2))
                findings.append(f"{rel}:{_line(text, m.start())}: H1 write SQL `{sql}`{via}")
            for m in H2.finditer(text):
                if m.start(2) in h1_at:
                    continue
                findings.append(f"{rel}:{_line(text, m.start())}: H2 write verb `{m.group(2)}` heading a SQL string{via}")
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
