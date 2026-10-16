"""Item 95: the copy tool (memora/copy_memories.py) against two stores served by
the real MCP tool functions (tests/copy_fakes.py)."""

import fcntl
import json
import os

import pytest

import memora
from memora import copy_memories as cm
from memora import server, storage
from memora.backends import LocalSQLiteBackend

from .copy_fakes import InProcessEndpoint, in_store

TAG = "acebar"


@pytest.fixture
def stores(monkeypatch, tmp_path):
    backends = {"memora": LocalSQLiteBackend(tmp_path / "memora.db"),
                "acebar": LocalSQLiteBackend(tmp_path / "acebar.db")}
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(storage, "backend_for", lambda name: backends[name])
    monkeypatch.setenv("MEMORA_DATABASES", json.dumps({n: str(tmp_path / f"{n}.db") for n in backends}))
    monkeypatch.setenv("MEMORA_DEFAULT_DB", "memora")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    server._tool_last_call.clear()
    server._tool_running.clear()
    for name in backends:
        with in_store(name) as c:
            c.commit()
    yield {"src": InProcessEndpoint("memora"), "dest": InProcessEndpoint("acebar")}
    server._tool_last_call.clear()
    server._tool_running.clear()


def create(ep, content, tags=(TAG,), metadata=None, created_at=None):
    rec = ep.call("memory_create", {"content": content, "tags": list(tags), "metadata": metadata,
                                    "suggest_similar": False})["memory"]
    if created_at:
        with in_store(ep.store) as c:
            c.execute("UPDATE memories SET created_at = ? WHERE id = ?", (created_at, rec["id"]))
            c.commit()
    return rec["id"]


def link(ep, a, b, edge_type, bidirectional=True):
    ep.call("memory_link", {"from_id": a, "to_id": b, "edge_type": edge_type, "bidirectional": bidirectional})


def related(ep, mid):
    rec = ep.call("memory_get", {"memory_id": mid, "follow": "all"})["memory"]
    return {(r["id"], r["edge_type"]) for r in rec.get("related") or []}


def opts(tmp_path, **kw):
    slept = []
    o = cm.ApplyOptions(state_dir=tmp_path / "copystate", sleep=lambda s: slept.append(s), unknown_wait_s=60, **kw)
    o.slept = slept
    return o


def plan_for(stores, tmp_path=None, **kw):
    for ep in stores.values():
        ep.calls.clear()                      # setup is over: what follows is the tool's own traffic
    return cm.build_plan(stores["src"], stores["dest"], tag=TAG, follow="active", project="acebar", **kw)


def dest_rows(stores):
    with in_store("acebar") as c:
        return [dict(r) for r in c.execute("SELECT * FROM memories ORDER BY id").fetchall()]


@pytest.fixture
def seeded(stores):
    """Five tagged memories with distinctive timestamps, plus one untagged
    memory the links point at (an external edge)."""
    s = stores["src"]
    ids = {
        "a": create(s, "alpha apples orchard note", metadata={"k": 1, "nested": {"x": [1, 2]}},
                    created_at="2020-01-02 03:04:05"),
        "b": create(s, "bravo bananas plantation", created_at="2021-05-06 07:08:09"),
        "c": create(s, "charlie cherries blossom", tags=(TAG, "extra")),
        "d": create(s, "delta dates oasis", created_at="2019-12-31 23:59:59"),
        "e": create(s, "echo elderberries hedge"),
        "out": create(s, "outside the selection kumquat", tags=("other",)),
    }
    return ids


def source_digest(stores):
    return cm._digest(cm.read_source(stores["src"], TAG, "active")), cm.source_total(stores["src"])


# ------------------------------------------------------------------ dry-run

def test_dry_run_writes_nothing_anywhere(stores, seeded):
    before = source_digest(stores)
    plan = plan_for(stores)
    assert plan["report"]["selected"] == 5
    assert plan["report"]["would_copy"] == sorted(seeded[k] for k in "abcde")
    assert plan["report"]["already_copied"] == []
    assert plan["report"]["created_at_becomes_copy_time"] is True and "original_created_at" in plan["report"]["note"]
    assert stores["src"].writes() == [] and stores["dest"].writes() == []
    assert source_digest(stores) == before
    assert dest_rows(stores) == []


def test_the_plan_file_is_0600_and_tamper_evident(stores, seeded, tmp_path):
    plan = plan_for(stores)
    path = tmp_path / "plan.json"
    cm.write_json(path, plan)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert cm.load_plan(path)["digest"] == plan["digest"]
    tampered = json.loads(path.read_text())
    tampered["rows"][0]["content"] = "something else"
    path.write_text(json.dumps(tampered))
    with pytest.raises(cm.CopyError, match="modified after the dry-run"):
        cm.load_plan(path)


def test_source_equal_to_destination_is_refused(stores, seeded):
    with pytest.raises(cm.CopyError, match="same store"):
        cm.build_plan(stores["src"], stores["src"], tag=TAG, follow="active", project="acebar")


# ------------------------------------------------------------------ apply

def test_apply_copies_content_tags_metadata_original_created_at_and_provenance(stores, seeded, tmp_path):
    before = source_digest(stores)
    plan = plan_for(stores)
    out = cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert out["ok"] is True and out["copied"] == 5
    src_rows = {r["id"]: r for r in plan["rows"]}
    mapping = json.loads((tmp_path / "copystate" / "memora-acebar.manifest.json").read_text())["map"]
    assert sorted(int(k) for k in mapping) == sorted(src_rows)
    for sid, did in mapping.items():
        rec = stores["dest"].call("memory_get", {"memory_id": did, "include_images": True, "follow": "all"})["memory"]
        s = src_rows[int(sid)]
        assert rec["content"] == s["content"]
        assert rec["created_at"] >= s["created_at"]                       # no API sets it: it is the copy time...
        if s["created_at"] < "2022":
            assert rec["created_at"] != s["created_at"]
        assert rec["metadata"]["original_created_at"] == s["created_at"]  # ...and the source's is kept
        assert set(s["tags"]) <= set(rec["tags"])
        for k, v in (s["metadata"] or {}).items():
            assert rec["metadata"][k] == v
        assert rec["metadata"][cm.PROV_KEY] == {"store": "memora", "id": int(sid)}
        assert rec["metadata"]["project"] == "acebar"
    # the originals are untouched, and no write tool ever went to the source
    assert source_digest(stores) == before
    assert stores["src"].writes() == []
    # embeddings were recomputed in the destination
    with in_store("acebar") as c:
        assert c.execute("SELECT COUNT(*) FROM memories_embeddings").fetchone()[0] == 5


def test_a_rerun_copies_nothing_twice(stores, seeded, tmp_path):
    plan = plan_for(stores)
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    n = len(dest_rows(stores))
    made = creates(stores["dest"])
    out = cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == n == 5
    assert creates(stores["dest"]) == made                                # no second create call
    assert out["ok"] is True


def test_a_superseded_copy_is_not_copied_again(stores, seeded, tmp_path):
    plan = plan_for(stores)
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    mapping = {int(k): v for k, v in json.loads((tmp_path / "copystate" /
                                                  "memora-acebar.manifest.json").read_text())["map"].items()}
    newer = create(stores["dest"], "a newer version, written later in the destination")
    link(stores["dest"], newer, mapping[seeded["a"]], "supersedes")           # the copy of 'a' is now superseded
    active = stores["dest"].call("memory_list", {"limit": -1, "follow": "active", "fields": ["id"]})["memories"]
    assert mapping[seeded["a"]] not in {m["id"] for m in active}              # hidden from the default lineage
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    contents = [r["content"] for r in dest_rows(stores)]
    assert contents.count("alpha apples orchard note") == 1                   # not duplicated


def test_a_source_row_that_already_has_the_provenance_key_is_refused(stores, seeded, tmp_path):
    create(stores["src"], "already carries provenance", metadata={cm.PROV_KEY: {"store": "x", "id": 1}})
    plan = plan_for(stores)
    assert plan["report"]["provenance_collisions"]
    with pytest.raises(cm.CopyError, match="no silent overwrite"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert dest_rows(stores) == []


def test_a_source_that_changed_since_the_dry_run_is_refused(stores, seeded, tmp_path):
    plan = plan_for(stores)
    create(stores["src"], "added after the dry-run")
    with pytest.raises(cm.CopyError, match="changed since the dry-run"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert dest_rows(stores) == []


def test_a_concurrent_copy_into_the_same_destination_is_refused(stores, seeded, tmp_path):
    plan = plan_for(stores)
    o = opts(tmp_path)
    o.state_dir.mkdir(parents=True)
    fd = os.open(o.state_dir / "acebar.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(cm.CopyError, match="another copy"):
            cm.apply_plan(plan, stores["src"], stores["dest"], o)
    finally:
        os.close(fd)
    assert dest_rows(stores) == []


def test_a_frozen_or_unhealthy_destination_is_refused(stores, seeded, tmp_path):
    plan = plan_for(stores)
    stores["dest"]._health = {"http": 200, "status": "ok", "freeze": {"state": "frozen"}}
    with pytest.raises(cm.CopyError, match="frozen"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    stores["dest"]._health = {"http": 503, "status": "unavailable"}
    with pytest.raises(cm.CopyError, match="not ok"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))


# ------------------------------------------------------------------ retries and unknown outcomes

def creates(ep):
    return [c for c in ep.calls if c[0] == "memory_create"]


def test_an_unknown_outcome_whose_row_landed_is_not_created_again(stores, seeded, tmp_path):
    plan = plan_for(stores)
    state = {"n": 0}

    def hook(tool, args, phase):
        if tool == "memory_create" and phase == "after":
            state["n"] += 1
            if state["n"] == 2:
                raise cm.EndpointUncertain("reply lost after the row was committed")
    stores["dest"].hook = hook
    out = cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert out["copied"] == 5 and len(dest_rows(stores)) == 5
    assert len(creates(stores["dest"])) == 5                          # exactly one create per row


def test_an_unknown_outcome_whose_row_never_landed_stops_the_run_and_a_rerun_finishes(stores, seeded, tmp_path):
    plan = plan_for(stores)
    state = {"n": 0}

    def hook(tool, args, phase):
        if tool == "memory_create" and phase == "before":
            state["n"] += 1
            if state["n"] == 3:
                raise cm.EndpointUncertain("connection reset before the request left")
    stores["dest"].hook = hook
    with pytest.raises(cm.CopyError, match="outcome of creating source id .* is unknown"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == 2                                # k of n created, nothing guessed
    stores["dest"].hook = None
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == 5                                # resumed, no duplicates
    contents = [r["content"] for r in dest_rows(stores)]
    assert len(contents) == len(set(contents))


def test_a_crash_after_k_creates_resumes_without_duplicates(stores, seeded, tmp_path):
    plan = plan_for(stores)

    class Boom(RuntimeError):
        pass
    state = {"n": 0}

    def hook(tool, args, phase):
        if tool == "memory_create" and phase == "before":
            state["n"] += 1
            if state["n"] == 4:
                raise Boom("process died")
    stores["dest"].hook = hook
    with pytest.raises(Boom):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == 3
    assert json.loads((tmp_path / "copystate" / "memora-acebar.json").read_text())["phase"] == "creating"
    stores["dest"].hook = None
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == 5


def test_a_create_reported_ok_whose_marker_is_not_visible_is_never_repeated(stores, seeded, tmp_path):
    """If the server accepts the create but the marker does not show up (e.g. the metadata was dropped), the tool
    must stop rather than create the same row again."""
    plan = plan_for(stores)
    real_call = stores["dest"].call

    def call(tool, args):
        if tool == "memory_create":
            args = {k: v for k, v in args.items() if k != "metadata"}
        return real_call(tool, args)
    stores["dest"].call = call
    with pytest.raises(cm.CopyError, match="marker is not visible"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == 1                                # one create, then stop


def test_a_create_that_returns_an_error_stops_the_run_and_links_nothing(stores, seeded, tmp_path, monkeypatch):
    plan = plan_for(stores)
    monkeypatch.setattr(memora, "TAG_WHITELIST", {"only-this-tag"})     # every tag is now rejected
    with pytest.raises(cm.CopyError):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert [c for c in stores["dest"].calls if c[0] == "memory_link"] == []


def test_the_copy_uses_only_leader_profile_tools_never_memory_import(stores, seeded, tmp_path):
    from memora.tool_profile import LEADER_TOOLS

    build_graph(stores, seeded)
    plan = plan_for(stores)
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    used = {c[0] for ep in stores.values() for c in ep.calls}
    assert used <= LEADER_TOOLS, sorted(used - LEADER_TOOLS)
    assert "memory_import" not in used


def test_duplicate_markers_stop_the_run(stores, seeded, tmp_path):
    plan = plan_for(stores)
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    row = plan["rows"][0]
    stores["dest"].call("memory_create", {"content": "a duplicate, by hand", "tags": [TAG],
                                          "metadata": cm._entry_metadata(row, "memora"), "suggest_similar": False})
    with pytest.raises(cm.CopyError, match="duplicate provenance markers"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))


# ------------------------------------------------------------------ links

def build_graph(stores, ids):
    s = stores["src"]
    link(s, ids["a"], ids["b"], "references")            # paired references/referenced_by
    link(s, ids["b"], ids["c"], "implements")            # paired implements/implemented_by
    link(s, ids["c"], ids["d"], "extends")               # paired extends/extended_by
    link(s, ids["d"], ids["e"], "related_to")            # a user-created related_to pair
    link(s, ids["e"], ids["a"], "contradicts")           # symmetric pair
    link(s, ids["a"], ids["c"], "supersedes", bidirectional=False)   # forward-only
    link(s, ids["a"], ids["out"], "references")          # external: dropped and counted


def test_every_internal_edge_type_and_direction_is_preserved(stores, seeded, tmp_path):
    build_graph(stores, seeded)
    plan = plan_for(stores)
    lk = plan["report"]["links"]
    assert lk["external_dropped"] == 1        # a->out, read from the selected row's side (the reverse lives on 'out')
    assert lk["unrepresentable"] == []
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    m = {int(k): v for k, v in json.loads((tmp_path / "copystate" /
                                           "memora-acebar.manifest.json").read_text())["map"].items()}
    d = stores["dest"]
    A, B, C, D, E = (m[seeded[k]] for k in "abcde")
    assert (B, "references") in related(d, A) and (A, "referenced_by") in related(d, B)
    assert (C, "implements") in related(d, B) and (B, "implemented_by") in related(d, C)
    assert (D, "extends") in related(d, C) and (C, "extended_by") in related(d, D)
    assert (E, "related_to") in related(d, D) and (D, "related_to") in related(d, E)
    assert (A, "contradicts") in related(d, E) and (E, "contradicts") in related(d, A)
    assert (C, "supersedes") in related(d, A)                              # forward-only kept
    assert (A, "superseded_by") not in related(d, C)                      # ...with no invented reciprocal
    # nothing in the destination points at a source-only id (the untagged memory)
    for did in m.values():
        assert all(peer in set(m.values()) for peer, _ in related(d, did))


def test_a_reciprocal_only_edge_is_refused_unless_allowed(stores, seeded, tmp_path):
    s = stores["src"]
    # a reciprocal-typed edge whose forward edge is not stored: remove the forward half by hand
    link(s, seeded["a"], seeded["b"], "references")
    with in_store("memora") as c:
        c.execute("UPDATE memories_crossrefs SET related = '[]' WHERE memory_id = ?", (seeded["a"],))
        c.commit()
    plan = plan_for(stores)
    assert plan["report"]["links"]["unrepresentable"] == [
        {"from": seeded["b"], "to": seeded["a"], "edge_type": "referenced_by",
         "why": "reciprocal edge without its forward edge"}]
    with pytest.raises(cm.CopyError, match="cannot create"):
        cm.apply_plan(plan, s, stores["dest"], opts(tmp_path))
    assert dest_rows(stores) == []                                        # refused before any write
    out = cm.apply_plan(plan, s, stores["dest"], opts(tmp_path, allow_unrepresentable=True))
    assert out["unrepresentable"] and out["copied"] == 5
    manifest = json.loads((tmp_path / "copystate" / "memora-acebar.manifest.json").read_text())
    assert manifest["links"]["unrepresentable"]                           # conspicuous in the manifest


def test_a_rerun_repairs_missing_links_with_nothing_left_to_import(stores, seeded, tmp_path):
    build_graph(stores, seeded)
    plan = plan_for(stores)
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    m = {int(k): v for k, v in json.loads((tmp_path / "copystate" /
                                           "memora-acebar.manifest.json").read_text())["map"].items()}
    with in_store("acebar") as c:                                         # an interrupted link phase
        c.execute("UPDATE memories_crossrefs SET related = '[]' WHERE memory_id IN (?, ?)", (m[seeded["a"]], m[seeded["b"]]))
        c.commit()
    assert (m[seeded["b"]], "references") not in related(stores["dest"], m[seeded["a"]])
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert (m[seeded["b"]], "references") in related(stores["dest"], m[seeded["a"]])
    assert len(dest_rows(stores)) == 5


def test_a_crash_in_the_link_phase_is_repaired_by_a_rerun(stores, seeded, tmp_path):
    build_graph(stores, seeded)
    plan = plan_for(stores)
    state = {"n": 0}

    class Boom(RuntimeError):
        pass

    def hook(tool, args, phase):
        if tool == "memory_link" and phase == "before":
            state["n"] += 1
            if state["n"] == 3:
                raise Boom("process died")
    stores["dest"].hook = hook
    with pytest.raises(Boom):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    st = json.loads((tmp_path / "copystate" / "memora-acebar.json").read_text())
    assert st["phase"] == "remapped"                                      # mapped and remapped; links unfinished
    stores["dest"].hook = None
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert len(dest_rows(stores)) == 5
    assert json.loads((tmp_path / "copystate" / "memora-acebar.json").read_text())["phase"] == "done"


# ------------------------------------------------------------------ metadata references

def test_merged_from_is_remapped_by_exact_key_after_the_ids_exist(stores, seeded, tmp_path):
    inside = create(stores["src"], "points inside", metadata={"merged_from": seeded["b"]})
    outside = create(stores["src"], "points outside", metadata={"merged_from": seeded["out"]})
    plan = plan_for(stores)
    assert plan["report"]["merged_from"] == {"remap": [inside], "dropped": [outside]}
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    m = {int(k): v for k, v in json.loads((tmp_path / "copystate" /
                                           "memora-acebar.manifest.json").read_text())["map"].items()}
    rec_in = stores["dest"].call("memory_get", {"memory_id": m[inside], "follow": "all"})["memory"]
    rec_out = stores["dest"].call("memory_get", {"memory_id": m[outside], "follow": "all"})["memory"]
    assert rec_in["metadata"]["merged_from"] == m[seeded["b"]]            # the destination id, not the source id
    assert "merged_from" not in rec_out["metadata"]                        # a dangling source id never lands
    manifest = json.loads((tmp_path / "copystate" / "memora-acebar.manifest.json").read_text())
    assert manifest["dropped_metadata_refs"] == [{"id": outside, "key": "merged_from", "value": seeded["out"]}]


def test_unknown_reference_shaped_metadata_is_reported_and_refused(stores, seeded, tmp_path):
    odd = create(stores["src"], "has a parent_id", metadata={"parent_id": 3, "count": 3, "ordinal": 7})
    plan = plan_for(stores)
    assert plan["report"]["unknown_refs"] == [{"id": odd, "key": "parent_id", "value": 3}]   # not 'count', not 'ordinal'
    with pytest.raises(cm.CopyError, match="cannot remap"):
        cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert dest_rows(stores) == []
    cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path, allow_unknown_refs=True))
    assert len(dest_rows(stores)) == 6


# ------------------------------------------------------------------ source changes during the run

def test_a_source_change_during_the_run_is_reported_as_concurrent_not_as_a_tool_write(stores, seeded, tmp_path):
    plan = plan_for(stores)
    state = {"n": 0}

    def hook(tool, args, phase):
        if tool == "memory_create" and phase == "after" and not state["n"]:
            state["n"] = 1
            create(stores["src"], "someone else wrote to the source meanwhile")
    stores["dest"].hook = hook
    out = cm.apply_plan(plan, stores["src"], stores["dest"], opts(tmp_path))
    assert out["ok"] is False and out["source_changed_during_run"] is True
    assert not [c for c in stores["src"].calls if c[0] in ("memory_import", "memory_link", "memory_update")]


# ------------------------------------------------------------------ pure link analysis

def row(i, *edges):
    return {"id": i, "related": [{"id": b, "edge_type": t, "score": s} for b, t, s in edges]}


def test_analyse_links_matrix():
    rows = [
        row(1, (2, "references", 1.0), (3, "related_to", 0.8)),
        row(2, (1, "referenced_by", 1.0)),
        row(3, (1, "related_to", 0.8), (9, "references", 1.0)),
        row(4, (1, "implemented_by", 1.0)),                       # reciprocal-only
        row(5, (1, "weird_type", 1.0)),                           # unknown type
    ]
    a = cm.analyse_links(rows)
    assert a["forward_paired"] == [(1, 2, "references"), (1, 3, "related_to")]
    assert a["forward_only"] == []
    assert {(b["from"], b["to"], b["edge_type"]) for b in a["unrepresentable"]} == {(4, 1, "implemented_by"),
                                                                                    (5, 1, "weird_type")}
    assert a["external_dropped"] == 1
    assert a["score_normalised"] == 2                              # the two 0.8 scores become 1.0


def test_a_symmetric_forward_only_edge_keeps_its_direction():
    a = cm.analyse_links([row(5, (2, "related_to", 1.0)), row(2)])
    assert a["forward_only"] == [(5, 2, "related_to")] and a["forward_paired"] == []


# ------------------------------------------------------------------ MCP endpoint plumbing

def test_mcp_endpoint_maps_transport_failures_to_an_uncertain_outcome(monkeypatch):
    ep = cm.McpEndpoint("http://127.0.0.1:1", "acebar", "tok", timeout=0.2)
    with pytest.raises(cm.EndpointUncertain):
        ep.call("memory_create", {"content": "x"})


def test_the_image_carries_the_copy_tool_next_to_the_operator_tool():
    """The tool runs inside memora-all (`docker exec memora-all python /app/scripts/copy_memories.py`): the
    Dockerfile must copy it, as it copies local_primary.py (the podman rehearsal found it missing)."""
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "COPY scripts/copy_memories.py scripts/copy_memories.py" in dockerfile
    assert "COPY scripts/local_primary.py scripts/local_primary.py" in dockerfile
