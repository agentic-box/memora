# Plan Review Log: Session Memory

**Reviewer:** codex (rv-3)
**Iterations:** 6
**Final verdict:** All High/Medium findings resolved. Remaining Low findings are in Phase B (clmux transport layer).

## Iteration 1: Initial review
- [High] D1 backend incompatible with exact-once close lifecycle → **Fixed: MVP scoped to local SQLite only**
- [High] Scoped retrieval needs new orchestrator, not hybrid_search tweak → **Fixed: new scoped_search function**
- [High] Session deltas need separate storage path (no FTS/embeddings/sync) → **Fixed: dedicated session_* storage functions**
- [Medium] branch_state semantic inconsistency → **Fixed: CAS everywhere, branch_state as source of truth**
- [Medium] Export/import doesn't cover session tables → **Fixed: extended**
- [Medium] Foreign keys not enabled → **Fixed: PRAGMA foreign_keys = ON**
- [Medium] Hook/daemon infra is clmux-side → **Fixed: split Phase A/B/C**
- [Low] Implementation order → **Fixed: reordered**
- [Low] MVP scope too large → **Fixed: trimmed**

## Iteration 2
- [High] event_seq not specified end-to-end → **Fixed: server-assigned from sessions.next_event_seq**
- [Medium] CAS base revision not persisted → **Fixed: base_snapshot_revision on sessions row**
- [Medium] Stale contradictory sections → **Fixed: cleaned up memory_kind, retrieval text**
- [Medium] Session index has no backing store → **Fixed: lives in sessions.summary column**
- [Low] Conflicted state not modeled → **Fixed: sessions.conflict INTEGER DEFAULT 0**

## Iteration 3
- [High] content_hash aliases legitimate duplicates → **Fixed: replaced with transport-level delta_id**
- [Medium] Episodic insert not crash-safe → **Fixed: INSERT ... WHERE NOT EXISTS**
- [Medium] branch_state first-write race → **Fixed: INSERT OR IGNORE + CAS retry**

## Iteration 4
- [High] Transcript breaks run/segment isolation → **Fixed: transcript_start_line/transcript_end_line bounds**
- [Medium] content_hash vs delta_id → **Fixed: pane_id:inode:gen:offset format**
- [Medium] Stale branch at session start → **Fixed: fresh git lookup, not scanner cache**

## Iteration 5
- [High] SessionStart not replay-idempotent → **Fixed: start_key with UNIQUE constraint**
- [Medium] Truncation collision with delta_id → **Fixed: generation counter + header hash**

## Iteration 6
- [High] start_key needs UNIQUE constraint → **Fixed**
- [Medium] File identity needs more than size check → **Fixed: header hash in cursor**
