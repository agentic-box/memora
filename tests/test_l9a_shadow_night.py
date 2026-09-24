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

    def health_probe():  # what ShadowHealthProbe reads from /health/db/s1
        return (admin.gate_health("s1") or {}).get("shadow")

    def run(day="2026-09-24", sleep=lambda s: None, probe=health_probe, **kw):
        log_all()
        _drain(world.app)
        kw.setdefault("stable_wait_s", 2.0)  # drained above: an unstable block here is a failure, not a wait
        return shadow_night.run_night("s1", world.shadow_path, world.replica.reader(), world.seed,
                                      today=day, sleep=sleep, probe=probe, **kw)
    world.seed = seed
    world.run = run
    world.health_probe = health_probe
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


# ------------------------------------------------------------ review 7701 P1-1

def _seeded_parent_child_update(night):
    """A memory in the seed (no outbox row ever) whose embedding alone is
    updated: the replicator sends the parent's current row with the child
    (replicator._add_parents)."""
    db = night.replica._db()
    db.execute("INSERT INTO memories (id, content) VALUES (500, 'seeded')")
    db.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (500, '[]')")
    db.commit()
    db.close()
    sh = sqlite3.connect(night.shadow_path)  # as `seed` would have copied them: no outbox rows
    sh.execute("INSERT INTO memories (id, content) VALUES (500, 'seeded')")
    sh.execute("INSERT INTO memories_embeddings (memory_id, embedding) VALUES (500, '[]')")
    sh.execute("DELETE FROM sync_outbox")
    sh.commit()
    sh.close()
    _export(night.replica.path, night.seed)
    conn = night.backend.connect()
    conn.execute("UPDATE memories_embeddings SET embedding = ? WHERE memory_id = ?", ('[[\"0\", 1.0]]', 500))
    conn.close()


def test_a_parent_the_replicator_adds_to_a_child_upsert_is_not_an_extra_key(night):
    """That parent log key is allowed; the night is clean."""
    _seeded_parent_child_update(night)
    out = night.run()
    logged = {(r["tbl"], tuple(r["pk"])) for r in replicator.iter_log("s1")}
    assert ("memories", (500,)) in logged, "the replicator added the parent"
    assert out["builder"]["key_set"] == {"missing_in_log": [], "extra_in_log": []}
    assert out["clean"], out


@pytest.mark.parametrize("variant", ["no-op update on the parent", "delete of an absent id",
                                     "parent upsert at another seq", "parent upsert in another attempt",
                                     "upsert of another id at the child's seq", "parent upsert of another shape",
                                     "parent without its child", "parent upsert missing a column",
                                     "parent with a no-op child", "labelled as the parent, writes another id"])
def test_an_extra_memories_key_that_is_not_an_added_parent_is_a_defect(night, variant):
    """Review 7701 P1-1 / 7721 P1: only the replicator's own memories UPSERT
    for that id, in the same attempt and seq as a real child upsert, passes."""
    _seeded_parent_child_update(night)
    night.run("2026-09-24")
    path = sorted(replicator.log_dir("s1").glob("*.jsonl"))[0]
    lines = path.read_text().splitlines()
    recs = [json.loads(ln) for ln in lines]
    parent = next(r for r in recs if r["tbl"] == "memories" and r["pk"] == [500])
    other_seq = parent["seq"] - 1  # within the cursor, and no child there
    bad_key = 500
    if variant == "no-op update on the parent":  # the reviewer's case
        add = [{**parent, "index": 5, "sql": "UPDATE memories SET content = content WHERE id = ?", "params": [500]}]
    elif variant == "delete of an absent id":
        bad_key = 999
        add = [{**parent, "index": 0, "pk": [999], "sql": "DELETE FROM memories WHERE id = ?", "params": [999]}]
    elif variant == "parent upsert at another seq":
        add = [{**parent, "seq": other_seq}]
    elif variant == "parent upsert in another attempt":
        # a distinct record (index 3): the same (seq, table, key, index) is a crash re-append
        add = [{**parent, "attempt_id": "0" * 16, "index": 3}]
    elif variant == "upsert of another id at the child's seq":
        bad_key = 501
        add = [{**parent, "pk": [501], "params": [501 if v == 500 else v for v in parent["params"]]}]
    elif variant == "parent upsert of another shape":
        add = [{**parent, "index": 3, "sql": parent["sql"].replace(" DO UPDATE SET ", " DO UPDATE SET content = content, ")}]
    elif variant == "parent upsert missing a column":  # the regex shape, not the builder's statement
        add = [{**parent, "index": 4, "sql": parent["sql"].replace("SET content = excluded.content, ", "SET ")}]
        assert add[0]["sql"] != parent["sql"]
    elif variant == "labelled as the parent, writes another id":  # the log key says 500, the row is 501
        add = [{**parent, "index": 6, "params": [501 if v == 500 else v for v in parent["params"]]}]
    elif variant == "parent with a no-op child":
        path.write_text("".join(ln + "\n" for ln, r in zip(lines, recs) if r["tbl"] != "memories_embeddings"
                                or r["pk"] != [500]))
        child = next(r for r in recs if r["tbl"] == "memories_embeddings" and r["pk"] == [500])
        add = [{**child, "index": 0, "sql": "UPDATE memories_embeddings SET embedding = embedding WHERE memory_id = ?",
                "params": [500]}]
    else:  # the child's records removed: a parent with no child
        path.write_text("".join(ln + "\n" for ln, r in zip(lines, recs) if r["tbl"] != "memories_embeddings"
                                or r["pk"] != [500]))
        add = []
    with open(path, "a") as fh:
        for r in add:
            fh.write(json.dumps(r) + "\n")
    out = night.run("2026-09-25")
    assert f"('memories', ({bad_key},))" in out["builder"]["key_set"]["extra_in_log"], (variant, out["builder"])
    assert not out["clean"]


# ------------------------------------------------------------ review 7701 P1-2

def _block(**kw):
    b = {"enabled": True, "applier_alive": True, "queue_depth": 0, "unfinished": 0, "pending_keys": 0,
         "inflight": 0, "generation": 7, "instance": "i1", "dirty": False}
    b.update(kw)
    return b


def _foreign_write(night):
    db = night.replica._db()
    db.execute("UPDATE memories SET content = 'foreign' WHERE id = 1")
    db.commit()
    db.close()


@pytest.mark.parametrize("busy", [{"queue_depth": 3}, {"unfinished": 1}, {"pending_keys": 2}, {"inflight": 1},
                                  {"applier_alive": False}, {"refused": "no D1 reader"}, None,
                                  {"generation": None, "_drop": "generation"}])
def test_an_unstable_applier_defers_the_night_and_touches_nothing(night, busy):
    """A backlog (or any not-drained state) never marks the shadow dirty and
    never counts a night: exit 6, shadow_state as it was -- even when D1
    differs, as it does while the shadow is behind."""
    _app_writes(night)
    assert night.run("2026-09-24")["clean_nights"] == 1
    before = _state(night.shadow_path)
    _foreign_write(night)
    if busy is None:
        block = None
    else:
        block = _block(**{k: v for k, v in busy.items() if k != "_drop"})
        if "_drop" in busy:
            del block[busy["_drop"]]
    slept = []

    def sleep(s):
        assert len(slept) < 50, "the wait did not stop at its bound"
        slept.append(s)
    out = night.run("2026-09-25", probe=lambda: block, stable_wait_s=0, sleep=sleep)
    assert out["deferred"] is True and out["clean"] is False and out["deferred_reason"]
    assert "compare" not in out and "builder" not in out
    assert _state(night.shadow_path) == before


def test_the_generation_moving_during_the_compare_defers(night):
    _app_writes(night)
    blocks = iter([_block(generation=7), _block(generation=8)])
    out = night.run(probe=lambda: next(blocks), stable_wait_s=0)
    assert out["deferred"] and "started during the compare" in out["deferred_reason"]
    assert _state(night.shadow_path)["clean_nights"] == 0


def test_a_restarted_applier_during_the_compare_defers(night):
    """A new server process starts its generation at 0 again: the instance
    tells them apart."""
    _app_writes(night)
    blocks = iter([_block(instance="i1"), _block(instance="i2")])
    out = night.run(probe=lambda: next(blocks), stable_wait_s=0)
    assert out["deferred"]


def test_a_backlog_after_the_compare_defers(night):
    _app_writes(night)
    blocks = iter([_block(), _block(queue_depth=1)])
    out = night.run(probe=lambda: next(blocks), stable_wait_s=0)
    assert out["deferred"] and out["deferred_reason"].startswith("after the compare")


def test_the_check_waits_for_the_applier_then_counts(night):
    _app_writes(night)
    blocks = iter([_block(queue_depth=4), _block(pending_keys=1), _block(), _block()])
    now = [0.0]
    polls = []

    def sleep(s):
        polls.append(s)
        now[0] += s
    out = night.run(probe=lambda: next(blocks), stable_wait_s=10, sleep=sleep, poll_s=1.0,
                    clock=lambda: now[0])
    assert out["clean"] and out["clean_nights"] == 1 and polls == [1.0, 1.0]


def test_the_wait_is_bounded(night):
    _app_writes(night)
    now = [0.0]
    polls = []

    def sleep(s):
        polls.append(s)
        now[0] += s
    def bounded_sleep(s):
        assert len(polls) < 50, "the wait did not stop at its bound"
        sleep(s)
    out = night.run(probe=lambda: _block(queue_depth=1), stable_wait_s=3, sleep=bounded_sleep, poll_s=1.0,
                    clock=lambda: now[0])
    assert out["deferred"] and len(polls) == 3


def test_the_real_health_block_carries_the_stability_fields(night):
    _app_writes(night)
    _drain(night.app)
    block = night.health_probe()
    assert shadow_night.unstable_reason(block) is None
    assert block["instance"] == night.app.instance and block["generation"] == night.app._generation
    night.app.begin_mutation()
    try:
        assert "inflight" in shadow_night.unstable_reason(night.health_probe())
    finally:
        night.app.end_mutation()


def test_the_cli_exits_6_when_deferred(night, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("lp_cli", REPO / "scripts" / "local_primary.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    tok = night.tmp / "health"
    tok.write_text("h" * 32)
    tok.chmod(0o600)
    seen = {}

    def fake_run_night(*a, **kw):
        seen.update(kw)
        return {"store": "s1", "clean": False, "deferred": True, "deferred_reason": "busy"}
    monkeypatch.setattr(shadow_night, "run_night", fake_run_night)
    code = cli.main(["shadow-night", "s1", "--shadow", str(night.shadow_path), "--seed-export", str(night.seed),
                     "--account", "a", "--database-id", "d", "--health-token-file", str(tok),
                     "--stable-wait-s", "12"])
    assert code == 6 and seen["stable_wait_s"] == 12.0
    assert isinstance(seen["probe"], shadow_night.ShadowHealthProbe)
    assert json.loads(capsys.readouterr().out)["deferred"] is True
