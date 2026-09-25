"""API2: the /api/v1 absorb executor on a local-primary (transactional) store.

Design: docs/api-v1-writes.md. Contract: contracts/memora-api/v1 (frozen).
`memora.api_v1` calls `execute` only for a store whose `writes_capability` is
"transactional". A claim in `api_idempotency` plus a fenced `done` update
inside the absorb's own transaction make the absorb exactly-once: a row with
status `done` and a stored `response_json` is the ONLY completion boundary
(plan §3.2). Phase 1/2 (LLM, embeddings, R2) run outside the write lock, as
L4 already enforces.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Tuple

from .backends import StoreWriteAborted, store_write

# How long a claim is held before a replay may take it over. Generous because
# phases 1-2 (LLM classification, embeddings) can run for minutes; it only
# bounds how long a WEDGED runner can block a retry.
LEASE_MS = int(os.getenv("MEMORA_API_IDEMPOTENCY_LEASE_MS", str(10 * 60 * 1000)))

# The frozen contract's error bodies (contracts/memora-api/v1/fixtures).
_BODY_KEY_CONFLICT = {"error": "key_conflict",
                      "message": "this idempotency key was used with a different request"}
_BODY_IN_PROGRESS = {"error": "in_progress",
                     "message": "another request holds this idempotency key; retry later"}

# Claim states returned by _claim (distinct from the bodies above).
_CLAIMED, _DONE, _CONFLICT, _IN_PROGRESS = "claimed", "done", "conflict", "in_progress"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_body(counts: Dict[str, Any], memory_ids: Any, took_ms: int) -> Dict[str, Any]:
    """The frozen `absorb_response` body, built once (and then stored verbatim,
    so a replay returns it byte-for-byte)."""
    return {
        "status": "done",
        "created": int(counts.get("created", 0)),
        "superseded": int(counts.get("superseded", 0)),
        "skipped": int(counts.get("skipped", 0)),
        "linked": int(counts.get("linked", 0)),
        "memory_ids": sorted(int(i) for i in memory_ids),
        "took_ms": int(took_ms),
    }


def _fenced_done(conn: sqlite3.Connection, key: str, owner: str, fence: int,
                 body: Dict[str, Any]) -> None:
    """Write the completion boundary. A 0-row update means another runner took
    the claim over; raising rolls the whole absorb transaction back."""
    cur = conn.execute(
        "UPDATE api_idempotency SET status='done', response_json=?, updated_at_ms=? "
        "WHERE key=? AND owner=? AND fence=?",
        (json.dumps(body), _now_ms(), key, owner, fence),
    )
    if cur.rowcount != 1:
        raise StoreWriteAborted(
            "the idempotency claim was taken over; the absorb was rolled back"
        )


def _claim(conn: sqlite3.Connection, key: str, sha: str) -> Tuple[str, Dict[str, Any], str, int]:
    """One short BEGIN IMMEDIATE claim. Returns (state, stored_body, owner,
    fence). A live claim held by another runner returns `_IN_PROGRESS`; a
    different request under the same key returns `_CONFLICT`; an expired
    `in_progress` or a `failed_clean` row is taken over with fence+1."""
    owner = uuid.uuid4().hex
    now = _now_ms()
    with store_write(conn):
        row = conn.execute(
            "SELECT request_sha256, status, owner, fence, response_json, lease_until_ms "
            "FROM api_idempotency WHERE key = ?", (key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO api_idempotency(key, request_sha256, status, owner, fence, "
                "response_json, lease_until_ms, created_at_ms, updated_at_ms) "
                "VALUES (?, ?, 'in_progress', ?, 0, NULL, ?, ?, ?)",
                (key, sha, owner, now + LEASE_MS, now, now),
            )
            return _CLAIMED, {}, owner, 0
        existing_sha, status, row_owner, fence, response_json, lease = (
            str(row[0]), str(row[1]), row[2], int(row[3]), row[4], row[5],
        )
        if status == "done":
            if existing_sha != sha:
                return _CONFLICT, {}, "", 0
            return _DONE, json.loads(response_json) if response_json else {}, "", 0
        if existing_sha != sha:
            return _CONFLICT, {}, "", 0
        if status == "in_progress" and lease is not None and int(lease) > now:
            return _IN_PROGRESS, {}, "", 0
        # Expired in_progress or failed_clean: takeover, fence+1. The
        # conditional WHERE cannot touch a different claim; under the write
        # lock no other writer can race it.
        fence += 1
        cur = conn.execute(
            "UPDATE api_idempotency SET status='in_progress', owner=?, fence=?, "
            "lease_until_ms=?, updated_at_ms=? WHERE key=? "
            "AND status IN ('in_progress','failed_clean') AND owner IS ? AND fence=?",
            (owner, fence, now + LEASE_MS, now, key, row_owner, fence - 1),
        )
        if cur.rowcount != 1:
            return _IN_PROGRESS, {}, "", 0
        return _CLAIMED, {}, owner, fence


def _memory_ids(result: Dict[str, Any]) -> List[int]:
    return [int(d["memory_id"]) for d in (result.get("decisions") or [])
            if d.get("action") in ("created", "consolidated") and isinstance(d.get("memory_id"), int)]


def _after_failure(conn: sqlite3.Connection, key: str, owner: str, fence: int):
    """A failure raised out of `absorb_memory`. A commit can be ambiguous and
    an exception can follow a successful done commit, so FIRST read the
    persisted claim: a `done` row means the absorb committed (a lost ack) and
    the stored body is the answer. Only a still-`in_progress` claim owned by
    this run may be marked `failed_clean`, and the conditional update can never
    overwrite `done`. Anything unreadable is left for replay/lease takeover."""
    try:
        row = conn.execute(
            "SELECT status, response_json FROM api_idempotency WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and str(row[0]) == "done":
        return 200, (json.loads(row[1]) if row[1] else {})
    if row is not None:
        try:
            with store_write(conn):
                conn.execute(
                    "UPDATE api_idempotency SET status='failed_clean', updated_at_ms=? "
                    "WHERE key=? AND owner=? AND fence=? AND status='in_progress'",
                    (_now_ms(), key, owner, fence),
                )
        except sqlite3.Error:
            pass
    raise


def execute(store: str, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Run one /api/v1 absorb: claim, absorb, fenced done. Returns the HTTP
    status and the JSON body. The caller has already validated `req`."""
    from .storage import absorb_memory, connect
    from .api_v1 import _bound_store, canonical_request_sha256

    key = req["idempotency_key"]
    sha = canonical_request_sha256(req)
    started = time.perf_counter()
    with _bound_store(store):
        conn = connect()
        try:
            state, stored, owner, fence = _claim(conn, key, sha)
            if state == _DONE:
                return 200, stored
            if state == _CONFLICT:
                return 409, dict(_BODY_KEY_CONFLICT)
            if state == _IN_PROGRESS:
                return 409, dict(_BODY_IN_PROGRESS)

            state_box: Dict[str, Any] = {}

            def phase3_done(c: sqlite3.Connection, counts: Dict[str, Any], memory_ids: List[int]) -> None:
                body = _build_body(counts, memory_ids, int((time.perf_counter() - started) * 1000))
                _fenced_done(c, key, owner, fence, body)
                state_box["body"] = body

            try:
                result = absorb_memory(
                    conn, req["facts"], source=req["source"], context=req.get("context"),
                    metadata=req.get("metadata"), tags=req.get("tags"), project=req["project"],
                    phase3_done=phase3_done,
                )
            except BaseException:
                return _after_failure(conn, key, owner, fence)

            if "body" in state_box:
                return 200, state_box["body"]
            # No effects: a pure skip returns before phase 3, so the done row is
            # written on its own (there are no effects to co-commit) under the
            # same fence.
            body = _build_body(result, _memory_ids(result), int((time.perf_counter() - started) * 1000))
            with store_write(conn):
                _fenced_done(conn, key, owner, fence, body)
            return 200, body
        finally:
            conn.close()
