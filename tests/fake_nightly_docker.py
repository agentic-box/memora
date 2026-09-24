#!/usr/bin/env python3
"""A fake `docker` for tests/test_nightly_compare.py: the running memora-all.

Every call is appended to $CALL_LOG (args joined by \\x1f, calls by \\x1e).
`exec <c> python -c <program> stores|health <db>` answers the script's
in-container program; `exec <c> python /app/scripts/local_primary.py
compare <db> ...` answers like the operator tool.

Knobs (JSON in env):
  NC_STORES    {db: {"uri": ..., "store": ...}} (default: two stores)
  NC_RC        {db: exit status of its compare} (default 0)
  NC_OUT       {db: the compare's JSON line (object)} (default: a clean summary)
  NC_REPL      {db: the replication block} (default: running, would_halt_count 0)
  NC_SLEEP     seconds each compare takes (to prove the runs never overlap)
  NC_RUNNING   a file that exists while a compare runs (overlap detection)
"""
import json
import os
import sys
import time

args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as fh:
    fh.write("\x1f".join(args) + "\x1f\x1e")

STORES = {"alpha": {"uri": "d1://acct-a/db-a", "store": "/data/alpha.db"},
          "beta": {"uri": "d1://acct-b/db-b", "store": "/data/beta.db"}}


def knob(name, default):
    raw = os.environ.get(name)
    return json.loads(raw) if raw else default


if args[:1] == ["exec"]:
    rest = args[2:]
    if rest[:2] == ["python", "-c"]:
        what = rest[3]
        if what == "stores":
            print(json.dumps(knob("NC_STORES", STORES)))
        else:
            db = rest[4]
            print(json.dumps(knob("NC_REPL", {}).get(db, {"status": "running", "would_halt_count": 0})))
        sys.exit(0)
    if rest[:2] == ["python", "/app/scripts/local_primary.py"] and rest[2] == "compare":
        db = rest[3]
        running = os.environ.get("NC_RUNNING")
        if running:
            if os.path.exists(running):
                print(json.dumps({"ok": False, "error": "OVERLAP"}))
                sys.exit(99)
            open(running, "w").close()
        time.sleep(float(os.environ.get("NC_SLEEP", "0")))
        rc = int(knob("NC_RC", {}).get(db, 0))
        out = knob("NC_OUT", {}).get(db) or {"ok": rc == 0, "clean": rc == 0, "diff_count": 0 if rc == 0 else 2,
                                             "d1_missing_vectors": 0, "skipped": None, "recorded": True}
        print(json.dumps(out))
        if running:
            os.remove(running)
        sys.exit(rc)
    sys.exit(0)  # sh -c (token files), rm
sys.exit(0)
