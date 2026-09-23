"""L4: absorb (and the other multi-statement writes) on a transactional
backend. docs/local-primary-implementation.md §3, §9 (b).

Offline, local SQLite. Every LLM, embedding and R2 client used here is
guarded: it fails the test if it is called while store_write holds the
write lock (the no-network rule). The d1:// absorb suites are untouched and
still run the D1 machinery.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time

import pytest

import memora
import memora.storage as storage
from memora import backends, image_storage, schema
from memora.backends import LocalSQLiteBackend, StoreWriteAborted, in_store_write, store_write

from tests.test_absorb_supersede_gate import FakeLLM, _old_block, _update, _verdict, _verify_by_old_text


# ------------------------------------------------------------------ guards

class _Guard:
    def __init__(self):
        self.calls = []

    def check(self, what):
        self.calls.append((what, in_store_write()))
        assert not in_store_write(), f"{what} called under the store_write lock"


@pytest.fixture()
def guard(monkeypatch, local_db):
    g = _Guard()
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    vecs = {}
    g.vecs = vecs

    def embed(c, m, t):
        g.check("embedding")
        return vecs.get(c, {"x": 1.0})

    real_many = storage._compute_embeddings_many

    def embed_many(items):
        g.check("embedding batch")
        return [vecs.get(c, {"x": 1.0}) for c, _m, _t in items]

    monkeypatch.setattr(storage, "_compute_embedding", embed)
    monkeypatch.setattr(storage, "_compute_embeddings_many", embed_many)
    return g


class GuardedLLM(FakeLLM):
    def __init__(self, guard, **kw):
        super().__init__(**kw)
        self._guard = guard

    def _create(self, **kwargs):
        self._guard.check("llm")
        return super()._create(**kwargs)


def _sim(value):
    return {"x": value, "y": math.sqrt(max(0.0, 1.0 - value * value))}


def _mem(conn, text, tags=("clmux/ideas",)):
    return storage.add_memory(conn, content=text, tags=list(tags))


def _absorb(monkeypatch, guard, llm, candidate, fact, *, score=0.72, **kw):
    guard.vecs[fact] = _sim(score)
    monkeypatch.setattr(storage, "_get_llm_client", lambda: llm)
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [{"score": score, "memory": candidate}])
    with storage.connect() as conn:
        result = storage.absorb_memory(conn, [fact], **kw)
        active = {m["id"] for m in storage.list_memories(conn, follow="active")}
    return result, active


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("memories", "memories_crossrefs", "memories_embeddings", "tombstones", "absorb_inflight")}


# ------------------------------------------------------------------ absorb in one transaction

def test_supersede_runs_every_llm_call_outside_the_transaction(guard, monkeypatch):
    with storage.connect() as conn:
        old = _mem(conn, "PASS retention window is 7 days")
    llm = GuardedLLM(guard, classify=_update(old["id"]), verify=_verdict(True, True, True))
    result, active = _absorb(monkeypatch, guard, llm, old, "retention window is 14 days")
    (decision,) = result["decisions"]
    assert decision["action"] == "superseded" and old["id"] not in active
    assert guard.calls and not any(under for _w, under in guard.calls)
    assert "tx_regates" not in result["profile"]["counters"]  # the pre-gate covered every leaf


def test_fork_on_local_pregates_every_live_leaf(guard, monkeypatch):
    with storage.connect() as conn:
        orig = _mem(conn, "ORIG deploy target is host one")
        good = _mem(conn, "PASS deploy target is host two")
        other = _mem(conn, "FAIL canary deploy target is host three")
        storage.add_link(conn, good["id"], orig["id"], edge_type="supersedes")
        storage.add_link(conn, other["id"], orig["id"], edge_type="supersedes")
    llm = GuardedLLM(guard, classify=_update(orig["id"]), verify=_verify_by_old_text("PASS"))
    result, active = _absorb(monkeypatch, guard, llm, orig, "deploy target is host four")
    (decision,) = result["decisions"]
    assert decision["action"] == "superseded"
    assert decision["target_ids"] == [good["id"]] and decision["not_superseded"] == [other["id"]]
    assert {decision["memory_id"], other["id"]} <= active and good["id"] not in active
    assert not any(under for _w, under in guard.calls)
    assert "tx_regates" not in result["profile"]["counters"]  # pre-existing fork leaves were pre-gated


def test_leaf_appearing_after_the_pregate_rolls_back_and_is_regated_outside(guard, monkeypatch):
    with storage.connect() as conn:
        old = _mem(conn, "PASS retention window is 7 days")
    real_pregate = storage._absorb_pregate_job
    newcomer = {}

    def pregate_then_race(conn, corpus, job, *, context):
        real_pregate(conn, corpus, job, context=context)
        if not newcomer:
            newcomer.update(_mem(conn, "LATE retention for the audit log only is 90 days"))
            storage.add_link(conn, newcomer["id"], old["id"], edge_type="supersedes")

    monkeypatch.setattr(storage, "_absorb_pregate_job", pregate_then_race)
    llm = GuardedLLM(guard, classify=_update(old["id"]), verify=_verify_by_old_text("PASS"))
    result, active = _absorb(monkeypatch, guard, llm, old, "retention window is 14 days")
    (decision,) = result["decisions"]
    assert decision["action"] == "linked" and decision["target_id"] == newcomer["id"]
    assert result["profile"]["counters"]["tx_regates"] == 1
    prompts = llm.verify_prompts()
    assert len(prompts) == 2 and "LATE" in _old_block(prompts[1])
    assert not any(under for _w, under in guard.calls)
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3  # old, newcomer, new: once


def test_regates_are_bounded_then_the_leaf_is_kept(guard, monkeypatch):
    with storage.connect() as conn:
        old = _mem(conn, "PASS retention window is 7 days")
    real_infos = storage._absorb_leaf_infos
    n = {"i": 0}

    def drifting(conn, corpus, vector, ids, **kw):
        infos = real_infos(conn, corpus, vector, ids, **kw)
        if in_store_write():  # the leaf looks different every time inside the transaction
            n["i"] += 1
            for info in infos.values():
                info["fingerprint"] = f"drift-{n['i']}"
        return infos

    monkeypatch.setattr(storage, "_absorb_leaf_infos", drifting)
    llm = GuardedLLM(guard, classify=_update(old["id"]), verify=_verdict(True, True, True))
    result, active = _absorb(monkeypatch, guard, llm, old, "retention window is 14 days")
    counters = result["profile"]["counters"]
    assert counters["tx_regates"] == storage._ABSORB_TX_REGATES
    assert counters["tx_ungated_leaves"] >= 1
    (decision,) = result["decisions"]
    assert decision["action"] in ("linked", "created") and old["id"] in active  # kept, not superseded
    assert not any(under for _w, under in guard.calls)


@pytest.mark.parametrize("hook", ["_after_absorb_owned_insert", "_after_absorb_resolve",
                                  "_before_absorb_supersede_links"])
def test_a_failure_at_any_hook_rolls_back_everything(guard, monkeypatch, hook):
    with storage.connect() as conn:
        old = _mem(conn, "PASS retention window is 7 days")
        schema.install_sync(conn, "d1://a/b", 0)  # the outbox must stay empty too
        before = _counts(conn)
        outbox_before = conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]

    def boom(*a, **k):
        raise RuntimeError(f"injected at {hook}")

    monkeypatch.setattr(storage, hook, boom)
    llm = GuardedLLM(guard, classify=_update(old["id"]), verify=_verdict(True, True, True))
    guard.vecs["retention window is 14 days"] = _sim(0.72)
    monkeypatch.setattr(storage, "_get_llm_client", lambda: llm)
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [{"score": 0.72, "memory": old}])
    with storage.connect() as conn:
        with pytest.raises(RuntimeError, match="injected"):
            storage.absorb_memory(conn, ["retention window is 14 days"])
        assert _counts(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == outbox_before
        assert storage.list_absorb_inflight(conn) == {"live": [], "orphaned": []}


def test_the_sibling_branch_is_unreachable_in_a_transaction(guard, monkeypatch):
    with storage.connect() as conn:
        old = _mem(conn, "PASS retention window is 7 days")
        before = _counts(conn)

    def heal(conn, new_id, *, keep=None, may_collapse=None):
        may_collapse(new_id + 1000, new_id)  # a "sibling" superseding the new row
        return set(keep or ())

    monkeypatch.setattr(storage, "_heal_supersession_fork", heal)
    llm = GuardedLLM(guard, classify=_update(old["id"]), verify=_verdict(True, True, True))
    guard.vecs["retention window is 14 days"] = _sim(0.72)
    monkeypatch.setattr(storage, "_get_llm_client", lambda: llm)
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [{"score": 0.72, "memory": old}])
    with storage.connect() as conn:
        with pytest.raises(RuntimeError, match="unreachable"):
            storage.absorb_memory(conn, ["retention window is 14 days"])
        assert _counts(conn) == before


def test_a_plain_create_is_one_transaction_with_no_inflight_row(guard, monkeypatch):
    monkeypatch.setattr(storage, "_search_snapshot_full", lambda *a, **k: [])
    with storage.connect() as conn:
        result = storage.absorb_memory(conn, ["a brand new transactional fact"])
        assert result["created"] == 1
        assert conn.execute("SELECT COUNT(*) FROM absorb_inflight").fetchone()[0] == 0


# ------------------------------------------------------------------ images and R2

class _R2:
    def __init__(self, guard, fail=False):
        self.guard, self.fail = guard, fail
        self.uploads, self.deletes = [], []

    def upload_image(self, **kw):
        self.guard.check("r2 upload")
        if self.fail:
            raise OSError("r2 down")
        self.uploads.append(kw["memory_id"])
        return f"r2://images/{kw['memory_id']}/x.png"

    def delete_memory_images(self, memory_id):
        self.guard.check("r2 delete")
        self.deletes.append(memory_id)
        return 1


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_images_are_deferred_outside_the_transaction(guard, monkeypatch):
    r2 = _R2(guard)
    monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: r2)
    seen_inside = {}
    with storage.connect() as conn:
        with store_write(conn):
            rec = storage.add_memory(conn, content="a memory with an image",
                                     metadata={"images": [{"src": PNG}]}, embedding={"x": 1.0}, commit=False)
            meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (rec["id"],)).fetchone()[0])
            seen_inside.update(meta)
        assert seen_inside.get("images_pending") is True and seen_inside["images"][0]["src"] == PNG
        assert r2.uploads == [rec["id"]]  # after the commit
        meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (rec["id"],)).fetchone()[0])
        assert "images_pending" not in meta and meta["images"][0]["src"].startswith("r2://")


def test_a_failed_deferred_apply_keeps_the_flag_and_the_sweep_retries(guard, monkeypatch):
    """(An R2 upload error itself is absorbed by _process_image_for_storage,
    which keeps the data URI -- existing behaviour. What leaves the flag is
    a failure of the follow-up as a whole.)"""
    r2 = _R2(guard)
    monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: r2)
    real = storage._process_metadata_images
    state = {"fail": True}

    def flaky(meta, memory_id=None):
        if state["fail"] and memory_id is not None:
            raise OSError("image processing failed")
        return real(meta, memory_id=memory_id)

    monkeypatch.setattr(storage, "_process_metadata_images", flaky)
    with storage.connect() as conn:
        with store_write(conn):
            rec = storage.add_memory(conn, content="an image that cannot be processed yet",
                                     metadata={"images": [{"src": PNG}]}, embedding={"x": 1.0}, commit=False)
        meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (rec["id"],)).fetchone()[0])
        assert meta.get("images_pending") is True and r2.uploads == []
        state["fail"] = False
        assert storage.sweep_pending_images(conn) == 1
        meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (rec["id"],)).fetchone()[0])
        assert "images_pending" not in meta and r2.uploads == [rec["id"]]
        assert storage.sweep_pending_images(conn) == 0


def test_r2_deletion_waits_for_the_commit(guard, monkeypatch):
    r2 = _R2(guard)
    monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: r2)
    with storage.connect() as conn:
        a = _mem(conn, "delete me after commit")
        b = _mem(conn, "delete me then roll back")
        with store_write(conn):
            assert storage.delete_memory(conn, a["id"])
            assert r2.deletes == []
        assert r2.deletes == [a["id"]]
        with pytest.raises(RuntimeError):
            with store_write(conn):
                storage.delete_memory(conn, b["id"])
                raise RuntimeError("roll back")
        assert r2.deletes == [a["id"]]
        assert storage.get_memory(conn, b["id"]) is not None


def test_import_on_a_local_store_is_one_store_write(local_db, monkeypatch):
    entered = []
    real = backends.store_write

    def spy(conn):
        entered.append(1)
        return real(conn)

    monkeypatch.setattr(backends, "store_write", spy)
    monkeypatch.setattr(storage, "_compute_embeddings_batch", lambda rows, model: [{"x": 1.0} for _ in rows],
                        raising=False)
    with storage.connect() as conn:
        out = storage._import_write_transactional(conn, [], "append")
    assert out["replaced"] is True and entered == [1]


def test_add_memory_refuses_to_embed_under_the_lock(guard):
    with storage.connect() as conn:
        with pytest.raises(RuntimeError, match="precomputed embedding"):
            with store_write(conn):
                storage.add_memory(conn, content="needs an embedding", commit=False)
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert not any(under for _w, under in guard.calls)



def test_deferred_apply_never_overwrites_a_row_changed_after_the_commit(guard, monkeypatch):
    r2 = _R2(guard)
    monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: r2)
    monkeypatch.setattr(storage, "_apply_deferred_images", lambda *a, **k: False)  # hold the follow-up
    with storage.connect() as conn:
        with store_write(conn):
            rec = storage.add_memory(conn, content="image row edited before its follow-up",
                                     metadata={"images": [{"src": PNG}]}, embedding={"x": 1.0}, commit=False)
        raw = conn.execute("SELECT metadata FROM memories WHERE id = ?", (rec["id"],)).fetchone()[0]
        edited = json.loads(raw)
        edited["note"] = "edited meanwhile"
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(edited), rec["id"]))
        conn.commit()
        monkeypatch.undo()
        real_apply = storage._apply_deferred_images
        real_process = storage._process_metadata_images

        def process_then_race(meta, memory_id=None):  # someone edits while the upload runs
            out = real_process(meta, memory_id=memory_id)
            again = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()[0])
            again["note"] = "edited during the upload"
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(again), memory_id))
            conn.commit()
            return out

        monkeypatch.setattr(storage, "_process_metadata_images", process_then_race)
        monkeypatch.setattr(image_storage, "get_image_storage_instance", lambda: r2)
        assert real_apply(conn, rec["id"]) is False
        meta = json.loads(conn.execute("SELECT metadata FROM memories WHERE id = ?", (rec["id"],)).fetchone()[0])
        assert meta["note"] == "edited during the upload" and meta.get("images_pending") is True


def test_a_leaf_appearing_between_classification_and_phase_3_is_pregated(guard, monkeypatch):
    """The realistic race (classification can take minutes): the pre-gate,
    not a transaction retry, checks the leaf that appeared meanwhile."""
    with storage.connect() as conn:
        old = _mem(conn, "PASS retention window is 7 days")
    real_gate = storage._absorb_gate_updates
    newcomer = {}

    def gate_then_race(conn, *a, **k):
        gates = real_gate(conn, *a, **k)
        newcomer.update(_mem(conn, "LATE retention for the audit log only is 90 days"))
        storage.add_link(conn, newcomer["id"], old["id"], edge_type="supersedes")
        return gates

    monkeypatch.setattr(storage, "_absorb_gate_updates", gate_then_race)
    llm = GuardedLLM(guard, classify=_update(old["id"]), verify=_verify_by_old_text("PASS"))
    result, active = _absorb(monkeypatch, guard, llm, old, "retention window is 14 days")
    (decision,) = result["decisions"]
    assert decision["action"] == "linked" and decision["target_id"] == newcomer["id"]
    assert "tx_regates" not in result["profile"]["counters"]
    assert not any(under for _w, under in guard.calls)
