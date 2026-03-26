"""Database schema management and connection helpers."""
from __future__ import annotations

import sqlite3

from .backends import D1Connection


def connect(storage_backend, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Create a database connection using the given storage backend.

    For cloud backends, this will automatically sync from cloud before use.
    """
    conn = storage_backend.connect(check_same_thread=check_same_thread)
    # Enable foreign key enforcement for local SQLite
    if not isinstance(conn, D1Connection):
        conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    if storage_backend.supports_sessions:
        _ensure_session_tables(conn)
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
    _ensure_crossrefs_table(conn)
    _ensure_events_table(conn)
    _ensure_actions_table(conn)
    _ensure_importance_columns(conn)
    _ensure_updated_at_column(conn)
    _ensure_session_columns(conn)


def _ensure_fts(conn: sqlite3.Connection) -> None:
    if isinstance(conn, D1Connection):
        return
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not table_exists:
        conn.execute(
            """
            CREATE VIRTUAL TABLE memories_fts
            USING fts5(content, metadata, tags)
            """
        )
        conn.commit()


def _ensure_embeddings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories_embeddings (
            memory_id INTEGER PRIMARY KEY,
            embedding TEXT,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )
        """
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

    if "importance" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 1.0")

    if "last_accessed" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN last_accessed TEXT")

    if "access_count" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0")

    conn.commit()


def _ensure_updated_at_column(conn: sqlite3.Connection) -> None:
    """Add updated_at column to memories table if it doesn't exist."""
    cursor = conn.execute("PRAGMA table_info(memories)")
    columns = {row[1] for row in cursor.fetchall()}

    if "updated_at" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN updated_at TEXT")
        conn.commit()


def _ensure_session_columns(conn: sqlite3.Connection) -> None:
    """Add session-related columns to memories table if they don't exist."""
    cursor = conn.execute("PRAGMA table_info(memories)")
    columns = {row[1] for row in cursor.fetchall()}

    for col, definition in [
        ("repo_identity", "TEXT"),
        ("session_id", "TEXT"),
        ("branch", "TEXT"),
        ("head_commit", "TEXT"),
        ("memory_kind", "TEXT"),
    ]:
        if col not in columns:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {definition}")

    conn.commit()

    # Create indexes if they don't exist
    for idx_name, idx_def in [
        ("idx_memories_repo", "memories(repo_identity)"),
        ("idx_memories_kind", "memories(memory_kind)"),
        ("idx_memories_session", "memories(session_id)"),
        ("idx_memories_branch", "memories(repo_identity, branch)"),
    ]:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")

    conn.commit()


def _ensure_session_tables(conn: sqlite3.Connection) -> None:
    """Create session memory tables (local SQLite only)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            claude_session_id TEXT,
            transcript_path TEXT,
            transcript_start_line INTEGER,
            transcript_end_line INTEGER,
            repo_identity TEXT NOT NULL,
            pane_id TEXT,
            branch TEXT,
            workspace_id TEXT,
            state TEXT NOT NULL DEFAULT 'open',
            close_phase INTEGER DEFAULT 0,
            close_payload TEXT,
            base_snapshot_revision INTEGER,
            conflict INTEGER DEFAULT 0,
            objective TEXT,
            outcome TEXT,
            summary TEXT,
            started_at TEXT NOT NULL,
            closed_at TEXT,
            head_commit_start TEXT,
            head_commit_end TEXT,
            next_event_seq INTEGER DEFAULT 1,
            start_key TEXT UNIQUE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_deltas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            structured_facts TEXT NOT NULL,
            delta_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            UNIQUE(session_id, event_seq),
            UNIQUE(session_id, delta_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS branch_state (
            repo_identity TEXT NOT NULL,
            branch TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            snapshot_revision INTEGER NOT NULL DEFAULT 1,
            session_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (repo_identity, branch)
        )
        """
    )

    # Create indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_identity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_branch ON sessions(repo_identity, branch)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deltas_session ON session_deltas(session_id)"
    )

    conn.commit()
