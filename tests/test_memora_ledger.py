"""Item 118: scripts/memora-ledger (decision ledger CLI and report)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memora-ledger"
NOW = "2026-10-21T12:00:00Z"


@pytest.fixture
def led(tmp_path):
    env = {**os.environ, "MEMORA_DECISION_LEDGER": str(tmp_path / "d.jsonl")}
    env.pop("MEMORA_READ_LEDGER", None)

    def run(*args):
        p = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    run.path = tmp_path / "d.jsonl"
    run.dir = tmp_path
    return run


def test_draft_then_label_roundtrip(led):
    assert led("draft", "item-1", "use plan A")[0] == 0
    assert led("label", "item-1", "changed", "--memory", "5", "--memory", "7", "switched to plan B")[0] == 0
    rows = [json.loads(l) for l in led.path.read_text().splitlines()]
    assert [r["type"] for r in rows] == ["draft", "label"]
    assert rows[1]["label"] == "changed" and rows[1]["memories"] == [5, 7]


def test_unknown_label_is_refused(led):
    led("draft", "item-1", "x")
    rc, _, err = led("label", "item-1", "helpful", "did a thing")
    assert rc == 2 and "unknown label" in err
    assert len(led.path.read_text().splitlines()) == 1  # nothing written
    # negative control: a known label is accepted
    assert led("label", "item-1", "unchanged", "kept it")[0] == 0


def test_label_and_missed_need_a_prior_draft(led):
    rc, _, err = led("label", "item-9", "unchanged", "kept")
    assert rc == 2 and "no draft" in err
    rc, _, err = led("missed", "item-9", "--memory", "3", "never found")
    assert rc == 2 and "no draft" in err
    assert not led.path.exists()
    led("draft", "item-9", "x")  # control
    assert led("label", "item-9", "unchanged", "kept")[0] == 0
    assert led("missed", "item-9", "--memory", "3", "never found")[0] == 0


def test_draft_twice_empty_text_and_bad_inputs_are_refused(led):
    assert led("draft", "d", "x")[0] == 0
    assert led("draft", "d", "y")[0] == 2
    assert led("draft", "e", "  ")[0] == 2
    assert led("missed", "d", "text only")[0] == 2           # --memory required
    assert led("read-back", "text only")[0] == 2             # --memory required
    assert led("read-back", "--memory", "3", "--created", "yesterday", "why")[0] == 2
    assert led("read-back", "--memory", "3", "--created", "2026-10-20T00:00:00Z", "why")[0] == 0


def test_report_on_fixtures(led, tmp_path):
    def w(path, rows):
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    w(led.path, [
        {"ts": "2026-10-01T09:00:00Z", "type": "draft", "id": "old-1", "text": "t"},
        {"ts": "2026-10-01T09:05:00Z", "type": "label", "id": "old-1", "label": "changed", "memories": [1], "text": "old"},
        {"ts": "2026-10-20T10:00:00Z", "type": "draft", "id": "d1", "text": "t"},
        {"ts": "2026-10-20T10:10:00Z", "type": "label", "id": "d1", "label": "changed", "memories": [10, 11], "text": "switched"},
        {"ts": "2026-10-20T11:00:00Z", "type": "draft", "id": "d2", "text": "t"},
        {"ts": "2026-10-20T11:10:00Z", "type": "label", "id": "d2", "label": "unchanged", "memories": [], "text": "same"},
        {"ts": "2026-10-20T12:00:00Z", "type": "draft", "id": "d3", "text": "t"},
        {"ts": "2026-10-20T12:10:00Z", "type": "label", "id": "d3", "label": "misleading", "memories": [12], "text": "stale"},
        {"ts": "2026-10-20T12:20:00Z", "type": "missed", "id": "d3", "memories": [99], "text": "m"},
        {"ts": "2026-10-20T13:00:00Z", "type": "draft", "id": "d4", "text": "t"},
        {"ts": "2026-10-21T00:00:00Z", "type": "readback", "memories": [50], "created": "2026-10-20T12:00:00Z", "text": "r"},
        {"ts": "2026-10-21T06:00:00Z", "type": "readback", "memories": [50], "text": "again"},
    ])
    tel = tmp_path / "t.jsonl"
    w(tel, [
        {"ts": "2026-10-20T10:02:00Z", "tool": "memory_hybrid_search", "latency_ms": 100, "memories": [{"id": 10}, {"id": 55}], "count": 2},
        {"ts": "2026-10-20T10:03:00Z", "tool": "memory_get", "latency_ms": 5, "memories": [{"id": 10}], "count": 1},
        {"ts": "2026-10-20T10:20:00Z", "tool": "memory_hybrid_search", "latency_ms": 900, "memories": [{"id": 11}], "count": 1},  # outside d1's window
        {"ts": "2026-10-20T11:05:00Z", "tool": "memory_semantic_search", "latency_ms": 300, "memories": [{"id": 77}], "count": 1},
        {"ts": "2026-10-20T12:05:00Z", "tool": "memory_list", "latency_ms": 200, "memories": [{"id": 12}], "count": 1},
    ])
    rc, out, err = led("report", "--since", "7d", "--telemetry", str(tel), "--now", NOW, "--seed", "1")
    assert rc == 0, err
    assert "decisions drafted: 4" in out                         # old-1 is outside 7d
    assert "  changed: 1" in out and "  unchanged: 1" in out and "  misleading: 1" in out
    assert "  avoided-repeat: 0" in out and "  unlabeled: 1" in out
    assert "labeled decision(s): 3" in out and "(memory_hybrid_search/memory_semantic_search/memory_list)" in out
    assert "  d1: 1 search(es)" in out                          # the 10:20 call is outside the draft..label window
    assert "p50 200  p95 300  max 300" in out                   # 100, 300, 200 -> memory_get not a search
    assert "1 of 3 cited memory id(s)" not in out
    assert "2 of 3 cited memory id(s) were returned" in out     # 10 and 12 returned; 11 is outside the window
    assert "misleading: 1   missed: 1 event(s) on 1 decision(s)" in out
    assert "item-record read-backs: 2 event(s), 1 distinct memory id(s)" in out
    assert "memory 50: first read 12.0 h after creation" in out
    for forced in ("d1  [changed]", "d3  [misleading]"):
        assert forced in out
    assert "d2  [changed]" not in out
    assert "ceil(20% of 4) = 1" in out
    # negative controls: all time sees the old decision; a narrow window drops the rest
    _, all_out, _ = led("report", "--telemetry", str(tel), "--now", NOW)
    assert "decisions drafted: 5" in all_out and "old-1  [changed]" in all_out
    _, narrow, _ = led("report", "--since", "1h", "--telemetry", str(tel), "--now", NOW)
    assert "decisions drafted: 0" in narrow and "d1" not in narrow


def test_report_reads_the_rotated_telemetry_and_tolerates_a_torn_line(led, tmp_path):
    led("draft", "x", "t")
    led("label", "x", "unchanged", "--memory", "4", "kept")
    rows = [json.loads(l) for l in led.path.read_text().splitlines()]
    ts = rows[0]["ts"]
    tel = tmp_path / "t.jsonl"
    (tmp_path / "t.jsonl.1").write_text(json.dumps({"ts": ts, "tool": "memory_hybrid_search", "latency_ms": 7, "memories": [{"id": 4}]}) + "\n")
    tel.write_text('{"ts": "torn')
    rc, out, err = led("report", "--telemetry", str(tel))
    assert rc == 0, err
    assert "1 of 1 cited memory id(s) were returned" in out
    # negative control: without the rotated file nothing is joined
    (tmp_path / "t.jsonl.1").unlink()
    assert "0 of 1 cited memory id(s) were returned" in led("report", "--telemetry", str(tel))[1]


def test_report_refuses_a_bad_since_and_a_corrupt_ledger(led):
    assert led("report", "--since", "7x")[0] == 2
    led.path.write_text("not json\n")
    assert led("report")[0] == 2
    led.path.write_text("")
    assert led("report")[0] == 0
