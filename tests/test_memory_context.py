"""Item 263: the memory_context MCP tool (synthetic stores only)."""
import asyncio

import pytest

from memora import server


def _mk(content, tags=None, metadata=None):
    r = asyncio.run(server.memory_create(
        content=content, tags=tags or ["ctx"], metadata=metadata, suggest_similar=False))
    return r["memory"]["id"]


def _ctx(query="zorblax quuxwidget", **kw):
    return asyncio.run(server.memory_context(query, **kw))


def _v2_ids(query, n, **kw):
    r = asyncio.run(server.memory_hybrid_search(query, limit=n, **kw))
    return [e["memory"]["id"] for e in r["results"]]


def _digest(project, body, **extra):
    return _mk(body, tags=[f"{project}/digest"],
               metadata={"kind": "digest", "project": project, **extra})


def test_hits_follow_v2_rank_order_with_shape(local_db):
    for i in range(6):
        _mk(f"zorblax quuxwidget note {i} " + "filler " * i,
            metadata={"item": 200 + i} if i == 2 else None)
    out = _ctx(limit=5)
    assert [h["id"] for h in out["hits"]] == _v2_ids("zorblax quuxwidget", 5), (
        "mutation: reorder hits and this goes red")
    h = out["hits"][0]
    assert set(h) >= {"id", "score", "preview", "tags", "created_at"}
    assert out["digest"] is None and out["truncated"] is False
    assert out["query"] == "zorblax quuxwidget"
    assert out["budget_used"] == sum(len(x["preview"]) for x in out["hits"])
    items = {x["id"]: x.get("item") for x in out["hits"]}
    assert 202 in items.values() and sum(v is not None for v in items.values()) == 1


def test_preview_is_whitespace_collapsed_and_capped(local_db):
    _mk("zorblax   quuxwidget\n\n" + "word  " * 200)
    [h] = _ctx(preview_chars=100)["hits"]
    assert len(h["preview"]) == 100 and "  " not in h["preview"] and "\n" not in h["preview"]


def test_budget_truncation_stops_before_overflow(local_db):
    for i in range(10):
        _mk(f"zorblax quuxwidget {i} " + "x" * 300)
    out = _ctx(limit=10, budget_chars=1000, preview_chars=300)
    assert out["truncated"] is True
    assert 0 < len(out["hits"]) < 10
    assert out["budget_used"] <= 1000
    assert out["budget_used"] == sum(len(h["preview"]) for h in out["hits"])
    big = _ctx(limit=10, budget_chars=100000, preview_chars=300)
    assert big["truncated"] is False and len(big["hits"]) == 10


def test_limit_caps_hits_without_truncation_flag(local_db):
    for i in range(6):
        _mk(f"zorblax quuxwidget {i}")
    out = _ctx(limit=3)
    assert len(out["hits"]) == 3 and out["truncated"] is False


def test_digest_is_newest_active_for_project_and_excluded_from_hits(local_db):
    _mk("zorblax quuxwidget regular memory")
    old = _digest("proj-a", "zorblax quuxwidget digest old")
    new = _digest("proj-a", "zorblax quuxwidget digest new")
    _digest("proj-b", "zorblax quuxwidget digest other project")
    out = _ctx(project="proj-a")
    assert out["digest"]["id"] == new, "mutation: pick the oldest and this goes red"
    assert out["digest"]["content"] == "zorblax quuxwidget digest new"
    assert out["digest"]["created_at"]
    hit_ids = [h["id"] for h in out["hits"]]
    assert new not in hit_ids and old not in hit_ids
    assert all(i != out["digest"]["id"] for i in hit_ids)
    assert len(hit_ids) == 1, "other-project digest must not appear as a hit either"


def test_superseded_digest_is_ignored(local_db):
    old = _digest("proj-a", "zorblax quuxwidget digest old")
    new = _digest("proj-a", "zorblax quuxwidget digest new")
    asyncio.run(server.memory_link(new, old, "supersedes"))
    assert _ctx(project="proj-a")["digest"]["id"] == new
    # the newest by id is superseded by an older-id memory: it must be skipped
    newer = _digest("proj-a", "zorblax quuxwidget digest newest")
    asyncio.run(server.memory_link(old, newer, "supersedes"))
    assert _ctx(project="proj-a")["digest"]["id"] != newer


def test_no_digest_is_null_not_error(local_db):
    _mk("zorblax quuxwidget regular")
    assert _ctx(project="proj-a")["digest"] is None
    assert _ctx()["digest"] is None


def test_include_digest_false_and_project_from_metadata_filter(local_db):
    d = _digest("proj-a", "zorblax quuxwidget digest")
    assert _ctx(project="proj-a", include_digest=False)["digest"] is None
    assert _ctx(metadata_filters={"project": "proj-a"})["digest"]["id"] == d


def test_long_digest_is_capped_at_half_budget(local_db):
    _digest("proj-a", "zorblax " + "d" * 5000)
    out = _ctx(project="proj-a", budget_chars=2000)
    assert len(out["digest"]["content"]) == 1000
    assert out["budget_used"] >= 1000
    short = _digest("proj-c", "short digest")
    out = _ctx(project="proj-c", budget_chars=2000)
    assert out["digest"]["id"] == short and out["digest"]["content"] == "short digest"


def test_filters_are_passed_through(local_db):
    a = _mk("zorblax quuxwidget alpha", tags=["keep"])
    _mk("zorblax quuxwidget beta", tags=["drop"])
    assert [h["id"] for h in _ctx(tags_all=["keep"])["hits"]] == [a]
    assert a not in [h["id"] for h in _ctx(tags_none=["keep"])["hits"]]
    assert [h["id"] for h in _ctx(tags_any=["keep"])["hits"]] == [a]
    m = _mk("zorblax quuxwidget gamma", metadata={"item": 7})
    assert [h["id"] for h in _ctx(metadata_filters={"item": 7})["hits"]] == [m]
    assert _ctx(date_from="2999-01-01")["hits"] == []


@pytest.mark.parametrize("kw,needle", [
    ({"query": "   "}, "query"),
    ({"limit": 0}, "limit"),
    ({"limit": 101}, "limit"),
    ({"budget_chars": 999}, "budget_chars"),
    ({"preview_chars": 79}, "preview_chars"),
])
def test_validation_errors(local_db, kw, needle):
    out = _ctx(**{"query": "zorblax", **kw})
    assert out["error"] == "invalid_input" and needle in out["message"]


def test_invalid_follow_is_an_error(local_db):
    assert _ctx(follow="bogus")["error"] == "invalid_follow"


def test_read_ledger_records_context_call(local_db, tmp_path, monkeypatch):
    import json
    a = _mk("zorblax quuxwidget ledgered")
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    _ctx()
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["memory_context"]
    assert a in [m["id"] for m in rows[0]["memories"]]
