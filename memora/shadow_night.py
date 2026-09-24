"""The nightly shadow check (docs/local-primary-implementation.md §2.9,
"Nightly check (shadow mode)"; slice L9a piece b). An operator command
(`local_primary.py shadow-night`), run where the shadow file and the
replicator log live (inside memora-all's container, or with MEMORA_DATA_DIR
pointing at the volume). It writes nothing to D1 and needs no barrier: D1 is
the source.

(a) Shadow vs D1. A full-table compare of the seven replicated tables (the
    replicator's memories_meta exclusions apart), D1 read through the READ
    token. A diffed key is re-read once on both sides after a short pause; a
    diff that persists is a shadow-apply bug.
(b) Builder. The key set in the replicator's log must equal the outbox key
    set up to sync_state.log_cursor_seq, and the log must not be halted (a
    halted replicator stops logging: no night is clean until an operator
    resumes it; the shadow itself is not marked dirty for it). Then the log
    is replayed, in order,
    into a scratch copy of the SQL export the shadow was seeded from, and
    compared with a .backup of the shadow taken now; keys the outbox touched
    after the cursor are not yet in the log and are left out. A diff is a
    statement-builder bug.

A night is clean when (a) and (b) show zero diffs and shadow_state.dirty is 0.
A clean night increments clean_nights once per UTC day (last_clean_night); a
failed one resets it to 0 and, for a diff, marks the shadow dirty (it must be
re-seeded). The cutover needs clean_nights >= 7 (plan §0 P4).
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .schema import SYNC_META_EXCLUDED, SYNC_TABLES
from .shadow import _norm, rows_equal

REQUIRED_CLEAN_NIGHTS = 7
Key = Tuple[str, Tuple[Any, ...]]


def _ro(path: Path):
    """A read-only connection to the live shadow file, through the store's
    read path (never a writer here: the applier owns the file)."""
    from .backends import LocalSQLiteBackend

    return LocalSQLiteBackend(Path(path)).connect_read_only()


def _scratch(path: Path) -> sqlite3.Connection:
    """A scratch file of this check (the backup, the replayed export)."""
    from .local_primary import _scratch_connect

    db = _scratch_connect(Path(path))
    db.row_factory = sqlite3.Row
    return db


def _key(table: str, row: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple(_norm(row[c]) for c in SYNC_TABLES[table])


def _keep(table: str, row: Dict[str, Any]) -> bool:
    return not (table == "memories_meta" and row.get("key") in SYNC_META_EXCLUDED)


def _local_rows(db: sqlite3.Connection, table: str) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    return {_key(table, dict(r)): dict(r) for r in db.execute(f'SELECT * FROM "{table}"')
            if _keep(table, dict(r))}


def _local_row(db: sqlite3.Connection, table: str, key: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    where = " AND ".join(f"{c} = ?" for c in SYNC_TABLES[table])
    r = db.execute(f'SELECT * FROM "{table}" WHERE {where}', key).fetchone()
    return dict(r) if r is not None else None


def compare_with_d1(shadow_path: Path, reader, *, pause_s: float = 2.0,
                    sleep: Callable[[float], None] = time.sleep) -> Dict[str, List[str]]:
    """(a): {table: [key descriptions]} of PERSISTENT diffs (after one re-read)."""
    from .local_primary import D1Reader

    d1 = D1Reader(reader)
    suspects: List[Key] = []
    db = _ro(shadow_path)
    try:
        for table in SYNC_TABLES:
            remote = {_key(table, r): r for r in d1.all_rows(table) if _keep(table, r)}
            local = _local_rows(db, table)
            for k in set(remote) | set(local):
                if not rows_equal(remote.get(k), local.get(k)):
                    suspects.append((table, k))
    finally:
        db.close()
    if not suspects:
        return {}
    sleep(pause_s)  # writes that were in flight during the full read settle
    out: Dict[str, List[str]] = {}
    db = _ro(shadow_path)
    try:
        for table, k in suspects:
            where = " AND ".join(f"{c} = ?" for c in SYNC_TABLES[table])
            rows = d1.rows(f'SELECT * FROM "{table}" WHERE {where}', k)
            remote = rows[0] if rows else None
            if not rows_equal(remote, _local_row(db, table, k)):
                side = "missing locally" if remote is not None and _local_row(db, table, k) is None else (
                    "missing on D1" if remote is None else "differs")
                out.setdefault(table, []).append(f"{list(k)} {side}")
    finally:
        db.close()
    return out


def _outbox_keys(db: sqlite3.Connection, lo: int, hi: Optional[int]) -> Dict[Key, int]:
    """{key: latest seq} of outbox rows with lo < seq (<= hi when given)."""
    sql = "SELECT seq, tbl, pk FROM sync_outbox WHERE seq > ?" + (" AND seq <= ?" if hi is not None else "")
    params = (lo, hi) if hi is not None else (lo,)
    out: Dict[Key, int] = {}
    for seq, tbl, pk in db.execute(sql, params):
        out[(tbl, tuple(_norm(v) for v in json.loads(pk)))] = seq
    return out


def builder_check(name: str, shadow_path: Path, seed_sql: Path, *,
                  log_records: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """(b): {"key_set": {...}, "diffs": {table: [...]}, "cursor": n}."""
    from . import replicator
    from .local_primary import load_sql

    work = Path(tempfile.mkdtemp(prefix=f"shadow-night-{name}-"))
    backup = work / "shadow.backup.db"
    src = _ro(shadow_path)
    dst = _scratch(backup)
    try:
        src.backup(dst)  # a consistent copy at one instant
    finally:
        dst.close()
        src.close()
    db = _scratch(backup)
    try:
        st = db.execute("SELECT log_cursor_seq, halted_reason FROM sync_state WHERE id = 1").fetchone()
        cursor, halted = int(st[0]), st[1]
        logged_upto = _outbox_keys(db, 0, cursor)
        after = _outbox_keys(db, cursor, None)
        records = [r for r in (log_records if log_records is not None else replicator.iter_log(name))
                   if int(r["seq"]) <= cursor]
        log_keys = {(r["tbl"], tuple(_norm(v) for v in r["pk"])) for r in records}
        missing_in_log = sorted(set(logged_upto) - log_keys, key=str)
        extra_in_log = sorted(log_keys - set(logged_upto), key=str)
        scratch = work / "replayed.db"
        load_sql(seed_sql, scratch)
        rdb = _scratch(scratch)
        try:
            for r in sorted(records, key=lambda r: (int(r["seq"]), int(r["index"]))):
                rdb.execute(r["sql"], tuple(r["params"]))
            rdb.commit()
            diffs: Dict[str, List[str]] = {}
            for table in SYNC_TABLES:
                replayed = _local_rows(rdb, table)
                shadow = _local_rows(db, table)
                for k in set(replayed) | set(shadow):
                    if (table, k) in after:
                        continue  # touched after the cursor: not in the log yet
                    if not rows_equal(replayed.get(k), shadow.get(k)):
                        diffs.setdefault(table, []).append(f"{list(k)}")
        finally:
            rdb.close()
    finally:
        db.close()
    return {"cursor": cursor, "log_records": len(records), "halted": halted,
            "key_set": {"missing_in_log": [str(k) for k in missing_in_log][:50],
                        "extra_in_log": [str(k) for k in extra_in_log][:50]},
            "diffs": diffs, "work_dir": str(work)}


def record_night(shadow_path: Path, clean: bool, reason: Optional[str], *,
                 today: Optional[str] = None) -> Dict[str, Any]:
    """Update shadow_state for one night; returns the new row."""
    from .backends import LocalSQLiteBackend, store_write

    day = today or time.strftime("%Y-%m-%d", time.gmtime())
    db = LocalSQLiteBackend(Path(shadow_path)).connect()  # the store's writer path
    try:
        with store_write(db):
            st = dict(db.execute("SELECT * FROM shadow_state WHERE id = 1").fetchone())
            _record(db, st, clean, reason, day)
        return dict(db.execute("SELECT * FROM shadow_state WHERE id = 1").fetchone())
    finally:
        db.close()


def _record(db, st: Dict[str, Any], clean: bool, reason: Optional[str], day: str) -> None:
    """The shadow_state update of one night (inside the caller's store_write)."""
    if clean and not st["dirty"]:
        if st["last_clean_night"] != day:
            db.execute("UPDATE shadow_state SET clean_nights = clean_nights + 1, last_clean_night = ? WHERE id = 1",
                       (day,))
        return
    db.execute("UPDATE shadow_state SET clean_nights = 0 WHERE id = 1")
    if reason:
        db.execute("UPDATE shadow_state SET dirty = 1, dirty_reason = COALESCE(dirty_reason, ?), "
                   "dirty_at = COALESCE(dirty_at, ?) WHERE id = 1",
                   (reason, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))


def run_night(name: str, shadow_path: Path, reader, seed_sql: Path, *, today: Optional[str] = None,
              pause_s: float = 2.0, sleep: Callable[[float], None] = time.sleep,
              log_records: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    shadow_path = Path(shadow_path)
    a = compare_with_d1(shadow_path, reader, pause_s=pause_s, sleep=sleep)
    b = builder_check(name, shadow_path, Path(seed_sql), log_records=log_records)
    b_bad = (bool(b["diffs"]) or bool(b["key_set"]["missing_in_log"]) or bool(b["key_set"]["extra_in_log"])
             or bool(b["halted"]))
    db = _ro(shadow_path)
    try:
        dirty = bool(db.execute("SELECT dirty FROM shadow_state WHERE id = 1").fetchone()[0])
    finally:
        db.close()
    reason = None
    if a:
        reason = f"nightly (a): the shadow differs from D1 in {sorted(a)}"
    elif b["halted"]:
        reason = None  # not a shadow defect: the log is halted (L3); the night is just not clean
    elif b_bad:
        reason = "nightly (b): the replicator log does not rebuild the shadow"
    clean = not a and not b_bad and not dirty
    if clean:
        import shutil

        shutil.rmtree(b["work_dir"], ignore_errors=True)  # kept only for diagnosing a failed night
        b = {k: v for k, v in b.items() if k != "work_dir"}
    state = record_night(shadow_path, clean, reason, today=today)
    return {"store": name, "clean": clean, "compare": a, "builder": b, "was_dirty": dirty,
            "shadow_state": state, "clean_nights": state["clean_nights"],
            "ready_for_cutover": bool(state["clean_nights"] >= REQUIRED_CLEAN_NIGHTS and not state["dirty"])}
