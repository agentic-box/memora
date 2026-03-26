"""Tests for session memory storage functions."""

import json

import memora
import memora.storage as storage
from memora.sessions import (
    SessionError,
    branch_state_get,
    scoped_search,
    session_close,
    session_delta,
    session_get,
    session_list,
    session_start,
)


def test_session_lifecycle(local_db):
    """Full session lifecycle: start -> delta -> close."""
    with storage.connect() as conn:
        # Start session
        result = session_start(
            conn,
            repo_identity="test-repo-abc123",
            branch="main",
            head_commit="deadbeef",
            objective="fix auth bug",
        )
        assert result["state"] == "open"
        assert result["replayed"] is False
        sid = result["session_id"]

        # Add deltas
        d1 = session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["auth.py"], "action": "edit"},
            delta_id="pane1:123:0:0",
        )
        assert d1["event_seq"] == 1
        assert d1["replayed"] is False

        d2 = session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["auth.py"], "action": "test"},
            delta_id="pane1:123:0:100",
        )
        assert d2["event_seq"] == 2

        # Close session
        close_result = session_close(
            conn,
            session_id=sid,
            summary="Fixed auth bug in auth.py",
            outcome="Tests pass",
            snapshot={"active_bug": None, "open_todos": []},
            head_commit_end="cafebabe",
        )
        assert close_result["state"] == "closed"
        assert close_result["conflict"] is False


def test_session_start_idempotency(local_db):
    """start_key makes session_start replay-idempotent."""
    with storage.connect() as conn:
        result1 = session_start(
            conn,
            repo_identity="test-repo",
            branch="main",
            start_key="pane1:100:0:0",
        )
        assert result1["replayed"] is False

        result2 = session_start(
            conn,
            repo_identity="test-repo",
            branch="main",
            start_key="pane1:100:0:0",
        )
        assert result2["replayed"] is True
        assert result2["session_id"] == result1["session_id"]


def test_session_delta_idempotency(local_db):
    """delta_id makes session_delta replay-idempotent."""
    with storage.connect() as conn:
        result = session_start(conn, repo_identity="test-repo", branch="main")
        sid = result["session_id"]

        d1 = session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["a.py"]},
            delta_id="pane1:100:0:50",
        )
        assert d1["event_seq"] == 1
        assert d1["replayed"] is False

        # Replay same delta_id
        d2 = session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["a.py"]},
            delta_id="pane1:100:0:50",
        )
        assert d2["event_seq"] == 1
        assert d2["replayed"] is True


def test_session_delta_on_closed_session(local_db):
    """Appending delta to closed session raises error."""
    with storage.connect() as conn:
        result = session_start(conn, repo_identity="test-repo", branch="main")
        sid = result["session_id"]
        session_close(conn, session_id=sid)

        try:
            session_delta(
                conn,
                session_id=sid,
                structured_facts={"x": 1},
                delta_id="test-delta",
            )
            assert False, "Should have raised SessionError"
        except SessionError:
            pass


def test_session_get(local_db):
    """session_get returns session with deltas."""
    with storage.connect() as conn:
        result = session_start(
            conn,
            repo_identity="test-repo",
            branch="dev",
            objective="add feature",
        )
        sid = result["session_id"]

        session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["feature.py"]},
            delta_id="d1",
        )

        got = session_get(conn, sid)
        assert got is not None
        assert got["id"] == sid
        assert got["objective"] == "add feature"
        assert len(got["deltas"]) == 1
        assert got["deltas"][0]["structured_facts"]["files"] == ["feature.py"]


def test_session_list(local_db):
    """session_list filters by repo/branch/state."""
    with storage.connect() as conn:
        session_start(conn, repo_identity="repo-a", branch="main")
        session_start(conn, repo_identity="repo-a", branch="dev")
        s3 = session_start(conn, repo_identity="repo-b", branch="main")
        session_close(conn, session_id=s3["session_id"])

        # All for repo-a
        result = session_list(conn, repo_identity="repo-a")
        assert len(result) == 2

        # Only closed
        result = session_list(conn, state="closed")
        assert len(result) == 1
        assert result[0]["repo_identity"] == "repo-b"


def test_branch_state_cas(local_db):
    """Branch state uses CAS — concurrent close with stale revision triggers conflict."""
    with storage.connect() as conn:
        # Session 1 starts, reads base_revision=None
        s1 = session_start(conn, repo_identity="repo", branch="main")

        # Session 1 closes, creates branch_state with revision=1
        r1 = session_close(
            conn,
            session_id=s1["session_id"],
            snapshot={"truth": "from-s1"},
        )
        assert r1["conflict"] is False

        # Verify branch_state
        state = branch_state_get(conn, "repo", "main")
        assert state is not None
        assert state["snapshot"]["truth"] == "from-s1"
        assert state["snapshot_revision"] == 1

        # Session 2 starts (reads base_revision=1)
        s2 = session_start(conn, repo_identity="repo", branch="main")

        # Session 3 also starts (reads base_revision=1)
        s3 = session_start(conn, repo_identity="repo", branch="main")

        # Session 2 closes (revision 1 -> 2, succeeds)
        r2 = session_close(
            conn,
            session_id=s2["session_id"],
            snapshot={"truth": "from-s2"},
        )
        assert r2["conflict"] is False

        # Session 3 closes (expects revision 1, but it's now 2 — conflict!)
        r3 = session_close(
            conn,
            session_id=s3["session_id"],
            snapshot={"truth": "from-s3"},
        )
        assert r3["conflict"] is True

        # Branch state should still be from session 2
        state = branch_state_get(conn, "repo", "main")
        assert state["snapshot"]["truth"] == "from-s2"
        assert state["snapshot_revision"] == 2


def test_branch_state_first_write_race(local_db):
    """Two sessions starting with no branch_state both handle INSERT gracefully."""
    with storage.connect() as conn:
        s1 = session_start(conn, repo_identity="repo", branch="new-branch")
        s2 = session_start(conn, repo_identity="repo", branch="new-branch")

        # Both have base_snapshot_revision=None
        assert s1["base_snapshot_revision"] is None
        assert s2["base_snapshot_revision"] is None

        # First close succeeds
        r1 = session_close(
            conn,
            session_id=s1["session_id"],
            snapshot={"first": True},
        )
        assert r1["conflict"] is False

        # Second close gets conflict (INSERT OR IGNORE fails, CAS retry fails)
        r2 = session_close(
            conn,
            session_id=s2["session_id"],
            snapshot={"second": True},
        )
        assert r2["conflict"] is True


def test_session_close_idempotent(local_db):
    """Calling session_close twice returns closed state without error."""
    with storage.connect() as conn:
        s = session_start(conn, repo_identity="repo", branch="main")
        session_close(conn, session_id=s["session_id"])

        # Second close is idempotent
        result = session_close(conn, session_id=s["session_id"])
        assert result["state"] == "closed"


def test_session_close_writes_episodic(local_db):
    """Session close writes an episodic memory entry."""
    with storage.connect() as conn:
        s = session_start(conn, repo_identity="repo", branch="main")
        sid = s["session_id"]

        session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["main.py"], "action": "refactor"},
            delta_id="d1",
        )

        session_close(
            conn,
            session_id=sid,
            summary="Refactored main module",
            outcome="Clean code",
        )

        # Check episodic memory was created
        row = conn.execute(
            "SELECT content, memory_kind, repo_identity, session_id FROM memories WHERE session_id = ? AND memory_kind = 'episodic'",
            (sid,),
        ).fetchone()
        assert row is not None
        assert "Refactored main module" in row[0]
        assert row[1] == "episodic"
        assert row[2] == "repo"
        assert row[3] == sid


def test_scoped_search_branch_state(local_db):
    """Scoped search returns branch_state as tier 1."""
    with storage.connect() as conn:
        s = session_start(conn, repo_identity="repo", branch="main")
        session_close(
            conn,
            session_id=s["session_id"],
            snapshot={"active_bug": "AUTH-123", "open_todos": ["fix login"]},
        )

        result = scoped_search(
            conn,
            "auth bug",
            repo_identity="repo",
            branch="main",
        )
        assert "branch_state" in result["sources"]
        assert result["results"][0]["source"] == "branch_state"


def test_scoped_search_session_deltas(local_db):
    """Scoped search finds matching session deltas."""
    with storage.connect() as conn:
        s = session_start(conn, repo_identity="repo", branch="main")
        sid = s["session_id"]

        session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["auth.py"], "action": "fix login bug"},
            delta_id="d1",
        )

        result = scoped_search(
            conn,
            "login",
            repo_identity="repo",
            branch="main",
            session_id=sid,
        )
        assert "session_deltas" in result["sources"]


def test_scoped_search_global(local_db):
    """Global scope searches all memories."""
    with storage.connect() as conn:
        storage.add_memory(
            conn,
            content="Global knowledge about authentication patterns",
            tags=["knowledge"],
        )

        result = scoped_search(conn, "authentication", scope="global")
        assert "memories" in result["sources"]


def test_foreign_keys_enabled(local_db):
    """Verify PRAGMA foreign_keys is ON."""
    with storage.connect() as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk[0] == 1


def test_session_tables_created(local_db):
    """Verify session tables exist after connect."""
    with storage.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "sessions" in tables
        assert "session_deltas" in tables
        assert "branch_state" in tables


def test_session_close_crash_resume(local_db):
    """Session close persists payload for crash recovery."""
    with storage.connect() as conn:
        s = session_start(conn, repo_identity="repo", branch="main")
        sid = s["session_id"]

        session_delta(
            conn,
            session_id=sid,
            structured_facts={"files": ["main.py"]},
            delta_id="d1",
        )

        # Simulate: close transitions to 'closing' but crashes before completing
        conn.execute(
            "UPDATE sessions SET state = 'closing', close_phase = 1, close_payload = ? WHERE id = ?",
            (json.dumps({"summary": "test summary", "outcome": "ok", "snapshot": {"x": 1}, "head_commit_end": "abc"}), sid),
        )
        conn.commit()

        # Resume close — should recover payload from DB
        result = session_close(conn, session_id=sid)
        assert result["state"] == "closed"

        # Verify summary was recovered from persisted payload
        row = conn.execute("SELECT summary, outcome FROM sessions WHERE id = ?", (sid,)).fetchone()
        assert row[0] == "test summary"
        assert row[1] == "ok"


def test_export_sessions(local_db):
    """export_sessions returns session data."""
    with storage.connect() as conn:
        s = session_start(conn, repo_identity="repo", branch="main")
        session_delta(
            conn,
            session_id=s["session_id"],
            structured_facts={"x": 1},
            delta_id="d1",
        )
        session_close(
            conn,
            session_id=s["session_id"],
            summary="test",
            snapshot={"y": 2},
        )

        exported = storage.export_sessions(conn)
        assert len(exported["sessions"]) == 1
        assert len(exported["session_deltas"]) == 1
        assert len(exported["branch_state"]) == 1


def test_export_memories_unchanged(local_db):
    """export_memories still returns a plain list (not broken by session changes)."""
    with storage.connect() as conn:
        storage.add_memory(conn, content="Test export memory content here", tags=["test"])
        exported = storage.export_memories(conn)
        assert isinstance(exported, list)
        assert len(exported) == 1
        assert exported[0]["content"] == "Test export memory content here"


def test_memory_session_columns(local_db):
    """Verify new columns on memories table."""
    with storage.connect() as conn:
        cursor = conn.execute("PRAGMA table_info(memories)")
        columns = {row[1] for row in cursor.fetchall()}
        for col in ["repo_identity", "session_id", "branch", "head_commit", "memory_kind"]:
            assert col in columns, f"Missing column: {col}"
