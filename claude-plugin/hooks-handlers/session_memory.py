#!/usr/bin/env python3
"""Session memory hook — writes session lifecycle events to Memora.

Handles: SessionStart, Stop, UserPromptSubmit
Runs synchronously (~1-2ms for local SQLite). No daemon needed.

State file: /tmp/memora-session-<claude_session_id>.json
  Stores the Memora session_id so Stop hooks can write deltas.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# State dir for tracking active sessions
STATE_DIR = Path("/tmp/memora-sessions")
STATE_DIR.mkdir(exist_ok=True)

# Where the worktree code lives
WORKTREE = Path(__file__).resolve().parent.parent.parent
VENV_PYTHON = Path.home() / "repos/agentic-box/memora/.venv/bin/python"


def _resolve_repo_identity(cwd: str) -> str:
    """Get repo identity from git remote origin URL hash."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip()
            return hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        pass
    # Fallback: hash the repo root path
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return "local-" + hashlib.sha256(result.stdout.strip().encode()).hexdigest()[:12]
    except Exception:
        pass
    return "unknown"


def _git_info(cwd: str) -> dict:
    """Get current branch and HEAD commit."""
    info = {"branch": "unknown", "head_commit": None}
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            info["branch"] = r.stdout.strip()
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            info["head_commit"] = r.stdout.strip()[:12]
    except Exception:
        pass
    return info


def _state_path(claude_session_id: str) -> Path:
    return STATE_DIR / f"{claude_session_id}.json"


def _load_state(claude_session_id: str) -> dict | None:
    p = _state_path(claude_session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def _save_state(claude_session_id: str, state: dict):
    _state_path(claude_session_id).write_text(json.dumps(state))


def _resolve_db_path() -> str:
    """Resolve MEMORA_DB_PATH from env, then .mcp.json, then fallback."""
    val = os.environ.get("MEMORA_DB_PATH")
    if val:
        return val
    for mcp_path in [
        WORKTREE / ".mcp.json",
        WORKTREE.parent.parent / ".mcp.json",
    ]:
        if mcp_path.exists():
            try:
                config = json.loads(mcp_path.read_text())
                env = config.get("mcpServers", {}).get("memory", {}).get("env", {})
                if env.get("MEMORA_DB_PATH"):
                    return env["MEMORA_DB_PATH"]
            except Exception:
                pass
    return "~/.local/share/memora/memories.db"


def _call_memora(func_name: str, **kwargs):
    """Call a memora.sessions function directly."""
    # Set PYTHONPATH so we load the worktree code
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORA_DB_PATH"] = _resolve_db_path()
    env["MEMORA_EMBEDDING_MODEL"] = os.environ.get("MEMORA_EMBEDDING_MODEL", "tfidf")
    env["MEMORA_ALLOW_ANY_TAG"] = "1"

    # Pass kwargs via stdin to avoid shell quoting issues
    args_json = json.dumps(kwargs, ensure_ascii=False)
    script = f"""
import json, sys
from memora.sessions import {func_name}
from memora.storage import connect
kwargs = json.load(sys.stdin)
with connect() as conn:
    result = {func_name}(conn, **kwargs)
    print(json.dumps(result, default=str))
"""
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), "-c", script],
            input=args_json,
            capture_output=True, text=True, timeout=5, env=env,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    return None


def _load_llm_config() -> dict | None:
    """Load LLM config from .mcp.json in project root or worktree."""
    for mcp_path in [
        WORKTREE / ".mcp.json",
        WORKTREE.parent.parent / ".mcp.json",  # main repo if in worktree
    ]:
        if mcp_path.exists():
            try:
                config = json.loads(mcp_path.read_text())
                env = config.get("mcpServers", {}).get("memory", {}).get("env", {})
                if env.get("MEMORA_LLM_ENABLED", "").lower() in ("true", "1"):
                    api_key = env.get("OPENAI_API_KEY")
                    if api_key:
                        return {
                            "api_key": api_key,
                            "base_url": env.get("OPENAI_BASE_URL"),
                            "model": env.get("MEMORA_LLM_MODEL", "gpt-4o-mini"),
                        }
            except Exception:
                pass
    return None


def _build_transcript(session_result: dict) -> str:
    """Build transcript string from session data. Tries raw JSONL first, falls back to deltas."""
    transcript = ""
    transcript_path = session_result.get("transcript_path")
    if transcript_path:
        tp = Path(os.path.expanduser(transcript_path))
        if tp.exists():
            try:
                lines = []
                for raw_line in tp.read_text().splitlines():
                    try:
                        entry = json.loads(raw_line)
                        role = entry.get("role", "")
                        content = entry.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        if role in ("user", "assistant") and content:
                            speaker = "User" if role == "user" else "Claude"
                            lines.append(f"{speaker}: {content[:2000]}")
                    except (json.JSONDecodeError, TypeError):
                        pass
                if lines:
                    transcript = "\n".join(lines)
            except Exception:
                pass

    # Fall back to deltas if no transcript
    if not transcript:
        deltas = session_result.get("deltas", [])
        lines = []
        for d in deltas:
            facts = d.get("structured_facts", {})
            if "user_prompt" in facts:
                lines.append(f"User: {facts['user_prompt']}")
            if "assistant_message" in facts:
                lines.append(f"Claude: {facts['assistant_message']}")
            elif "assistant_hint" in facts:
                lines.append(f"Claude: {facts['assistant_hint']}")
            if "files" in facts:
                lines.append(f"Files: {', '.join(facts['files'])}")
            if "decisions" in facts:
                lines.append(f"Decisions: {', '.join(facts['decisions'])}")
            if "error_hint" in facts:
                lines.append(f"Error: {facts['error_hint']}")
        transcript = "\n".join(lines)

    # Cap to ~12000 chars for richer LLM compression input
    if len(transcript) > 12000:
        transcript = transcript[:12000] + "\n... (truncated)"

    return transcript


def _llm_compress_sync(session_id: str, llm_config: dict) -> dict | None:
    """Synchronous LLM compression. Returns dict with summary, snapshot, episodic fields or None."""
    # Need memora imports — add to path if needed
    if str(WORKTREE) not in sys.path:
        sys.path.insert(0, str(WORKTREE))

    from memora.sessions import session_get
    from memora.storage import connect

    with connect() as conn:
        result = session_get(conn, session_id)

    if not result or len(result.get("deltas", [])) < 2:
        return None

    transcript = _build_transcript(result)
    if not transcript:
        return None

    prompt = f"""Analyze this coding session and produce TWO outputs in a single JSON object.

**Output 1: "snapshot"** — A compact branch state for cold-starting the next session. Max 40 lines of markdown. Use this exact structure:

## What Just Happened
1-2 sentences. What was accomplished, any reframe.

## In Progress
Active items with status. Only what needs continuation. Omit if nothing is in progress.

## Decisions Made
Bullet list — the "why" alongside the "what". Most valuable field.

## Next Session
"Start with:" verb + object. "Also ready:" other unblocked items. Omit if unclear.

## Open Questions
Unresolved items carrying forward. Omit if none.

**Output 2: "episodic"** — A detailed session summary for long-term search. No line limit. Include rationale, tradeoffs considered, dead ends, error context, and anything useful for future recall.

Return JSON with these fields:
- "summary": One sentence (max 150 chars) describing what was accomplished
- "outcome": One sentence on the result/status
- "snapshot_md": The branch state markdown (max 40 lines, structured as above)
- "episodic_md": The detailed session narrative (no limit)
- "open_todos": Array of unfinished items (empty array if none)
- "touched_files": Array of files mentioned (empty array if none)
- "decisions": Array of key decisions made (empty array if none)
- "errors_encountered": Array of errors hit during the session (empty array if none)

Session transcript:
{transcript}

Return ONLY valid JSON, no markdown fences."""

    try:
        import openai
        client_kwargs = {"api_key": llm_config["api_key"]}
        if llm_config.get("base_url"):
            client_kwargs["base_url"] = llm_config["base_url"]
        client = openai.OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=llm_config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())
    except Exception:
        return None


def _apply_llm_result(session_id: str, llm_result: dict):
    """Write LLM compression result to DB (session summary + branch_state + episodic memory)."""
    if str(WORKTREE) not in sys.path:
        sys.path.insert(0, str(WORKTREE))

    from memora.storage import connect

    summary = llm_result.get("summary", "")[:200]
    outcome = llm_result.get("outcome", "")[:200]
    snapshot_md = llm_result.get("snapshot_md", "")
    episodic_md = llm_result.get("episodic_md", "")

    snapshot = {}
    if snapshot_md:
        snapshot["narrative"] = snapshot_md
    if llm_result.get("open_todos"):
        snapshot["open_todos"] = llm_result["open_todos"]
    if llm_result.get("touched_files"):
        snapshot["touched_files"] = llm_result["touched_files"]
    if llm_result.get("decisions"):
        snapshot["decisions"] = llm_result["decisions"]
    if llm_result.get("errors_encountered"):
        snapshot["errors_encountered"] = llm_result["errors_encountered"]

    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET summary = ?, outcome = ? WHERE id = ?",
            (summary, outcome, session_id),
        )
        row = conn.execute(
            "SELECT repo_identity, branch FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row and snapshot:
            conn.execute(
                """INSERT INTO branch_state (repo_identity, branch, snapshot, snapshot_revision, session_id, updated_at)
                VALUES (?, ?, ?, 1, ?, datetime('now'))
                ON CONFLICT(repo_identity, branch) DO UPDATE SET
                    snapshot = excluded.snapshot,
                    snapshot_revision = snapshot_revision + 1,
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at""",
                (row[0], row[1], json.dumps(snapshot), session_id),
            )

        if episodic_md and row:
            from memora.storage import add_memory
            episodic_content = f"## Session: {summary}\n\n{episodic_md}"
            add_memory(
                conn,
                content=episodic_content,
                tags=["memora/sessions"],
                metadata={
                    "memory_kind": "episodic",
                    "session_id": session_id,
                    "repo_identity": row[0],
                    "branch": row[1],
                },
            )
        conn.commit()


def _build_close_summary_structured(memora_session_id: str) -> dict:
    """Fast structured summary from deltas (~2ms). No LLM."""
    result = _call_memora("session_get", session_id=memora_session_id)
    if not result or "deltas" not in result:
        return {"summary": None, "snapshot": None}

    deltas = result.get("deltas", [])
    if not deltas:
        return {"summary": "Session with 0 turns", "snapshot": None}

    prompts = []
    hints = []
    files = set()
    todos = []

    for d in deltas:
        facts = d.get("structured_facts", {})
        if "user_prompt" in facts:
            prompts.append(facts["user_prompt"][:100])
        if "assistant_hint" in facts:
            hints.append(facts["assistant_hint"][:100])
        if "files" in facts:
            for f in facts["files"]:
                files.add(f)
        if "todos" in facts:
            todos.extend(facts["todos"])

    parts = []
    if prompts:
        parts.append(prompts[0])
    if hints:
        parts.append(hints[-1])
    summary = " | ".join(parts) if parts else f"Session with {len(deltas)} turns"
    if len(summary) > 200:
        summary = summary[:197] + "..."

    snapshot = {}
    if files:
        snapshot["touched_files"] = sorted(files)
    if todos:
        snapshot["open_todos"] = todos
    if prompts:
        snapshot["objective"] = prompts[0]

    return {"summary": summary, "snapshot": snapshot or None}


def _llm_compress_and_apply(memora_session_id: str) -> bool:
    """Run LLM compression synchronously. Returns True if successful."""
    llm_config = _load_llm_config()
    if not llm_config:
        return False

    # Set env vars for memora storage
    os.environ.setdefault("MEMORA_DB_PATH", _resolve_db_path())
    os.environ.setdefault("MEMORA_EMBEDDING_MODEL", "tfidf")
    os.environ.setdefault("MEMORA_ALLOW_ANY_TAG", "1")

    llm_result = _llm_compress_sync(memora_session_id, llm_config)
    if not llm_result:
        return False

    _apply_llm_result(memora_session_id, llm_result)
    return True


def handle_session_start(payload: dict):
    """Create a new Memora session on SessionStart."""
    claude_sid = payload.get("session_id", "")
    cwd = payload.get("cwd", os.getcwd())
    source = payload.get("source", "startup")

    # Don't create new session on compact — just a context refresh
    if source == "compact":
        return

    repo_id = _resolve_repo_identity(cwd)
    git = _git_info(cwd)

    # Check if we already have a session for this claude_session_id
    existing = _load_state(claude_sid)
    if existing and source == "resume":
        # Verify the session is still open before reusing
        session_info = _call_memora("session_get", session_id=existing["memora_session_id"])
        if session_info and session_info.get("state") == "open":
            return
        # Session was closed (manually or by another process) — fall through to create new one
        existing = None

    # Auto-close previous session: structured close + sync LLM compression
    if existing:
        memora_sid = existing["memora_session_id"]
        close_data = _build_close_summary_structured(memora_sid)
        _call_memora(
            "session_close",
            session_id=memora_sid,
            summary=close_data.get("summary"),
            snapshot=close_data.get("snapshot"),
        )
        _state_path(claude_sid).unlink(missing_ok=True)
        # Sync LLM compression — snapshot is fresh before context injection
        _llm_compress_and_apply(memora_sid)

    # Also close any other open sessions for this repo+branch (orphan cleanup)
    for state_file in STATE_DIR.glob("*.json"):
        try:
            other = json.loads(state_file.read_text())
            if other.get("repo_identity") == repo_id and other.get("branch") == git["branch"]:
                if other.get("memora_session_id") != (existing or {}).get("memora_session_id"):
                    orphan_sid = other["memora_session_id"]
                    _call_memora(
                        "session_close",
                        session_id=orphan_sid,
                        summary="Auto-closed: orphaned session",
                    )
                    state_file.unlink(missing_ok=True)
                    # Orphans get sync compression too — ensures fresh snapshot
                    _llm_compress_and_apply(orphan_sid)
        except Exception:
            pass

    transcript_path = payload.get("transcript_path")

    result = _call_memora(
        "session_start",
        repo_identity=repo_id,
        branch=git["branch"],
        head_commit=git["head_commit"],
        claude_session_id=claude_sid,
        transcript_path=transcript_path,
    )

    if result and "session_id" in result:
        _save_state(claude_sid, {
            "memora_session_id": result["session_id"],
            "repo_identity": repo_id,
            "branch": git["branch"],
            "delta_seq": 0,
            "transcript_path": transcript_path,
        })

    # Build context to inject into Claude's system prompt
    return _build_session_context(repo_id, git["branch"])


def _build_session_context(repo_identity: str, branch: str) -> str:
    """Build additionalContext from branch state + recent sessions."""
    parts = []

    # 1. Branch state (current truth)
    state = _call_memora("branch_state_get", repo_identity=repo_identity, branch=branch)
    if state and state.get("snapshot"):
        snap = state["snapshot"]
        parts.append("## Current Branch State")
        parts.append(f"Branch: `{branch}` (revision {state.get('snapshot_revision', '?')})")
        # If we have a narrative snapshot from LLM compression, use it directly
        if snap.get("narrative"):
            parts.append("")
            parts.append(snap["narrative"])
        else:
            # Fallback to structured fields
            if snap.get("open_todos"):
                parts.append("**Open TODOs:**")
                for t in snap["open_todos"]:
                    parts.append(f"  - {t}")
            if snap.get("active_bug"):
                parts.append(f"**Active bug:** {snap['active_bug']}")
            if snap.get("touched_files"):
                parts.append(f"**Recently touched:** {', '.join(snap['touched_files'][:10])}")
            if snap.get("constraints"):
                parts.append("**Constraints:**")
                for c in snap["constraints"]:
                    parts.append(f"  - {c}")
            if snap.get("objective"):
                parts.append(f"**Last objective:** {snap['objective']}")

    # 2. Recent closed sessions (last 5)
    sessions = _call_memora(
        "session_list",
        repo_identity=repo_identity,
        branch=branch,
        state="closed",
        limit=5,
    )
    # session_list returns a plain list when called directly
    if sessions and isinstance(sessions, list):
        session_entries = sessions
    elif sessions and isinstance(sessions, dict):
        session_entries = sessions.get("sessions", [])
    else:
        session_entries = []

    if session_entries:
        parts.append("")
        parts.append("## Recent Sessions")
        for s in session_entries:
            summary = s.get("summary") or "(no summary)"
            date = (s.get("closed_at") or s.get("started_at", ""))[:16]
            conflict = " [CONFLICT]" if s.get("conflict") else ""
            parts.append(f"- [{date}]{conflict} {summary}")

    if not parts:
        return ""

    return "\n".join(["# Session Memory (Memora)", ""] + parts + [
        "",
        "Use `memory_scoped_search` to search for more context.",
    ])


def _extract_file_paths(text: str) -> list:
    """Extract file paths mentioned in text."""
    import re
    patterns = [
        r'`([a-zA-Z0-9_./\-]+\.[a-zA-Z]{1,10})`',      # `path/to/file.ext`
        r'(?:^|\s)(\S+/\S+\.[a-zA-Z]{1,10})(?:\s|$|:)',  # path/to/file.ext
    ]
    files = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            path = match.group(1)
            # Filter out URLs and very short matches
            if not path.startswith(("http", "//")) and "/" in path and len(path) > 3:
                files.add(path)
    return sorted(files)[:20]  # Cap at 20


def _extract_git_ops(text: str) -> list:
    """Extract git operations mentioned in text."""
    import re
    ops = []
    git_patterns = [
        (r'git commit.*?-m\s*["\'](.+?)["\']', "commit"),
        (r'git push\s+(\S+)', "push"),
        (r'git merge\s+(\S+)', "merge"),
        (r'git checkout\s+(\S+)', "checkout"),
        (r'committed.*?`([a-f0-9]{7,})`', "commit"),
    ]
    for pattern, op_type in git_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            ops.append({"op": op_type, "detail": match.group(1)[:80]})
    return ops[:10]


def _extract_decisions(text: str) -> list:
    """Extract decision-like statements from text."""
    import re
    decisions = []
    # Look for decision indicators
    for pattern in [
        r"(?:decided|choosing|going with|using|switched to|changed to)\s+(.{10,80}?)(?:\.|$)",
        r"(?:instead of|rather than)\s+(.{10,80}?)(?:,|\.|$)",
    ]:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            decisions.append(match.group(1).strip())
    return decisions[:5]


def handle_stop(payload: dict):
    """Write a delta on Stop (Claude finished a turn)."""
    claude_sid = payload.get("session_id", "")
    state = _load_state(claude_sid)
    if not state:
        return

    memora_sid = state["memora_session_id"]
    last_msg = payload.get("last_assistant_message", "")

    if not last_msg:
        return

    # Extract rich structured facts from the full assistant message
    facts = {}

    cwd = payload.get("cwd", "")
    if cwd:
        facts["cwd"] = cwd

    # Full response (up to 2000 chars for LLM compression at close)
    facts["assistant_message"] = last_msg[:2000]

    # Extracted structure
    files = _extract_file_paths(last_msg)
    if files:
        facts["files"] = files

    git_ops = _extract_git_ops(last_msg)
    if git_ops:
        facts["git_ops"] = git_ops

    decisions = _extract_decisions(last_msg)
    if decisions:
        facts["decisions"] = decisions

    # Detect errors/failures
    if any(kw in last_msg.lower() for kw in ["error", "failed", "traceback", "exception"]):
        # Extract first error-like line
        for line in last_msg.split("\n"):
            if any(kw in line.lower() for kw in ["error", "failed", "traceback"]):
                facts["error_hint"] = line.strip()[:200]
                break

    state["delta_seq"] += 1
    delta_id = f"{claude_sid[:8]}:{state['delta_seq']}"

    _call_memora(
        "session_delta",
        session_id=memora_sid,
        structured_facts=facts,
        delta_id=delta_id,
    )

    _save_state(claude_sid, state)


def handle_prompt(payload: dict):
    """Track user prompt for objective extraction."""
    claude_sid = payload.get("session_id", "")
    state = _load_state(claude_sid)
    if not state:
        return

    prompt = payload.get("prompt", "")
    if not prompt:
        return

    state["delta_seq"] += 1
    delta_id = f"{claude_sid[:8]}:{state['delta_seq']}"

    facts = {"user_prompt": prompt[:1000]}

    # Extract file references from user prompt too
    files = _extract_file_paths(prompt)
    if files:
        facts["files_mentioned"] = files

    _call_memora(
        "session_delta",
        session_id=state["memora_session_id"],
        structured_facts=facts,
        delta_id=delta_id,
    )

    _save_state(claude_sid, state)


def _is_session_enabled() -> bool:
    """Check MEMORA_SESSION_ENABLED in env or .mcp.json. Defaults to False."""
    # Check env first (set by MCP server or explicitly)
    env_val = os.environ.get("MEMORA_SESSION_ENABLED", "")
    if env_val:
        return env_val.lower() in ("true", "1", "yes")

    # Check .mcp.json
    for mcp_path in [
        WORKTREE / ".mcp.json",
        WORKTREE.parent.parent / ".mcp.json",
    ]:
        if mcp_path.exists():
            try:
                config = json.loads(mcp_path.read_text())
                env = config.get("mcpServers", {}).get("memory", {}).get("env", {})
                val = env.get("MEMORA_SESSION_ENABLED", "")
                if val:
                    return val.lower() in ("true", "1", "yes")
            except Exception:
                pass
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        sys.exit(0)

    # Feature flag — disabled by default
    if not _is_session_enabled():
        print(json.dumps({}))
        return

    event = payload.get("hook_event_name", "")
    output = {}

    if event == "SessionStart":
        context = handle_session_start(payload)
        if context:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
    elif event == "Stop":
        handle_stop(payload)
    elif event == "UserPromptSubmit":
        handle_prompt(payload)

    print(json.dumps(output))


if __name__ == "__main__":
    main()
