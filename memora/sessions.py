"""Session memory storage functions.

Dedicated storage path for session lifecycle operations.
These bypass FTS, embeddings, crossrefs, and cloud sync.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SessionError(Exception):
    """Raised for session operation failures."""
    pass


class SessionBackendError(SessionError):
    """Raised when the backend does not support sessions."""
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _require_sessions(backend) -> None:
    """Raise if the backend does not support session memory."""
    if not backend.supports_sessions:
        raise SessionBackendError(
            "Session memory requires local SQLite backend. "
            "Current backend does not support sessions."
        )


def session_start(
    conn: sqlite3.Connection,
    *,
    repo_identity: str,
    branch: str,
    head_commit: Optional[str] = None,
    objective: Optional[str] = None,
    claude_session_id: Optional[str] = None,
    pane_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    transcript_path: Optional[str] = None,
    transcript_start_line: Optional[int] = None,
    start_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new session.

    Reads base_snapshot_revision from branch_state for CAS at close time.
    Replay-idempotent via start_key: if a session with this start_key exists,
    returns the existing session.
    """
    # Read base_snapshot_revision from branch_state
    row = conn.execute(
        "SELECT snapshot_revision FROM branch_state WHERE repo_identity = ? AND branch = ?",
        (repo_identity, branch),
    ).fetchone()
    base_snapshot_revision = row[0] if row else None

    session_id = str(uuid.uuid4())
    now = _now()

    if start_key:
        # Atomic idempotency: INSERT OR IGNORE + check if our row was inserted
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (
                id, claude_session_id, transcript_path, transcript_start_line,
                repo_identity, pane_id, branch, workspace_id,
                state, base_snapshot_revision, objective,
                started_at, head_commit_start, start_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                session_id, claude_session_id, transcript_path, transcript_start_line,
                repo_identity, pane_id, branch, workspace_id,
                base_snapshot_revision, objective,
                now, head_commit, start_key,
            ),
        )
        conn.commit()
        # Check if a session with this start_key exists (either ours or a prior one)
        existing = conn.execute(
            "SELECT id, state FROM sessions WHERE start_key = ?",
            (start_key,),
        ).fetchone()
        if existing and existing[0] != session_id:
            return {"session_id": existing[0], "state": existing[1], "replayed": True}
    else:
        conn.execute(
            """
            INSERT INTO sessions (
                id, claude_session_id, transcript_path, transcript_start_line,
                repo_identity, pane_id, branch, workspace_id,
                state, base_snapshot_revision, objective,
                started_at, head_commit_start, start_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                session_id, claude_session_id, transcript_path, transcript_start_line,
                repo_identity, pane_id, branch, workspace_id,
                base_snapshot_revision, objective,
                now, head_commit, start_key,
            ),
        )
        conn.commit()

    return {
        "session_id": session_id,
        "state": "open",
        "base_snapshot_revision": base_snapshot_revision,
        "replayed": False,
    }


def session_delta(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    structured_facts: Dict[str, Any],
    delta_id: str,
) -> Dict[str, Any]:
    """Append a delta to a session. Fast path — no FTS/embeddings/crossrefs/sync.

    Server assigns event_seq from sessions.next_event_seq.
    Retry-idempotent via delta_id.
    """
    # Verify session exists and is open
    session = conn.execute(
        "SELECT state, next_event_seq FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not session:
        raise SessionError(f"Session {session_id} not found")
    if session[0] != "open":
        raise SessionError(f"Session {session_id} is {session[0]}, not open")

    event_seq = session[1]
    facts_json = json.dumps(structured_facts, ensure_ascii=False)

    # Atomic idempotency: INSERT OR IGNORE on UNIQUE(session_id, delta_id)
    inserted = conn.execute(
        """
        INSERT OR IGNORE INTO session_deltas (session_id, event_seq, structured_facts, delta_id)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, event_seq, facts_json, delta_id),
    ).rowcount

    if not inserted:
        # Replay: delta_id already exists, return its event_seq
        existing = conn.execute(
            "SELECT event_seq FROM session_deltas WHERE session_id = ? AND delta_id = ?",
            (session_id, delta_id),
        ).fetchone()
        conn.commit()
        return {"event_seq": existing[0], "replayed": True}

    conn.execute(
        "UPDATE sessions SET next_event_seq = ? WHERE id = ?",
        (event_seq + 1, session_id),
    )
    conn.commit()

    return {"event_seq": event_seq, "replayed": False}


def session_close(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    summary: Optional[str] = None,
    outcome: Optional[str] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    head_commit_end: Optional[str] = None,
) -> Dict[str, Any]:
    """Close a session using the exact-once state machine.

    Phases:
    1. Transition open -> closing (CAS)
    2. Write branch_state (CAS on snapshot_revision)
    3. Write episodic memory
    4. Write session summary/outcome
    5. Transition closing -> closed
    """
    session = conn.execute(
        """SELECT id, state, close_phase, repo_identity, branch,
                  base_snapshot_revision, close_payload
           FROM sessions WHERE id = ?""",
        (session_id,),
    ).fetchone()
    if not session:
        raise SessionError(f"Session {session_id} not found")

    state = session[1]
    close_phase = session[2]
    repo_identity = session[3]
    branch = session[4]
    base_rev = session[5]
    stored_payload = session[6]

    # On resume (state=closing), recover close input from persisted payload
    if state == "closing" and stored_payload:
        try:
            payload = json.loads(stored_payload)
            summary = summary or payload.get("summary")
            outcome = outcome or payload.get("outcome")
            snapshot = snapshot or payload.get("snapshot")
            head_commit_end = head_commit_end or payload.get("head_commit_end")
        except (json.JSONDecodeError, TypeError):
            pass

    # Phase 0: Transition to closing + persist close payload
    if state == "open":
        close_payload = json.dumps({
            "summary": summary,
            "outcome": outcome,
            "snapshot": snapshot,
            "head_commit_end": head_commit_end,
        }, ensure_ascii=False)
        updated = conn.execute(
            "UPDATE sessions SET state = 'closing', close_phase = 1, close_payload = ? WHERE id = ? AND state = 'open'",
            (close_payload, session_id),
        ).rowcount
        if not updated:
            # Race: another caller already transitioned
            session = conn.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session and session[0] == "closed":
                return {"state": "closed", "conflict": False}
            state = session[0] if session else "unknown"
        else:
            state = "closing"
            close_phase = 1
            conn.commit()
    elif state == "closed":
        return {"state": "closed", "conflict": False}
    # If state == 'closing', resume from close_phase

    conflict = False

    # Phase 1: Write branch_state (CAS)
    if close_phase <= 1 and snapshot is not None:
        snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        now = _now()

        if base_rev is None:
            # New branch — INSERT OR IGNORE
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO branch_state
                    (repo_identity, branch, snapshot, snapshot_revision, session_id, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (repo_identity, branch, snapshot_json, session_id, now),
            ).rowcount
            if not inserted:
                # PK conflict: another session created this row first. Try CAS update.
                # Read current revision and attempt CAS with base_rev=NULL expectation
                # Since we expected NULL but row exists, this is a conflict.
                conflict = True
        else:
            # Existing branch — CAS update
            updated = conn.execute(
                """
                UPDATE branch_state
                SET snapshot = ?, snapshot_revision = snapshot_revision + 1,
                    session_id = ?, updated_at = ?
                WHERE repo_identity = ? AND branch = ? AND snapshot_revision = ?
                """,
                (snapshot_json, session_id, now, repo_identity, branch, base_rev),
            ).rowcount
            if not updated:
                conflict = True

        conn.execute(
            "UPDATE sessions SET close_phase = 2, conflict = ? WHERE id = ?",
            (1 if conflict else 0, session_id),
        )
        conn.commit()
        close_phase = 2

    # Phase 2: Write episodic memory (idempotent — check before insert)
    if close_phase <= 2:
        # Collect all deltas for this session
        deltas = conn.execute(
            "SELECT structured_facts FROM session_deltas WHERE session_id = ? ORDER BY event_seq",
            (session_id,),
        ).fetchall()

        # Build episodic content from summary + deltas
        episodic_parts = []
        if summary:
            episodic_parts.append(summary)
        if outcome:
            episodic_parts.append(f"Outcome: {outcome}")
        if deltas:
            facts_summary = []
            for d in deltas:
                try:
                    facts = json.loads(d[0])
                    if isinstance(facts, dict):
                        for k, v in facts.items():
                            facts_summary.append(f"- {k}: {v}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if facts_summary:
                episodic_parts.append("Key facts:\n" + "\n".join(facts_summary[:20]))

        episodic_content = "\n\n".join(episodic_parts) if episodic_parts else f"Session {session_id}"

        # Check if episodic already exists
        existing_episodic = conn.execute(
            "SELECT id FROM memories WHERE session_id = ? AND memory_kind = 'episodic'",
            (session_id,),
        ).fetchone()

        if not existing_episodic:
            from .storage import add_memory
            metadata = {
                "session_id": session_id,
                "repo_identity": repo_identity,
                "branch": branch,
            }
            mem = add_memory(
                conn,
                content=episodic_content,
                metadata=metadata,
                tags=["memora/session-episodic"],
            )
            # Set session-specific columns
            conn.execute(
                """UPDATE memories
                   SET repo_identity = ?, session_id = ?, branch = ?,
                       head_commit = ?, memory_kind = 'episodic'
                   WHERE id = ?""",
                (repo_identity, session_id, branch, head_commit_end, mem["id"]),
            )

        conn.execute(
            "UPDATE sessions SET close_phase = 3 WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        close_phase = 3

    # Phase 3: Write session summary and outcome
    if close_phase <= 3:
        now = _now()
        conn.execute(
            """UPDATE sessions
               SET summary = ?, outcome = ?, head_commit_end = ?,
                   close_phase = 4, closed_at = ?, state = 'closed'
               WHERE id = ?""",
            (summary, outcome, head_commit_end, now, session_id),
        )
        conn.commit()

    return {"state": "closed", "conflict": conflict}


def session_get(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """Get a session with all its deltas."""
    session = conn.execute(
        "SELECT * FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not session:
        return None

    keys = [desc[0] for desc in conn.execute("SELECT * FROM sessions LIMIT 0").description]
    result = dict(zip(keys, session))

    deltas = conn.execute(
        """SELECT event_seq, structured_facts, delta_id, created_at
           FROM session_deltas WHERE session_id = ? ORDER BY event_seq""",
        (session_id,),
    ).fetchall()
    result["deltas"] = [
        {
            "event_seq": d[0],
            "structured_facts": json.loads(d[1]) if d[1] else {},
            "delta_id": d[2],
            "created_at": d[3],
        }
        for d in deltas
    ]

    return result


def session_list(
    conn: sqlite3.Connection,
    *,
    repo_identity: Optional[str] = None,
    branch: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List sessions with optional filters."""
    conditions = []
    params = []

    if repo_identity:
        conditions.append("repo_identity = ?")
        params.append(repo_identity)
    if branch:
        conditions.append("branch = ?")
        params.append(branch)
    if state:
        conditions.append("state = ?")
        params.append(state)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    rows = conn.execute(
        f"""SELECT id, repo_identity, branch, state, objective, summary,
                   outcome, started_at, closed_at, conflict
            FROM sessions WHERE {where}
            ORDER BY started_at DESC LIMIT ?""",
        params,
    ).fetchall()

    return [
        {
            "session_id": r[0],
            "repo_identity": r[1],
            "branch": r[2],
            "state": r[3],
            "objective": r[4],
            "summary": r[5],
            "outcome": r[6],
            "started_at": r[7],
            "closed_at": r[8],
            "conflict": bool(r[9]),
        }
        for r in rows
    ]


def branch_state_get(
    conn: sqlite3.Connection,
    repo_identity: str,
    branch: str,
) -> Optional[Dict[str, Any]]:
    """Get the current branch state snapshot."""
    row = conn.execute(
        """SELECT snapshot, snapshot_revision, session_id, updated_at
           FROM branch_state WHERE repo_identity = ? AND branch = ?""",
        (repo_identity, branch),
    ).fetchone()
    if not row:
        return None

    return {
        "snapshot": json.loads(row[0]) if row[0] else {},
        "snapshot_revision": row[1],
        "session_id": row[2],
        "updated_at": row[3],
    }


def scoped_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    scope: str = "auto",
    repo_identity: Optional[str] = None,
    branch: Optional[str] = None,
    session_id: Optional[str] = None,
    top_k: int = 10,
) -> Dict[str, Any]:
    """Scoped search orchestrator across branch_state, session_deltas, and memories.

    Queries three separate data sources and merges results:
    - branch_state: direct lookup (current truth)
    - session_deltas: field matching on current session
    - memories: hybrid search with repo/branch filtering

    Args:
        scope: "auto" (tiered), "project" (repo-wide), or "global"
    """
    from .storage import hybrid_search

    results: List[Dict[str, Any]] = []
    sources_used: List[str] = []

    # Tier 1: Branch state (always first for auto/project scope)
    if scope in ("auto", "project") and repo_identity and branch:
        state = branch_state_get(conn, repo_identity, branch)
        if state and state["snapshot"]:
            results.append({
                "source": "branch_state",
                "score": 1.0,
                "data": state,
            })
            sources_used.append("branch_state")

    # Tier 2: Session deltas (auto scope only, current session)
    if scope == "auto" and session_id:
        deltas = conn.execute(
            """SELECT event_seq, structured_facts, created_at
               FROM session_deltas WHERE session_id = ?
               ORDER BY event_seq DESC LIMIT ?""",
            (session_id, top_k),
        ).fetchall()
        if deltas:
            delta_results = []
            for d in deltas:
                try:
                    facts = json.loads(d[1]) if d[1] else {}
                except (json.JSONDecodeError, TypeError):
                    facts = {}
                # Simple text matching on fact values
                fact_text = json.dumps(facts).lower()
                if query.lower() in fact_text:
                    delta_results.append({
                        "event_seq": d[0],
                        "facts": facts,
                        "created_at": d[2],
                    })
            if delta_results:
                results.append({
                    "source": "session_deltas",
                    "score": 0.9,
                    "data": delta_results,
                })
                sources_used.append("session_deltas")

    # Tier 3: Memories (hybrid search, scoped by repo/branch)
    if scope == "global" or not results or len(results) < top_k:
        # Build metadata filters based on scope
        metadata_filters = None
        tags_none = None

        if scope == "auto" and repo_identity:
            # First try: repo + branch scoped
            memory_results = hybrid_search(
                conn,
                query,
                top_k=top_k,
                metadata_filters={"repo_identity": repo_identity},
            )

            # Filter by branch in Python (metadata_filters only supports exact match)
            if branch:
                branch_results = [
                    r for r in memory_results
                    if r.get("memory", {}).get("branch") == branch
                    or r.get("memory", {}).get("branch") is None  # pre-migration
                ]
                if branch_results:
                    memory_results = branch_results

            if not memory_results:
                # Widen: repo only (all branches)
                memory_results = hybrid_search(
                    conn,
                    query,
                    top_k=top_k,
                )
        elif scope == "project" and repo_identity:
            memory_results = hybrid_search(
                conn,
                query,
                top_k=top_k,
            )
        else:
            # Global
            memory_results = hybrid_search(
                conn,
                query,
                top_k=top_k,
            )

        if memory_results:
            # Rank: episodic > NULL (pre-migration) > artifact
            def _kind_rank(r):
                kind = r.get("memory", {}).get("memory_kind")
                if kind == "episodic":
                    return 0
                elif kind is None:
                    return 1
                elif kind == "artifact":
                    return 2
                return 3

            memory_results.sort(key=lambda r: (_kind_rank(r), -r.get("score", 0)))

            results.append({
                "source": "memories",
                "score": memory_results[0]["score"] if memory_results else 0,
                "data": memory_results[:top_k],
            })
            sources_used.append("memories")

    return {
        "results": results,
        "sources": sources_used,
        "scope": scope,
        "query": query,
    }
