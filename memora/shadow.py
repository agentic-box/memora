"""Shadow-local mode (docs/local-primary-implementation.md §2.9, §0 P4).

While a store is still served from D1 (its registry URI stays d1://), a
local SQLite shadow of it is kept in step, off the request path, so that a
week of clean nights can be proven before the cutover. D1 stays the primary:
every read and every write still go to D1, and nothing here writes D1.

MEMORA_SHADOW_LOCAL='{"<db>": "/data/shadow/<db>.db"}' names the stores in
shadow and their shadow files. A shadow file is a seeded local store
(`local_primary.py seed`, with the sync outbox installed) plus a
`shadow_state` row (`local_primary.py shadow-init`).

The hook. D1Backend.connect() returns a ShadowingD1Connection for a shadowed
store. It changes nothing the app sees: D1Connection._execute_api (the L2
write gate and intent journal) runs as it always does, and
  * after a mutating statement D1 answered successfully, still inside the
    write gate, `_after_mutation` enqueues (sql, params, D1's meta) for the
    applier when the target is one of the seven replicated tables; so a
    completed freeze also means every admitted write has been enqueued;
  * on any exception from a mutating statement it marks the shadow dirty
    and re-raises the same exception object (a refusal BEFORE sending --
    the write gate or the intent journal refusing -- reached nothing and
    is not dirty);
  * execute_batch raises.
execute, executemany and executescript are the parent's own code, so the
result and exception shapes cannot differ.

The applier. One thread, one FIFO queue. For each item:
  1. replay the statement on the shadow in one store_write; the outbox
     triggers record the touched keys;
  2. add D1's last_row_id as a key for an INSERT into memories or
     memories_actions, and the statement's own primary key when the parser
     can map it; the keys join the PENDING set.
Copy-back runs at a QUIESCENT point: no mutation of this store is in flight
through a shadowed connection and every enqueued item has been replayed, so
D1 holds exactly the state after the last replayed statement. (Copying back
after each item, as §2.9 first described, reads D1 state that LATER writes
already produced: a false written-value mismatch, or a later replay that
conflicts with the future row copy-back just installed -- the app's own
embedding DELETE+INSERT does that.) At the quiescent point:
  3. read every pending key from D1 by primary key through
     D1SelectOnlyConnection (the read token), OUTSIDE any local transaction
     (no network while store_write is held), requiring
     meta.served_by_primary and, where the last statement on the key was the
     key's own parameterised write, its written values; up to 5 attempts
     200 ms apart. If a mutation started meanwhile the reads are discarded
     and taken again at the next quiescent point;
  4. in one store_write, make each local row equal to D1's (the replicator's
     statement shapes: upsert, delete+insert for embeddings, delete), then
     re-read it: it must equal D1's row column for column, and a key absent
     on D1 must be absent locally.

Dirty rule (plan §2.9, every failure point sets shadow_state.dirty with a
reason; D1 is never touched and nothing retries into D1; a dirty shadow is
re-seeded and its clean-night count restarts):
  1  a mutating D1 request raises or times out (unknown outcome)
  2  an element of executemany/executescript raises after earlier ones
  3  enqueue fails, or the applier is not running when an item arrives
  4  the local replay raises (that item's transaction is rolled back)
  5  a copy-back read fails, stays replica-served, or shows a written-value
     mismatch after the retries
  6  after copy-back a local row differs from D1's, or a key gone on D1 is
     still present locally
  7  a statement is unknown, or a mutation's target cannot be resolved
  7b a DDL statement succeeds through the shadowed connection
  8  the applier thread dies
  9  a start finds shadow_state.clean_shutdown = 0 (a restart or crash with
     items possibly still queued: the queue is in memory)
  10 a D1 write not made through the wrapper: invisible per write; the
     nightly compare finds it (and the writer freeze prevents it)

The applier refuses to start -- and every write then marks the shadow dirty
(row 3) -- unless MEMORA_D1_READ_TOKEN is set and the D1 database's read
replication mode is "disabled" (a replica could serve a stale copy-back).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .backends import D1Connection, LocalSQLiteBackend, store_write
from .intent_journal import IntentJournalError
from .schema import SYNC_META_EXCLUDED, SYNC_TABLES
from .sql_classify import DDL, MUTATION, READ, TXN, classify_statement, derive_effect
from .write_gate import StoreReadOnlyError

logger = logging.getLogger(__name__)

SHADOW_ENV = "MEMORA_SHADOW_LOCAL"
COPY_BACK_RETRIES = 5
COPY_BACK_SLEEP_S = 0.2
LAST_ROW_ID_TABLES = ("memories", "memories_actions")


class ShadowConfigError(RuntimeError):
    """MEMORA_SHADOW_LOCAL is unusable. Raised at startup (exit 2)."""


class ShadowDirty(Exception):
    """One item could not be mirrored exactly. str() is the dirty reason."""


def shadow_config() -> Dict[str, str]:
    """{store name: shadow file path} from MEMORA_SHADOW_LOCAL ({} when unset)."""
    raw = os.getenv(SHADOW_ENV, "").strip()
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise ShadowConfigError(f"{SHADOW_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in cfg.items()):
        raise ShadowConfigError(f"{SHADOW_ENV} must be a JSON object of store name -> shadow file path")
    return cfg


def _norm(v: Any) -> Any:
    """One value compared across D1 JSON and SQLite (D1 may return 1 for 1.0,
    and a BLOB as a list of byte values)."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (bytes, bytearray)):
        return {"$hex": bytes(v).hex()}
    if isinstance(v, list):
        return {"$hex": bytes(v).hex()}
    return v


def rows_equal(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return set(a) == set(b) and all(_norm(a[k]) == _norm(b[k]) for k in a)


@dataclass
class ShadowItem:
    sql: str
    params: Tuple[Any, ...]
    meta: Dict[str, Any]
    target: str
    main: str


# ------------------------------------------------------------------ the hook

class ShadowingD1Connection(D1Connection):
    """A D1Connection whose successful writes are mirrored into the store's
    shadow file (module docstring). The app-visible behaviour is the
    parent's: _execute_api is wrapped, never re-implemented."""

    def _applier(self) -> "ShadowApplier":
        return applier_for(getattr(self._backend, "store_name", None) or self.database_id)

    def execute_batch(self, statements) -> list:
        raise StoreReadOnlyError("execute_batch is not available on a shadowed connection")

    def _execute_api(self, sql: str, params: tuple = None) -> dict:
        try:
            mutating = classify_statement(sql).kind != READ
        except Exception:
            mutating = True
        applier = None
        if mutating:
            try:
                applier = self._applier()
                applier.begin_mutation()
            except BaseException:
                logger.exception("shadow: begin_mutation failed")
                applier = None
        try:
            return super()._execute_api(sql, params)
        except (StoreReadOnlyError, IntentJournalError):
            # Refused before anything was sent: the gate (frozen, read-only)
            # or the intent journal. D1 is unchanged, so is the shadow.
            raise
        except BaseException as exc:
            try:
                if classify_statement(sql).kind != READ:
                    self._applier().mark_dirty(
                        f"row 1/2: a mutating D1 request raised {type(exc).__name__}: {str(exc)[:160]}")
            except BaseException:  # never mask the app's exception
                logger.exception("shadow: could not mark the shadow dirty")
            raise
        finally:
            if applier is not None:
                applier.end_mutation()

    def _after_mutation(self, classified, sql: str, params, result: dict) -> None:
        try:
            applier = self._applier()
        except BaseException:
            logger.exception("shadow: no applier")
            return
        try:
            if classified.kind == MUTATION:
                target = (classified.target or "").lower()
                if not target:
                    applier.mark_dirty(f"row 7: the target of a mutation could not be resolved: {sql[:120]}")
                    return
                if target not in SYNC_TABLES:
                    return  # not a replicated table: ignored (plan §2.9)
                meta = ((result.get("result") or [{}])[0] or {}).get("meta") or {}
                applier.enqueue(ShadowItem(sql, tuple(params or ()), dict(meta), target, classified.main))
            elif classified.kind == DDL:
                applier.mark_dirty(f"row 7b: DDL succeeded on D1: {sql[:120]}")
            elif classified.kind == TXN:
                return  # no data effect
            else:
                applier.mark_dirty(f"row 7: unknown statement succeeded on D1: {sql[:120]}")
        except BaseException as exc:
            try:
                applier.mark_dirty(f"row 3: enqueue failed ({type(exc).__name__}: {str(exc)[:120]})")
            except BaseException:
                logger.exception("shadow: could not mark the shadow dirty")


def shadow_connection_class(store_name: Optional[str]):
    """The connection class D1Backend.connect() uses for a store."""
    if store_name and store_name in _config_for_connect():
        return ShadowingD1Connection
    return D1Connection


def _config_for_connect() -> Dict[str, str]:
    # A malformed value fails server startup (validate_shadow_config); here,
    # on the request path, it must not break plain D1 connections.
    try:
        return shadow_config()
    except ShadowConfigError:
        return {}


# ------------------------------------------------------------------ the applier

class ShadowApplier:
    """One thread and a FIFO queue per shadowed store (module docstring)."""

    def __init__(self, name: str, path: str, reader=None, *, retries: int = COPY_BACK_RETRIES,
                 retry_sleep: float = COPY_BACK_SLEEP_S, sleep: Callable[[float], None] = time.sleep):
        self.name = name
        self.path = Path(path)
        self.backend = LocalSQLiteBackend(self.path)  # built from MEMORA_SHADOW_LOCAL: no registry gate
        self.reader = reader
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.sleep = sleep
        self.queue: "queue.Queue[ShadowItem]" = queue.Queue()
        self.alive = False
        self.refused: Optional[str] = None
        self.dirty_reason: Optional[str] = None
        self._died = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Mutations in flight through shadowed connections, and a generation
        # bumped by each start: a copy-back is valid only if no mutation
        # started while it read (module docstring).
        self._inflight = 0
        self._generation = 0
        # Keys replayed but not yet copied back, with the written values
        # expected where the last statement on the key was its own write.
        self._pending_keys: Dict[Tuple[str, tuple], Optional[Dict[str, Any]]] = {}

    # ---------------------------------------------------------------- state

    def _state(self) -> Optional[Dict[str, Any]]:
        conn = self.backend.connect()
        try:
            row = conn.execute("SELECT * FROM shadow_state WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _update_state(self, sql: str, params: tuple = ()) -> None:
        conn = self.backend.connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def mark_dirty(self, reason: str) -> None:
        """Record the FIRST reason in shadow_state (the clean-night count
        restarts). Never raises: the D1 path must not notice."""
        with self._lock:
            first = self.dirty_reason is None
            if first:
                self.dirty_reason = reason
        logger.error("shadow %s dirty: %s", self.name, reason)
        if not first:
            return
        try:
            self._update_state(
                "UPDATE shadow_state SET dirty = 1, dirty_reason = COALESCE(dirty_reason, ?), "
                "dirty_at = COALESCE(dirty_at, ?), clean_nights = 0 WHERE id = 1",
                (reason, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        except Exception:
            logger.exception("shadow %s: could not persist the dirty flag (kept in memory)", self.name)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self.reader is None:
            self.refused = self.refused or "no D1 reader"
            return
        state = self._state()
        if state is None:
            self.refused = "no shadow_state row: run `local_primary.py shadow-init`"
            return
        if int(state.get("dirty") or 0):
            self.dirty_reason = state.get("dirty_reason") or "dirty"
        if not int(state.get("clean_shutdown") or 0):
            self.mark_dirty("row 9: the previous run did not shut down cleanly (clean_shutdown = 0); "
                            "queued items may have been lost")
        self._update_state("UPDATE shadow_state SET clean_shutdown = 0 WHERE id = 1")
        self._stop.clear()
        self.alive = True
        self._thread = threading.Thread(target=self._run, name=f"memora-shadow-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> bool:
        """Drain and stop. clean_shutdown = 1 only when the queue drained and
        the thread ended by itself."""
        thread = self._thread
        if thread is None:
            return False
        self._stop.set()
        thread.join(timeout)
        drained = (not thread.is_alive() and not self._died and self.queue.empty()
                   and not self._pending_keys)
        if drained:
            try:
                self._update_state("UPDATE shadow_state SET clean_shutdown = 1 WHERE id = 1")
            except Exception:
                logger.exception("shadow %s: could not record the clean shutdown", self.name)
                drained = False
        return drained

    def begin_mutation(self) -> None:
        with self._lock:
            self._inflight += 1
            self._generation += 1

    def end_mutation(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def _quiet(self) -> Tuple[bool, int]:
        with self._lock:
            return self._inflight == 0 and self.queue.unfinished_tasks == 0, self._generation

    def enqueue(self, item: ShadowItem) -> None:
        if not self.alive:
            why = self.refused or ("it died" if self._died else "it was not started")
            self.mark_dirty(f"row 3: a write arrived while the applier was not running ({why})")
            return
        self.queue.put_nowait(item)

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"enabled": True, "queue_depth": self.queue.qsize(), "applier_alive": self.alive,
                               "pending_keys": len(self._pending_keys),
                               "dirty": self.dirty_reason is not None, "dirty_reason": self.dirty_reason}
        if self.refused:
            out["refused"] = self.refused
        try:
            st = self._state() or {}
            out["dirty"] = bool(st.get("dirty")) or out["dirty"]
            out["dirty_reason"] = st.get("dirty_reason") or out["dirty_reason"]
            out["clean_nights"] = int(st.get("clean_nights") or 0)
            out["last_clean_night"] = st.get("last_clean_night")
        except Exception as exc:
            out["state_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return out

    # ---------------------------------------------------------------- the loop

    def _run(self) -> None:
        conn = None
        try:
            conn = self.backend.connect()
            while True:
                if self._stop.is_set() and self.queue.empty():
                    # A last copy-back when the store is quiet; clean_shutdown
                    # needs the pending keys settled (stop()).
                    self._copy_back_if_quiet(conn)
                    break
                try:
                    item = self.queue.get(timeout=0.05)
                except queue.Empty:
                    self._copy_back_if_quiet(conn)
                    continue
                try:
                    self._apply(conn, item)
                except ShadowDirty as exc:
                    self.mark_dirty(str(exc))
                except Exception as exc:
                    self.mark_dirty(f"row 4: applying {item.main} on {item.target} failed "
                                    f"({type(exc).__name__}: {str(exc)[:160]})")
                finally:
                    self.queue.task_done()
                self._copy_back_if_quiet(conn)
        except BaseException as exc:
            self._died = True
            self.mark_dirty(f"row 8: the applier thread died ({type(exc).__name__}: {str(exc)[:160]})")
        finally:
            self.alive = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.exception("shadow %s: close failed", self.name)

    def _apply(self, conn, item: ShadowItem) -> None:
        # 1. replay (row 4: raises inside store_write -> rolled back)
        try:
            with store_write(conn):
                before = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_outbox").fetchone()[0]
                conn.execute(item.sql, item.params)
                touched = {(r[0], tuple(json.loads(r[1])))
                           for r in conn.execute("SELECT tbl, pk FROM sync_outbox WHERE seq > ?", (before,))}
        except Exception as exc:
            raise ShadowDirty(f"row 4: the local replay of {item.main} on {item.target} failed and was rolled "
                              f"back ({type(exc).__name__}: {str(exc)[:160]})") from exc
        # 2. the statement's own key(s), and the written values to expect
        _target, keys, post = derive_effect(item.sql, item.params)
        direct: List[Tuple[str, tuple]] = []
        pk_cols = SYNC_TABLES[item.target]
        if keys and all(c in keys for c in pk_cols):
            direct.append((item.target, tuple(keys[c] for c in pk_cols)))
        rid = item.meta.get("last_row_id")
        if item.main in ("INSERT", "REPLACE") and item.target in LAST_ROW_ID_TABLES and rid:
            direct.append((item.target, (int(rid),)))
        with self._lock:
            for key in touched:
                self._pending_keys[key] = None  # a later statement on a key voids its expectation
            for key in direct:
                self._pending_keys[key] = dict(post) if post else None
            for key in list(self._pending_keys):
                if key[0] == "memories_meta" and key[1][0] in SYNC_META_EXCLUDED:
                    del self._pending_keys[key]

    def _copy_back_if_quiet(self, conn) -> None:
        """Steps 3 and 4 of the module docstring, at a quiescent point."""
        if not self._pending_keys:
            return
        quiet, generation = self._quiet()
        if not quiet:
            return
        with self._lock:
            pending = dict(self._pending_keys)
        order = sorted(pending, key=lambda k: (k[0], [str(v) for v in k[1]]))
        d1_rows = {}
        try:
            for key in order:
                d1_rows[key] = self._read_back(key[0], key[1], pending[key])
        except ShadowDirty as exc:
            quiet, now = self._quiet()
            if not quiet or now != generation:
                return  # a mutation started while reading: read again later
            self.mark_dirty(str(exc))
            with self._lock:
                for key in order:
                    self._pending_keys.pop(key, None)
            return
        quiet, now = self._quiet()
        if not quiet or now != generation:
            return  # D1 may have moved while we read: read again at the next quiet point
        from .replicator import _build_statements

        try:
            with store_write(conn):
                columns: Dict[str, List[str]] = {}
                for (tbl, pk), row in d1_rows.items():
                    cols = columns.setdefault(tbl, [r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")')])
                    for sql, params in _build_statements(tbl, list(pk), row, cols):
                        conn.execute(sql, params)
                for (tbl, pk), row in d1_rows.items():
                    where = " AND ".join(f"{c} = ?" for c in SYNC_TABLES[tbl])
                    local = conn.execute(f"SELECT * FROM {tbl} WHERE {where}", pk).fetchone()
                    local = dict(local) if local is not None else None
                    if not rows_equal(row, local):
                        raise ShadowDirty(f"row 6: after copy-back {tbl} {list(pk)} differs from D1")
        except ShadowDirty as exc:
            self.mark_dirty(str(exc))
        except Exception as exc:
            self.mark_dirty(f"row 6: copy-back could not be applied locally ({type(exc).__name__}: {str(exc)[:160]})")
        finally:
            with self._lock:
                for key in order:
                    if self._pending_keys.get(key, "absent") == pending[key]:
                        self._pending_keys.pop(key, None)

    def _read_back(self, tbl: str, pk: tuple, expect: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        where = " AND ".join(f"{c} = ?" for c in SYNC_TABLES[tbl])
        sql = f"SELECT * FROM {tbl} WHERE {where}"
        last = "not tried"
        for attempt in range(self.retries):
            if attempt:
                self.sleep(self.retry_sleep)
            try:
                rows, meta = self.reader.execute(sql, pk)
            except Exception as exc:
                last = f"read failed ({type(exc).__name__}: {str(exc)[:120]})"
                continue
            if not (meta or {}).get("served_by_primary"):
                last = "served by a replica"
                continue
            row = dict(rows[0]) if rows else None
            if expect:
                if row is None or any(c in row and _norm(row[c]) != _norm(v) for c, v in expect.items()):
                    last = "the written values are not visible yet"
                    continue
            return row
        raise ShadowDirty(f"row 5: copy-back of {tbl} {list(pk)} failed after {self.retries} attempts: {last}")


# ------------------------------------------------------------------ registry and startup

_APPLIERS: Dict[str, ShadowApplier] = {}
_APPLIERS_LOCK = threading.Lock()


def applier_for(name: str) -> ShadowApplier:
    """The applier of a shadowed store. One that was never started (or was
    refused) still records dirtiness: every write then marks the shadow
    dirty (row 3)."""
    with _APPLIERS_LOCK:
        app = _APPLIERS.get(name)
        if app is None:
            path = _config_for_connect().get(name)
            if path is None:
                raise ShadowConfigError(f"store {name!r} is not in {SHADOW_ENV}")
            app = _APPLIERS[name] = ShadowApplier(name, path)
            app.refused = "not started"
        return app


def read_replication_mode(account_id: str, database_id: str, token: str) -> str:
    """D1's read_replication.mode for a database, from the REST API."""
    import urllib.request

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    return str(((body.get("result") or {}).get("read_replication") or {}).get("mode") or "unknown")


def start_shadow_appliers(*, reader_factory=None, replication_mode=None) -> Dict[str, Dict[str, Any]]:
    """Server startup: one applier per MEMORA_SHADOW_LOCAL entry. Each needs
    the store on d1:// in the registry, MEMORA_D1_READ_TOKEN, and read
    replication disabled; otherwise it is refused (and writes mark the
    shadow dirty)."""
    from .backends import D1SelectOnlyConnection
    from .storage import _store_refusals, database_registry

    cfg = shadow_config()
    registry = database_registry()
    out: Dict[str, Dict[str, Any]] = {}
    for name, path in cfg.items():
        with _APPLIERS_LOCK:
            app = _APPLIERS.get(name)
            if app is None or not app.alive:
                app = _APPLIERS[name] = ShadowApplier(name, path)
        if app.alive:
            out[name] = {"started": True}
            continue
        uri = registry.get(name, "")
        try:
            if name in _store_refusals:
                raise ShadowConfigError(f"store refused: {_store_refusals[name]}")
            if not uri.startswith("d1://"):
                raise ShadowConfigError(f"a shadowed store must be served from d1://, not {uri!r}")
            account, database = uri[len("d1://"):].split("/", 1)
            token = os.getenv("MEMORA_D1_READ_TOKEN", "").strip()
            if not token:
                raise ShadowConfigError("MEMORA_D1_READ_TOKEN is not set (the shadow reads D1 with it)")
            mode = (replication_mode or read_replication_mode)(account, database, token)
            if mode != "disabled":
                raise ShadowConfigError(f"D1 read replication is {mode!r}; the shadow requires 'disabled'")
            app.reader = (reader_factory or D1SelectOnlyConnection)(account, database, token)
            app.refused = None
            app.start()
        except Exception as exc:
            app.refused = f"{type(exc).__name__}: {exc}"
            logger.error("shadow applier for %s not started: %s", name, app.refused)
        out[name] = {"started": app.alive, **({"refused": app.refused} if app.refused else {})}
    return out


def stop_shadow_appliers(timeout: float = 30.0) -> Dict[str, bool]:
    with _APPLIERS_LOCK:
        apps = list(_APPLIERS.values())
    return {a.name: a.stop(timeout) for a in apps}


def shadow_status(name: str) -> Optional[Dict[str, Any]]:
    """The /health/db/<name> shadow block, or None when not shadowed."""
    if name not in _config_for_connect():
        return None
    return applier_for(name).status()


def _reset_for_tests() -> None:
    stop_shadow_appliers(timeout=5.0)
    with _APPLIERS_LOCK:
        _APPLIERS.clear()


def init_shadow_file(path: str) -> Dict[str, Any]:
    """`local_primary.py shadow-init`: make a SEEDED store a shadow file --
    shadow_state installed, clean, and marked cleanly shut down so the first
    applier start does not mark it dirty. Refuses a file without the sync
    outbox (the seed installs it)."""
    from . import schema

    backend = LocalSQLiteBackend(Path(path))
    if not Path(path).is_file():
        raise ShadowConfigError(f"{path} does not exist: seed it first (`local_primary.py seed`)")
    conn = backend.connect()
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sync_state'").fetchone() is None:
            raise ShadowConfigError(f"{path} has no sync outbox: it was not seeded by `local_primary.py seed`")
        schema.install_shadow_state(conn)
        conn.execute("UPDATE shadow_state SET dirty = 0, dirty_reason = NULL, dirty_at = NULL, "
                     "clean_shutdown = 1, clean_nights = 0, last_clean_night = NULL WHERE id = 1")
        conn.commit()
        return dict(conn.execute("SELECT * FROM shadow_state WHERE id = 1").fetchone())
    finally:
        conn.close()
