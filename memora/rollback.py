"""The §5.3 rollback runbook (H6) and `restamp` (write path 4), L6.

docs/local-primary-implementation.md §5.3, §8 L6. Run by hand, in three
phases, so every deployment action stays the operator's (stop, repoint,
start) and every step boundary is checked:

- `drain`  (memora-all live): place the freeze (or keep the operator's),
  wait until the replicator -- which drains through its gate-exempt writer
  under the freeze -- has acked the whole outbox. Then the operator stops
  memora-all.
- `verify` (memora-all STOPPED, re-checked at every boundary): a verified
  export and receipt of the post-drain D1 (P1); a barrier compare against
  the local file (any diff stops the rollback -- nothing on D1 is changed
  or deleted, P7: keys present only on D1 are reported for a human); the
  D1 sequence high-water (§4, a HALT stops); a read-only integrity audit of
  D1 that must equal the local store's; `recheck` against the receipt (only
  the sequence counters may have moved). Then it prints the repoint: the
  registry entry back to d1://, the store out of MEMORA_REPLICAS.
- `finish` (after the repoint and the start): the store must be served from
  D1 (its intent journal present, no replicator) and still frozen with
  nothing in flight; then the freeze is lifted.

D1 writes: only L5's sequence UPDATE (verify). `restamp` is separate and
never automatic: after the rollback, under a freeze and a fresh receipt, it
audits D1 read-only and writes exactly one memories_meta row,
`embedding_integrity`, through an allow-listed operator writer.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import compare as cmp
from . import local_primary as lp

# the audit fields that must agree between D1 and the local store
AUDIT_FIELDS = ("reps", "mixed", "unknown_encoding_ids", "recurring_unknown_ids", "missing_ids", "orphan_ids",
                "memory_count", "embedding_count", "missing_count", "orphan_embedding_count")


@dataclass
class RollbackDeps:
    db: str
    store: Path
    account_id: str
    database_id: str
    reader: lp.D1Reader
    admin: Any                      # lp.AdminClient: memora-all's freeze/health/admin routes
    stopped: Any                    # lp.ServiceStopped: memora-all must be down (verify)
    r2: Any
    out_dir: Path
    d1_audit_conn: Callable[[], Any]  # a read-only D1Connection (read token)
    writer_factory: Optional[Callable[[], Any]] = None  # the operator writer (sequence UPDATE)
    container: str = "memora-all"
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    poll_s: float = 5.0
    drain_timeout_s: float = 600.0

    def lp_deps(self, barrier) -> lp.Deps:
        return lp.Deps(reader=self.reader, freeze=barrier, r2=self.r2, account_id=self.account_id,
                       database_id=self.database_id, writer_factory=self.writer_factory, clock=self.clock)


# ------------------------------------------------------------------ state

def _state_path(deps: RollbackDeps) -> Path:
    return Path(deps.out_dir) / deps.db / "rollback-state.json"


def _identity(deps: RollbackDeps) -> Dict[str, str]:
    return {"db": deps.db, "store": str(Path(deps.store).resolve()), "account_id": deps.account_id,
            "database_id": deps.database_id}


def load_state(deps: RollbackDeps, *, need: Optional[str] = None) -> Dict[str, Any]:
    try:
        st = json.loads(_state_path(deps).read_text())
    except (OSError, ValueError):
        st = {}
    if need is not None:
        if st.get("identity") != _identity(deps):
            raise lp.L5Refused(f"no rollback in progress for {_identity(deps)} ({_state_path(deps)})")
        ph = st.get("phases", {}).get(need)
        if not ph or ph.get("running"):
            raise lp.L5Refused(f"the rollback phase {need!r} has not completed ({_state_path(deps)})")
    return st


PHASES = ("drain", "verify", "finish")


def _run_phase(deps: RollbackDeps, name: str, body) -> Dict[str, Any]:
    """Run one phase with a fresh generation id (review 7712 P1-3):
    starting it clears every later phase; it is marked running while it
    runs; a failure clears it; verify records the drain generation it
    followed, and finish requires that pairing."""
    import uuid

    if name == "drain":
        st = {"identity": _identity(deps), "phases": {}}
    else:
        st = load_state(deps, need=PHASES[PHASES.index(name) - 1])
    phases = st.setdefault("phases", {})
    for later in PHASES[PHASES.index(name):]:
        phases.pop(later, None)
    if name != "finish":
        phases.pop("verify_failed", None)
    if name == "finish" and phases["verify"].get("drain_gen") != phases["drain"].get("gen"):
        raise lp.L5Refused("the completed verify does not follow the current drain: re-run the verify phase")
    rec: Dict[str, Any] = {"gen": uuid.uuid4().hex, "running": True, "started_at": _now(deps)}
    if name == "verify":
        rec["drain_gen"] = phases["drain"]["gen"]
    phases[name] = rec
    _save_state(deps, st)
    try:
        result, record = body(deps, st)
    except BaseException:
        failed = load_state(deps)  # the body may have added verify_failed
        failed.setdefault("phases", {}).pop(name, None)
        _save_state(deps, failed)
        raise
    st = load_state(deps)
    st["phases"][name] = {**rec, **record, "running": False, "at": _now(deps)}
    _save_state(deps, st)
    return result


def _save_state(deps: RollbackDeps, st: Dict[str, Any]) -> None:
    path = _state_path(deps)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True, default=str))
    os.replace(tmp, path)


def _now(deps: RollbackDeps) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(deps.clock()))


# ------------------------------------------------------------------ phase 1: drain (live)

def phase_drain(deps: RollbackDeps) -> Dict[str, Any]:
    """§5.3 steps 1-2: freeze ingress (the replicator keeps draining through
    connect_replicator), wait for lag_rows = 0."""
    return _run_phase(deps, "drain", _drain)


def _drain(deps: RollbackDeps, st: Dict[str, Any]):
    deps.admin.freeze()  # places it, or keeps the operator's; checked
    deadline = deps.clock() + deps.drain_timeout_s
    while True:
        deps.admin.check("while draining")
        acked, head = cmp.live_acked(deps.store), cmp.live_head(deps.store)
        if acked >= head:
            break
        if deps.clock() >= deadline:
            raise lp.L5Refused(f"not drained after {deps.drain_timeout_s:.0f} s (acked {acked}, head {head}); "
                               "the freeze stays -- see the replicator's health (halted?)")
        deps.sleep(deps.poll_s)
    deps.admin.check("after the drain")
    return ({"phase": "drain", "drained_head": head,
             "next": f"stop memora-all (docker stop {deps.container}), then run: rollback {deps.db} --phase verify"},
            {"head": head})


# ------------------------------------------------------------------ phase 2: verify (stopped)

def _needed_deletions(report: Dict[str, Any]) -> Dict[str, Any]:
    return {t: e["only_d1"] for t, e in report["tables"].items() if e["only_d1"]}


def _audit_local(deps: RollbackDeps, work: Path) -> Dict[str, Any]:
    from .embeddings import verify_embedding_integrity

    work.mkdir(parents=True, exist_ok=True)
    copy = lp.backup_store(deps.store, work / "audit-copy.db")
    conn = lp._scratch_connect(copy)
    try:
        return verify_embedding_integrity(conn, stamp=False)
    finally:
        conn.close()


def _audit_d1(deps: RollbackDeps) -> Dict[str, Any]:
    from .embeddings import verify_embedding_integrity

    return verify_embedding_integrity(deps.d1_audit_conn(), stamp=False)


def phase_verify(deps: RollbackDeps) -> Dict[str, Any]:
    """§5.3 steps 3-7 with memora-all stopped (docker State.Running=false at
    every boundary). Any failure stops the rollback before the repoint."""
    return _run_phase(deps, "verify", _verify)


def _verify(deps: RollbackDeps, st: Dict[str, Any]):
    stopped = deps.stopped
    stopped.check("before the rollback verify")
    acked, head = cmp.live_acked(deps.store), cmp.live_head(deps.store)
    if acked < head:
        raise lp.L5Refused(f"the store is not drained (acked {acked}, head {head}): restart memora-all under "
                           "the freeze and re-run the drain phase")
    ld = deps.lp_deps(stopped)
    receipt = lp.export(deps.db, ld, deps.out_dir)  # step 3
    env = cmp.Env(store=deps.store, reader=deps.reader, work=Path(deps.out_dir) / "work", barrier=stopped,
                  sleep=deps.sleep, clock=deps.clock, poll_s=deps.poll_s)
    report, path, sha, _rec = cmp.run_compare(  # step 4: begin, compare, report, verified record
        env, cmp.DirectRecorder(deps.store, deps.db), lambda: {**cmp.barrier_compare(env, drain_timeout_s=0),
                                                               "rollback": True},
        db=deps.db, account_id=deps.account_id, database_id=deps.database_id, out_dir=deps.out_dir)
    phase: Dict[str, Any] = {"at": _now(deps), "receipt": str(receipt), "compare_report": str(path),
                             "compare_clean": report["clean"]}
    if not report["clean"]:
        st["phases"]["verify_failed"] = phase
        _save_state(deps, st)
        raise lp.L5Halt(f"the rollback compare is not clean ({report['diff_count']} diffs; report {path}). "
                        f"Nothing on D1 was changed or deleted (P7). Keys present only on D1 -- deletions a human "
                        f"must decide: {_needed_deletions(report)}")
    seq = lp.sequence_highwater(deps.db, str(receipt), deps.store, ld, deps.out_dir)  # step 5 (HALT propagates)
    phase["sequence"] = {"statements": seq["statements"], "d1_after": seq.get("d1_after")}
    stopped.check("before the integrity audit")
    d1_audit, local_audit = _audit_d1(deps), _audit_local(deps, Path(deps.out_dir) / "work")  # step 6
    differ = sorted(k for k in AUDIT_FIELDS if d1_audit.get(k) != local_audit.get(k))
    phase["integrity"] = {"d1": {k: d1_audit.get(k) for k in AUDIT_FIELDS}, "differs": differ}
    if differ:
        st["phases"]["verify_failed"] = phase
        _save_state(deps, st)
        raise lp.L5Halt(f"D1's embedding integrity audit differs from the local store's in {differ}")
    final = lp.recheck(deps.db, str(receipt), ld, deps.out_dir)  # step 7
    if Path(final) != Path(receipt):
        a = lp.load_receipt(str(receipt), deps.db, account_id=deps.account_id, database_id=deps.database_id,
                            max_age_s=None)["tables"]
        b = lp.load_receipt(str(final), deps.db, account_id=deps.account_id, database_id=deps.database_id)["tables"]
        moved = sorted(t for t in set(a) | set(b) if a.get(t) != b.get(t))
        if moved != [lp.SEQUENCE_TABLE] or not phase["sequence"]["statements"]:
            st["phases"]["verify_failed"] = phase
            _save_state(deps, st)
            raise lp.L5Halt(f"D1 changed during the rollback beyond the sequence step: {moved}")
    phase["final_receipt"] = str(final)
    uri = lp.d1_uri(deps.account_id, deps.database_id)
    return ({"phase": "verify", "receipt": str(final), "compare_report": str(path), **phase,
             "repoint": {"MEMORA_DATABASES": {deps.db: uri}, "MEMORA_REPLICAS": f"remove {deps.db!r}"},
             "next": f"set MEMORA_DATABASES[{deps.db!r}] = {uri!r}, remove {deps.db!r} from MEMORA_REPLICAS, "
                     f"start memora-all, then run: rollback {deps.db} --phase finish"},
            phase)


# ------------------------------------------------------------------ phase 3: finish (repointed, live)

def phase_finish(deps: RollbackDeps) -> Dict[str, Any]:
    """§5.3 step 9, under the freeze still in place (review 7712):
    - the store is ready (/health/db 200 ok) and served from THE D1 database
      this rollback verified: /admin/data-volume (admin token) reports its
      live backend identity, which must be d1://<account>/<database>;
    - D1 has not drifted since the verify: `recheck` against its final
      receipt (full per-table hashes, read token); any change HALTS;
    then the freeze is lifted (the only automatic thaw here)."""
    return _run_phase(deps, "finish", _finish)


def _lock_check(deps: RollbackDeps, where: str) -> None:
    """--lock-barrier (X3): during finish memora-all is up but serving D1, so
    it must not hold the LOCAL store's primary lock; this run holds it."""
    if isinstance(deps.stopped, lp.LockBarrier):
        deps.stopped.check(where)


def _finish(deps: RollbackDeps, st: Dict[str, Any]):
    _lock_check(deps, "before the finish")
    deps.admin.check("before the finish")
    status, body = deps.admin._request("GET", f"/health/db/{deps.db}")
    if status != 200 or body.get("status") != "ok":
        raise lp.L5Refused(f"{deps.db} is not ready (/health/db answered {status}, status {body.get('status')!r})")
    if "journal" not in body or "replication" in body:
        raise lp.L5Refused(f"{deps.db} is not served from D1 yet (health shows "
                           f"{sorted(k for k in body if k in ('journal', 'replication'))}): repoint and restart first")
    status, dv = deps.admin._request("GET", "/admin/data-volume")
    entry = (dv.get("stores") or {}).get(deps.db) if status == 200 else None
    want = lp.d1_uri(deps.account_id, deps.database_id)
    live = ((entry or {}).get("identity") or {}).get("d1_uri")
    if not entry or entry.get("kind") != "d1" or live != want or entry.get("refused"):
        raise lp.L5Refused(f"{deps.db} is served from {live or (entry or {}).get('kind') or 'unknown'} "
                           f"(/admin/data-volume {status}), not the verified {want}; the freeze stays")
    final = st["phases"]["verify"]["final_receipt"]
    _lock_check(deps, "before the D1 recheck")
    fresh = lp.recheck(deps.db, final, deps.lp_deps(deps.admin), deps.out_dir)
    if Path(fresh) != Path(final):
        a = lp.load_receipt(final, deps.db, account_id=deps.account_id, database_id=deps.database_id,
                            max_age_s=None)["tables"]
        b = lp.load_receipt(str(fresh), deps.db, account_id=deps.account_id,
                            database_id=deps.database_id)["tables"]
        moved = sorted(t for t in set(a) | set(b) if a.get(t) != b.get(t))
        raise lp.L5Halt(f"D1 changed since the rollback verify in {moved}; the freeze stays -- find the "
                        "writer, then re-run the rollback from the drain phase")
    _lock_check(deps, "before lifting the freeze")
    deps.admin.check("before lifting the freeze")
    deps.admin.thaw()
    return ({"phase": "finish", "thawed": True, "served_from": live,
             "next": f"optional, operator-run: restamp {deps.db} --receipt <a fresh receipt> (under a freeze)"},
            {"served_from": live})


# ------------------------------------------------------------------ restamp (write path 4)

def restamp(deps: RollbackDeps, receipt_path: str) -> Dict[str, Any]:
    """After the rollback compare and the repoint: under the freeze and a
    fresh receipt (rechecked), audit D1 read-only and write exactly one
    memories_meta row, embedding_integrity, with the value
    verify_embedding_integrity(stamp=True) would write. Read back."""
    from .embeddings import integrity_stamp, integrity_stamp_value, verify_embedding_integrity

    load_state(deps, need="verify")
    ld = deps.lp_deps(deps.admin)
    used = lp.recheck(deps.db, receipt_path, ld, deps.out_dir)  # the freeze required; fresh receipt
    if deps.writer_factory is None:
        raise lp.L5Refused("restamp needs the operator writer: pass --credential-file")
    deps.admin.check("before the audit")
    result = verify_embedding_integrity(deps.d1_audit_conn(), stamp=False)
    audit = {k: v for k, v in result.items() if k != "fingerprint"}
    stamped = integrity_stamp(audit, result.get("fingerprint"))
    value = integrity_stamp_value(stamped)
    deps.admin.check("before the restamp write")
    writer = deps.writer_factory()
    try:
        res = writer.send(lp.RESTAMP_SQL, (lp.RESTAMP_KEY, value))
    except lp.L5Refused:
        raise
    except Exception as exc:
        raise lp.L5Halt(f"D1 rejected the restamp: {type(exc).__name__}: {exc}")
    if isinstance(res, dict) and res.get("success") is False:
        raise lp.L5Halt(f"D1 rejected the restamp: {res}")
    back = deps.reader.rows("SELECT value FROM memories_meta WHERE key = ?", (lp.RESTAMP_KEY,))
    if not back or back[0].get("value") != value:
        raise lp.L5Halt("D1 does not read back the restamp")
    return {"restamped": True, "receipt": str(used), "generation": stamped["generation"],
            "fingerprint": stamped["fingerprint"], "audit": {k: audit.get(k) for k in AUDIT_FIELDS}}


def d1_audit_connection(account_id: str, database_id: str, read_token: str):
    """A D1Connection for the read-only audit: the READ token, and marked
    read-only so any mutation is refused before it is sent."""
    from .backends import D1Connection

    conn = D1Connection(account_id, database_id, read_token)
    conn.read_only_reason = "operator integrity audit (read token): never writes"
    return conn
