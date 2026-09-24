#!/usr/bin/env python3
"""A fake `docker` for tests/test_cutover_store.py: memora-all on nuc8.

Every call is appended to $CALL_LOG (args joined by \\x1f, calls by \\x1e).
`exec memora-all python /app/scripts/local_primary.py CMD ...` prints the
operator tool's JSON line; `exec memora-all python -c <health program>`
prints what the script's health program would. State ($STATE, JSON): the
freeze and the last compare, moved by the fake tool calls.

Knobs:
  TOOL_RC_<CMD>     exit status of that tool command (CMD upper-cased, - as _)
  TOOL_OUT_<CMD>    its JSON line instead of the default
  REPL              the replication block (JSON; "null" = none)
  REG_ENTRY         memora-all's MEMORA_DATABASES[db] (default /data/<db>.db)
  ENV_REPLICATION   memora-all's MEMORA_REPLICATION (default write)
  FAKE_INTERVAL_S   the replication block's interval_s (default 60)
  FREEZE_STAYS_OPEN 1: the freeze call succeeds but the store is not frozen
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as fh:
    fh.write("\x1f".join(args) + "\x1f\x1e")
state_path = os.environ["STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"freeze": "open", "compare": None}


def save():
    json.dump(state, open(state_path, "w"))


def knob(name, default=None):
    return os.environ.get(name, default)


if args[:2] == ["exec", "memora-all"]:
    rest = args[2:]
    if rest[:2] == ["python", "/app/scripts/local_primary.py"]:
        cmd, tail = rest[2], rest[3:]
        key = cmd.upper().replace("-", "_")
        rc = int(knob(f"TOOL_RC_{key}", "0"))
        default = {"ok": rc == 0}
        if cmd == "recheck":
            default["receipt"] = tail[tail.index("--receipt") + 1]
        if cmd == "export":
            default["receipt"] = "/data/exports/re/20990101T000000Z.receipt.json"
        if cmd in ("compare", "fk-audit"):
            default = {"clean": rc == 0}
        print(knob(f"TOOL_OUT_{key}") or json.dumps(default))
        if rc == 0:
            if cmd == "freeze" and knob("FREEZE_STAYS_OPEN") != "1":
                state["freeze"] = "frozen"
            if cmd == "thaw":
                state["freeze"] = "open"
            if cmd == "compare":
                state["compare"] = "clean"
            save()
        sys.exit(rc)
    if rest[:2] == ["python", "-c"]:  # the script's /health/db program
        db = rest[3]
        repl = json.loads(knob("REPL") or json.dumps(
            {"mode": "write", "status": "running", "halted_reason": None, "lag_rows": 0, "head_seq": 0,
             "last_acked_seq": 0, "interval_s": float(knob("FAKE_INTERVAL_S", "60")),
             "compare_consumed_seq": 0, "last_compare_mode": None, "last_compare_clean": None}))
        if isinstance(repl, dict) and state.get("compare") == "clean" and not knob("REPL"):
            repl.update(last_compare_mode="barrier", last_compare_clean=True)
        print(json.dumps({
            "status": "ok", "http": 200,
            "freeze": {"state": state["freeze"], "in_flight": 0, "open_intents": []},
            "replication": repl, "registry_entry": knob("REG_ENTRY", f"/data/{db}.db"),
            "env_replicas": {db: "d1://acct/db3"}, "env_replication": knob("ENV_REPLICATION", "write")}))
        sys.exit(0)
    if rest[:2] == ["python3", "-c"]:  # the sql path of a fresh receipt
        print("/data/exports/re/20990101T000000Z.sql")
        sys.exit(0)
    sys.exit(0)  # sh -c (token files), mkdir, rm
if args[0] == "cp":
    if knob("CP_RC"):
        sys.exit(int(knob("CP_RC")))
    if args[1].startswith("memora-all:"):  # out of the container: the file lands on the host
        src = args[1].split(":", 1)[1]
        open(os.path.join(args[2], os.path.basename(src)), "w").write(f"copy of {src}\n")
    sys.exit(0)
sys.exit(0)
