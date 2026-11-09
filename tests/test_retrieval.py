"""Item 260: retrieval v2 (memora/retrieval.py) -- synthetic stores only."""
import asyncio
import json

import pytest

from memora import retrieval, server, storage


def _mk(conn, content, **kw):
    r = storage.add_memory(conn, content=content, **kw)
    return r["id"] if "id" in r else r["memory"]["id"]


@pytest.fixture
def conn(local_db):
    with storage.connect() as c:
        yield c


def _ids(hits):
    return [h["memory"]["id"] for h in hits]


# --- lexical leg -----------------------------------------------------------

def test_long_query_gets_ranked_keyword_hits_where_v1_gets_none(conn):
    best = _mk(conn, "zorblax quuxwidget calibration notes")
    some = _mk(conn, "zorblax only mentioned once here")
    _mk(conn, "unrelated gardening advice about tomatoes")
    long_query = (
        "please remind me of everything we know about the zorblax quuxwidget "
        "situation because nothing else in this wordy brief appears anywhere"
    )
    # v1: implicit AND of every word matches nothing.
    assert storage.list_memories(conn, query=long_query, limit=10) == []
    hits = retrieval.lexical_search(conn, long_query, limit=10)
    assert _ids(hits)[:2] == [best, some], (
        "mutation: AND the terms instead of OR and this list is empty"
    )
    assert hits[0]["score"] > hits[1]["score"], "bm25 ranks, not created_at"


def test_bm25_rank_beats_recency(conn):
    old = _mk(conn, "frobnicate frobnicate frobnicate protocol")
    new = _mk(conn, "frobnicate mentioned in passing among many other filler words here")
    assert new > old
    assert _ids(retrieval.lexical_search(conn, "frobnicate protocol", limit=5)) == [old, new]


def test_stopwords_and_noise_dropped_and_deduped():
    terms = retrieval.normalize_terms("The def class SELF and the Zorblax zorblax a x_y")
    assert terms == ["zorblax"]
    assert retrieval.normalize_terms('"AND" OR NOT * ( ) NEAR') == ["near"]


def test_fts_syntax_in_query_is_inert(conn):
    _mk(conn, "plain note about widgets")
    for q in ['widgets" OR "', "widgets AND NOT", "widg*", "(widgets", "col:widgets", '"']:
        retrieval.lexical_search(conn, q, limit=5)  # must not raise


def test_idf_cap_keeps_rarest_terms_and_drops_absent_ones(conn):
    for i in range(6):
        _mk(conn, f"commonword filler {i}")
    rare = _mk(conn, "commonword raretoken")
    terms = retrieval.normalize_terms("commonword raretoken ghostterm")
    assert retrieval.select_terms(conn, terms, 48) == terms  # fits: untouched
    assert retrieval.select_terms(conn, terms, 1) == ["raretoken"]
    assert retrieval.select_terms(conn, terms, 2) == ["commonword", "raretoken"]
    assert _ids(retrieval.lexical_search(conn, "commonword raretoken ghostterm", limit=1, max_terms=1)) == [rare]


def test_many_terms_are_capped(conn, monkeypatch):
    _mk(conn, "needleterm alpha")
    words = " ".join(f"filler{i}" for i in range(300))
    seen = []
    real = retrieval.select_terms

    def spy(c, terms, n):
        out = real(c, terms, n)
        seen.append(len(out))
        return out

    monkeypatch.setattr(retrieval, "select_terms", spy)
    hits = retrieval.lexical_search(conn, f"needleterm {words}", limit=5, max_terms=48)
    assert seen == [1], "absent terms are dropped, the present one kept"
    assert len(hits) == 1
    assert len(retrieval.select_terms(conn, retrieval.normalize_terms(words), 10)) <= 10


def test_filters_apply_to_lexical(conn):
    a = _mk(conn, "sharedtoken one", tags=["keep"])
    _mk(conn, "sharedtoken two", tags=["drop"])
    assert _ids(retrieval.lexical_search(conn, "sharedtoken", limit=5, tags_all=["keep"])) == [a]
    assert retrieval.lexical_search(conn, "sharedtoken", limit=5, date_to="2000-01-01") == []


def test_fts_disabled_ranks_by_matched_term_count(conn):
    two = _mk(conn, "alphaterm betaterm together")
    one = _mk(conn, "alphaterm alone")
    newer_one = _mk(conn, "betaterm alone too")
    conn.execute("DROP TABLE memories_fts")
    conn.commit()
    assert not storage._fts_enabled(conn)
    hits = retrieval.lexical_search(conn, "alphaterm betaterm gammaterm", limit=10)
    ids = _ids(hits)
    assert ids[0] == two, "most matched terms first"
    assert ids[1:] == [newer_one, one], "ties: newest first"
    assert [h["score"] for h in hits] == [2.0, 1.0, 1.0]


# --- identifier leg --------------------------------------------------------

def test_item_n_resolves_metadata_item_not_item_number(conn):
    rec = _mk(conn, "record body", metadata={"kind": "item-record", "item": 260})
    _mk(conn, "plan step fragment", metadata={"item_number": 260})
    _mk(conn, "other item", metadata={"kind": "item-record", "item": 61})
    assert _ids(retrieval.identifier_lookup(conn, "what did Item 260 decide?")) == [rec]
    assert _ids(retrieval.identifier_lookup(conn, "see #260")) == [rec]
    assert retrieval.identifier_lookup(conn, "Item 26") == []
    mutated_hits = retrieval.identifier_lookup(conn, "Item 260")
    assert mutated_hits[0]["score"] >= 2000, "metadata hit outranks a phrase hit"


def test_item_stored_as_string_also_resolves(conn):
    rec = _mk(conn, "record", metadata={"item": "77"})
    assert _ids(retrieval.identifier_lookup(conn, "Item 77")) == [rec]


def test_memory_id_mnnnn(conn):
    first = _mk(conn, "first")
    _mk(conn, "second")
    assert _ids(retrieval.identifier_lookup(conn, f"see m{first} please")) == [first]


def test_underscore_dotted_and_path_identifiers(conn):
    snake = _mk(conn, "the bug lives in hybrid_search_scored near the keyword leg")
    decoy = _mk(conn, "hybrid notes. search things. scored well")
    path = _mk(conn, "edit src/a/b.zig then rebuild")
    ver = _mk(conn, "released as v0.5.7.13 today")
    dotted = _mk(conn, "call storage.list_memories with a query")
    camel = _mk(conn, "the fooBarBaz helper is used")
    ids = lambda q: _ids(retrieval.identifier_lookup(conn, q))
    assert ids("hybrid_search_scored") == [snake]
    assert decoy not in ids("hybrid_search_scored")
    assert ids("src/a/b.zig") == [path]
    assert ids("v0.5.7.13") == [ver]
    assert ids("storage.list_memories") == [dotted]
    assert ids("fooBarBaz") == [camel]


def test_sha_prefix_and_more_identifiers_rank_higher(conn):
    one = _mk(conn, "landed 46d5c0afb0a0296d92f8a9a47e431349268f8acf as v0.5.7.13")
    two = _mk(conn, "only v0.5.7.13 here")
    hits = retrieval.identifier_lookup(conn, "46d5c0a v0.5.7.13")
    assert _ids(hits) == [one, two]
    assert hits[0]["matched"] == 2 and hits[1]["matched"] == 1
    assert retrieval.extract_identifiers("deadbeef cafe1234 1234567")["shas"] == ["cafe1234"]


# --- fusion ----------------------------------------------------------------

def test_weighted_rrf_orders_by_weights():
    legs = [(1.0, [1, 2, 3]), (1.0, [3, 2, 9])]
    both = retrieval.weighted_rrf(legs, 60)
    assert max(both, key=both.get) in (2, 3) and both[2] > both[1]
    heavy = retrieval.weighted_rrf([(1.0, [1, 2]), (5.0, [2, 1])], 60)
    assert heavy[2] > heavy[1]
    light = retrieval.weighted_rrf([(5.0, [1, 2]), (1.0, [2, 1])], 60)
    assert light[1] > light[2]
    assert retrieval.weighted_rrf([(0.0, [1])], 60) == {}


def test_search_v2_fusion_follows_leg_weights(conn):
    lex = _mk(conn, "xylophone xylophone xylophone tuning")
    ident = _mk(conn, "wholly different text", metadata={"kind": "item-record", "item": 5})
    q = "xylophone Item 5"
    only_ident = retrieval.search_v2(conn, q, top_k=5, dense_weight=0, lexical_weight=0, decompose=False)
    assert [r["memory"]["id"] for r in only_ident][:1] == [ident]
    only_lex = retrieval.search_v2(conn, q, top_k=5, dense_weight=0, identifier_weight=0, decompose=False)
    assert [r["memory"]["id"] for r in only_lex][:1] == [lex]
    both = retrieval.search_v2(conn, q, top_k=5, decompose=False)
    assert {r["memory"]["id"] for r in both} >= {lex, ident}
    assert all(set(r) == {"score", "memory", "cosine"} for r in both)


def test_decomposition_adds_paragraph_dense_lists(conn, monkeypatch):
    _mk(conn, "alpha topic memory")
    calls = []
    real = storage.semantic_search

    def spy(c, text, **kw):
        calls.append(text)
        return real(c, text, **kw)

    monkeypatch.setattr(storage, "semantic_search", spy)
    para = lambda w: (w + " ") * 30
    q = "\n\n".join([para("alpha"), para("beta"), "short"] + [para(f"p{i}") for i in range(8)])
    retrieval.search_v2(conn, q, top_k=3, decompose=True)
    assert len(calls) == 1 + 6, "whole query + at most 6 paragraphs over 60 chars"
    calls.clear()
    retrieval.search_v2(conn, q, top_k=3, decompose=False)
    assert len(calls) == 1
    calls.clear()
    retrieval.search_v2(conn, "one short paragraph only", top_k=3, decompose=True)
    assert len(calls) == 1


def test_follow_active_drops_superseded(conn):
    old = _mk(conn, "quasarmode configuration old behaviour")
    new = _mk(conn, "quasarmode configuration new behaviour")
    storage.add_link(conn, new, old, edge_type="supersedes")
    hit_ids = lambda follow: [r["memory"]["id"] for r in retrieval.search_v2(conn, "quasarmode configuration", top_k=5, follow=follow)]
    assert old in hit_ids(None)
    active = hit_ids("active")
    assert new in active and old not in active, "mutation: skip apply_follow and old returns"


def test_search_v2_is_read_only(conn):
    for i in range(5):
        _mk(conn, f"snapshot probe {i} Item {i}", metadata={"item": i})
    conn.commit()
    snap = lambda: (
        conn.execute("SELECT rowid, content, metadata, tags FROM memories_fts ORDER BY rowid").fetchall(),
        conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
        conn.execute("SELECT id, content, access_count FROM memories ORDER BY id").fetchall(),
    )
    before = [[tuple(r) for r in part] for part in snap()]
    changes = conn.total_changes
    retrieval.search_v2(conn, "snapshot probe Item 3 src/x.py m2 46d5c0a", top_k=5, follow="active")
    assert conn.total_changes == changes
    assert [[tuple(r) for r in part] for part in snap()] == before


# --- wiring ----------------------------------------------------------------

def _seed_default_probe(token):
    with storage.connect() as c:
        for i in range(4):
            _mk(c, f"{token} document {i} extra words")


def test_default_engine_is_v2_and_v1_stays_unchanged(local_db, monkeypatch):
    token = "defaultprobe-zzxq"
    _seed_default_probe(token)
    v2_calls = []
    real_v2 = retrieval.search_v2

    def spy_v2(*a, **kw):
        v2_calls.append(kw)
        return real_v2(*a, **kw)

    monkeypatch.setattr(retrieval, "search_v2", spy_v2)
    default = asyncio.run(server.memory_hybrid_search(token, limit=3, follow="active"))
    assert len(v2_calls) == 1, "the default path runs v2"
    with storage.connect() as c:
        expect_v2 = real_v2(c, token, top_k=3, follow="active")
    assert [r["memory"]["id"] for r in default["results"]] == [r["memory"]["id"] for r in expect_v2]
    assert [r["score"] for r in default["results"]] == [r["score"] for r in expect_v2]
    assert all("cosine" not in r and set(r) == {"score", "memory"} for r in default["results"])

    monkeypatch.setattr(retrieval, "search_v2", lambda *a, **k: pytest.fail("v2 ran for engine=v1"))
    v1 = asyncio.run(server.memory_hybrid_search(token, limit=3, follow="active", engine="v1"))
    with storage.connect() as c:
        direct = storage.hybrid_search(c, token, top_k=3, follow="active")
    assert [r["memory"]["id"] for r in v1["results"]] == [r["memory"]["id"] for r in direct]
    assert [r["score"] for r in v1["results"]] == [r["score"] for r in direct]


def test_digest_and_cli_search_use_v2_negative_control(local_db, monkeypatch, capsys):
    token = "callerprobe-qqxz"
    _seed_default_probe(token)
    seen = []
    real_v2 = retrieval.search_v2

    def spy_v2(*a, **kw):
        seen.append(kw)
        return real_v2(*a, **kw)

    monkeypatch.setattr(retrieval, "search_v2", spy_v2)
    asyncio.run(server.memory_digest(token, k=3))
    assert len(seen) == 1 and seen[0]["follow"] == "active"
    from memora import cli
    cli.cmd_search(token, top_k=3)
    assert len(seen) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 3 and all(set(e) == {"score", "memory"} for e in out["results"])
    # negative control: the v1 engine does not reach search_v2
    asyncio.run(server.memory_hybrid_search(token, limit=3, engine="v1"))
    assert len(seen) == 2


def test_write_paths_are_not_switched(local_db, monkeypatch):
    """absorb dedup / supersede checks and crossref linking stay on their v1 path."""
    monkeypatch.setattr(retrieval, "search_v2", lambda *a, **k: pytest.fail("a write path ran v2"))
    token = "writepath-vvkj"
    with storage.connect() as c:
        a = _mk(c, f"{token} first note about caching")
    out = asyncio.run(server.memory_absorb([f"{token} first note about caching"]))
    assert out is not None
    del a


def test_engine_v2_through_the_tool_and_invalid_engine(local_db):
    with storage.connect() as c:
        rec = _mk(c, "body without the words", metadata={"kind": "item-record", "item": 9})
        _mk(c, "noise memory")
    out = asyncio.run(server.memory_hybrid_search("what happened in Item 9", engine="v2", follow="active"))
    assert out["results"][0]["memory"]["id"] == rec
    assert "cosine" not in out["results"][0]
    bad = asyncio.run(server.memory_hybrid_search("x", engine="v3"))
    assert bad["error"] == "invalid_engine"


# --- F1: filters apply before the identifier candidate cap -------------------

@pytest.mark.parametrize("fts", [True, False])
def test_identifier_filters_apply_before_the_candidate_cap(conn, monkeypatch, fts):
    monkeypatch.setattr(retrieval, "_PER_IDENTIFIER_ROWS", 3)
    monkeypatch.setattr(retrieval, "_FTS_MAX_ROWS", 50)
    eligible = _mk(conn, "old note about README.md", project="memora", tags=["keep"])
    conn.execute("UPDATE memories SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (eligible,))
    for i in range(10):  # newer, more numerous, other project / tag
        _mk(conn, f"clmux note {i} about README.md", project="clmux", tags=["other"])
    conn.commit()
    if not fts:
        conn.execute("DROP TABLE memories_fts")
        conn.commit()
    ident = lambda **kw: _ids(retrieval.identifier_lookup(conn, "README.md", limit=5, **kw))
    assert ident(project="memora") == [eligible], "project filter beyond the raw cap"
    assert ident(tags_all=["keep"]) == [eligible], "tag filter beyond the raw cap"
    assert ident(date_to="2021-01-01") == [eligible], "date filter beyond the raw cap"
    assert ident(project="memora", tags_all=["keep"], date_to="2021-01-01") == [eligible]
    assert len(ident()) == 3, "unfiltered still honours the per-identifier cap"


def test_item_lookup_filters_apply_before_the_cap(conn, monkeypatch):
    monkeypatch.setattr(retrieval, "_PER_IDENTIFIER_ROWS", 2)
    monkeypatch.setattr(retrieval, "_FTS_MAX_ROWS", 50)
    eligible = _mk(conn, "record", metadata={"kind": "item-record", "item": 7}, project="memora")
    for i in range(6):
        _mk(conn, f"other repo record {i}", metadata={"kind": "item-record", "item": 7}, project="clmux")
    assert _ids(retrieval.identifier_lookup(conn, "Item 7", project="memora")) == [eligible]


def test_zero_weight_legs_are_skipped_and_knobs_validated(conn, monkeypatch):
    _mk(conn, "somethingelse")
    monkeypatch.setattr(storage, "semantic_search", lambda *a, **k: pytest.fail("dense ran at weight 0"))
    retrieval.search_v2(conn, "somethingelse", dense_weight=0)
    for bad in ({"dense_weight": float("nan")}, {"lexical_weight": -1}, {"rrf_k": 0}, {"dense_depth": 0}):
        with pytest.raises(ValueError):
            retrieval.search_v2(conn, "somethingelse", **bad)


def test_leader_selected_defaults():
    import inspect

    d = {k: v.default for k, v in inspect.signature(retrieval.search_v2).parameters.items()}
    assert (d["rrf_k"], d["lexical_weight"], d["identifier_weight"], d["dense_weight"]) == (20.0, 1.5, 1.0, 1.0)
    assert d["decompose"] is False and d["bm25_weights"] == (1.0, 0.3, 0.6)
    assert (d["dense_depth"], d["lexical_depth"], d["identifier_depth"], d["max_terms"]) == (100, 100, 50, 48)
