#!/usr/bin/env python3
"""Local-primary operator tool (docs/local-primary-implementation.md §0 P1, §4).

Run by hand, never by the service. The logic lives in memora/local_primary.py;
this file only parses arguments and builds the dependencies.

  export  <db>              verified export under the freeze, R2 copy, receipt
  recheck <db> --receipt R  under the freeze: D1 unchanged since R? (else a fresh export)

Exit codes: 0 done, 2 refused (nothing changed), 3 halted (see the message).
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
        sp.add_argument("--health-token-file", help="0600 file with MEMORA_HEALTH_TOKEN (default: the admin token)")
        sp.add_argument("--service-stopped", action="store_true",
                        help="the barrier is memora-all being stopped (docker), not the freeze")
        sp.add_argument("--container", default="memora-all")
        r2 = sp.add_mutually_exclusive_group(required=True)
        r2.add_argument("--r2-bucket", help="R2 bucket (S3 API)")
        r2.add_argument("--r2-dir", help="a directory standing in for R2 (rehearsals)")
        sp.add_argument("--out-dir", default="/data/exports")
        sp.add_argument("--native-export", action="store_true",
                        help="try `wrangler d1 export --remote` first (read token); the paged SELECT is the fallback")

    common(sub.add_parser("export", help="P1 verified export with receipt"))
    rc = sub.add_parser("recheck", help="P1 freeze-recheck of a receipt")
    common(rc)
    rc.add_argument("--receipt", required=True)
    return p


def _deps(args) -> lp.Deps:
    from memora.backends import D1SelectOnlyConnection

    token = lp.read_token(args.read_token_file)
    reader = lp.D1Reader(D1SelectOnlyConnection(args.account, args.database_id, token))
    if args.service_stopped:
        barrier = lp.ServiceStopped(args.container)
    else:
        if not args.admin_token_file:
            raise lp.L5Refused("--admin-token-file is required to place the freeze (or pass --service-stopped)")
        admin = lp.load_credential_file(args.admin_token_file)
        health = lp.load_credential_file(args.health_token_file) if args.health_token_file else None
        barrier = lp.FreezeClient(args.memora_url, admin, args.db, health_token=health)
    r2 = lp.FsR2(Path(args.r2_dir)) if args.r2_dir else lp.S3R2(args.r2_bucket)
    return lp.Deps(reader=reader, freeze=barrier, r2=r2, account_id=args.account,
                   database_id=args.database_id, d1_name=args.d1_name, read_token=token,
                   native_export=args.native_export)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        deps = _deps(args)
        if args.cmd == "export":
            receipt = lp.export(args.db, deps, Path(args.out_dir))
            out = {"ok": True, "receipt": str(receipt)}
        else:
            receipt = lp.recheck(args.db, args.receipt, deps, Path(args.out_dir))
            out = {"ok": True, "receipt": str(receipt), "fresh_export": str(receipt) != args.receipt}
    except lp.L5Halt as exc:
        print(json.dumps({"ok": False, "halted": str(exc)}))
        return 3
    except lp.L5Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
