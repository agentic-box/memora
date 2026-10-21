"""Item 118: the read telemetry (MEMORA_READ_LEDGER, memora/read_ledger.py)."""

import asyncio
import json
import logging
import os
import threading
import time

import pytest

import memora.server as server
from memora import read_ledger

SECRET = "SENTINEL-BODY-do-not-log-7731"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("MEMORA_READ_LEDGER", raising=False)
    monkeypatch.delenv("MEMORA_READ_LEDGER_MAX_BYTES", raising=False)
    monkeypatch.setattr(read_ledger, "_warned", False)
    monkeypatch.setattr(read_ledger, "_dropped", 0)
    yield
    assert read_ledger.flush(10)   # nothing queued leaks into the next test


def _mk(content, tags=("task",), metadata=None):
    return asyncio.run(server.memory_create(content=content, tags=list(tags), metadata=metadata))["memory"]["id"]


def _lines(path):
    assert read_ledger.flush(10)
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_off_by_default_writes_nothing(local_db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(read_ledger, "_enqueue", lambda *a: calls.append(a))
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
        assert read_ledger.flush(10)
    strip = lambda r: {k: v for k, v in r.items() if k != "profile"}  # noqa: E731
    assert strip(r1) == strip(r2) == strip(baseline)
    warns = [r for r in caplog.records if r.name == "memora.read_ledger"]
    assert len(warns) == 1  # warned once, not per read
    # negative control: a writable path logs and does not warn
    caplog.clear()
    monkeypatch.setattr(read_ledger, "_warned", False)
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(tmp_path / "ok.jsonl"))
    asyncio.run(server.memory_get(mid))
    assert read_ledger.flush(10)
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


def _record_many(n, caller="c", query=None):
    for i in range(n):
        read_ledger.record("memory_hybrid_search", {"query": query or f"q{i}", "memory_id": i}, {"results": [{"memory": {"id": i}}]},
                           1.0, caller_fn=lambda: caller)
        if i % 100 == 99:
            assert read_ledger.flush(10)   # stay under the queue bound: dropping is tested on its own
    assert read_ledger.flush(10)
    assert read_ledger._dropped == 0


def _watch_total(monkeypatch, path):
    """Record the live + rotated BYTES after every real append."""
    seen = []
    real = read_ledger._append

    def spy(p, data, cap):
        real(p, data, cap)
        seen.append(sum(os.path.getsize(f) for f in (p, p + ".1") if os.path.exists(f)))

    monkeypatch.setattr(read_ledger, "_append", spy)
    return seen


def test_rotation_keeps_one_old_file_and_the_total_under_the_cap(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    monkeypatch.setenv("MEMORA_READ_LEDGER_MAX_BYTES", str(read_ledger.MIN_MAX_BYTES))
    seen = _watch_total(monkeypatch, path)
    _record_many(1500)
    assert (tmp_path / "led.jsonl.1").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["led.jsonl", "led.jsonl.1"]  # one rotation only
    assert max(seen) <= read_ledger.MIN_MAX_BYTES          # at EVERY append, not only at the end
    assert max(seen) > read_ledger.MIN_MAX_BYTES // 2      # ...and the spy saw the files fill up
    ids = [r["requested_id"] for r in _lines(path)]
    assert ids[-1] == 1499 and ids == sorted(ids)
    # negative control: the default cap does not rotate the same lines
    big = tmp_path / "big"
    big.mkdir()
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(big / "led.jsonl"))
    monkeypatch.delenv("MEMORA_READ_LEDGER_MAX_BYTES")
    _record_many(1500)
    assert [p.name for p in big.iterdir()] == ["led.jsonl"]


def test_the_cap_counts_utf8_bytes_and_a_tiny_cap_is_raised(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    monkeypatch.setenv("MEMORA_READ_LEDGER_MAX_BYTES", "1")        # clamped to MIN_MAX_BYTES
    seen = _watch_total(monkeypatch, path)
    _record_many(600, caller="é" * 80, query="日本語" * 100)
    assert max(seen) <= read_ledger.MIN_MAX_BYTES
    assert read_ledger._max_bytes() == read_ledger.MIN_MAX_BYTES
    last = _lines(path)[-1]
    assert len(last["query"]) == 200 and len(last["caller"].encode()) == 160   # chars != bytes
    # the longest possible line (200 ids with versions, non-ASCII everywhere) fits in half the floor
    worst = read_ledger.build_line(
        "memory_hybrid_search", {"query": "日" * 500, "metadata_filters": {"project": "日" * 200}, "memory_id": 10**12},
        {"count": 10**6, "results": [{"memory": {"id": 10**12 + i, "updated_at": "2026-10-21T12:00:00.123456+00:00"}} for i in range(500)]},
        123456.789, "ストア" * 40, "é" * 200)
    assert len(worst.encode()) <= read_ledger.MIN_MAX_BYTES // 2


def test_a_lowered_cap_discards_the_oversized_old_files(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    monkeypatch.setenv("MEMORA_READ_LEDGER_MAX_BYTES", str(read_ledger.MIN_MAX_BYTES))
    (tmp_path / "led.jsonl.1").write_bytes(b"x" * 300_000)   # written under a bigger cap
    path.write_bytes(b"y" * 100_000)
    seen = _watch_total(monkeypatch, path)
    _record_many(3)
    assert max(seen) <= read_ledger.MIN_MAX_BYTES
    assert [json.loads(l)["requested_id"] for l in path.read_text().splitlines()] == [0, 1, 2]
    # negative control: within the cap, old data is kept
    path.unlink()
    (tmp_path / "led.jsonl.1").write_bytes(b'{"keep":1}\n')
    _record_many(1)
    assert (tmp_path / "led.jsonl.1").read_bytes() == b'{"keep":1}\n'


def test_a_blocked_writer_never_blocks_the_read_or_the_event_loop(local_db, tmp_path, monkeypatch):
    mid = _mk("stable note")
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    gate, entered = threading.Event(), threading.Event()
    real = read_ledger._append

    def stuck(p, data, cap):
        entered.set()
        assert gate.wait(30)
        real(p, data, cap)

    monkeypatch.setattr(read_ledger, "_append", stuck)
    ticks = []

    async def scenario():
        async def heartbeat():
            while True:
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        t0 = time.perf_counter()
        for _ in range(3):
            await server.memory_get(mid)          # the first line is stuck in the writer
        dt = time.perf_counter() - t0
        await asyncio.sleep(0.1)
        hb.cancel()
        return dt

    dt = asyncio.run(scenario())
    assert entered.is_set() and dt < 10               # the reads finished while the write was stuck
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 1.0                    # the loop kept ticking
    assert not path.exists()                           # nothing was written yet
    gate.set()                                         # negative control: released, all 3 lines arrive
    assert len(_lines(path)) == 3


def test_a_full_queue_drops_lines_without_blocking_or_raising(tmp_path, monkeypatch, caplog):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    gate = threading.Event()
    real = read_ledger._append
    monkeypatch.setattr(read_ledger, "_append", lambda p, d, c: (gate.wait(30), real(p, d, c)))
    n = read_ledger.QUEUE_MAX + 50
    with caplog.at_level(logging.WARNING, logger="memora.read_ledger"):
        t0 = time.perf_counter()
        for i in range(n):
            read_ledger.record("memory_get", {"memory_id": i}, {"memory": {"id": i}}, 1.0)
        assert time.perf_counter() - t0 < 5
    assert read_ledger._dropped >= 49
    assert len([r for r in caplog.records if r.name == "memora.read_ledger"]) == 1   # warned once
    gate.set()
    # negative control: what was accepted is written, in order, and nothing else
    ids = [r["requested_id"] for r in _lines(path)]
    assert len(ids) == n - read_ledger._dropped and ids == sorted(ids) and ids[0] == 0


def test_ids_survive_field_projection_and_big_responses_are_marked(local_db, tmp_path, monkeypatch):
    a = _mk("projection alpha note about deploys")
    b = _mk("projection beta note about deploys")
    asyncio.run(server.memory_update(a, content="projection alpha note about deploys, edited"))
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    full = asyncio.run(server.memory_get(a))["memory"]
    asyncio.run(server.memory_get(a, fields=["content"]))
    asyncio.run(server.memory_list(query="projection", fields=["content_preview"]))
    asyncio.run(server.memory_semantic_search("projection note", fields=["content_preview"]))
    asyncio.run(server.memory_hybrid_search("projection note", fields=["score"]))
    rows = _lines(path)
    assert full.get("updated_at")
    assert [m["id"] for m in rows[1]["memories"]] == [a] and rows[1]["memories"][0]["v"] == full["updated_at"]
    for r in rows[2:]:
        assert {a, b} <= {m["id"] for m in r["memories"]}, r
        assert not r.get("truncated")
    # a response larger than MAX_IDS is marked, not silently cut
    monkeypatch.setattr(read_ledger, "MAX_IDS", 1)
    asyncio.run(server.memory_list(query="projection", limit=-1))
    big = _lines(path)[-1]
    assert big["truncated"] == big["count"] >= 2 and len(big["memories"]) == 1


def test_follow_latest_and_related_are_recorded_with_real_ids(local_db, tmp_path, monkeypatch):
    old = _mk("lineage old toolchain instruction zig 0.14")
    new = _mk("lineage current toolchain instruction zig 0.15")
    asyncio.run(server.memory_link(new, old, "supersedes"))
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    asyncio.run(server.memory_get(old))
    asyncio.run(server.memory_get(old, follow="all"))
    rel = asyncio.run(server.memory_related(new, refresh=True))
    redirected, exact, related = _lines(path)
    assert redirected["requested_id"] == old and [m["id"] for m in redirected["memories"]] == [new]
    assert [m["id"] for m in exact["memories"]] == [old]
    assert [m["id"] for m in related["memories"]] == [e["id"] for e in rel["related"]]
    assert related["memories"], "the fixture must produce a non-empty related list"


def test_caller_label_comes_from_the_hook_and_is_optional(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    read_ledger.record("memory_get", {}, {"memory": {"id": 1}}, 1.0, caller_fn=lambda: "ab-leader")
    read_ledger.record("memory_get", {}, {"memory": {"id": 1}}, 1.0, caller_fn=lambda: None)
    a, b = _lines(path)
    assert a["caller"] == "ab-leader" and "caller" not in b
    assert server._ledger_caller() is None  # no request in scope: no label, no error


def test_an_oversized_line_is_dropped_at_the_write_boundary(tmp_path, monkeypatch, caplog):
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    monkeypatch.setenv("MEMORA_READ_LEDGER_MAX_BYTES", str(read_ledger.MIN_MAX_BYTES))
    seen = _watch_total(monkeypatch, path)
    huge = "日" * 200_000
    with caplog.at_level(logging.WARNING, logger="memora.read_ledger"):
        read_ledger.record("memory_get", {}, {"memory": {"id": 1, "updated_at": huge}}, 1.0, store_fn=lambda: huge)
        read_ledger.record("memory_get", {}, {"memory": {"id": 2}}, 1.0)
        assert read_ledger.flush(10)
    rows = _lines(path)
    assert [r["memories"][0]["id"] for r in rows] == [1, 2]       # bounded strings keep the line small
    assert len(rows[0]["store"]) == 80 and len(rows[0]["memories"][0]["v"]) == 40
    assert max(seen) <= read_ledger.MIN_MAX_BYTES
    # the string bounds hold the oversized fixture to a short line; a line that still exceeds
    # half the cap is refused by _enqueue itself
    written = []
    monkeypatch.setattr(read_ledger, "_append", lambda p, d, c: written.append(d))
    read_ledger._enqueue(str(path), b"x" * (read_ledger.MIN_MAX_BYTES // 2 + 1), read_ledger.MIN_MAX_BYTES)
    assert read_ledger.flush(10) and written == [] and read_ledger._dropped >= 1
    read_ledger._enqueue(str(path), b"x" * (read_ledger.MIN_MAX_BYTES // 2), read_ledger.MIN_MAX_BYTES)   # control
    assert read_ledger.flush(10) and len(written) == 1


def test_a_short_os_write_still_produces_one_whole_line(tmp_path, monkeypatch):
    path = tmp_path / "led.jsonl"
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: real(fd, bytes(b)[:7]))   # every write is short
    read_ledger._append(str(path), b'{"a":1234567890}\n', 100_000)
    monkeypatch.undo()
    assert path.read_bytes() == b'{"a":1234567890}\n'


def test_a_validation_error_after_note_logs_no_ids(local_db, tmp_path, monkeypatch):
    mid = _mk("some note")
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv("MEMORA_READ_LEDGER", str(path))
    r = asyncio.run(server.memory_get(mid, fields=["no_such_field"]))
    assert "error" in r
    (row,) = _lines(path)
    assert row["memories"] == [] and row["error"] == "invalid_fields"
    asyncio.run(server.memory_get(mid))                      # control: a good read logs its id
    assert [m["id"] for m in _lines(path)[-1]["memories"]] == [mid]
