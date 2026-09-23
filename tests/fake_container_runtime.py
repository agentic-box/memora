#!/usr/bin/env python3
"""A fake `docker` / `container` runtime for the launcher tests.

Every call is appended to $CALL_LOG (args joined by \\x1f, calls by \\x1e).
Volumes are directories under $VOLROOT/<name>. A `run --rm` that carries the
/data migration program (scripts/migrate_data_volume.sh) really runs it, with
/from and /to mapped to those directories, so the tests see the bytes that
would land in the named volume. Nothing touches a real runtime.

Environment knobs:
  CURRENT_MOUNT   what the existing container mounts at /data (inspect)
  EXISTING        container name `list --all` reports (unset = none)
  INSPECT_OUT     raw text `inspect NAME` prints instead of the JSON
  INSPECT_RC / LIST_RC / LIST_ALL_RC / PS_RC
                  force `inspect` / `list` / `list --all` / `ps` to fail
  RUNNING_LIST    text `list` prints (e.g. "memora-t running")
  PS_RUNNING      volume name `ps -q --filter volume=` reports as in use
  RENAME_RC       exit status of `rename` (default 0)
  COPY_RC         force the migration run to exit with this status
  ARGV_OUT        where `run -d` writes its argv, one per line
"""
import json
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as fh:
    fh.write("\x1f".join(args) + "\x1f\x1e")

volroot = os.environ.get("VOLROOT", "")


def vol(name):
    # A host path (bind mount) is itself; a volume name is a directory.
    return name if name.startswith("/") else os.path.join(volroot, name)


verb = args[0] if args else ""
listing_all = verb == "list" and "--all" in args
for knob, name in (("INSPECT_RC", "inspect"), ("LIST_RC", "list"), ("LIST_ALL_RC", "list --all"), ("PS_RC", "ps")):
    this = "list --all" if listing_all else verb
    if this == name and os.environ.get(knob):
        print(f"{name}: simulated failure", file=sys.stderr)
        sys.exit(int(os.environ[knob]))
if verb == "inspect":
    if "--format" in args:  # deploy: `docker inspect memora-all --format …`
        print(os.environ.get("CURRENT_MOUNT", ""))
        sys.exit(0)
    if os.environ.get("INSPECT_OUT") is not None:
        print(os.environ["INSPECT_OUT"])
        sys.exit(0)
    cur = os.environ.get("CURRENT_MOUNT", "")
    if not cur:
        sys.exit(1)
    print(json.dumps([{"Name": args[1], "Mounts": [{"Name": cur, "Destination": "/data"}]}]))
    sys.exit(0)
if verb == "volume":
    name = args[2]
    if args[1] == "inspect":
        if not os.path.isdir(vol(name)):
            sys.exit(1)
        if "--format" in args:
            print(name)
        sys.exit(0)
    if args[1] == "create":
        os.makedirs(vol(name), exist_ok=True)
        print(name)
        sys.exit(0)
if verb == "list":
    if "--all" in args:
        if os.environ.get("EXISTING"):
            print("ID IMAGE STATE")
            print(f"{os.environ['EXISTING']} memora stopped")
        sys.exit(0)
    print(os.environ.get("RUNNING_LIST", ""))
    sys.exit(0)
if verb == "ps":
    target = next((a.split("=", 1)[1] for a in args if a.startswith("volume=")), None)
    if target and target == os.environ.get("PS_RUNNING"):
        print("abc123")
    sys.exit(0)
if verb == "rename":
    sys.exit(int(os.environ.get("RENAME_RC", "0")))
if verb == "exec":
    sys.stdin.read()
    sys.exit(0)
if verb == "run":
    if "-d" in args:
        with open(os.environ["ARGV_OUT"], "w") as fh:
            fh.write("\n".join(args) + "\n")
        sys.exit(0)
    if "migrate_data_volume" in args:
        if os.environ.get("COPY_RC"):
            sys.exit(int(os.environ["COPY_RC"]))
        mounts = {}
        for i, a in enumerate(args):
            if a == "-v":
                src, dst = args[i + 1].split(":")[:2]
                mounts[dst] = vol(src)
        i = args.index("-c")
        script, rest = args[i + 1], args[i + 2:]
        env = dict(os.environ, MIGRATE_FROM=mounts["/from"], MIGRATE_TO=mounts["/to"])
        sys.exit(subprocess.run(["sh", "-c", script, *rest], env=env).returncode)
    sys.exit(0)
sys.exit(0)
