"""The §5.2 compare (H4, L6): a local store against D1, and in log mode the
replicator's log against the store (§2.9 (b)).

docs/local-primary-implementation.md §5.2, §2.7, §2.9. Every replicated
table, every column, keys present on one side only, the embedding
provenance fields, memories_meta minus the §1 exclusions. Local rows always
come from ONE consistent `.backup` snapshot `S` of the store. D1 is read
with the SELECT-only reader (the read token) and never written.

Modes (run by `scripts/local_primary.py compare`):
- barrier: under the freeze (or with memora-all stopped), drained, zero
  diffs required;
- nightly: S, its head H, wait for the acks to pass H, read D1, exclude the
  keys K written after S, one retry that RETAKES S, H and K; keys excluded
  on two consecutive nights are reported (the weekly barrier covers them);
- log: the log's key set must match the outbox's up to log_cursor_seq, then
  the log is replayed into the store's seed export and compared with S.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .local_primary import (D1Reader, L5Refused, _norm, _order_for, _scratch_connect, backup_store,
                            load_receipt, load_sql)
from .replicator import added_parent_columns, is_added_parent

PROVENANCE = ("representation", "dimension", "encoding_source", "writer_token")
EMBEDDINGS = "memories_embeddings"
Key = Tuple[str, Tuple[Any, ...]]


def _tables() -> Dict[str, Tuple[str, ...]]:
    from .schema import SYNC_TABLES

    return dict(SYNC_TABLES)


def _excluded_meta() -> Tuple[str, ...]:
    from .schema import SYNC_META_EXCLUDED

    return tuple(SYNC_META_EXCLUDED)


class CompareRefused(L5Refused):
    """A precondition failed (not drained, not frozen, ...): nothing compared."""


# ------------------------------------------------------------------ rows

@dataclass
class Side:
    """One side's rows: {table: {pk tuple: {column: normalised value}}} and
    its column lists."""
    rows: Dict[str, Dict[Tuple, Dict[str, Any]]] = field(default_factory=dict)
    columns: Dict[str, List[str]] = field(default_factory=dict)


def _keep(table: str, row: Dict[str, Any]) -> bool:
    return not (table == "memories_meta" and row.get("key") in _excluded_meta())


def file_side(path: Path) -> Side:
    """A SQLite file's replicated tables (a snapshot, a scratch replay)."""
    side = Side()
    db = _scratch_connect(Path(path))
    db.row_factory = sqlite3.Row
    try:
        for t, pk in _tables().items():
            info = [(r[1], int(r[5] or 0)) for r in db.execute(f'PRAGMA table_info("{t}")')]
            side.columns[t] = [c for c, _ in info]
            side.rows[t] = {}
            if not info:
                continue
            for r in db.execute(f'SELECT * FROM "{t}" ORDER BY {_order_for(t, info)}'):
                row = dict(r)
                if _keep(t, row):
                    side.rows[t][tuple(row[c] for c in pk)] = {c: _norm(v) for c, v in row.items()}
    finally:
        db.close()
    return side


def d1_side(reader: D1Reader) -> Side:
    """D1's replicated tables, through the SELECT-only reader."""
    side = Side()
    present = {n for n, _ in reader.tables()}
    for t, pk in _tables().items():
        side.rows[t] = {}
        if t not in present:
            side.columns[t] = []
            continue
        side.columns[t] = [c for c, _ in reader.columns(t)]
        for row in reader.all_rows(t):
            if _keep(t, row):
                side.rows[t][tuple(row[c] for c in pk)] = {c: _norm(v) for c, v in row.items()}
    return side


# ------------------------------------------------------------------ the compare

def _pk_json(pk: Tuple) -> List[Any]:
    return [_norm(v) for v in pk]


def compare_sides(local: Side, other: Side, *, exclude: Iterable[Key] = ()) -> Dict[str, Any]:
    """Every table, every column, both key sets. `exclude` removes keys
    written after the snapshot (nightly K). Returns the report body."""
    skip: Set[Key] = {(t, tuple(pk)) for t, pk in exclude}
    tables: Dict[str, Any] = {}
    diffs = missing_vectors = 0
    for t in _tables():
        lcols, ocols = local.columns.get(t, []), other.columns.get(t, [])
        lrows, orows = local.rows.get(t, {}), other.rows.get(t, {})
        entry: Dict[str, Any] = {
            "local": len(lrows), "d1": len(orows),
            "columns_only_local": sorted(set(lcols) - set(ocols)),
            "columns_only_d1": sorted(set(ocols) - set(lcols)),
            "only_local": [], "only_d1": [], "changed": [], "provenance_mismatch": [], "excluded": 0,
        }
        diffs += len(entry["columns_only_local"]) + len(entry["columns_only_d1"])
        for pk in sorted(set(lrows) | set(orows), key=lambda k: [str(v) for v in k]):
            if (t, pk) in skip:
                entry["excluded"] += 1
                continue
            lrow, orow = lrows.get(pk), orows.get(pk)
            if orow is None:
                entry["only_local"].append(_pk_json(pk))
                if t == EMBEDDINGS and lrow.get("embedding") is not None:
                    missing_vectors += 1
                continue
            if lrow is None:
                entry["only_d1"].append(_pk_json(pk))
                continue
            changed = sorted(c for c in set(lrow) | set(orow) if lrow.get(c) != orow.get(c))
            if not changed:
                continue
            entry["changed"].append({"pk": _pk_json(pk), "columns": changed})
            if t == EMBEDDINGS:
                if set(changed) <= set(PROVENANCE):
                    entry["provenance_mismatch"].append({"pk": _pk_json(pk), "columns": changed})
                if lrow.get("embedding") is not None and orow.get("embedding") is None:
                    missing_vectors += 1
        diffs += len(entry["only_local"]) + len(entry["only_d1"]) + len(entry["changed"])
        tables[t] = entry
    return {"clean": diffs == 0, "diff_count": diffs, "d1_missing_vectors": missing_vectors, "tables": tables}


# ------------------------------------------------------------------ the store's sync state

def snapshot_state(path: Path) -> Dict[str, Any]:
    """sync_state of a snapshot, plus `head`: the highest outbox seq ever
    assigned (sqlite_sequence survives pruning)."""
    db = _scratch_connect(Path(path))
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        if row is None:
            raise CompareRefused(f"{path}: no sync_state row (replication is not installed)")
        st = dict(row)
        seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'sync_outbox'").fetchone()
        top = db.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_outbox").fetchone()[0]
        st["head"] = max(int(seq[0]) if seq else 0, int(top))
        return st
    finally:
        db.close()


def _live(store: Path, fn: Callable[[sqlite3.Connection], Any]) -> Any:
    """A read of the LIVE store (read-only connection, never creates)."""
    from .backends import LocalSQLiteBackend

    conn = LocalSQLiteBackend(Path(store)).connect_read_only()
    try:
        return fn(conn)
    except sqlite3.OperationalError as exc:
        if "no such table: sync_" in str(exc):  # found by the R1 rehearsal: a plain store, not a primary
            raise CompareRefused(f"{store}: replication is not installed on this store ({exc})")
        raise
    finally:
        conn.close()


def live_acked(store: Path) -> int:
    return int(_live(store, lambda c: c.execute("SELECT last_acked_seq FROM sync_state WHERE id = 1").fetchone()[0]))


def live_head(store: Path) -> int:
    def head(c):
        seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'sync_outbox'").fetchone()
        top = c.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_outbox").fetchone()[0]
        return max(int(seq[0]) if seq else 0, int(top))

    return int(_live(store, head))


def live_keys_after(store: Path, seq: int) -> Set[Key]:
    """K: the live outbox keys with seq > `seq` (written after S)."""
    rows = _live(store, lambda c: c.execute("SELECT tbl, pk FROM sync_outbox WHERE seq > ?", (seq,)).fetchall())
    return {(t, tuple(json.loads(pk))) for t, pk in rows}


def _wait(cond: Callable[[], bool], timeout_s: float, poll_s: float, sleep: Callable[[float], None],
          clock: Callable[[], float]) -> bool:
    deadline = clock() + timeout_s
    while True:
        if cond():
            return True
        if clock() >= deadline:
            return False
        sleep(poll_s)


@dataclass
class Env:
    """What a compare touches, injectable for tests."""
    store: Path
    reader: D1Reader
    work: Path
    barrier: Any = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    poll_s: float = 5.0
    snapshots: List[Path] = field(default_factory=list)  # removed once the run is recorded


def _snapshot(env: Env, tag: str) -> Tuple[Path, Dict[str, Any]]:
    from .local_primary import _sha256_file

    env.work.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"compare-{tag}-", dir=str(env.work))) / "S.db"
    env.snapshots.append(path.parent)
    backup_store(env.store, path)
    st = snapshot_state(path)
    st["snapshot"] = {"snapshot_path": str(path.resolve()), "snapshot_sha256": _sha256_file(path),
                      "snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(env.clock()))}
    return path, st


# ------------------------------------------------------------------ barrier

def barrier_compare(env: Env, *, drain_timeout_s: float = 600.0) -> Dict[str, Any]:
    """§5.2 barrier: the freeze (or the stopped service) is required and
    re-checked at every boundary; drained to lag_rows = 0; one snapshot;
    every key compared; zero diffs required. consumed_seq = H when clean."""
    env.barrier.require("before the barrier compare")
    if not _wait(lambda: live_acked(env.store) >= live_head(env.store), drain_timeout_s, env.poll_s,
                 env.sleep, env.clock):
        raise CompareRefused(f"not drained after {drain_timeout_s:.0f} s (acked {live_acked(env.store)}, "
                             f"head {live_head(env.store)})")
    env.barrier.check("after the drain")
    snap, st = _snapshot(env, "barrier")
    if int(st["last_acked_seq"]) < st["head"]:
        raise CompareRefused(f"the snapshot is not drained (acked {st['last_acked_seq']}, head {st['head']})")
    env.barrier.check("after the snapshot")
    d1 = d1_side(env.reader)
    env.barrier.check("after reading D1")
    body = compare_sides(file_side(snap), d1)
    return {"mode": "barrier", "snapshot_head": st["head"], "consumed_seq": st["head"] if body["clean"] else None,
            **st["snapshot"], **body}


# ------------------------------------------------------------------ nightly

def _nightly_once(env: Env, attempt: int, wait_timeout_s: float) -> Dict[str, Any]:
    snap, st = _snapshot(env, f"nightly{attempt}")
    h = st["head"]
    if not _wait(lambda: live_acked(env.store) >= h, wait_timeout_s, env.poll_s, env.sleep, env.clock):
        return {"skipped": f"the acks did not reach H={h} within {wait_timeout_s:.0f} s", "snapshot_head": h,
                **st["snapshot"]}
    d1 = d1_side(env.reader)
    k = live_keys_after(env.store, h)  # read AFTER D1: every key written after S is in K
    body = compare_sides(file_side(snap), d1, exclude=k)
    return {"snapshot_head": h, "excluded_keys": sorted([t, list(pk)] for t, pk in k), **st["snapshot"], **body}


def _previous_night_keys(state_path: Path, night: str) -> List[List[Any]]:
    """K of the last recorded night BEFORE `night` (a rerun on the same
    night compares with the night before, not with itself)."""
    try:
        st = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return []
    if st.get("night") != night:
        return st.get("keys", [])
    return (st.get("previous") or {}).get("keys", [])


def _save_night(state_path: Path, night: str, keys: List[List[Any]], previous: List[List[Any]]) -> None:
    try:
        st = json.loads(state_path.read_text())
    except (OSError, ValueError):
        st = {}
    prev = st.get("previous") if st.get("night") == night else {"night": st.get("night"), "keys": previous}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"night": night, "keys": keys, "previous": prev}))
    tmp.replace(state_path)


def nightly_compare(env: Env, *, state_path: Path, wait_timeout_s: float = 1800.0,
                    night: Optional[str] = None) -> Dict[str, Any]:
    """§5.2 nightly: S, H, wait for last_acked_seq >= H (skip and alert past
    the timeout), read D1, exclude K; on a diff one retry that RETAKES S, H
    and K. Clean -> consumed_seq = H. Keys in K on two consecutive nights
    are reported (hot keys, left to the weekly barrier compare)."""
    attempts = []
    for attempt in (1, 2):
        run = _nightly_once(env, attempt, wait_timeout_s)
        attempts.append({k: run.get(k) for k in ("snapshot_head", "diff_count", "skipped")})
        if run.get("skipped") or run["clean"]:
            break
    night = night or time.strftime("%Y-%m-%d", time.gmtime(env.clock()))
    this_k = run.get("excluded_keys", [])
    prev_k = _previous_night_keys(Path(state_path), night)
    prev_set = {(t, tuple(pk)) for t, pk in prev_k}
    hot = sorted(([t, pk] for t, pk in this_k if (t, tuple(pk)) in prev_set), key=str)
    if not run.get("skipped"):
        _save_night(Path(state_path), night, this_k, prev_k)
    out = {"mode": "nightly", "night": night, "attempts": attempts,
           "hot_keys": hot, **run}
    out["consumed_seq"] = run["snapshot_head"] if run.get("clean") and not run.get("skipped") else None
    if run.get("skipped"):
        out.update({"clean": False, "diff_count": None, "d1_missing_vectors": None})
    return out


# ------------------------------------------------------------------ log (shadow period, §2.9 (b))

def _outbox_keys(path: Path, upto: int) -> Dict[Key, int]:
    db = _scratch_connect(Path(path))
    try:
        return {(t, tuple(json.loads(pk))): int(seq)
                for seq, t, pk in db.execute("SELECT seq, tbl, pk FROM sync_outbox WHERE seq <= ?", (upto,))}
    finally:
        db.close()


def log_compare(env: Env, *, db: str, receipt_path: str, account_id: str, database_id: str,
                log_records: Callable[[], Iterable[Dict[str, Any]]], drain_timeout_s: float = 600.0
                ) -> Dict[str, Any]:
    """§2.9 (b), the statement builder: (1) the log's key set up to
    log_cursor_seq must cover the outbox's, and any extra log key must be a
    `memories` parent the replicator added for a child; (2) the log replayed
    in order into the store's seed export must equal S (the store at the
    same seq). A diff is a builder bug. D1 is not read."""
    receipt = load_receipt(receipt_path, db, account_id=account_id, database_id=database_id,
                           max_age_s=None)  # the seed export: older than a day by design
    def drained() -> bool:
        return _live(env.store, lambda c: int(c.execute(
            "SELECT log_cursor_seq FROM sync_state WHERE id = 1").fetchone()[0])) >= live_head(env.store)

    if not _wait(drained, drain_timeout_s, env.poll_s, env.sleep, env.clock):
        raise CompareRefused(f"the log cursor did not reach the head within {drain_timeout_s:.0f} s")
    snap, st = _snapshot(env, "log")
    cursor = int(st["log_cursor_seq"])
    if cursor < st["head"]:
        raise CompareRefused(f"the snapshot is ahead of the log (cursor {cursor}, head {st['head']})")
    outbox = _outbox_keys(snap, cursor)
    records = [r for r in log_records() if int(r["seq"]) <= cursor]
    logged: Dict[Key, int] = {}
    for r in records:
        logged.setdefault((r["tbl"], tuple(r["pk"])), int(r["seq"]))
    missing = sorted(set(outbox) - set(logged), key=str)
    # An extra log key is allowed only as the memories parent the replicator
    # added to a child upsert, in its exact statement shape (review 7721 P1).
    sdb = _scratch_connect(Path(snap))
    try:
        parent_cols = added_parent_columns(sdb)  # the real schema, not the record's own column list
    finally:
        sdb.close()
    extra = sorted((k for k in set(logged) - set(outbox) if not is_added_parent(k, records, parent_cols)),
                   key=str)
    scratch = Path(tempfile.mkdtemp(prefix="compare-log-replay-", dir=str(env.work))) / "replay.db"
    load_sql(Path(receipt["sql_path"]), scratch)
    rdb = _scratch_connect(scratch)
    replay_error = None
    try:
        rdb.execute("PRAGMA foreign_keys = ON")  # D1 enforces them
        for r in records:
            try:
                rdb.execute(r["sql"], tuple(r["params"]))
            except sqlite3.Error as exc:
                replay_error = f"seq {r['seq']} index {r['index']}: {exc}"
                break
        rdb.commit()
    finally:
        rdb.close()
    body = compare_sides(file_side(snap), file_side(scratch))
    key_diffs = len(missing) + len(extra)
    clean = body["clean"] and key_diffs == 0 and replay_error is None
    return {"mode": "log", "snapshot_head": st["head"], "log_cursor_seq": cursor, "log_records": len(records),
            **st["snapshot"],
            "keys_missing_from_log": [[t, list(pk)] for t, pk in missing],
            "unexpected_log_keys": [[t, list(pk)] for t, pk in extra], "replay_error": replay_error,
            **body, "clean": clean, "diff_count": body["diff_count"] + key_diffs + (replay_error is not None),
            "consumed_seq": None}


# ------------------------------------------------------------------ report and record

def write_report(report: Dict[str, Any], out_dir: Path, db: str, clock: Callable[[], float] = time.time
                 ) -> Tuple[Path, str]:
    import hashlib

    base = Path(out_dir) / db
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    path, n = base / f"compare-{report['mode']}-{stamp}.json", 1
    while path.exists():
        n += 1
        path = base / f"compare-{report['mode']}-{stamp}-{n}.json"
    raw = json.dumps(report, indent=2, sort_keys=True, default=str).encode("utf-8")
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


class AdminRecorder:
    """memora-all serves the store: begin/record/abort through its admin
    routes, which verify the report themselves (review 7695)."""

    def __init__(self, admin):
        self.admin = admin

    def _post(self, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        status, out = self.admin._post_json(f"/admin/compare/{self.admin.db}/{action}", body)
        if status != 200:
            raise L5Refused(f"memora-all refused the compare {action} ({status}): {out}")
        return out

    def begin(self) -> str:
        return self._post("begin", {})["run_id"]

    def record(self, report_path: Path, sha: str) -> Dict[str, Any]:
        return self._post("record", {"report": str(Path(report_path).resolve()), "report_sha256": sha})

    def abort(self, run_id: str) -> None:
        self._post("abort", {"run_id": run_id})


class DirectRecorder:
    """memora-all is stopped: the same begin/verify/record, written under
    the store's primary lock."""

    def __init__(self, store: Path, db: str):
        self.store, self.db = Path(store), db

    def _with(self, fn):
        from .backends import LocalSQLiteBackend, StoreLockedError, acquire_primary_lock, release_primary_lock

        try:
            acquire_primary_lock(self.store)
        except StoreLockedError as exc:
            raise CompareRefused(f"cannot record on {self.store}: {exc} (memora-all serves it: use its admin "
                                 "route instead)")
        try:
            conn = LocalSQLiteBackend(self.store).connect()
            try:
                return fn(conn)
            finally:
                conn.close()
        finally:
            release_primary_lock(self.store)

    def begin(self) -> str:
        from .replicator import begin_compare

        return self._with(begin_compare)["run_id"]

    def record(self, report_path: Path, sha: str) -> Dict[str, Any]:
        from .replicator import CompareNotRecorded, record_compare

        try:
            return self._with(lambda c: record_compare(c, db=self.db, report_path=str(report_path),
                                                       report_sha256=sha))
        except CompareNotRecorded as exc:
            raise L5Refused(f"the compare was not recorded: {exc}")

    def abort(self, run_id: str) -> None:
        from .replicator import abort_compare

        self._with(lambda c: abort_compare(c, run_id))


class NoRecorder:
    """--no-record: a report only."""

    def begin(self) -> str:
        import uuid

        return "unrecorded-" + uuid.uuid4().hex

    def record(self, report_path: Path, sha: str) -> None:
        return None

    def abort(self, run_id: str) -> None:
        return None


def run_compare(env: Env, recorder, run: Callable[[], Dict[str, Any]], *, db: str, account_id: str,
                database_id: str, out_dir: Path) -> Tuple[Dict[str, Any], Path, str, Any]:
    """One compare run: begin (the store registers its start), the mode's
    compare, the report (with the run id and the store's D1 identity), the
    record (verified by the store's side), the snapshots removed. A failure
    or a skipped run aborts the registration."""
    from .local_primary import d1_uri

    run_id = recorder.begin()
    recorded, done = None, False
    try:
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(env.clock()))
        report = run()
        report.update({"run_id": run_id, "db": db, "account_id": account_id, "database_id": database_id,
                       "d1_uri": d1_uri(account_id, database_id), "store": str(Path(env.store).resolve()),
                       "started_at": started})
        path, sha = write_report(report, out_dir, db, env.clock)
        if report.get("skipped"):
            recorder.abort(run_id)
        else:
            recorded = recorder.record(path, sha)
        done = True
    except BaseException:
        try:
            recorder.abort(run_id)
        except Exception:
            pass
        raise
    finally:
        import shutil

        if done:  # a failed run keeps its snapshot as evidence
            for d in env.snapshots:
                shutil.rmtree(d, ignore_errors=True)
    return report, path, sha, recorded
