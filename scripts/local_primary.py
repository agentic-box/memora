#!/usr/bin/env python3
"""Local-primary operator tool (docs/local-primary-implementation.md §0 P1, §4).

Run by hand, never by the service. The logic lives in memora/local_primary.py;
this file only parses arguments and builds the dependencies.

  freeze  <db>              place memora-all's freeze on the store (POST /admin/freeze)
  export  <db>              verified export under the freeze, R2 copy, receipt
  recheck <db> --receipt R  under the SAME freeze: D1 unchanged since R? (else a fresh export)
  seed    <db> --receipt R --out /data/<db>.db   (rechecks R under the same freeze first)
  sequence-highwater <db> --receipt R --local /data/<db>.db --credential-file F [--dry-run]
  thaw    <db>              lift the freeze -- the only command that does
  snapshot <db> --store /data/<db>.db          nightly: backup, gzip, R2, keep 14
  volume-check --store /data/<db>.db ...      alert (exit 4) when free space is low

No command lifts the freeze by itself: a recheck and the step that relies
on it (seed, sequence-highwater) run under one freeze, lifted by `thaw`
when the procedure is done.

Exit codes: 0 done, 2 refused (nothing changed), 3 halted (see the message),
4 volume alert.
Every run prints one JSON line on stdout with the outcome.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memora import local_primary as lp  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local_primary.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("db", help="the store's name in MEMORA_DATABASES")
        sp.add_argument("--account", required=True, help="Cloudflare account id")
        sp.add_argument("--database-id", required=True, help="the D1 database id")
        sp.add_argument("--d1-name", help="the D1 database name (native export); default: <db>")
        sp.add_argument("--read-token-file", help="0600 file with the D1 READ token (default: MEMORA_D1_READ_TOKEN)")
        sp.add_argument("--memora-url", default="http://127.0.0.1:8000", help="memora-all's base URL")
        sp.add_argument("--admin-token-file", help="0600 file with the admin token (freeze)")
        sp.add_argument("--health-token-file", help="0600 file with MEMORA_HEALTH_TOKEN (required with the freeze)")
        sp.add_argument("--service-stopped", action="store_true",
                        help="the barrier is memora-all being stopped (docker), not the freeze")
        sp.add_argument("--container", default="memora-all")
        r2 = sp.add_mutually_exclusive_group(required=True)
        r2.add_argument("--r2-bucket", help="R2 bucket (S3 API)")
        r2.add_argument("--r2-dir", help="a directory standing in for R2 (rehearsals)")
        sp.add_argument("--out-dir", default="/data/exports")
        sp.add_argument("--native-export", action="store_true",
                        help="try `wrangler d1 export --remote` first (read token); the paged SELECT is the fallback")

    for name, text in (("freeze", "place the freeze"), ("thaw", "lift the freeze (the only command that does)")):
        fz = sub.add_parser(name, help=text)
        fz.add_argument("db")
        fz.add_argument("--memora-url", default="http://127.0.0.1:8000")
        fz.add_argument("--admin-token-file", required=True)
        fz.add_argument("--health-token-file", required=True)
    common(sub.add_parser("export", help="P1 verified export with receipt"))
    rc = sub.add_parser("recheck", help="P1 freeze-recheck of a receipt")
    common(rc)
    rc.add_argument("--receipt", required=True)
    sd = sub.add_parser("seed", help="§4 a new local store from a verified export")
    common(sd)
    sd.add_argument("--receipt", required=True)
    sd.add_argument("--out", required=True)
    sd.add_argument("--replica-uri", help="optional; must equal d1://<account>/<database-id> (derived from them)")
    sd.add_argument("--rehearse", action="store_true", help="seed into a temp path instead of --out")
    sq = sub.add_parser("sequence-highwater", help="§4 H7 raise D1's sqlite_sequence to the local high-water")
    common(sq)
    sq.add_argument("--receipt", required=True)
    sq.add_argument("--local", required=True, help="the local store file")
    sq.add_argument("--credential-file", help="0600 file with the operator's D1 edit token")
    sq.add_argument("--dry-run", action="store_true")
    sn = sub.add_parser("snapshot", help="§4 nightly snapshot of a local store to R2")
    sn.add_argument("db")
    sn.add_argument("--store", required=True)
    r2 = sn.add_mutually_exclusive_group(required=True)
    r2.add_argument("--r2-bucket")
    r2.add_argument("--r2-dir")
    sn.add_argument("--work-dir", default="/data/snapshots-tmp")
    sn.add_argument("--keep", type=int, default=lp.SNAPSHOT_KEEP)
    vc = sub.add_parser("volume-check", help="§4 volume alert")
    vc.add_argument("--store", action="append", required=True)
    vc.add_argument("--min-free-pct", type=float, default=10.0)
    return p


def _freeze_client(args) -> lp.FreezeClient:
    if not args.health_token_file:
        raise lp.L5Refused("--health-token-file is required with the freeze (it is never the admin token)")
    admin = lp.load_credential_file(args.admin_token_file)
    health = lp.load_credential_file(args.health_token_file)
    return lp.FreezeClient(args.memora_url, admin, args.db, health_token=health)


def _deps(args) -> lp.Deps:
    from memora.backends import D1SelectOnlyConnection

    token = lp.read_token(args.read_token_file)
    reader = lp.D1Reader(D1SelectOnlyConnection(args.account, args.database_id, token))
    if args.service_stopped:
        barrier = lp.ServiceStopped(args.container)
    else:
        if not args.admin_token_file:
            raise lp.L5Refused("--admin-token-file is required to place the freeze (or pass --service-stopped)")
        barrier = _freeze_client(args)
    writer_factory = None
    if getattr(args, "credential_file", None):
        lp.load_credential_file(args.credential_file)  # refuse a bad file before anything runs
        writer_factory = lambda: lp.OperatorD1Writer.from_credential_file(  # noqa: E731
            args.account, args.database_id, args.credential_file)
    return lp.Deps(reader=reader, freeze=barrier, r2=_r2(args), account_id=args.account,
                   database_id=args.database_id, d1_name=args.d1_name, read_token=token,
                   native_export=args.native_export, writer_factory=writer_factory)


def _r2(args):
    return lp.FsR2(Path(args.r2_dir)) if args.r2_dir else lp.S3R2(args.r2_bucket)


FROZEN_STEPS = ("export", "recheck", "seed", "sequence-highwater")


def _recovery(args) -> dict:
    """A failed step leaves the freeze in place on purpose (review 7621
    P1-2); say how to lift it if the procedure is abandoned (7630 v)."""
    if args.cmd not in FROZEN_STEPS or getattr(args, "service_stopped", False):
        return {}
    return {"freeze": "left in place (a failed step never lifts it)",
            "recovery": f"to abandon the procedure: local_primary.py thaw {args.db} "
                        f"--memora-url {args.memora_url} --admin-token-file <file> --health-token-file <file>"}


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd in ("freeze", "thaw"):
            client = _freeze_client(args)
            client.freeze() if args.cmd == "freeze" else client.thaw()
            print(json.dumps({"ok": True, args.cmd: args.db}))
            return 0
        if args.cmd == "volume-check":
            out = lp.volume_check([Path(s) for s in args.store], min_free_pct=args.min_free_pct)
            print(json.dumps(out))
            return 0 if out["ok"] else 4
        if args.cmd == "snapshot":
            out = {"ok": True, **lp.snapshot(args.db, Path(args.store), _r2(args), Path(args.work_dir),
                                             keep=args.keep)}
            print(json.dumps(out))
            return 0
        deps = _deps(args)
        if args.cmd == "export":
            receipt = lp.export(args.db, deps, Path(args.out_dir))
            out = {"ok": True, "receipt": str(receipt)}
        elif args.cmd == "recheck":
            receipt = lp.recheck(args.db, args.receipt, deps, Path(args.out_dir))
            out = {"ok": True, "receipt": str(receipt), "fresh_export": str(receipt) != args.receipt}
        elif args.cmd == "seed":
            out = {"ok": True, **lp.seed(args.db, args.receipt, Path(args.out), deps, Path(args.out_dir),
                                         replica_uri=args.replica_uri, rehearse=args.rehearse)}
        else:
            out = {"ok": True, **lp.sequence_highwater(args.db, args.receipt, Path(args.local), deps,
                                                       Path(args.out_dir), dry_run=args.dry_run)}
    except lp.L5Halt as exc:
        print(json.dumps({"ok": False, "halted": str(exc), **_recovery(args)}))
        return 3
    except lp.L5Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc), **_recovery(args)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
