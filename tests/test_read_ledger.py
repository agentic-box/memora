"""Item 118: the read telemetry (MEMORA_READ_LEDGER, memora/read_ledger.py)."""

import asyncio
import json
import logging

import pytest

import memora.server as server
from memora import read_ledger

SECRET = "SENTINEL-BODY-do-not-log-7731"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("MEMORA_READ_LEDGER", raising=False)
    monkeypatch.delenv("MEMORA_READ_LEDGER_MAX_BYTES", raising=False)
    monkeypatch.setattr(read_ledger, "_warned", False)


def _mk(content, tags=("task",), metadata=None):
    return asyncio.run(server.memory_create(content=content, tags=list(tags), metadata=metadata))["memory"]["id"]


def _lines(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_off_by_default_writes_nothing(local_db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(read_ledger, "_append", lambda *a: calls.append(a))
    mid = _mk("alpha note")
    asyncio.run(server.memory_get(mid))
    assert calls == []
    # control: the same read with the variable set appends
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(tmp_path / "led.jsonl"))
    asyncio.run(server.memory_get(mid))
    assert len(calls) == 1


def test_each_read_tool_appends_one_line_with_ids(local_db, tmp_path, monkeypatch):
    a = _mk("deploy watchdog restart policy")
    b = _mk("graph sidebar layout notes")
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    asyncio.run(server.memory_hybrid_search("deploy watchdog"))
    asyncio.run(server.memory_semantic_search("graph sidebar"))
    asyncio.run(server.memory_list(query="deploy"))
    asyncio.run(server.memory_get(b))
    asyncio.run(server.memory_related(a))
    rows = _lines(path)
    assert [r["tool"] for r in rows] == list(read_ledger.READ_TOOLS[:2]) + ["memory_list", "memory_get", "memory_related"]
    hy, _, ls, get, rel = rows
    assert a in [m["id"] for m in hy["memories"]] and hy["count"] == len(hy["memories"])
    assert hy["query"] == "deploy watchdog" and len(hy["query_hash"]) == 16
    assert [m["id"] for m in get["memories"]] == [b] and get["requested_id"] == b
    assert all(isinstance(r["latency_ms"], (int, float)) and r["ts"].endswith("Z") for r in rows)
    assert a in [m["id"] for m in ls["memories"]]
    assert rel["tool"] == "memory_related" and "query" not in rel
    # negative control: a write tool is not a read tool and adds no line
    _mk("another note")
    assert len(_lines(path)) == 5


def test_error_response_is_recorded_with_its_code(local_db, tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    asyncio.run(server.memory_get(999999))
    (row,) = _lines(path)
    assert row["error"] == "not_found" and row["count"] == 0 and row["memories"] == []


def test_no_memory_content_tags_or_metadata_in_the_log(local_db, tmp_path, monkeypatch):
    mid = _mk(f"body {SECRET}", tags=("task", "tag-sentinel-55"), metadata={"note": "meta-sentinel-66"})
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    asyncio.run(server.memory_hybrid_search("body"))
    asyncio.run(server.memory_get(mid))
    asyncio.run(server.memory_list(query="body", content_mode="full"))
    text = path.read_text()
    assert text.count("\n") == 3 and '"id":%d' % mid in text
    for needle in (SECRET, "tag-sentinel-55", "meta-sentinel-66"):
        assert needle not in text
    # negative control: the scan can see a needle that is logged (the query text)
    asyncio.run(server.memory_hybrid_search(SECRET))
    assert SECRET in path.read_text()


def test_query_text_is_truncated_and_hash_covers_the_whole_query(local_db, tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    long_q = "x" * 500
    asyncio.run(server.memory_hybrid_search(long_q))
    asyncio.run(server.memory_hybrid_search("x" * 200 + "y"))
    a, b = _lines(path)
    assert len(a["query"]) == 200 and len(b["query"]) == 200
    assert a["query_hash"] != b["query_hash"]  # same 200-char prefix, different hash


def test_a_failing_append_never_fails_or_changes_the_read(local_db, tmp_path, monkeypatch, caplog):
    mid = _mk("stable note")
    baseline = asyncio.run(server.memory_get(mid))
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(blocker / "sub" / "led.jsonl"))  # parent is a file
    with caplog.at_level(logging.WARNING, logger="memora.read_ledger"):
        r1 = asyncio.run(server.memory_get(mid))
        r2 = asyncio.run(server.memory_get(mid))
    strip = lambda r: {k: v for k, v in r.items() if k != "profile"}  # noqa: E731
    assert strip(r1) == strip(r2) == strip(baseline)
    warns = [r for r in caplog.records if r.name == "memora.read_ledger"]
    assert len(warns) == 1  # warned once, not per read
    # negative control: a writable path logs and does not warn
    caplog.clear()
    monkeypatch.setattr(read_ledger, "_warned", False)
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(tmp_path / "ok.jsonl"))
    asyncio.run(server.memory_get(mid))
    assert not [r for r in caplog.records if r.name == "memora.read_ledger"]
    assert (tmp_path / "ok.jsonl").exists()


def test_a_tool_that_raises_still_raises_and_is_logged(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))

    @read_ledger.ledgered("memory_get")
    async def boom(memory_id: int):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(boom(7))
    (row,) = _lines(path)
    assert row["error"] == "RuntimeError" and row["requested_id"] == 7


def test_rotation_keeps_one_old_file_and_caps_the_live_one(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    monkeypatch.setenv("MEMORA_READ_LEDGER_MAX_BYTES", "1000")
    for i in range(60):
        read_ledger.record("memory_get", {"memory_id": i}, {"memory": {"id": i}}, 1.0)
    assert (tmp_path / "led.jsonl.1").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["led.jsonl", "led.jsonl.1"]  # one rotation only
    assert path.stat().st_size + (tmp_path / "led.jsonl.1").stat().st_size <= 1000  # the cap is the TOTAL
    ids = [r["requested_id"] for r in _lines(path)]
    assert ids[-1] == 59 and ids == sorted(ids)
    # negative control: the default cap does not rotate 60 small lines
    big = tmp_path / "big"
    big.mkdir()
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(big / "led.jsonl"))
    monkeypatch.delenv("MEMORA_READ_LEDGER_MAX_BYTES")
    for i in range(60):
        read_ledger.record("memory_get", {"memory_id": i}, {"memory": {"id": i}}, 1.0)
    assert [p.name for p in big.iterdir()] == ["led.jsonl"]


def test_caller_label_comes_from_the_hook_and_is_optional(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    read_ledger.record("memory_get", {}, {"memory": {"id": 1}}, 1.0, caller_fn=lambda: "ab-leader")
    read_ledger.record("memory_get", {}, {"memory": {"id": 1}}, 1.0, caller_fn=lambda: None)
    a, b = _lines(path)
    assert a["caller"] == "ab-leader" and "caller" not in b
    assert server._ledger_caller() is None  # no request in scope: no label, no error
