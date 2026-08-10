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
    cursor = conn.execute("PRAGMA table_info(memories)")
    columns = {row[1] for row in cursor.fetchall()}

    _add_column_if_missing(conn, "memories", "importance", "REAL DEFAULT 1.0")
    _add_column_if_missing(conn, "memories", "last_accessed", "TEXT")
    _add_column_if_missing(conn, "memories", "access_count", "INTEGER DEFAULT 0")

    conn.commit()


def _ensure_updated_at_column(conn: sqlite3.Connection) -> None:
    """Add updated_at column to memories table if it doesn't exist."""
    cursor = conn.execute("PRAGMA table_info(memories)")
    _add_column_if_missing(conn, "memories", "updated_at", "TEXT")
    conn.commit()


    # Note: memory_absorb stores source/confidence in metadata (not separate columns)
    # to avoid schema migration and keep provenance co-located with other metadata.
