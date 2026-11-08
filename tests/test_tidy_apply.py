"""Item 255: the tidy apply step (memora/tidy_apply.py): metadata-only, sealed, approved, guarded."""
import hashlib
import json
import re
from pathlib import Path

import pytest

import memora
import memora.storage as storage
from memora import dream, tidy, tidy_apply as ta
from memora.backends import LocalSQLiteBackend
from memora.tidy import TidyError, TidyLimits
from tests.l3_fakes import URI
from memora import schema

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def store(tmp_path, monkeypatch):
    backend = LocalSQLiteBackend(tmp_path / "memories.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    conn = backend.connect()
    schema.ensure_schema(conn)
    schema.install_sync(conn, URI, 0)
    conn.close()
    return backend


def add(content, tags=None, meta=None):
    with storage.connect() as conn:
        r = storage.add_memory(conn, content="memo: " + content, tags=list(tags or []), metadata=dict(meta or {}))
        conn.commit()
    return r["id"] if "id" in r else r["memory"]["id"]


def mem(mid):
    with storage.connect() as conn:
        return dream._fresh(conn, mid)[0]


def sha(mid):
    return mem(mid).state_sha


def outbox():
    with storage.connect() as conn:
        return [tuple(r) for r in conn.execute("SELECT tbl, op, pk FROM sync_outbox ORDER BY seq")]


def embeddings():
    with storage.connect() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT memory_id, embedding FROM memories_embeddings")}


def seal(steps, **kw):
    plan = {"version": tidy.TIDY_PLAN_VERSION, "mode": "dry-run", "run": "r1", "created_ts": 1.0,
            "candidates": ["clmux", "memora"], "steps": steps, **kw}
    plan["digest"] = tidy.plan_digest(plan)
    return plan


def sp(sid, mid, proj="memora", conf="high", **kw):
    return {"id": sid, "op": "set_project", "target": mid, "project": proj, "evidence": ["x"], "confidence": conf,
            "needs_approval": False, "status": "eligible", "expect": {str(mid): sha(mid)}, "depends_on": [], **kw}


def lk(sid, a, b, et="references", **kw):
    return {"id": sid, "op": "link", "a": a, "b": b, "edge_type": et, "needs_approval": False, "status": "eligible",
            "expect": {str(a): sha(a), str(b): sha(b)}, "depends_on": [], **kw}


def rt(sid, mid, add_, remove=(), **kw):
    return {"id": sid, "op": "retag", "target": mid, "add": list(add_), "remove": list(remove), "needs_approval": False,
            "status": "eligible", "expect": {str(mid): sha(mid)}, "depends_on": [], **kw}


def dec_for(plan, tmp_path, **kw):
    p = tmp_path / "dec.json"
    p.write_text(json.dumps({"report_digest": plan["digest"], **kw}))
    return ta.load_decisions(str(p), plan)


ALL = [{"op": "set_project"}, {"op": "link"}, {"op": "retag"}]
LIM = TidyLimits()


def run(plan, dec, lim=LIM):
    return ta.apply_report(plan, dec, lim)


def reasons(res):
    return {r["id"]: r["reason"] for r in res["skipped"] + res["deferred"]}


# ------------------------------------------------------------------ decisions

def test_decisions_refuse_wrong_digest_unknown_id_and_malformed(tmp_path):
    plan = seal([{"id": "s1", "op": "retag"}])
    for body, msg in (({"report_digest": "zzz"}, "report_digest"),
                      ({"report_digest": plan["digest"], "approve": ["nope"]}, "not in the report"),
                      ({"report_digest": plan["digest"], "reject": ["nope"]}, "not in the report"),
                      ({"report_digest": plan["digest"], "approve": "s1"}, "list of strings"),
                      ({"report_digest": plan["digest"], "bogus": 1}, "unknown key"),
                      ({"report_digest": plan["digest"], "approve_ops": [{"op": "frob"}]}, "approve_ops"),
                      ({"report_digest": plan["digest"], "approve_ops": [{"op": "link", "min_confidence": "x"}]}, "min_confidence"),
                      ({"report_digest": plan["digest"], "approve_ops": [{"op": "link", "min_confidence": []}]}, "min_confidence"),
                      ({"report_digest": plan["digest"], "approve_ops": [{"op": "link", "min_confidence": {}}]}, "min_confidence"),
                      ({"report_digest": plan["digest"], "approve_ops": [{"op": ["link"]}]}, "approve_ops")):
        p = tmp_path / "d.json"
        p.write_text(json.dumps(body))
        with pytest.raises(TidyError, match=msg):
            ta.load_decisions(str(p), plan)
    p.write_text("{not json")
    with pytest.raises(TidyError):
        ta.load_decisions(str(p), plan)
    p.write_text("[]")
    with pytest.raises(TidyError):
        ta.load_decisions(str(p), plan)


def test_approval_rules(tmp_path):
    steps = [{"id": "a", "op": "set_project", "confidence": "high", "needs_approval": False},
             {"id": "b", "op": "set_project", "confidence": "medium", "needs_approval": False},
             {"id": "c", "op": "set_project", "confidence": "low", "needs_approval": False},
             {"id": "d", "op": "link", "needs_approval": False},
             {"id": "e", "op": "link", "needs_approval": True},
             {"id": "f", "op": "link", "needs_approval": False}]
    plan = seal(steps)
    d = dec_for(plan, tmp_path, approve_ops=[{"op": "set_project", "min_confidence": "medium"}, {"op": "link"}],
                approve=["c", "e"], reject=["f", "a"])
    got = {s["id"]: ta.approved_by(s, d) for s in steps}
    assert got["a"] is None                        # reject wins over approve_ops
    assert got["b"] and got["c"] == "approve:id"    # medium by rule, low by id
    assert got["d"] and got["e"] == "approve:id"    # needs_approval only by id ...
    assert got["f"] is None
    d2 = dec_for(plan, tmp_path, approve_ops=[{"op": "link"}])
    assert ta.approved_by(steps[4], d2) is None     # ... never by approve_ops
    d3 = dec_for(plan, tmp_path, approve_ops=[{"op": "link", "min_confidence": "high"}])
    assert ta.approved_by(steps[3], d3) is None     # a step with no confidence never meets a confidence floor


def test_nothing_approved_means_nothing_written(store, tmp_path):
    a, b = add("alpha"), add("beta")
    plan = seal([sp("b1", a), lk("s1", a, b), rt("s2", b, ["t"])])
    before_box, before_emb = outbox(), embeddings()
    res = run(plan, dec_for(plan, tmp_path))
    assert res["counts"]["applied"] == 0 and {s["reason"] for s in res["skipped"]} == {"not approved"}
    assert outbox() == before_box and mem(a).metadata.get("project") is None and embeddings() == before_emb


def test_unsupported_ops_are_never_written_even_when_approved(store, tmp_path):
    a = add("alpha")
    steps = [{"id": f"x{i}", "op": op, "needs_approval": False, "expect": {str(a): sha(a)}, "status": "eligible"}
             for i, op in enumerate(["merge", "retire", "frobnicate"])]
    plan = seal(steps)
    d = dec_for(plan, tmp_path, approve=[s["id"] for s in steps])
    before = outbox()
    res = run(plan, d)
    assert res["counts"]["applied"] == 0 and outbox() == before
    assert all(s["reason"] == f"skipped: {ta.NOT_SUPPORTED}" for s in res["skipped"]) and len(res["skipped"]) == 3


# ------------------------------------------------------------------ writes and guards

def test_rehearsal_outbox_u_only_content_and_embeddings_unchanged_and_rerun_applies_nothing(store, tmp_path):
    a, b, c = add("alpha note about things"), add("beta note about stuff"), add("gamma", meta={"kind": "item-record"})
    plan = seal([sp("b1", a), rt("s2", b, ["newtag", "other"]), lk("s3", a, c, "extends"), rt("s4", c, ["more"])])
    d = dec_for(plan, tmp_path, approve_ops=ALL)
    before_emb, before_content = embeddings(), {i: mem(i).content for i in (a, b, c)}
    n0 = len(outbox())
    res = run(plan, d)
    assert res["counts"]["applied"] == 4 and not res["skipped"], res
    rows = outbox()[n0:]
    assert rows and {op for _t, op, _pk in rows} == {"U"} and all(op != "D" for _t, op, _pk in outbox())
    assert embeddings() == before_emb                                    # no re-embed
    assert {i: mem(i).content for i in (a, b, c)} == before_content      # text untouched
    assert mem(a).metadata["project"] == "memora" and set(mem(b).tags) == {"newtag", "other"}
    assert {(r["id"], r["edge_type"]) for r in mem(a).refs} >= {(c, "extends")} and (a, "extended_by") in {(r["id"], r["edge_type"]) for r in mem(c).refs}
    assert "more" in mem(c).tags                                         # a protected memory still gets an added tag
    n1 = len(outbox())
    again = run(plan, d)                                                 # same plan, same decisions
    assert again["counts"]["applied"] == 0 and again["counts"]["already_done"] == 4 and len(outbox()) == n1
    assert all(s["reason"].startswith("already done") for s in again["skipped"])


def test_metadata_update_does_not_call_the_embedding_provider_or_llm(store, tmp_path, monkeypatch):
    a = add("alpha")
    plan = seal([sp("b1", a), rt("s2", a, ["t"])])
    def boom(*a, **k):
        raise AssertionError("embedding/LLM called")
    monkeypatch.setattr(storage, "_compute_embedding", boom)
    monkeypatch.setattr(storage, "_get_llm_client", boom)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert res["counts"]["applied"] == 2


def test_set_project_never_overwrites_and_skips_protected_and_changed(store, tmp_path):
    a, b, c, d_ = add("a"), add("b"), add("c", tags=["item-record"]), add("d")
    steps = [sp("b1", a), sp("b2", b), sp("b3", c), sp("b4", d_)]
    plan = seal(steps)
    with storage.connect() as conn:                       # b gains a project, d changes, after the plan
        storage.update_memory(conn, b, metadata={"project": "clmux"}, embed=False)
        storage.update_memory(conn, d_, metadata={"note": "x"}, embed=False)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    r = reasons(res)
    assert [x["id"] for x in res["applied"]] == ["b1"]
    assert "never overwritten" in r["b2"] and mem(b).metadata["project"] == "clmux"
    assert r["b3"].startswith("protected") and mem(c).metadata.get("project") is None
    assert "state sha" in r["b4"] and mem(d_).metadata.get("project") is None


def test_set_project_bad_project_name_is_a_skip_not_a_crash(store, tmp_path):
    a, b = add("a"), add("b")
    plan = seal([sp("b1", a, proj="Bad Name!"), sp("b2", b)])
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert [x["id"] for x in res["applied"]] == ["b2"] and "write failed" in reasons(res)["b1"]


def test_retag_adds_only_and_ignores_removals(store, tmp_path):
    a = add("a", tags=["keep", "old"])
    plan = seal([rt("s1", a, ["new"], remove=["old"])])
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert set(mem(a).tags) == {"keep", "old", "new"} and res["applied"][0]["remove_ignored"] == ["old"]


def test_link_is_idempotent_and_never_overwrites_another_edge(store, tmp_path):
    a, b, c, d_ = add("a"), add("b"), add("c"), add("d")
    with storage.connect() as conn:
        storage.add_link(conn, a, b, edge_type="references")
        storage.add_link(conn, a, c, edge_type="supersedes", bidirectional=False)     # a hides... c by a forward edge
    plan = seal([lk("s1", a, b, "references"), lk("s2", a, c, "extends"), lk("s3", b, d_, "contradicts")])
    n0 = len(outbox())
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    r = reasons(res)
    assert r["s1"].startswith("already done") and "different edge" in r["s2"]
    assert [x["id"] for x in res["applied"]] == ["s3"]
    assert {x["edge_type"] for x in mem(a).refs if x["id"] == c} == {"supersedes"}     # the hiding edge is intact
    assert len(outbox()) > n0


def test_link_skips_when_an_endpoint_changed_or_is_retired(store, tmp_path):
    a, b, c = add("a"), add("b"), add("c")
    plan = seal([lk("s1", a, b), lk("s2", a, c)])
    with storage.connect() as conn:
        storage.update_memory(conn, b, tags=["z"], embed=False)
        storage.add_link(conn, add("new"), c, edge_type="supersedes", bidirectional=False)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert not res["applied"] and "state sha" in reasons(res)["s1"] and "retired" in reasons(res)["s2"]


def test_a_later_step_still_matches_after_this_runs_own_earlier_write(store, tmp_path):
    a, b = add("a"), add("b")
    plan = seal([sp("b1", a), rt("s2", a, ["t"]), lk("s3", a, b)])       # all judged against the same original state
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert [x["id"] for x in res["applied"]] == ["b1", "s2", "s3"], res["skipped"]


def test_caps_are_charged_before_dispatch_and_the_rest_deferred(store, tmp_path):
    ids = [add(f"m{i}") for i in range(5)]
    plan = seal([sp(f"b{i}", m) for i, m in enumerate(ids)] + [lk("l1", ids[0], ids[1]), lk("l2", ids[2], ids[3]),
                                                              rt("t1", ids[4], ["x"])])
    lim = TidyLimits(max_backfill=2, max_writes=3)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL), lim)
    assert [x["id"] for x in res["applied"]] == ["b0", "b1", "l1", "t1"]
    assert {d["id"]: d["reason"] for d in res["deferred"]} == {"b2": "cap:max_backfill", "b3": "cap:max_backfill",
                                                                "b4": "cap:max_backfill", "l2": "cap:max_writes"}
    assert res["counts"]["writes_charged"] == 3 and res["counts"]["backfill_charged"] == 2
    assert mem(ids[2]).metadata.get("project") is None


def test_deadline_stops_dispatch_and_defers_the_rest(store, tmp_path):
    ids = [add(f"m{i}") for i in range(3)]
    plan = seal([sp(f"b{i}", m) for i, m in enumerate(ids)])
    t = iter(range(0, 1000))
    clock = lambda: next(t) * 1.0  # noqa: E731 - every reading advances one second
    res = ta.apply_report(plan, dec_for(plan, tmp_path, approve_ops=ALL), TidyLimits(max_seconds=12), clock=clock)
    assert res["aborted"] == "max_seconds"
    assert len(res["applied"]) < 3 and len(res["applied"]) + len(res["deferred"]) + len(res["skipped"]) >= 3


def test_dependency_not_applied_is_skipped(store, tmp_path):
    a, b = add("a"), add("b")
    plan = seal([sp("b1", a, conf="low"), dict(rt("s2", b, ["t"]), depends_on=["b1"])])
    res = run(plan, dec_for(plan, tmp_path, approve_ops=[{"op": "retag"}, {"op": "set_project", "min_confidence": "high"}]))
    assert not res["applied"] and "dependency" in reasons(res)["s2"]


# ------------------------------------------------------------------ CLI

def write_plan(tmp_path, plan):
    p = tmp_path / "report.json"
    p.write_text(json.dumps(plan))
    return p


def test_cli_refuses_edited_report_and_old_report(tmp_path):
    plan = seal([{"id": "s1", "op": "retag"}], created_ts=__import__("time").time())
    p = write_plan(tmp_path, plan)
    d = tmp_path / "d.json"
    d.write_text(json.dumps({"report_digest": plan["digest"]}))
    assert tidy.main(["--apply", "--plan", str(p), "--decisions", str(d), "--check"]) == 0
    plan["steps"][0]["op"] = "merge"                                   # edited after sealing
    p.write_text(json.dumps(plan))
    assert tidy.main(["--apply", "--plan", str(p), "--decisions", str(d), "--check"]) == 2
    old = seal([], created_ts=1.0)
    p.write_text(json.dumps(old))
    d.write_text(json.dumps({"report_digest": old["digest"]}))
    assert tidy.main(["--apply", "--plan", str(p), "--decisions", str(d), "--check"]) == 2
    with pytest.raises(SystemExit):
        tidy.main(["--apply"])                                          # --plan and --decisions are required


def test_cli_apply_writes_a_result_file_next_to_the_plan(store, tmp_path, monkeypatch, capsys):
    a = add("a")
    plan = seal([sp("b1", a)], created_ts=__import__("time").time())
    p = write_plan(tmp_path, plan)
    d = tmp_path / "d.json"
    d.write_text(json.dumps({"report_digest": plan["digest"], "approve_ops": ALL}))
    cfg = tmp_path / "tidy.json"
    cfg.write_text(json.dumps({"state_dir": str(tmp_path / "state")}))
    assert tidy.main(["--apply", "--plan", str(p), "--decisions", str(d), "--config", str(cfg)]) == 0
    out = json.loads(capsys.readouterr().out)
    res = json.loads(Path(out["result"]).read_text())
    assert Path(out["result"]).parent == tmp_path and res["counts"]["applied"] == 1 and mem(a).metadata["project"] == "memora"
    assert not any(k for k in res if k == "content")


def test_apply_holds_the_run_lock(store, tmp_path):
    a = add("a")
    plan = seal([sp("b1", a)], created_ts=__import__("time").time())
    p = write_plan(tmp_path, plan)
    d = tmp_path / "d.json"
    d.write_text(json.dumps({"report_digest": plan["digest"], "approve_ops": ALL}))
    cfg = tmp_path / "tidy.json"
    cfg.write_text(json.dumps({"state_dir": str(tmp_path / "state")}))
    with dream.run_lock(tmp_path / "state"):
        assert tidy.main(["--apply", "--plan", str(p), "--decisions", str(d), "--config", str(cfg)]) == 2
    assert mem(a).metadata.get("project") is None


# ------------------------------------------------------------------ the module has no model, git or network path

def test_apply_module_has_no_model_git_network_delete_or_content_edit():
    code = "\n".join(l for l in (ROOT / "memora" / "tidy_apply.py").read_text().splitlines() if not l.lstrip().startswith("#"))
    for forbidden in (r"subprocess", r"GitSource", r"urllib", r"requests", r"socket", r"\bjudge\b", r"claude", r"openai",
                      r"delete_memory", r"remove_link", r"sqlite3\.connect"):
        assert not re.search(forbidden, code, re.I), forbidden
    assert "DELETE " not in code                                  # the only raw SQL writes are the guarded edge statement
    assert code.count("INSERT INTO memories_crossrefs") == 2 and code.count("UPDATE memories_crossrefs") == 2
    assert len(re.findall(r"\b(INSERT|UPDATE)\b ", code)) == 4          # the link edge and the digest edge, two shapes each
    assert code.count("update_memory(") == 3 and code.count("embed=False") == 2 and "_guarded_edge(" in code
    assert code.count("force_reindex=True") == 1             # the third: completing OUR half-created digest, no text change
    # Item 259: the ONE create is the digest's, through the store's own add_memory; nothing else is added
    assert code.count("add_memory(") == 1 and code.count("content=") == 1 and "skip_crossrefs" in code


def test_nothing_in_the_unit_or_run_scripts_contains_apply():
    files = [p for p in (ROOT / "scripts" / "tidy").rglob("*") if p.is_file()]
    assert any(p.name == "memora-tidy.service" for p in files)
    for p in files:
        assert "--apply" not in p.read_text(), p


# ------------------------------------------------------------------ storage: the embed=False path

def test_update_memory_embed_false_keeps_the_vector_and_refreshes_fts_but_default_still_embeds(store, monkeypatch):
    a = add("alpha note")
    before = embeddings()[a]
    with storage.connect() as conn:
        storage.update_memory(conn, a, tags=["t1"], embed=False)
    assert embeddings()[a] == before and "t1" in mem(a).tags
    with storage.connect() as conn:
        assert conn.execute("SELECT 1 FROM memories_fts WHERE memories_fts MATCH 't1'").fetchone() is not None
        with pytest.raises(ValueError):
            storage.update_memory(conn, a, content="changed text", embed=False)
        storage.update_memory(conn, a, tags=["t1", "t2"])          # default path computes an embedding
    assert embeddings()[a] != before


# ------------------------------------------------------------------ round 1: statement-level link guards, exact aliases,
# add-only tags, durable replay

def _interfere_on_call(monkeypatch, n, action):
    """Run `action()` just before the n-th guarded edge statement (1 = forward, 2 = reverse)."""
    real, calls = ta._guarded_edge, {"n": 0}
    def wrapper(*a, **k):
        calls["n"] += 1
        if calls["n"] == n:
            action()
        return real(*a, **k)
    monkeypatch.setattr(ta, "_guarded_edge", wrapper)
    return calls


def _edge_types(mid, peer):
    return {r["edge_type"] for r in mem(mid).refs if r["id"] == peer}


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("kind", ["endpoint_edit", "retirement", "typed_edge"])
def test_link_interleaving_never_overwrites_or_writes_across_a_concurrent_change(store, tmp_path, monkeypatch, phase, kind):
    a, b, other = add("a note"), add("b note"), add("other note")
    plan = seal([lk("s1", a, b, "references")])
    def action():
        with storage.connect() as conn:
            if kind == "endpoint_edit":
                storage.update_memory(conn, b if phase == 1 else a, tags=["foreign"], embed=False)
            elif kind == "retirement":
                storage.add_link(conn, other, b if phase == 1 else a, edge_type="supersedes", bidirectional=False)
            else:       # a hiding edge between the pair, added by someone else, on the holder of THIS write
                storage.add_link(conn, a if phase == 1 else b, b if phase == 1 else a, edge_type="supersedes",
                                 bidirectional=False)
    _interfere_on_call(monkeypatch, phase, action)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert not res["applied"]
    if kind == "typed_edge":
        holder, peer = (a, b) if phase == 1 else (b, a)
        assert "supersedes" in _edge_types(holder, peer)         # the foreign edge is intact, not replaced
        assert "references" not in _edge_types(holder, peer) and "extends" not in _edge_types(holder, peer)
    if phase == 1:
        assert "references" not in _edge_types(a, b)             # nothing written at all
    else:
        assert "references" in _edge_types(a, b) and "referenced_by" not in _edge_types(b, a)   # only the forward half


def test_exact_aliases_a_concurrent_edit_between_commit_and_read_blocks_later_steps(store, tmp_path, monkeypatch):
    a, b = add("a note"), add("b note")
    plan = seal([rt("s1", a, ["t1"]), rt("s2", a, ["t2"]), lk("s3", a, b)])
    real = storage.update_memory
    def racing(conn, mid, **kw):
        out = real(conn, mid, **kw)
        if kw.get("tags") and "t1" in kw["tags"]:
            real(conn, mid, metadata={"foreign": "x"}, embed=False)       # unrelated metadata edit after OUR commit
        return out
    monkeypatch.setattr(storage, "update_memory", racing)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert [x["id"] for x in res["applied"]] == ["s1"] and "anomaly" in res["applied"][0]
    r = reasons(res)
    assert "state sha" in r["s2"] and "state sha" in r["s3"]
    assert "t2" not in mem(a).tags and "references" not in _edge_types(a, b)


def test_retag_never_rewrites_an_existing_tag(store, tmp_path):
    a = add("a note", tags=["architecture"])                      # generic, bare, projectless
    p = add("p note", tags=["architecture"], meta={"kind": "item-record"})
    with storage.connect() as conn:                               # the project is set elsewhere, the bare tag stays
        storage.update_memory(conn, p, metadata={"project": "memora"}, embed=False)
    assert "architecture" in mem(p).tags
    plan = seal([sp("b1", a), rt("s1", a, ["extra"]), rt("s2", p, ["extra"])])
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert "architecture" in mem(a).tags and "extra" not in mem(a).tags and "architecture" in mem(p).tags
    assert "rewrite existing tag" in reasons(res)["s1"] and "rewrite existing tag" in reasons(res)["s2"]


def test_replay_after_a_cap_limited_run_completes_the_rest_and_foreign_edits_still_refuse(store, tmp_path):
    a, b, c, d_ = add("a note"), add("b note"), add("c note"), add("d note")
    plan = seal([rt("s1", a, ["t"]), lk("s2", a, b), rt("s3", c, ["u"]), lk("s4", c, d_)])
    dec = dec_for(plan, tmp_path, approve_ops=ALL)
    j = tmp_path / "plan.applied.json"
    r1 = ta.apply_report(plan, dec, TidyLimits(max_writes=3), journal=j)
    assert [x["id"] for x in r1["applied"]] == ["s1", "s2"] and {d["id"] for d in r1["deferred"]} == {"s3", "s4"}
    with storage.connect() as conn:                                # a foreign edit of c after the first run
        storage.update_memory(conn, c, metadata={"foreign": 1}, embed=False)
    n = len(outbox())
    r2 = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert {x["id"] for x in r2["skipped"] if x["reason"].startswith("already done")} == {"s1", "s2"}
    assert "state sha" in reasons(r2)["s3"] and "state sha" in reasons(r2)["s4"]       # foreign edit refuses
    assert len(outbox()) == n


def test_replay_completes_unfinished_approved_work_and_finished_work_adds_no_outbox_rows(store, tmp_path):
    a, b, c = add("a note"), add("b note"), add("c note")
    plan = seal([rt("s1", a, ["t"]), lk("s2", a, b), lk("s3", b, c)])
    dec = dec_for(plan, tmp_path, approve_ops=ALL)
    j = tmp_path / "plan.applied.json"
    r1 = ta.apply_report(plan, dec, TidyLimits(max_writes=2), journal=j)     # retag 1, link needs 2: deferred
    assert [x["id"] for x in r1["applied"]] == ["s1"] and {d["id"] for d in r1["deferred"]} == {"s2", "s3"}
    n = len(outbox())
    r2 = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert [x["id"] for x in r2["applied"]] == ["s2", "s3"] and reasons(r2)["s1"].startswith("already done")
    n2 = len(outbox())
    assert n2 > n and not [r for r in outbox()[n:] if r[0] == "memories" and r[2] == f"[{a}]"]    # s1 wrote nothing again
    r3 = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert not r3["applied"] and len(outbox()) == n2


def test_half_written_link_is_completed_by_a_rerun_with_one_direction(store, tmp_path, monkeypatch):
    a, b = add("a note"), add("b note")
    plan = seal([lk("s1", a, b)])
    dec = dec_for(plan, tmp_path, approve_ops=ALL)
    real, calls = ta._guarded_edge, {"n": 0}
    def flaky(*x, **k):
        calls["n"] += 1
        return False if calls["n"] == 2 else real(*x, **k)         # the reverse write is refused once
    monkeypatch.setattr(ta, "_guarded_edge", flaky)
    r1 = run(plan, dec)
    assert not r1["applied"] and "partial" in reasons(r1)["s1"] and "references" in _edge_types(a, b)
    monkeypatch.setattr(ta, "_guarded_edge", real)
    r2 = run(plan, dec)
    assert [x["directions"] for x in r2["applied"]] == [1] and "referenced_by" in _edge_types(b, a)
    assert r2["counts"]["writes_charged"] == 1


def test_a_deadline_inside_a_write_is_ambiguous_with_its_step_id_and_not_deferred(store, tmp_path, monkeypatch):
    import time as _t
    a, b = add("a note"), add("b note")
    plan = seal([sp("b1", a), sp("b2", b)])
    real = storage.update_memory
    def slow(*x, **k):
        _t.sleep(0.6)
        return real(*x, **k)
    monkeypatch.setattr(storage, "update_memory", slow)
    res = ta.apply_report(plan, dec_for(plan, tmp_path, approve_ops=ALL), TidyLimits(max_seconds=0.2))
    assert res["aborted"] == "max_seconds" and [x["id"] for x in res["ambiguous"]] == ["b1"]
    assert {d["id"] for d in res["deferred"]} == {"b2"}


# ------------------------------------------------------------------ round 2: the commit-to-journal window

class Crash(BaseException):
    """A process death: not an Exception, so nothing in the apply loop swallows it."""


def _journal(tmp_path):
    return json.loads((tmp_path / "plan.applied.json").read_text())


def test_crash_after_the_retag_commit_before_the_post_read_replays_to_completion(store, tmp_path, monkeypatch):
    a, b = add("a note"), add("b note")
    plan = seal([rt("s1", a, ["t"]), lk("s2", a, b)])
    dec = dec_for(plan, tmp_path, approve_ops=ALL)
    j = tmp_path / "plan.applied.json"
    def die(*x, **k):
        raise Crash()
    real_after = ta._after
    monkeypatch.setattr(ta, "_after", die)
    with pytest.raises(Crash):
        ta.apply_report(plan, dec, TidyLimits(), journal=j)
    monkeypatch.setattr(ta, "_after", real_after)
    assert "t" in mem(a).tags                                       # the retag committed
    jr = _journal(tmp_path)
    assert jr["report_digest"] == plan["digest"] and list(jr["post"].values())[0] == [sha(a)]   # intent was durable first
    r2 = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert reasons(r2)["s1"].startswith("already done") and [x["id"] for x in r2["applied"]] == ["s2"]
    n = len(outbox())
    r3 = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert not r3["applied"] and len(outbox()) == n                  # third replay: zero additional rows


def test_deadline_in_set_project_that_commits_later_is_settled_then_replayed(store, tmp_path, monkeypatch):
    import time as _t
    a = add("a note")
    plan = seal([sp("b1", a), rt("s2", a, ["t"])])
    dec = dec_for(plan, tmp_path, approve_ops=ALL)
    j = tmp_path / "plan.applied.json"
    real = storage.update_memory
    def slow(*x, **k):
        _t.sleep(0.6)
        return real(*x, **k)
    monkeypatch.setattr(storage, "update_memory", slow)
    r1 = ta.apply_report(plan, dec, TidyLimits(max_seconds=0.2), journal=j)
    assert r1["aborted"] == "max_seconds" and [x["id"] for x in r1["ambiguous"]] == ["b1"]
    for _ in range(50):                                               # settle the in-flight call
        if mem(a).metadata.get("project"):
            break
        _t.sleep(0.1)
    monkeypatch.setattr(storage, "update_memory", real)
    assert mem(a).metadata.get("project") == "memora"
    r2 = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert reasons(r2)["b1"].startswith("already done") and [x["id"] for x in r2["applied"]] == ["s2"]
    n = len(outbox())
    assert not ta.apply_report(plan, dec, TidyLimits(), journal=j)["applied"] and len(outbox()) == n


@pytest.mark.parametrize("window", ["committed", "never_committed"])
def test_foreign_edits_in_each_recovery_window_still_refuse(store, tmp_path, monkeypatch, window):
    a, b = add("a note"), add("b note")
    plan = seal([rt("s1", a, ["t"]), lk("s2", a, b), rt("s3", a, ["u"])])
    dec = dec_for(plan, tmp_path, approve_ops=ALL)
    j = tmp_path / "plan.applied.json"
    def die(*x, **k):
        raise Crash()
    real_after, real_upd = ta._after, storage.update_memory
    if window == "committed":
        monkeypatch.setattr(ta, "_after", die)
    else:
        monkeypatch.setattr(storage, "update_memory", die)          # dies after the intent, before the commit
    with pytest.raises(Crash):
        ta.apply_report(plan, dec, TidyLimits(), journal=j)
    monkeypatch.setattr(ta, "_after", real_after)
    monkeypatch.setattr(storage, "update_memory", real_upd)
    with storage.connect() as conn:
        storage.update_memory(conn, a, metadata={"foreign": 1}, embed=False)     # someone else edits A
    r = ta.apply_report(plan, dec, TidyLimits(), journal=j)
    assert not r["applied"] and all("state sha" in reasons(r)[i] or reasons(r)[i].startswith("already done")
                                    for i in ("s1", "s2", "s3"))
    assert "references" not in _edge_types(a, b) and "u" not in mem(a).tags
    if window == "never_committed":
        assert "t" not in mem(a).tags


def test_an_unwritable_journal_writes_nothing(store, tmp_path):
    a = add("a note")
    plan = seal([sp("b1", a)])
    with pytest.raises(TidyError, match="journal"):
        ta.apply_report(plan, dec_for(plan, tmp_path, approve_ops=ALL), TidyLimits(),
                        journal=tmp_path / "no-such-dir" / "j.json")
    assert mem(a).metadata.get("project") is None


def test_retag_refuses_to_retarget_a_legacy_typed_tag(store, tmp_path):
    a = add("a note", tags=["memora/issues"])
    with storage.connect() as conn:
        storage.update_memory(conn, a, metadata={"project": "clmux"}, embed=False)
    before = list(mem(a).tags)
    plan = seal([rt("s1", a, ["extra"])])
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    if res["applied"]:       # the store keeps the tag as is: add-only holds
        assert set(before) <= set(mem(a).tags)
    else:
        assert "rewrite existing tag" in reasons(res)["s1"] and mem(a).tags == before


# ------------------------------------------------------------------ Item 259: the project digest

DIG_OPS = ("bootstrap_digest", "replace_digest", "repair_digest")


def old_digest(run="old", project="memora", digest_of="tidy", text="digest: older text"):
    with storage.connect() as conn:
        r = storage.add_memory(conn, content=text, tags=[f"{project}/digest"], project=project,
                               metadata={"kind": "digest", "project": project, "digest_of": digest_of, "run": run})
        conn.commit()
    return r["id"]


def pm(text, **kw):                                   # a memory of project memora, citable by a digest
    return add(text, meta={"project": "memora"}, **kw)


def dg(sid, op, cites, supersedes=None, project="memora", run="r9", body=None, **kw):
    st = {"id": sid, "op": op, "create": "@d1", "sections": {}, "tags": [f"{project}/digest"],
          "body": body or "## Current decisions\n- the first decision [" + ", ".join(f"m{c}" for c in cites) + "]\n",
          "metadata": {"kind": "digest", "project": project, "digest_of": "tidy", "run": run, "covers": list(cites)},
          "cites": list(cites), "depends_on": [], "needs_approval": False, "status": "eligible",
          "charge": {"bootstrap_digest": 1, "replace_digest": 3}[op], "expect": {}}
    if supersedes is not None:
        st["supersedes"] = supersedes
        st["expect"] = {str(supersedes): sha(supersedes)}
    st.update(kw)
    return st


def rp(sid, winner, loser, **kw):
    return {"id": sid, "op": "repair_digest", "winner": winner, "supersedes": loser, "both": sorted([winner, loser]),
            "charge": 2, "needs_approval": False, "status": "eligible", "depends_on": [], "cites": [],
            "expect": {str(winner): sha(winner), str(loser): sha(loser)}, **kw}


def dseal(steps):
    return seal(steps, project="memora")


def live_digest_ids():
    with storage.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM memories WHERE json_extract(metadata, '$.kind') = 'digest' ORDER BY id")]
        return [i for i in ids if not dream._retired(conn, i, storage)]


def n_memories():
    with storage.connect() as conn:
        return conn.execute("SELECT count(*) FROM memories").fetchone()[0]


def edge_types(a, b):
    return {r["edge_type"] for r in mem(a).refs if r.get("id") == b}


def d_dec(plan, tmp_path, ids):
    return dec_for(plan, tmp_path, approve=list(ids))


def test_digest_steps_are_approved_by_id_only_whatever_the_report_says(store, tmp_path):
    c = pm("cited")
    plan = dseal([dg("s1", "bootstrap_digest", [c]), dg("s2", "replace_digest", [c], supersedes=old_digest())])
    for op in DIG_OPS:                                  # approve_ops can not name a digest op at all
        p = tmp_path / "d.json"
        p.write_text(json.dumps({"report_digest": plan["digest"], "approve_ops": [{"op": op}]}))
        with pytest.raises(TidyError, match="by its id only"):
            ta.load_decisions(str(p), plan)
    forged = {"approve": [], "reject": [], "approve_ops": [{"op": "bootstrap_digest"}]}   # even a hand-built one
    assert ta.approved_by(plan["steps"][0], forged) is None                  # the report says needs_approval: false
    assert ta.approved_by(plan["steps"][0], dict(forged, approve=["s1"])) == "approve:id"
    assert ta.approved_by(plan["steps"][0], dict(forged, approve=["s1"], reject=["s1"])) is None
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert res["counts"]["applied"] == 0 and live_digest_ids() == [plan["steps"][1]["supersedes"]]


def test_bootstrap_creates_one_digest_with_embedding_u_i_outbox_no_auto_links_and_a_rerun_creates_nothing(store, tmp_path):
    c1, c2 = pm("first cited note about alpha"), pm("second cited note about beta")
    other = pm("unrelated note about alpha beta")
    plan = dseal([dg("s1", "bootstrap_digest", [c1, c2])])
    d = d_dec(plan, tmp_path, ["s1"])
    before_emb, before_content, n0, o0 = embeddings(), {i: mem(i).content for i in (c1, c2, other)}, n_memories(), len(outbox())
    res = run(plan, d)
    assert res["counts"]["applied"] == 1 and res["counts"]["writes_charged"] == 1, res
    (nid,) = live_digest_ids()
    m = mem(nid)
    assert m.content.strip() == plan["steps"][0]["body"].strip() and n_memories() == n0 + 1
    assert m.metadata["kind"] == "digest" and m.metadata["project"] == "memora" and m.metadata["digest_of"] == "tidy"
    assert m.metadata["run"] == "r9" and m.metadata["covers"] == [c1, c2] and "memora/digest" in m.tags
    assert nid in embeddings() and {i: v for i, v in embeddings().items() if i != nid} == before_emb
    assert {i: mem(i).content for i in (c1, c2, other)} == before_content
    assert m.refs == [] and all(r.get("id") != nid for i in (c1, c2, other) for r in mem(i).refs)   # no auto related_to
    new_rows = outbox()[o0:]
    assert new_rows and {op for _t, op, _pk in new_rows} <= {"I", "U"} and all(op != "D" for _t, op, _pk in outbox())
    n1, o1 = n_memories(), len(outbox())
    again = run(plan, d)                                          # the same plan again
    assert again["counts"]["applied"] == 0 and again["counts"]["already_done"] == 1
    assert n_memories() == n1 and len(outbox()) == o1 and live_digest_ids() == [nid]


def test_creating_a_digest_calls_no_llm_no_subprocess_and_no_suggestion_logic(store, tmp_path, monkeypatch):
    import subprocess
    c = pm("cited note")
    def boom(*a, **k):
        raise AssertionError("an LLM, a subprocess or a suggestion path was called")
    monkeypatch.setattr(storage, "_get_llm_client", boom, raising=False)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    for name in ("suggest_similar", "_suggest_tags", "find_similar_paths"):
        monkeypatch.setattr(storage, name, boom, raising=False)
    monkeypatch.setattr(storage, "_update_crossrefs_for_memory", boom)            # the auto-link pass must not run
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 1 and len(live_digest_ids()) == 1


def test_bootstrap_refuses_when_the_project_already_has_a_live_digest(store, tmp_path):
    c, p = pm("cited"), old_digest()
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    n0 = n_memories()
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert "stale" in reasons(res)["s1"] and n_memories() == n0 and live_digest_ids() == [p]


def test_replace_creates_then_supersedes_both_ways_never_deletes_and_a_rerun_creates_nothing(store, tmp_path):
    c = pm("cited note")
    p = old_digest()
    p_content, p_embed = mem(p).content, embeddings()[p]
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    o0 = len(outbox())
    res = run(plan, d)
    assert res["counts"]["applied"] == 1 and res["counts"]["writes_charged"] == 3, res
    (nid,) = live_digest_ids()
    assert nid != p and "supersedes" in edge_types(nid, p) and "superseded_by" in edge_types(p, nid)
    assert mem(p).content == p_content and embeddings()[p] == p_embed             # the old digest is kept as it was
    new_rows = outbox()[o0:]
    assert {op for _t, op, _pk in new_rows} <= {"I", "U"} and all(op != "D" for _t, op, _pk in outbox())
    n1, o1 = n_memories(), len(outbox())
    again = run(plan, d)
    assert again["counts"]["applied"] == 0 and n_memories() == n1 and len(outbox()) == o1
    assert reasons(again)["s1"].startswith("already done") and live_digest_ids() == [nid]


@pytest.mark.parametrize("how", ["changed", "two_live", "none_live", "foreign_kind"])
def test_replace_guards_refuse_and_write_nothing(store, tmp_path, how):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    if how == "changed":
        with storage.connect() as conn:
            storage.update_memory(conn, p, tags=["memora/digest", "extra"], embed=False)
    elif how == "two_live":
        old_digest(run="other")
    elif how == "none_live":
        newer = old_digest(run="newer")
        with storage.connect() as conn:
            dream._guarded_edge(conn, storage, newer, p, "supersedes", p, dream._fresh(conn, p)[1], True)
    elif how == "foreign_kind":
        with storage.connect() as conn:                       # the "digest" is not one any more: another project's
            storage.update_memory(conn, p, metadata={"project": "clmux"}, embed=False)
    n0, o0 = n_memories(), len(outbox())
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 0 and n_memories() == n0 and len(outbox()) == o0, reasons(res)


def test_a_crash_after_the_create_before_the_edges_resumes_without_a_second_digest(store, tmp_path, monkeypatch):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = ta._edges
    def crash(*a, **k):
        raise RuntimeError("process died")
    monkeypatch.setattr(ta, "_edges", crash)
    res = run(plan, d)
    assert "write failed" in reasons(res)["s1"] and len(live_digest_ids()) == 2     # D exists, nothing hidden yet
    n1 = n_memories()
    monkeypatch.setattr(ta, "_edges", real)
    again = run(plan, d)
    assert again["counts"]["applied"] == 1 and n_memories() == n1                   # edges only: no second create
    (nid,) = live_digest_ids()
    assert "supersedes" in edge_types(nid, p) and "superseded_by" in edge_types(p, nid)
    assert run(plan, d)["counts"]["applied"] == 0


def test_a_forward_only_state_gets_its_reverse_edge_from_a_rerun(store, tmp_path, monkeypatch):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = ta._digest_edge
    def forward_only(cx, holder, peer, et, guards):
        return False if et == "superseded_by" else real(cx, holder, peer, et, guards)
    monkeypatch.setattr(ta, "_digest_edge", forward_only)
    res = run(plan, d)
    assert "partial" in res["applied"][0] and "superseded_by" not in edge_types(p, live_digest_ids()[0])
    monkeypatch.setattr(ta, "_digest_edge", real)
    n1 = n_memories()
    again = run(plan, d)
    assert again["counts"]["applied"] == 1 and n_memories() == n1 and "superseded_by" in edge_types(p, live_digest_ids()[0])


def test_repair_writes_the_edges_only_for_two_tidy_digests_and_never_creates(store, tmp_path):
    older, newer = old_digest(run="a"), old_digest(run="b")
    plan = dseal([rp("s1", newer, older)])
    d = d_dec(plan, tmp_path, ["s1"])
    n0 = n_memories()
    res = run(plan, d)
    assert res["counts"]["applied"] == 1 and res["counts"]["writes_charged"] == 2 and n_memories() == n0, res
    assert live_digest_ids() == [newer] and "supersedes" in edge_types(newer, older) and "superseded_by" in edge_types(older, newer)
    assert all(op != "D" for _t, op, _pk in outbox())
    assert run(plan, d)["counts"]["applied"] == 0 and reasons(run(plan, d))["s1"].startswith("already done")


@pytest.mark.parametrize("how", ["not_tidy", "winner_older", "three_live", "changed"])
def test_repair_guards_refuse_and_write_nothing(store, tmp_path, how):
    older = old_digest(run="a", digest_of="someone-else" if how == "not_tidy" else "tidy")
    newer = old_digest(run="b")
    win, lose = (older, newer) if how == "winner_older" else (newer, older)
    plan = dseal([rp("s1", win, lose)])
    if how == "three_live":
        old_digest(run="c")
    if how == "changed":
        with storage.connect() as conn:
            storage.update_memory(conn, older, tags=["memora/digest", "x"], embed=False)
    o0 = len(outbox())
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 0 and len(outbox()) == o0 and len(live_digest_ids()) >= 2, reasons(res)


def test_a_digest_step_whose_dependency_did_not_commit_is_deferred_not_rewritten(store, tmp_path):
    c, a, b = pm("cited"), pm("a note"), pm("b note")
    merge = {"id": "s1", "op": "merge", "needs_approval": False, "status": "eligible", "expect": {str(a): sha(a)}}
    link = lk("s2", a, b)
    with storage.connect() as conn:
        storage.update_memory(conn, b, tags=["touched"], embed=False)                 # the link's judged state is stale
    plan = dseal([merge, link, dg("s3", "bootstrap_digest", [c], depends_on=["s1"]),
                  dg("s4", "bootstrap_digest", [c], depends_on=["s2"])])
    n0 = n_memories()
    res = run(plan, d_dec(plan, tmp_path, ["s1", "s2", "s3", "s4"]))
    r = reasons(res)
    assert r["s3"] == "deferred: dependency s1" and r["s4"] == "deferred: dependency s2"
    assert n_memories() == n0 and live_digest_ids() == []
    assert {x["id"] for x in res["deferred"]} == {"s3", "s4"}


def test_a_dependency_that_committed_lets_the_digest_run(store, tmp_path):
    c, a, b = pm("cited"), pm("a note"), pm("b note")
    plan = dseal([lk("s1", a, b), dg("s2", "bootstrap_digest", [c], depends_on=["s1"])])
    res = run(plan, d_dec(plan, tmp_path, ["s1", "s2"]))
    assert res["counts"]["applied"] == 2 and len(live_digest_ids()) == 1


def test_a_digest_citing_a_symbolic_merge_result_is_refused_and_the_body_is_never_rewritten(store, tmp_path):
    c = pm("cited")
    n0 = n_memories()
    for kw in ({"cites": [c, "@c3"]}, {"body": "## Current decisions\n- merged [@c3]\n"}):
        st = dg("s1", "bootstrap_digest", [c])
        st.update(kw)
        plan = dseal([st])
        res = run(plan, d_dec(plan, tmp_path, ["s1"]))
        assert "@c3" in reasons(res)["s1"] and "refused" in reasons(res)["s1"]
        assert n_memories() == n0


@pytest.mark.parametrize("how", ["gone", "retired", "other_project", "no_project"])
def test_every_citation_must_be_a_live_memory_of_the_project_at_apply_time(store, tmp_path, how):
    good, bad = pm("good"), pm("bad")
    plan = dseal([dg("s1", "bootstrap_digest", [good, bad])])
    with storage.connect() as conn:
        if how == "gone":
            conn.execute("DELETE FROM memories WHERE id = ?", (bad,))
            conn.commit()
        elif how == "retired":
            newer = pm("newer")
            dream._guarded_edge(conn, storage, newer, bad, "supersedes", bad, dream._fresh(conn, bad)[1], True)
        elif how == "other_project":
            storage.update_memory(conn, bad, metadata={"project": "clmux"}, embed=False)
        else:
            conn.execute("UPDATE memories SET metadata = NULL WHERE id = ?", (bad,))
            conn.commit()
    n0 = n_memories()
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert "refused: cited memory" in reasons(res)["s1"] and n_memories() == n0 and live_digest_ids() == []


def test_digest_charges_are_reserved_before_dispatch(store, tmp_path):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    n0 = n_memories()
    res = run(plan, d_dec(plan, tmp_path, ["s1"]), TidyLimits(max_writes=2))         # a replace is 3
    assert reasons(res)["s1"] == "cap:max_writes" and n_memories() == n0 and live_digest_ids() == [p]
    res = run(plan, d_dec(plan, tmp_path, ["s1"]), TidyLimits(max_writes=3))
    assert res["counts"]["applied"] == 1 and res["counts"]["writes_charged"] == 3


@pytest.mark.parametrize("bad", ["no_run", "wrong_project", "no_tag", "empty_body", "bootstrap_with_expect"])
def test_malformed_digest_steps_are_skipped_before_the_store_is_read(store, tmp_path, bad):
    c = pm("cited")
    st = dg("s1", "bootstrap_digest", [c])
    if bad == "no_run":
        del st["metadata"]["run"]
    elif bad == "wrong_project":
        st["metadata"]["project"] = "clmux"
    elif bad == "no_tag":
        st["tags"] = ["other"]
    elif bad == "empty_body":
        st["body"] = "  "
    else:
        st["expect"] = {str(c): sha(c)}
    plan = dseal([st])
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert "malformed" in reasons(res)["s1"] and live_digest_ids() == []


def test_digest_step_counts_in_check_mode(tmp_path, capsys):
    plan = dseal([dg("s1", "bootstrap_digest", [1])])
    p, d = tmp_path / "p.json", tmp_path / "d.json"
    p.write_text(json.dumps(plan))
    d.write_text(json.dumps({"report_digest": plan["digest"], "approve": ["s1"]}))
    assert ta.main(["--apply", "--plan", str(p), "--decisions", str(d), "--check", "--max-age-hours", "1e9"]) == 0
    assert json.loads(capsys.readouterr().out)["would_attempt_by_op"] == {"bootstrap_digest": 1}


# ------------------------------------------------------------------ Item 259: the store's derived metadata is not an anomaly

@pytest.mark.parametrize("meta", [{"hierarchy": {"path": ["memora", "issues"]}},
                                  {"section": "memora", "subsection": "issues"},
                                  {"hierarchy": {"section": "memora", "subsection": "issues"}, "tasks": [], "done": False}])
def test_set_project_on_a_row_with_a_hierarchy_or_section_is_not_an_anomaly_and_later_steps_still_run(store, tmp_path, meta):
    a = add("a note with a hierarchy")
    with storage.connect() as conn:                      # a legacy row as the live store has: metadata not yet canonical
        conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(meta), a))
        conn.commit()
    plan = seal([sp("s1", a), rt("s2", a, ["more"])])
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert res["counts"]["applied"] == 2 and res["counts"]["anomalies"] == 0, res
    assert not any("anomaly" in x for x in res["applied"]) and "more" in mem(a).tags
    assert mem(a).metadata["project"] == "memora"
    assert run(plan, dec_for(plan, tmp_path, approve_ops=ALL))["counts"]["applied"] == 0


def test_a_real_foreign_edit_after_set_project_is_still_an_anomaly(store, tmp_path, monkeypatch):
    a = add("a note")
    plan = seal([sp("s1", a), rt("s2", a, ["more"])])
    real = storage.update_memory
    def racing(conn, mid, **kw):
        r = real(conn, mid, **kw)
        conn.execute("UPDATE memories SET metadata = json_set(metadata, '$.sneaky', 1) WHERE id = ?", (mid,))
        conn.commit()
        return r
    monkeypatch.setattr(storage, "update_memory", racing)
    res = run(plan, dec_for(plan, tmp_path, approve_ops=ALL))
    assert res["counts"]["anomalies"] == 1 and "state sha" in reasons(res)["s2"]


def test_the_journal_write_is_fsynced_with_its_directory(store, tmp_path, monkeypatch):
    import os
    a = add("a note")
    plan = seal([sp("s1", a)])
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real(fd))[1])
    ta.apply_report(plan, dec_for(plan, tmp_path, approve_ops=ALL), LIM, journal=tmp_path / "j.applied.json")
    import stat
    assert any(stat.S_ISDIR(m) for m in synced) and any(stat.S_ISREG(m) for m in synced)


# ------------------------------------------------------------------ Item 259 round 1: the write carries the state

def _mutate(how, win, lose, third_run="third"):
    """Change the world the way a concurrent writer could, with the store's own functions."""
    with storage.connect() as conn:
        if how == "winner_edit":
            storage.update_memory(conn, win, tags=["memora/digest", "edited"], embed=False)
        elif how == "winner_project":
            storage.update_memory(conn, win, metadata={"project": "clmux"}, embed=False)
        elif how == "winner_retired":
            other = old_digest(run="zzz", project="clmux")
            dream._guarded_edge(conn, storage, other, win, "supersedes", win, dream._fresh(conn, win)[1], True)
        elif how == "third_live":
            old_digest(run=third_run)
        elif how == "loser_edit":
            storage.update_memory(conn, lose, tags=["memora/digest", "edited"], embed=False)
        conn.commit()


@pytest.mark.parametrize("how", ["winner_edit", "winner_project", "winner_retired", "third_live", "loser_edit"])
def test_forward_edge_statement_refuses_a_change_between_the_check_and_the_write(store, tmp_path, monkeypatch, how):
    older, newer = old_digest(run="a"), old_digest(run="b")
    plan = dseal([rp("s1", newer, older)])
    real = ta._digest_edge
    def racing(cx, holder, peer, et, guards):
        if et == "supersedes":
            _mutate(how, newer, older)
        return real(cx, holder, peer, et, guards)
    monkeypatch.setattr(ta, "_digest_edge", racing)
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert "supersedes" not in edge_types(newer, older) and older in live_digest_ids(), res
    assert res["counts"]["applied"] == 0 or "partial" in res["applied"][0]


@pytest.mark.parametrize("how", ["winner_edit", "winner_project", "winner_retired", "third_live", "loser_edit"])
def test_reverse_edge_statement_refuses_a_change_after_our_forward_edge(store, tmp_path, monkeypatch, how):
    older, newer = old_digest(run="a"), old_digest(run="b")
    plan = dseal([rp("s1", newer, older)])
    real = ta._digest_edge
    def racing(cx, holder, peer, et, guards):
        if et == "superseded_by":
            _mutate(how, newer, older)
        return real(cx, holder, peer, et, guards)
    monkeypatch.setattr(ta, "_digest_edge", racing)
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert "superseded_by" not in edge_types(older, newer) and "partial" in res["applied"][0], res
    assert "supersedes" in edge_types(newer, older)           # our own forward edge is the only thing written


@pytest.mark.parametrize("op", ["bootstrap_digest", "replace_digest"])
def test_a_digest_that_appears_while_the_embedding_is_computed_makes_the_insert_write_nothing(store, tmp_path, monkeypatch, op):
    c = pm("cited")
    p = old_digest() if op == "replace_digest" else None
    st = dg("s1", op, [c], supersedes=p) if p else dg("s1", op, [c])
    plan = dseal([st])
    real, fired = storage._compute_embedding, []
    def slow(*a, **k):
        if not fired:
            fired.append(1)
            with storage.connect() as conn:       # a competitor creates its own digest during our embedding
                storage.add_memory(conn, content="competitor digest", project="memora", tags=["memora/digest"],
                                   metadata={"kind": "digest", "project": "memora", "digest_of": "tidy", "run": "rival"})
                conn.commit()
        return real(*a, **k)
    n0 = n_memories()
    monkeypatch.setattr(storage, "_compute_embedding", slow)
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 0 and "stale" in reasons(res)["s1"], res
    assert n_memories() == n0 + 1                              # only the competitor; ours was never inserted
    assert all(op_ != "D" for _t, op_, _pk in outbox())
    assert len(live_digest_ids()) == (2 if p else 1)           # no duplicate live digest from us


@pytest.mark.parametrize("how", ["cite_retired", "cite_other_project"])
def test_a_resume_revalidates_citations_before_it_hides_anything(store, tmp_path, monkeypatch, how):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = ta._edges
    monkeypatch.setattr(ta, "_edges", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
    run(plan, d)
    monkeypatch.setattr(ta, "_edges", real)
    with storage.connect() as conn:
        if how == "cite_retired":
            newer = pm("newer")
            dream._guarded_edge(conn, storage, newer, c, "supersedes", c, dream._fresh(conn, c)[1], True)
        else:
            storage.update_memory(conn, c, metadata={"project": "clmux"}, embed=False)
    n1 = n_memories()
    res = run(plan, d)
    assert "refused: cited memory" in reasons(res)["s1"] and n_memories() == n1
    assert p in live_digest_ids() and len(live_digest_ids()) == 2     # P is still live


def _forward_only(monkeypatch):
    real = ta._digest_edge
    monkeypatch.setattr(ta, "_digest_edge", lambda cx, h, p_, et, g: False if et == "superseded_by" else real(cx, h, p_, et, g))
    return real


def test_a_resume_after_a_forward_only_write_refuses_when_the_loser_was_edited(store, tmp_path, monkeypatch):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = _forward_only(monkeypatch)
    run(plan, d)
    monkeypatch.setattr(ta, "_digest_edge", real)
    with storage.connect() as conn:
        storage.update_memory(conn, p, tags=["memora/digest", "edited"], embed=False)   # kind and project kept
    (nid,) = live_digest_ids()
    res = run(plan, d)
    assert "superseded_by" not in edge_types(p, nid) and "changed since it was judged" in str(res["applied"][0]), res


def test_unchanged_resume_completes_exactly_the_one_missing_edge(store, tmp_path, monkeypatch):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = _forward_only(monkeypatch)
    run(plan, d)
    monkeypatch.setattr(ta, "_digest_edge", real)
    n1 = n_memories()
    res = run(plan, d)
    assert res["counts"]["applied"] == 1 and res["counts"]["writes_charged"] == 1 and n_memories() == n1
    assert res["applied"][0].get("reverse") is True and "forward" not in res["applied"][0]


@pytest.mark.parametrize("how", ["loser_edit", "winner_edit", "winner_not_tidy"])
def test_repair_resume_with_one_live_digest_revalidates_everything(store, tmp_path, monkeypatch, how):
    older, newer = old_digest(run="a"), old_digest(run="b")
    plan = dseal([rp("s1", newer, older)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = _forward_only(monkeypatch)
    run(plan, d)
    monkeypatch.setattr(ta, "_digest_edge", real)
    with storage.connect() as conn:
        if how == "loser_edit":
            storage.update_memory(conn, older, tags=["memora/digest", "edited"], embed=False)
        elif how == "winner_edit":
            storage.update_memory(conn, newer, tags=["memora/digest", "edited"], embed=False)
        else:
            storage.update_memory(conn, newer, metadata={"digest_of": "someone"}, embed=False)
    res = run(plan, d)
    assert "superseded_by" not in edge_types(older, newer) and res["counts"]["applied"] == 0, res


def test_repair_resume_unchanged_completes_the_missing_edge(store, tmp_path, monkeypatch):
    older, newer = old_digest(run="a"), old_digest(run="b")
    plan = dseal([rp("s1", newer, older)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = _forward_only(monkeypatch)
    run(plan, d)
    monkeypatch.setattr(ta, "_digest_edge", real)
    res = run(plan, d)
    assert res["counts"]["applied"] == 1 and "superseded_by" in edge_types(older, newer) and res["counts"]["writes_charged"] == 1


# ------------------------------------------------------------------ Item 259 round 1: a partial create is not a committed digest

def _partial_embedding_once(monkeypatch):
    real, fired = storage._upsert_embedding, []
    def failing(conn, mid, vec, **kw):
        if not fired:
            fired.append(1)
            conn.commit()                               # an auto-committing backend: the row and its FTS are durable
            raise RuntimeError("vector store down")
        return real(conn, mid, vec, **kw)
    monkeypatch.setattr(storage, "_upsert_embedding", failing)


def test_a_partial_bootstrap_create_is_completed_by_the_rerun_not_taken_for_done(store, tmp_path, monkeypatch):
    c = pm("cited")
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    d = d_dec(plan, tmp_path, ["s1"])
    _partial_embedding_once(monkeypatch)
    res = run(plan, d)
    assert "partial create" in reasons(res)["s1"]
    (nid,) = live_digest_ids()
    assert nid not in embeddings()                                   # inserted, but no vector yet
    n1 = n_memories()
    again = run(plan, d)
    assert again["counts"]["applied"] == 1 and again["applied"][0].get("completed") == nid
    assert nid in embeddings() and n_memories() == n1 and live_digest_ids() == [nid]
    assert run(plan, d)["counts"]["already_done"] == 1
    assert all(op != "D" for _t, op, _pk in outbox())


def test_a_partial_replace_create_hides_nothing_until_it_is_complete(store, tmp_path, monkeypatch):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    _partial_embedding_once(monkeypatch)
    res = run(plan, d)
    assert "partial create" in reasons(res)["s1"]
    new = [i for i in live_digest_ids() if i != p]
    assert len(new) == 1 and p in live_digest_ids() and "supersedes" not in edge_types(new[0], p)
    n1 = n_memories()
    again = run(plan, d)
    assert again["counts"]["applied"] == 1 and n_memories() == n1
    assert live_digest_ids() == new and new[0] in embeddings() and "superseded_by" in edge_types(p, new[0])
    assert all(op != "D" for _t, op, _pk in outbox())


def test_a_materially_edited_digest_of_the_run_is_not_taken_for_ours(store, tmp_path):
    c = pm("cited")
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    d = d_dec(plan, tmp_path, ["s1"])
    run(plan, d)
    (nid,) = live_digest_ids()
    with storage.connect() as conn:
        storage.update_memory(conn, nid, metadata={"covers": [999]}, embed=False)
    n1 = n_memories()
    res = run(plan, d)
    assert "stale" in reasons(res)["s1"] and n_memories() == n1


def test_several_matching_live_digests_are_an_ambiguous_state_not_done(store, tmp_path):
    c = pm("cited")
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    for _ in range(2):
        with storage.connect() as conn:
            body = plan["steps"][0]["body"]
            storage.add_memory(conn, content=body, project="memora", tags=["memora/digest"],
                               metadata={**plan["steps"][0]["metadata"], "indexing": {"skip_crossrefs": True}})
            conn.commit()
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert reasons(res)["s1"].startswith("ambiguous")


def test_a_malformed_repair_both_list_is_a_shape_refusal(store, tmp_path):
    older, newer = old_digest(run="a"), old_digest(run="b")
    st = rp("s1", newer, older)
    st["both"] = [newer, "x"]
    plan = dseal([st])
    assert "malformed" in reasons(run(plan, d_dec(plan, tmp_path, ["s1"])))["s1"]


# ------------------------------------------------------------------ Item 259 round 2: validated snapshot, citations at create/completion

def _sql_edit(mid, how="content"):
    """A foreign writer that bypasses the store API (what a concurrent client's raw statement could do)."""
    with storage.connect() as conn:
        if how == "content":
            conn.execute("UPDATE memories SET content = content || ' (edited)', updated_at = '2099-01-01 00:00:00' WHERE id = ?", (mid,))
        else:
            conn.execute("UPDATE memories SET tags = '[\"memora/digest\", \"foreign\"]', updated_at = '2099-01-01 00:00:00' WHERE id = ?", (mid,))
        conn.commit()


def test_repair_winner_edited_right_after_the_caller_verified_it_hides_nothing(store, tmp_path, monkeypatch):
    older, newer = old_digest(run="a"), old_digest(run="b")
    plan = dseal([rp("s1", newer, older)])
    real = ta._edges
    def racing(cx, step, win, lose, check, *extra):
        _sql_edit(win)
        return real(cx, step, win, lose, check, *extra)
    monkeypatch.setattr(ta, "_edges", racing)
    o0 = len(outbox())
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 0 and "state sha" in reasons(res)["s1"]
    assert sorted(live_digest_ids()) == sorted([older, newer]) and not ({"supersedes", "superseded_by"} & (edge_types(newer, older) | edge_types(older, newer)))


@pytest.mark.parametrize("phase", ["after_create", "on_resume"])
def test_replace_winner_edited_after_create_or_identification_hides_nothing(store, tmp_path, monkeypatch, phase):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    d = d_dec(plan, tmp_path, ["s1"])
    real = ta._edges
    def racing(cx, step, win, lose, check, *extra):
        _sql_edit(win)
        return real(cx, step, win, lose, check, *extra)
    if phase == "on_resume":
        monkeypatch.setattr(ta, "_edges", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died")))
        run(plan, d)
    monkeypatch.setattr(ta, "_edges", racing)
    res = run(plan, d)
    new = [i for i in live_digest_ids() if i != p]
    if phase == "after_create":
        assert "no longer this step's sealed digest" in str(res["applied"][0])
        assert len(new) == 1 and "supersedes" not in edge_types(new[0], p)
    else:
        assert "no longer this step's sealed digest" in str(res["applied"][0])
    assert p in live_digest_ids() and "superseded_by" not in edge_types(p, new[0])


@pytest.mark.parametrize("how", ["cite_retired", "cite_moved"])
def test_a_partial_bootstrap_is_not_completed_after_a_citation_stopped_being_valid(store, tmp_path, monkeypatch, how):
    c, newer = pm("cited"), pm("newer")
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    d = d_dec(plan, tmp_path, ["s1"])
    _partial_embedding_once(monkeypatch)
    run(plan, d)
    (nid,) = live_digest_ids()
    with storage.connect() as conn:
        if how == "cite_retired":
            dream._guarded_edge(conn, storage, newer, c, "supersedes", c, dream._fresh(conn, c)[1], True)
        else:
            storage.update_memory(conn, c, metadata={"project": "clmux"}, embed=False)
    res = run(plan, d)
    assert "refused: cited memory" in reasons(res)["s1"] and res["counts"]["applied"] == 0 and nid not in embeddings()


@pytest.mark.parametrize("op", ["bootstrap_digest", "replace_digest"])
@pytest.mark.parametrize("how", ["cite_retired", "cite_moved"])
def test_a_citation_that_stops_being_valid_during_the_embedding_makes_the_insert_write_nothing(store, tmp_path, monkeypatch, op, how):
    c = pm("cited")
    p = old_digest() if op == "replace_digest" else None
    plan = dseal([dg("s1", op, [c], supersedes=p) if p else dg("s1", op, [c])])
    real, fired = storage._compute_embedding, []
    def slow(*a, **k):
        if not fired:
            fired.append(1)
            with storage.connect() as conn:
                if how == "cite_retired":
                    dream._guarded_edge(conn, storage, pm("newer"), c, "supersedes", c, dream._fresh(conn, c)[1], True)
                else:
                    storage.update_memory(conn, c, metadata={"project": "clmux"}, embed=False)
        return real(*a, **k)
    monkeypatch.setattr(storage, "_compute_embedding", slow)
    n0 = n_memories()
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 0 and "stale" in reasons(res)["s1"]
    assert len(live_digest_ids()) == (1 if p else 0) and (p is None or live_digest_ids() == [p])


def test_the_reindex_of_a_partial_create_is_refused_when_the_row_was_edited_meanwhile(store, tmp_path, monkeypatch):
    c = pm("cited")
    plan = dseal([dg("s1", "bootstrap_digest", [c])])
    d = d_dec(plan, tmp_path, ["s1"])
    _partial_embedding_once(monkeypatch)
    run(plan, d)
    (nid,) = live_digest_ids()
    real = storage.update_memory
    def racing(conn, mid, **kw):
        _sql_edit(mid, "tags")                              # a foreign edit between our read and the guarded reindex
        return real(conn, mid, **kw)
    monkeypatch.setattr(storage, "update_memory", racing)
    res = run(plan, d)
    assert "changed during the completion" in reasons(res)["s1"] and nid not in embeddings()
    assert "foreign" in mem(nid).tags and res["counts"]["applied"] == 0


def test_the_store_checks_the_retirement_the_same_way_in_sql_as_in_python(store):
    a, b = pm("a"), pm("b")
    cx = type("C", (), {})()
    cx.st = storage
    with storage.connect() as conn:
        sql = ta._retired_sql(cx, "m.id")
        def sql_retired(mid):
            return bool(conn.execute("SELECT " + sql + " FROM memories m WHERE m.id = ?", (mid,)).fetchone()[0])
        assert not sql_retired(a) and not dream._retired(conn, a, storage)
        dream._guarded_edge(conn, storage, b, a, "supersedes", a, dream._fresh(conn, a)[1], True)
        assert sql_retired(a) and dream._retired(conn, a, storage)
        x, y = pm("x"), pm("y")
        raw = dream._fresh(conn, y)[1]                       # reverse-only: y says it is superseded_by x, no forward edge
        dream._guarded_edge(conn, storage, y, x, "superseded_by", y, raw, False)
        assert sql_retired(y) and dream._retired(conn, y, storage)


# ------------------------------------------------------------------ Item 259 round 3: the completion write carries the state

def _during_next_embedding(monkeypatch, how, c, newer):
    real, fired = storage._compute_embedding, []
    def hooked(*a, **k):
        if not fired:
            fired.append(1)
            with storage.connect() as conn:
                if how == "cite_retired":
                    dream._guarded_edge(conn, storage, newer, c, "supersedes", c, dream._fresh(conn, c)[1], True)
                elif how == "cite_moved":
                    storage.update_memory(conn, c, metadata={"project": "clmux"}, embed=False)
                else:
                    storage.add_memory(conn, content="competitor digest", project="memora", tags=["memora/digest"],
                                       metadata={"kind": "digest", "project": "memora", "digest_of": "tidy", "run": "rival"})
                conn.commit()
        return real(*a, **k)
    monkeypatch.setattr(storage, "_compute_embedding", hooked)
    return real


@pytest.mark.parametrize("op", ["bootstrap_digest", "replace_digest"])
@pytest.mark.parametrize("how", ["cite_retired", "cite_moved", "competing"])
def test_completion_embedding_race_on_a_citation_or_the_live_set_refuses_completion_and_hiding(store, tmp_path, monkeypatch, op, how):
    c, newer = pm("cited"), pm("newer")
    p = old_digest() if op == "replace_digest" else None
    plan = dseal([dg("s1", op, [c], supersedes=p) if p else dg("s1", op, [c])])
    d = d_dec(plan, tmp_path, ["s1"])
    _partial_embedding_once(monkeypatch)
    run(plan, d)
    nid = [i for i in live_digest_ids() if i != p][0]
    real = _during_next_embedding(monkeypatch, how, c, newer)
    res = run(plan, d)                                              # the replay: its completion embedding is the race
    assert res["counts"]["applied"] == 0 and "during the completion" in reasons(res)["s1"], res
    if how != "competing":          # the competitor's own create back-fills missing vectors of its neighbours itself
        assert nid not in embeddings(), "an invalid partial digest was promoted"
    if p:
        assert p in live_digest_ids() and "superseded_by" not in edge_types(p, nid) and "supersedes" not in edge_types(nid, p)
    assert all(op_ != "D" for _t, op_, _pk in outbox())
    monkeypatch.setattr(storage, "_compute_embedding", real)


@pytest.mark.parametrize("op", ["bootstrap_digest", "replace_digest"])
def test_completion_unchanged_completes_the_same_id_and_a_third_replay_adds_nothing(store, tmp_path, monkeypatch, op):
    c = pm("cited")
    p = old_digest() if op == "replace_digest" else None
    plan = dseal([dg("s1", op, [c], supersedes=p) if p else dg("s1", op, [c])])
    d = d_dec(plan, tmp_path, ["s1"])
    _partial_embedding_once(monkeypatch)
    run(plan, d)
    nid = [i for i in live_digest_ids() if i != p][0]
    n1 = n_memories()
    assert run(plan, d)["counts"]["applied"] == 1
    assert nid in embeddings() and n_memories() == n1 and live_digest_ids() == [nid]
    n2, o2 = n_memories(), len(outbox())
    third = run(plan, d)
    assert third["counts"]["applied"] == 0 and n_memories() == n2 and len(outbox()) == o2
    assert all(op_ != "D" for _t, op_, _pk in outbox())


# ------------------------------------------------------------------ Item 259 (leader condition): the core write path is unchanged

class _Spy:
    def __init__(self, conn):
        self._c, self.calls = conn, []

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        return self._c.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _guarded_update_calls(guard_kwargs):
    mid = add("a note for the pin")
    with storage.connect() as conn:
        raw = dream._fresh(conn, mid)[1]
        spy = _Spy(conn)
        storage.update_memory(spy, mid, tags=["pin"], expected_row=raw, embed=False, **guard_kwargs)
        return mid, [c for c in spy.calls if c[0].startswith("UPDATE memories SET content")]


def test_update_memory_without_an_update_guard_runs_exactly_the_statement_it_always_ran(store):
    expected = (
        "UPDATE memories SET content = ?, metadata = ?, tags = ?, updated_at = ? "
        "WHERE id = ? AND content = ? AND metadata IS ? AND tags IS ? AND updated_at IS ? "
        "AND NOT EXISTS (SELECT 1 FROM tombstones WHERE memory_id = ?) "
        "AND NOT EXISTS (SELECT 1 FROM tombstone_components WHERE memory_id = ?) "
        "AND (SELECT related FROM memories_crossrefs WHERE memory_id = ?) IS ? "
        "AND NOT EXISTS (" + storage._SUPERSEDES_EDGE_TO_SQL + ")")
    for kw in ({}, {"update_guard": None}):
        mid, calls = _guarded_update_calls(kw)
        assert len(calls) == 1 and calls[0][0] == expected, kw
        assert len(calls[0][1]) == 14 and calls[0][1][-1] == mid          # no extra parameter either
    mid2, with_guard = _guarded_update_calls({"update_guard": ("1 = 1", ())})
    assert with_guard[0][0] == expected + " AND 1 = 1"                    # the guard is only ever appended


def test_update_memory_update_guard_false_writes_nothing(store):
    mid = add("a note for the guard")
    with storage.connect() as conn:
        raw = dream._fresh(conn, mid)[1]
        with pytest.raises(storage.ConcurrentUpdateError):
            storage.update_memory(conn, mid, tags=["x"], expected_row=raw, embed=False, update_guard=("1 = 0", ()))
    assert "x" not in mem(mid).tags


def test_replace_insert_is_refused_when_the_old_digest_changes_during_the_embedding(store, tmp_path, monkeypatch):
    c = pm("cited")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    real, fired = storage._compute_embedding, []
    def slow(*a, **k):
        if not fired:
            fired.append(1)
            _sql_edit(p, "tags")                       # the judged old digest is edited while we embed
        return real(*a, **k)
    monkeypatch.setattr(storage, "_compute_embedding", slow)
    n0 = n_memories()
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert res["counts"]["applied"] == 0 and "stale" in reasons(res)["s1"] and n_memories() == n0


def test_reverse_edge_is_refused_when_another_memory_also_supersedes_the_loser(store, tmp_path, monkeypatch):
    older, newer = old_digest(run="a"), old_digest(run="b")
    other = pm("another memory")
    plan = dseal([rp("s1", newer, older)])
    real = ta._digest_edge
    def racing(cx, holder, peer, et, guards):
        if et == "superseded_by":
            with storage.connect() as conn:                # a second superseder of the loser appears after our forward edge
                dream._guarded_edge(conn, storage, other, older, "supersedes", older, dream._fresh(conn, older)[1], False)
        return real(cx, holder, peer, et, guards)
    monkeypatch.setattr(ta, "_digest_edge", racing)
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert "superseded_by" not in edge_types(older, newer) and "partial" in res["applied"][0], res


def test_a_replacement_forward_edge_is_refused_when_a_citation_goes_stale_after_the_create(store, tmp_path, monkeypatch):
    c, newer = pm("cited"), pm("newer")
    p = old_digest()
    plan = dseal([dg("s1", "replace_digest", [c], supersedes=p)])
    real = ta._digest_edge
    def racing(cx, holder, peer, et, guards):
        if et == "supersedes":
            with storage.connect() as conn:
                dream._guarded_edge(conn, storage, newer, c, "supersedes", c, dream._fresh(conn, c)[1], True)
        return real(cx, holder, peer, et, guards)
    monkeypatch.setattr(ta, "_digest_edge", racing)
    res = run(plan, d_dec(plan, tmp_path, ["s1"]))
    assert p in live_digest_ids() and "partial" in res["applied"][0] and "superseded_by" not in edge_types(p, [i for i in live_digest_ids() if i != p][0])
