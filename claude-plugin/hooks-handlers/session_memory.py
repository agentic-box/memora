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


def _call_memora(func_name: str, **kwargs):
    """Call a memora.sessions function directly."""
    # Set PYTHONPATH so we load the worktree code
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORA_DB_PATH"] = os.environ.get("MEMORA_DB_PATH", "/tmp/memora-session-test.db")
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


def _llm_compress(deltas: list, llm_config: dict) -> dict | None:
    """Use LLM to compress session deltas into a summary + snapshot."""
    try:
        import openai
    except ImportError:
        return None

    # Build conversation transcript from deltas
    lines = []
    for d in deltas:
        facts = d.get("structured_facts", {})
        if "user_prompt" in facts:
            lines.append(f"User: {facts['user_prompt']}")
        if "assistant_hint" in facts:
            lines.append(f"Claude: {facts['assistant_hint']}")
        if "files" in facts:
            lines.append(f"Files: {', '.join(facts['files'])}")
        if "todos" in facts:
            lines.append(f"TODOs: {', '.join(facts['todos'])}")

    if not lines:
        return None

    transcript = "\n".join(lines)
    # Cap transcript to ~3000 chars to keep token cost low
    if len(transcript) > 3000:
        transcript = transcript[:3000] + "\n... (truncated)"

    prompt = f"""Summarize this coding session. Return JSON with exactly these fields:
- "summary": One sentence (max 150 chars) describing what was accomplished
- "outcome": One sentence on the result/status
- "open_todos": Array of unfinished items (empty array if none)
- "touched_files": Array of files mentioned (empty array if none)
- "decisions": Array of key decisions made (empty array if none)

Session transcript:
{transcript}

Return ONLY valid JSON, no markdown fences."""

    try:
        client_kwargs = {"api_key": llm_config["api_key"]}
        if llm_config.get("base_url"):
            client_kwargs["base_url"] = llm_config["base_url"]
        client = openai.OpenAI(**client_kwargs)

        response = client.chat.completions.create(
            model=llm_config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())
    except Exception:
        return None


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


def _background_llm_compress(memora_session_id: str):
    """Fire LLM compression in background process. Updates session summary after close."""
    llm_config = _load_llm_config()
    if not llm_config:
        return

    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORA_DB_PATH"] = os.environ.get("MEMORA_DB_PATH", "/tmp/memora-session-test.db")
    env["MEMORA_EMBEDDING_MODEL"] = os.environ.get("MEMORA_EMBEDDING_MODEL", "tfidf")
    env["MEMORA_ALLOW_ANY_TAG"] = "1"

    # Pass LLM config + session ID to a background script
    script_data = json.dumps({
        "session_id": memora_session_id,
        "llm_config": llm_config,
    })

    script = """
import json, sys, sqlite3, os
from memora.sessions import session_get
from memora.storage import connect

data = json.load(sys.stdin)
session_id = data["session_id"]
llm_config = data["llm_config"]

# Get deltas
with connect() as conn:
    result = session_get(conn, session_id)

if not result or len(result.get("deltas", [])) < 2:
    sys.exit(0)

deltas = result["deltas"]

# Build transcript
lines = []
for d in deltas:
    facts = d.get("structured_facts", {})
    if "user_prompt" in facts:
        lines.append(f"User: {facts['user_prompt']}")
    if "assistant_hint" in facts:
        lines.append(f"Claude: {facts['assistant_hint']}")
    if "files" in facts:
        lines.append(f"Files: {', '.join(facts['files'])}")
    if "todos" in facts:
        lines.append(f"TODOs: {', '.join(facts['todos'])}")

if not lines:
    sys.exit(0)

transcript = "\\n".join(lines)
if len(transcript) > 3000:
    transcript = transcript[:3000] + "\\n... (truncated)"

prompt = f\"\"\"Summarize this coding session. Return JSON with exactly these fields:
- "summary": One sentence (max 150 chars) describing what was accomplished
- "outcome": One sentence on the result/status
- "open_todos": Array of unfinished items (empty array if none)
- "touched_files": Array of files mentioned (empty array if none)
- "decisions": Array of key decisions made (empty array if none)

Session transcript:
{transcript}

Return ONLY valid JSON, no markdown fences.\"\"\"

try:
    import openai
    client_kwargs = {"api_key": llm_config["api_key"]}
    if llm_config.get("base_url"):
        client_kwargs["base_url"] = llm_config["base_url"]
    client = openai.OpenAI(**client_kwargs)
    response = client.chat.completions.create(
        model=llm_config["model"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0,
    )
    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\\n", 1)[1] if "\\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    llm_result = json.loads(text.strip())
except Exception:
    sys.exit(0)

# Update the closed session with LLM summary
summary = llm_result.get("summary", "")[:200]
outcome = llm_result.get("outcome", "")[:200]
snapshot = {}
if llm_result.get("open_todos"):
    snapshot["open_todos"] = llm_result["open_todos"]
if llm_result.get("touched_files"):
    snapshot["touched_files"] = llm_result["touched_files"]
if llm_result.get("decisions"):
    snapshot["decisions"] = llm_result["decisions"]

with connect() as conn:
    conn.execute(
        "UPDATE sessions SET summary = ?, outcome = ? WHERE id = ?",
        (summary, outcome, session_id),
    )
    if snapshot:
        # Update branch_state snapshot with LLM-extracted data
        row = conn.execute(
            "SELECT repo_identity, branch FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row:
            import json as j
            conn.execute(
                "UPDATE branch_state SET snapshot = ? WHERE repo_identity = ? AND branch = ?",
                (j.dumps(snapshot), row[0], row[1]),
            )
    conn.commit()
"""
    try:
        # Fire and forget — subprocess runs in background
        proc = subprocess.Popen(
            [str(VENV_PYTHON), "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        proc.stdin.write(script_data.encode())
        proc.stdin.close()
    except Exception:
        pass


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
        # --continue: reuse existing session
        return

    # Auto-close previous session: fast structured close + async LLM upgrade
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
        # Background LLM compression — upgrades summary after ~10s
        _background_llm_compress(memora_sid)

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
                    _background_llm_compress(orphan_sid)
        except Exception:
            pass

    result = _call_memora(
        "session_start",
        repo_identity=repo_id,
        branch=git["branch"],
        head_commit=git["head_commit"],
        claude_session_id=claude_sid,
    )

    if result and "session_id" in result:
        _save_state(claude_sid, {
            "memora_session_id": result["session_id"],
            "repo_identity": repo_id,
            "branch": git["branch"],
            "delta_seq": 0,
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


def handle_stop(payload: dict):
    """Write a delta on Stop (Claude finished a turn)."""
    claude_sid = payload.get("session_id", "")
    state = _load_state(claude_sid)
    if not state:
        return

    memora_sid = state["memora_session_id"]
    last_msg = payload.get("last_assistant_message", "")

    # Extract structured facts from the assistant message
    facts = {}

    # Detect file references
    cwd = payload.get("cwd", "")
    if cwd:
        facts["cwd"] = cwd

    # Keep a short summary of what Claude said (truncated)
    if last_msg:
        # First 200 chars as a hint
        facts["assistant_hint"] = last_msg[:200]

    if not facts:
        return

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

    # Write prompt as a delta too
    state["delta_seq"] += 1
    delta_id = f"{claude_sid[:8]}:{state['delta_seq']}"

    _call_memora(
        "session_delta",
        session_id=state["memora_session_id"],
        structured_facts={"user_prompt": prompt[:300]},
        delta_id=delta_id,
    )

    _save_state(claude_sid, state)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

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
