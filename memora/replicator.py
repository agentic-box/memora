"""One-way replication of a local primary store to its D1 replica.

docs/local-primary-implementation.md §2 (and §0 P2-P4, §6.1). One daemon
thread per replicated store reads the store's `sync_outbox` in seq order,
coalesces it per key, reads each key's current local row, and either logs
the statements it would send (log mode) or sends them to D1 (write mode).

What it may send to D1 is fixed (P2): per-key UPSERTs on six tables, a
DELETE+INSERT pair for `memories_embeddings`, per-key DELETEs, per-key
read-back SELECTs and the epoch SELECT. `_check_statement` re-parses every
statement before it is sent; anything else halts the store.

Dark by construction: nothing starts unless MEMORA_REPLICATION is `log` or
`write` AND MEMORA_REPLICAS (or MEMORA_SHADOW_LOCAL) names the store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .schema import SYNC_TABLES
from .write_gate import data_dir

logger = logging.getLogger(__name__)

MODE_LOG = "log"
MODE_WRITE = "write"
EPOCH_SQL = "SELECT value FROM memories_meta WHERE key = 'embedding_change_epoch'"
EMBEDDINGS = "memories_embeddings"
# D1 enforces foreign keys (https://developers.cloudflare.com/d1/sql-api/foreign-keys/);
# memora's replicated children of memories(id) (schema.py, ON DELETE CASCADE).
FK_PARENT = "memories"
FK_CHILDREN = ("memories_embeddings", "memories_crossrefs")
DELETE_GUARD_ROWS = 50
DELETE_GUARD_FRACTION = 0.01
BACKOFF_MAX_S = 60.0
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReplicatorStatementError(RuntimeError):
    """A statement outside the P2 allow-list. The store halts."""


class ReplicatorConfigError(RuntimeError):
    """The replicator cannot start for this store (configuration)."""


# ------------------------------------------------------------------ P2 builder

def _cols_sql(cols: Sequence[str]) -> str:
    return ", ".join(cols)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)) and not isinstance(value, bool):
        return value
    raise ReplicatorStatementError(f"value of type {type(value).__name__} cannot be sent to D1")


def _build_statements(tbl: str, pk: Sequence[Any], row: Optional[Dict[str, Any]],
                      columns: Sequence[str]) -> List[Tuple[str, tuple]]:
    """The statements that make D1's row for (tbl, pk) equal the local row:
    an UPSERT of every column (a DELETE+INSERT pair for memories_embeddings,
    whose D1 update trigger would null `representation`), or, when the local
    row is gone, a DELETE by the full primary key."""
    if tbl not in SYNC_TABLES:
        raise ReplicatorStatementError(f"table {tbl!r} is not replicated")
    pk_cols = SYNC_TABLES[tbl]
    if len(pk) != len(pk_cols):
        raise ReplicatorStatementError(f"{tbl}: primary key {pk!r} does not match {pk_cols}")
    where = " AND ".join(f"{c} = ?" for c in pk_cols)
    delete = (f"DELETE FROM {tbl} WHERE {where}", tuple(_json_safe(v) for v in pk))
    if row is None:
        return [delete]
    cols = [c for c in columns if c in row]
    if not all(_IDENT.match(c) for c in cols) or not set(pk_cols) <= set(cols):
        raise ReplicatorStatementError(f"{tbl}: unusable column list {cols}")
    values = tuple(_json_safe(row[c]) for c in cols)
    marks = ", ".join("?" for _ in cols)
    if tbl == EMBEDDINGS:
        return [delete, (f"INSERT INTO {tbl} ({_cols_sql(cols)}) VALUES ({marks})", values)]
    rest = [c for c in cols if c not in pk_cols]
    sets = ", ".join(f"{c} = excluded.{c}" for c in rest)
    return [(f"INSERT INTO {tbl} ({_cols_sql(cols)}) VALUES ({marks}) "
             f"ON CONFLICT({_cols_sql(pk_cols)}) DO UPDATE SET {sets}", values)]


# ------------------------------------------------------------------ P2 checker

_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_UPSERT = re.compile(
    rf"INSERT INTO (?P<t>{_ID}) \((?P<cols>{_ID}(?:, {_ID})*)\) VALUES \((?P<q>\?(?:, \?)*)\) "
    rf"ON CONFLICT\((?P<pk>{_ID}(?:, {_ID})*)\) DO UPDATE SET (?P<set>{_ID} = excluded\.{_ID}(?:, {_ID} = excluded\.{_ID})*)"
)
_INSERT = re.compile(rf"INSERT INTO (?P<t>{_ID}) \((?P<cols>{_ID}(?:, {_ID})*)\) VALUES \((?P<q>\?(?:, \?)*)\)")
_DELETE = re.compile(rf"DELETE FROM (?P<t>{_ID}) WHERE (?P<w>{_ID} = \?(?: AND {_ID} = \?)*)")
_READBACK = re.compile(rf"SELECT \* FROM (?P<t>{_ID}) WHERE (?P<w>{_ID} = \?(?: AND {_ID} = \?)*)")


def _where_cols(w: str) -> Tuple[str, ...]:
    return tuple(part.split(" = ")[0] for part in w.split(" AND "))


def _check_statement(sql: str) -> str:
    """Accept exactly the P2 shapes (plan §0 P2); return the shape's name.
    Raises ReplicatorStatementError for anything else: DROP, TRUNCATE,
    UPDATE, a DELETE or read-back without the full primary key,
    sqlite_sequence, any table outside the seven, multiple statements."""
    if not isinstance(sql, str):
        raise ReplicatorStatementError("statement is not a string")
    if sql == EPOCH_SQL:
        return "epoch"
    m = _UPSERT.fullmatch(sql)
    if m:
        t = m["t"]
        cols = tuple(m["cols"].split(", "))
        pk = tuple(m["pk"].split(", "))
        sets = tuple(part.split(" = ")[0] for part in m["set"].split(", "))
        setsrc = tuple(part.split("excluded.")[1] for part in m["set"].split(", "))
        if (t in SYNC_TABLES and t != EMBEDDINGS and pk == SYNC_TABLES[t] and len(set(cols)) == len(cols)
                and set(pk) <= set(cols) and m["q"].count("?") == len(cols)
                and sets == setsrc and set(sets) == set(cols) - set(pk) and len(sets) == len(set(sets))):
            return "upsert"
        raise ReplicatorStatementError(f"rejected upsert: {sql[:200]}")
    m = _INSERT.fullmatch(sql)
    if m:
        cols = tuple(m["cols"].split(", "))
        if (m["t"] == EMBEDDINGS and set(SYNC_TABLES[EMBEDDINGS]) <= set(cols) and len(set(cols)) == len(cols)
                and m["q"].count("?") == len(cols)):
            return "insert"
        raise ReplicatorStatementError(f"rejected insert: {sql[:200]}")
    for shape, rx in (("delete", _DELETE), ("readback", _READBACK)):
        m = rx.fullmatch(sql)
        if m:
            if m["t"] in SYNC_TABLES and _where_cols(m["w"]) == SYNC_TABLES[m["t"]]:
                return shape
            raise ReplicatorStatementError(f"rejected {shape}: {sql[:200]}")
    raise ReplicatorStatementError(f"statement outside the replicator allow-list: {sql[:200]}")


# ------------------------------------------------------------------ outbox

@dataclass
class _Key:
    seq: int
    tbl: str
    pk: list
    row: Optional[Dict[str, Any]] = None
    statements: List[Tuple[str, tuple]] = field(default_factory=list)


@dataclass
class _Batch:
    lo: int
    hi: int
    keys: List[_Key]
    statements: List[Tuple[str, tuple]] = field(default_factory=list)
    deletes: Dict[str, int] = field(default_factory=dict)

    @property
    def attempt_id(self) -> str:
        """Stable for the same range and the same net deletes: what an
        operator allows with `resume --allow-deletes`."""
        dels = sorted((k.tbl, json.dumps(k.pk)) for k in self.keys if k.row is None)
        raw = json.dumps([self.lo, self.hi, dels], separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _table_columns(conn, tbl: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]


def _local_row(conn, tbl: str, pk: Sequence[Any]) -> Optional[Dict[str, Any]]:
    where = " AND ".join(f"{c} = ?" for c in SYNC_TABLES[tbl])
    row = conn.execute(f"SELECT * FROM {tbl} WHERE {where}", tuple(pk)).fetchone()
    return dict(row) if row is not None else None


def read_batch(conn, cursor: int, limit: int, *, hi_cap: Optional[int] = None) -> Optional[_Batch]:
    """The next outbox rows after `cursor` in seq order, coalesced per
    (table, key) keeping the highest seq, with each key's CURRENT local row
    (present -> upsert, absent -> delete). None when there is nothing."""
    sql = "SELECT seq, tbl, op, pk FROM sync_outbox WHERE seq > ?"
    params: list = [cursor]
    if hi_cap is not None:
        sql += " AND seq <= ?"
        params.append(hi_cap)
    rows = conn.execute(sql + " ORDER BY seq LIMIT ?", (*params, limit)).fetchall()
    if not rows:
        return None
    latest: Dict[Tuple[str, str], int] = {}
    for seq, tbl, _op, pk in rows:
        latest[(tbl, pk)] = seq
    keys = [_Key(seq, tbl, json.loads(pk)) for (tbl, pk), seq in sorted(latest.items(), key=lambda kv: kv[1])]
    columns: Dict[str, List[str]] = {}
    batch = _Batch(lo=rows[0][0], hi=rows[-1][0], keys=keys)
    for k in keys:
        if k.tbl not in SYNC_TABLES:
            raise ReplicatorStatementError(f"outbox names a table that is not replicated: {k.tbl!r}")
        k.row = _local_row(conn, k.tbl, k.pk)
    _add_parents(conn, batch)
    # Dependency order (review 7599 P1-2; D1 enforces foreign keys): parent
    # upserts first, then every other table, then parent deletes -- so a
    # child is never written before its parent, and a parent is deleted
    # only after its children.
    def phase(k: _Key) -> int:
        if k.tbl == FK_PARENT:
            return 0 if k.row is not None else 2
        return 1
    batch.keys = sorted(batch.keys, key=lambda k: (phase(k), k.seq))
    for k in batch.keys:
        if k.tbl not in columns:
            columns[k.tbl] = _table_columns(conn, k.tbl)
        if k.row is None:
            batch.deletes[k.tbl] = batch.deletes.get(k.tbl, 0) + 1
        k.statements = _build_statements(k.tbl, k.pk, k.row, columns[k.tbl])
        for stmt in k.statements:
            _check_statement(stmt[0])
            batch.statements.append(stmt)
    return batch


def _add_parents(conn, batch: _Batch) -> None:
    """Every child upsert in the batch travels with its parent's CURRENT row,
    even when the parent's own outbox rows are outside the range: a batch
    never splits a child from the parent it needs. A child whose parent is
    gone locally is sent as a delete (D1 cannot hold an orphan)."""
    present = {(k.tbl, json.dumps(k.pk)) for k in batch.keys}
    extra: List[_Key] = []
    for k in batch.keys:
        if k.tbl not in FK_CHILDREN or k.row is None:
            continue
        pid = k.row.get("memory_id")
        key = (FK_PARENT, json.dumps([pid]))
        if key in present:
            continue
        parent = _local_row(conn, FK_PARENT, [pid])
        if parent is None:
            logger.warning("replicator: %s row %s has no parent memories row %s locally; sending a delete",
                           k.tbl, k.pk, pid)
            k.row = None
            continue
        present.add(key)
        extra.append(_Key(k.seq, FK_PARENT, [pid], parent))
    batch.keys.extend(extra)


def delete_guard(conn, batch: _Batch) -> Optional[str]:
    """P3: None when the batch's net deletes are within bounds, else the
    halt reason. A table's deletes exceed the guard when they are more than
    DELETE_GUARD_ROWS or more than DELETE_GUARD_FRACTION of its local rows
    (so any delete from a table under 100 rows halts, deliberately)."""
    for tbl, n in sorted(batch.deletes.items()):
        total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] + n
        if n > DELETE_GUARD_ROWS or n > DELETE_GUARD_FRACTION * total:
            return f"delete_guard: {tbl} {n}/{total} attempt={batch.attempt_id}"
    return None


# ------------------------------------------------------------------ log mode (P4)

def log_dir(name: str) -> Path:
    return data_dir() / "replica-log" / name


def append_log(name: str, batch: _Batch, *, now: Optional[float] = None) -> Path:
    """Append one JSONL record per statement, flush, fsync (and fsync the
    directory when the file is new). Returns the file. The caller advances
    log_cursor_seq only after this returns (plan §2.2 step 5, P1-5)."""
    directory = log_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
    path = directory / f"{day}.jsonl"
    new = not path.exists()
    lines = []
    for k in batch.keys:
        for index, stmt in enumerate(k.statements):
            lines.append(json.dumps({"attempt_id": batch.attempt_id, "seq": k.seq, "index": index,
                                     "tbl": k.tbl, "pk": k.pk, "sql": stmt[0], "params": list(stmt[1])},
                                    separators=(",", ":")))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    if new:
        dfd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return path


def iter_log(name: str):
    """The logged statements of a store, oldest first, deduplicated by
    (seq, index) -- a crash between a log fsync and the cursor update makes
    the next cycle append the same range again -- with a torn final line
    dropped."""
    seen = set()
    directory = log_dir(name)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        data = path.read_bytes()
        end = data.rfind(b"\n") + 1
        for raw in data[:end].split(b"\n")[:-1]:
            rec = json.loads(raw)
            key = (rec["seq"], rec["index"])
            if key in seen:
                continue
            seen.add(key)
            yield rec


# ------------------------------------------------------------------ D1 connections

class ReplicaD1Connection:
    """The replicator's D1 writer (plan §2.4, §6.1). Built with the
    replicator token (MEMORA_D1_REPLICATOR_TOKEN), never CLOUDFLARE_API_TOKEN;
    schema-free (never runs ensure_schema, so no DDL); not behind the
    application write gate or intent journal (the replicator has its own
    durable in-flight marker, H3). Every statement it sends passes
    _check_statement first."""

    def __init__(self, account_id: str, database_id: str, api_token: str):
        from .backends import D1Connection

        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self._raw = D1Connection(account_id, database_id, api_token)  # transport + error mapping only

    def _post_json(self, body: dict) -> dict:
        """One POST of `body` to /query (the raw transport)."""
        from .backends import D1DefiniteError, _D1Transport

        conn = self._raw
        t = conn._own_transport()
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        status, _gh, raw = t.post("/query", json.dumps(body).encode("utf-8"), headers, retry_safe=False)
        if status >= 400:
            err = D1DefiniteError if status < 500 else RuntimeError
            raise err(f"D1 API error ({status}): {raw.decode(errors='replace')}")
        result = json.loads(raw.decode())
        if not result.get("success"):
            errors = result.get("errors", [])
            raise D1DefiniteError(f"D1 query failed: {errors[0].get('message') if errors else 'Unknown error'}")
        return result

    def execute_batch(self, stmts: Sequence[Tuple[str, Sequence[Any]]]) -> List[dict]:
        """Send `stmts` as one REST batch; per-statement results. Falls back to
        one statement per request when D1 rejects the batch body (HTTP 400).
        Correctness never relies on the batch being atomic."""
        from .backends import D1DefiniteError

        for sql, _ in stmts:
            _check_statement(sql)
        body = {"batch": [{"sql": sql, "params": list(params)} for sql, params in stmts]}
        try:
            result = self._post_json(body)
            return list(result.get("result") or [])
        except D1DefiniteError as exc:
            if "(400)" not in str(exc):
                raise
            logger.warning("D1 rejected the batch body (%s); sending one statement per request", exc)
        out = []
        for sql, params in stmts:
            result = self._post_json({"sql": sql, "params": list(params)})
            out.extend(result.get("result") or [])
        return out


def _replica_ids(replica_uri: str) -> Tuple[str, str]:
    if not replica_uri.startswith("d1://") or replica_uri.count("/") != 3:
        raise ReplicatorConfigError(f"replica URI must be d1://<account>/<database>: {replica_uri!r}")
    account, database = replica_uri[5:].split("/", 1)
    return account, database


def _reader_for(replica_uri: str):
    from .backends import D1SelectOnlyConnection

    account, database = _replica_ids(replica_uri)
    return D1SelectOnlyConnection.from_env(account, database)


def _writer_for(replica_uri: str) -> ReplicaD1Connection:
    token = os.getenv("MEMORA_D1_REPLICATOR_TOKEN", "").strip()
    if not token:
        raise ReplicatorConfigError("write mode needs MEMORA_D1_REPLICATOR_TOKEN (never CLOUDFLARE_API_TOKEN)")
    account, database = _replica_ids(replica_uri)
    return ReplicaD1Connection(account, database, token)


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return float(a) == float(b)
    return a == b


def _same_row(local: Optional[Dict[str, Any]], remote: Optional[Dict[str, Any]]) -> bool:
    if local is None or remote is None:
        return local is None and remote is None
    return set(local) <= set(remote) and all(_same_value(v, remote.get(c)) for c, v in local.items())


# ------------------------------------------------------------------ the replicator

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class StoreReplicator:
    """One store's replicator thread (plan §2.1-§2.7)."""

    def __init__(self, name: str, local, replica_uri: str, *, mode: str, shadow: bool = False,
                 batch_rows: int = 100, poll_s: float = 5.0,
                 writer_factory: Optional[Callable[[str], Any]] = None,
                 reader_factory: Optional[Callable[[str], Any]] = None,
                 broadcast: Optional[Callable[[], None]] = None):
        if mode not in (MODE_LOG, MODE_WRITE):
            raise ReplicatorConfigError(f"mode must be log or write, not {mode!r}")
        if shadow and mode == MODE_WRITE:
            raise ReplicatorConfigError(f"{name}: write mode refuses to start on a shadow store")
        self.name = name
        self.local = local
        self.replica_uri = replica_uri
        self.mode = mode
        self.shadow = shadow
        self.batch_rows = batch_rows
        self.poll_s = poll_s
        self._writer_factory = writer_factory or _writer_for
        self._reader_factory = reader_factory or _reader_for
        self._broadcast = broadcast if broadcast is not None else _default_broadcast
        self._writer = None
        self._reader = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn = None
        self._relaxed_epoch_before: Optional[int] = None
        self._backoff = 0.0
        self._lock = threading.Lock()
        self._metrics: Dict[str, Any] = {"mode": mode, "status": "disabled", "last_error": None,
                                         "d1_missing_vectors": None}
        # The H3 marker as the store's gate sees it (review 7599 P1-1): set in
        # the same critical section as the marker's commit, BEFORE the send,
        # and cleared only after the ack (or the reconcile) commits.
        self._marker_lock = threading.RLock()  # re-entrant: the owner may read it during the ack
        self._marker: Optional[str] = None

    # --------------------------------------------------------------- local state

    def _state(self) -> Dict[str, Any]:
        row = self._conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        if row is None:
            raise ReplicatorConfigError(f"{self.name}: no sync_state row (install_sync has not run)")
        return dict(row)

    def _local_txn(self, statements: Sequence[Tuple[str, tuple]]) -> None:
        """One store_write (BEGIN IMMEDIATE under the store's write lock,
        plan §3) on the replicator's anchor writer."""
        from .backends import store_write

        with store_write(self._conn):
            for sql, params in statements:
                self._conn.execute(sql, params)

    def _halt(self, reason: str) -> None:
        logger.error("replicator %s halted: %s", self.name, reason)
        self._local_txn([("UPDATE sync_state SET halted_reason = ?, halted_at = ? WHERE id = 1",
                          (reason, _now_iso()))])
        self._refresh_metrics(status="halted")

    def _set_error(self, message: str) -> None:
        try:
            self._local_txn([("UPDATE sync_state SET last_error = ? WHERE id = 1", (message[:500],))])
        except Exception:
            pass
        with self._lock:
            self._metrics["last_error"] = message[:500]

    # --------------------------------------------------------------- one cycle

    def run_once(self) -> str:
        """One cycle; returns what happened (for tests and the loop)."""
        state = self._state()
        if state.get("halted_reason"):
            self._refresh_metrics(status="halted")
            return "halted"
        if state["replica_uri"] != self.replica_uri:
            self._halt(f"config: MEMORA_REPLICAS says {self.replica_uri!r} but sync_state says {state['replica_uri']!r}")
            return "halted"
        if self.mode == MODE_LOG:
            return self._log_cycle(state)
        if state.get("inflight_id"):
            return self._reconcile(state)
        return self._write_cycle(state)

    def _next_batch(self, cursor: int, state: Dict[str, Any], hi_cap: Optional[int] = None):
        try:
            batch = read_batch(self._conn, cursor, self.batch_rows, hi_cap=hi_cap)
        except ReplicatorStatementError as exc:
            self._halt(f"statement_rejected: {exc}")
            return None, "halted"
        if batch is None:
            return None, "idle"
        reason = delete_guard(self._conn, batch)
        if reason and state.get("allow_deletes_attempt") != batch.attempt_id:
            self._halt(reason)
            return None, "halted"
        return batch, None

    def _log_cycle(self, state: Dict[str, Any]) -> str:
        batch, outcome = self._next_batch(int(state["log_cursor_seq"]), state)
        if batch is None:
            self._refresh_metrics(status="running")
            return outcome
        append_log(self.name, batch)
        _test_hook("MEMORA_TEST_KILL_AFTER_LOG_FSYNC")
        self._local_txn([("UPDATE sync_state SET log_cursor_seq = ?, allow_deletes_attempt = NULL WHERE id = 1",
                          (batch.hi,))])
        self._refresh_metrics(status="running")
        return "logged"

    # --------------------------------------------------------------- write mode

    def _d1(self):
        if self._writer is None:
            self._writer = self._writer_factory(self.replica_uri)
        return self._writer

    def _read(self):
        if self._reader is None:
            self._reader = self._reader_factory(self.replica_uri)
        return self._reader

    def _read_epoch(self) -> int:
        rows, _meta = self._read().execute(EPOCH_SQL)
        if not rows:
            raise RuntimeError("D1 has no embedding_change_epoch row")
        return int(rows[0]["value"])

    def _write_cycle(self, state: Dict[str, Any]) -> str:
        batch, outcome = self._next_batch(int(state["last_acked_seq"]), state)
        if batch is None:
            self._refresh_metrics(status="running")
            return outcome
        expected = state.get("d1_epoch_expected")
        if expected is None:
            self._halt("config: sync_state.d1_epoch_expected is not set (the seed sets it)")
            return "halted"
        inflight = uuid.uuid4().hex
        # The whole send..ack is one in-flight entry of the store's gate
        # (exempt: it drains while ingress is frozen), so freeze() waits for
        # it; a failure leaves the durable marker, i.e. an open intent.
        token = self._gate_enter("replicator send")
        try:
            # H3: durable marker BEFORE anything is sent.
            with self._marker_lock:
                self._local_txn([("UPDATE sync_state SET inflight_id = ?, inflight_lo = ?, inflight_hi = ?, "
                                  "inflight_epoch_before = ?, inflight_at = ? WHERE id = 1",
                                  (inflight, batch.lo, batch.hi, int(expected), _now_iso()))])
                self._marker = inflight
            # H2 preflight: its own request, before any mutating request.
            pre = self._read_epoch()
            relaxed = self._relaxed_epoch_before
            if (relaxed is None and pre != int(expected)) or (relaxed is not None and pre < relaxed):
                self._halt(f"foreign_writer: expected {expected} got {pre}")
                return "halted"
            results = self._d1().execute_batch(batch.statements + [(EPOCH_SQL, ())])
            _test_hook("MEMORA_TEST_KILL_AFTER_SEND")
            if len(results) != len(batch.statements) + 1 or not all(r.get("success") is True for r in results):
                raise RuntimeError("D1 batch did not succeed for every statement")
            post_rows = results[-1].get("results") or []
            if not post_rows:
                raise RuntimeError("the batch's epoch postcheck returned no row")
            self._relaxed_epoch_before = None
            self._ack(batch.hi, int(post_rows[0]["value"]), unverified=False)
            return "sent"
        finally:
            self._gate_leave(token)

    def _gate_enter(self, desc: str):
        gate_fn = getattr(self.local, "write_gate", None)
        if gate_fn is None or self.shadow:
            return None
        gate = gate_fn()
        return (gate, gate.enter(desc, exempt=True))

    def _gate_leave(self, token) -> None:
        if token is not None:
            token[0].leave(token[1])

    def _ack(self, hi: int, post_epoch: int, *, unverified: bool) -> None:
        """Ack and prune in one local transaction (plan §2.4). Outbox rows are
        kept until a clean compare consumed them, and for at least 24 h."""
        with self._marker_lock:
            self._ack_locked(hi, post_epoch, unverified=unverified)
            self._marker = None
        self._backoff = 0.0
        self._refresh_metrics(status="running")
        try:
            self._broadcast()
        except Exception as exc:  # the broadcast is advisory
            logger.warning("replicator %s: broadcast after ack failed: %s", self.name, exc)

    def _ack_locked(self, hi: int, post_epoch: int, *, unverified: bool) -> None:
        self._local_txn([
            ("UPDATE sync_state SET last_acked_seq = ?, d1_epoch_expected = ?, inflight_id = NULL, "
             "inflight_lo = NULL, inflight_hi = NULL, inflight_epoch_before = NULL, inflight_at = NULL, "
             "last_ack_at = ?, last_error = NULL, allow_deletes_attempt = NULL, "
             "epoch_unverified_batches = epoch_unverified_batches + ? WHERE id = 1",
             (hi, post_epoch, _now_iso(), 1 if unverified else 0)),
            ("DELETE FROM sync_outbox WHERE seq <= MIN(?, (SELECT compare_consumed_seq FROM sync_state WHERE id = 1)) "
             "AND created_at < julianday('now') - 1", (hi,)),
        ])

    def _reconcile(self, state: Dict[str, Any]) -> str:
        """H3: a marker is present, so the previous send's outcome is unknown.
        Read the range's keys back from D1: all equal -> ack (epoch
        unverified); otherwise clear the marker and resend with the
        preflight relaxed to >= the marker's epoch_before."""
        token = self._gate_enter("replicator reconcile")
        try:
            return self._reconcile_entered(state)
        finally:
            self._gate_leave(token)

    def _reconcile_entered(self, state: Dict[str, Any]) -> str:
        lo, hi = int(state["inflight_lo"]), int(state["inflight_hi"])
        batch = read_batch(self._conn, lo - 1, 1_000_000, hi_cap=hi)
        reader = self._read()
        matched = True
        for k in (batch.keys if batch else []):
            where = " AND ".join(f"{c} = ?" for c in SYNC_TABLES[k.tbl])
            sql = f"SELECT * FROM {k.tbl} WHERE {where}"
            _check_statement(sql)
            rows, _meta = reader.execute(sql, tuple(k.pk))
            if not _same_row(k.row, rows[0] if rows else None):
                matched = False
                break
        if matched:
            self._ack(hi, self._read_epoch(), unverified=True)
            return "reconciled-acked"
        self._relaxed_epoch_before = int(state["inflight_epoch_before"])
        with self._marker_lock:
            self._local_txn([("UPDATE sync_state SET inflight_id = NULL, inflight_lo = NULL, inflight_hi = NULL, "
                              "inflight_epoch_before = NULL, inflight_at = NULL WHERE id = 1", ())])
            self._marker = None
        return "reconciled-resend"

    # --------------------------------------------------------------- metrics

    def _refresh_metrics(self, *, status: Optional[str] = None) -> None:
        try:
            st = self._state()
            cursor = int(st["last_acked_seq"] if self.mode == MODE_WRITE else st["log_cursor_seq"])
            head = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_outbox").fetchone()[0]
            lag, oldest = self._conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM sync_outbox WHERE seq > ?", (cursor,)).fetchone()
        except Exception:
            return
        with self._lock:
            m = self._metrics
            if status is not None:
                m["status"] = status
            if st.get("halted_reason"):
                m["status"] = "halted"
            m.update({
                "mode": self.mode, "head_seq": max(int(head), cursor), "last_acked_seq": int(st["last_acked_seq"]),
                "log_cursor_seq": int(st["log_cursor_seq"]), "lag_rows": int(lag),
                "oldest_unacked_julianday": oldest, "last_ack_at": st.get("last_ack_at"),
                "halted_reason": st.get("halted_reason"), "last_error": st.get("last_error"),
                "epoch_unverified_batches": int(st.get("epoch_unverified_batches") or 0),
                "inflight_id": st.get("inflight_id"),
            })

    def status(self) -> Dict[str, Any]:
        """Never calls D1 (plan §2.5): the last cycle's view of the store."""
        with self._lock:
            out = dict(self._metrics)
        oldest = out.pop("oldest_unacked_julianday", None)
        out["oldest_unacked_age_s"] = (
            round((2440587.5 + time.time() / 86400.0 - float(oldest)) * 86400.0, 1) if oldest is not None else 0.0)
        return out

    def open_marker(self) -> List[str]:
        """The H3 in-flight marker, as an open intent of the store's gate
        (plan §1): a frozen store with a marker is frozen-unsafe. It mirrors
        the DURABLE marker (set with its commit, before the send; cleared
        after the ack commits), never the last metrics refresh."""
        with self._marker_lock:
            iid = self._marker
        return [f"replica-inflight:{iid}"] if iid else []

    # --------------------------------------------------------------- thread

    def _open(self) -> None:
        self._conn = self.local.connect_replicator()
        with self._marker_lock:
            self._marker = self._state().get("inflight_id")  # a marker left by a previous process
        self._refresh_metrics(status="running")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"memora-replicator-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.wake()
        if self._thread is not None:
            self._thread.join(timeout)

    def wake(self) -> None:
        from .backends import commit_event

        commit_event(self.local.db_path).set()

    def _loop(self) -> None:
        from .backends import commit_event

        event = commit_event(self.local.db_path)
        try:
            self._open()
        except Exception as exc:
            self._set_error(f"open failed: {exc}")
            return
        while not self._stop.is_set():
            wait = self.poll_s
            try:
                outcome = self.run_once()
                if outcome in ("sent", "logged", "reconciled-acked", "reconciled-resend"):
                    continue  # there may be more: no wait
            except Exception as exc:
                self._backoff = min(BACKOFF_MAX_S, max(1.0, self._backoff * 2))
                wait = self._backoff
                self._set_error(f"{type(exc).__name__}: {exc}")
                self._refresh_metrics(status="backoff")
            event.wait(wait)
            event.clear()
        try:
            self._conn.close()
        except Exception:
            pass


def _test_hook(env: str) -> None:
    """Process-death injection for the kill tests (tests only)."""
    if os.getenv(env) == "1":
        os._exit(137)


def _default_broadcast() -> None:
    from .cloud_sync import schedule_sync

    schedule_sync()


# ------------------------------------------------------------------ operator actions

def resume(conn, *, accept_d1_epoch: Optional[int] = None, allow_deletes: Optional[str] = None) -> str:
    """Clear a halt (plan §2.6, §0 P3); `local_primary.py resume` (L5) calls
    this. A foreign-writer halt needs accept_d1_epoch (the D1 epoch read after
    a §5.2 barrier compare); a delete-guard halt needs allow_deletes equal to
    the halted attempt id, which allows that one attempt only. Returns the
    cleared reason."""
    row = conn.execute("SELECT halted_reason FROM sync_state WHERE id = 1").fetchone()
    reason = row[0] if row else None
    if not reason:
        raise ValueError("the store is not halted")
    updates = ["halted_reason = NULL", "halted_at = NULL"]
    params: list = []
    if reason.startswith("foreign_writer:"):
        if accept_d1_epoch is None:
            raise ValueError("a foreign-writer halt needs accept_d1_epoch (after a barrier compare)")
        updates.append("d1_epoch_expected = ?")
        params.append(int(accept_d1_epoch))
    elif reason.startswith("delete_guard:"):
        attempt = reason.rsplit("attempt=", 1)[-1]
        if allow_deletes != attempt:
            raise ValueError(f"a delete-guard halt needs allow_deletes={attempt}")
        updates.append("allow_deletes_attempt = ?")
        params.append(attempt)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"UPDATE sync_state SET {', '.join(updates)} WHERE id = 1", params)
    conn.commit()
    return reason


# ------------------------------------------------------------------ startup

_REPLICATORS: Dict[str, StoreReplicator] = {}


def replicator_for(name: str) -> Optional[StoreReplicator]:
    return _REPLICATORS.get(name)


def _json_env(name: str) -> Dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ReplicatorConfigError(f"{name} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        raise ReplicatorConfigError(f"{name} must be a JSON object of name -> value")
    return value


def start_replicators(*, start: bool = True) -> Dict[str, Any]:
    """Start one replicator per configured store (plan §2.1). Dark unless
    MEMORA_REPLICATION is log|write and MEMORA_REPLICAS or MEMORA_SHADOW_LOCAL
    names the store. Returns {name: status or {"error": ...}}."""
    from .backends import LocalSQLiteBackend
    from .storage import _store_refusals, backend_for

    mode = os.getenv("MEMORA_REPLICATION", "").strip().lower()
    if mode not in (MODE_LOG, MODE_WRITE):
        return {}
    out: Dict[str, Any] = {}
    replicas = _json_env("MEMORA_REPLICAS")
    shadows = _json_env("MEMORA_SHADOW_LOCAL")
    plans = [(n, uri, False) for n, uri in replicas.items()] + [(n, path, True) for n, path in shadows.items()]
    for name, value, shadow in plans:
        try:
            if name in _REPLICATORS:
                continue
            if name in _store_refusals:
                # The /data check refused this store (memora/data_volume.py).
                # A shadow file is built from its path, not through
                # backend_for, so without this it would be created on the
                # unfit /data.
                raise ReplicatorConfigError(f"{name}: store refused: {_store_refusals[name]}")
            if shadow:
                local = LocalSQLiteBackend(Path(value))
                store_mode = MODE_LOG  # forced: a shadow never writes D1
            else:
                local = backend_for(name)
                store_mode = mode
                if not isinstance(local, LocalSQLiteBackend):
                    raise ReplicatorConfigError(f"{name}: a replicated store must be local (sqlite://)")
            conn = local.connect_replicator()
            try:
                row = conn.execute("SELECT replica_uri FROM sync_state WHERE id = 1").fetchone()
            except Exception:
                row = None
            finally:
                conn.close()
            if row is None:
                raise ReplicatorConfigError(f"{name}: no sync_state (install_sync has not run)")
            replica_uri = row[0]
            if not shadow and replica_uri != value:
                raise ReplicatorConfigError(f"{name}: MEMORA_REPLICAS {value!r} != sync_state {replica_uri!r}")
            if store_mode == MODE_WRITE:
                _writer_for(replica_uri)  # fail early without the replicator token
            rep = StoreReplicator(name, local, replica_uri, mode=store_mode, shadow=shadow)
            if not shadow:
                gate = local.write_gate()
                if gate.journal_status is None:
                    gate.journal_status = lambda rep=rep: (rep.open_marker(), None)
            _REPLICATORS[name] = rep
            if start:
                rep.start()
            out[name] = {"mode": store_mode, "shadow": shadow}
        except Exception as exc:
            logger.error("replicator for %s not started: %s", name, exc)
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def stop_replicators() -> None:
    for rep in list(_REPLICATORS.values()):
        rep.stop()
    _REPLICATORS.clear()
