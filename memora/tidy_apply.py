"""The tidy apply step (Item 255, docs/tidy-design.md section 5, docs/tidy.md "Apply").

`memora-tidy --apply --plan REPORT --decisions DECISIONS` writes the METADATA-ONLY steps of a sealed tidy report:
`set_project` (project label), `link` (a typed edge) and `retag` (tags are only ever ADDED). They never change a
memory's text, never hide a memory and never delete anything. Every other op (merge, retire, the digest steps,
anything unknown) is listed as skipped and is never written, even when approved.

No model call, no git, no network except what the store itself does (a D1-backed store replicates through its own
outbox). The store is the configured one (like `memora-dream --apply`); the run takes dream's run lock; every store
call runs on one worker thread bounded by the run's remaining time (an in-flight call at the deadline is AMBIGUOUS
and reported, never retried).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import dream, tidy
from .dream import DreamError
from .tidy import TidyError

DIGEST_OPS = ("bootstrap_digest", "replace_digest", "repair_digest")
SUPPORTED_OPS = ("set_project", "link", "retag") + DIGEST_OPS
KNOWN_OPS = SUPPORTED_OPS + ("merge", "retire")
CONF_RANK = {"low": 1, "medium": 2, "high": 3}
CHARGE = {"set_project": 1, "link": 2, "retag": 1,      # store writes per step (the design's charges, 5.3)
          "bootstrap_digest": 1, "replace_digest": 3, "repair_digest": 2}
NOT_SUPPORTED = "op not supported by this apply"


# --------------------------------------------------------------------------- decisions


def load_decisions(path: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Parse and validate a decisions file against the (already verified) report. Raises TidyError."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise TidyError(f"cannot read decisions {path}: {e}")
    if not isinstance(raw, dict):
        raise TidyError("decisions must be a JSON object")
    allowed = {"report_digest", "approve", "reject", "approve_statements", "approve_ops"}
    extra = sorted(set(raw) - allowed)
    if extra:
        raise TidyError(f"decisions has unknown key(s): {', '.join(extra)}")
    if raw.get("report_digest") != plan.get("digest"):
        raise TidyError("decisions report_digest does not match the report: they were made for another report")
    ids = {st.get("id") for st in plan.get("steps", []) if isinstance(st, dict)}
    out: Dict[str, Any] = {"approve": [], "reject": [], "approve_ops": []}
    for key in ("approve", "reject", "approve_statements"):
        v = raw.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise TidyError(f"decisions '{key}' must be a list of strings")
        if key != "approve_statements":
            unknown = sorted(set(v) - ids)
            if unknown:
                raise TidyError(f"decisions '{key}' names step id(s) not in the report: {', '.join(unknown)}")
            out[key] = list(dict.fromkeys(v))
    rules = raw.get("approve_ops", [])
    if not isinstance(rules, list):
        raise TidyError("decisions 'approve_ops' must be a list")
    for r in rules:
        if not isinstance(r, dict) or set(r) - {"op", "min_confidence"} or not isinstance(r.get("op"), str) \
                or r["op"] not in KNOWN_OPS:
            raise TidyError("each approve_ops entry must be {\"op\": <known op>[, \"min_confidence\": high|medium|low]}")
        if r["op"] in DIGEST_OPS:
            raise TidyError(f"approve_ops cannot name {r['op']}: a digest step is approved by its id only")
        if "min_confidence" in r and not (isinstance(r["min_confidence"], str) and r["min_confidence"] in CONF_RANK):
            raise TidyError("approve_ops min_confidence must be high, medium or low")
        out["approve_ops"].append(dict(r))
    return out


def approved_by(step: Dict[str, Any], dec: Dict[str, Any]) -> Optional[str]:
    """Why a step is approved (None = not approved). Reject wins; a needs_approval step only by its id."""
    sid = step.get("id")
    if sid in dec["reject"]:
        return None
    if sid in dec["approve"]:
        return "approve:id"
    if step.get("op") in DIGEST_OPS:    # whatever the report says, a digest (a new memory) is approved by id only
        return None
    if step.get("needs_approval") is not False:          # true, or anything that is not exactly false: by id only
        return None
    for r in dec["approve_ops"]:
        if r["op"] != step.get("op"):
            continue
        need = r.get("min_confidence")
        if need is None:
            return f"approve_ops:{r['op']}"
        have = CONF_RANK.get(step.get("confidence"))
        if have is not None and have >= CONF_RANK[need]:
            return f"approve_ops:{r['op']}>={need}"
    return None


def _digest_shape(step: Dict[str, Any], project: Any) -> Optional[str]:
    op = step.get("op")
    isint = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
    exp = step.get("expect")
    if not isinstance(exp, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in exp.items()):
        return "malformed step: expect"
    if not isinstance(step.get("depends_on", []), list):
        return "malformed step: depends_on"
    if not (isinstance(project, str) and project):
        return "malformed report: no project"
    if op == "repair_digest":
        w, lo, both = step.get("winner"), step.get("supersedes"), step.get("both")
        if not (isint(w) and isint(lo)) or w == lo or not isinstance(both, list) or not all(isint(x) for x in both) \
                or sorted(both) != sorted([w, lo]) or set(exp) != {str(w), str(lo)}:
            return "malformed repair_digest step"
        return None
    body, tags, md, cites = step.get("body"), step.get("tags"), step.get("metadata"), step.get("cites")
    if not (isinstance(body, str) and body.strip()) or not isinstance(tags, list) or not tags \
            or not all(isinstance(t, str) and t.strip() for t in tags) or f"{project}/digest" not in tags:
        return f"malformed {op} step: body or tags"
    if not isinstance(md, dict) or md.get("kind") != "digest" or md.get("project") != project \
            or md.get("digest_of") != "tidy" or not isinstance(md.get("run"), str) \
            or not isinstance(md.get("covers"), list) or not all(isint(c) for c in md["covers"]):
        return f"malformed {op} step: metadata"
    if not isinstance(cites, list) or not cites or not all(isint(c) or isinstance(c, str) for c in cites):
        return f"malformed {op} step: cites"
    if op == "bootstrap_digest":
        return None if not exp and "supersedes" not in step else "malformed bootstrap_digest step: expect"
    old = step.get("supersedes")
    if not isint(old) or set(exp) != {str(old)}:
        return "malformed replace_digest step"
    return None


def shape_problem(step: Dict[str, Any], project: Any = None) -> Optional[str]:
    """Structure of a supported step, before anything is read from the store."""
    op = step.get("op")
    if op in DIGEST_OPS:
        return _digest_shape(step, project)
    isint = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
    exp = step.get("expect")
    if not isinstance(exp, dict) or not exp or not all(isinstance(k, str) and isinstance(v, str) for k, v in exp.items()):
        return "malformed step: expect"
    if not isinstance(step.get("depends_on", []), list):
        return "malformed step: depends_on"
    if op == "set_project":
        if not isint(step.get("target")) or not (isinstance(step.get("project"), str) and step["project"].strip()):
            return "malformed set_project step"
        if str(step["target"]) not in exp:
            return "malformed set_project step: expect lacks the target"
    elif op == "link":
        a, b = step.get("a"), step.get("b")
        if not (isint(a) and isint(b)) or a == b or step.get("edge_type") not in tidy.EDGE_TYPES:
            return "malformed link step"
        if not {str(a), str(b)} <= set(exp):
            return "malformed link step: expect lacks an endpoint"
    elif op == "retag":
        add = step.get("add")
        if not isint(step.get("target")) or not isinstance(add, list) or not add \
                or not all(isinstance(t, str) and t.strip() for t in add):
            return "malformed retag step"
        if str(step["target"]) not in exp:
            return "malformed retag step: expect lacks the target"
    return None


# --------------------------------------------------------------------------- selection (pure, no store)


def select(plan: Dict[str, Any], dec: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Split the report's steps with the decisions alone: `run` (supported and approved, in report order) and the
    rest with their reasons. No store is read; the guards run later, against the store."""
    run: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for st in plan.get("steps", []):
        sid, op = st.get("id"), st.get("op")
        row = {"id": sid, "op": op}
        if sid in dec["reject"]:
            skipped.append({**row, "reason": "rejected in decisions"})
        elif op not in SUPPORTED_OPS:
            skipped.append({**row, "reason": f"skipped: {NOT_SUPPORTED}"})
        elif (why := approved_by(st, dec)) is None:
            skipped.append({**row, "reason": "not approved" + (" (needs_approval: approve it by id)"
                                                                if st.get("needs_approval") is not False else "")})
        elif st.get("status", "eligible") != "eligible":
            skipped.append({**row, "reason": f"step status is {st.get('status')!r}, not eligible"})
        elif (bad := shape_problem(st, plan.get("project"))) is not None:
            skipped.append({**row, "reason": bad})
        else:
            run.append(dict(st, _approved=why))
    return {"run": run, "skipped": skipped}


def dry_counts(plan: Dict[str, Any], dec: Dict[str, Any]) -> Dict[str, Any]:
    """What `select` would run and leave, by op and reason. Touches no store (an upper bound: the store guards
    and the caps can only skip more)."""
    sel = select(plan, dec)
    by_op: Dict[str, int] = {}
    for st in sel["run"]:
        by_op[st["op"]] = by_op.get(st["op"], 0) + 1
    reasons: Dict[str, int] = {}
    for s in sel["skipped"]:
        k = f"{s['op']}: {s['reason']}"
        reasons[k] = reasons.get(k, 0) + 1
    flagged = sum(1 for st in sel["run"] if st["op"] == "set_project" and st.get("protected") is True)
    return {"steps": len(plan.get("steps", [])), "would_attempt": len(sel["run"]), "would_attempt_by_op": by_op,
            "set_project_flagged_protected_in_plan (the store decides at apply; these skip if still protected)": flagged,
            "not_attempted": len(sel["skipped"]), "not_attempted_by_reason": dict(sorted(reasons.items()))}


# --------------------------------------------------------------------------- apply


class _Ctx:
    """Per-run state: the writer, the caps, and the post-write state of memories this run changed."""

    def __init__(self, W: Any, storage_mod: Any, conn: Any, lim: Any):
        self.W, self.st, self.conn, self.lim = W, storage_mod, conn, lim
        self.writes = 0
        self.backfill = 0
        # (id, sha judged in the plan) -> sha after OUR exact write; persisted in the journal so a rerun can tell
        # our earlier effects from a foreign edit
        self.alias: Dict[Tuple[int, str], set] = {}
        self.journal: Optional[Path] = None
        self.digest: Optional[str] = None
        self.project: Optional[str] = None
        self.created: Dict[str, int] = {}       # digest step id -> the memory it created (kept in the journal)

    def save_journal(self) -> None:
        if self.journal is None:
            return
        body = {"report_digest": self.digest, "created": dict(self.created),
                "post": {f"{m}:{w}": sorted(v) for (m, w), v in sorted(self.alias.items())}}
        tmp = self.journal.with_name(self.journal.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(body, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.journal)
        try:        # make the rename itself durable (the file was fsynced above)
            dfd = os.open(str(self.journal.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def intend(self, mid: int, want: Optional[str], post_sha: str) -> Optional[str]:
        """WRITE-AHEAD: record the state sha the sealed mutation WILL produce, durably, BEFORE it is dispatched. A
        crash, a deadline or a failed journal write after the commit can then never lose it: a rerun accepts exactly
        that predicted state (and the judged one), nothing else. None = recorded; else the reason (write nothing)."""
        self.alias.setdefault((mid, want), set()).add(post_sha)
        try:
            self.save_journal()
        except OSError as e:
            return f"the replay journal is not writable ({type(e).__name__}); nothing written"
        return None

    def load_journal(self) -> None:
        if self.journal is None or not self.journal.is_file():
            return
        try:
            raw = json.loads(self.journal.read_text())
            if raw.get("report_digest") != self.digest:
                return
            c = raw.get("created")
            if isinstance(c, dict):
                self.created = {k: v for k, v in c.items() if isinstance(k, str) and isinstance(v, int)}
            for k, v in raw["post"].items():
                m, w = k.split(":", 1)
                if isinstance(v, list) and all(isinstance(x, str) for x in v):
                    self.alias[(int(m), w)] = set(v)
        except (OSError, ValueError, KeyError, AttributeError):
            self.alias.clear()      # an unreadable journal only means later steps are checked against the original state

    def reserve(self, op: str, n: Optional[int] = None) -> Optional[str]:
        if self.W.stuck or self.W.remaining() <= 0:
            return "max_seconds"
        n = CHARGE[op] if n is None else n
        if op == "set_project":
            if self.backfill + n > self.lim.max_backfill:
                return "cap:max_backfill"
            self.backfill += n
        else:
            if self.writes + n > self.lim.max_writes:
                return "cap:max_writes"
            self.writes += n
        return None

    def read(self, mid: int) -> Optional[Tuple[Any, Dict[str, Any]]]:
        return self.W.run("read", lambda: dream._fresh(self.conn, mid))

    def retired(self, mid: int) -> bool:
        return bool(self.W.run("read", lambda: dream._retired(self.conn, mid, self.st)))

    def sha_ok(self, mid: int, m: Any, step: Dict[str, Any]) -> bool:
        want = step["expect"].get(str(mid))
        return m.state_sha == want or m.state_sha in self.alias.get((mid, want), ())


def _predict_sha(m: Any, metadata: Dict[str, Any], tags: Sequence[str]) -> str:
    """The state sha of `m` after a mutation to these metadata and tags (content never changes)."""
    return dream.Mem(m.id, m.content, dict(metadata), list(tags), m.updated_at, m.created_at, list(m.refs)).state_sha


def _canon(cx: "_Ctx", meta: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata as the store itself writes it back. Every update runs it through the store's canonical form
    (`section`/`subsection` become `hierarchy.path`, `tasks`/`done` are reshaped): a project set on a row that has
    a section gives it a derived `hierarchy`. The expected post-write state must be computed the same way."""
    try:
        return dict(cx.st._build_metadata_dict(dict(meta)))
    except (ValueError, AttributeError, TypeError):
        return dict(meta)


def _norm(tags: Sequence[str]) -> List[str]:
    return [t.strip() for t in tags]


def _do_set_project(cx: _Ctx, step: Dict[str, Any]) -> Dict[str, Any]:
    mid, proj = step["target"], step["project"]
    got = cx.read(mid)
    if got is None:
        return {"skip": "memory is gone"}
    m, raw = got
    if tidy._has_project(m):
        if m.metadata.get("project") == proj:
            return {"skip": f"already done: the memory already has project {proj!r}"}
        return {"skip": "the memory now has a project: never overwritten"}
    if not cx.sha_ok(mid, m, step):
        return {"skip": "the memory changed since it was judged (state sha mismatch)"}
    if cx.retired(mid):
        return {"skip": "the memory is retired"}
    if tidy.is_protected(m):
        return {"skip": "protected: only links and added tags are written to a protected memory"}
    why = cx.reserve("set_project")
    if why:
        return {"defer": why}
    new_meta = {**m.metadata, "project": proj}
    resolved = cx.st._resolve_project(None, m.tags, new_meta, strict=False)
    tags_after = cx.st._retarget_typed_tags(list(m.tags), resolved, new_meta)
    for md in (_canon(cx, new_meta), new_meta):          # the store's canonical form, or the metadata as given
        bad = cx.intend(mid, step["expect"].get(str(mid)), _predict_sha(m, md, tags_after))
        if bad:
            return {"skip": bad}
    res = cx.W.run("update_memory", lambda: cx.st.update_memory(
        cx.conn, mid, metadata={"project": proj}, expected_row=raw, embed=False))
    return _after(cx, step, mid, m, res, {"project": proj}, {"project": proj})


def _do_retag(cx: _Ctx, step: Dict[str, Any]) -> Dict[str, Any]:
    mid = step["target"]
    got = cx.read(mid)
    if got is None:
        return {"skip": "memory is gone"}
    m, raw = got
    add = [t for t in dict.fromkeys(_norm(step["add"]))]
    missing = [t for t in add if t not in m.tags]
    if not missing:
        return {"skip": "already done: every tag is already present"}
    if not cx.sha_ok(mid, m, step):
        return {"skip": "the memory changed since it was judged (state sha mismatch)"}
    if cx.retired(mid):
        return {"skip": "the memory is retired"}
    want = list(m.tags) + missing
    # update_memory normalises and retargets the WHOLE tag list; add-only means every old tag must survive that
    # unchanged, so it is previewed BEFORE the write (same helpers, same order) and refused when it would rewrite one
    project = cx.st._resolve_project(None, want, m.metadata, strict=False)
    result = cx.st._retarget_typed_tags(list(cx.st._normalize_tags(list(want), project)), project, m.metadata)
    lost = [t for t in m.tags if t not in result]
    if lost:
        return {"skip": f"adding tags would rewrite existing tag(s) {lost}: not an add-only change"}
    why = cx.reserve("retag")
    if why:
        return {"defer": why}
    for md in (m.metadata, _canon(cx, m.metadata)):
        bad = cx.intend(mid, step["expect"].get(str(mid)), _predict_sha(m, md, result))
        if bad:
            return {"skip": bad}
    res = cx.W.run("update_memory", lambda: cx.st.update_memory(
        cx.conn, mid, tags=want, expected_row=raw, embed=False))
    extra: Dict[str, Any] = {"added": missing}
    if step.get("remove"):
        extra["remove_ignored"] = list(step["remove"])
    return _after(cx, step, mid, m, res, {}, extra)


def _after(cx: _Ctx, step: Dict[str, Any], mid: int, m: Any, res: Any, meta_patch: Dict[str, Any],
           extra: Dict[str, Any]) -> Dict[str, Any]:
    """Authorize later steps of this plan on the memory ONLY when the row now is exactly what our write produced
    (the returned content, tags and timestamp, and the old metadata plus our patch). Anything else, such as a
    concurrent edit between our commit and this read, is reported and gives no alias: later steps then refuse."""
    out: Dict[str, Any] = dict(extra)
    got = cx.read(mid)
    f = got[0] if got is not None else None
    own = set(dream._OWN_KEYS)
    exp_meta = {k: v for k, v in _canon(cx, {**m.metadata, **meta_patch}).items() if k not in own}
    exact = (f is not None and isinstance(res, dict) and f.content == m.content == res.get("content")
             and sorted(f.tags) == sorted(res.get("tags") or []) and f.updated_at == res.get("updated_at")
             and {k: v for k, v in _canon(cx, f.metadata).items() if k not in own} == exp_meta
             and (step["op"] == "set_project" or set(m.tags) <= set(f.tags)))
    if exact:
        cx.alias.setdefault((mid, step["expect"].get(str(mid))), set()).add(f.state_sha)
        cx.save_journal()
        if step["op"] == "set_project" and sorted(f.tags) != sorted(m.tags):
            out["tags_retargeted"] = sorted(set(f.tags) ^ set(m.tags))
    else:
        out["anomaly"] = "the row after the write is not exactly what the write produced; later steps on it will refuse"
    return {"applied": out}


def _guarded_edge(conn: Any, st: Any, holder: int, peer: int, et: str, raw_h: Dict[str, Any],
                  raw_p: Dict[str, Any]) -> bool:
    """Write `holder --et--> peer` in ONE statement that also requires BOTH endpoints to still be exactly the rows
    read (content, metadata, tags, updated_at, crossrefs blob), neither tombstoned, neither already hidden by a newer memory.
    A concurrent edit, a retirement or a typed edge added since the read makes it write nothing (False)."""
    exists = conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id = ?", (holder,)).fetchone()
    cur = [r for r in dream._json(raw_h["related"], []) if isinstance(r, dict) and r.get("id") != peer]
    cur.append({"id": peer, "score": 1.0, "edge_type": et})
    blob = json.dumps(cur, ensure_ascii=False)
    g_h, p_h = dream._state_guard(st, holder, raw_h, True)
    g_p, p_p = dream._state_guard(st, peer, raw_p, True)
    if exists is not None:
        res = conn.execute("UPDATE memories_crossrefs SET related = ? WHERE memory_id = ? AND related IS ? AND "
                           + g_h + " AND " + g_p, (blob, holder, raw_h["related"], *p_h, *p_p))
    else:
        res = conn.execute("INSERT INTO memories_crossrefs(memory_id, related) SELECT ?, ? WHERE " + g_h + " AND " + g_p,
                           (holder, blob, *p_h, *p_p))
    conn.commit()
    return getattr(res, "rowcount", 0) == 1


def _same_row(x: Dict[str, Any], y: Dict[str, Any], keys: Sequence[str]) -> bool:
    return all(x.get(k) == y.get(k) for k in keys)


def _do_link(cx: _Ctx, step: Dict[str, Any]) -> Dict[str, Any]:
    a, b, et = step["a"], step["b"], step["edge_type"]
    ga, gb = cx.read(a), cx.read(b)
    if ga is None or gb is None:
        return {"skip": "an endpoint is gone"}
    (ma, raw_a), (mb, raw_b) = ga, gb
    rev = cx.st._get_reverse_edge_type(et)
    fa = [r for r in ma.refs if r.get("id") == b]
    fb = [r for r in mb.refs if r.get("id") == a]
    for ref, want in ((fa, et), (fb, rev)):
        for r in ref:
            if r.get("edge_type") not in (want, "related_to"):     # the automatic similarity edge is replaced, as memory_link does
                return {"skip": f"a different edge already links {a} and {b} ({r.get('edge_type')!r}); not overwritten"}
    need_fwd = not any(r.get("edge_type") == et for r in fa)
    need_rev = not any(r.get("edge_type") == rev for r in fb)
    if not (need_fwd or need_rev):
        return {"skip": f"already done: the {et} edge exists on both sides"}
    if not (cx.sha_ok(a, ma, step) and cx.sha_ok(b, mb, step)):
        return {"skip": "an endpoint changed since it was judged (state sha mismatch)"}
    if cx.retired(a) or cx.retired(b):
        return {"skip": "an endpoint is retired"}
    why = cx.reserve("link", int(need_fwd) + int(need_rev))
    if why:
        return {"defer": why}
    if need_fwd and not cx.W.run("guarded_edge", lambda: _guarded_edge(cx.conn, cx.st, a, b, et, raw_a, raw_b)):
        return {"skip": "an endpoint changed at the write; nothing written"}
    if need_rev:
        ga2, gb2 = cx.read(a), cx.read(b)
        keys = ("content", "metadata", "tags", "updated_at")
        if ga2 is None or gb2 is None or not _same_row(ga2[1], raw_a, keys) or not _same_row(gb2[1], raw_b, keys) \
                or gb2[1]["related"] != raw_b["related"] \
                or not any(r.get("id") == b and r.get("edge_type") == et for r in ga2[0].refs):
            return {"skip": "partial: an endpoint changed after the forward edge; the reverse edge was not written "
                            "(a rerun completes it unless someone else changed an endpoint: then a new plan is needed)"}
        if not cx.W.run("guarded_edge", lambda: _guarded_edge(cx.conn, cx.st, b, a, rev, gb2[1], ga2[1])):
            return {"skip": "partial: an endpoint changed at the reverse write; only the forward edge exists "
                            "(a rerun completes it)"}
    cx.W.run("log", lambda: cx.st._log_action(cx.conn, a, "link", f"Linked #{a} -> #{b} ({et}) by tidy"))
    cx.W.run("commit", cx.conn.commit)
    return {"applied": {"edge": et, "directions": int(need_fwd) + int(need_rev)}}


# --------------------------------------------------------------------------- the project digest (design 6.4)
#
# Every mutation below is ONE statement that carries the operation's expected state (the rows involved, still exactly
# as read; the live digests of the project; for the reverse edge the distinct state after our own forward edge), so a
# change between a check and the write makes it write nothing. The checks before it only choose what to try.


def _live_digests(cx: _Ctx) -> List[int]:
    """The live digests of the project NOW: kind digest, this project, not retired by any source the store honours."""
    rows = cx.W.run("read", lambda: cx.conn.execute(
        "SELECT id FROM memories WHERE json_extract(metadata, '$.kind') = 'digest' "
        "AND json_extract(metadata, '$.project') = ? ORDER BY id", (cx.project,)).fetchall())
    return [int(r[0]) for r in rows if not cx.retired(int(r[0]))]


def _retired_sql(cx: _Ctx, col: str) -> str:
    """SQL: the memory whose id is the expression `col` is retired by any source `dream._retired` honours: a tombstone,
    a `supersedes` edge held by another memory, or a `superseded_by` entry naming a memory that exists. ONE definition,
    used by every statement below, so the checks and the writes cannot disagree."""
    return ("(EXISTS (SELECT 1 FROM tombstones t WHERE t.memory_id = " + col + ") "
            "OR EXISTS (SELECT 1 FROM tombstone_components t2 WHERE t2.memory_id = " + col + ") "
            "OR EXISTS (" + cx.st._SUPERSEDES_EDGE_TO_SQL.replace("?", col) + ") "
            "OR EXISTS (SELECT 1 FROM memories_crossrefs rc, json_each(CASE WHEN json_valid(rc.related) "
            "THEN rc.related ELSE '[]' END) rj WHERE rc.memory_id = " + col + " "
            "AND (CASE WHEN rj.type = 'object' THEN json_extract(rj.value, '$.edge_type') END) = 'superseded_by' "
            "AND EXISTS (SELECT 1 FROM memories rx WHERE rx.id = "
            "(CASE WHEN rj.type = 'object' THEN CAST(json_extract(rj.value, '$.id') AS INTEGER) END))))")


def _live_only(cx: _Ctx, allowed: Sequence[int]) -> Tuple[str, tuple]:
    """SQL predicate: the project has no live digest outside `allowed`."""
    ph = ",".join("?" for _ in allowed) or "-1"
    sql = ("NOT EXISTS (SELECT 1 FROM memories d WHERE json_extract(d.metadata, '$.kind') = 'digest' "
           "AND json_extract(d.metadata, '$.project') = ? AND d.id NOT IN (" + ph + ") AND NOT "
           + _retired_sql(cx, "d.id") + ")")
    return sql, (cx.project, *allowed)


def _cites_live(cx: _Ctx, cites: Sequence[Any]) -> Tuple[str, tuple]:
    """SQL predicate: every cited memory is still a live memory of the project, checked IN the statement that creates
    the digest (a citation retired or moved while the embedding was computed makes the insert write nothing)."""
    ids = sorted({int(c) for c in cites})
    ph = ",".join("?" for _ in ids)
    return ("(SELECT count(*) FROM memories m WHERE m.id IN (" + ph + ") AND json_extract(m.metadata, '$.project') = ? "
            "AND NOT " + _retired_sql(cx, "m.id") + ") = ?", (*ids, cx.project, len(ids)))


def _only_superseder(cx: _Ctx, win: int, lose: int) -> Tuple[str, tuple]:
    """SQL predicate: `win` holds a `supersedes` edge to `lose` and nobody else does (the reverse-phase state)."""
    src = cx.st._SUPERSEDES_EDGE_TO_SQL.replace("SELECT 1 FROM", "SELECT c.memory_id AS src FROM", 1)
    return (f"EXISTS (SELECT 1 FROM ({src}) s WHERE s.src = ?) AND NOT EXISTS (SELECT 1 FROM ({src}) s2 "
            "WHERE s2.src != ?)", (lose, win, lose, win))


def _digest_edge(cx: _Ctx, holder: int, peer: int, et: str, guards: Sequence[Tuple[str, tuple]]) -> bool:
    """Write `holder --et--> peer` onto holder's crossrefs row in ONE statement that also requires every guard
    predicate. False = a guard refused, nothing written."""
    conn = cx.conn
    exists = conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id = ?", (holder,)).fetchone()
    raw = exists[0] if exists else None
    cur = [r for r in dream._json(raw, []) if isinstance(r, dict) and r.get("id") != peer]
    cur.append({"id": peer, "score": 1.0, "edge_type": et})
    blob = json.dumps(cur, ensure_ascii=False)
    g_sql = " AND ".join(g[0] for g in guards)
    g_params = tuple(p for g in guards for p in g[1])
    if exists is not None:
        res = conn.execute("UPDATE memories_crossrefs SET related = ? WHERE memory_id = ? AND related IS ? AND " + g_sql,
                           (blob, holder, raw, *g_params))
    else:
        res = conn.execute("INSERT INTO memories_crossrefs(memory_id, related) SELECT ?, ? WHERE " + g_sql,
                           (holder, blob, *g_params))
    conn.commit()
    return getattr(res, "rowcount", 0) == 1


def _is_digest(cx: _Ctx, m: Any) -> bool:
    return m.metadata.get("kind") == "digest" and m.metadata.get("project") == cx.project


def _is_ours(cx: _Ctx, m: Any, step: Dict[str, Any]) -> bool:
    """This very step's digest: the sealed metadata, tags and body (and our own create marker), nothing materially edited."""
    return (_is_digest(cx, m) and all(m.metadata.get(k) == v for k, v in step["metadata"].items())
            and m.metadata.get("indexing") == {"skip_crossrefs": True}
            and set(step["tags"]) <= set(m.tags) and m.content.strip() == step["body"].strip())


def _complete(cx: _Ctx, mid: int) -> bool:
    """The create finished: the vector and (when the store has one) the FTS row exist. `add_memory` inserts the row
    first, so a failure after it leaves a row without them."""
    def chk() -> bool:
        if cx.conn.execute("SELECT 1 FROM memories_embeddings WHERE memory_id = ?", (mid,)).fetchone() is None:
            return False
        if cx.st._fts_enabled(cx.conn):
            return cx.conn.execute("SELECT 1 FROM memories_fts WHERE rowid = ?", (mid,)).fetchone() is not None
        return True
    return bool(cx.W.run("read", chk))


def _complete_it(cx: _Ctx, step: Dict[str, Any], mid: int, guards: Sequence[Tuple[str, tuple]] = ()) -> Optional[str]:
    """Finish a partial create of OUR digest through the store's own reindex (vector and FTS; no text change). The row
    is read now, must be this step's sealed digest, and the reindex is guarded by that exact row (expected_row) AND by
    the operation's `guards` (citations, live digests, the expected old digest), carried in the write itself: the
    reindex computes its embedding BEFORE that statement, so a check before the call would not cover it. None = complete; else the reason."""
    if _complete(cx, mid):
        return None
    got = cx.read(mid)
    if got is None or not _is_ours(cx, got[0], step):
        return f"digest {mid} is no longer this step's sealed digest; not completed"
    try:
        guard = (" AND ".join(g[0] for g in guards), tuple(p for g in guards for p in g[1])) if guards else None
        cx.W.run("update_memory", lambda: cx.st.update_memory(
            cx.conn, mid, force_reindex=True, expected_row=got[1], update_guard=guard))
    except cx.st.ConcurrentUpdateError:
        return f"digest {mid} (or a citation, or the live digests) changed during the completion; refused"
    return None if _complete(cx, mid) else f"digest {mid} could not be completed"


def _cite_problem(cx: _Ctx, step: Dict[str, Any]) -> Optional[str]:
    """Citations must be real, live memories of the project NOW. A symbolic id (@c3) names a merge result, and merges
    are not applied here, so it can never be resolved. The body is never rewritten: the step is refused."""
    syms = sorted({c for c in step["cites"] if isinstance(c, str)}
                  | {x for x in tidy.symbols_in(step["body"]) if x != step.get("create")})
    if syms:
        return f"refused: cites a planned create ({', '.join(syms)}); merges are not applied, so it cannot be resolved"
    for c in sorted(set(step["cites"])):
        got = cx.read(c)
        if got is None:
            return f"refused: cited memory {c} is gone"
        if cx.retired(c):
            return f"refused: cited memory {c} is retired"
        if got[0].metadata.get("project") != cx.project:
            return f"refused: cited memory {c} is not in project {cx.project!r}"
    return None


def _edge_flags(cx: _Ctx, win: int, lose: int) -> Optional[Tuple[bool, bool]]:
    """(forward edge exists, reverse edge exists); None when a digest is gone."""
    gw, gl = cx.read(win), cx.read(lose)
    if gw is None or gl is None:
        return None
    return (any(r.get("id") == lose and r.get("edge_type") == "supersedes" for r in gw[0].refs),
            any(r.get("id") == win and r.get("edge_type") == "superseded_by" for r in gl[0].refs))


def _snapshots(cx: _Ctx, step: Dict[str, Any], win: int, lose: int, check: Callable[[Any, Any], Optional[str]]):
    """ONE read of both digests, validated against their authority (`check`: the sealed expectation for a repair, this
    step's own sealed create for a replacement; the loser always against its judged sha). The raw rows returned are
    the very rows that were validated, so the statement guards authorise nothing else. str = the reason to stop."""
    gw, gl = cx.read(win), cx.read(lose)
    if gw is None or gl is None:
        return "a digest is gone; no edge written"
    if not (_is_digest(cx, gw[0]) and _is_digest(cx, gl[0])):
        return "an endpoint is not a digest of this project; no edge written"
    if not cx.sha_ok(lose, gl[0], step):
        return f"digest {lose} changed since it was judged; no edge written"
    bad = check(gw[0], gl[0])
    if bad:
        return bad
    if not _complete(cx, win):
        return f"digest {win} is not completely written (no vector or index); no edge written"
    return gw, gl


def _edges(cx: _Ctx, step: Dict[str, Any], win: int, lose: int, check: Callable[[Any, Any], Optional[str]],
           extra: Sequence[Tuple[str, tuple]] = ()) -> Dict[str, Any]:
    """`win` supersedes `lose`: the forward edge held by the winner, then the reverse edge on the loser. Both rows are
    validated (`_snapshots`) and the SAME snapshot feeds the statement guards; the validation is repeated for the
    reverse phase on a fresh snapshot. Forward = both rows as validated, no superseder, no live digest outside the pair;
    reverse = the winner is the loser's only superseder, winner unchanged and unhidden, only the winner live. Edges that
    already exist are not rewritten."""
    snap = _snapshots(cx, step, win, lose, check)
    if isinstance(snap, str):
        return {"partial": snap}
    (mw, raw_w), (ml, raw_l) = snap
    fwd = any(r.get("id") == lose and r.get("edge_type") == "supersedes" for r in mw.refs)
    rev = any(r.get("id") == win and r.get("edge_type") == "superseded_by" for r in ml.refs)
    out: Dict[str, Any] = {}
    if not fwd:
        if cx.retired(lose):
            return {"partial": f"digest {lose} is already hidden by another memory; no edge written"}
        guards = [dream._state_guard(cx.st, lose, raw_l, True), dream._state_guard(cx.st, win, raw_w, True),
                  _live_only(cx, [win, lose]), *extra]
        if not cx.W.run("guarded_edge", lambda: _digest_edge(cx, win, lose, "supersedes", guards)):
            return {"partial": "the forward edge was refused (the state changed at the write); both digests stay "
                               "live and the next dry run seals a repair_digest"}
        out["forward"] = True
        snap = _snapshots(cx, step, win, lose, check)
        if isinstance(snap, str) or not _same_row(snap[1][1], raw_l, ("content", "metadata", "tags", "updated_at", "related")):
            out["partial"] = "a digest changed after the forward edge; the reverse edge was not written"
            return out
        (mw, raw_w), (ml, raw_l) = snap
    if not rev:
        guards = [dream._state_guard(cx.st, lose, raw_l, False), dream._state_guard(cx.st, win, raw_w, True),
                  _only_superseder(cx, win, lose), _live_only(cx, [win])]
        if not cx.W.run("guarded_edge", lambda: _digest_edge(cx, lose, win, "superseded_by", guards)):
            out["partial"] = "the reverse edge was refused (the state changed); the forward edge alone hides the old digest"
            return out
        out["reverse"] = True
    if out:
        cx.W.run("log", lambda: cx.st._log_action(cx.conn, win, "link", f"Digest #{win} supersedes #{lose} by tidy"))
        cx.W.run("commit", cx.conn.commit)
    return out


def _replace_check(cx: _Ctx, step: Dict[str, Any], win: int) -> Callable[[Any, Any], Optional[str]]:
    return lambda mw, ml: None if _is_ours(cx, mw, step) else \
        f"digest {win} is no longer this step's sealed digest; no edge written"


def _created(cx: _Ctx, step: Dict[str, Any], guards: Sequence[Tuple[str, tuple]]) -> Dict[str, Any]:
    """Create the digest through the store's own create path: its embedding is computed like any create (the one
    place this apply needs the embedding environment). No absorb, no dedup, no suggestion, no model: `add_memory`
    is the storage function, the similarity crossrefs are switched off for this row (`indexing.skip_crossrefs`), and
    the row insert itself carries the expected live-digest state (`insert_guard`), so a digest that appeared while the
    embedding was computed makes it write nothing. {"id"}, {"refused"} or {"partial": id}."""
    md = {**step["metadata"], "indexing": {"skip_crossrefs": True}}
    sql = " AND ".join(g[0] for g in guards)
    params = tuple(p for g in guards for p in g[1])
    try:
        new = cx.W.run("add_memory", lambda: cx.st.add_memory(
            cx.conn, content=step["body"], metadata=md, tags=list(step["tags"]), project=cx.project,
            insert_guard=(sql, params)))
    except cx.st.CreateGuardRefused:
        return {"refused": "stale: the live digests or a citation changed before the insert; nothing was written"}
    except cx.st.MemoryWriteError as e:
        cx.created[step["id"]] = int(e.memory_id)
        return {"partial": int(e.memory_id)}
    nid = int(new["id"] if "id" in new else new["memory"]["id"])
    cx.created[step["id"]] = nid
    try:
        cx.save_journal()           # informational: the rerun finds the digest in the store, not here
    except OSError:
        pass
    return {"id": nid}


def _create_result(cx: _Ctx, got: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if "refused" in got:
        return {"skip": got["refused"]}
    if "partial" in got:
        return {"skip": f"partial create: digest {got['partial']} was inserted but not completed (counted as written); "
                        "a rerun completes it and nothing is hidden until then"}
    return None


def _do_digest(cx: _Ctx, step: Dict[str, Any]) -> Dict[str, Any]:
    op, ids = step["op"], _live_digests(cx)
    if op == "repair_digest":
        return _do_repair(cx, step, ids)
    mine = [i for i in ids if (g := cx.read(i)) is not None and _is_ours(cx, g[0], step)]
    if len(mine) > 1:
        return {"skip": f"ambiguous: several live digests match this step ({mine}); a leader decides"}
    old = step.get("supersedes")
    if op == "bootstrap_digest":
        if mine:
            if ids != mine:
                return {"skip": f"stale: the project has other live digest(s) {ids}"}
            if _complete(cx, mine[0]):
                return {"skip": f"already done: digest {mine[0]} of this run is the live digest"}
            bad = _cite_problem(cx, step)             # completing makes it searchable: the citations must hold now
            if bad:
                return {"skip": bad + "; the incomplete digest is not completed"}
            why = cx.reserve(op)
            if why:
                return {"defer": why}
            bad = _complete_it(cx, step, mine[0], [_live_only(cx, [mine[0]]), _cites_live(cx, step["cites"])])
            return {"skip": bad} if bad else {"applied": {"completed": mine[0]}}
        if ids:
            return {"skip": f"stale: the project already has live digest(s) {ids}; a bootstrap needs none"}
        bad = _cite_problem(cx, step)
        if bad:
            return {"skip": bad}
        why = cx.reserve(op)
        if why:
            return {"defer": why}
        got = _created(cx, step, [_live_only(cx, []), _cites_live(cx, step["cites"])])
        return _create_result(cx, got) or {"applied": {"created": got["id"]}}
    # replace_digest
    if mine:
        d = mine[0]
        if ids not in ([d], sorted([d, old])):
            return {"skip": f"stale: the live digests are {ids}, the step was sealed against [{old}] and its own digest"}
        flags = _edge_flags(cx, d, old)
        if flags is None:
            return {"skip": f"digest {old} is gone"}
        if all(flags) and _complete(cx, d):
            return {"skip": f"already done: digest {d} of this run is live and supersedes {old}"}
        bad = _cite_problem(cx, step)             # a resume revalidates every citation before it hides anything
        if bad:
            return {"skip": bad}
        extra = 0 if _complete(cx, d) else 1
        why = cx.reserve(op, extra + int(not flags[0]) + int(not flags[1]))
        if why:
            return {"defer": why}
        if extra:
            got_old = cx.read(old)
            if got_old is None:
                return {"skip": f"digest {old} is gone; nothing hidden"}
            bad = _complete_it(cx, step, d, [dream._state_guard(cx.st, old, got_old[1], True), _live_only(cx, [d, old]),
                                             _cites_live(cx, step["cites"])])
            if bad:
                return {"skip": bad + "; nothing hidden"}
        return {"applied": {"created": None, "resumed": d, "superseded": old,
                            **_edges(cx, step, d, old, _replace_check(cx, step, d),
                                     [_cites_live(cx, step["cites"])])}}
    if ids != [old]:
        return {"skip": f"stale: the live digests are {ids}, the step was sealed against [{old}]"}
    got_old = cx.read(old)
    if got_old is None or not _is_digest(cx, got_old[0]):
        return {"skip": f"digest {old} is not a digest of this project"}
    if not cx.sha_ok(old, got_old[0], step):
        return {"skip": f"digest {old} changed since it was judged (state sha mismatch)"}
    bad = _cite_problem(cx, step)
    if bad:
        return {"skip": bad}
    why = cx.reserve(op)
    if why:
        return {"defer": why}
    got = _created(cx, step, [dream._state_guard(cx.st, old, got_old[1], True), _live_only(cx, [old]),
                              _cites_live(cx, step["cites"])])
    res = _create_result(cx, got)
    if res:
        return res
    nid = got["id"]
    return {"applied": {"created": nid, "superseded": old, **_edges(cx, step, nid, old, _replace_check(cx, step, nid),
                                                                  [_cites_live(cx, step["cites"])])}}


def _do_repair(cx: _Ctx, step: Dict[str, Any], ids: List[int]) -> Dict[str, Any]:
    """Both digests are re-read and every expectation is checked again, also when only the winner is live (a resume):
    both judged rows, both tidy-made, the winner later. Creates nothing; writes the missing edges only."""
    win, lose = step["winner"], step["supersedes"]
    if ids not in ([win], sorted([win, lose])):
        return {"skip": f"stale: the live digests are {ids}, the step was sealed against {sorted([win, lose])}"}
    flags = _edge_flags(cx, win, lose)
    if flags is None:
        return {"skip": "a digest is gone"}
    if all(flags):
        return {"skip": f"already done: digest {win} supersedes {lose} on both sides"}
    def check(mw: Any, ml: Any) -> Optional[str]:        # the sealed expectation, applied to the snapshot that is written against
        if not (mw.metadata.get("digest_of") == "tidy" == ml.metadata.get("digest_of")):
            return "a digest was not made by tidy: not repaired automatically"
        if (mw.created_at or "", win) <= (ml.created_at or "", lose):
            return f"digest {win} is not the later one; not repaired"
        if not cx.sha_ok(win, mw, step):
            return f"digest {win} changed since it was judged (state sha mismatch)"
        return None

    why = cx.reserve("repair_digest", int(not flags[0]) + int(not flags[1]))
    if why:
        return {"defer": why}
    out = _edges(cx, step, win, lose, check)
    if "forward" not in out and "reverse" not in out:
        return {"skip": out.get("partial", "nothing written")}
    return {"applied": {"superseded": lose, **out}}


_DO: Dict[str, Callable[[_Ctx, Dict[str, Any]], Dict[str, Any]]] = {
    "set_project": _do_set_project, "link": _do_link, "retag": _do_retag,
    "bootstrap_digest": _do_digest, "replace_digest": _do_digest, "repair_digest": _do_digest}


def apply_report(plan: Dict[str, Any], dec: Dict[str, Any], lim: Any, storage_mod: Any = None,
                 clock: Callable[[], float] = time.monotonic, journal: Optional[Path] = None) -> Dict[str, Any]:
    """Apply the approved, supported steps of a verified report. Add-only metadata writes through the store's own
    functions: no content change, no embedding, no model, no delete, no hiding edge."""
    if storage_mod is None:
        from . import storage as storage_mod  # noqa: PLC0415
    sel = select(plan, dec)
    skipped: List[Dict[str, Any]] = list(sel["skipped"])
    applied: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    aborted: Optional[str] = None
    W = dream._Writer(clock, clock(), lim.max_seconds)
    cx: Optional[_Ctx] = None
    conn: Any = None
    cur: Optional[Dict[str, Any]] = None
    done_ids: set = set()
    try:
        conn = W.run("open", lambda: storage_mod.connect(check_same_thread=False))
        cx = _Ctx(W, storage_mod, conn, lim)
        cx.journal, cx.digest = journal, plan.get("digest")
        cx.project = plan.get("project")
        cx.load_journal()
        if journal is not None:
            try:
                cx.save_journal()        # fail before any write when the replay journal cannot be written
            except OSError as e:
                raise TidyError(f"the replay journal {journal} is not writable ({e}); nothing was written")
        for st in sel["run"]:
            cur = st
            row = {"id": st["id"], "op": st["op"]}
            dep = [d for d in st.get("depends_on", []) if d not in done_ids]
            if dep and st["op"] in DIGEST_OPS:      # never rewritten, never run on a result that did not commit (6.3)
                deferred.append({**row, "reason": f"deferred: dependency {', '.join(map(str, dep))}"})
                continue
            if dep:
                skipped.append({**row, "reason": f"dependency not applied: {', '.join(map(str, dep))}"})
                continue
            try:
                res = _DO[st["op"]](cx, st)
            except dream._Deadline:
                raise
            except Exception as e:      # one failure never blocks the rest; a started write is counted as written
                skipped.append({**row, "reason": f"write failed (counted as written): {type(e).__name__}: {e}"[:300]})
                continue
            if "applied" in res:
                applied.append({**row, **res["applied"], "approved_by": st["_approved"]})
                done_ids.add(st["id"])
            elif "defer" in res:
                deferred.append({**row, "reason": res["defer"]})
            else:
                skipped.append({**row, "reason": res["skip"]})
                if res["skip"].startswith("already done"):
                    done_ids.add(st["id"])
    except dream._Deadline as d:
        aborted = "max_seconds"
        if d.started:
            ambiguous.append({"id": cur["id"] if cur else None, "op": cur["op"] if cur else d.op, "call": d.op,
                              "why": "in flight when the deadline passed; it may still commit"})
        # the steps never reached are deferred, not lost; the ambiguous one is neither
        reached = {r["id"] for r in applied + skipped + deferred + ambiguous}
        deferred.extend({"id": s["id"], "op": s["op"], "reason": "max_seconds"} for s in sel["run"]
                        if s["id"] not in reached)
    finally:
        if not W.stuck and conn is not None:
            try:
                W.run("close", conn.close)
            except Exception:  # noqa: BLE001
                pass
    already = sum(1 for s in skipped if s["reason"].startswith("already done"))
    by_op: Dict[str, int] = {}
    for a in applied:
        by_op[a["op"]] = by_op.get(a["op"], 0) + 1
    return {"mode": "tidy-apply", "plan_run": plan.get("run"), "report_digest": plan.get("digest"),
            "aborted": aborted, "ambiguous": ambiguous,
            "counts": {"steps": len(plan.get("steps", [])), "applied": len(applied), "applied_by_op": by_op,
                       "skipped": len(skipped), "already_done": already, "deferred": len(deferred),
                       "ambiguous": len(ambiguous), "writes_charged": cx.writes if cx else 0,
                       "backfill_charged": cx.backfill if cx else 0,
                       "anomalies": sum(1 for a in applied if "anomaly" in a)},
            "limits": {"max_writes": lim.max_writes, "max_backfill": lim.max_backfill, "max_seconds": lim.max_seconds},
            "applied": applied, "skipped": skipped, "deferred": deferred, "dispatch_log": W.log}


# --------------------------------------------------------------------------- CLI


def result_path(plan_path: str, now: Optional[float] = None) -> Path:
    stamp = _dt.datetime.fromtimestamp(time.time() if now is None else now, _dt.timezone.utc)
    p = Path(plan_path)
    return p.with_name(f"{p.stem}.apply-{stamp:%Y%m%dT%H%M%SZ}.json")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="memora-tidy --apply", description=(
        "Apply the metadata-only steps (set_project, link, add-only retag) of a sealed tidy report. No model, "
        "no git. Writes the configured store: run it as docs/tidy.md describes."))
    ap.add_argument("--apply", action="store_true", required=True)
    ap.add_argument("--plan", required=True, help="the tidy dry-run report")
    ap.add_argument("--decisions", required=True, help="the decisions file (report_digest, approve, reject, approve_ops)")
    ap.add_argument("--config", help=f"JSON config (default $MEMORA_TIDY_CONFIG or {tidy.DEFAULT_CONFIG})")
    ap.add_argument("--check", action="store_true",
                    help="parse the report and decisions and print what would be attempted; no lock, no store")
    ap.add_argument("--max-age-hours", type=float, default=48.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        plan = tidy.load_tidy_plan(args.plan, max_age_hours=args.max_age_hours)
        dec = load_decisions(args.decisions, plan)
        if args.check:
            print(json.dumps(dry_counts(plan, dec), indent=2))
            return 0
        cfg = tidy.TidyConfig.load(args.config)
        with dream.run_lock(cfg.state_dir):
            dream.lower_priority(cfg.limits.nice)
            out = apply_report(plan, dec, cfg.limits, journal=Path(str(args.plan) + ".applied.json"))
            path = result_path(args.plan)
            dream.write_report(str(path), out)
            print(json.dumps({"result": str(path), "aborted": out["aborted"], **out["counts"]}, indent=2))
            return 4 if out["aborted"] else 0
    except DreamError as e:
        print(f"memora-tidy: {e}", file=sys.stderr)
        return 2
