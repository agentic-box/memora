"""Item 252: the tidy pass, dry run only (memora/tidy.py, docs/tidy-design.md, docs/tidy.md)."""
import contextlib
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

import memora.storage as storage
from memora import dream, tidy
from memora.dream import Mem
from memora.tidy import (ClusterCtx, TidyConfig, TidyError, TidyLimits, Unsupported, split_statements)

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------- helpers


def M(mid, content, *, tags=None, meta=None, created="2026-01-01 00:00:00", refs=None, project="clmux"):
    metadata = {"project": project, **(meta or {})} if project else dict(meta or {})
    return Mem(mid, content, metadata, list(tags or []), created, created, list(refs or []))


def data_of(prompt):
    return json.loads(prompt.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0])


def ctx_of(*mems, vocab=None):
    return ClusterCtx("c1", {m.id: m for m in mems}, "clmux", vocab or {"clmux", "clmux/x"}, 0.0)


def ans(**kw):
    base = {"keep": [], "link": [], "retag": [], "contradiction": [], "merge": [], "retire": []}
    base.update(kw)
    return base


def keep(*ids):
    return [{"id": i, "reason": "r"} for i in ids]


def row(src, outcome, out=None, of=None, why=None):
    return {"src": list(src), "outcome": outcome, "out": out, "of": list(of) if of else None, "why": why}


def merge_entry(sources, text, statements, reason="r"):
    return {"sources": sorted(sources), "text": text, "statements": statements, "reason": reason}


LIM = TidyLimits()


def vmerge(entry, *mems):
    ctx = ctx_of(*mems)
    return tidy.validate_merge(entry, {"sources": sorted(m.id for m in mems), "reason": "r"}, ctx, "run1", LIM)


# ----------------------------------------------------------------------------- statements (3.1)


def test_splitter_sentences_bullets_headers_and_context():
    st = split_statements("# Rules\n\nUse X. Do not use X.\n\n- If prod, then A.\n  - nested B\n- C e.g. D. E")
    assert [(s.text, s.ctx) for s in st] == [
        ("Use X.", ("Rules",)), ("Do not use X.", ("Rules",)), ("If prod, then A.", ("Rules",)),
        ("nested B", ("Rules", "If prod, then A.")), ("C e.g. D. E", ("Rules",))]
    # the whole statement identity includes its context: the same sentence under another header differs
    a = split_statements("# One\nUse X.")[0]
    b = split_statements("# Two\nUse X.")[0]
    assert a.text == b.text and a.key() != b.key()


def test_identity_is_whitespace_folding_only_cm2_is_not_cm_squared():
    a, b = M(1, "Measure in cm\u00b2."), M(2, "Measure in cm2.")
    assert split_statements(a.content)[0].key() != split_statements(b.content)[0].key()
    e = merge_entry([1, 2], "Measure in cm2.", [row((1, 0), "duplicate", of=(2, 0)), row((2, 0), "kept", 0)])
    assert "not word for word equal" in vmerge(e, a, b)[1]
    e = merge_entry([1, 2], "Measure in cm\u00b2.", [row((1, 0), "kept", 0), row((2, 0), "duplicate", of=(1, 0))])
    assert "not word for word equal" in vmerge(e, a, b)[1]
    assert split_statements("a   b\n c")[0].text == "a b c"       # whitespace still folds


def test_scope_survives_lists_or_the_form_is_unsupported():
    scoped = "- Only in staging:\n\n  - Disable authentication."          # loose nested list
    with pytest.raises(Unsupported, match="loose nested"):
        split_statements(scoped)
    tight = split_statements("- Only in staging:\n  - Disable authentication.")
    assert tight[1].ctx == ("Only in staging:",)
    flat = split_statements("- Only in staging:\n- Disable authentication.")
    assert tight[1].key() != flat[1].key()
    lead = split_statements("Only in staging:\n\n- Disable authentication.\n- Skip tls.")
    assert [s.ctx for s in lead[1:]] == [("Only in staging:",)] * 2
    assert lead[1].key() != split_statements("- Disable authentication.")[0].key()
    assert split_statements("Rules:\n- a\n\n# New\n- b")[-1].ctx == ("New",)
    assert [s.text for s in split_statements("1. a\n2. b")] == ["1. a", "2. b"]
    # negative controls through the validators, with the inventories
    a = M(1, scoped.replace("\n\n", "\n"))
    b = M(2, "- Only in staging:\n- Disable authentication.", created="2026-02-01 00:00:00")
    inv = [(s.text, s.ctx) for s in split_statements(a.content)], [(s.text, s.ctx) for s in split_statements(b.content)]
    assert inv[0][1] == ("Disable authentication.", ("Only in staging:",))
    assert inv[1][1] == ("Disable authentication.", ())
    e = merge_entry([1, 2], b.content, [row((1, 0), "kept", 0), row((1, 1), "kept", 1),
                                        row((2, 0), "duplicate", of=(1, 0)), row((2, 1), "duplicate", of=(1, 1))])
    assert "word for word" in vmerge(e, a, b)[1]                                   # merge: not eligible
    red = {"id": 1, "by": 2, "why": "redundant", "replaces": []}
    _, p = tidy.validate_cluster_answer(ans(retire=[red], keep=keep(2)), ctx_of(a, b))
    assert "not redundant" in p                                                     # retire: not redundant
    loose_src = M(3, scoped)                                                        # the loose form itself
    _, p = tidy.validate_cluster_answer(ans(retire=[{**red, "id": 3}], keep=keep(2)), ctx_of(loose_src, b))
    assert "unsupported form" in p


SCOPE_CASES = {          # name -> (note, supported?)
    "loose nested bullet": ("- Only in staging:\n\n  - Disable authentication.", False),
    "continuation paragraph": ("- Only in staging:\n\n  Disable authentication.", False),
    "numbered continuation (3 spaces)": ("1. Only in staging:\n\n   Disable authentication.", False),
    "deep continuation (4 spaces)": ("- a\n  - Only in staging:\n\n    Disable authentication.", False),
    "lazy continuation": ("- Only in staging:\nDisable authentication.", False),
    "tight nested bullet": ("- Only in staging:\n  - Disable authentication.", True),
    "flat control": ("- Only in staging:\n- Disable authentication.", True),
    "unindented paragraph (new block)": ("- Only in staging:\n\nDisable authentication.", True),
    "list lead": ("Only in staging:\n\n- Disable authentication.", True),
}


def inventory(note):
    try:
        return [(s.text, s.ctx) for s in split_statements(note)]
    except Unsupported as e:
        return f"UNSUPPORTED({e})"


@pytest.mark.parametrize("name", sorted(SCOPE_CASES))
def test_every_scope_form_is_supported_with_its_scope_or_unsupported(name):
    note, supported = SCOPE_CASES[name]
    inv = inventory(note)
    print(f"\n{name!r}: {inv}")
    assert isinstance(inv, list) == supported
    flat = inventory("- Only in staging:\n- Disable authentication.")
    if supported and inv[-1][1]:                       # a supported SCOPED form never collapses into the flat one
        assert inv[-1] != flat[-1]
    if supported and not inv[-1][1]:                   # the unscoped forms are unscoped in the flat note too
        assert inv[-1] == flat[-1]


@pytest.mark.parametrize("name", [n for n, (_t, ok) in sorted(SCOPE_CASES.items()) if not ok])
def test_scope_changing_forms_can_never_be_eligible_merge_or_redundant_retire(name):
    note, _ = SCOPE_CASES[name]
    flat_text = "- Only in staging:\n- Disable authentication."
    a, a2, flat = M(1, note), M(2, note, created="2026-01-02 00:00:00"), M(3, flat_text, created="2026-02-01 00:00:00")
    # merge: two scoped copies -> the flattened output, with kept/duplicate rows claimed
    e = merge_entry([1, 2], flat_text, [row((1, 0), "kept", 0), row((1, 1), "kept", 1),
                                        row((2, 0), "duplicate", of=(1, 0)), row((2, 1), "duplicate", of=(1, 1))])
    step, problem = vmerge(e, a, a2)
    print(f"\nmerge[{name}]: step={step} problem={problem!r}")
    assert step is None and "unsupported form" in problem
    # redundant retire of the scoped source by a newer flattened note
    red = {"id": 1, "by": 3, "why": "redundant", "replaces": []}
    ok, problem = tidy.validate_cluster_answer(ans(retire=[red], keep=keep(2, 3)), ctx_of(a, a2, flat))
    print(f"retire[{name}]: ok={ok} problem={problem!r}")
    assert ok is None and "unsupported form" in problem


def test_scoped_supported_forms_are_not_redundant_against_the_flat_note():
    for name in ("tight nested bullet", "list lead"):
        note = SCOPE_CASES[name][0]
        a, flat = M(1, note), M(3, "- Only in staging:\n- Disable authentication.\nx", created="2026-02-01 00:00:00")
        flat = M(3, "- Only in staging:\n- Disable authentication.", created="2026-02-01 00:00:00")
        _, p = tidy.validate_cluster_answer(ans(retire=[{"id": 1, "by": 3, "why": "redundant", "replaces": []}],
                                                keep=keep(3)), ctx_of(a, flat))
        print(f"\nretire[{name}] vs flat: {p!r}")
        assert "not redundant" in p
    flat_a, flat_b = M(1, "- Only in staging:\n- Disable authentication."), M(2, "- Only in staging:\n- Disable authentication.")
    e = merge_entry([1, 2], flat_a.content, [row((1, 0), "kept", 0), row((1, 1), "kept", 1),
                                             row((2, 0), "duplicate", of=(1, 0)), row((2, 1), "duplicate", of=(1, 1))])
    step, problem = vmerge(e, flat_a, flat_b)
    print(f"merge[flat control]: status={step['status']} problem={problem!r}")
    assert problem is None and step["status"] == "eligible"           # the control: a true flat duplicate is eligible


@pytest.mark.parametrize("bad", ["```\ncode\n```", "| a | b |\n|---|---|", "<b>x</b>", "\tcode", "    indented code",
                                 "a\x00b", "x" * (tidy.NOTE_CAP + 1), "   ", "line\r\nline"])
def test_splitter_fails_closed_on_unsupported_forms(bad):
    with pytest.raises(Unsupported):
        split_statements(bad)


def test_unsupported_member_is_context_only_never_merged_or_retired():
    a, b = M(1, "```\nx\n```"), M(2, "Use X.")
    ctx = ctx_of(a, b)
    assert not ctx.hideable(1) and ctx.hideable(2)
    _, problem = tidy.validate_cluster_answer(ans(keep=keep(2), retire=[
        {"id": 1, "by": 2, "why": "redundant", "replaces": []}]), ctx)
    assert problem  # also has no primary disposition for 1 handled below
    _, problem = tidy.validate_cluster_answer(ans(retire=[{"id": 1, "by": 2, "why": "redundant", "replaces": []}],
                                                  keep=keep(2)), ctx)
    assert "unsupported form" in problem


# ----------------------------------------------------------------------------- validate_merge: worked table (a)


A1 = M(1, "Uploads require admin approval. Logs are kept 30 days.")
B1 = M(2, "Logs are kept 30 days.", created="2026-02-01 00:00:00")


def test_merge_exact_duplicate_is_eligible_and_charged_1_plus_2n():
    e = merge_entry([1, 2], "Uploads require admin approval. Logs are kept 30 days.", [
        row((1, 0), "kept", 0), row((1, 1), "kept", 1), row((2, 0), "duplicate", of=(1, 1))])
    step, problem = vmerge(e, A1, B1)
    assert problem is None
    assert step["status"] == "eligible" and not step["needs_approval"] and step["charge"] == 5
    assert set(step["expect"]) == {"1", "2"} and step["metadata"]["tidy"]["merged_from"] == [1, 2]


def test_use_x_vs_do_not_use_x_is_not_a_duplicate_by_containment():
    a, b = M(1, "Use X."), M(2, "Do not use X.")
    e = merge_entry([1, 2], "Do not use X.", [row((1, 0), "duplicate", of=(2, 0)), row((2, 0), "kept", 0)])
    _, problem = vmerge(e, a, b)
    assert "not word for word equal" in problem
    # even honestly labelled, dropping "Use X." is only ever HELD for approval, never auto-eligible
    e = merge_entry([1, 2], "Do not use X.", [row((1, 0), "dropped", why="quoted_history"), row((2, 0), "kept", 0)])
    step, problem = vmerge(e, a, b)
    assert problem is None and step["status"] == "held" and step["held_statements"] == ["1#0"]
    # and a cluster flagged as a contradiction can never be merged at all
    _, problem = tidy.validate_cluster_answer(ans(contradiction=[{"ids": [1, 2], "quotes": [
        {"id": 1, "quote": "Use X."}, {"id": 2, "quote": "Do not use X."}]}],
        merge=[{"sources": [1, 2], "reason": "r"}]), ctx_of(a, b))
    assert "two primary dispositions" in problem


def test_numeric_constraint_containment_is_not_a_duplicate():
    a, b = M(1, "Allow 30 days."), M(2, "Do not allow 30 days.")
    e = merge_entry([1, 2], "Do not allow 30 days.", [row((1, 0), "duplicate", of=(2, 0)), row((2, 0), "kept", 0)])
    assert "not word for word equal" in vmerge(e, a, b)[1]
    # the same with matching case, where plain substring containment WOULD hold
    a, b = M(1, "Allow 30 days."), M(2, "Never Allow 30 days.")
    assert "Allow 30 days." in "Never Allow 30 days."
    e = merge_entry([1, 2], "Never Allow 30 days.", [row((1, 0), "duplicate", of=(2, 0)), row((2, 0), "kept", 0)])
    assert "not word for word equal" in vmerge(e, a, b)[1]


def test_partial_quote_constraint_is_held_not_kept_and_never_eligible():
    a, b = M(1, "Uploads require admin approval."), M(2, "Logs are kept 30 days.")
    reworded = merge_entry([1, 2], "Uploads require approval. Logs are kept 30 days.", [
        row((1, 0), "reworded", 0, why="shorter"), row((2, 0), "kept", 1)])
    step, problem = vmerge(reworded, a, b)
    assert problem is None and step["status"] == "held" and step["held_statements"] == ["1#0"]
    # claiming it as `kept` is a mechanical REJECTION: a partial quote certifies nothing
    lying = merge_entry([1, 2], "Uploads require approval. Logs are kept 30 days.", [
        row((1, 0), "kept", 0), row((2, 0), "kept", 1)])
    assert "word for word" in vmerge(lying, a, b)[1]


def test_unmapped_and_invented_statements_are_rejected():
    full = [row((1, 0), "kept", 0), row((1, 1), "kept", 1), row((2, 0), "duplicate", of=(1, 1))]
    text = "Uploads require admin approval. Logs are kept 30 days."
    assert "unmapped_statement:1#1" in vmerge(merge_entry([1, 2], text, [full[0], full[2]]), A1, B1)[1]
    assert "assigned twice" in vmerge(merge_entry([1, 2], text, full + [full[0]]), A1, B1)[1]
    assert "not in the inventory" in vmerge(merge_entry([1, 2], text, full + [row((1, 9), "kept", 0)]), A1, B1)[1]
    invented = text + " Retries are capped at 3."
    assert "statement_without_source" in vmerge(merge_entry([1, 2], invented, full), A1, B1)[1]
    # an invented number in a reworded statement is rejected by the token guard even though it is "held"
    inv = merge_entry([1, 2], "Uploads require admin approval. Logs are kept 31 days.", [
        row((1, 0), "kept", 0), row((1, 1), "reworded", 1, why="x"), row((2, 0), "reworded", 1, why="x")])
    assert "invented_token" in vmerge(inv, A1, B1)[1]


def test_kept_twice_dropped_without_reason_and_duplicate_in_same_source_are_rejected():
    text = "Uploads require admin approval. Logs are kept 30 days."
    both = merge_entry([1, 2], text, [row((1, 0), "kept", 0), row((1, 1), "kept", 1), row((2, 0), "kept", 1)])
    assert "kept by two" in vmerge(both, A1, B1)[1]
    drop = merge_entry([1, 2], text, [row((1, 0), "kept", 0), row((1, 1), "kept", 1),
                                      row((2, 0), "dropped", why="looks redundant")])
    assert "no allowed reason" in vmerge(drop, A1, B1)[1]
    same = M(3, "Logs are kept 30 days. Logs are kept 30 days.")
    e = merge_entry([2, 3], "Logs are kept 30 days.", [row((2, 0), "kept", 0), row((3, 0), "duplicate", of=(2, 0)),
                                                       row((3, 1), "duplicate", of=(3, 0))])
    assert vmerge(e, B1, same)[1]


def test_merge_shape_size_envelope_and_unsupported_text():
    full = [row((1, 0), "kept", 0), row((1, 1), "kept", 1), row((2, 0), "duplicate", of=(1, 1))]
    text = "Uploads require admin approval. Logs are kept 30 days."
    assert "wrong shape" in vmerge({"sources": [1, 2]}, A1, B1)[1]
    assert "wrong shape" in vmerge(merge_entry([1, 3], text, full), A1, B1)[1]
    assert "unsupported form" in vmerge(merge_entry([1, 2], "```\n" + text + "\n```", full), A1, B1)[1]
    assert "envelope" in vmerge(merge_entry([1, 2], text + " <<<", full), A1, B1)[1]
    short = merge_entry([1, 2], "Uploads.", [row((1, 0), "reworded", 0, why="x"), row((1, 1), "dropped", why="quoted_history"),
                                            row((2, 0), "dropped", why="quoted_history")])
    assert "much shorter" in vmerge(short, A1, B1)[1]


def test_merge_metadata_policy():
    a = M(1, "Use X.", meta={"section": "s", "status": "open", "kind": "note"})
    b = M(2, "Use X.", meta={"section": "s", "status": "closed"})
    e = merge_entry([1, 2], "Use X.", [row((1, 0), "kept", 0), row((2, 0), "duplicate", of=(1, 0))])
    step, problem = vmerge(e, a, b)
    # a differing scalar is NOT unioned: not copied, recorded, and the step needs approval
    assert problem is None and "status" not in step["metadata"] and step["metadata"]["kind"] == "note"
    assert step["metadata"]["tidy"]["conflicts"]["status"] == {"1": "open", "2": "closed"}
    assert step["needs_approval"] and step["status"] == "held"
    c = M(3, "Use X.", meta={"section": "other"})
    assert "metadata.section" in vmerge(merge_entry([1, 3], "Use X.", [row((1, 0), "kept", 0),
                                                                        row((3, 0), "duplicate", of=(1, 0))]), a, c)[1]
    d = M(4, "Use X.", meta={"section": "s", "kind": "other"})
    assert "metadata.kind" in vmerge(merge_entry([1, 4], "Use X.", [row((1, 0), "kept", 0),
                                                                     row((4, 0), "duplicate", of=(1, 0))]), a, d)[1]


# ----------------------------------------------------------------------------- cluster answers (3.2, 3.3, 5)


def test_dispositions_are_exactly_once_and_complete():
    a, b, c = M(1, "Use X."), M(2, "Use Y."), M(3, "Use Z.")
    ctx = ctx_of(a, b, c)
    assert tidy.validate_cluster_answer(ans(keep=keep(1, 2, 3)), ctx)[1] is None
    assert "without a primary disposition" in tidy.validate_cluster_answer(ans(keep=keep(1, 2)), ctx)[1]
    assert "two primary" in tidy.validate_cluster_answer(ans(keep=keep(1, 2, 3, 3)), ctx)[1]
    assert "unknown member" in tidy.validate_cluster_answer(ans(keep=keep(1, 2, 3, 9)), ctx)[1]
    assert "wrong shape" in tidy.validate_cluster_answer({"keep": []}, ctx)[1]
    assert tidy.validate_cluster_answer(ans(keep=keep(1, 2, 3), link=[{"a": 1, "b": 2, "edge_type": "related_to",
                                                                        "reason": "r"}]), ctx)[1] == "bad link row"
    assert tidy.validate_cluster_answer(ans(keep=keep(1, 2, 3), extra=[]), ctx)[1] == "wrong shape"


def test_protected_members_are_never_merged_or_retired():
    for prot in ({"kind": "item-record"}, {"kind": "decision"}, {"kind": "procedure"}, {"kind": "digest"}):
        a, b = M(1, "Use X.", meta=prot), M(2, "Use X. ")
        _, p = tidy.validate_cluster_answer(ans(merge=[{"sources": [1, 2], "reason": "r"}]), ctx_of(a, b))
        assert "protected" in p, prot
        _, p = tidy.validate_cluster_answer(ans(retire=[{"id": 1, "by": 2, "why": "redundant", "replaces": []}],
                                                keep=keep(2)), ctx_of(a, b))
        assert "protected" in p, prot
    d = M(1, "Use X.", tags=["decision"])
    assert "protected" in tidy.validate_cluster_answer(ans(merge=[{"sources": [1, 2], "reason": "r"}]),
                                                       ctx_of(d, M(2, "Use X.")))[1]
    assert tidy.is_protected(M(1, "x", tags=["item-record"]))


def test_retire_needs_demonstrated_redundancy_or_approved_obsolescence():
    old = M(1, "Use X. Port is 8000.", created="2026-01-01 00:00:00")
    new = M(2, "Use X. Port is 8080. Logs are kept.", created="2026-03-01 00:00:00")
    ctx = ctx_of(old, new)
    red = {"id": 1, "by": 2, "why": "redundant", "replaces": []}
    # not word for word in `by` (Port 8000 vs 8080): a shared reference or topic is never enough
    assert "not redundant" in tidy.validate_cluster_answer(ans(retire=[red], keep=keep(2)), ctx)[1]
    dup_old = M(1, "Use X.", created="2026-01-01 00:00:00")
    ok, p = tidy.validate_cluster_answer(ans(retire=[red], keep=keep(2)), ctx_of(dup_old, new))
    assert p is None
    obs = {"id": 1, "by": 2, "why": "obsolete", "replaces": [{"old": 1, "new": 1}]}
    assert tidy.validate_cluster_answer(ans(retire=[obs], keep=keep(2)), ctx)[1] is None
    assert "needs replaces" in tidy.validate_cluster_answer(ans(retire=[{**obs, "replaces": []}], keep=keep(2)), ctx)[1]
    assert "does not exist" in tidy.validate_cluster_answer(
        ans(retire=[{**obs, "replaces": [{"old": 7, "new": 1}]}], keep=keep(2)), ctx)[1]
    # the target must be NEWER, live, and not itself hidden or contradicted
    assert "not newer" in tidy.validate_cluster_answer(
        ans(retire=[{"id": 2, "by": 1, "why": "obsolete", "replaces": [{"old": 0, "new": 0}]}], keep=keep(1)), ctx)[1]
    c = M(3, "Use X.", created="2026-04-01 00:00:00")
    chain = [{"id": 1, "by": 2, "why": "redundant", "replaces": []}, {"id": 2, "by": 3, "why": "redundant", "replaces": []}]
    assert "hidden" in tidy.validate_cluster_answer(ans(retire=chain, keep=keep(3)), ctx_of(dup_old, M(2, "Use X.", created="2026-03-01 00:00:00"), c))[1]


def test_contradictions_are_flag_only_and_need_verbatim_quotes():
    a, b = M(1, "Use X."), M(2, "Do not use X.")
    ctx = ctx_of(a, b)
    good = ans(contradiction=[{"ids": [1, 2], "quotes": [{"id": 1, "quote": "Use X."}, {"id": 2, "quote": "not use X"}]}])
    assert tidy.validate_cluster_answer(good, ctx)[1] is None
    bad = ans(contradiction=[{"ids": [1, 2], "quotes": [{"id": 1, "quote": "Use X!"}, {"id": 2, "quote": "not use X"}]}])
    assert "not verbatim" in tidy.validate_cluster_answer(bad, ctx)[1]


def test_retag_stays_in_vocabulary_and_protected_is_add_only():
    a = M(1, "Use X.", tags=["misc", "clmux"])
    ctx = ctx_of(a, M(2, "y"), vocab={"clmux", "clmux/x", "misc"})
    ok = {"id": 1, "add": ["clmux/x"], "remove": ["misc"], "reason": "r"}
    assert tidy.validate_retag(ok, ctx) is None
    assert "vocabulary" in tidy.validate_retag({**ok, "add": ["brand-new"]}, ctx)
    assert "not on memory" in tidy.validate_retag({**ok, "remove": ["nope"]}, ctx)
    assert "empty" in tidy.validate_retag({**ok, "add": [], "remove": []}, ctx)
    p = M(1, "Use X.", tags=["misc"], meta={"kind": "decision"})
    assert "adding only" in tidy.validate_retag(ok, ctx_of(p, M(2, "y"), vocab={"clmux/x", "misc"}))
    assert tidy.validate_retag({**ok, "remove": []}, ctx_of(p, M(2, "y"), vocab={"clmux/x", "misc"})) is None
    u = M(1, "```\nx\n```", tags=["misc"])                                        # context-only: add only
    assert "adding only" in tidy.validate_retag(ok, ctx_of(u, M(2, "y"), vocab={"clmux/x", "misc"}))
    assert tidy.validate_retag({**ok, "remove": []}, ctx_of(u, M(2, "y"), vocab={"clmux/x", "misc"})) is None


# ----------------------------------------------------------------------------- grouping (2)


def test_related_to_is_never_evidence_but_typed_edges_cosine_and_shared_references_are():
    a = M(1, "alpha Item 44", refs=[{"id": 2, "score": 1.0, "edge_type": "related_to"}])
    b = M(2, "beta unrelated words")
    # absorb's hardcoded 1.0 related_to (m453) and a stored 0.99 related_to carry no weight
    edges = tidy.build_evidence([a, b], {}, LIM, "clmux")
    assert edges == {}
    c = M(3, "gamma", refs=[{"id": 2, "score": 0.99, "edge_type": "related_to"}])
    assert tidy.build_evidence([b, c], {}, LIM, "clmux") == {}
    typed = M(3, "gamma", refs=[{"id": 2, "score": 1.0, "edge_type": "extends"}])
    assert tidy.build_evidence([b, typed], {}, LIM, "clmux")[(2, 3)]["w"] == 2.0
    emb = {1: {"x": 1.0, "y": 0.1}, 2: {"x": 1.0, "y": 0.2}, 3: {"z": 1.0}}
    e = tidy.build_evidence([M(1, "a"), M(2, "b"), M(3, "c")], emb, LIM, "clmux")
    assert set(e) == {(1, 2)} and e[(1, 2)]["w"] == 1.0
    ref = tidy.build_evidence([M(1, "see Item 44 and Item 45"), M(2, "Item 44, Item 45 again")], {}, LIM, "clmux")
    assert ref[(1, 2)]["w"] == 2.0
    gen = [M(i, "Item 44") for i in range(1, 2 + tidy.GENERIC_REF_HOLDERS)]
    assert tidy.build_evidence(gen, {}, LIM, "clmux") == {}      # too generic to group by


def test_clusters_join_at_weight_two_split_to_size_and_skip_singletons():
    mems = [M(i, f"n{i}") for i in range(1, 8)]
    edges = {(1, 2): {"w": 2.0, "why": ["a"]}, (2, 3): {"w": 1.0, "why": ["b"]}, (4, 5): {"w": 2.5, "why": ["c"]}}
    cl, singles = tidy.make_clusters(mems, edges, LIM)
    assert [c["members"] for c in cl] == [[4, 5], [1, 2]] and singles == 3
    assert cl[0]["id"] == "c1" and cl[0]["weight"] == 2.5
    big = [M(i, f"n{i}") for i in range(1, 16)]
    chain = {(i, i + 1): {"w": 2.0 + (0.1 if i == 7 else 0), "why": ["x"]} for i in range(1, 15)}
    cl, singles = tidy.make_clusters(big, chain, TidyLimits(max_cluster=8))
    assert all(2 <= len(c["members"]) <= 8 for c in cl) and sum(len(c["members"]) for c in cl) + singles == 15


def test_digest_memories_and_other_projects_are_not_clustered_and_retired_are_out():
    cfg = TidyConfig(limits=TidyLimits())
    mems = [M(1, "x Item 44 Item 45"), M(2, "y Item 44 Item 45"), M(3, "z Item 44 Item 45", project="other"),
            M(4, "d Item 44 Item 45", meta={"kind": "digest"}), M(5, "r Item 44 Item 45")]
    rep = tidy.run_tidy(mems, {}, {5}, cfg, "clmux", lambda p, timeout=None: json.dumps(ans(keep=keep(1, 2))), None,
                        now=1.0)
    assert [c["members"] for c in rep["clusters"]] == []     # judge_write None defers; cluster listing below
    assert rep["counts"]["live"] == 3 and rep["counts"]["clusterable"] == 2
    assert rep["deferred_clusters"] == ["c1"]


# ----------------------------------------------------------------------------- the whole run with a fake judge


def world():
    return [M(1, "Uploads require admin approval. Logs are kept 30 days. See Item 44.", created="2026-01-01 00:00:00"),
            M(2, "Logs are kept 30 days. See Item 44.", created="2026-02-01 00:00:00"),
            M(3, "Retries are capped at 3. See Item 44.", meta={"kind": "item-record"}, tags=["item-record"],
              created="2026-03-01 00:00:00")]


EMB = {1: {"a": 1.0}, 2: {"a": 1.0}, 3: {"a": 1.0}}
MERGED = "Uploads require admin approval. Logs are kept 30 days. See Item 44."


class Fake:
    def __init__(self, merged=MERGED, digest=True, group=None):
        self.calls = []
        self.merged, self.digest, self.group_ans = merged, digest, group

    def group(self, prompt, timeout=None):
        self.calls.append("group")
        if self.group_ans is not None:
            return self.group_ans(prompt) if callable(self.group_ans) else self.group_ans
        return json.dumps(ans(merge=[{"sources": [1, 2], "reason": "same facts"}], keep=keep(3)))

    def write(self, prompt, timeout=None):
        task = prompt.split("\n", 1)[0]
        self.calls.append(task)
        if task == "TASK: merge":
            return json.dumps({"merges": [merge_entry([1, 2], self.merged, [
                row((1, 0), "kept", 0), row((1, 1), "kept", 1), row((1, 2), "kept", 2),
                row((2, 0), "duplicate", of=(1, 1)), row((2, 1), "duplicate", of=(1, 2))])]})
        notes = data_of(prompt)["notes"]
        by = {n["cite"]: n for n in notes}
        lines = {"decisions": [], "followups": [], "constraints": []}
        if 3 in by:
            lines["decisions"].append({"text": "Retries are capped at 3.", "cites": [3], "quote": "Retries are capped at 3."})
        if "@c1" in by:
            lines["constraints"].append({"text": "Logs are kept 30 days.", "cites": ["@c1"], "quote": "Logs are kept 30 days."})
        return json.dumps(lines)


def run(fake, mems=None, **lim):
    cfg = TidyConfig(limits=TidyLimits(**lim))
    return tidy.run_tidy(mems or world(), EMB, set(), cfg, "clmux", fake.group, fake.write, now=1_700_000_000.0,
                         models={"group": "haiku", "write": "sonnet"})


def test_full_run_plans_a_merge_a_digest_with_symbolic_ids_and_seals_it():
    fake = Fake()
    rep = run(fake)
    assert fake.calls == ["group", "TASK: merge", "TASK: digest"]
    assert rep["judge"]["calls"] == {"group": 1, "write": 2, "total": 3}
    merge, digest = rep["steps"]
    assert merge["op"] == "merge" and merge["create"] == "@c1" and merge["charge"] == 5 and merge["status"] == "eligible"
    assert digest["op"] == "bootstrap_digest" and digest["create"] == "@d1" and digest["depends_on"] == [merge["id"]]
    assert "[@c1]" in digest["body"] and "[m3]" in digest["body"] and digest["cites"] == [3, "@c1"]
    assert digest["metadata"]["kind"] == "digest" and digest["tags"] == ["clmux/digest"]
    assert rep["counts"]["charge"] == 6 and rep["digest_input"]["included"] == 2
    assert rep["version"] == "tidy-1" and rep["mode"] == "dry-run"
    assert rep["digest"] == tidy.plan_digest(rep)
    # the protected item-record stayed: it is context, kept, and in no hiding step
    assert all(3 not in s.get("sources", []) and s.get("id_hidden") != 3 for s in rep["steps"])
    assert rep["clusters"][0]["keep"] == [3] and "steps" in rep and rep["sources"]["1"]["statements"][0]["span"]


def test_rejected_merge_falls_back_to_keep_and_hides_nothing():
    rep = run(Fake(merged="Uploads require approval. Logs are kept 30 days. See Item 44."))
    assert [s["op"] for s in rep["steps"]] == ["bootstrap_digest"] or all(s["op"] != "merge" for s in rep["steps"])
    cl = rep["clusters"][0]
    assert cl["proposals_rejected"] and cl["merge_failed_kept"] == [1, 2]


def test_a_cluster_answer_that_deviates_rejects_the_whole_cluster():
    rep = run(Fake(group=json.dumps(ans(keep=keep(1, 2)))))                    # member 3 has no disposition
    assert rep["clusters"][0]["status"] == "rejected" and not [s for s in rep["steps"] if s["op"] != "bootstrap_digest"]
    assert rep["rejected"][0]["op"] == "cluster"
    rep = run(Fake(group="not json"))
    assert rep["clusters"][0]["problem"] == "judge answer is not JSON"

    def boom(prompt, timeout=None):
        raise RuntimeError("down")
    cfg = TidyConfig(limits=TidyLimits())
    rep = tidy.run_tidy(world(), EMB, set(), cfg, "clmux", boom, boom, now=1.0)
    assert rep["clusters"][0]["problem"] == "judge failed: RuntimeError"


def test_call_budget_reserves_the_digest_call_first():
    fake = Fake()
    rep = run(fake, max_llm_calls=2)
    # the cluster may use cap-1 = 1 call: no merge call, but the digest still gets its reserved call
    assert fake.calls == ["group", "TASK: digest"]
    assert rep["judge"]["calls"]["total"] == 2 and rep["stopped_by"] == "max_llm_calls"
    assert any(r["reason"] == "call budget" for r in rep["clusters"][0]["proposals_rejected"])
    assert rep["steps"][-1]["op"] == "bootstrap_digest"
    fake = Fake()
    rep = run(fake, max_llm_calls=1)
    # cap 1: no cluster call at all (cap - 1 = 0), the one reserved call still goes to the digest
    assert fake.calls == ["TASK: digest"] and rep["deferred_clusters"] == ["c1"]


def test_caps_defer_steps_and_the_write_charge_is_pruned_before_sealing():
    rep = run(Fake(), max_merges=0)
    assert {"cluster": "c1", "op": "merge", "reason": "cap:max_merges"} in rep["deferred"]
    assert all(s["op"] != "merge" for s in rep["steps"])
    rep = run(Fake(), max_writes=5)          # merge 5 + digest 1 > 5: the merge is deferred, the digest is kept
    assert {"cluster": "c1", "op": "merge", "reason": "cap:max_writes"} in rep["deferred"]
    assert rep["counts"]["charge"] <= 5 and rep["steps"][-1]["op"] == "bootstrap_digest"
    rep = run(Fake(), max_clusters=0)
    assert rep["deferred_clusters"] == ["c1"] and rep["stopped_by"] == "max_clusters"


def test_the_digest_charge_never_exceeds_max_writes():
    for cap, charge in ((0, 1), (1, 3)):
        mems = world() + ([] if charge == 1 else [M(801, "d", meta={"kind": "digest", "digest_of": "tidy"})])
        rep = run(Fake(), mems, max_writes=cap)
        assert rep["counts"]["charge"] <= cap and all(s["op"] not in ("bootstrap_digest", "replace_digest")
                                                      for s in rep["steps"])
        assert {"cluster": None, "op": "bootstrap_digest" if charge == 1 else "replace_digest",
                "reason": "cap:max_writes"} in rep["deferred"]
    dg = [M(801, "d", meta={"kind": "digest", "digest_of": "tidy"}),
          M(905, "d", meta={"kind": "digest", "digest_of": "tidy"}, created="2026-02-01 00:00:00")]
    rep = run(Fake(), world() + dg, max_writes=1)                     # repair costs 2
    assert all(s["op"] != "repair_digest" for s in rep["steps"]) and rep["counts"]["charge"] <= 1
    rep = run(Fake(), world() + dg, max_writes=2)
    assert rep["steps"][-1]["op"] == "repair_digest" and rep["counts"]["charge"] <= 2


def test_judge_unavailable_makes_a_report_with_no_proposals():
    cfg = TidyConfig(limits=TidyLimits())
    rep = tidy.run_tidy(world(), EMB, set(), cfg, "clmux", None, None, now=1.0, judge_note="unavailable: x")
    assert rep["steps"] == [] and rep["judge"]["status"] == "unavailable: x" and rep["deferred_clusters"] == ["c1"]


def test_references_checked_makes_review_candidates_never_a_retire():
    mems = world()
    mems[1].metadata["references_checked"] = {"summary": "missing", "found": 0, "missing": 2}
    rep = run(Fake(), mems)
    assert {"kind": "references", "id": 2, "references_checked": "missing",
            "counts": {"found": 0, "missing": 2, "stale": None, "unknown": None}} in rep["review"]
    assert all(s["op"] != "retire" for s in rep["steps"])
    prompt_seen = []
    f = Fake(group=lambda p: (prompt_seen.append(p), json.dumps(ans(keep=keep(1, 2, 3))))[1])
    run(f, mems)
    assert any(m["references_checked"] == "missing" for m in data_of(prompt_seen[0])["members"])


# ----------------------------------------------------------------------------- the digest (6)


def test_digest_lines_need_a_cited_note_a_verbatim_quote_and_no_invented_token():
    rows = [{"cite": 1, "text": "Port is 8080 in `serve.py`."}, {"cite": "@c1", "text": "Merged note."}]
    good = {"decisions": [{"text": "Port is 8080.", "cites": [1], "quote": "Port is 8080"}], "followups": [],
            "constraints": [{"text": "Merged note.", "cites": ["@c1"], "quote": "Merged"}]}
    content, rej, problem = tidy.validate_digest(good, rows, LIM)
    assert problem is None and rej == [] and tidy.render_digest(content["sections"]).splitlines() == [
        "## Current decisions", "- Port is 8080. [m1]", "", "## Constraints / pitfalls", "- Merged note. [@c1]"]
    bad = {"decisions": [{"text": "Port is 9090.", "cites": [1], "quote": "Port is 8080"},      # invented number
                         {"text": "Port is 8080.", "cites": [2], "quote": "Port is 8080"},      # not in the input
                         {"text": "Port is 8080.", "cites": [1], "quote": "Port was 8080"},     # quote not verbatim
                         {"text": "Port is 8080.", "cites": [], "quote": "Port is 8080"},       # no id
                         {"text": "Port is 8080.", "cites": [1], "quote": "Port is 8080"}],
           "followups": [], "constraints": []}
    content, rej, problem = tidy.validate_digest(bad, rows, LIM)
    assert len(rej) == 4 and len(content["sections"]["decisions"]) == 1
    assert tidy.validate_digest({"decisions": []}, rows, LIM)[2] == "wrong shape"
    only_bad = dict(bad, decisions=bad["decisions"][:4])
    assert tidy.validate_digest(only_bad, rows, LIM)[2] == "no valid line"
    many = {"decisions": [{"text": f"Port is 8080 n{i}.", "cites": [1], "quote": "Port is 8080"} for i in range(5)],
            "followups": [], "constraints": []}
    c, r, _ = tidy.validate_digest(many, rows, TidyLimits(digest_max_lines=3))
    assert len(c["sections"]["decisions"]) == 3 and len(r) == 2


def test_digest_input_is_bounded_ranked_and_reports_its_coverage():
    now = 1_800_000_000.0
    old = "2020-01-01 00:00:00"
    mems = [M(1, "old note", created=old), M(2, "decision note", created=old, meta={"kind": "decision"}),
            M(3, "todo note", created=old, meta={"type": "todo"}), M(4, "read a lot", created=old),
            M(5, "recent note", created=tidy.dream._iso(now - 86400).replace("T", " ").replace("Z", "")),
            M(6, "gone", created=old), M(7, "prior digest", meta={"kind": "digest"})]
    steps = [{"op": "retire", "id_hidden": 6}]
    rows, cov = tidy.digest_candidates(mems, steps, {4: 9}, TidyLimits(digest_max_memories=4,
                                                                       digest_recent_share=0.0), now)
    assert [r["cite"] for r in rows] == [2, 3, 5, 4]       # protected, open task, recent, then most read
    assert cov["considered"] == 5 and cov["included"] == 4 and cov["excluded"] == 1
    assert cov["chars"] == sum(len(r["text"]) for r in rows) and cov["recent_included"] == 0
    rows, cov = tidy.digest_candidates(mems, steps, {}, TidyLimits(digest_max_chars=20), now)
    assert cov["chars"] <= 20 and cov["excluded"] == cov["considered"] - cov["included"]


def test_digest_state_machine_bootstrap_replace_repair_review():
    def dg(i, by="tidy", t="2026-01-01 00:00:00", proj="clmux"):
        return M(i, "digest", meta={"kind": "digest", "digest_of": by}, created=t, project=proj)
    assert tidy.digest_state([]) == ("bootstrap_digest", None)
    assert tidy.digest_state([dg(801)]) == ("replace_digest", {"old": 801})
    op, info = tidy.digest_state([dg(801), dg(905, t="2026-02-01 00:00:00")])
    assert op == "repair_digest" and info["winner"].id == 905 and info["loser"].id == 801
    assert tidy.digest_state([dg(801), dg(905, by="hand")])[0] == "review"
    assert tidy.digest_state([dg(1), dg(2), dg(3)])[0] == "review"
    # end to end: two tidy-made digests -> a repair step that creates nothing (charge 2) and no digest call
    mems = world() + [dg(801), dg(905, t="2026-02-01 00:00:00")]
    fake = Fake()
    rep = run(fake, mems)
    rs = rep["steps"][-1]
    assert rs["op"] == "repair_digest" and rs["charge"] == 2 and rs["winner"] == 905 and rs["supersedes"] == 801
    assert "body" not in rs and "TASK: digest" not in fake.calls and set(rs["expect"]) == {"801", "905"}
    rep = run(Fake(), world() + [dg(801), dg(905, by="hand")])
    assert rep["review"][0]["kind"] == "digests" and rep["steps"][-1]["op"] != "repair_digest"
    # one prior digest -> replace_digest that supersedes it, charge 3, expect pinned
    rep = run(Fake(), world() + [dg(801)])
    rd = rep["steps"][-1]
    assert rd["op"] == "replace_digest" and rd["supersedes"] == 801 and rd["charge"] == 3 and "801" in rd["expect"]
    assert rep["counts"]["charge"] == 5 + 3


# ----------------------------------------------------------------------------- symbolic ids and apply-time hooks


def test_symbol_substitution_is_token_aware():
    text = "see @c3, @c30 and @c3. also @c300x [m1]"
    assert tidy.substitute_symbols(text, {"@c3": 41, "@c30": 42, "@c300": 43}) == "see 41, 42 and 41. also 43x [m1]"
    with pytest.raises(TidyError):
        tidy.substitute_symbols("uses @c4", {"@c3": 1})
    assert tidy.symbols_in("@c3 @c30 @d1 @c3") == ["@c3", "@c30", "@d1"]


def look(mems, retired=()):
    by = {m.id: m for m in mems}
    return lambda i: (by[i], i in retired) if i in by else None


def test_revalidate_step_checks_every_expectation_citation_and_dependency():
    mems = [M(1, "a"), M(2, "b", meta={"kind": "decision"}), M(3, "c", project="other"), M(7, "seven")]
    step = {"op": "merge", "expect": {"1": mems[0].state_sha}, "depends_on": ["s1"], "cites": [1, "@c1"],
            "body": "x @c1 @c2", "create": "@c9"}
    assert tidy.revalidate_step(step, look(mems), {"@c1": 7}, ["s1"], "clmux") == ["symbol_not_committed:@c2"]
    got = tidy.revalidate_step(step, look(mems), {}, [], "clmux")
    assert "dependency_not_applied:s1" in got and "symbol_not_committed:@c1" in got
    changed = dict(step, expect={"1": "0" * 24, "2": mems[1].state_sha, "9": "x"})
    got = tidy.revalidate_step(changed, look(mems, retired={1}), {"@c1": 7, "@c2": 8}, ["s1"], "clmux")
    assert {"changed:1", "retired:1", "protected:2", "missing:9"} <= set(got)
    cites = dict(step, cites=[1, 3, 9])
    got = tidy.revalidate_step(cites, look(mems, retired={1}), {"@c1": 7, "@c2": 8}, ["s1"], "clmux")
    assert {"cited_missing_or_retired:1", "cited_other_project:3", "cited_missing_or_retired:9"} <= set(got)
    # a RESOLVED symbolic citation is looked up like a numeric one: committed is not the same as live
    sym = dict(step, cites=["@c1"], body="", depends_on=[], expect={})
    assert tidy.revalidate_step(sym, look(mems), {"@c1": 7}, [], "clmux") == []                       # valid control
    assert tidy.revalidate_step(sym, look(mems), {"@c1": 99}, [], "clmux") == ["cited_missing_or_retired:@c1"]
    assert tidy.revalidate_step(sym, look(mems, retired={7}), {"@c1": 7}, [], "clmux") == ["cited_missing_or_retired:@c1"]
    assert tidy.revalidate_step(sym, look(mems), {"@c1": 3}, [], "clmux") == ["cited_other_project:@c1"]
    dg = {"op": "replace_digest", "expect": {"2": mems[1].state_sha}, "depends_on": [], "cites": [], "body": ""}
    assert tidy.revalidate_step(dg, look(mems), {}, [], "clmux") == ["not_a_project_digest:2"]


def test_edge_phases_have_their_own_expected_state():
    step = {"op": "replace_digest", "expect": {"801": "sha801"}}
    pre = {"target_id": 801, "target_sha": "sha801", "new_id": 905, "new_exists": True, "forward_edge": False,
           "superseders": [], "live_digests": [801, 905]}
    assert tidy.check_edge_phase("forward", step, pre) is None
    post = dict(pre, forward_edge=True, superseders=[905], live_digests=[905])
    assert tidy.check_edge_phase("reverse", step, post) is None
    # the reverse write must NOT be judged by the forward precondition, and vice versa
    assert tidy.check_edge_phase("forward", step, post) is not None
    assert "forward edge is not committed" in tidy.check_edge_phase("reverse", step, pre)
    assert "not only by" in tidy.check_edge_phase("reverse", step, dict(post, superseders=[905, 7]))
    assert "exactly the winning digest" in tidy.check_edge_phase("reverse", step, dict(post, live_digests=[801, 905]))
    assert "judged row" in tidy.check_edge_phase("forward", step, dict(pre, target_sha="other"))
    assert "not committed" in tidy.check_edge_phase("forward", step, dict(pre, new_exists=False))
    assert "sealed against" in tidy.check_edge_phase("forward", step, dict(pre, live_digests=[801]))
    repair = {"op": "repair_digest", "expect": {"801": "sha801", "905": "s905"}, "both": [801, 905]}
    rp = dict(pre, live_digests=[905, 801])
    assert tidy.check_edge_phase("forward", repair, rp) is None
    assert tidy.check_edge_phase("reverse", repair, dict(rp, forward_edge=True, superseders=[905],
                                                        live_digests=[905])) is None
    assert tidy.check_edge_phase("sideways", step, pre) == "unknown phase"


# ----------------------------------------------------------------------------- the seal


def test_load_tidy_plan_checks_version_mode_digest_and_age(tmp_path):
    rep = run(Fake())
    p = tmp_path / "r.json"
    dream.write_report(str(p), rep)
    assert tidy.load_tidy_plan(str(p), now=rep["created_ts"] + 10)["run"] == rep["run"]
    with pytest.raises(TidyError, match="48|old"):
        tidy.load_tidy_plan(str(p), now=rep["created_ts"] + 49 * 3600)
    for key, val, msg in (("version", "tidy-0", "not a tidy"), ("mode", "apply", "not a tidy")):
        q = tmp_path / f"{key}.json"
        q.write_text(json.dumps({**rep, key: val}))
        with pytest.raises(TidyError, match=msg):
            tidy.load_tidy_plan(str(q), now=rep["created_ts"])
    edited = json.loads(p.read_text())
    edited["steps"][0]["body"] += " extra"
    q = tmp_path / "edited.json"
    q.write_text(json.dumps(edited))
    with pytest.raises(TidyError, match="digest mismatch"):
        tidy.load_tidy_plan(str(q), now=rep["created_ts"])
    assert oct(p.stat().st_mode & 0o777) == "0o600"


# ----------------------------------------------------------------------------- config, judges, CLI: no write path


def test_config_and_judges(tmp_path, monkeypatch):
    cp = tmp_path / "t.json"
    cp.write_text(json.dumps({"project": "clmux", "models": {"group": "haiku", "write": "sonnet"},
                              "limits": {"max_merges": 3}}))
    cfg = TidyConfig.load(str(cp))
    assert cfg.limits.max_merges == 3 and cfg.models == {"group": "haiku", "write": "sonnet"}
    assert str(cfg.state_dir).endswith("memora-dream")        # the lock is shared with dream by default
    for bad in ({"limits": {"nope": 1}}, {"limits": {"max_merges": -1}}, {"models": {"x": "y"}}, {"models": {"group": ""}}):
        cp.write_text(json.dumps(bad))
        with pytest.raises(TidyError):
            TidyConfig.load(str(cp))
    with pytest.raises(TidyError, match="no tidy config"):
        TidyConfig.load(str(tmp_path / "missing.json"))
    with pytest.raises(dream.JudgeError, match="models.write"):
        tidy.make_judges({"group": "haiku"}, 10)
    monkeypatch.setenv("MEMORA_DREAM_CLAUDE_BIN", sys.executable)
    g, w = tidy.make_judges({"group": "haiku", "write": "sonnet"}, 10)
    argv = g._argv()
    assert (g.model, w.model) == ("haiku", "sonnet") and argv[argv.index("--system-prompt") + 1] == tidy.TIDY_SYSTEM
    assert "--safe-mode" in argv and "--setting-sources" in argv and isinstance(g, dream.ClaudeCodeJudge)


def test_there_is_no_apply_flag_and_no_write_path_in_the_module(tmp_path):
    with pytest.raises(SystemExit) as e:
        tidy.main(["--apply"])
    assert e.value.code == 2
    src = (ROOT / "memora" / "tidy.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for forbidden in (r"add_memory", r"update_memory", r"delete_memory", r"link_memories", r"\bsupersede\b",
                      r"\.commit\(", r"INSERT ", r"UPDATE ", r"DELETE ", r"sqlite3\.connect", r"connect_without_schema",
                      r"storage\.connect\(", r"add_argument\(.--apply", r"apply_plan"):
        assert not re.search(forbidden, code), forbidden
    assert "open_readonly_file" in src


def test_unit_and_timer_are_dry_run_only_and_weekly():
    svc = (ROOT / "scripts" / "tidy" / "memora-tidy.service").read_text()
    tmr = (ROOT / "scripts" / "tidy" / "memora-tidy.timer").read_text()
    assert "--apply" not in svc and "--apply" not in tmr
    assert "memora.tidy" in svc and "--db" in svc and "--out-dir" in svc and "Nice=10" in svc
    assert re.search(r"^OnCalendar=Sun ", tmr, re.M)
    ex = json.loads((ROOT / "scripts" / "tidy" / "tidy.json.example").read_text())
    assert set(ex["models"]) == {"group", "write"} and TidyLimits.from_dict(ex["limits"])


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from memora.backends import LocalSQLiteBackend
    import memora
    backend = LocalSQLiteBackend(tmp_path / "memories.db")
    monkeypatch.setattr(storage, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(storage, "EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(memora, "TAG_WHITELIST", set())
    with storage.connect() as conn:
        conn.commit()
    return tmp_path / "memories.db"


def _add(content, **meta):
    with storage.connect() as conn:
        r = storage.add_memory(conn, content=content, tags=["clmux"], metadata={"project": "clmux", **meta})
        conn.commit()
    return r["id"] if "id" in r else r["memory"]["id"]


def _state():
    with storage.connect() as conn:
        rows = conn.execute("SELECT id, content, metadata, tags, updated_at FROM memories ORDER BY id").fetchall()
        refs = conn.execute("SELECT memory_id, related FROM memories_crossrefs ORDER BY memory_id").fetchall()
        outbox = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return hashlib.sha256(json.dumps([[tuple(r) for r in rows], [tuple(r) for r in refs], tuple(outbox)],
                                     default=str).encode()).hexdigest()


def test_cli_end_to_end_reads_a_store_copy_writes_only_the_report(store, tmp_path, monkeypatch, capsys):
    text1 = "Logs are kept 30 days. Uploads need approval. See Item 44 and Item 45."
    text2 = "Logs are kept 30 days. See Item 44 and Item 45."
    i1, i2 = _add(text1), _add(text2)
    before = _state()
    cp = tmp_path / "tidy.json"
    cp.write_text(json.dumps({"project": "clmux", "models": {"group": "haiku", "write": "sonnet"},
                              "state_dir": str(tmp_path / "state")}))
    seen = []

    def group(prompt, timeout=None):
        seen.append("g")
        return json.dumps(ans(keep=keep(i1, i2)))

    def write(prompt, timeout=None):
        seen.append("w")
        return json.dumps({"decisions": [], "followups": [], "constraints": [
            {"text": "Logs are kept 30 days.", "cites": [i2], "quote": "Logs are kept 30 days."}]})
    monkeypatch.setattr(tidy, "make_judges", lambda models, timeout: (group, write))
    out = tmp_path / "out"
    assert tidy.main(["--config", str(cp), "--db", str(store), "--out-dir", str(out)]) == 0
    assert seen == ["g", "w"]
    (report_path,) = list(out.iterdir())
    rep = tidy.load_tidy_plan(str(report_path))
    assert rep["project"] == "clmux" and rep["counts"]["live"] == 2 and rep["steps"][-1]["op"] == "bootstrap_digest"
    assert _state() == before                                   # the store was not touched
    assert "tidy dry run" in capsys.readouterr().err
    # judge unavailable: still a report, exit 3
    monkeypatch.setattr(tidy, "make_judges", lambda m, t: (_ for _ in ()).throw(dream.JudgeError("no model")))
    assert tidy.main(["--config", str(cp), "--db", str(store), "--out", str(tmp_path / "r2.json")]) == 3
    # no store copy given, no project: refused, the live store is never opened
    assert tidy.main(["--config", str(cp)]) == 2
    cp.write_text(json.dumps({"models": {"group": "h", "write": "s"}, "state_dir": str(tmp_path / "state")}))
    assert tidy.main(["--config", str(cp), "--db", str(store)]) == 2


def test_tidy_and_dream_share_one_lock(store, tmp_path):
    cp = tmp_path / "tidy.json"
    cp.write_text(json.dumps({"project": "clmux", "models": {"group": "h", "write": "s"},
                              "state_dir": str(tmp_path / "state")}))
    with dream.run_lock(tmp_path / "state"):
        assert tidy.main(["--config", str(cp), "--db", str(store)]) == 2
    with tidy_lock(tmp_path / "state"):
        with pytest.raises(DreamErr):
            with dream.run_lock(tmp_path / "state"):
                pass


tidy_lock = dream.run_lock
DreamErr = dream.DreamError


def test_retired_ids_follow_storage_retirement_rules(store):
    a, b, c = _add("alpha"), _add("beta"), _add("gamma")
    mems = dream.load_memories(storage.connect_without_schema())
    mems[0].refs.append({"id": b, "edge_type": "supersedes", "score": 1.0})
    with contextlib.closing(storage.connect_without_schema()) as conn:
        assert tidy.retired_ids(conn, mems) == {b}
    assert tidy.cosine({"a": 1.0}, {"a": 2.0}) == pytest.approx(1.0) and tidy.cosine({}, {"a": 1}) == 0.0


# ----------------------------------------------------------------------------- Item 254: digest input recency share


def _aged(i, days):
    return M(i, f"Note number {i} about topic{i}.", created=tidy.dream._iso(1_800_000_000.0 - days * 86400)
             .replace("T", " ").replace("Z", ""))


def test_the_newest_memories_get_a_reserved_share_of_the_digest_input():
    now = 1_800_000_000.0
    old = [_aged(i, 400 + i) for i in range(1, 21)]                       # 20 old, heavily read
    new = [_aged(100 + i, i) for i in range(1, 4)]                        # 3 new, never read
    reads = {m.id: 50 for m in old}
    lim = TidyLimits(digest_max_memories=6, digest_recent_share=0.5, recent_days=0.0)   # read-count-led ranking
    rows, cov = tidy.digest_candidates(old + new, [], reads, lim, now)
    ids = [r["cite"] for r in rows]
    assert set(ids) >= {101, 102, 103} and len(ids) == 6                 # the newest three are in, then the read-most
    assert cov["recent_included"] == 3 and cov["recent_ids"] == [101, 102, 103]
    assert cov["recent_share"] == 0.5 and cov["recent_reserved_count"] == 3 and cov["recent_reserved_chars"] == 30000
    # negative control: no share reproduces the old read-count-led input, which misses the new memories
    rows0, cov0 = tidy.digest_candidates(old + new, [], reads, TidyLimits(digest_max_memories=6,
                                                                          digest_recent_share=0.0, recent_days=0.0), now)
    assert not ({101, 102, 103} & {r["cite"] for r in rows0}) and cov0["recent_included"] == 0
    # the share is a share of BOTH bounds: a tiny char bound reserves a tiny amount
    rows2, cov2 = tidy.digest_candidates(old + new, [], reads, TidyLimits(digest_max_chars=60, digest_recent_share=0.5), now)
    assert cov2["chars"] <= 60 and cov2["recent_reserved_chars"] == 30
    with pytest.raises(TidyError, match="between 0 and 1"):
        TidyLimits.from_dict({"digest_recent_share": 1.5})


def test_recent_share_is_recorded_in_the_sealed_report():
    rep = run(Fake())
    di = rep["digest_input"]
    assert {"recent_share", "recent_included", "recent_ids", "newer_notes", "candidates_with_newer"} <= set(di)
    assert rep["digest"] == tidy.plan_digest(rep)


# ----------------------------------------------------------------------------- Item 254: overtaken decisions (Zig 0.17)


def zig_world():
    return [
        M(1294, "Wait for Zig 0.17.1 before porting libghostty. See Item 230.", created="2026-09-01 00:00:00"),
        M(1291, "The libghostty mirror is parked until Zig 0.16 or 0.17. See Item 230.", created="2026-09-02 00:00:00"),
        M(1380, "The Zig 0.17 port of libghostty landed on 2026-10-28. See Item 230.", created="2026-10-28 00:00:00"),
        M(1500, "Unrelated retries are capped at 3.", created="2026-10-30 00:00:00")]


def zig_group(prompt):
    return json.dumps(ans(keep=keep(*[m["id"] for m in data_of(prompt)["members"]])))


class ZigWriter:
    def __init__(self, mode):
        self.mode, self.prompts = mode, []

    def __call__(self, prompt, timeout=None):
        assert prompt.startswith("TASK: digest")
        d = data_of(prompt)
        self.prompts.append(d)
        old = {"text": "Wait for Zig 0.17.1 before porting libghostty.", "cites": [1294],
               "quote": "Wait for Zig 0.17.1 before porting"}
        if self.mode == "current":
            return json.dumps({"decisions": [old], "followups": [], "constraints": []})
        if self.mode == "overtaken":
            return json.dumps({"decisions": [], "followups": [], "constraints": [], "overtaken": [{
                "text": "Waiting for Zig 0.17.1 before porting was overtaken: the Zig 0.17 port landed on 2026-10-28.",
                "cites": [1294, 1380], "quote": "Wait for Zig 0.17.1 before porting"}]})
        if self.mode == "old-only":         # an overtaken line that does not cite a newer note is refused
            return json.dumps({"decisions": [{"text": "Retries are capped at 3.", "cites": [1500],
                                              "quote": "retries are capped at 3"}], "followups": [], "constraints": [],
                               "overtaken": [{"text": "Wait for Zig 0.17.1.", "cites": [1294],
                                              "quote": "Wait for Zig 0.17.1"}]})
        return json.dumps({"decisions": [{"text": "Retries are capped at 3.", "cites": [1500],
                                          "quote": "retries are capped at 3"}], "followups": [], "constraints": []})


def run_zig(mode):
    w = ZigWriter(mode)
    cfg = TidyConfig(limits=TidyLimits())
    emb = {m.id: {"a": 1.0} for m in zig_world()}
    rep = tidy.run_tidy(zig_world(), emb, set(), cfg, "clmux", zig_group, w, now=1_800_000_000.0)
    return rep, w


def test_digest_writer_sees_the_newer_memories_and_marks_the_overtaken_decision():
    rep, w = run_zig("overtaken")
    d = w.prompts[0]
    notes = {n["cite"]: n for n in d["notes"]}
    assert 1380 in notes[1294]["newer"] and 1380 in notes[1291]["newer"] and "newer" not in notes[1500]
    assert {n["cite"] for n in d["newer_notes"]} >= {1380} and "landed on 2026-10-28" in str(d["newer_notes"])
    dg = rep["steps"][-1]
    assert dg["op"] == "bootstrap_digest" and "## Overtaken" in dg["body"] and "## Current decisions" not in dg["body"]
    assert "[m1294, m1380]" in dg["body"] and 1380 in dg["cites"]
    ot = [r for r in rep["review"] if r["kind"] == "overtaken"]
    assert ot == [{"kind": "overtaken", "older": [1294], "newer": [1380], "text": ot[0]["text"],
                   "action": "retire or contradiction candidate: leader decides, never auto"}]
    assert {"id": 1294, "newer": notes[1294]["newer"], "placed": ["overtaken"]} in rep["digest_flags"]
    assert all(s["op"] not in ("retire", "merge") for s in rep["steps"])         # never auto-retired
    assert rep["digest_input"]["candidates_with_newer"] >= 2 and rep["digest"] == tidy.plan_digest(rep)


def test_a_decision_kept_current_despite_a_newer_memory_is_flagged_for_the_leader():
    rep, _ = run_zig("current")
    flag = next(f for f in rep["digest_flags"] if f["id"] == 1294)
    assert 1380 in flag["newer"] and flag["placed"] == ["decisions"]
    assert not [r for r in rep["review"] if r["kind"] == "overtaken"]
    rep, _ = run_zig("left-out")
    flag = next(f for f in rep["digest_flags"] if f["id"] == 1294)
    assert flag["placed"] == ["left_out"]


def test_overtaken_line_must_cite_an_older_and_an_offered_newer_note():
    rep, _ = run_zig("old-only")
    assert any(r["op"] == "digest_line" and "newer note offered" in r["reason"] for r in rep["rejected"])
    assert "## Overtaken" not in rep["steps"][-1]["body"]
    rows = [{"cite": 1294, "text": "Wait for Zig 0.17.1 before porting."}]
    newer = [{"cite": 1380, "date": "2026-10-28", "text": "The Zig 0.17 port landed."}]
    line = {"text": "Zig 0.17 port landed.", "cites": [1294, 1380], "quote": "The Zig 0.17 port landed."}
    ans_ = {"decisions": [], "followups": [], "constraints": [], "overtaken": [line]}
    assert tidy.validate_digest(ans_, rows, LIM, {1294: [1380]}, newer)[0] is not None
    # a newer note that was not offered for THIS older note is refused
    assert tidy.validate_digest(ans_, rows, LIM, {1294: []}, newer)[2] == "no valid line"
    assert tidy.validate_digest(ans_, rows, LIM, None, newer)[2] == "no valid line"
    # the old 3-section shape still works
    assert tidy.validate_digest({"decisions": [], "followups": [], "constraints": [
        {"text": "Wait for Zig 0.17.1.", "cites": [1294], "quote": "Wait for Zig 0.17.1"}]}, rows, LIM)[2] is None


def test_newer_context_needs_a_shared_reference_or_rare_words_and_is_newer():
    rows = [{"cite": 1, "text": ""}, {"cite": 2, "text": ""}, {"cite": 3, "text": ""}]
    mems = [M(1, "Port waits for the frobnicator release. See Item 7.", created="2026-01-01 00:00:00"),
            M(2, "Frobnicator release is out, Item 7 closed.", created="2026-02-01 00:00:00"),
            M(3, "Nothing alike here.", created="2026-03-01 00:00:00"),
            M(4, "Older note on Item 7.", created="2025-12-01 00:00:00")]
    nm, notes = tidy.newer_context(rows, mems, [], TidyLimits())
    assert nm == {1: [2]} and [n["cite"] for n in notes] == [2]                 # 4 is older, 3 is unrelated
    assert tidy.newer_context(rows, mems, [{"op": "retire", "id_hidden": 2}], TidyLimits())[0] == {}
    assert tidy.newer_context(rows, mems, [], TidyLimits(newer_per_note=0))[0] == {}


# ----------------------------------------------------------------------------- Item 254: one repair call per rejected cluster


def test_a_rejected_cluster_gets_one_repair_call_with_the_validator_error():
    seen = []

    def group(prompt):
        seen.append(prompt)
        return json.dumps(ans(keep=keep(1, 2))) if len(seen) == 1 else json.dumps(ans(keep=keep(1, 2, 3)))
    fake = Fake(group=group)
    rep = run(fake)
    assert fake.calls.count("group") == 2 and rep["judge"]["calls"]["group"] == 2
    assert "Your previous answer was rejected" in seen[1] and "primary disposition" in seen[1]
    cl = rep["clusters"][0]
    assert cl["status"] == "judged" and cl["repair"]["result"] == "ok" and "primary disposition" in cl["repair"]["first_problem"]
    # still failing closed after the one repair
    fake = Fake(group=json.dumps(ans(keep=keep(1, 2))))
    rep = run(fake)
    assert fake.calls.count("group") == 2                                   # one repair, not a loop
    cl = rep["clusters"][0]
    assert cl["status"] == "rejected" and cl["repair"]["result"] == cl["problem"]
    assert not [s for s in rep["steps"] if s["op"] not in ("bootstrap_digest",)]
    # the repair is counted against max_llm_calls (cap 2, digest reserve 1: one cluster call, no repair)
    fake = Fake(group=json.dumps(ans(keep=keep(1, 2))))
    rep = run(fake, max_llm_calls=2)
    assert fake.calls.count("group") == 1 and "repair" not in rep["clusters"][0] and rep["judge"]["calls"]["total"] <= 2
    # a judge that failed outright is not retried with an error text
    def boom(prompt, timeout=None):
        raise RuntimeError("down")
    rep = tidy.run_tidy(world(), EMB, set(), TidyConfig(limits=TidyLimits()), "clmux", boom, boom, now=1.0)
    assert rep["judge"]["calls"]["group"] == 1 and "repair" not in rep["clusters"][0]


# ----------------------------------------------------------------------------- Item 254: project backfill


def NP(mid, content="x", **kw):
    kw.setdefault("project", None)
    return M(mid, content, **kw)


def bf(mems, judge=None, retired=(), **lim):
    cfg = TidyConfig(limits=TidyLimits(**lim), projects=["clmux", "memora", "acebar"])
    return tidy.run_backfill(mems, set(retired), cfg, judge, now=1_800_000_000.0, models={"group": "haiku"})


def test_backfill_deterministic_signals_never_overwrite_and_list_protected_apart():
    mems = [M(1, "labelled clmux", project="clmux"), M(2, "labelled memora", project="memora"),
            M(3, "labelled acebar", project="acebar"),
            NP(10, "a note", tags=["clmux/x"]),
            NP(11, "Item record body.", tags=["item-record"], meta={"kind": "item-record", "repo": "agentic-box/memora"}),
            NP(12, "a decision", tags=["acebar/ui"], meta={"kind": "decision"}),
            NP(13, "no signal at all"),
            M(14, "already has one", tags=["clmux/x"], project="memora")]
    rep = bf(mems)
    st = {s["target"]: s for s in rep["steps"]}
    assert set(st) == {10, 11, 12} and 14 not in st and 13 in [u["id"] for u in rep["unresolved"]]
    assert st[10]["project"] == "clmux" and st[10]["confidence"] == "high" and st[10]["source"] == "signals"
    assert st[10]["evidence"] == ["tag:clmux/x"] and st[11]["project"] == "memora" and "metadata.repo" in st[11]["evidence"][0]
    assert rep["lists"] == {"protected": [12], "item_records": [11], "other": [10]}
    assert rep["by_project"] == {"acebar": 1, "clmux": 1, "memora": 1} and rep["counts"]["without_project"] == 4
    assert rep["judge"]["calls"]["total"] == 0 and rep["mode"] == "dry-run" and rep["plan"] == "backfill"
    assert rep["candidates"] == ["acebar", "clmux", "memora"] and rep["digest"] == tidy.plan_digest(rep)
    assert all(s["charge"] == 1 and s["expect"] == {str(s["target"]): next(m for m in mems if m.id == s["target"]).state_sha}
               for s in rep["steps"])
    # the validator refuses to overwrite and checks the rest
    by = {m.id: m for m in mems}
    cands = rep["candidates"]
    assert tidy.validate_set_project(st[10], by[10], cands) is None
    assert "never overwritten" in tidy.validate_set_project(dict(st[10], target=14), by[14], cands)
    assert tidy.validate_set_project(dict(st[10], project="zzz"), by[10], cands) == "project is not a candidate"
    assert tidy.validate_set_project(dict(st[10], evidence=[]), by[10], cands) == "no evidence"
    assert tidy.validate_set_project(dict(st[10], confidence="sure"), by[10], cands) == "bad confidence"
    assert "changed" in tidy.validate_set_project(dict(st[10], expect={"10": "zz"}), by[10], cands)
    assert tidy.validate_set_project(dict(st[10], op="link"), by[10], cands) == "not a set_project step"
    # apply-time revalidation refuses a memory that has gained a project since
    look = lambda i: (by[i], False)   # noqa: E731
    assert tidy.revalidate_step(st[10], look, {}, [], "clmux") == []
    by[10] = M(10, "a note", tags=["clmux/x"], project="memora")
    assert tidy.revalidate_step(st[10], look, {}, [], "clmux") == ["changed:10", "has_project:10"]


def test_backfill_ambiguous_signals_go_to_haiku_which_may_say_unknown():
    labelled = [M(1, "uses `shared/util.py`", project="clmux"), M(2, "uses shared/util.py", project="memora")]
    ambiguous = NP(20, "Fix the shared tag handling. Exact words here.", tags=["clmux/a", "memora/b"])
    weak = NP(21, "Edits docs/readme_only.md for clmux.", tags=["acebar/z"])         # one signal of 3: decisive
    weak2 = NP(22, "Edits src/only_clmux.py and src/other.py.")
    labelled.append(M(3, "touches src/only_clmux.py", project="clmux"))
    seen = []

    def judge(prompt, timeout=None):
        d = data_of(prompt)
        seen.append(d)
        assert prompt.startswith("TASK: project") and [n["id"] for n in d["notes"]] == [20, 22]
        assert d["notes"][0]["options"] == ["clmux", "memora"] and "signals" in d["notes"][0]
        return json.dumps({"answers": [
            {"id": 20, "project": "unknown", "quote": "", "confidence": "low"},
            {"id": 22, "project": "clmux", "quote": "Edits src/only_clmux.py", "confidence": "medium"}]})
    rep = bf(labelled + [ambiguous, weak, weak2], judge)
    by = {s["target"]: s for s in rep["steps"]}
    assert set(by) == {21, 22} and by[21]["source"] == "signals" and by[22]["source"] == "judge"
    assert by[22]["confidence"] == "medium" and by[22]["evidence"][-1].startswith("quote:")
    assert {"id": 20, "reason": "judge: unknown"} in rep["unresolved"] and rep["judge"]["calls"]["total"] == 1
    # refused judge rows: not an offered option, quote not in the note, high confidence, missing row, bad shape
    def bad(prompt, timeout=None):
        return json.dumps({"answers": [
            {"id": 20, "project": "acebar", "quote": "Exact words", "confidence": "low"},
            {"id": 22, "project": "clmux", "quote": "words not in the note", "confidence": "medium"}]})
    rep = bf(labelled + [ambiguous, weak2], bad)
    assert not rep["steps"] and {u["reason"] for u in rep["unresolved"]} == {
        "project is not one of the offered options", "quote is not word for word in the note"}
    rep = bf(labelled + [ambiguous], lambda p, timeout=None: json.dumps({"answers": []}))
    assert rep["unresolved"] == [{"id": 20, "reason": "no answer"}]
    rep = bf(labelled + [ambiguous], lambda p, timeout=None: json.dumps({"answers": [
        {"id": 20, "project": "clmux", "quote": "Exact words", "confidence": "high"}]}))
    assert rep["unresolved"] == [{"id": 20, "reason": "bad confidence"}]
    rep = bf(labelled + [ambiguous], lambda p, timeout=None: "nope")
    assert rep["unresolved"] == [{"id": 20, "reason": "judge answer is not JSON"}]
    rep = bf(labelled + [ambiguous], None)
    assert rep["unresolved"] == [{"id": 20, "reason": "judge unavailable"}] and not rep["steps"]
    rep = bf(labelled + [ambiguous, weak2], judge, max_llm_calls=0)
    assert {"id": 20, "reason": "call budget"} in rep["unresolved"] and rep["stopped_by"] == "max_llm_calls"


def test_backfill_cap_retired_and_config():
    mems = [M(1, "x", project="clmux")] + [NP(100 + i, "n", tags=["clmux/x"]) for i in range(5)]
    rep = bf(mems, max_backfill=3)
    assert [s["target"] for s in rep["steps"]] == [100, 101, 102] and [s["id"] for s in rep["steps"]] == ["b1", "b2", "b3"]
    assert [d["id"] for d in rep["deferred"]] == [103, 104] and rep["stopped_by"] == "max_backfill"
    assert [s["target"] for s in bf(mems, retired=[100])["steps"]] == [101, 102, 103, 104]      # retired are skipped
    assert TidyLimits().max_backfill == 200


def test_backfill_config_and_candidates_come_from_config_and_the_store(tmp_path):
    cp = tmp_path / "c.json"
    cp.write_text(json.dumps({"models": {"group": "h", "write": "s"}, "projects": ["clmux", "agentic-box"],
                              "repo_paths": {"clmux": ["src/"]}}))
    cfg = TidyConfig.load(str(cp))
    assert cfg.projects == ["clmux", "agentic-box"] and cfg.repo_paths == {"clmux": ["src/"]}
    for bad in ({"projects": "clmux"}, {"projects": [1]}, {"repo_paths": {"a": "b"}}):
        cp.write_text(json.dumps({"models": {"group": "h", "write": "s"}, **bad}))
        with pytest.raises(TidyError):
            TidyConfig.load(str(cp))
    mems = [M(1, "a", project="memora"), NP(2, "b")]
    assert tidy.backfill_candidates(mems, ["clmux"]) == ["clmux", "memora"]
    # a configured repo tree and the labelled-memory path index give path signals
    cfg = TidyConfig(limits=TidyLimits(), projects=["clmux", "memora"], repo_paths={"clmux": ["Sources/"]})
    rep = tidy.run_backfill([NP(5, "Edit Sources/App/main.swift")], set(), cfg, None, now=1.0)
    assert not rep["steps"]                                                  # one path = 2 < 3: not decisive alone
    rep = tidy.run_backfill([NP(5, "Edit Sources/App/main.zig and Sources/Other/b.zig")], set(), cfg, None, now=1.0)
    assert [(s["target"], s["project"]) for s in rep["steps"]] == [(5, "clmux")]


def test_backfill_cli_end_to_end_reads_a_store_copy_and_writes_only_the_report(store, tmp_path, capsys):
    with storage.connect() as conn:
        storage.add_memory(conn, content="Labelled.", tags=["clmux"], metadata={"project": "clmux"})
        storage.add_memory(conn, content="Unlabelled.", tags=["clmux/x"], metadata={})
        conn.commit()
    before = _state()
    cp = tmp_path / "tidy.json"
    cp.write_text(json.dumps({"models": {"group": "haiku"}, "state_dir": str(tmp_path / "state")}))
    out = tmp_path / "out"
    assert tidy.main(["--config", str(cp), "--db", str(store), "--backfill", "--out-dir", str(out)]) == 0
    (rp,) = list(out.iterdir())
    rep = tidy.load_tidy_plan(str(rp))
    assert rep["plan"] == "backfill" and [(s["target"], s["project"]) for s in rep["steps"]] == [(2, "clmux")]
    assert _state() == before and "backfill dry run" in capsys.readouterr().err
    assert rp.name.startswith("tidy-backfill-")
    # no group model: still a report from signals, not an error
    cp.write_text(json.dumps({"models": {}, "state_dir": str(tmp_path / "state")}))
    assert tidy.main(["--config", str(cp), "--db", str(store), "--backfill", "--out", str(tmp_path / "r.json")]) == 0


# ----------------------------------------------------------------------------- Item 254 round 2


class _Proc:
    def __init__(self, out="", rc=0):
        self.stdout, self.returncode, self.stderr = out, rc, ""


def _mirror_dir(tmp_path, name):
    d = tmp_path / "git" / name
    (d / "objects").mkdir(parents=True)
    (d / "HEAD").write_text("ref: refs/heads/main\n")
    return d


def _runner(trees, calls=None, fail=()):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        name = Path(cmd[cmd.index("-C") + 1]).name
        sub = cmd[cmd.index("-C") + 2:]
        if name in fail:
            raise OSError("boom")
        if sub[0] == "ls-tree":
            return _Proc("\n".join(trees[name]) + "\n")
        return _Proc("true\n")
    return run


def test_backfill_uses_mirror_trees_where_available(tmp_path):
    for n in ("clmux", "memora", "dead", "multi"):
        _mirror_dir(tmp_path, n)
    mir = {"git_dir": str(tmp_path / "git"), "repos": {
        "clmux": {}, "memora": {}, "missing": {"projects": ["acebar"]}, "dead": {"projects": ["acebar"]},
        "multi": {"projects": ["clmux", "memora"]}}}
    calls = []
    trees, status = tidy.load_mirror_trees(mir, ["acebar", "clmux", "memora"], runner=_runner({
        "clmux": ["Sources/Term/pane.zig", "shared/util.py", "README.md"],
        "memora": ["memora/tidy.py", "shared/util.py"], "dead": [], "multi": []}, calls, fail=("dead",)))
    assert set(trees) == {"clmux", "memora"} and "Sources/Term/pane.zig" in trees["clmux"]
    assert status["clmux"] == "ok" and status["missing"].startswith("no clone")
    assert status["dead"] == "tree unavailable (git failed)"
    assert status["multi"].startswith("ambiguous")
    assert all("fetch" not in c for c in calls) and all(c[0] == "git" for c in calls)         # read-only plumbing only
    # the genuine unlabelled corpus: two real repo paths, no tag, no repo metadata, no labelled mention
    cfg = TidyConfig(limits=TidyLimits(), projects=["clmux", "memora"])
    note = NP(5, "Fix Sources/Term/pane.zig and Sources/Term/split.zig crash.")
    assert not tidy.run_backfill([note], set(), cfg, None, now=1.0)["steps"]                  # without mirrors: no signal
    trees2 = {"clmux": frozenset({"Sources/Term/pane.zig", "Sources/Term/split.zig"}), "memora": frozenset({"memora/tidy.py"})}
    rep = tidy.run_backfill([note], set(), cfg, None, trees=trees2, mirror_status={"clmux": "ok"}, now=1.0)
    st = rep["steps"][0]
    assert (st["target"], st["project"], st["confidence"], st["source"]) == (5, "clmux", "high", "signals")
    assert "mirror tree" in st["evidence"][0] and rep["mirrors"] == {"clmux": "ok"}
    # a path present in two trees, a bare file name, and an absent path give no signal
    both = {"clmux": frozenset({"shared/util.py", "README.md"}), "memora": frozenset({"shared/util.py"})}
    assert not tidy.run_backfill([NP(6, "See shared/util.py and shared/util.py again")], set(), cfg, None, trees=both, now=1.0)["steps"]
    assert not tidy.run_backfill([NP(7, "Read README.md and docs/none.md")], set(), cfg, None, trees=both, now=1.0)["steps"]
    # no mirrors configured: nothing, quietly
    assert tidy.load_mirror_trees(None, ["a"]) == ({}, {})
    assert tidy.load_mirror_trees({"git_dir": "x", "repos": {"a": {}}}, ["a"])[1]["a"]
    cp = tmp_path / "c.json"
    cp.write_text(json.dumps({"models": {"group": "h", "write": "s"}, "mirrors": {"git_dir": "x"}}))
    with pytest.raises(TidyError, match="mirrors"):
        TidyConfig.load(str(cp))
    cp.write_text(json.dumps({"models": {"group": "h", "write": "s"}, "mirrors": mir}))
    assert TidyConfig.load(str(cp)).mirrors == mir


def test_malformed_backfill_values_fail_closed_per_row():
    mem = NP(20, "Exact words here.", tags=["clmux/a", "memora/b"])
    labelled = [M(1, "x", project="clmux"), M(2, "y", project="memora")]
    for bad_project in ([], {}, 5, None):
        rep = bf(labelled + [mem], lambda p, timeout=None, v=bad_project: json.dumps({"answers": [
            {"id": 20, "project": v, "quote": "Exact words", "confidence": "low"}]}))
        assert rep["unresolved"] == [{"id": 20, "reason": "project is not a string"}] and not rep["steps"]
    rep = bf(labelled + [mem], lambda p, timeout=None: json.dumps({"answers": [
        {"id": 20, "project": "clmux", "quote": "Exact words", "confidence": []}]}))
    assert rep["unresolved"] == [{"id": 20, "reason": "bad confidence"}]


def test_left_out_overtaken_candidates_become_review_rows_and_backfill_honours_its_cap_first():
    rep, _ = run_zig("left-out")
    row_ = [r for r in rep["review"] if r["kind"] == "potential_overtaken"]
    assert {r["older"] for r in row_} >= {1294} and all(r["newer"] for r in row_)
    # backfill: deterministic steps fill the cap, so no model call is spent on the ambiguous ones
    labelled = [M(1, "x", project="clmux"), M(2, "y", project="memora")]
    mems = labelled + [NP(100 + i, "n", tags=["clmux/x"]) for i in range(3)] + [NP(50, "w", tags=["clmux/a", "memora/b"])]
    seen = []
    rep = bf(mems, lambda p, timeout=None: seen.append(p) or "{}", max_backfill=3)
    assert seen == [] and rep["judge"]["calls"]["total"] == 0 and len(rep["steps"]) == 3
    assert {"id": 50, "reason": "cap:max_backfill (not asked)"} in rep["deferred"]
