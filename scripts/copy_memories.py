#!/usr/bin/env python3
"""Copy memories from one store of memora-all to another (Item 95).

Run INSIDE the memora-all container (the only execution location: the copy
lock and state live on its /data volume), against its own endpoint:

  docker exec memora-all python /app/scripts/copy_memories.py \\
      --source memora --dest acebar --tag acebar \\
      --health-token-file /dev/shm/h.token --plan /data/copy/plan.json          # dry-run

  docker exec memora-all python /app/scripts/copy_memories.py \\
      --source memora --dest acebar --plan /data/copy/plan.json --apply \\
      --health-token-file /dev/shm/h.token

The dry-run reads the source and the destination and writes only the plan file
(0600): the selected rows in full, their links and a digest. It sends no write
to any store. --apply copies from that saved plan and refuses if the source no
longer matches it. docs/add-store-runbook.md explains the policy for links,
metadata references and retries.

Exit: 0 done (a dry-run prints its report), 2 refused or failed (the message
says what was and was not done), 4 done but the source changed during the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memora import copy_memories as cm  # noqa: E402
from memora import local_primary as lp  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copy_memories.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, help="the store to read (never written)")
    p.add_argument("--dest", required=True, help="the store to write, through its running server")
    p.add_argument("--tag", help="copy the memories carrying this tag (dry-run)")
    p.add_argument("--follow", default="active", help="lineage filter for the selection (default: active)")
    p.add_argument("--project", help="metadata.project for the copies (default: the destination's name)")
    p.add_argument("--plan", required=True, help="the plan file: written by the dry-run, read by --apply")
    p.add_argument("--apply", action="store_true", help="copy from --plan (default: dry-run)")
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="memora-all's base URL")
    p.add_argument("--health-token-file", required=True, help="0600 file with MEMORA_HEALTH_TOKEN")
    p.add_argument("--state-dir", default="/data/copy", help="lock, state and manifest (on memora-all's volume)")
    p.add_argument("--timeout", type=float, default=600.0, help="per-call HTTP timeout in seconds")
    p.add_argument("--max-wait", type=float, default=240.0, help="how long to wait out the import cooldown")
    p.add_argument("--allow-unrepresentable", action="store_true",
                   help="copy without edges memory_link cannot create (listed in the manifest)")
    p.add_argument("--allow-unknown-refs", action="store_true",
                   help="copy reference-shaped metadata this tool cannot remap, unchanged")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        health = lp.load_credential_file(args.health_token_file)
        src = cm.McpEndpoint(args.base_url, args.source, health, timeout=args.timeout)
        dest = cm.McpEndpoint(args.base_url, args.dest, health, timeout=args.timeout)
        if not args.apply:
            if not args.tag:
                raise cm.CopyError("the dry-run needs --tag")
            plan = cm.build_plan(src, dest, tag=args.tag, follow=args.follow, project=args.project or args.dest)
            cm.write_json(Path(args.plan), plan)
            print(json.dumps({"ok": True, "dry_run": True, "plan": args.plan, "digest": plan["digest"],
                              "report": plan["report"]}, indent=2))
            return 0
        plan = cm.load_plan(Path(args.plan))
        opts = cm.ApplyOptions(state_dir=Path(args.state_dir), allow_unrepresentable=args.allow_unrepresentable,
                               allow_unknown_refs=args.allow_unknown_refs, max_wait_s=args.max_wait,
                               log=lambda m: print(m, file=sys.stderr))
        out = cm.apply_plan(plan, src, dest, opts)
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 4
    except (cm.CopyError, lp.L5Refused) as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
