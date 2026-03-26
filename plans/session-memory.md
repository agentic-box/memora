# Multi-Project Session Memory for Memora + clmux

**Issue:** agentic-box/memora#38
**Date:** 2026-03-26
**Status:** Draft (v8 — post-codex-review)
**Research:** Codex + Gemini findings synthesized

## Problem

Skill-based recording (filepaths, line refs, repo info per session) creates transcript logs, not memory. Too noisy, token-heavy, inaccurate. Users need session memory that captures decisions and intent, not raw history.

## Core Architecture

Three-tier memory with scoped retrieval:

```
Session (ephemeral) --> Project/Branch (durable) --> Global (fallback)
```

Each tier has different persistence rules, retrieval priority, and write frequency.

## Identity Model

First-class identity hierarchy (must be frozen before implementation):

- **`repo_identity`** -- canonical repo ID. Primary: normalized git remote origin URL hash. Fallback for no-remote repos: repo root path hash + user-assignable alias. Forks get their OWN identity (fork URL differs from upstream). User can manually link two repo_identities as aliases if they want shared memory across fork/upstream.
- **`branch`** -- git branch name. State snapshots are branch-scoped, not project-wide.
- **`head_commit`** -- commit hash at time of write. Enables validity tracking.
- **`workspace_instance`** -- runtime-local clmux workspace ID. Ephemeral, not used for scoping memory.
- **`session_id`** -- uuid per session. Ephemeral, used for grouping deltas.
- **`snapshot_revision`** -- monotonic counter per (repo_identity, branch). Enables compare-and-swap on state overwrites.
- **`event_sequence`** -- per-session monotonic counter. Enables idempotent delta ingestion and dedup on retries.

## Schema Changes (Memora)

### New `sessions` table

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,              -- uuid (memora session)
    claude_session_id TEXT,           -- Claude Code conversation UUID (persists across --continue)
    transcript_path TEXT,             -- path to conversation JSONL (for LLM compression)
    transcript_start_line INTEGER,    -- first line in transcript for this session/run
    transcript_end_line INTEGER,      -- last line in transcript for this session/run (set at close)
    repo_identity TEXT NOT NULL,      -- canonical repo ID
    pane_id TEXT,                     -- tmux pane (sessions are pane-scoped, not workspace-scoped)
    branch TEXT,                      -- git branch at session start
    workspace_id TEXT,                -- clmux workspace (runtime, informational)
    state TEXT NOT NULL DEFAULT 'open',  -- open -> closing -> closed (exact-once state machine)
    close_phase INTEGER DEFAULT 0,    -- crash-resumable close progress (0-5)
    close_payload TEXT,               -- JSON: accumulated close data
    base_snapshot_revision INTEGER,   -- snapshot_revision at session start (for CAS at close, persisted for crash recovery)
    conflict INTEGER DEFAULT 0,       -- 1 if CAS failed at close (revision moved). Session still closes normally but branch_state is not updated.
    objective TEXT,                   -- auto-extracted from first prompt
    outcome TEXT,                     -- what actually happened
    summary TEXT,                     -- one-liner session index (lives here, not in separate table)
    started_at TEXT NOT NULL,
    closed_at TEXT,
    head_commit_start TEXT,           -- commit at session start
    head_commit_end TEXT,             -- commit at session close
    next_event_seq INTEGER DEFAULT 1, -- server-side monotonic counter, assigned on each delta write
    start_key TEXT UNIQUE             -- replay-idempotency key (pane_id:inode:gen:offset of SessionStart line)
);
CREATE INDEX idx_sessions_repo ON sessions(repo_identity);
CREATE INDEX idx_sessions_branch ON sessions(repo_identity, branch);
```

### New `session_deltas` table (separate from memories)

Session deltas are high-frequency, low-value writes that should NOT trigger embedding computation, FTS updates, or crossref calculation. Stored separately and merged into memories only at session close.

```sql
CREATE TABLE session_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,       -- per-session monotonic counter (server-assigned)
    structured_facts TEXT NOT NULL,   -- JSON: branch, files, todos, etc.
    delta_id TEXT NOT NULL,           -- daemon-assigned UUID or pane:inode:offset (transport-level idempotency)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE(session_id, event_seq),    -- prevents double-insert from single MCP call retry
    UNIQUE(session_id, delta_id)      -- prevents duplicate insert from daemon retry after lost response
);
CREATE INDEX idx_deltas_session ON session_deltas(session_id);
```

### New `branch_state` table (dedicated current-truth store)

Branch state is the single source of truth for "what is true now" per (repo, branch). Full replacement on each session close — no merge, no tombstones needed. Resolved items are simply absent from the new snapshot.

```sql
CREATE TABLE branch_state (
    repo_identity TEXT NOT NULL,
    branch TEXT NOT NULL,
    snapshot TEXT NOT NULL,           -- JSON: branch, commit, active_bug, open_todos, touched_files, constraints, narrative
    snapshot_revision INTEGER NOT NULL DEFAULT 1,
    session_id TEXT NOT NULL,         -- which session last wrote this
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo_identity, branch)
);
```

### New indexed columns on `memories`

```sql
ALTER TABLE memories ADD COLUMN repo_identity TEXT;
ALTER TABLE memories ADD COLUMN session_id TEXT;
ALTER TABLE memories ADD COLUMN branch TEXT;
ALTER TABLE memories ADD COLUMN head_commit TEXT;
ALTER TABLE memories ADD COLUMN memory_kind TEXT;  -- NULL for existing rows
-- episodic: session summaries (written at session close)
-- artifact: logs, test outputs, commit notes
-- NULL/unknown: pre-migration memories (ranked below episodic)
-- NOTE: "state" (current truth) lives in branch_state table, NOT in memories
CREATE INDEX idx_memories_repo ON memories(repo_identity);
CREATE INDEX idx_memories_kind ON memories(memory_kind);
CREATE INDEX idx_memories_session ON memories(session_id);
CREATE INDEX idx_memories_branch ON memories(repo_identity, branch);
```

### Migration for existing data

Existing memories get `memory_kind = NULL` (not 'state'). They participate in search but are excluded from state-priority ranking until explicitly classified. No silent misclassification.

## Hook Events and Payload

### Events we handle

| Event | When | Memory action |
|---|---|---|
| **SessionStart** | Conversation begins or resumes | Open/resume memory session. `source` field: "resume", "startup", "clear", "compact" |
| **UserPromptSubmit** | User sends message | Update `last_prompt_at`, extract objective candidate |
| **Stop** | Claude finishes turn | Append delta from `last_assistant_message` + pane state |
| **PreCompact** | Before context compaction | Opportunity for incremental compression of recent deltas |
| **Notification** | Permission prompt | Track interruptions |

### Payload fields (verified)

```json
{
  "session_id": "780c547c-...",              // conversation UUID, persists across --continue
  "transcript_path": "~/.claude/.../abc.jsonl", // full conversation transcript
  "cwd": "/Users/spok/repos/project",       // current working directory
  "permission_mode": "default",              // default|plan|dontAsk
  "hook_event_name": "SessionStart|UserPromptSubmit|Stop|PreCompact|Notification",
  "prompt": "fix the auth bug",             // user message (UserPromptSubmit only)
  "last_assistant_message": "I've fixed...", // Claude's response (Stop only)
  "source": "resume"                        // SessionStart only: resume|startup|clear|compact
}
```

### How we use each field

- **`session_id`** -- Durable conversation thread ID (like LangGraph's `thread_id`). Used to group all runs/segments of one conversation. NOT used alone to detect resume — use `SessionStart.source` instead.
- **`source`** (SessionStart) -- Authoritative lifecycle signal. `"resume"` = `--continue`, `"startup"` = new session, `"clear"` = context cleared, `"compact"` = after compaction.
- **`transcript_path`** -- Full conversation JSONL. Read at session close for final LLM compression pass. Also available for incremental extraction on PreCompact.
- **`cwd`** -- Immediate repo_identity resolution on every event. More reliable than git scanner polling.
- **`prompt`** (UserPromptSubmit) -- Candidate session objective (first prompt). Low-confidence for vague starts ("continue", "pick up where I left off"). Revised after 1-3 turns if initial extraction is weak. User can override explicitly.
- **`last_assistant_message`** (Stop) -- High-value hint for delta extraction (decisions, files, rationale). NOT the sole delta source — supplement with pane state (CWD changes, permission interruptions, tool attempts not in prose, subagent activity).
- **`permission_mode`** -- Differentiate plan-mode sessions from implementation sessions.

## Hook Transport: Zero-Latency Writes

The hook fires synchronously — if slow, it blocks Claude Code. All memory writes must be non-blocking.

### Design: local file append + async daemon sync

```
Hook script → append JSON line to /tmp/clmux-deltas-<pane_id>.jsonl  (~0.1ms)
  ↓ (async, daemon reads periodically)
clmuxd → reads file (1s interval or inotify/kqueue)
  → processes deltas
  → local: writes to Memora directly
  → remote: batches and sends via SSH tunnel to local Memora
```

**Why not Unix socket on hot path:**
- Local: ~5ms (acceptable but unnecessary)
- Remote via SSH tunnel: ~50-200ms (blocks Claude Code)
- Socket down: hook hangs until timeout

**Why local file:**
- ~0.1ms append (never blocks)
- Works identically on local and remote hosts
- If daemon is down, deltas queue in the file
- If tunnel is down (remote), deltas queue until recovery
- Daemon reads and flushes asynchronously

### event_seq assignment and replay safety

`event_seq` is **server-assigned** (by Memora), not client-assigned (by the hook or daemon). The daemon does NOT include event_seq in JSONL records. Flow:
1. Hook appends raw event JSON to JSONL file (no seq, no delta_id)
2. Daemon reads new lines, tracks file offset in a persistent cursor file (`/tmp/clmux-deltas-<pane_id>.cursor`)
3. Daemon assigns a `delta_id` to each line: `<pane_id>:<file_inode>:<gen>:<byte_offset>` — stable, transport-level identity derived from file position. `gen` is a generation counter stored in the cursor file. Bumped on any file-identity discontinuity: file size < cursor offset, OR file header mismatch (daemon writes a magic+timestamp line as first line of each new JSONL file; cursor stores this header hash). This handles truncate-and-refill while daemon is down.
4. Daemon calls `session_delta(session_id, structured_facts, delta_id)` via MCP
5. Memora checks UNIQUE(session_id, delta_id). If conflict → return existing event_seq (idempotent). Otherwise assign `event_seq = sessions.next_event_seq`, increment atomically.
6. On daemon restart: reads cursor file (contains offset + gen + header_hash). Checks file header against stored hash. If mismatch OR file size < cursor offset → bumps gen, reprocesses from start. delta_id dedup (with new gen) prevents false collisions even for truncate-and-refill scenarios.
7. Identical structured_facts from separate events get separate delta_ids (different byte offsets), so legitimate duplicate events are preserved.

### Hook script (updated)

```bash
*'"hook_event_name":"SessionStart"'*)
    # Extract source and session_id, write to delta file
    echo "$EVENT" >> /tmp/clmux-deltas-${CLMUX_PANE_ID}.jsonl
    "$CLMUX" activity pulse --auto 2>/dev/null
    ;;
*'"hook_event_name":"UserPromptSubmit"'*)
    echo "$EVENT" >> /tmp/clmux-deltas-${CLMUX_PANE_ID}.jsonl
    "$CLMUX" status clear --auto __prompt
    "$CLMUX" activity pulse --auto 2>/dev/null
    ;;
*'"hook_event_name":"Stop"'*)
    echo "$EVENT" >> /tmp/clmux-deltas-${CLMUX_PANE_ID}.jsonl
    "$CLMUX" status clear --auto __prompt
    "$CLMUX" activity stop --auto 2>/dev/null
    ;;
*'"hook_event_name":"PreCompact"'*)
    echo "$EVENT" >> /tmp/clmux-deltas-${CLMUX_PANE_ID}.jsonl
    ;;
*'"hook_event_name":"Notification"'*)
    echo "$EVENT" >> /tmp/clmux-deltas-${CLMUX_PANE_ID}.jsonl
    ;;
```

The activity dot commands (`pulse`/`stop`) still go through the Unix socket (fast, already proven). Memory deltas go to the file (fastest possible).

## Session Boundary Detection

Session boundaries use the `SessionStart` hook as the primary signal, with `session_id` for thread grouping and idle/PID as fallbacks.

### Session start detection

A `SessionStart` hook fires. The daemon reads `source` and `session_id`:

- **`source: "startup"`** -- New conversation. Close any old memory session for this pane, create a new one.
- **`source: "resume"`** -- `--continue` or resumed conversation. Find the existing memory session by `claude_session_id`. Create a new **run/segment** under the same conversation thread (append-only, never reopen a closed session).
- **`source: "clear"`** -- Context cleared mid-conversation. Close current segment, open a new one under the same thread.
- **`source: "compact"`** -- After compaction. Trigger incremental compression of recent deltas (PreCompact opportunity).

### Conversation threads vs memory sessions

One conversation (`claude_session_id`) can span multiple memory sessions (runs/segments):

```
claude --continue (resume)     claude --continue (resume)
    |                              |
    v                              v
[run 1: session A] → closed → [run 2: session B] → closed → [run 3: session C]
|<-------------- same claude_session_id, three memory sessions ------------->|
```

Each run is an independent memory session with its own deltas, close lifecycle, and branch_state CAS. The `claude_session_id` groups them for retrieval — "show me everything from this conversation."

### Session start data capture

On `SessionStart` for a new session/run:
- `claude_session_id` from payload (links runs in one conversation)
- `transcript_path` stored for later LLM compression; `transcript_start_line` set to current transcript line count (0 for new conversations)
- `repo_identity` resolved from `cwd` field (immediate, no polling wait)
- `branch` and `head_commit` from **fresh git lookup** at daemon ingest time (`git rev-parse --abbrev-ref HEAD` + `git rev-parse HEAD` in the pane's CWD). NOT from the 5s scanner cache — the user may have just checked out a new branch.
- `base_snapshot_revision` from `branch_state` table for the resolved `(repo_identity, branch)`
- Objective: candidate extracted from first `prompt` in subsequent `UserPromptSubmit`. Revised after 1-3 turns if vague. User can override.

### Session end detection

Multiple signals, combined:

1. **Idle timeout** -- No activity event fires for X minutes after the last event. Timer checks: "has it been >N minutes since last_event_at AND is the turn closed (last Stop >= last UserPromptSubmit)?" If the turn is still open (UserPromptSubmit fired but no Stop yet), do NOT close — the agent may be in a long-running operation. Only close on idle timeout while turn is open if PID is dead (heartbeat failure).

2. **Process exit** -- Detected via pane PID ancestry tracking, not string-matching `pane_current_command`. The daemon records the PID of the agent process at session start (from hook-maintained session PID). When the PID exits (checked via `kill(pid, 0)` or `/proc/<pid>`), trigger session close. More robust than command-name matching.

3. **Next session start** -- Finalize the previous session when a new `UserPromptSubmit` fires after a long gap. Fallback for missed idle timeout (e.g., daemon restart).

4. **Repo/branch change** -- Detected via git scanner polling (already runs every 5s), not at delta time. When the scanner detects a pane's `(repo_identity, branch)` changed, it immediately closes the current session and opens a new one — even mid-turn. If a turn spans a scope change (edited repo A then switched to B before Stop), the session is closed at detection time and the Stop delta is attributed to the new session. Any deltas already written to the old session stay there — they were correct at write time.

5. **Explicit command** -- User runs `/snapshot` or `clmux memory snapshot` manually.

6. **Daemon startup recovery** -- On daemon start, sweep for orphaned sessions (state='open' or state='closing', no recent activity). For 'closing': resume from `close_phase`. For 'open': close with a "recovered" flag.

### Turn vs session

Multiple `UserPromptSubmit`/`Stop` cycles with short gaps between them are the **same session**, different turns. Only when the gap exceeds the idle timeout (or the process exits) does the session end.

```
UserPromptSubmit → Stop → (2 min) → UserPromptSubmit → Stop → (45 min) → session close
|<-------------- same session, two turns ------------->|                    |
                                                                    idle timeout fires
```

### Existing infrastructure

- `agent_active_at` timestamp per workspace (socket_server.zig) -- extend to `last_event_at`
- `PollEventLoop` timer callbacks (event_loop.zig) -- same pattern as port/git scanners
- `clmux-hook.sh` processes UserPromptSubmit/Stop/Notification events

## Memory Lifecycle

### Session start
1. First `UserPromptSubmit` after idle gap or process restart
2. clmux daemon creates session record via Memora (with repo_identity, branch, head_commit)
3. Load state snapshot for current (repo_identity, branch) -- small, always loaded
4. Load session index (one-liners, scannable)
5. Full episodic archive available on-demand, not preloaded

### During session (each `Stop` event)
1. Daemon reads delta from `/tmp/clmux-deltas-<pane>.jsonl` (written by hook, ~0.1ms)
2. Extract structured delta from multiple sources:
   - `last_assistant_message` -- high-value hint: decisions stated, files mentioned, todos created
   - `cwd` from payload -- repo/branch context
   - Pane state from git scanner -- actual CWD changes, branch switches, uncommitted files
   - These are complementary: `last_assistant_message` captures intent, pane state captures reality
3. Append to `session_deltas` table with event_seq for idempotency
4. No LLM inference, no embedding computation -- structured regex/pattern extraction only
5. Deltas are durable immediately (survive daemon restart) and queryable

### On PreCompact
1. Opportunity for incremental compression of recent deltas
2. Summarize deltas accumulated since last compaction into a mini-snapshot
3. Reduces work needed at session close

### Session close (idle timeout, process exit, or next session start)

Session close follows an exact-once state machine: `open -> closing -> closed`. The transition from `open` to `closing` is atomic (CAS on session state). If two triggers race (e.g., idle timeout + next session start), only one succeeds.

1. **Transition session state**: `open -> closing` (CAS, fails if already closing/closed). Persist `close_phase` integer (0-5) for crash recovery.
2. **Structured extraction** (phase 1): branch, head commit, active bug, open todos, touched files, constraints -- derived from accumulated deltas and `last_assistant_message` payloads. Store in session row as `close_payload` JSON.
3. **LLM compression** (phase 2, async): three-layer input:
   - Accumulated deltas (structured, from turns)
   - PreCompact mini-snapshots (if any)
   - `transcript_path` JSONL, sliced to `[transcript_start_line, transcript_end_line]` — only this session's turns, not the whole conversation thread. `transcript_end_line` is set at close time from the current transcript length.
   Extract rationale, tradeoffs, dead ends. Append to `close_payload`. If transcript is missing/truncated, fall back to deltas only.
4. **Branch state CAS** (phase 3): write to `branch_state` table. Uses `base_snapshot_revision` from the `sessions` row (persisted at session start, survives crash recovery).
   - **Existing branch**: `UPDATE branch_state SET snapshot=?, snapshot_revision=snapshot_revision+1, session_id=?, updated_at=? WHERE repo_identity=? AND branch=? AND snapshot_revision=?` (base revision from sessions row). If 0 rows updated → `sessions.conflict = 1`.
   - **New branch** (`base_snapshot_revision IS NULL`): `INSERT OR IGNORE INTO branch_state (repo_identity, branch, snapshot, snapshot_revision, session_id) VALUES (?, ?, ?, 1, ?)`. If INSERT was ignored (PK conflict from concurrent first-write), retry as existing-branch CAS. If the CAS also fails → `sessions.conflict = 1`. This handles the race where two sessions both start with no existing row.
5. **Append episodic** (phase 4): insert to `memories` with `memory_kind='episodic'`. Uses `INSERT ... WHERE NOT EXISTS (SELECT 1 FROM memories WHERE session_id=? AND memory_kind='episodic')` to prevent duplicate episodic rows on crash recovery. The `(session_id, memory_kind)` pair is effectively unique for episodic rows.
6. **Write session summary** (phase 5): update `sessions.summary` with one-liner index and `sessions.outcome` with outcome text.
7. **Transition session state**: `closing -> closed`, set `closed_at` (phase complete).
8. Clean up `session_deltas` for this session (or archive).

**Crash recovery**: on daemon startup, find sessions with `state='closing'`. Read `close_phase` and `base_snapshot_revision` from the session row, resume from the failed step. Each phase is idempotent: CAS for branch_state (idempotent — same base revision yields same result), UPSERT for episodic (keyed by session_id).

## New MCP Tools

```
session_start(repo_identity, branch, head_commit?, objective?, claude_session_id?, pane_id?, workspace_id?, transcript_path?, transcript_start_line?, start_key?) -> session_id
  -- Creates session row. Reads base_snapshot_revision from branch_state for the given (repo_identity, branch).
  -- Initializes next_event_seq = 1. Returns session_id (uuid).
  -- Replay-idempotent: caller provides start_key (same delta_id format: pane_id:inode:offset of the SessionStart JSONL line).
  -- If a session with this start_key already exists, returns the existing session_id (idempotent).
  -- start_key is stored on the sessions row for dedup.

session_delta(session_id, structured_facts, delta_id) -> event_seq
  -- Fast write to session_deltas. Server assigns event_seq from sessions.next_event_seq, increments atomically.
  -- No FTS, no embeddings, no crossrefs, no cloud sync.
  -- Retry-idempotent: caller provides delta_id (daemon-assigned UUID or pane_id:file_inode:byte_offset).
  -- UNIQUE(session_id, delta_id) catches retries where the write succeeded but response was lost.
  -- If delta_id conflicts, returns the existing event_seq (idempotent success, not error).
  -- Note: delta_id is a transport-level identity, NOT content-based. Identical structured_facts
  -- from separate events get separate delta_ids and separate event_seqs.

session_close(session_id, summary?, outcome?) -> {state, conflict}
  -- Triggers exact-once close state machine. CAS on sessions.state (open -> closing).
  -- Runs close phases using base_snapshot_revision from sessions row (server-side, not caller-supplied).
  -- Returns final state and whether CAS conflict occurred on branch_state.

session_list(repo_identity?, branch?) -> [{session_id, summary, objective, outcome, branch, started_at, closed_at, conflict}]
  -- Lists sessions from sessions table. Filtered by repo_identity and/or branch.
  -- summary field is the one-liner index (lives in sessions.summary, not a separate table).

session_get(session_id) -> {session row + deltas}
  -- Full session detail including all deltas.

branch_state_get(repo_identity, branch) -> {snapshot, snapshot_revision, updated_at}
  -- Direct lookup of current truth for a branch.
```

## Retrieval: Scoped Search Orchestrator

Scoped retrieval is a **new orchestrator function** (`scoped_search` in `storage.py`), not a modification to `hybrid_search`. It queries three separate data sources and merges results:

### Data sources

1. **`session_deltas`** — structured JSON, searched by field matching (not semantic). Immediate context for the current session.
2. **`branch_state`** — dedicated table, direct lookup by `(repo_identity, branch)`. Single row, always loaded. This is the authoritative "current truth", NOT `memory_kind=state` in `memories`.
3. **`memories`** — existing hybrid search (FTS + semantic). Filtered by `repo_identity` and optionally `branch` when scope is narrower than global. `memory_kind` used only for ranking episodic summaries, not for state lookup.

### --scope=auto (default)
1. Load `branch_state` snapshot for current `(repo_identity, branch)` — always first, always cheap
2. Search `session_deltas` for current session (field match on session_id)
3. If coverage weak → `hybrid_search` on `memories` filtered by `(repo_identity, branch)` with `memory_kind IN ('episodic', NULL)`
4. If still weak → `hybrid_search` on `memories` filtered by `repo_identity` only (all branches)
5. If still weak → `hybrid_search` on all `memories` (global fallback)

### --scope=project
Skip session deltas. Load branch_state + search `memories` filtered by `repo_identity` (all branches).

### --scope=global
Search all `memories` via `hybrid_search`, ranked by semantic similarity. No session or branch filtering.

### Ranking
`branch_state` > session deltas > recent episodic (kind='episodic') > pre-migration (kind=NULL) > artifact (kind='artifact').

## clmux Integration

### clmux-hook.sh changes

```bash
*'"hook_event_name":"UserPromptSubmit"'*)
    "$CLMUX" status clear --auto __prompt
    "$CLMUX" activity pulse --auto 2>/dev/null
    # New: signal session activity (daemon tracks timing for session boundaries)
    "$CLMUX" memory pulse --auto 2>/dev/null
    ;;
*'"hook_event_name":"Stop"'*)
    "$CLMUX" status clear --auto __prompt
    "$CLMUX" activity stop --auto 2>/dev/null
    # New: append session delta to Memora
    "$CLMUX" memory delta --auto 2>/dev/null
    ;;
```

### Daemon-side session management

Sessions are **pane-scoped**, not workspace-scoped. A workspace with two split panes in different repos has two independent sessions. The daemon tracks per-pane:

- `last_event_at` -- timestamp of last pulse or delta
- `last_prompt_at` -- timestamp of last UserPromptSubmit
- `last_stop_at` -- timestamp of last Stop
- `session_pid` -- PID of agent process
- `session_id` -- current active session
- `repo_identity` -- resolved from pane CWD's git remote

Timer callback in clmuxd event loop (same pattern as port scanner):
- `memory pulse` updates `last_event_at`, `last_prompt_at`, records agent PID at session start
- `memory delta` updates `last_event_at`, `last_stop_at`, appends structured delta with event_seq
- Timer checks every 60s per pane:
  - If turn is closed (`last_stop_at >= last_prompt_at`) AND `now - last_event_at > idle_timeout` -> trigger session close
  - If turn is open AND PID is dead (`kill(pid, 0)` fails) -> trigger session close
  - If turn is open AND alive -> do nothing (long-running operation)
- Daemon startup: sweep orphaned sessions (state='open', no recent activity) and close them

### New `clmux memory` subcommands
- `clmux memory pulse --auto` -- signal session activity (timestamp + PID update)
- `clmux memory delta --auto` -- extract structured state from current pane, send to Memora
- `clmux memory snapshot --auto` -- trigger full session close with LLM compression
- `clmux memory search <query> [--scope auto|project|global]` -- scoped search

### Repo identity resolution
clmux reads git remote origin from the pane's CWD (already tracked by git scanner), normalizes and hashes it. For repos with no remote, uses repo root path hash. User can override via `clmux workspace set-repo-alias`.

## Cross-Project Memory

- All memories in one SQLite DB, scoped by `repo_identity`
- Patterns learned in project A findable from project B via global semantic search
- No separate DBs per project -- one store, scoped queries
- Branch-scoped state prevents cross-branch contamination within a project

## MVP Backend Scope

**MVP is local SQLite only.** D1 and cloud-synced backends are explicitly excluded from session memory features until post-MVP. Rationale: D1 auto-commits every statement and has no rollback, making the exact-once close lifecycle impossible without significant workarounds. Cloud sync triggers on every write, incompatible with high-frequency delta writes.

Session tables (`sessions`, `session_deltas`, `branch_state`) are created only when the backend is `LocalSQLiteBackend`. Session MCP tools return a clear error ("session memory requires local SQLite backend") on D1/cloud backends.

### Future: Remote and Cloud

Remote hosts (SSH, containers, VMs) push memory to local clmuxd via SSH reverse tunnel (same as clipboard). Cloud (D1) deferred to post-MVP with authn/authz, tenant isolation, and replay protection.

## Dedicated Session Storage Path

Session operations (`session_delta`, `session_close`) bypass the standard memory write pipeline entirely. They do NOT trigger:
- FTS updates (deltas are structured JSON, not searchable text)
- Embedding computation (no semantic search on raw deltas)
- Crossref calculation
- Cloud sync (`sync_after_write()`)
- Event emission

This is implemented as a separate set of storage functions in `storage.py` (`session_*` functions) that write directly to the session tables. Only at session close, when the compressed episodic summary is written to `memories`, does the standard pipeline activate (FTS + embeddings + crossrefs).

## Foreign Key Enforcement

Add `PRAGMA foreign_keys = ON` to `schema.py:ensure_schema()` for all local SQLite connections (not D1, which doesn't support it). Required for `ON DELETE CASCADE` on `session_deltas`.

## Risk Mitigations

| Risk | Mitigation |
|---|---|
| Write amplification | Separate session_deltas table + dedicated storage path (no FTS/embeddings/sync) |
| Search pollution | memory_kind filtering in scoped_search orchestrator, graph excludes session artifacts |
| Identity drift | Canonical repo_identity with fallback + user override |
| Concurrency | snapshot_revision CAS on state writes, merge on conflict |
| Secret leakage | MEMORA_IGNORE + content scanning + no cloud without auth |
| Stale memories | Branch-scoped state, head_commit tracking, conflict detection on start |
| Token cost | LLM compression only on session close, not every turn |
| Backward compat | New columns nullable, existing memories keep kind=NULL |
| Tunnel down | Hook fails silently. Deltas lost but non-critical |
| D1/cloud backend | Session tools gated by `supports_sessions` — clear error, no silent failure |
| Foreign key cascade | `PRAGMA foreign_keys = ON` enforced on local SQLite connections |
| Crash recovery | Daemon startup sweeps orphaned sessions, idle timeout not dependent on Stop |
| Duplicate writes | event_seq + UNIQUE constraint on session_deltas |

## Implementation Order

### Phase A: Memora-side (this repo)

1. **Backend capability gate** -- Add `supports_sessions` property to backend classes. `LocalSQLiteBackend` returns True, others False. Session MCP tools check this before operating.
2. **Foreign key enforcement** -- Add `PRAGMA foreign_keys = ON` to `ensure_schema()` for local SQLite connections.
3. **Schema migration** -- New tables (`sessions`, `session_deltas`, `branch_state`) + new columns on `memories` (`repo_identity`, `session_id`, `branch`, `head_commit`, `memory_kind`, `snapshot_revision`). All additive, non-breaking. Existing memories keep `memory_kind = NULL`.
4. **Dedicated session storage functions** -- `session_start()`, `session_delta()`, `session_close()`, `session_list()`, `get_branch_state()` in `storage.py`. These bypass FTS/embeddings/crossrefs/sync. `session_close` implements the exact-once state machine with crash-resumable `close_phase`.
5. **Session MCP tools** -- Expose session storage functions as MCP tools in `server.py`. Backend gate check on each tool.
6. **Scoped search orchestrator** -- New `scoped_search()` function in `storage.py` that queries `branch_state`, `session_deltas`, and `memories` with tiered fallback. New `memory_scoped_search` MCP tool.
7. **Export/import extension** -- Extend `export_memories()` and `import_memories()` to include `sessions`, `session_deltas`, `branch_state` tables. Add `memory_kind` filter to graph data loading to exclude raw session artifacts.
8. **Tests** -- Unit tests for all session storage functions, scoped search, backend gating, export/import with session data.

### Phase B: clmux-side (separate repo, dependency on Phase A)

9. **Repo identity resolution** -- git remote hash utility in clmux with no-remote fallback
10. **Hook integration** -- Hook writes to `/tmp/clmux-deltas-<pane>.jsonl`, daemon reads async and calls Memora session MCP tools
11. **Session boundary detection** -- idle timeout + PID-based exit detection in clmuxd event loop
12. **Manual snapshot** -- `clmux memory snapshot` command

### Phase C: Enhancements (post-MVP)

13. **LLM compression** -- async summarization at session close (transcript + deltas)
14. **Conflict detection** -- compare snapshot vs current repo state on session start
15. **Remote memory forwarding** -- SSH tunnel setup
16. **Cloud deployment** -- D1 mode with security model

## Industry References

- **Letta (MemGPT):** Active RAM model, agent manages own core memory + archival state
- **Zep:** Temporal knowledge graph, episodic vs semantic, tracks when facts were true
- **mem0:** Smart fact extraction, auto-updates across User/Session/Agent levels
- **LangGraph:** Thread memory (session) vs namespace long-term memory (project)
- **Zep/Graphiti (Nov 2025):** Deterministic extraction on hot path, LLM as fallback only
- Claude Code MEMORY.md: ~200 line scratchpad, single project, no semantic search

## Design Decisions (frozen)

1. **State is branch-scoped**, not project-wide. Prevents cross-branch contamination and last-writer-wins corruption.
2. **Session deltas live in a separate table**, not in memories. Avoids triggering embeddings/FTS/crossrefs on every turn.
3. **Remote writes terminate at clmuxd** (Mode 1). Single transport, consistent semantics. No direct Memora HTTP forwarding in MVP.
4. **Existing memories get kind=NULL**, not 'state'. No silent misclassification.
5. **Turn deltas are objective signals only** (branch, files, todos). No LLM inference per turn — rationale extraction deferred to session close.
6. **Sessions are pane-scoped**, not workspace-scoped. A workspace with split panes in different repos has independent sessions per pane.
7. **Session close is exact-once** via state machine (open -> closing -> closed). CAS on state transition prevents double-summarize.
8. **Forks get their own repo_identity** (fork URL differs from upstream). Users can manually alias two identities for shared memory.
9. **Idle close is gated on turn state**. Do not finalize while a turn is open (UserPromptSubmit without Stop) unless PID is dead.
10. **Branch state is full-replace with CAS**. Dedicated `branch_state` table. Close uses `UPDATE ... WHERE snapshot_revision = base_revision` (CAS). If revision moved (concurrent close), the session is marked `conflicted` — its episodic summary is still written but branch_state is not updated. For new branches (no existing row): `INSERT` with revision=1. No merge, no tombstones.
11. **Repo/branch change auto-closes session**. If pane CWD or git branch changes mid-session, close current and open new. Prevents mixed-scope deltas.
12. **Session close is crash-resumable**. `close_phase` integer tracks progress. Daemon startup resumes stale `closing` sessions from the failed step.
13. **Hook writes go to local file, not socket**. `echo >> /tmp/clmux-deltas-<pane>.jsonl` (~0.1ms, never blocks Claude Code). Daemon reads async.
14. **Never reopen a closed session**. `--continue` creates a new run/segment under the same `claude_session_id`. Append-only.
15. **SessionStart hook is the authoritative lifecycle signal**. `source` field distinguishes startup/resume/clear/compact. Not inferred from `session_id` comparison.
16. **`last_assistant_message` is a hint, not sole delta source**. Supplemented with pane state for CWD changes, tool attempts, subagent activity.
17. **Objective extraction is low-confidence**. Candidate from first prompt, revised after 1-3 turns, user can override.

## Open Questions

- Token budget for LLM compression at session close -- how much is acceptable?
- Idle timeout duration for auto session close -- 5min? 15min? 30min?
- Should session start auto-load context into Claude's conversation, or just make it searchable?
- How to handle submodules (nested repo_identities within a pane)?
- Compression model: use the same Claude model the agent is running, or a cheaper/faster model?

## Review History

- **v1** -- Initial draft from research synthesis
- **v2** -- Addressed codex review findings:
  - Added first-class identity model (repo_identity, branch, snapshot_revision, event_seq)
  - Branch-scoped state instead of project-wide (fixes last-writer-wins corruption)
  - Separate session_deltas table (fixes write amplification and retrieval-before-close gap)
  - PID-based process exit detection instead of command-name matching
  - Daemon startup recovery sweep for orphaned sessions
  - Idle timeout based on last_event_at, not dependent on Stop specifically
  - CAS on state overwrites with snapshot_revision
  - Existing memories get kind=NULL, not 'state' (no silent misclassification)
  - Frozen transport decision: remote writes go through clmuxd only
  - Cloud mode deferred to post-MVP with security model requirement
  - Moved conflict detection earlier in implementation order
  - Added event_seq for idempotent delta ingestion
- **v3** -- Addressed codex v2 review findings:
  - Session close is exact-once via state machine (open -> closing -> closed) with CAS
  - Idle close gated on turn state: don't close while turn is open unless PID is dead
  - Sessions are pane-scoped, not workspace-scoped (fixes multi-repo workspace issue)
  - Fork identity frozen: forks get own identity, manual aliasing for shared memory
  - State merge semantics defined: field-level merge for structured, append for narrative
  - Episodic/index writes keyed by session_id (UNIQUE) to prevent double-append
- **v4** -- Addressed codex v3 review findings:
  - Session close is crash-resumable: close_phase integer tracks progress, daemon resumes stale closing sessions
  - Repo/branch change mid-pane auto-closes session and opens new one (prevents mixed-scope deltas)
  - Dedicated branch_state table with PRIMARY KEY (repo_identity, branch) — no duplicate state rows possible
  - Full-replace semantics for branch state (not merge) — resolved items are absent, no tombstone needed
  - Retrieval updated to read from branch_state directly for Tier 1
- **v6** -- Hook payload integration:
  - Documented verified Claude Code hook payload fields (session_id, transcript_path, cwd, prompt, last_assistant_message)
  - session_id from payload used for conversation identity (detects --continue/resume)
  - Delta extraction uses last_assistant_message (Claude's actual output, not pane state inference)
  - LLM compression reads transcript_path for full conversation context
  - Session objective auto-extracted from first prompt field
  - cwd used for immediate repo_identity resolution
  - Sessions table gets claude_session_id, transcript_path, close_phase, close_payload columns
- **v7** -- Hook transport and lifecycle refinements (codex v6 review):
  - Zero-latency hook writes: local file append (~0.1ms), daemon reads async
  - SessionStart hook with source field (resume/startup/clear/compact) as authoritative lifecycle signal
  - Never reopen closed sessions — --continue creates new run/segment under same thread
  - last_assistant_message as hint, not sole delta source — supplemented with pane state
  - PreCompact hook for incremental compression opportunity
  - Three-layer LLM compression input: deltas + mini-snapshots + transcript
  - Objective extraction is low-confidence candidate, revised after 1-3 turns
- **v5** -- Addressed codex v4 review findings:
  - Branch state uses CAS (base_snapshot_revision captured at session start), not INSERT OR REPLACE
  - Out-of-order closes marked as conflicted, don't publish to branch_state
  - Repo/branch change detected by git scanner polling (5s), not at delta time
  - Mid-turn scope changes handled by closing session at detection, not waiting for Stop
- **v8** -- Implementation feasibility review (codex deep-work review):
  - MVP explicitly scoped to local SQLite only — D1/cloud excluded via `supports_sessions` backend gate
  - Dedicated session storage path bypasses FTS/embeddings/crossrefs/sync (fixes write amplification)
  - Scoped retrieval is a new orchestrator (`scoped_search`), not a `hybrid_search` modification
  - Queries `branch_state` (dedicated table) for current truth, NOT `memory_kind=state` in memories
  - Fixed branch-state semantic inconsistency: CAS-based writes, not INSERT OR REPLACE
  - Added `PRAGMA foreign_keys = ON` for local SQLite (required for ON DELETE CASCADE)
  - Export/import extended to cover session tables; graph data filters session artifacts
  - Implementation split into Phase A (Memora), Phase B (clmux), Phase C (enhancements)
  - Implementation reordered: backend gate → schema → storage → tools → retrieval → tests
- **v9** -- Addressed codex v8 iteration 3 findings:
  - session_delta retry-idempotent via content_hash (SHA-256 of structured_facts). UNIQUE(session_id, content_hash) catches retries where write succeeded but response was lost.
  - Episodic memory insertion uses INSERT ... WHERE NOT EXISTS to prevent duplicate episodic rows on crash recovery
  - branch_state first-write race handled: INSERT OR IGNORE + retry as CAS update. Two sessions starting with no existing row both get clean conflict handling.
  - Notification event added to hook JSONL script (was missing, drops permission/interruption context)
- **v10** -- Addressed codex v9 iteration 4 findings:
  - Transcript bounds per session: transcript_start_line/transcript_end_line on sessions table. LLM compression only reads this session's slice, not the whole thread.
  - Replaced content_hash with transport-level delta_id (pane_id:file_inode:byte_offset). Identical structured_facts from separate events get separate delta_ids. UNIQUE(session_id, delta_id) for retry safety.
  - Session start uses fresh git lookup (git rev-parse) at daemon ingest time, not 5s scanner cache. Prevents stale branch/base binding.
- **v11** -- Addressed codex v10 iteration 5 findings:
  - session_start replay-idempotent via start_key (same format as delta_id). Duplicate SessionStart lines return existing session_id.
  - delta_id includes generation counter (gen) to handle in-place truncation. Format: pane_id:inode:gen:offset. Daemon increments gen when file size < cursor offset.
- **v12** -- Addressed codex v11 iteration 6 findings:
  - start_key column now has UNIQUE constraint (was just TEXT). INSERT OR IGNORE + return existing semantics.
  - File identity uses header hash (magic+timestamp first line) in addition to size check. Cursor stores offset+gen+header_hash. Detects truncate-and-refill even when file grows past old offset.
