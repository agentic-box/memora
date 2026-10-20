"""Add a brand-new, empty store to memora-all (Item 95; docs/add-store-runbook.md).

Two operator steps live here; the rest of the procedure is the unchanged
cutover (scripts/cutover_store.sh) and scripts/add_store.sh.

bootstrap_d1
    Gives a NEW, EMPTY Cloudflare D1 database the app's own schema, and does
    nothing else. The replicator is schema-free (it never sends DDL), so a new
    replica database needs its schema before the first replicated write. The
    leader approved this as a scoped exception to "a verified export before
    every D1-affecting step" (a database with no schema cannot have a
    receipt): the exact database is pinned (account + id + name from the
    user's `wrangler d1 create`), proven read-only to be that database, to be
    readable, to hold zero user objects and to be configured nowhere; then ONLY
    what `schema.ensure_schema` sends over D1 is allowed (CREATE ... IF NOT
    EXISTS, ADD COLUMN, and its one `memories_meta` epoch row), enforced by a
    statement guard on the connection that refuses everything else BEFORE it
    is sent (the allow-list is the exact set of write statements the initializer itself
    sends, recorded from `ensure_schema` on a scratch D1). The guard is a D1Connection subclass, so ensure_schema takes its
    D1 branches (no FTS, no api_idempotency, no sync objects). Nothing is
    served while it runs: the store is in no registry.

pre_freeze / clear_freeze
    The persisted freeze (/data/freeze/<db>) written BEFORE the store is
    first served, so it comes up frozen and nothing can write it through
    memora-all until the cutover's barrier compare is clean and it is thawed.
    clear_freeze is the rollback helper: it refuses while the running service
    still routes the store.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .backends import (
    D1Backend,
    D1Connection,
    ReadReplicationRefused,
    read_replication_allowed,
    require_read_replication_disabled,
    warn_read_replication_override,
)
from .local_primary import L5Refused, _user_table
from .sql_classify import READ, classify_statement

DB_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
STATE_VERSION = 1
# memories_meta carries the embedding epoch; sqlite_sequence is SQLite's own.
META_TABLE = "memories_meta"
EPOCH_KEY = "embedding_change_epoch"


class AddStoreRefused(L5Refused):
    """A guard refused; nothing was changed by the refused step."""


# ------------------------------------------------------------------ statement guard

_WS = re.compile(r"\s+")


def _norm_stmt(sql: str) -> str:
    text = _WS.sub(" ", sql.strip()).strip()
    return text[:-1].rstrip() if text.endswith(";") else text


_REFERENCE_WRITES: Optional[frozenset] = None


def reference_writes() -> frozenset:
    """The exact (whitespace-normalised) write statements `schema.ensure_schema`
    sends over a D1 connection to an empty database, recorded by running that
    very function on a scratch D1 (a D1Connection subclass, so its D1 branches
    run). This IS the allow-list: the guard cannot drift from the initializer,
    and an arbitrary CREATE TABLE / INDEX / trigger that the initializer does
    not send is refused."""
    global _REFERENCE_WRITES
    if _REFERENCE_WRITES is None:
        from . import schema

        conn = ScratchD1Connection()
        try:
            schema.ensure_schema(conn)
            _REFERENCE_WRITES = frozenset(
                _norm_stmt(sql) for sql in conn.sent
                if classify_statement(sql).kind != READ)
        finally:
            conn.close()
    return _REFERENCE_WRITES


def check_bootstrap_statement(sql: str, params: Optional[Sequence[Any]] = None) -> None:
    """Raise AddStoreRefused unless `sql` is a read or EXACTLY one of the write
    statements `ensure_schema` sends over D1 (reference_writes). Called BEFORE
    a statement is sent."""
    text = _norm_stmt(sql)
    if classify_statement(sql).kind == READ:
        return
    if params:
        raise AddStoreRefused(f"bootstrap refused a statement with parameters: {text[:80]!r}")
    if text in reference_writes():
        return
    raise AddStoreRefused(f"bootstrap refused a statement that ensure_schema does not send: {text[:120]!r}")


class _GuardMixin:
    """Applied to a D1Connection subclass: `_send` is the one point every
    statement leaves through, so the guard cannot be bypassed by execute,
    executemany or executescript."""

    def _send(self, sql: str, params: tuple = None) -> dict:  # type: ignore[override]
        check_bootstrap_statement(sql, params)
        return super()._send(sql, params)  # type: ignore[misc]


class BootstrapD1Connection(_GuardMixin, D1Connection):
    """The real HTTP connection, behind the guard (still a D1Connection)."""


class BootstrapD1Backend(D1Backend):
    """A D1Backend whose connections are guarded. Everything else (gate,
    journal, bookmarks, the read-replication refusal) is the stock backend.
    ``connect`` does not call ``super().connect``; it calls the same check."""

    def connect(self, *, check_same_thread: bool = True) -> D1Connection:
        self._require_read_replication_disabled()
        journal = self.journal()
        unusable = journal.fatal or journal.broken
        self.write_gate()
        conn = BootstrapD1Connection(self.account_id, self.database_id, self.api_token)
        if unusable:
            conn.read_only_reason = unusable
        conn._session_token = self.get_latest_bookmark()
        conn._backend = self
        return conn


# ------------------------------------------------------------------ scratch D1 (reference + tests)

def _refuse_registered_store(path: str) -> None:
    """A scratch D1 is the REMOTE side of a D1Connection, never a memora store: it opens ":memory:" or a file the
    caller owns (tests), and refuses a path that is one of memora-all's registered local stores (which must only be
    opened through LocalSQLiteBackend.connect, under the store lock)."""
    from .storage import database_registry

    try:
        routes = database_registry()
    except Exception:
        return
    real = os.path.realpath(path)
    for spec in routes.values():
        spec = str(spec)
        if "://" in spec and not spec.startswith("file://"):
            continue
        local = spec[len("file://"):] if spec.startswith("file://") else spec
        if os.path.realpath(local) == real:
            raise ValueError(f"ScratchD1Connection refuses {path}: it is a registered memora store")


class ScratchD1Connection(D1Connection):
    """A D1Connection whose HTTP leg is a local SQLite database. It takes
    every D1 branch (it IS a D1Connection) and is what the reference schema is
    built on, so the live database is compared with what the app's D1 path
    produces, not with a local-SQLite schema. Tests use it as the fake D1."""

    def __init__(self, path: str = ":memory:", account_id: str = "scratch", database_id: str = "scratch"):
        super().__init__(account_id, database_id, "scratch")
        if str(path) != ":memory:":
            _refuse_registered_store(str(path))
        self._sql = sqlite3.connect(path, check_same_thread=False)
        self._sql.row_factory = sqlite3.Row
        self.sent: List[str] = []

    def _execute_api(self, sql: str, params: tuple = None) -> dict:
        return self._send(sql, params)

    def _send(self, sql: str, params: tuple = None) -> dict:
        self.sent.append(_WS.sub(" ", sql.strip()))
        cur = self._sql.execute(sql, () if params is None else tuple(params))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        self._sql.commit()
        return {"success": True, "result": [{"results": rows, "meta": {"last_row_id": cur.lastrowid or 0,
                                                                         "changes": max(cur.rowcount, 0)}}]}

    def close(self) -> None:
        self._sql.close()


class GuardedScratchD1Connection(_GuardMixin, ScratchD1Connection):
    """The scratch D1 behind the same guard (tests of the bootstrap path)."""


# ------------------------------------------------------------------ signatures

def _query(execute: Callable[[str, tuple], List[Dict[str, Any]]], sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    return execute(sql, params)


def _norm_sql(sql: Optional[str]) -> str:
    return _WS.sub(" ", (sql or "").strip())


def schema_signature(execute: Callable[[str, tuple], List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Normalised definitions of every user object: sqlite_master SQL per
    (type, name) and PRAGMA table_info per table (name, type, notnull,
    default, pk). A right-named object with a wrong definition differs."""
    objs = _query(execute, "SELECT type, name, tbl_name, sql FROM sqlite_master "
                           "WHERE sql IS NOT NULL ORDER BY type, name")
    definitions: Dict[str, str] = {}
    tables: List[str] = []
    for o in objs:
        if not _user_table(o["name"]) or not _user_table(o["tbl_name"]):
            continue
        definitions[f"{o['type']}:{o['name']}"] = _norm_sql(o["sql"])
        if o["type"] == "table":
            tables.append(o["name"])
    columns: Dict[str, List[List[Any]]] = {}
    for t in tables:
        cols = _query(execute, "SELECT name, type, \"notnull\" AS nn, dflt_value, pk FROM pragma_table_info(?) "
                               "ORDER BY cid", (t,))
        columns[t] = [[c["name"], c["type"], c["nn"], c["dflt_value"], c["pk"]] for c in cols]
    return {"definitions": definitions, "columns": columns}


def reference_signature() -> Dict[str, Any]:
    """What `ensure_schema` builds over the D1 branches, on a scratch database."""
    from . import schema

    conn = ScratchD1Connection()
    try:
        schema.ensure_schema(conn)
        return schema_signature(lambda sql, params: [dict(r) for r in conn._sql.execute(sql, params).fetchall()])
    finally:
        conn.close()


def diff_signature(live: Dict[str, Any], want: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in sorted(set(live["definitions"]) | set(want["definitions"])):
        a, b = live["definitions"].get(key), want["definitions"].get(key)
        if a is None:
            out.append(f"missing {key}")
        elif b is None:
            out.append(f"unexpected {key}")
        elif a != b:
            out.append(f"definition differs {key}")
    for t in sorted(set(live["columns"]) | set(want["columns"])):
        if live["columns"].get(t) != want["columns"].get(t):
            out.append(f"columns differ {t}")
    return out


# ------------------------------------------------------------------ durable state

def bootstrap_dir(data_dir: Optional[Path] = None) -> Path:
    from .write_gate import data_dir as _data_dir

    return (Path(data_dir) if data_dir is not None else _data_dir()) / "bootstrap"


def _write_json_atomic(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_state(db: str, data_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = bootstrap_dir(data_dir) / f"{db}.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except ValueError as exc:
        raise AddStoreRefused(f"{path} is not valid JSON ({exc}); inspect it by hand, nothing was changed")
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise AddStoreRefused(f"{path} has an unknown format; inspect it by hand, nothing was changed")
    return state


def _save_state(db: str, state: Dict[str, Any], data_dir: Optional[Path]) -> None:
    state = dict(state, version=STATE_VERSION, db=db, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _write_json_atomic(bootstrap_dir(data_dir) / f"{db}.json", state)


# ------------------------------------------------------------------ D1 identity (REST)

def fetch_d1_info(account_id: str, database_id: str, token: str, *, timeout: float = 30.0,
                  opener: Callable[..., Any] = urllib.request.urlopen) -> Dict[str, Any]:
    """The database's {uuid, name, read_replication_mode} from Cloudflare's REST API (read token).

    The mode uses ``shadow.replication_mode_of`` (the same parser as a d1:// primary). A network
    or permission failure is ``AddStoreRefused``; a missing mode is ``"unknown"`` and the identity
    check then refuses."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise AddStoreRefused(f"Cloudflare answered HTTP {exc.code} for the pinned database: the read token "
                              "cannot see it (is the token's account scope right?); nothing was changed")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AddStoreRefused(f"cannot read the pinned database's identity: {exc}; nothing was changed")
    from .shadow import replication_mode_of

    result = body.get("result") or {}
    return {"uuid": result.get("uuid"), "name": result.get("name"),
            "read_replication_mode": replication_mode_of(body)}


# ------------------------------------------------------------------ bootstrap

@dataclass
class BootstrapDeps:
    """Everything bootstrap touches, injectable for tests."""
    info: Callable[[], Dict[str, Any]]                                   # pinned db's {uuid, name} (read token)
    read: Callable[[str, tuple], List[Dict[str, Any]]]                   # SELECT-only (read token)
    open_writer: Callable[[], D1Connection]                              # guarded connection (edit credential)
    configured_ids: Set[str] = field(default_factory=set)               # ids in MEMORA_DATABASES / MEMORA_REPLICAS
    configured_names: Set[str] = field(default_factory=set)             # store names in the registry / replicas
    data_dir: Optional[Path] = None
    log: Callable[[str], None] = lambda msg: None


def configured_from_env(env: Optional[Dict[str, str]] = None) -> Tuple[Set[str], Set[str]]:
    """(database ids, store names) memora-all's own MEMORA_DATABASES and
    MEMORA_REPLICAS name. A malformed value refuses (the check cannot be made)."""
    env = os.environ if env is None else env
    ids: Set[str] = set()
    names: Set[str] = set()
    for key in ("MEMORA_DATABASES", "MEMORA_REPLICAS"):
        raw = (env.get(key) or "").strip()
        if not raw:
            continue
        try:
            mapping = json.loads(raw)
        except ValueError as exc:
            raise AddStoreRefused(f"{key} is not JSON ({exc}); the configured-ids check cannot be made")
        if not isinstance(mapping, dict):
            raise AddStoreRefused(f"{key} is not a JSON object; the configured-ids check cannot be made")
        for name, uri in mapping.items():
            names.add(str(name))
            m = re.fullmatch(r"d1://([^/\s]+)/([^/\s]+)", str(uri))
            if m:
                ids.add(m.group(2))
    return ids, names


def _user_objects(read: Callable[[str, tuple], List[Dict[str, Any]]]) -> List[str]:
    rows = read("SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "AND name NOT LIKE '_cf_%' ORDER BY type, name", ())
    return [f"{r['type']}:{r['name']}" for r in rows if _user_table(r["tbl_name"])]


def _business_counts(read: Callable[[str, tuple], List[Dict[str, Any]]]) -> Dict[str, int]:
    tables = [r["name"] for r in read("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name", ())
              if _user_table(r["name"]) and r["name"] != META_TABLE]
    return {t: int(read(f'SELECT COUNT(*) AS n FROM "{t}"', ())[0]["n"]) for t in tables}


def _verify_initialised(deps: BootstrapDeps, *, want: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Schema identity (backend-aware), memories_meta == only the epoch row
    '0', every business table empty. Raises AddStoreRefused on any mismatch."""
    want = want if want is not None else reference_signature()
    diffs = diff_signature(schema_signature(deps.read), want)
    if diffs:
        raise AddStoreRefused("the database's schema is not what the app's initializer builds: "
                              + "; ".join(diffs[:10]) + " -- inspect it by hand; no automatic replay")
    meta = deps.read(f"SELECT key, value FROM {META_TABLE} ORDER BY key", ())
    if [(r["key"], str(r["value"])) for r in meta] != [(EPOCH_KEY, "0")]:
        raise AddStoreRefused(f"{META_TABLE} is not exactly the epoch row '0': {meta!r}")
    counts = _business_counts(deps.read)
    full = {t: n for t, n in counts.items() if n}
    if full:
        raise AddStoreRefused(f"business tables are not empty: {full}")
    return {"tables": sorted(counts), "meta": [EPOCH_KEY]}


def _refuse_read_replication(info: Dict[str, Any], database_id: str) -> None:
    """Refuse a primary whose mode (already parsed by ``fetch_d1_info``) is not disabled.

    Missing or empty is ``unknown``, which refuses. The override skips the refuse and warns.
    Called only after the uuid and name match, so a wrong database stays an identity error.
    """
    if read_replication_allowed():
        warn_read_replication_override(database_id)
        return
    mode = info.get("read_replication_mode")
    if not isinstance(mode, str) or mode == "":
        mode = "unknown"
    try:
        require_read_replication_disabled(database_id, mode)
    except ReadReplicationRefused as exc:
        raise AddStoreRefused(f"{exc}; nothing was changed") from exc


def _check_identity(deps: BootstrapDeps, database_id: str, d1_name: str, where: str) -> None:
    info = deps.info()
    if info.get("uuid") != database_id or info.get("name") != d1_name:
        raise AddStoreRefused(f"the database at the pinned id is {info.get('name')!r}/{info.get('uuid')!r}, not the "
                              f"pinned {d1_name!r}/{database_id!r} {where}; nothing was changed")
    _refuse_read_replication(info, database_id)


def bootstrap_d1(db: str, *, account_id: str, database_id: str, d1_name: str, deps: BootstrapDeps) -> Dict[str, Any]:
    """See the module docstring. Returns a summary; raises AddStoreRefused
    (nothing changed beyond what the durable state records)."""
    if not DB_NAME_RE.fullmatch(db or ""):
        raise AddStoreRefused(f"store name {db!r} must match {DB_NAME_RE.pattern}")
    if not ID_RE.fullmatch(account_id or "") or not ID_RE.fullmatch(database_id or ""):
        raise AddStoreRefused("--account and --database-id must be the Cloudflare ids (letters, digits, '-', '_')")
    if not d1_name:
        raise AddStoreRefused("--d1-name (the database name from `wrangler d1 create`) is required")
    if database_id in deps.configured_ids:
        raise AddStoreRefused("the pinned database id is already configured in MEMORA_DATABASES or MEMORA_REPLICAS: "
                              "this command only initialises a database no store uses; nothing was changed")
    if db in deps.configured_names:
        raise AddStoreRefused(f"store {db!r} is already in MEMORA_DATABASES or MEMORA_REPLICAS; nothing was changed")

    pinned = {"account_id": account_id, "database_id": database_id, "d1_name": d1_name}
    state = read_state(db, deps.data_dir)
    if state is not None and any(state.get(k) != v for k, v in pinned.items()):
        raise AddStoreRefused(f"the bootstrap state for {db!r} was written for other ids; inspect "
                              f"{bootstrap_dir(deps.data_dir) / (db + '.json')} by hand; nothing was changed")

    _check_identity(deps, database_id, d1_name, "(identity check)")
    try:
        objects = _user_objects(deps.read)
    except Exception as exc:  # the read token cannot read the new database
        raise AddStoreRefused(f"the read token cannot read the pinned database: {exc}; nothing was changed")

    if objects:
        # Not empty. Only a COMPLETE, matching initialisation by THIS command is accepted: there must be a
        # durable state for the same pins (a resume after the state write was lost, or after the verification
        # was not reached); with no state at all the database is not known to be ours: a manual recovery
        # decision, never an automatic adoption. A partial or different schema is never replayed.
        if state is None:
            raise AddStoreRefused("the database already holds objects but there is no bootstrap state for it: it is "
                                  "not known to have been initialised by this command (a lost state file is a "
                                  "manual recovery decision: inspect the database, then restore or recreate the "
                                  f"state by hand); nothing was changed")
        deps.log("the database already holds objects: verifying it instead of initialising")
        summary = _verify_initialised(deps)
        _save_state(db, dict(pinned, phase="verified", verified_without_ddl=True), deps.data_dir)
        return {"status": "already-initialised", **summary, **pinned_view(pinned)}

    # --- the LAST pre-init gate: identity again, zero user objects again.
    _check_identity(deps, database_id, d1_name, "(immediately before the DDL)")
    if _user_objects(deps.read):
        raise AddStoreRefused("the database changed between the emptiness check and the DDL; nothing was changed")
    _save_state(db, dict(pinned, phase="ddl_started"), deps.data_dir)

    from . import schema

    conn = deps.open_writer()
    try:
        schema.ensure_schema(conn)
        pending = schema.schema_pending(conn)
    finally:
        conn.close()
    if pending:
        raise AddStoreRefused(f"after the initializer the schema is still pending: {pending[:8]}")
    _save_state(db, dict(pinned, phase="ddl_done"), deps.data_dir)

    summary = _verify_initialised(deps)
    _check_identity(deps, database_id, d1_name, "(after the DDL)")
    _save_state(db, dict(pinned, phase="verified"), deps.data_dir)
    return {"status": "initialised", **summary, **pinned_view(pinned)}


def pinned_view(pinned: Dict[str, str]) -> Dict[str, Any]:
    return {"d1_name": pinned["d1_name"], "database_id": pinned["database_id"]}


# ------------------------------------------------------------------ persisted freeze

def pre_freeze(db: str, *, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Write /data/freeze/<db> for a store that is bootstrapped and not yet
    served. Idempotent."""
    from .write_gate import freeze_file, persist_freeze

    if not DB_NAME_RE.fullmatch(db or ""):
        raise AddStoreRefused(f"store name {db!r} must match {DB_NAME_RE.pattern}")
    state = read_state(db, data_dir)
    if state is None or state.get("phase") != "verified":
        raise AddStoreRefused(f"{db!r} has no verified bootstrap state: run bootstrap-d1 first; nothing was changed")
    existed = freeze_file(db).exists()
    persist_freeze(db)
    return {"freeze_file": str(freeze_file(db)), "already_present": existed}


def clear_freeze(db: str, *, routes: Dict[str, str]) -> Dict[str, Any]:
    """Remove the persisted freeze of a store the running service no longer
    routes (rollback of an interim store). `routes` is the service's own
    registry, as it runs NOW."""
    from .write_gate import freeze_file, remove_freeze

    if not DB_NAME_RE.fullmatch(db or ""):
        raise AddStoreRefused(f"store name {db!r} must match {DB_NAME_RE.pattern}")
    if db in routes:
        raise AddStoreRefused(f"the running service still routes {db!r}: redeploy without it and verify the route "
                              "is gone first; the freeze stays; nothing was changed")
    existed = freeze_file(db).exists()
    remove_freeze(db)
    return {"removed": existed}


# ------------------------------------------------------------------ preconditions for the resumable steps

def status(db: str, *, account_id: str, database_id: str, d1_name: str, require_freeze: bool = True,
           data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Read-only. The preconditions every resumable step after the bootstrap needs, proven BEFORE any
    configuration edit or deploy: a verified bootstrap state for exactly the pinned account/id/name, and
    (by default) the persisted freeze file. Refuses otherwise; initialises and freezes nothing."""
    from .write_gate import freeze_file

    if not DB_NAME_RE.fullmatch(db or ""):
        raise AddStoreRefused(f"store name {db!r} must match {DB_NAME_RE.pattern}")
    state = read_state(db, data_dir)
    if state is None:
        raise AddStoreRefused(f"no bootstrap state for {db!r}: run the bootstrap step first (or this is the wrong "
                              "container/volume); nothing was changed")
    pinned = {"account_id": account_id, "database_id": database_id, "d1_name": d1_name}
    diff = sorted(k for k, v in pinned.items() if state.get(k) != v)
    if diff:
        raise AddStoreRefused(f"the bootstrap state of {db!r} was written for other {', '.join(diff)} than the ones "
                              "given: refusing to continue with a different database; nothing was changed")
    if state.get("phase") != "verified":
        raise AddStoreRefused(f"the bootstrap state of {db!r} is {state.get('phase')!r}, not verified: rerun the "
                              "bootstrap step; nothing was changed")
    frozen = freeze_file(db).exists()
    if require_freeze and not frozen:
        raise AddStoreRefused(f"the persisted freeze of {db!r} is missing: run the pre-freeze step first; nothing was changed")
    return {"phase": "verified", "freeze_file": frozen, **pinned_view(pinned)}


# ------------------------------------------------------------------ first real replicated write: verification

def _row_digest(row: Dict[str, Any]) -> str:
    import hashlib

    parts = [row.get(k) for k in ("id", "content", "metadata", "tags", "created_at", "updated_at")]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def verify_replication(db: str, *, interval_s: float, expect_min_rows: int, timeout_s: float,
                       health: Callable[[], Dict[str, Any]], local_rows: Callable[[], List[Dict[str, Any]]],
                       d1_rows: Callable[[Sequence[int]], Dict[int, Dict[str, Any]]], d1_count: Callable[[], int],
                       transactional: bool, poll_s: float = 3.0, sleep: Callable[[float], None] = time.sleep,
                       monotonic: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """The first REAL replicated write of a cut-over store, proven end to end (the empty-store barrier compare
    before the thaw is vacuous for the replication path). Call after the thaw and after at least one write
    (the copy). Checks, each recorded: the store is a local transactional primary; the replicator is in write
    mode at the configured interval, running and not halted; the store is thawed; at least `expect_min_rows`
    rows exist; the outbox drains (last_acked_seq reaches head_seq, lag 0, head > 0) within `timeout_s`
    (2 x interval + 30 s); and EVERY local row is present in D1 with identical columns (read through the READ
    token) and the D1 row count equals the local count. Never writes anything. Returns {ok, checks, ...}."""
    checks: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, detail: Any = None) -> bool:
        checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail is not None else {})})
        return bool(ok)

    h = health()
    rep = h.get("replication") if isinstance(h.get("replication"), dict) else {}
    fr = h.get("freeze") or {}
    record("health ok", h.get("http") == 200 and h.get("status") == "ok", {"http": h.get("http"), "status": h.get("status")})
    record("writes are transactional (a local SQLite primary)", transactional)
    record("served from the local file", str(h.get("registry_entry")) in (f"/data/{db}.db", f"file:///data/{db}.db"),
           h.get("registry_entry"))
    record("thawed", fr.get("state") == "open", fr.get("state"))
    record("replication mode write", rep.get("mode") == "write", rep.get("mode"))
    record("replication interval", rep.get("interval_s") == float(interval_s), rep.get("interval_s"))
    record("replicator running, not halted", rep.get("status") == "running" and not rep.get("halted_reason"),
           {"status": rep.get("status"), "halted_reason": rep.get("halted_reason")})
    if not all(c["ok"] for c in checks):
        return {"ok": False, "checks": checks, "stopped": "preconditions"}

    start = monotonic()
    while True:
        rep = (health().get("replication") or {})
        drained = (rep.get("head_seq") or 0) > 0 and rep.get("last_acked_seq") == rep.get("head_seq") \
            and rep.get("lag_rows") == 0 and rep.get("status") == "running" and not rep.get("halted_reason")
        if drained or monotonic() - start >= timeout_s:
            break
        sleep(poll_s)
    waited = round(monotonic() - start, 1)
    record("a write exists and the outbox drained within the bound", drained,
           {"waited_s": waited, "timeout_s": timeout_s, "head_seq": rep.get("head_seq"),
            "last_acked_seq": rep.get("last_acked_seq"), "lag_rows": rep.get("lag_rows"),
            "status": rep.get("status"), "halted_reason": rep.get("halted_reason"), "last_error": rep.get("last_error")})
    if not drained:
        return {"ok": False, "checks": checks, "stopped": "ack did not reach head in time"}

    local = {int(r["id"]): r for r in local_rows()}
    record(f"at least {expect_min_rows} local rows", len(local) >= expect_min_rows, len(local))
    remote = d1_rows(sorted(local))
    missing = sorted(i for i in local if i not in remote)
    differ = sorted(i for i in local if i in remote and _row_digest(local[i]) != _row_digest(remote[i]))
    record("every local row is in D1 (read back through the read token)", not missing, missing[:20])
    record("every row's columns are identical in D1", not differ, differ[:20])
    n_remote = d1_count()
    record("D1 row count equals the local count", n_remote == len(local), {"d1": n_remote, "local": len(local)})
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "rows": len(local), "waited_s": waited}
