"""Shadow-local mode, piece (b): the nightly check and the health block
(docs/local-primary-implementation.md §2.9). Offline, on the same fake D1 as
tests/test_l9a_shadow.py."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memora import admin, replicator, shadow_night
from tests.test_l9a_shadow import REPO, _drain, _state, world  # noqa: F401  (fixture)


def _export(path: Path, out: Path) -> Path:
    db = sqlite3.connect(path)
    try:
        out.write_text("\n".join(db.iterdump()))
    finally:
        db.close()
    return out


@pytest.fixture
def night(world, monkeypatch):
    """world + the seed export taken BEFORE any write, and a log-mode
    replicator that has logged everything."""
    seed = _export(world.replica.path, world.tmp / "seed.sql")
    monkeypatch.setenv("MEMORA_REPLICATION", "log")

    def log_all():
        _drain(world.app)
        out = replicator.start_replicators(start=False)
        rep = replicator._REPLICATORS["s1"]
        try:
            rep._open()
            while rep.run_once() == "logged":
                pass
        finally:
            replicator.stop_replicators()
        return out

    def run(day="2026-09-24", sleep=lambda s: None):
        log_all()
        return shadow_night.run_night("s1", world.shadow_path, world.replica.reader(), seed,
                                      today=day, sleep=sleep)
    world.run = run
    return world


def _app_writes(w, n=3):
    conn = w.backend.connect()
    ids = [conn.execute("INSERT INTO memories (content) VALUES (?)", (f"m{i}",)).lastrowid for i in range(n)]
    conn.execute("UPDATE memories SET tags = ? WHERE id = ?", ('["t"]', ids[0]))
    conn.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (?, ?)", (ids[1], "[]"))
    # No DELETE here: any delete from a table under 100 rows trips L3's
    # delete guard (P3), which log mode reports as a would-halt event; the
    # tests below that want one make it themselves.
    conn.execute("UPDATE memories SET content = ? WHERE id = ?", ("m2!", ids[2]))
    conn.close()
    return ids


def test_a_clean_night_counts_once_per_day(night):
    _app_writes(night)
    out = night.run("2026-09-24")
    assert out["clean"], out
    assert out["compare"] == {} and out["builder"]["diffs"] == {}
    assert out["builder"]["key_set"] == {"missing_in_log": [], "extra_in_log": []}
    assert out["clean_nights"] == 1 and out["ready_for_cutover"] is False
    assert night.run("2026-09-24")["clean_nights"] == 1, "the same day counts once"
    assert night.run("2026-09-25")["clean_nights"] == 2


def test_seven_clean_nights_make_the_store_ready(night):
    for d in range(1, 8):
        out = night.run(f"2026-10-0{d}")
        if d == 6:
            assert out["clean_nights"] == 6 and out["ready_for_cutover"] is False, "six nights are not enough"
    assert out["clean_nights"] == 7 and out["ready_for_cutover"] is True


def test_a_foreign_d1_write_is_found_and_the_shadow_goes_dirty(night):
    """Row 10: a write that did not go through the wrapper. (Drained first:
    a foreign write to a key the app touched just before is simply copied
    back at the next quiet point; any other one is this check's job.)"""
    _app_writes(night)
    _drain(night.app)
    db = night.replica._db()
    db.execute("UPDATE memories SET content = 'foreign' WHERE id = 1")
    db.commit()
    db.close()
    out = night.run()
    assert not out["clean"] and "memories" in out["compare"]
    st = _state(night.shadow_path)
    assert st["dirty"] == 1 and st["clean_nights"] == 0 and "nightly (a)" in st["dirty_reason"]


def test_a_transient_diff_that_settles_is_not_a_diff(night):
    """A write lands on D1 during the full read; the applier catches up in
    the pause; the re-read sees no diff. (Drained first, so the write is
    not simply copied back before the compare.)"""
    _app_writes(night)
    _drain(night.app)
    db = night.replica._db()
    db.execute("UPDATE memories SET content = 'in flight' WHERE id = 1")
    db.commit()
    db.close()

    def catch_up(_s):  # the applier mirrors that write during the pause
        sh = sqlite3.connect(night.shadow_path)
        sh.execute("UPDATE memories SET content = 'in flight' WHERE id = 1")
        sh.commit()
        sh.close()
    paused = []
    out = night.run(sleep=lambda s: (paused.append(s), catch_up(s)))
    assert paused, "the diff was seen in the full read"
    assert out["compare"] == {}


def test_a_tampered_log_fails_the_builder_check(night):
    _app_writes(night)
    night.run()
    path = sorted(replicator.log_dir("s1").glob("*.jsonl"))[0]
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["params"] = [("tampered" if isinstance(v, str) and not v.startswith("[") else v) for v in rec["params"]]
    lines[0] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    out = night.run("2026-09-25")
    assert not out["clean"] and out["builder"]["diffs"]
    assert _state(night.shadow_path)["clean_nights"] == 0


def test_a_missing_log_record_fails_the_key_set_check(night):
    _app_writes(night)
    night.run()
    path = sorted(replicator.log_dir("s1").glob("*.jsonl"))[0]
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[1:]) + "\n")
    out = night.run("2026-09-25")
    assert not out["clean"] and out["builder"]["key_set"]["missing_in_log"]


def test_an_already_dirty_shadow_is_not_a_clean_night(night):
    night.app.mark_dirty("earlier")
    out = night.run()
    assert not out["clean"] and out["was_dirty"] and out["clean_nights"] == 0


def test_health_carries_the_shadow_block(night):
    _app_writes(night)
    _drain(night.app)
    block = admin.gate_health("s1")["shadow"]
    assert {"enabled", "dirty", "dirty_reason", "queue_depth", "applier_alive", "clean_nights",
            "pending_keys"} <= set(block)
    assert block["enabled"] is True and block["applier_alive"] is True and block["dirty"] is False


def test_health_has_no_shadow_block_without_a_shadow(monkeypatch, tmp_path):
    from memora import shadow
    monkeypatch.delenv("MEMORA_SHADOW_LOCAL", raising=False)
    assert shadow.shadow_status("s1") is None


def test_cli_help_lists_the_commands():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "local_primary.py"), "shadow-night", "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--seed-export" in r.stdout


def test_a_delete_guard_trip_in_log_mode_is_reported_for_the_night_and_the_night_stays_clean(night):
    """Leader 7699 (§9 (n)): log mode does not halt on the P3 delete guard;
    the night reports the would-halt events since the last night, and is
    not failed by them."""
    ids = _app_writes(night)
    conn = night.backend.connect()
    conn.execute("DELETE FROM memories WHERE id = ?", (1,))
    conn.close()
    out = night.run("2026-09-24")
    assert out["clean"], out
    assert out["builder"]["halted"] is None
    [ev] = out["would_halt"]
    assert out["builder"]["would_halt"] == out["would_halt"]
    assert (ev["tbl"], ev["deletes"]) == ("memories", 1) and ev["attempt_id"]
    assert out["clean_nights"] == 1
    assert _state(night.shadow_path)["would_halt_reported_id"] == ev["id"]
    out = night.run("2026-09-25")
    assert out["clean"] and out["would_halt"] == [], "an event is reported for one night only"
    assert out["clean_nights"] == 2
    conn = night.backend.connect()
    conn.execute("DELETE FROM memories WHERE id = ?", (ids[2],))  # ids[1] has an embedding row
    conn.close()
    out = night.run("2026-09-26")
    assert out["clean"], out
    assert [e["deletes"] for e in out["would_halt"]] == [1]
    assert out["would_halt"][0]["id"] > ev["id"]


def test_a_halted_replicator_log_fails_check_b(night):
    """A halted log (any L3 halt cause) no longer covers the shadow's
    changes: the night is not clean, and the shadow is not marked dirty --
    it is not a shadow defect."""
    _app_writes(night)
    night.run("2026-09-24")
    from memora.backends import LocalSQLiteBackend, store_write

    shadow = LocalSQLiteBackend(night.shadow_path).connect()  # the shadow file's writer path
    try:
        with store_write(shadow):
            shadow.execute("UPDATE sync_state SET halted_reason = 'statement_rejected: test', "
                           "halted_at = 'now' WHERE id = 1")
    finally:
        shadow.close()
    conn = night.backend.connect()
    conn.execute("UPDATE memories SET content = 'after the halt' WHERE id = 1")
    conn.close()
    out = night.run("2026-09-25")
    assert not out["clean"] and out["builder"]["halted"] == "statement_rejected: test"
    assert out["clean_nights"] == 0 and not _state(night.shadow_path)["dirty"]


def test_a_failed_night_resets_earlier_clean_nights(night):
    _app_writes(night)
    assert night.run("2026-09-24")["clean_nights"] == 1
    assert night.run("2026-09-25")["clean_nights"] == 2
    db = night.replica._db()
    db.execute("UPDATE memories SET content = 'foreign' WHERE id = 2")
    db.commit()
    db.close()
    out = night.run("2026-09-26")
    assert not out["clean"] and out["clean_nights"] == 0 and _state(night.shadow_path)["clean_nights"] == 0


def test_an_extra_log_key_fails_the_key_set_check_alone(night):
    """A log record for a key the outbox never touched, which replays to no
    change: only the key-set check can see it."""
    _app_writes(night)
    night.run()
    path = sorted(replicator.log_dir("s1").glob("*.jsonl"))[0]
    first = json.loads(path.read_text().splitlines()[0])
    extra = {**first, "index": 99, "tbl": "memories", "pk": [99999],
             "sql": "DELETE FROM memories WHERE id = ?", "params": [99999]}
    with open(path, "a") as fh:
        fh.write(json.dumps(extra) + "\n")
    out = night.run("2026-09-25")
    assert out["builder"]["diffs"] == {} and out["builder"]["key_set"]["extra_in_log"]
    assert not out["clean"]
