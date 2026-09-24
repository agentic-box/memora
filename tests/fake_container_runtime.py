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
  INSPECT_FROM_RUN  1: `inspect NAME` (no --format) of the container `run -d`
                  created answers from that run's argv: Config.Env from its
                  -e flags, Mounts from its -v flags (RW false for :ro)
  RUN_INSPECT_EXTRA_ENV  an extra Config.Env entry in that answer
  RUN_INSPECT_RW  1: report every mount of that answer writable
  SECRETS_UNREADABLE  1: a `run --rm … python -c` with the token mount runs
                  its program against a directory that does not exist

A `run --rm … python -c PROGRAM` that mounts a directory at
/run/secrets/memora (the deploy's token-file preflight) really runs PROGRAM
with this repo's memora, its -e variables, and /run/secrets/memora mapped
to the host directory.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # run via a docker symlink
SECRETS_MOUNT = "/run/secrets/memora"

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
    argv_out = os.environ.get("ARGV_OUT", "")
    if os.environ.get("INSPECT_FROM_RUN") == "1" and argv_out and os.path.exists(argv_out):
        ran = open(argv_out).read().splitlines()
        if ran[ran.index("--name") + 1] == args[1]:
            env = [ran[i + 1] for i, a in enumerate(ran) if a == "-e"]
            if os.environ.get("RUN_INSPECT_EXTRA_ENV"):
                env.append(os.environ["RUN_INSPECT_EXTRA_ENV"])
            mounts = []
            for i, a in enumerate(ran):
                if a == "-v":
                    src, dst, *opt = ran[i + 1].split(":")
                    mounts.append({"Name": src, "Source": src, "Destination": dst,
                                   "RW": os.environ.get("RUN_INSPECT_RW") == "1"
                                   or "ro" not in (opt[0].split(",") if opt else [])})
            print(json.dumps([{"Name": args[1], "Config": {"Env": env}, "Mounts": mounts}]))
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
    name = args[-1] if args[1] == "create" else args[2]  # `volume create [--label k=v]... NAME`
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
    host_secrets = next((args[i + 1].split(":")[0] for i, a in enumerate(args)
                         if a == "-v" and args[i + 1].split(":")[1:2] == [SECRETS_MOUNT]), None)
    if host_secrets and "python" in args and "-c" in args:
        if os.environ.get("SECRETS_UNREADABLE") == "1":
            host_secrets = os.path.join(host_secrets, "does-not-exist")
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": REPO, "PYTHONDONTWRITEBYTECODE": "1"}
        for i, a in enumerate(args):
            if a == "-e":
                k, v = args[i + 1].split("=", 1)
                env[k] = v.replace(SECRETS_MOUNT, host_secrets, 1)
        code = args[args.index("-c") + 1]
        # cwd /: memora/__init__ reads a .mcp.json found above the cwd
        sys.exit(subprocess.run([sys.executable, "-c", code], env=env, cwd="/").returncode)
    sys.exit(0)
sys.exit(0)
