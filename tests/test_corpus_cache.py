"""Tests for the process-local exact vector cache (memora absorb step 3).

The cache reuses the loaded corpus snapshot across absorb calls when the DB's
monotonic `embedding_change_epoch` is unchanged, so D1 is not re-read per
absorb. get_corpus_snapshot returns a PRIVATE copy-on-write fork; a full scan
runs only when the epoch changed (insert/update/delete/re-embed/model-epoch/
external writer) or the cache is empty. These tests assert both the fast path
(no full scan) and the fallback (the loader spy shows a re-scan ran), plus that
forks isolate mutations and the cache is invalidated after absorb writes.
"""

import pytest

import memora.storage as storage
from memora.backends import LocalSQLiteBackend

SEED = ["alpha one", "bravo two", "charlie three", "delta four", "echo five"]


def _cache_key(conn):
    from memora.embeddings import _store_cache_key
    model, _epoch = storage._corpus_meta(conn)
    return storage._corpus_cache_key_for(_store_cache_key(conn), model)


def _direct_insert(conn, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, ?, ?, ?)",
        (content, None, "[]", "2026-01-01 00:00:00"),
    )
    new_id = cur.lastrowid
    conn.execute(
        "INSERT INTO memories_embeddings(memory_id, embedding, representation, encoding_source) "
        "VALUES (?, '{}', 'empty', 'python')",
        (new_id,),
    )
    conn.commit()
    return new_id


@pytest.fixture()
def cached_db(tmp_path, monkeypatch):
    """A seeded store with the cache primed, plus a loader spy."""
    storage._corpus_cache.clear()
    backend = LocalSQLiteBackend(tmp_path / "cache.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    original = storage._load_corpus_snapshot
    loads = {"n": 0}

    def counting(conn, **kwargs):
        loads["n"] += 1
        return original(conn, **kwargs)

    monkeypatch.setattr(storage, "_load_corpus_snapshot", counting)
    with storage.connect() as conn:
        for content in SEED:
            storage.add_memory(conn, content=content, commit=True)
        storage.get_corpus_snapshot(conn)  # prime the cache
    loads["n"] = 0  # reset after priming
    yield {"backend": backend, "loads": loads}
    storage._corpus_cache.clear()


def test_cache_reused_when_fresh(cached_db):
    """A fresh epoch reuses the cached snapshot -- no full re-scan."""
    with cached_db["backend"].connect() as conn:
        got = storage.get_corpus_snapshot(conn)
    assert len(got) == len(SEED)
    assert cached_db["loads"]["n"] == 0, "mutation: fresh epoch still re-scans"


def test_cache_holds_across_absorb_calls_after_no_write(cached_db, monkeypatch):
    """Non-writing absorbs (all duplicates) reuse the cache -- no re-scan.

    Absorb writes NOTHING here (all facts duplicate existing rows), so the
    epoch is unchanged and the cache stays valid across calls.
    """
    with cached_db["backend"].connect() as conn:
        for content in SEED:
            storage.absorb_memory(conn, [content])  # duplicates -> skip
    with cached_db["backend"].connect() as conn:
        storage.get_corpus_snapshot(conn)
    # The two non-writing absorbs reused the cache: zero additional scans.
    assert cached_db["loads"]["n"] == 0, "mutation: cache not reused across non-writing absorbs"


def test_absorb_write_invalidates_cache(cached_db):
    """An absorb that writes a new memory invalidates the cache -> next re-scans."""
    with cached_db["backend"].connect() as conn:
        storage.absorb_memory(conn, ["brand new fact zeta"])
    with cached_db["backend"].connect() as conn:
        got = storage.get_corpus_snapshot(conn)
    assert len(got) == len(SEED) + 1, "mutation: invalidate dropped and new row missing"
    assert cached_db["loads"]["n"] == 1, "mutation: writing absorb did not invalidate cache"


def test_insert_forces_fallback(cached_db):
    """An insert after the cached load changes the epoch -> exact re-scan."""
    with cached_db["backend"].connect() as conn:
        storage.add_memory(conn, content="brand new row zeta", commit=True)
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: insert did not force the fallback"


def test_update_forces_fallback(cached_db):
    """An update (re-embed, new writer_token) forces the exact re-scan."""
    with cached_db["backend"].connect() as conn:
        target = conn.execute("SELECT id FROM memories LIMIT 1").fetchone()["id"]
        storage.update_memory(conn, target, content=SEED[0] + " revised followup words")
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: update did not force the fallback"


def test_delete_forces_fallback(cached_db):
    """A delete (row gone) forces the exact re-scan."""
    with cached_db["backend"].connect() as conn:
        target = conn.execute("SELECT id FROM memories LIMIT 1").fetchone()["id"]
        storage.delete_memory(conn, target)
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: delete did not force the fallback"


def test_reeembed_forces_fallback(cached_db):
    """A same-model re-embed (new writer_token, count unchanged) forces fallback.

    The case a naive max(id) high-water would MISS: no new id, yet the vector
    changed. The epoch trigger catches it.
    """
    with cached_db["backend"].connect() as conn:
        target = conn.execute("SELECT id FROM memories LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE memories_embeddings SET writer_token = 'new-token-abc' WHERE memory_id = ?",
            (target,),
        )
        conn.commit()
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: re-embed did not force the fallback"


def test_model_epoch_change_forces_fallback(cached_db):
    """A model-epoch change (different embedding model key) forces the fallback."""
    from memora import embeddings
    with cached_db["backend"].connect() as conn:
        embeddings.set_stored_embedding_model(conn, "model-B")
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: model change did not force fallback"


def test_process_restart_forces_fallback(cached_db):
    """A process restart (empty cache) forces the exact re-scan."""
    storage._corpus_cache.clear()  # simulate restart
    with cached_db["backend"].connect() as conn:
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: restart did not force the fallback"


def test_external_writer_forces_fallback(cached_db):
    """A DIRECT DB write (bypassing cache) forces the exact re-scan."""
    with cached_db["backend"].connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, ?, ?, ?)",
            ("external direct write", None, "[]", "2026-01-01 00:00:00"),
        )
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO memories_embeddings(memory_id, embedding, representation, encoding_source) "
            "VALUES (?, '{}', 'empty', 'python')",
            (new_id,),
        )
        conn.commit()
        storage.get_corpus_snapshot(conn)
    assert cached_db["loads"]["n"] == 1, "mutation: external write did not force fallback"


def test_fork_isolates_mutation_from_base(cached_db):
    """A fork's append/discard must NOT leak into the shared base or other forks."""
    base = storage._corpus_base.__wrapped__ if hasattr(storage._corpus_base, "__wrapped__") else None
    # Re-fetch the current base via a fresh fork path: capture the cached base.
    with cached_db["backend"].connect() as conn:
        fork1 = storage.get_corpus_snapshot(conn)
        before = len(fork1)
        # Mutate fork1 only.
        fork1.append(9999, {"x": 1.0}, "2026-01-01", None, "python")
        fork1.discard(list(fork1._by_id)[0])
        # A second fork from the same cache must be unaffected.
        fork2 = storage.get_corpus_snapshot(conn)
        assert len(fork2) == before, "mutation: fork mutation leaked into base"
        assert 9999 not in fork2._by_id, "mutation: append leaked into another fork"


def test_failed_absorb_does_not_pollute_cache(cached_db, monkeypatch):
    """A failing absorb's append must not leak into the shared base, and the
    compensated (deleted) row must not be visible on the next fork.

    Raise from _after_absorb_owned_insert, which fires AFTER corpus.append.
    Wrapping add_memory cannot prove this: that raise happens before absorb
    receives `record`, so append never runs.
    """
    bases = list(storage._corpus_cache.values())
    assert bases, "fixture must have primed a cached base"
    base = bases[0].snapshot
    base_len = len(base)
    created_id = {"v": None}

    def boom(mid, nonce):
        created_id["v"] = mid
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(storage, "_after_absorb_owned_insert", boom)
    monkeypatch.setattr(
        storage,
        "_classify_fact_against_matches",
        lambda fact, matches: ([], []),
    )
    with cached_db["backend"].connect() as conn:
        with pytest.raises(RuntimeError, match="injected write failure"):
            storage.absorb_memory(conn, ["fact that will fail"])
        assert created_id["v"] is not None, (
            "mutation: hook never fired -- append-then-fail path not reached"
        )
        assert storage.get_memory(conn, created_id["v"]) is None

    assert created_id["v"] not in base._by_id, (
        "mutation: corpus.append on the shared base leaks the compensated id"
    )
    assert len(base) == base_len, "mutation: shared base grew during failed absorb"
    with cached_db["backend"].connect() as conn:
        got = storage.get_corpus_snapshot(conn)
    assert created_id["v"] not in got._by_id, (
        "mutation: failed absorb's compensated row leaked into the next fork"
    )
    assert cached_db["loads"]["n"] == 1, "mutation: failure did not invalidate the cache"


def test_publish_under_stable_epoch_only(cached_db, monkeypatch):
    """All retries stay unstable: the last load is returned uncached.

    The writer runs AFTER the snapshot is taken so the returned view is
    missing the last insert. Publishing that view under the post-write
    epoch would certify an incomplete corpus. Bounded PIT, not exact.
    """
    original = storage._load_corpus_snapshot
    storage._corpus_cache.clear()
    last_id = {"v": None}

    with cached_db["backend"].connect() as conn:
        def unstable_load(conn, **kwargs):
            snap = original(conn, **kwargs)
            # Concurrent writer AFTER the SELECT: snapshot does not include it.
            last_id["v"] = _direct_insert(conn, "midload writer after snapshot")
            return snap

        monkeypatch.setattr(storage, "_load_corpus_snapshot", unstable_load)
        got = storage.get_corpus_snapshot(conn)
        assert isinstance(got, storage._CorpusSnapshot)
        assert cached_db["loads"]["n"] == storage._CORPUS_LOAD_RETRIES, (
            "mutation: retries did not all run; loader count "
            f"{cached_db['loads']['n']} != {storage._CORPUS_LOAD_RETRIES}"
        )
        key = _cache_key(conn)
        assert key not in storage._corpus_cache, (
            "mutation: caching `loaded` after retry exhaustion publishes "
            "a snapshot that never observed the last writer"
        )
        # Last insert is in the DB but not in the uncached PIT view.
        assert last_id["v"] not in got._by_id


def test_publish_after_retry_when_epoch_stabilises(cached_db, monkeypatch):
    """First load unstable, second load stable: two loads, cached epoch
    equals the DB epoch, and the row inserted during the race is in the
    cached base."""
    original = storage._load_corpus_snapshot
    storage._corpus_cache.clear()
    inserted = {"id": None}

    with cached_db["backend"].connect() as conn:
        def once_unstable(conn, **kwargs):
            snap = original(conn, **kwargs)
            if inserted["id"] is None:
                inserted["id"] = _direct_insert(conn, "race insert then stable")
            return snap

        monkeypatch.setattr(storage, "_load_corpus_snapshot", once_unstable)
        storage.get_corpus_snapshot(conn)
        assert cached_db["loads"]["n"] == 2, (
            "mutation: did not retry once then cache; "
            f"loads={cached_db['loads']['n']}"
        )
        key = _cache_key(conn)
        assert key in storage._corpus_cache, "mutation: stable retry never published"
        entry = storage._corpus_cache[key]
        _model, db_epoch = storage._corpus_meta(conn)
        assert entry.epoch == db_epoch, (
            "mutation: cached under a mismatched epoch "
            f"(cached {entry.epoch} vs db {db_epoch})"
        )
        assert inserted["id"] in entry.snapshot._by_id, (
            "mutation: cached base missing the row that landed during the first load"
        )


def test_absent_epoch_row_does_not_cache(cached_db):
    """Deleted epoch row is not a valid stamp. Triggers then update zero
    rows, so a cache under coerced 0 would never invalidate again."""
    storage._corpus_cache.clear()
    with cached_db["backend"].connect() as conn:
        conn.execute(
            "DELETE FROM memories_meta WHERE key = ?", (storage._EPOCH_KEY,),
        )
        conn.commit()
        storage.get_corpus_snapshot(conn)
        assert not any(e.epoch == 0 for e in storage._corpus_cache.values()), (
            "mutation: treating a missing epoch as 0 caches under a dead stamp"
        )
        assert cached_db["loads"]["n"] == 1
        storage.add_memory(conn, content="mutation after missing epoch", commit=True)
        storage.get_corpus_snapshot(conn)
        assert cached_db["loads"]["n"] == 2, (
            "mutation: missing epoch was cached; second get after a write hit it"
        )
        assert not any(e.epoch == 0 for e in storage._corpus_cache.values())


def test_malformed_epoch_row_does_not_cache(cached_db):
    """Non-numeric epoch is not a valid stamp. A second get without a
    mutation must also load (proves no cache-under-0). SQLite CAST of
    the garbage then a write would hide that bug if we only checked
    post-mutation."""
    storage._corpus_cache.clear()
    with cached_db["backend"].connect() as conn:
        conn.execute(
            "UPDATE memories_meta SET value = 'not-an-int' WHERE key = ?",
            (storage._EPOCH_KEY,),
        )
        conn.commit()
        storage.get_corpus_snapshot(conn)
        assert not any(e.epoch == 0 for e in storage._corpus_cache.values()), (
            "mutation: treating a non-numeric epoch as 0 caches under a dead stamp"
        )
        storage.get_corpus_snapshot(conn)
        assert cached_db["loads"]["n"] == 2, (
            "mutation: malformed epoch was cached; second get before a write hit it"
        )
        storage.add_memory(conn, content="mutation after malformed epoch", commit=True)
        storage.get_corpus_snapshot(conn)
        assert cached_db["loads"]["n"] == 3, (
            "mutation: malformed epoch left a cache hit after the write"
        )


def test_warm_hit_is_one_meta_select(cached_db):
    """A warm cache hit reads model stamp and epoch in ONE memories_meta
    SELECT. Store identity is local. Two statements here is the remote
    100-400ms tax this change exists to drop."""
    with cached_db["backend"].connect() as conn:
        sqls = []
        conn.set_trace_callback(sqls.append)
        got = storage.get_corpus_snapshot(conn)
        conn.set_trace_callback(None)
    meta = [s for s in sqls if "memories_meta" in s]
    assert len(meta) == 1, (
        f"mutation: warm hit issued {len(meta)} memories_meta statements: {meta}"
    )
    assert "IN" in meta[0].upper(), (
        "mutation: split the combined SELECT back into two key lookups"
    )
    assert cached_db["loads"]["n"] == 0, "mutation: warm hit re-scanned"
    assert got._cache_key, "mutation: fork dropped the resolved cache key"


def test_invalidate_reuses_resolved_key(cached_db):
    """Passing the already-resolved key must not issue a memories_meta
    statement. Re-querying the model stamp on every absorb write is the
    second half of the two-round-trip tax."""
    with cached_db["backend"].connect() as conn:
        fork = storage.get_corpus_snapshot(conn)
        key = fork._cache_key
        assert key, "fixture prime must leave a resolved key on the fork"
        sqls = []
        conn.set_trace_callback(sqls.append)
        storage.invalidate_corpus_cache(conn, key=key)
        conn.set_trace_callback(None)
        assert sqls == [], (
            f"mutation: invalidate with a resolved key still queried: {sqls}"
        )
        assert key not in storage._corpus_cache, (
            "mutation: invalidate ignored the resolved key"
        )


def test_cached_results_equal_fresh_scan(cached_db):
    """Cached search results are identical to a fresh exact D1 scan."""
    with cached_db["backend"].connect() as conn:
        query = storage._compute_embedding("charlie three", None, [])
        cached = storage._search_snapshot_full(conn, storage.get_corpus_snapshot(conn), query, top_k=5, min_score=0.0)
        fresh = storage._load_corpus_snapshot(conn)
        fresh_res = storage._search_snapshot_full(conn, fresh, query, top_k=5, min_score=0.0)
        cached_ids = [(m["memory"]["id"], round(m["score"], 6)) for m in cached]
        fresh_ids = [(m["memory"]["id"], round(m["score"], 6)) for m in fresh_res]
        assert cached_ids == fresh_ids, "mutation: cache diverged from fresh exact scan"
