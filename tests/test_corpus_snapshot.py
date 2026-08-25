"""Tests for the skinny absorb corpus snapshot (scan-once).

The absorb read-timeout fix loads the whole corpus ONCE per absorb call into a
skinny in-memory snapshot, scores every fact against it exhaustively, reuses it
for the write-time crossref pass, and appends newly created memories. These
tests prove the snapshot is decision-equivalent to the old per-scan exhaustive
scorer, that pagination cannot change an answer, and that the corpus is read
exactly once per absorb.
"""

import json

import pytest

import memora.storage as storage
from memora.backends import LocalSQLiteBackend


# Distinct vocab so cosine gives near-zero overlap between unrelated rows and a
# clear winner for the query row. The last row is the query's best match and has
# the highest id, so at page_size=1 it lives on the final page.
SEED_CONTENTS = [
    "alpha one",
    "bravo two",
    "charlie three",
    "delta four",
    "echo five",
    "foxtrot six",
    "golf seven",
    "hotel eight",
    "india nine",
    "juliet ten",
    "kilo eleven",
    "lima twelve",
]


def _seed(backend) -> None:
    with backend.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)


def _fresh_absorb(db_path, facts, monkeypatch):
    """Run absorb against a freshly seeded store; returns the decisions list."""
    backend = LocalSQLiteBackend(db_path)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    with storage.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)
        return storage.absorb_memory(conn, facts)


def test_absorb_scan_once_equals_per_fact_exhaustive(tmp_path, monkeypatch):
    """Decisions from scan-once must equal the old per-fact exhaustive behavior.

    The OLD path ran a fresh full corpus scan for every fact (and again per
    created memory) via _search_by_vector. We simulate it by making
    _search_snapshot_full delegate to the exhaustive _search_by_vector each
    call, then assert the same decisions on an identical store.
    """
    facts = ["zeta upstream deploy", "new unrelated fact"]

    # Scan-once (real) path.
    snap_result = _fresh_absorb(tmp_path / "snap.db", facts, monkeypatch)

    # Old exhaustive per-fact path: _search_snapshot_full becomes a thin
    # delegate to the full exhaustive scorer, which is what the pre-change
    # absorb did every fact.
    original = storage._search_snapshot_full

    def exhaustive_delegate(conn, corpus, vector, **kwargs):
        return storage._search_by_vector(conn, vector, **kwargs)

    monkeypatch.setattr(storage, "_search_snapshot_full", exhaustive_delegate)
    old_result = _fresh_absorb(tmp_path / "old.db", facts, monkeypatch)
    monkeypatch.setattr(storage, "_search_snapshot_full", original)

    snap_decisions = [(d["action"], d.get("fact")) for d in snap_result["decisions"]]
    old_decisions = [(d["action"], d.get("fact")) for d in old_result["decisions"]]
    assert snap_decisions == old_decisions, (
        f"mutation: break scan-once equivalence and this assert goes red\n"
        f"  snap: {snap_decisions}\n  old:  {old_decisions}"
    )


def test_corpus_is_loaded_exactly_once_per_absorb(local_db, monkeypatch):
    """The whole point of the change: the corpus is read exactly once."""
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)

        calls = {"n": 0}
        original = storage._load_corpus_snapshot

        def counting(conn, **kwargs):
            calls["n"] += 1
            return original(conn, **kwargs)

        monkeypatch.setattr(storage, "_load_corpus_snapshot", counting)
        storage.absorb_memory(conn, ["zeta upstream deploy", "alpha followup", "charlie followup"])

    assert calls["n"] == 1, (
        "mutation: load the corpus per fact (not once) and this goes red"
    )


def test_page_size_invariance(local_db):
    """Pagination must never change a search answer."""
    monkeypatch = None
    with storage.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)
        query = storage._compute_embedding("kilo eleven", None, [])
        reference = None
        for page_size in (1, 7, 100, 1000):
            corpus = storage._load_corpus_snapshot(conn, page_size=page_size)
            got = corpus.search(query, top_k=5)
            assert len(got) > 0
            if reference is None:
                reference = got
            else:
                assert got == reference, (
                    f"mutation: page_size={page_size} changed the answer (ref {reference}, got {got})"
                )


def test_snapshot_matches_exhaustive_scorer(local_db):
    """Differential: snapshot top-k equals the exhaustive scorer over hard cases.

    Covers zero token overlap, a best match that only appears beyond page 1
    (page_size=1), and tie-break on created_at (old vs new).
    """
    with storage.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)

        queries = [
            "kilo eleven",          # exact match on the highest-id row
            "lima twelve",          # another late row
            "totally unrelated words",  # near-zero overlap with everything
        ]
        for page_size in (1, 100):
            corpus = storage._load_corpus_snapshot(conn, page_size=page_size)
            for q in queries:
                vector = storage._compute_embedding(q, None, [])
                snap = storage._search_snapshot_full(conn, corpus, vector, top_k=5, min_score=0.0)
                ex = storage._search_by_vector(conn, vector, top_k=5, min_score=0.0)
                snap_ids = [(m["memory"]["id"], round(m["score"], 6)) for m in snap]
                ex_ids = [(m["memory"]["id"], round(m["score"], 6)) for m in ex]
                assert snap_ids == ex_ids, (
                    f"mutation: snapshot diverged from exhaustive scorer for {q!r} "
                    f"page={page_size} (snap {snap_ids}, ex {ex_ids})"
                )


def test_absorb_appends_created_memories_to_snapshot(local_db, monkeypatch):
    """absorb must append each created memory to the in-memory corpus so a
    later scan in the same call sees it (the snapshot-append path)."""
    with storage.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)

        appended = {}
        original = storage._load_corpus_snapshot

        def spy(conn, **kwargs):
            corpus = original(conn, **kwargs)
            appended["corpus"] = corpus
            appended["initial_len"] = len(corpus)
            return corpus

        monkeypatch.setattr(storage, "_load_corpus_snapshot", spy)
        result = storage.absorb_memory(conn, ["zeta upstream deploy", "eta upstream deploy"])

        created = result.get("created", 0)
        assert created >= 1, "test setup: absorb should create at least one memory"
        assert len(appended["corpus"]) == appended["initial_len"] + created, (
            "mutation: drop the corpus.append on create and this goes red "
            f"(expected {appended['initial_len'] + created}, got {len(appended['corpus'])})"
        )


def test_snapshot_append_makes_new_memory_visible(local_db):
    """A memory appended mid-call (a just-created fact) is visible to later scans.

    This is the path that keeps a later created memory's crossref pass seeing an
    earlier created memory, matching the old live-DB re-read behavior.
    """
    with storage.connect() as conn:
        for content in SEED_CONTENTS:
            storage.add_memory(conn, content=content, commit=True)
        corpus = storage._load_corpus_snapshot(conn)
        before = len(corpus)

        # Simulate a just-created memory whose embedding strongly matches a query.
        created_id = 9999
        created_vector = {"brand_new_token": 1.0}
        corpus.append(created_id, created_vector, "2026-01-01 00:00:00", None, "python")

        assert len(corpus) == before + 1
        got = corpus.search({"brand_new_token": 1.0}, top_k=1, min_score=0.0)
        assert got and got[0][0] == created_id, (
            "mutation: drop the append so a created memory is invisible and this goes red"
        )


def test_snapshot_crossref_uses_in_memory_metadata_type(local_db, monkeypatch):
    """The snapshot crossref path drops document memories in-process (no DB fan-out).

    Confirms the corpus-supplied crossref pass filters document types using the
    snapshot's metadata_type rather than re-querying per result.
    """
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        # A document-root memory that must never become a crossref target.
        doc = storage.add_memory(
            conn,
            content="document root content alpha",
            metadata={"type": "document_root"},
            commit=True,
        )
        plain = storage.add_memory(conn, content="plain memory bravo", commit=True)
        corpus = storage._load_corpus_snapshot(conn)

        target_vector = {"bravo": 1.0}
        related = storage._update_crossrefs_for_memory(
            conn, 99999, vector=target_vector, top_k=5, min_score=0.0, corpus=corpus,
        )
        related_ids = {r["id"] for r in related}
        assert doc["id"] not in related_ids, (
            "mutation: snapshot crossref ignores metadata_type and this goes red"
        )


def test_repair_failure_fails_closed_no_create(local_db, monkeypatch):
    """A legacy row whose repair embedding RAISES must fail closed: no create.

    The old lazy-backfill propagated the failure so the fact was NOT written.
    If repair swallowed it, absorb would score against a corpus missing that
    memory and could create its duplicate. Here the incoming fact's embedding
    SUCCEEDS but a legacy row's repair RAISES -- assert no create happens.
    """
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        # A fully-embedded row so the corpus loads fine initially.
        storage.add_memory(conn, content="normal row content", commit=True)
        # A legacy row with a NULL embedding (no vector) that must be repaired.
        conn.execute(
            "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, NULL, ?, ?)",
            ("legacy repair target", "[]", "2026-01-01 00:00:00"),
        )
        legacy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        real_compute = storage._compute_embedding
        calls = {"n": 0}

        def flaky_compute(content, *a, **k):
            calls["n"] += 1
            if content == "legacy repair target":
                raise RuntimeError("provider down")
            return real_compute(content, *a, **k)

        monkeypatch.setattr(storage, "_compute_embedding", flaky_compute)

        with pytest.raises(RuntimeError, match="provider down"):
            storage.absorb_memory(conn, ["brand new fact zeta"])

        # The fact was not written.
        rows = conn.execute(
            "SELECT id FROM memories WHERE content = 'brand new fact zeta'"
        ).fetchall()
        assert not rows, (
            "mutation: swallow the repair failure and a duplicate gets created here"
        )


def test_repair_certifies_empty_vector_once(local_db, monkeypatch):
    """A newly repaired EMPTY vector must be certified exactly once.

    A legacy punctuation-only row under TF-IDF yields an empty vector; the old
    (buggy) path skipped the upsert, so every absorb re-fetched and recomputed
    it forever. The fix upserts {} so it becomes a certified-empty marker.
    """
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, NULL, ?, ?)",
            ("...", None, "2026-01-01 00:00:00"),  # punctuation-only -> empty TF-IDF bag
        )
        legacy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        real_compute = storage._compute_embedding
        calls = {"n": 0}

        def counting_compute(content, *a, **k):
            calls["n"] += 1
            return real_compute(content, *a, **k)

        monkeypatch.setattr(storage, "_compute_embedding", counting_compute)

        corpus = storage._load_corpus_snapshot(conn)
        # First load: the empty vector was computed and upserted (certified).
        assert calls["n"] >= 1

        row = conn.execute(
            "SELECT representation, encoding_source FROM memories_embeddings WHERE memory_id = ?",
            (legacy_id,),
        ).fetchone()
        assert row is not None, "mutation: repair skipped the empty upsert"
        assert row["representation"] == "empty" and row["encoding_source"] == "python", (
            "mutation: empty repaired vector not certified"
        )

        # Second load: the certified-empty marker must prevent re-computation.
        before = calls["n"]
        storage._load_corpus_snapshot(conn)
        assert calls["n"] == before, (
            "mutation: certified-empty row is recomputed every load"
        )


def test_repair_certified_empty_not_recomputed(local_db, monkeypatch):
    """A PRE-EXISTING certified-empty row is never recomputed by the loader."""
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        # A punctuation-only memory (empty TF-IDF bag), inserted directly since
        # add_memory refuses an empty vector.
        conn.execute(
            "INSERT INTO memories (content, metadata, tags, created_at) VALUES (?, ?, ?, ?)",
            ("!!!", None, "[]", "2026-01-01 00:00:00"),
        )
        m = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Build the CANONICAL certified-empty marker exactly as repair/upsert
        # would: _upsert_embedding(conn, id, {}) stores embedding=NULL with
        # representation='empty', encoding_source='python'. (embedding_to_json
        # maps {} -> None.) Using the real upsert guarantees the fixture hits
        # the loader's certified-empty branch (embedding falsy + rep empty),
        # not the ordinary JSON-vector branch that a truthy '{}' would take.
        storage._upsert_embedding(conn, m, {})
        conn.commit()

        # Assert the PERSISTED shape is the canonical marker.
        persisted = conn.execute(
            "SELECT embedding, representation, encoding_source FROM memories_embeddings WHERE memory_id = ?",
            (m,),
        ).fetchone()
        assert persisted["embedding"] is None, "fixture: certified-empty must store NULL embedding"
        assert persisted["representation"] == "empty" and persisted["encoding_source"] == "python"

        real_compute = storage._compute_embedding
        calls = {"n": 0}

        def counting_compute(content, *a, **k):
            calls["n"] += 1
            return real_compute(content, *a, **k)

        monkeypatch.setattr(storage, "_compute_embedding", counting_compute)
        corpus = storage._load_corpus_snapshot(conn)
        assert calls["n"] == 0, (
            "mutation: loader recomputes a pre-existing certified-empty row"
        )
        # The certified-empty row is present but unsearchable: it must never be
        # returned as a candidate, even at min_score=0.
        got = corpus.search({"anything": 1.0}, top_k=50, min_score=0.0)
        got_ids = {entry_id for entry_id, _ in got}
        assert m not in got_ids, (
            "mutation: certified-empty row is searchable"
        )


def test_absorb_discard_removes_compensated_memory_from_later_crossref(local_db, monkeypatch):
    """A compensated (deleted) created memory is discarded from the snapshot, so a
    LATER phase-3 job's crossref scan must not list it.

    The existing tombstone tests use a single phase-3 job, so they stay green
    even if corpus.discard is deleted entirely (the stale snapshot entry only
    matters to a SUBSEQUENT job's crossref). Here job1 is compensated+deleted;
    job2 then creates a memory and its crossref (computed against the corpus in
    add_memory) must omit job1's id.
    """
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    with storage.connect() as conn:
        t1 = storage.add_memory(conn, content="discard target one", commit=True)
        t2 = storage.add_memory(conn, content="discard target two", commit=True)

        monkeypatch.setattr(
            storage,
            "_search_snapshot_full",
            lambda *a, **k: [{"score": 0.5, "memory": t1}],
        )
        monkeypatch.setattr(
            storage,
            "_classify_fact_against_matches",
            lambda fact, matches: ([{
                "memory_id": t1["id"] if "one" in fact else t2["id"],
                "relationship": "UPDATE",
                "reason": "update",
            }], []),
        )
        monkeypatch.setattr(storage, "_compute_embedding", lambda *a, **k: {"x": 1.0})

        seen = {"new_ids": []}

        def before_link(new_id, targets):
            seen["new_ids"].append(new_id)
            # Tombstone job1's target only, so job1 gets compensated (deleted).
            if t1["id"] in targets:
                storage._tombstone_component(
                    conn, t1["id"], reason="raced-delete",
                    content_by_id={t1["id"]: t1["content"]},
                )

        storage._before_absorb_supersede_links = before_link
        try:
            result = storage.absorb_memory(
                conn, ["fact one updating target one", "fact two updating target two"]
            )
        finally:
            storage._before_absorb_supersede_links = None

        # Job1 was compensated (tombstoned); job2 created a real memory.
        job1_id = seen["new_ids"][0]
        created = [d for d in result["decisions"] if d["action"] in ("superseded", "created")]
        assert created, "fixture: job2 must produce a live memory"
        job2_id = created[0]["memory_id"]
        assert job2_id != job1_id

        # Job1 is gone from the store (compensated).
        assert storage.get_memory(conn, job1_id) is None, (
            "fixture: job1 should have been compensated (deleted)"
        )

        # Job2's stored crossrefs must NOT reference the compensated job1 id.
        row = conn.execute(
            "SELECT related FROM memories_crossrefs WHERE memory_id = ?", (job2_id,)
        ).fetchone()
        assert row is not None, "fixture: job2 should have crossrefs"
        related_ids = {r["id"] for r in json.loads(row["related"])}
        assert job1_id not in related_ids, (
            "mutation: delete corpus.discard and the compensated job1 leaks into job2's crossref"
        )


def test_absorb_point_in_time_dedup_is_documented_and_bounded(local_db, monkeypatch):
    """Concurrency divergence is a DOCUMENTED, bounded limitation, not silent.

    The snapshot is a point-in-time view: a write committed by another agent
    AFTER load is invisible. This test makes that explicit -- it asserts the
    corpus is snapshotted once at the start of the call, so a DB insert that
    would have been a duplicate is NOT re-read mid-call. This is the contract
    the code comment states; the test pins the documented behavior so a future
    change to it is a deliberate decision, not an accident.
    """
    with storage.connect() as conn:
        storage.add_memory(conn, content="existing alpha", commit=True)

        loads = {"n": 0}
        original = storage._load_corpus_snapshot

        def spy(conn, **kwargs):
            loads["n"] += 1
            return original(conn, **kwargs)

        monkeypatch.setattr(storage, "_load_corpus_snapshot", spy)
        corpus = storage._load_corpus_snapshot(conn)
        # Drive the production path absorb uses (_search_snapshot_full) so the
        # mutation that makes a search re-read the live DB is reachable.
        assert storage._search_snapshot_full(conn, corpus, {"existing": 1.0}, top_k=1, min_score=0.0)

        # Simulate a concurrent writer committing a NEW near-duplicate after load.
        late = storage.add_memory(conn, content="existing alpha followup", commit=True)
        # The already-loaded snapshot does NOT contain it (bounded point-in-time).
        got = storage._search_snapshot_full(
            conn, corpus, {"followup": 1.0, "alpha": 1.0, "existing": 1.0}, top_k=5, min_score=0.0)
        snap_ids = {m["memory"]["id"] for m in got}
        assert late["id"] not in snap_ids, (
            "mutation: snapshot unexpectedly sees post-load writes (unbounded)"
        )
        # A FRESH snapshot does see it -- the bounded window closes on the next call.
        fresh = storage._load_corpus_snapshot(conn)
        fresh_got = {m["memory"]["id"] for m in storage._search_snapshot_full(
            conn, fresh, {"followup": 1.0, "alpha": 1.0, "existing": 1.0}, top_k=5, min_score=0.0)}
        assert late["id"] in fresh_got, (
            "mutation: fresh snapshot cannot see the committed write"
        )
        # The loader ran exactly twice: the initial snapshot and the fresh one.
        assert loads["n"] == 2, (
            "mutation: loader ran an unexpected number of times"
        )
