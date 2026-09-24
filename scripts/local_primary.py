#!/usr/bin/env python3
"""Local-primary operator tool (docs/local-primary-implementation.md §0 P1, §4).

Run by hand, never by the service. The logic lives in memora/local_primary.py;
this file only parses arguments and builds the dependencies.

  freeze  <db>              place memora-all's freeze on the store (POST /admin/freeze)
  export  <db>              verified export under the freeze, R2 copy, receipt
  recheck <db> --receipt R  under the SAME freeze: D1 unchanged since R? (else a fresh export)
  seed    <db> --receipt R --out /data/<db>.db   (rechecks R under the same freeze first)
  sequence-highwater <db> --receipt R --local /data/<db>.db --credential-file F [--dry-run]
  restore <db> --receipt R --out /data/<db>.db              default: FULL re-seed (old store kept aside)
  restore <db> --from-r2 KEY --receipt R                    prepare: write conflicts-<ts>.json
  restore <db> --from-r2 KEY --receipt R --conflicts F --approve A --out P --credential-file C
          --service-stopped [--dry-run]      (apply: memora-all stopped; --dry-run writes nothing)
  reconcile <db> [--accept ID --receipt R --operator NAME --decision applied|not-applied --evidence-sha256 X]
  resume  <db> --store /data/<db>.db [--accept-d1-epoch N | --allow-deletes ATTEMPT]   (memora-all stopped)
  compare <db> --mode barrier|nightly|log --store P   §5.2 compare; report; record (exit 5 diff, 6 skipped)
  thaw    <db>              lift the freeze -- the only command that does
  snapshot <db> --store /data/<db>.db          nightly: backup, gzip, R2, keep 14
  volume-check --store /data/<db>.db ...      alert (exit 4) when free space is low
  check-endpoint            F4a: authenticated round trip to memora-all through a
                            SCRATCH local store, before a client is repointed (L8)

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
    ce = sub.add_parser("check-endpoint", help="F4a: prove the memora-all endpoint before repointing a client")
    ce.add_argument("--memora-url", required=True, help="memora-all's base URL, e.g. http://nuc8:8920")
    ce.add_argument("--health-token-file", required=True, help="0600 file with MEMORA_HEALTH_TOKEN")
    ce.add_argument("--admin-token-file", required=True, help="0600 file with MEMORA_ADMIN_TOKEN")
    ce.add_argument("--store", default="scratch", help="a LOCAL SQLite store in the registry (default: scratch)")
    ce.add_argument("--no-write", action="store_true", help="skip the create/get/delete of a throwaway memory")
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
    rs = sub.add_parser("restore", help="§4 restore: full re-seed, or --from-r2 with conflict groups")
    common(rs)
    rs.add_argument("--receipt", required=True)
    rs.add_argument("--out", help="the store file (default restore, and --from-r2 apply)")
    rs.add_argument("--replica-uri", help="optional; must equal d1://<account>/<database-id>")
    rs.add_argument("--rehearse", action="store_true")
    rs.add_argument("--from-r2", help="snapshot key in R2 (e.g. <db>/<ts>.db.gz)")
    rs.add_argument("--conflicts", help="the conflicts file written by the prepare step")
    rs.add_argument("--approve", help="the operator's selections: {conflicts_sha256, selections: {group: d1|snapshot}}")
    rs.add_argument("--credential-file", help="0600 file with the operator's D1 edit token (snapshot selections)")
    rs.add_argument("--dry-run", action="store_true", help="print the exact statements, write nothing")
    rs.add_argument("--allow-deletes", help="the attempt id a delete-guard refusal names (that attempt only)")
    rc2 = sub.add_parser("reconcile", help="§1 open D1 write intents: show, or --accept one")
    rc2.add_argument("db")
    rc2.add_argument("--account", required=True)
    rc2.add_argument("--database-id", required=True)
    rc2.add_argument("--memora-url", default="http://127.0.0.1:8000")
    rc2.add_argument("--admin-token-file", required=True)
    rc2.add_argument("--health-token-file", required=True)
    rc2.add_argument("--accept", type=int, metavar="INTENT_ID")
    rc2.add_argument("--receipt")
    rc2.add_argument("--operator")
    rc2.add_argument("--decision", choices=lp.RECONCILE_DECISIONS)
    rc2.add_argument("--evidence-sha256")
    rm = sub.add_parser("resume", help="§2.6/P3 clear a replicator halt (memora-all stopped)")
    rm.add_argument("db")
    rm.add_argument("--store", required=True)
    rm.add_argument("--account", required=True)
    rm.add_argument("--database-id", required=True)
    rm.add_argument("--read-token-file")
    g = rm.add_mutually_exclusive_group()
    g.add_argument("--accept-d1-epoch", type=int)
    g.add_argument("--allow-deletes")
    cp = sub.add_parser("compare", help="§5.2 compare a local store with D1 (or its log, in log mode)")
    cp.add_argument("db")
    cp.add_argument("--mode", required=True, choices=("barrier", "nightly", "log"))
    cp.add_argument("--store", required=True, help="the live local store file")
    cp.add_argument("--account", required=True)
    cp.add_argument("--database-id", required=True)
    cp.add_argument("--read-token-file")
    cp.add_argument("--memora-url", default="http://127.0.0.1:8000")
    cp.add_argument("--admin-token-file", help="0600; the freeze (barrier) and recording through memora-all")
    cp.add_argument("--health-token-file", help="0600; required with --admin-token-file")
    cp.add_argument("--service-stopped", action="store_true",
                    help="memora-all is stopped: the barrier is docker State.Running=false, record directly")
    cp.add_argument("--container", default="memora-all")
    cp.add_argument("--out-dir", default="/data/compare", help="reports and the nightly state")
    cp.add_argument("--work-dir", help="snapshots (default: <out-dir>/work)")
    cp.add_argument("--receipt", help="log mode: the store's seed export receipt")
    cp.add_argument("--log-dir", help="log mode: the replicator log directory (default: its data-volume path)")
    cp.add_argument("--wait-timeout", type=float, default=1800.0, help="nightly: wait for the acks (s)")
    cp.add_argument("--drain-timeout", type=float, default=600.0, help="barrier/log: wait for the drain (s)")
    cp.add_argument("--no-record", action="store_true", help="write the report only")
    cp.add_argument("--brief-freeze", action="store_true",
                    help="barrier (the weekly job): place the freeze if none is in place and lift only that one")
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
            args.account, args.database_id, args.credential_file, allow_restore=args.cmd == "restore")
    return lp.Deps(reader=reader, freeze=barrier, r2=_r2(args), account_id=args.account,
                   database_id=args.database_id, d1_name=args.d1_name, read_token=token,
                   native_export=args.native_export, writer_factory=writer_factory)


def _r2(args):
    return lp.FsR2(Path(args.r2_dir)) if args.r2_dir else lp.S3R2(args.r2_bucket)


FROZEN_STEPS = ("export", "recheck", "seed", "sequence-highwater", "restore")


def _recovery(args) -> dict:
    """A failed step leaves the freeze in place on purpose (review 7621
    P1-2); say how to lift it if the procedure is abandoned (7630 v)."""
    if args.cmd not in FROZEN_STEPS or getattr(args, "service_stopped", False):
        return {}
    return {"freeze": "left in place (a failed step never lifts it)",
            "recovery": f"to abandon the procedure: local_primary.py thaw {args.db} "
                        f"--memora-url {args.memora_url} --admin-token-file <file> --health-token-file <file>"}


def _compare(args) -> int:
    """§5.2: run the compare, write the report, record the outcome (through
    memora-all's admin route, or directly with --service-stopped). Exit 0
    clean, 5 diffs, 6 skipped (nightly: the acks did not reach H)."""
    from memora import compare as cmp
    from memora.backends import D1SelectOnlyConnection
    from memora.replicator import iter_log

    reader = lp.D1Reader(D1SelectOnlyConnection(args.account, args.database_id, lp.read_token(args.read_token_file)))
    admin = None
    if args.admin_token_file:
        if not args.health_token_file:
            raise lp.L5Refused("--health-token-file is required with --admin-token-file")
        admin = lp.AdminClient(args.memora_url, lp.load_credential_file(args.admin_token_file), args.db,
                               health_token=lp.load_credential_file(args.health_token_file))
    barrier = lp.ServiceStopped(args.container) if args.service_stopped else admin
    if not args.no_record and not args.service_stopped and admin is None:
        raise lp.L5Refused("recording the outcome needs memora-all's admin route (--admin-token-file, "
                           "--health-token-file) or --service-stopped; or pass --no-record")
    out_dir = Path(args.out_dir)
    env = cmp.Env(store=Path(args.store), reader=reader, work=Path(args.work_dir or out_dir / "work"),
                  barrier=barrier)
    if args.mode == "barrier":
        if barrier is None:
            raise lp.L5Refused("a barrier compare needs the freeze (--admin-token-file ...) or --service-stopped")
        placed = False
        if args.brief_freeze:
            if admin is None:
                raise lp.L5Refused("--brief-freeze needs memora-all's admin route")
            status, body = admin._request("GET", f"/health/db/{args.db}")
            if (body.get("freeze") or {}).get("state") not in ("frozen", "frozen-unsafe"):
                admin.freeze()  # the weekly job's own brief freeze
                placed = True
        try:
            report = cmp.barrier_compare(env, drain_timeout_s=args.drain_timeout)
        finally:
            if placed:
                admin.thaw()  # only the freeze this run placed; an operator's stays
        report["brief_freeze_placed"] = placed
    elif args.mode == "nightly":
        report = cmp.nightly_compare(env, state_path=out_dir / args.db / "nightly-state.json",
                                     wait_timeout_s=args.wait_timeout)
    else:
        if not args.receipt:
            raise lp.L5Refused("log mode needs --receipt (the store's seed export)")
        log_dir = Path(args.log_dir) if args.log_dir else None
        report = cmp.log_compare(env, db=args.db, receipt_path=args.receipt, account_id=args.account,
                                 database_id=args.database_id, log_records=lambda: iter_log(args.db, log_dir),
                                 drain_timeout_s=args.drain_timeout)
    report.update({"db": args.db, "store": args.store})
    path, sha = cmp.write_report(report, out_dir, args.db)
    recorded = None
    if not args.no_record and not report.get("skipped"):
        fields = cmp.record_fields(report, sha)
        recorded = cmp.record_direct(Path(args.store), fields) if args.service_stopped else admin.post_compare(fields)
    summary = {"ok": bool(report["clean"]), "mode": args.mode, "report": str(path), "report_sha256": sha,
               "clean": report["clean"], "diff_count": report.get("diff_count"),
               "d1_missing_vectors": report.get("d1_missing_vectors"), "consumed_seq": report.get("consumed_seq"),
               "hot_keys": len(report.get("hot_keys") or []), "skipped": report.get("skipped"), "recorded": recorded}
    print(json.dumps(summary))
    return 6 if report.get("skipped") else (0 if report["clean"] else 5)


def _restore(args, deps) -> dict:
    if not args.from_r2:
        if not args.out:
            raise lp.L5Refused("restore needs --out (the store file)")
        return lp.restore(args.db, args.receipt, Path(args.out), deps, Path(args.out_dir),
                          replica_uri=args.replica_uri, rehearse=args.rehearse)
    if args.rehearse:
        raise lp.L5Refused("--rehearse does not apply to restore --from-r2: its apply writes D1; use --dry-run "
                           "for the complete no-write plan")
    if not args.conflicts and not args.approve:
        return lp.restore_prepare(args.db, args.from_r2, args.receipt, deps, Path(args.out_dir))
    if not (args.conflicts and args.approve and args.out):
        raise lp.L5Refused("the apply step needs --conflicts, --approve and --out")
    conflicts = json.loads(Path(args.conflicts).read_text())
    if conflicts.get("snapshot_key") != args.from_r2:
        raise lp.L5Refused(f"{args.conflicts} was prepared for {conflicts.get('snapshot_key')!r}, not {args.from_r2!r}")
    return lp.restore_apply(args.db, args.conflicts, args.approve, args.receipt, deps, Path(args.out),
                            Path(args.out_dir), replica_uri=args.replica_uri, dry_run=args.dry_run,
                            allow_deletes=args.allow_deletes)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "check-endpoint":
            from memora.endpoint_check import EndpointCheckFailed, check_endpoint

            health = lp.load_credential_file(args.health_token_file)
            admin = lp.load_credential_file(args.admin_token_file)
            try:
                report = check_endpoint(args.memora_url, health, admin, args.store, write=not args.no_write)
            except EndpointCheckFailed as exc:
                print(json.dumps({"ok": False, "refused": str(exc), "step": exc.step}))
                return 2
            print(json.dumps(report))
            return 0
        if args.cmd in ("freeze", "thaw"):
            client = _freeze_client(args)
            client.freeze() if args.cmd == "freeze" else client.thaw()
            print(json.dumps({"ok": True, args.cmd: args.db}))
            return 0
        if args.cmd == "reconcile":
            client = lp.AdminClient(args.memora_url, lp.load_credential_file(args.admin_token_file), args.db,
                                    health_token=lp.load_credential_file(args.health_token_file))
            if args.accept is None:
                out = {"ok": True, **client.intents()}
            else:
                missing = [f for f in ("receipt", "operator", "decision", "evidence_sha256") if not getattr(args, f)]
                if missing:
                    raise lp.L5Refused(f"--accept needs {', '.join('--' + m.replace('_', '-') for m in missing)}")
                out = {"ok": True, **lp.reconcile_accept(
                    args.db, client, intent_id=args.accept, receipt_path=args.receipt, operator=args.operator,
                    decision=args.decision, evidence_sha256=args.evidence_sha256,
                    account_id=args.account, database_id=args.database_id)}
            print(json.dumps(out))
            return 0
        if args.cmd == "compare":
            return _compare(args)
        if args.cmd == "resume":
            from memora.backends import D1SelectOnlyConnection

            reader = lp.D1Reader(D1SelectOnlyConnection(args.account, args.database_id,
                                                        lp.read_token(args.read_token_file)))
            out = {"ok": True, **lp.resume_store(Path(args.store), reader, accept_d1_epoch=args.accept_d1_epoch,
                                                 allow_deletes=args.allow_deletes)}
            print(json.dumps(out))
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
        elif args.cmd == "restore":
            out = {"ok": True, **_restore(args, deps)}
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
