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
    assert "item-record read-backs: 2 event(s) in the window, 1 distinct memory id(s)" in out
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
    assert led("report", "--no-telemetry")[0] == 2
    led.path.write_text("")
    assert led("report", "--no-telemetry")[0] == 0


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _overlap_fixture(led, tmp_path, draft_extra=None):
    """Two OVERLAPPING decisions, two callers, two stores that BOTH contain id 1."""
    ex = draft_extra or {}
    _w(led.path, [
        {"ts": "2026-10-20T10:00:00Z", "type": "draft", "id": "A", "text": "t", **ex.get("A", {})},
        {"ts": "2026-10-20T10:01:00Z", "type": "draft", "id": "B", "text": "t", **ex.get("B", {})},
        {"ts": "2026-10-20T10:10:00Z", "type": "label", "id": "A", "label": "changed", "memories": [1], "text": "a"},
        {"ts": "2026-10-20T10:11:00Z", "type": "label", "id": "B", "label": "changed", "memories": [1], "text": "b"},
    ])
    tel = tmp_path / "t.jsonl"
    _w(tel, [
        {"ts": "2026-10-20T10:05:00Z", "tool": "memory_hybrid_search", "latency_ms": 10, "caller": "alice", "store": "s1", "memories": [{"id": 1}]},
        {"ts": "2026-10-20T10:06:00Z", "tool": "memory_hybrid_search", "latency_ms": 20, "caller": "bob", "store": "s2", "memories": [{"id": 2}]},
    ])
    return tel


def test_ambiguous_attribution_is_not_counted_as_cited_returned(led, tmp_path):
    tel = _overlap_fixture(led, tmp_path)
    rc, out, err = led("report", "--telemetry", str(tel), "--now", NOW)
    assert rc == 0, err
    assert "cited vs returned over 0 unambiguous decision(s): 0 of 0 cited" in out
    assert "ambiguous decisions, not counted above: 2" in out
    assert "A: overlaps another decision; reads from stores s1,s2; reads from callers alice,bob" in out
    assert "  A: 2 search(es) [ambiguous]" in out
    # negative control: naming the caller and store on each draft attributes the reads, and the
    # other caller's / store's id 1 is not credited
    tel = _overlap_fixture(led, tmp_path, {"A": {"caller": "alice", "store": "s1"}, "B": {"caller": "bob", "store": "s2"}})
    out = led("report", "--telemetry", str(tel), "--now", NOW)[1]
    assert "cited vs returned over 2 unambiguous decision(s): 1 of 2 cited" in out   # A: id 1 returned; B: id 1 is s1's
    assert "ambiguous decisions, not counted above: 0" in out
    assert "  A: 1 search(es)" in out and "  B: 1 search(es)" in out


def test_draft_records_store_and_caller(led):
    assert led("draft", "d", "x", "--store", "memora", "--caller", "ab-leader")[0] == 0
    row = json.loads(led.path.read_text())
    assert row["store"] == "memora" and row["caller"] == "ab-leader"
    assert led("draft", "e", "x")[0] == 0
    assert "store" not in json.loads(led.path.read_text().splitlines()[1])


def test_a_truncated_read_makes_the_decision_ambiguous(led, tmp_path):
    led("draft", "t", "x")
    led("label", "t", "changed", "--memory", "9", "c")
    ts = json.loads(led.path.read_text().splitlines()[0])["ts"]
    tel = tmp_path / "t.jsonl"
    _w(tel, [{"ts": ts, "tool": "memory_list", "latency_ms": 5, "memories": [{"id": 1}], "count": 500, "truncated": 500}])
    out = led("report", "--telemetry", str(tel))[1]
    assert "t: a read returned more ids than were logged" in out and "unambiguous decision(s): 0 of 0" in out
    _w(tel, [{"ts": ts, "tool": "memory_list", "latency_ms": 5, "memories": [{"id": 9}], "count": 1}])   # control
    assert "unambiguous decision(s): 1 of 1" in led("report", "--telemetry", str(tel))[1]


def test_first_read_is_all_time_even_when_reporting_a_later_window(led, tmp_path):
    _w(led.path, [
        {"ts": "2026-10-01T10:00:00Z", "type": "readback", "memories": [50], "created": "2026-10-01T09:00:00Z", "text": "r"},
        {"ts": "2026-10-20T10:00:00Z", "type": "readback", "memories": [50], "text": "again"},
        {"ts": "2026-10-20T11:00:00Z", "type": "readback", "memories": [51], "created": "2026-10-20T10:00:00Z", "text": "r"},
    ])
    out = led("report", "--since", "7d", "--no-telemetry", "--now", NOW)[1]
    assert "2 event(s) in the window, 2 distinct memory id(s)" in out
    assert "memory 50: first read 1.0 h after creation (before the window)" in out   # not the 10-20 read
    assert "memory 51: first read 1.0 h after creation" in out and "memory 51: first read 1.0 h after creation (before" not in out
    # negative control: an all-time window shows the same first read, without the marker
    out = led("report", "--no-telemetry", "--now", NOW)[1]
    assert "memory 50: first read 1.0 h after creation\n" in out


def test_same_caller_overlap_and_open_drafts_are_still_ambiguous(led, tmp_path):
    base = [
        {"ts": "2026-10-20T10:00:00Z", "type": "draft", "id": "A", "text": "t", "caller": "alice", "store": "s1"},
        {"ts": "2026-10-20T10:01:00Z", "type": "draft", "id": "B", "text": "t", "caller": "alice", "store": "s1"},
        {"ts": "2026-10-20T10:10:00Z", "type": "label", "id": "A", "label": "changed", "memories": [1], "text": "a"},
        {"ts": "2026-10-20T10:11:00Z", "type": "label", "id": "B", "label": "changed", "memories": [1], "text": "b"},
    ]
    tel = tmp_path / "t.jsonl"
    _w(tel, [{"ts": "2026-10-20T10:05:00Z", "tool": "memory_hybrid_search", "latency_ms": 10, "caller": "alice", "store": "s1", "memories": [{"id": 1}]}])
    _w(led.path, base)
    out = led("report", "--telemetry", str(tel), "--now", NOW)[1]
    assert "over 0 unambiguous decision(s)" in out and "A: overlaps another decision" in out
    # an UNLABELED draft overlaps a labeled one too
    _w(led.path, [base[0], {**base[1], "id": "OPEN"}, base[2], {"ts": "2026-10-20T10:12:00Z", "type": "label", "id": "A", "label": "changed", "memories": [1], "text": "a"}][:3])
    out = led("report", "--telemetry", str(tel), "--now", NOW)[1]
    assert "A: overlaps another decision" in out
    # controls: different callers, or no overlap, are attributed
    _w(led.path, [base[0], {**base[1], "caller": "bob"}, base[2], base[3]])
    assert "ambiguous decisions, not counted above: 0" in led("report", "--telemetry", str(tel), "--now", NOW)[1]
    _w(led.path, [base[0], base[2], {**base[1], "ts": "2026-10-20T10:20:00Z"}, {**base[3], "ts": "2026-10-20T10:30:00Z"}])
    assert "ambiguous decisions, not counted above: 0" in led("report", "--telemetry", str(tel), "--now", NOW)[1]


def test_a_read_that_returned_an_error_does_not_count_as_returned(led, tmp_path):
    led("draft", "e", "x")
    led("label", "e", "changed", "--memory", "9", "c")
    ts = json.loads(led.path.read_text().splitlines()[0])["ts"]
    tel = tmp_path / "t.jsonl"
    _w(tel, [{"ts": ts, "tool": "memory_get", "latency_ms": 1, "memories": [{"id": 9}], "error": "invalid_fields"}])
    assert "over 1 unambiguous decision(s): 0 of 1" in led("report", "--telemetry", str(tel))[1]
    _w(tel, [{"ts": ts, "tool": "memory_get", "latency_ms": 1, "memories": [{"id": 9}]}])
    assert "over 1 unambiguous decision(s): 1 of 1" in led("report", "--telemetry", str(tel))[1]


def test_a_decision_with_a_store_only_credits_that_stores_reads(led, tmp_path):
    def run(store):
        led.path.unlink(missing_ok=True)
        led("draft", "s", "x", *(["--store", store] if store else []))
        led("label", "s", "changed", "--memory", "5", "c")
        ts = json.loads(led.path.read_text().splitlines()[0])["ts"]
        tel = tmp_path / "t.jsonl"
        _w(tel, [{"ts": ts, "tool": "memory_get", "latency_ms": 1, "store": "s1", "memories": [{"id": 1}]},
                 {"ts": ts, "tool": "memory_get", "latency_ms": 1, "store": "s2", "memories": [{"id": 5}]}])
        return led("report", "--telemetry", str(tel))[1]

    assert "over 1 unambiguous decision(s): 0 of 1" in run("s1")      # id 5 came from s2: not credited
    assert "over 1 unambiguous decision(s): 1 of 1" in run("s2")      # control
    assert "over 0 unambiguous decision(s)" in run(None)              # unnamed store, two stores: ambiguous


def _decided(led):
    led("draft", "x", "t")
    led("label", "x", "unchanged", "--memory", "4", "kept")
    return json.loads(led.path.read_text().splitlines()[0])["ts"]


def test_report_without_a_telemetry_file_fails_and_names_the_path(led, tmp_path):
    _decided(led)
    missing = tmp_path / "nope.jsonl"
    rc, out, err = led("report", "--telemetry", str(missing))
    assert rc == 2 and out == ""
    assert str(missing) in err and "--telemetry PATH" in err and "--no-telemetry" in err
    # default path: no env, no .ledger file -> still an error (never "joined: 0")
    rc, out, err = led("report", "--telemetry", str(tmp_path / "x" / "read-ledger.jsonl"))
    assert rc == 2 and "searches joined" not in out
    # a rotated file alone counts as present
    (tmp_path / "nope.jsonl.1").write_text("")
    assert led("report", "--telemetry", str(missing))[0] == 0


def test_report_no_telemetry_says_not_measured(led, tmp_path):
    _decided(led)
    rc, out, err = led("report", "--no-telemetry")
    assert rc == 0, err
    assert "decisions drafted: 1" in out and "  unchanged: 1" in out
    assert out.count("not measured") == 3
    assert "0 of 1" not in out and "search(es)" not in out and "WARNING" not in out
    assert led("report", "--no-telemetry", "--telemetry", str(tmp_path / "t"))[0] == 2


def test_report_warns_once_on_a_stale_telemetry_file(led, tmp_path):
    ts = _decided(led)
    tel = tmp_path / "t.jsonl"
    tel.write_text(json.dumps({"ts": "2020-01-01T00:00:00Z", "tool": "memory_hybrid_search", "memories": []}) + "\n")
    rc, out, err = led("report", "--telemetry", str(tel))
    assert rc == 0, err
    assert out.count("WARNING") == 1 and "stale" in out
    # normal path: a line inside the span -> no warning
    tel.write_text(json.dumps({"ts": ts, "tool": "memory_hybrid_search", "latency_ms": 7, "memories": [{"id": 4}]}) + "\n")
    out = led("report", "--telemetry", str(tel))[1]
    assert "WARNING" not in out and "1 of 1 cited memory id(s) were returned" in out
    # an empty file is stale too
    tel.write_text("")
    assert led("report", "--telemetry", str(tel))[1].count("WARNING") == 1


def _lab(led, did, label, *extra):
    assert led("draft", did, "x")[0] == 0
    assert led("label", did, label, *extra, "did it")[0] == 0


def test_audit_needs_a_labelled_decision_and_valid_args(led):
    assert led("audit", "i1", "confirm", "ok")[0] == 2        # no draft
    led("draft", "i1", "x")
    rc, _, err = led("audit", "i1", "confirm", "ok")
    assert rc == 2 and "no label" in err                       # drafted, unlabelled
    led("label", "i1", "changed", "did it")
    assert led("audit", "i1", "maybe", "ok")[0] == 2
    assert led("audit", "i1", "relabel", "bogus", "r")[0] == 2
    assert led("audit", "i1", "relabel", "r")[0] == 2          # relabel needs a label
    assert led("audit", "i1", "confirm", "unchanged", "r")[0] == 2
    assert led("audit", "i1", "confirm", "  ")[0] == 2
    assert len(led.path.read_text().splitlines()) == 2         # nothing written by refusals
    assert led("audit", "i1", "confirm", "seen in diff", "--by", "ab-reviewer")[0] == 0
    row = json.loads(led.path.read_text().splitlines()[-1])
    assert row["type"] == "audit" and row["by"] == "ab-reviewer" and row["label"] == "changed"


def test_report_without_audit_rows_has_zero_counts(led):
    _lab(led, "a", "changed")
    out = led("report", "--no-telemetry")[1]
    assert "verified wins" in out and ": 0\n" in out.split("verified wins")[1]
    assert "unaudited wins (changed/avoided-repeat, no audit yet): 1" in out
    assert "time saved (label carries --time-saved): 0" in out
    assert "progress against the rule: 1 of 40 decisions, 0 of 4 verified wins" in out
    assert "  changed: 1" in out


def test_audit_relabel_confirm_reject_drive_the_counts(led):
    for d in ("i120", "i123", "i127", "i131"):
        _lab(led, d, "changed")
        assert led("audit", d, "relabel", "unchanged", "no real change", "--by", "ab-reviewer")[0] == 0
    _lab(led, "i125", "unchanged")
    assert led("audit", "i125", "relabel", "avoided-repeat", "skipped a rerun")[0] == 0
    _lab(led, "i129", "changed")
    assert led("audit", "i129", "confirm", "diff matches")[0] == 0
    _lab(led, "i130", "changed")
    assert led("audit", "i130", "reject", "not supported")[0] == 0
    _lab(led, "i140", "avoided-repeat")
    out = led("report", "--no-telemetry")[1]
    assert "  unchanged: 4" in out and "  changed: 2" in out and "  avoided-repeat: 2" in out
    assert "verified wins (changed/avoided-repeat, reviewer-confirmed or relabelled to one): 2" in out
    assert "unaudited wins (changed/avoided-repeat, no audit yet): 1" in out
    assert "rejected wins (reviewer rejected the leader's label): 1" in out
    assert "2 of 4 verified wins" in out and "8 of 40 decisions" in out
    # the audit list shows effective labels and who audited
    assert "i125  [avoided-repeat]" in out and "(audited: confirm)" in out
    assert "i120  [unchanged]" not in out.split("every changed")[1].split("random sample")[0]


def test_last_audit_wins_and_a_stale_confirm_does_not_count(led):
    _lab(led, "a", "changed")
    led("audit", "a", "reject", "no")
    led("audit", "a", "confirm", "yes after all")
    assert "2 of" not in led("report", "--no-telemetry")[1]
    assert "1 of 4 verified wins" in led("report", "--no-telemetry")[1]
    # leader relabels after the confirm: the confirm judged "changed", so it is stale
    led("label", "a", "avoided-repeat", "re-thought")
    out = led("report", "--no-telemetry")[1]
    assert "0 of 4 verified wins" in out and "unaudited wins (changed/avoided-repeat, no audit yet): 0" in out


def test_time_saved_is_counted_apart_and_keeps_the_label(led):
    _lab(led, "t", "unchanged", "--time-saved", "found the handler without grepping")
    rc, _, err = led("label", "t", "unchanged", "--time-saved", " ", "again")
    assert rc == 2
    out = led("report", "--no-telemetry")[1]
    assert "time saved (label carries --time-saved): 1" in out
    assert "t: found the handler without grepping" in out
    assert "  unchanged: 1" in out and "0 of 4 verified wins" in out
