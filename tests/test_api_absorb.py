"""API2: the /api/v1 absorb executor on a local primary.

docs/api-v1-writes.md, plan §3.2. Offline local SQLite. The claim plus the
fenced `done` row written in the absorb's own transaction make the absorb
exactly-once: a `done` row with a stored response_json is the only completion
boundary.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from memora import api_absorb, api_v1, storage
from memora.backends import StoreWriteAborted
from scripts.memora_api_contract import schema_errors

KEY = "api2:key:1"


def _req(key=KEY, facts=("a brand new api2 fact",), project="memora", source="landing"):
    return {"idempotency_key": key, "facts": list(facts), "project": project, "source": source}


def _row(conn, key=KEY):
    return conn.execute(
        "SELECT status, owner, fence, response_json, request_sha256 "
        "FROM api_idempotency WHERE key = ?", (key,),
    ).fetchone()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


@pytest.fixture()
def no_hits(monkeypatch):
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])


def test_the_idempotency_table_is_created_and_the_schema_is_current(local_db):
    from memora import schema

    with storage.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_idempotency'"
        ).fetchone()
    assert schema.schema_pending(sqlite3.connect(str(storage.STORAGE_BACKEND.db_path))) == []


def test_store_is_transactional_is_false_for_refused_stores(local_db):
    assert storage.store_is_transactional("default") is True
    backend = storage.STORAGE_BACKEND
    storage.set_store_refusals({None: "data volume failed"})
    assert storage.store_is_transactional("default") is False
    storage.set_store_refusals({})
    backend.refused_reason = "store refused in this process"
    try:
        assert storage.store_is_transactional("default") is False
    finally:
        backend.refused_reason = None
    assert storage.store_is_transactional("default") is True


def test_store_is_transactional_is_false_on_a_malformed_registry(local_db, monkeypatch):
    def boom():
        raise storage.DatabaseRegistryError("bad registry")

    monkeypatch.setattr(storage, "database_registry", boom)
    assert storage.store_is_transactional("default") is False


def test_a_refused_named_local_store_is_unsupported(local_db, monkeypatch):
    path = str(storage.STORAGE_BACKEND.db_path)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"memora": path}))
    backend = storage.STORAGE_BACKEND
    monkeypatch.setattr(storage, "backend_for", lambda name: backend)
    backend.refused_reason = "store refused in this process"
    try:
        assert storage.store_is_transactional("memora") is False
    finally:
        backend.refused_reason = None


def test_a_startup_refused_named_store_is_unsupported(local_db, monkeypatch):
    path = str(storage.STORAGE_BACKEND.db_path)
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({"memora": path}))
    storage.set_store_refusals({"memora": "data volume failed"})
    # backend_for calls _raise_if_refused, so the data-volume refusal is caught.
    assert storage.store_is_transactional("memora") is False


def test_absorb_creates_and_records_a_done_row(local_db, no_hits):
    status, body = api_absorb.execute("default", _req(facts=("a fresh new fact",)))
    assert status == 200 and body["status"] == "done"
    assert body["created"] == 1 and body["memory_ids"] and body["took_ms"] >= 0
    assert schema_errors("absorb_response.json", body) == []  # the frozen contract
    with storage.connect() as conn:
        row = _row(conn)
        assert row[0] == "done" and json.loads(row[3]) == body


def test_replay_returns_the_stored_body_without_re_embedding(local_db, no_hits, monkeypatch):
    calls = {"n": 0}
    real = storage._compute_embeddings_many

    def counting(items):
        calls["n"] += 1
        return real(items)

    monkeypatch.setattr(storage, "_compute_embeddings_many", counting)
    first = api_absorb.execute("default", _req(facts=("replay this exactly once",)))
    after_first = calls["n"]
    second = api_absorb.execute("default", _req(facts=("replay this exactly once",)))
    assert first == (200, first[1]) and second[0] == 200 and second[1] == first[1]
    assert calls["n"] == after_first  # no embedding (or any LLM) work on replay


def test_same_key_different_request_is_a_409_conflict(local_db, no_hits):
    assert api_absorb.execute("default", _req(facts=("the first request",)))[0] == 200
    status, body = api_absorb.execute("default", _req(facts=("a different request",)))
    assert status == 409 and body["error"] == "key_conflict"
    assert schema_errors("error_response.json", body) == []


def test_a_live_claim_answers_409_in_progress(local_db, no_hits):
    req = _req(facts=("a fact to hold the claim",))
    now = api_absorb._now_ms()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO api_idempotency(key, request_sha256, status, owner, fence, "
            "response_json, lease_until_ms, created_at_ms, updated_at_ms) "
            "VALUES (?, ?, 'in_progress', 'other', 0, NULL, ?, ?, ?)",
            (KEY, api_v1.canonical_request_sha256(req), now + 60000, now, now),
        )
        conn.commit()
    status, body = api_absorb.execute("default", req)
    assert status == 409 and body["error"] == "in_progress"
    assert schema_errors("error_response.json", body) == []


def test_an_expired_claim_is_taken_over_with_fence_plus_one(local_db, no_hits):
    req = _req(facts=("take this over please",))
    now = api_absorb._now_ms()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO api_idempotency(key, request_sha256, status, owner, fence, "
            "response_json, lease_until_ms, created_at_ms, updated_at_ms) "
            "VALUES (?, ?, 'in_progress', 'other', 3, NULL, ?, ?, ?)",
            (KEY, api_v1.canonical_request_sha256(req), now - 1, now, now),
        )
        conn.commit()
    status, body = api_absorb.execute("default", req)
    assert status == 200 and body["created"] == 1
    with storage.connect() as conn:
        row = _row(conn)
        assert row[0] == "done" and row[2] == 4  # fence+1


def test_a_stale_runner_cannot_commit_after_a_takeover(local_db, no_hits, monkeypatch):
    req = _req(facts=("the wedged runner fact",))
    real = api_absorb._fenced_done

    def stale(conn, key, owner, fence, body):
        # A takeover lands while the first runner is inside its transaction.
        conn.execute("UPDATE api_idempotency SET owner='taken', fence=fence+1 WHERE key=?", (key,))
        real(conn, key, owner, fence, body)  # the old owner+fence now matches nothing

    monkeypatch.setattr(api_absorb, "_fenced_done", stale)
    with pytest.raises(StoreWriteAborted):
        api_absorb.execute("default", req)
    with storage.connect() as conn:
        assert _count(conn) == 0  # every effect (and the in-tx takeover) rolled back
        row = _row(conn)
        # The run failed with the claim still ours and in_progress, so the
        # executor recorded it failed_clean (an explicitly retryable claim).
        assert row[0] == "failed_clean" and row[2] == 0

    # A fresh run takes over the failed_clean claim and commits exactly once.
    monkeypatch.setattr(api_absorb, "_fenced_done", real)
    status, body = api_absorb.execute("default", req)
    assert status == 200 and body["created"] == 1
    with storage.connect() as conn:
        assert _count(conn) == 1


def test_a_pure_skip_still_writes_done(local_db, no_hits):
    # "ab" is shorter than the minimum fact length, so absorb skips it before
    # phase 3; the done row must still be written (plan §3.2).
    status, body = api_absorb.execute("default", _req(facts=("ab",)))
    assert status == 200 and body["status"] == "done"
    assert body["created"] == 0 and body["skipped"] == 1 and body["memory_ids"] == []
    with storage.connect() as conn:
        assert _row(conn)[0] == "done"


def test_an_exception_after_the_done_commit_replays_the_stored_body(local_db, no_hits, monkeypatch):
    real = storage.absorb_memory

    def commit_then_raise(*a, **k):
        real(*a, **k)  # commits the fenced done row inside the absorb transaction
        raise RuntimeError("the ack was lost after the commit")

    monkeypatch.setattr(storage, "absorb_memory", commit_then_raise)
    status, body = api_absorb.execute("default", _req(facts=("the ack loss fact",)))
    assert status == 200 and body["status"] == "done"
    # A replay returns the same stored body, still without re-running anything.
    monkeypatch.setattr(storage, "absorb_memory", real)
    status2, body2 = api_absorb.execute("default", _req(facts=("the ack loss fact",)))
    assert status2 == 200 and body2 == body
