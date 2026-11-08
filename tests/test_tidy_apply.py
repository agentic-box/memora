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
             for i, op in enumerate(["merge", "retire", "bootstrap_digest", "replace_digest", "repair_digest", "frobnicate"])]
    plan = seal(steps)
    d = dec_for(plan, tmp_path, approve=[s["id"] for s in steps])
    before = outbox()
    res = run(plan, d)
    assert res["counts"]["applied"] == 0 and outbox() == before
    assert all(s["reason"] == f"skipped: {ta.NOT_SUPPORTED}" for s in res["skipped"]) and len(res["skipped"]) == 6


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
                      r"delete_memory", r"remove_link", r"\bcontent=", r"supersede", r"add_memory",
                      r"sqlite3\.connect"):
        assert not re.search(forbidden, code, re.I), forbidden
    assert "DELETE " not in code                                  # the only raw SQL writes are the guarded edge statement
    assert code.count("INSERT INTO memories_crossrefs") == 1 and code.count("UPDATE memories_crossrefs") == 1
    assert len(re.findall(r"\b(INSERT|UPDATE)\b ", code)) == 2
    assert code.count("update_memory(") == 2 and code.count("embed=False") == 2 and "_guarded_edge(" in code


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
