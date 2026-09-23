#!/usr/bin/env python3
"""Audit hosts for memora clients that can reach Cloudflare D1 directly
(docs/local-primary-implementation.md §6 F4-F6, slice L8).

  scripts/audit_configs.py                       # this host, $HOME
  scripts/audit_configs.py --local ~/repos/agentic-box
  scripts/audit_configs.py --host nuc8 --host ob1 --host bestation --host re
  scripts/audit_configs.py --local --host nuc8 --json

Each remote host is audited by piping memora/config_audit.py (standard
library only) to `ssh -o BatchMode=yes HOST python3 - --json`, with the host
name as its label, so nothing needs to be installed there. Token values are
masked on the host they are read on and never cross the network in full.

A host that cannot be audited (ssh or python3 fails, or its output is not
the audit's JSON) counts as NOT clean: an unaudited host may still hold a
direct-D1 client.

Exit: 0 every host clean, 1 a direct-D1 client remains or a host could not
be audited, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memora import config_audit  # noqa: E402

AUDIT_SOURCE = Path(config_audit.__file__)


def audit_remote(host: str, roots, memora_all, *, ssh: str = "ssh", timeout: int = 300) -> dict:
    cmd = [ssh, "-o", "BatchMode=yes", host, "python3", "-", "--json", "--host-label", host]
    for m in memora_all:
        cmd += ["--memora-all", m]
    cmd += list(roots)
    try:
        r = subprocess.run(cmd, input=AUDIT_SOURCE.read_text(), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"host": host, "clean": False, "findings": [], "blocking": 0,
                "errors": [f"could not run the audit over ssh: {exc}"]}
    try:
        result = json.loads(r.stdout.strip().splitlines()[-1])
        if not isinstance(result, dict) or result.get("host") != host or "clean" not in result:
            raise ValueError("unexpected output")
    except (ValueError, IndexError):
        detail = (r.stderr or r.stdout).strip()[-300:]
        return {"host": host, "clean": False, "findings": [], "blocking": 0,
                "errors": [f"audit did not complete (exit {r.returncode}): {detail}"]}
    if r.returncode not in (0, 1) or bool(result["clean"]) != (r.returncode == 0):
        result = dict(result, clean=False,
                      errors=list(result.get("errors", [])) + [f"audit exit {r.returncode} disagrees with its report"])
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", nargs="*", metavar="ROOT", default=None,
                    help="audit this host (default root: $HOME)")
    ap.add_argument("--host", action="append", default=[], help="a host to audit over ssh")
    ap.add_argument("--remote-root", action="append", default=[],
                    help="roots on the remote hosts (default: their $HOME)")
    ap.add_argument("--memora-all", action="append", default=[], metavar="PATH")
    ap.add_argument("--ssh", default="ssh", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.local is None and not args.host:
        args.local = []
    results = []
    if args.local is not None:
        roots = [Path(r) for r in args.local] or [Path.home()]
        missing = [str(r) for r in roots if not r.expanduser().exists()]
        if missing:
            print(f"audit_configs: no such path: {', '.join(missing)}", file=sys.stderr)
            return 2
        label = config_audit.os.uname().nodename.split(".")[0]
        results.append(config_audit.audit(roots, host=label, memora_all=args.memora_all))
    for host in args.host:
        results.append(audit_remote(host, args.remote_root, args.memora_all, ssh=args.ssh))
    clean = all(r["clean"] for r in results)
    if args.json:
        print(json.dumps({"clean": clean, "hosts": results}))
    else:
        for r in results:
            for f in r["findings"]:
                tag = "memora-all" if f["memora_all"] else "DIRECT-D1"
                name = f" {f['name']}" if "name" in f else ""
                print(f"{tag:10} {r['host']}:{f['file']}:{f['line']}: {f['kind']}{name} {f['value']}")
            for e in r["errors"]:
                print(f"ERROR      {r['host']}: {e}")
            print(f"{r['host']}: {'clean' if r['clean'] else 'NOT clean'} ({r['blocking']} direct-D1 finding(s))")
        print("ALL CLEAN" if clean else "NOT CLEAN: repoint the clients above to memora-all (docs/local-primary-credentials.md)")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
