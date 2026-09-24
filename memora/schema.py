"""Database schema management and connection helpers."""
from __future__ import annotations

import sqlite3
import threading
import weakref

from .backends import D1Connection

# Cache of backends whose schema has already been ensured in this process.
# We stash a *signature* on each backend instance (so the cache entry dies
# with the backend — no id() reuse hazard), with a WeakKeyDictionary fallback
# for weakref-capable backends that reject direct attribute assignment. If a
# backend supports neither (e.g. __slots__ without __weakref__), caching is
# silently disabled for that instance — ensure_schema() just runs every call,
# which is today's behavior, so this is a safe degradation.
#
# The signature for backends with a ``cache_path`` attribute (e.g.
# ``CloudSQLiteBackend``) is the underlying file's ``(st_ino, st_dev)`` pair,
# so when ``sync_before_use()`` replaces the file via ``shutil.move``/
# ``os.rename`` the cache is invalidated automatically. Normal writes (commits)
# keep the same inode, so steady-state tool calls still hit the cache.
#
# For backends without a ``cache_path`` (``D1Backend``, in-memory, bare
# ``SQLiteBackend``), the signature is the sentinel ``True`` — identity-only,
# matching pre-fix behavior.
_schema_lock = threading.Lock()
_schema_ensured_fallback: "weakref.WeakKeyDictionary[object, object]" = weakref.WeakKeyDictionary()


def _backend_schema_signature(storage_backend):
    """Return a value that changes when the underlying DB file is replaced.

    ``None`` means "can't compute a stable signature right now" — the caller
    should treat this as a cache miss and re-run ensure_schema (but not cache
    the result, since the next call would miss again).
    """
    cache_path = getattr(storage_backend, "cache_path", None)
    if cache_path is not None:
        try:
            st = cache_path.stat()
        except (OSError, AttributeError):
            return None
        return (st.st_ino, st.st_dev)
    return True


def _backend_schema_ensured(storage_backend) -> bool:
    current_sig = _backend_schema_signature(storage_backend)
    if current_sig is None:
        return False
    stored = getattr(storage_backend, "_schema_ensured", None)
    if stored is not None and stored == current_sig:
        return True
    try:
        fallback_sig = _schema_ensured_fallback.get(storage_backend)
    except TypeError:
        # Backend not weak-referenceable (e.g. __slots__ without __weakref__).
        return False
    return fallback_sig is not None and fallback_sig == current_sig


def _mark_backend_schema_ensured(storage_backend) -> None:
    sig = _backend_schema_signature(storage_backend)
    if sig is None:
        # Can't compute a signature — don't cache. Next call will re-run
        # ensure_schema, which is the safe fallback.
        return
    try:
        storage_backend._schema_ensured = sig
        return
    except (AttributeError, TypeError):
        pass
    try:
        _schema_ensured_fallback[storage_backend] = sig
    except TypeError:
        # Backend not weak-referenceable and not attribute-settable: caching
        # is disabled for this instance. ensure_schema() will re-run, matching
        # pre-cache behavior.
        pass


def connect(storage_backend, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Create a database connection using the given storage backend.

    For cloud backends, this will automatically sync from cloud before use.

    ``ensure_schema()`` is run once per backend instance per process. On D1 HTTP
    it issues 7–9 round-trips (~4–8 s); the per-call version was the single
    biggest source of tool-call latency. Cache is tied to the backend instance
    lifetime — a new backend triggers a fresh ensure_schema pass.
    """
    conn = storage_backend.connect(check_same_thread=check_same_thread)
    if getattr(conn, "read_only", False):
        # A read-only connection (its store's write journal is unavailable in
        # this process) never runs schema setup: it would be refused anyway.
        return conn
    if not _backend_schema_ensured(storage_backend):
        with _schema_lock:
            if not _backend_schema_ensured(storage_backend):
                ensure_schema(conn)
                _mark_backend_schema_ensured(storage_backend)
    return conn


def sync_to_cloud(storage_backend) -> None:
    """Sync database to cloud storage if using a cloud backend."""
    storage_backend.sync_after_write()


def get_backend_info(storage_backend) -> dict:
    """Get information about the current storage backend."""
    return storage_backend.get_info()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            metadata TEXT,
            tags TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
        """
    )
    conn.commit()
    _ensure_fts(conn)
    _ensure_embeddings_table(conn)
    _ensure_integrity_epoch_triggers(conn)
    _ensure_crossrefs_table(conn)
    _ensure_events_table(conn)
    _ensure_actions_table(conn)
    _ensure_importance_columns(conn)
    _ensure_updated_at_column(conn)
    _ensure_tombstones_table(conn)
    _ensure_absorb_inflight_table(conn)
    _ensure_import_lease_table(conn)
    _ensure_sync_outbox(conn)


def _ensure_fts(conn: sqlite3.Connection) -> None:
    if isinstance(conn, D1Connection):
        return
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not table_exists:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE memories_fts
                USING fts5(content, metadata, tags)
                """
            )
        except Exception as exc:
            # Another upgrader may have won the create race. Re-read and only
            # suppress the known concurrent-create outcome.
            if "already exists" not in str(exc).lower():
                raise
            if not conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            ).fetchone():
                raise
        conn.commit()


def _ensure_embeddings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_embeddings (
            memory_id INTEGER PRIMARY KEY,
            embedding TEXT,
            representation TEXT,
            dimension INTEGER,
            encoding_source TEXT,
            writer_token TEXT,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )
        """
    )
    for name, sql_type in (
        ("representation", "TEXT"),
        ("dimension", "INTEGER"),
        ("encoding_source", "TEXT"),
        ("writer_token", "TEXT"),
    ):
        _add_column_if_missing(conn, "memories_embeddings", name, sql_type)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_embeddings_representation "
        "ON memories_embeddings(representation, dimension)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, sql_type: str) -> None:
    """Race-safe additive migration: tolerate only a verified duplicate winner."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name in columns:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    except Exception as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        raise RuntimeError(f"concurrent migration did not add {table}.{name}")


def _ensure_integrity_epoch_triggers(conn: sqlite3.Connection) -> None:
    """DB-owned epoch advances for every writer, including D1/worker SQL."""
    conn.execute(
        "INSERT OR IGNORE INTO memories_meta(key, value) VALUES ('embedding_change_epoch', '0')"
    )
    for table in ("memories", "memories_embeddings"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_embedding_epoch_{table}_{action.lower()}
                AFTER {action} ON {table}
                BEGIN
                    UPDATE memories_meta
                       SET value = CAST(value AS INTEGER) + 1
                     WHERE key = 'embedding_change_epoch';
                END
                """
            )
    # Older/external writers update only embedding on conflict. Clear the
    # Python-owned representation facts rather than certifying a new payload
    # with stale normalized metadata.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_embedding_external_update
        AFTER UPDATE OF embedding ON memories_embeddings
        WHEN NEW.writer_token IS OLD.writer_token
        BEGIN
            UPDATE memories_embeddings
               SET representation = NULL, dimension = NULL,
                   encoding_source = 'unknown', writer_token = NULL
             WHERE memory_id = NEW.memory_id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_embedding_external_insert
        AFTER INSERT ON memories_embeddings
        WHEN NEW.writer_token IS NULL
        BEGIN
            UPDATE memories_embeddings
               SET representation = NULL, dimension = NULL,
                   encoding_source = 'unknown'
             WHERE memory_id = NEW.memory_id;
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_embedding_repairs (
            memory_id INTEGER PRIMARY KEY,
            repaired_generation TEXT NOT NULL,
            repaired_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _ensure_crossrefs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_crossrefs (
            memory_id INTEGER PRIMARY KEY,
            related TEXT,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()


def _ensure_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            tags TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            consumed INTEGER DEFAULT 0,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()


def _ensure_actions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _ensure_importance_columns(conn: sqlite3.Connection) -> None:
    """Add importance scoring columns to memories table if they don't exist."""
    _add_column_if_missing(conn, "memories", "importance", "REAL DEFAULT 1.0")
    _add_column_if_missing(conn, "memories", "last_accessed", "TEXT")
    _add_column_if_missing(conn, "memories", "access_count", "INTEGER DEFAULT 0")

    conn.commit()


def _ensure_updated_at_column(conn: sqlite3.Connection) -> None:
    """Add updated_at column to memories table if it doesn't exist."""
    _add_column_if_missing(conn, "memories", "updated_at", "TEXT")
    conn.commit()


def _ensure_tombstones_table(conn: sqlite3.Connection) -> None:
    """Tombstones survive the deleted row (no FK). Python absorb/import consult them."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tombstones (
            content_hash TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (content_hash, memory_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tombstones_hash ON tombstones(content_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tombstones_memory ON tombstones(memory_id)"
    )
    # Component-level retirement marker: one INSERT covers every member id.
    # Written FIRST (single D1 statement) so a later per-hash insert failure
    # cannot leave leftover ancestors current.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tombstone_components (
            memory_id INTEGER PRIMARY KEY,
            content_hash TEXT,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    _add_column_if_missing(conn, "tombstone_components", "content_hash", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tombstone_components_hash "
        "ON tombstone_components(content_hash)"
    )
    conn.commit()


def _ensure_absorb_inflight_table(conn: sqlite3.Connection) -> None:
    """Durable in-flight absorb records so process death can be reconciled.

    absorb_nonce lives only in the writer process today; this table is the
    boot-visible counterpart. Lease, not PID, distinguishes live work from
    a dead writer — two servers on the same database must not reap each other.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS absorb_inflight (
            nonce TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            owner TEXT,
            owned_ids TEXT,
            status TEXT NOT NULL DEFAULT 'in_flight'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_absorb_inflight_lease "
        "ON absorb_inflight(lease_until, status)"
    )
    conn.commit()


def _ensure_import_lease_table(conn: sqlite3.Connection) -> None:
    """The store's import lease (memora.storage._ImportLease): at most ONE
    row, so at most one D1 import runs on a store at a time. The row lives in
    the store's own database, so its fixed key is per store by construction.
    The import-marker sweep never touches rows of the import holding a live
    lease."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_lease (
            lease_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            started_at TEXT NOT NULL,
            lease_until TEXT NOT NULL
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------- local primary
# docs/local-primary-implementation.md §1. The outbox and its triggers exist
# only on a LOCAL store that install_sync() enabled (the seed script, L5);
# they are never created on D1, and ensure_schema only maintains them.

SYNC_TRIGGER_VERSION = 1

# table -> primary-key columns, in pk order
SYNC_TABLES = {
    "memories": ("id",),
    "memories_embeddings": ("memory_id",),
    "memories_crossrefs": ("memory_id",),
    "tombstones": ("content_hash", "memory_id"),
    "tombstone_components": ("memory_id",),
    "memories_actions": ("id",),
    "memories_meta": ("key",),
}
# memories_meta keys that are never replicated: D1's own epoch (the
# foreign-writer check depends on it), the process-local rebuild lease, and
# the integrity stamp bound to the local epoch.
SYNC_META_EXCLUDED = ("embedding_change_epoch", "embedding_rebuild_lease", "embedding_integrity")

_SYNC_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS sync_outbox (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl TEXT NOT NULL,
  op  TEXT NOT NULL CHECK (op IN ('U','D')),
  pk  TEXT NOT NULL,
  created_at REAL NOT NULL DEFAULT (julianday('now'))
)
"""
_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS sync_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  replica_uri TEXT NOT NULL,
  last_acked_seq INTEGER NOT NULL,
  log_cursor_seq INTEGER NOT NULL DEFAULT 0,
  compare_consumed_seq INTEGER NOT NULL DEFAULT 0,
  d1_epoch_expected INTEGER,
  trigger_version INTEGER NOT NULL,
  inflight_id TEXT, inflight_lo INTEGER, inflight_hi INTEGER,
  inflight_epoch_before INTEGER, inflight_at TEXT,
  halted_reason TEXT, halted_at TEXT,
  allow_deletes_attempt TEXT, epoch_unverified_batches INTEGER NOT NULL DEFAULT 0,
  last_ack_at TEXT, last_error TEXT,
  last_compare_at TEXT, last_compare_mode TEXT, last_compare_clean INTEGER,
  d1_missing_vectors INTEGER, last_compare_report TEXT, compare_runs TEXT
)
"""
# Columns added after L2's first sync_state (the replicator, L3); ensured on
# existing tables by _ensure_sync_outbox.
_SYNC_STATE_ADDED = (
    ("allow_deletes_attempt", "TEXT"),
    ("epoch_unverified_batches", "INTEGER NOT NULL DEFAULT 0"),
    ("last_ack_at", "TEXT"),
    ("last_error", "TEXT"),
    # the §5.2 compare's outcome (L6): health fields and §2.7's d1_missing_vectors
    ("last_compare_at", "TEXT"),
    ("last_compare_mode", "TEXT"),
    ("last_compare_clean", "INTEGER"),
    ("d1_missing_vectors", "INTEGER"),
    ("last_compare_report", "TEXT"),
    ("compare_runs", "TEXT"),  # {run_id: julianday start} of the compares in progress
    # Log-mode delete-guard events (L9a, leader 7699): counted, never halting.
    ("would_halt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_would_halt", "TEXT"),
)
# One row per batch the P3 delete guard WOULD have halted in log mode (it
# sends nothing to D1, so it records and keeps logging; write mode halts).
# Local only: not a replicated table, no triggers.
_SYNC_WOULD_HALT_DDL = """
CREATE TABLE IF NOT EXISTS sync_would_halt (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  tbl TEXT NOT NULL,
  deletes INTEGER NOT NULL,
  total INTEGER NOT NULL,
  threshold TEXT NOT NULL,
  attempt_id TEXT NOT NULL UNIQUE
)
"""
_SHADOW_STATE_DDL = """
CREATE TABLE IF NOT EXISTS shadow_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  dirty INTEGER NOT NULL DEFAULT 0, dirty_reason TEXT, dirty_at TEXT,
  clean_shutdown INTEGER NOT NULL DEFAULT 0,
  clean_nights INTEGER NOT NULL DEFAULT 0, last_clean_night TEXT,
  would_halt_reported_id INTEGER NOT NULL DEFAULT 0
)
"""


def sync_trigger_ddl() -> list:
    """The 28 CREATE TRIGGER statements (7 tables x insert/update/delete,
    plus one update_pk trigger per table)."""
    out = []
    excluded = ", ".join(f"'{k}'" for k in SYNC_META_EXCLUDED)
    for table, pk in SYNC_TABLES.items():
        for action, ref, op in (("insert", "NEW", "U"), ("update", "NEW", "U"), ("delete", "OLD", "D")):
            when = f"WHEN {ref}.key NOT IN ({excluded}) " if table == "memories_meta" else ""
            cols = ", ".join(f"{ref}.{c}" for c in pk)
            out.append(
                f"CREATE TRIGGER trg_sync_{table}_{action} AFTER {action.upper()} ON {table} {when}"
                f"BEGIN INSERT INTO sync_outbox(tbl, op, pk) VALUES ('{table}', '{op}', json_array({cols})); END"
            )
        changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in pk)
        if table == "memories_meta":
            changed = f"({changed}) AND OLD.key NOT IN ({excluded})"
        cols = ", ".join(f"OLD.{c}" for c in pk)
        out.append(
            f"CREATE TRIGGER trg_sync_{table}_update_pk AFTER UPDATE ON {table} WHEN {changed} "
            f"BEGIN INSERT INTO sync_outbox(tbl, op, pk) VALUES ('{table}', 'D', json_array({cols})); END"
        )
    return out


def _sync_trigger_names(conn: sqlite3.Connection) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_sync_%'"
    ).fetchall()]


def _install_sync_triggers_locked(conn: sqlite3.Connection) -> None:
    """Drop every trg_sync_* trigger and recreate the current set. The caller
    holds a BEGIN IMMEDIATE transaction."""
    for name in _sync_trigger_names(conn):
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for ddl in sync_trigger_ddl():
        conn.execute(ddl)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _ensure_sync_outbox(conn: sqlite3.Connection) -> None:
    """Maintain -- never enable -- replication (plan §1): nothing on D1,
    nothing on a store without a sync_state row; upgrade the triggers in one
    BEGIN IMMEDIATE when their version is older than SYNC_TRIGGER_VERSION."""
    if isinstance(conn, D1Connection):
        return
    if not _has_table(conn, "sync_state"):
        return
    have = {r[1] for r in conn.execute("PRAGMA table_info(sync_state)").fetchall()}
    for col, decl in _SYNC_STATE_ADDED:
        if col not in have:
            conn.execute(f"ALTER TABLE sync_state ADD COLUMN {col} {decl}")
    conn.execute(_SYNC_WOULD_HALT_DDL)
    conn.commit()
    row = conn.execute("SELECT trigger_version FROM sync_state WHERE id = 1").fetchone()
    if row is None or int(row[0]) >= SYNC_TRIGGER_VERSION:
        return
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT trigger_version FROM sync_state WHERE id = 1").fetchone()
        if row is not None and int(row[0]) < SYNC_TRIGGER_VERSION:
            _install_sync_triggers_locked(conn)
            conn.execute("UPDATE sync_state SET trigger_version = ? WHERE id = 1", (SYNC_TRIGGER_VERSION,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def install_sync(conn: sqlite3.Connection, replica_uri: str, d1_epoch) -> None:
    """Enable replication on a LOCAL store (only the seed script calls this):
    the outbox, the state row and the triggers, in one BEGIN IMMEDIATE.
    last_acked_seq starts at 0 with an empty outbox."""
    if isinstance(conn, D1Connection):
        raise ValueError("install_sync is for local stores; D1 never carries sync objects")
    if _has_table(conn, "sync_state"):
        raise ValueError("sync is already installed on this store")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_SYNC_OUTBOX_DDL)
        conn.execute(_SYNC_STATE_DDL)
        for col, decl in _SYNC_STATE_ADDED:
            if col not in {r[1] for r in conn.execute("PRAGMA table_info(sync_state)")}:
                conn.execute(f"ALTER TABLE sync_state ADD COLUMN {col} {decl}")
        conn.execute(_SYNC_WOULD_HALT_DDL)
        conn.execute(
            "INSERT INTO sync_state (id, replica_uri, last_acked_seq, d1_epoch_expected, trigger_version) "
            "VALUES (1, ?, 0, ?, ?)",
            (replica_uri, d1_epoch, SYNC_TRIGGER_VERSION),
        )
        _install_sync_triggers_locked(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def install_shadow_state(conn: sqlite3.Connection) -> None:
    """The shadow file's own state row (plan §2.9; used by L3b)."""
    if isinstance(conn, D1Connection):
        raise ValueError("shadow_state belongs to a local shadow file")
    conn.execute(_SHADOW_STATE_DDL)
    conn.execute("INSERT OR IGNORE INTO shadow_state (id) VALUES (1)")
    conn.commit()


# Note: memory_absorb stores source/confidence in metadata (not separate columns)
# to avoid schema migration and keep provenance co-located with other metadata.
