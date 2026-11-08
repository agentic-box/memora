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
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import dream, tidy
from .dream import DreamError
from .tidy import TidyError

SUPPORTED_OPS = ("set_project", "link", "retag")
KNOWN_OPS = SUPPORTED_OPS + ("merge", "retire", "bootstrap_digest", "replace_digest", "repair_digest")
CONF_RANK = {"low": 1, "medium": 2, "high": 3}
CHARGE = {"set_project": 1, "link": 2, "retag": 1}      # store writes per step (the design's charges)
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
        if not isinstance(r, dict) or set(r) - {"op", "min_confidence"} or r.get("op") not in KNOWN_OPS:
            raise TidyError("each approve_ops entry must be {\"op\": <known op>[, \"min_confidence\": high|medium|low]}")
        if "min_confidence" in r and r["min_confidence"] not in CONF_RANK:
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


def shape_problem(step: Dict[str, Any]) -> Optional[str]:
    """Structure of a supported step, before anything is read from the store."""
    op = step.get("op")
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
        elif (bad := shape_problem(st)) is not None:
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
        self.alias: Dict[Tuple[int, str], str] = {}     # (id, sha judged in the plan) -> sha after OUR write

    def reserve(self, op: str) -> Optional[str]:
        if self.W.stuck or self.W.remaining() <= 0:
            return "max_seconds"
        n = CHARGE[op]
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
        return m.state_sha == want or self.alias.get((mid, want)) == m.state_sha


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
    before_tags = list(m.tags)
    res = cx.W.run("update_memory", lambda: cx.st.update_memory(
        cx.conn, mid, metadata={"project": proj}, expected_row=raw, embed=False))
    return _after(cx, step, mid, m, before_tags, res, {"project": proj})


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
    why = cx.reserve("retag")
    if why:
        return {"defer": why}
    before_tags = list(m.tags)
    res = cx.W.run("update_memory", lambda: cx.st.update_memory(
        cx.conn, mid, tags=before_tags + missing, expected_row=raw, embed=False))
    extra = {"added": missing}
    if step.get("remove"):
        extra["remove_ignored"] = list(step["remove"])
    return _after(cx, step, mid, m, before_tags, res, extra)


def _after(cx: _Ctx, step: Dict[str, Any], mid: int, m: Any, before_tags: List[str], res: Any,
           extra: Dict[str, Any]) -> Dict[str, Any]:
    """Record the memory's new state sha as 'ours' so later steps of the same plan still match, and check that the
    write kept the text and every old tag."""
    got = cx.read(mid)
    out: Dict[str, Any] = dict(extra)
    if got is not None:
        want = step["expect"].get(str(mid))
        cx.alias[(mid, want)] = got[0].state_sha
        if got[0].content != m.content:
            out["anomaly"] = "content changed"
        elif not set(before_tags) <= set(got[0].tags):
            out["anomaly"] = "a tag was lost"
        elif step["op"] == "set_project" and sorted(got[0].tags) != sorted(before_tags):
            out["tags_retargeted"] = sorted(set(got[0].tags) ^ set(before_tags))
    return {"applied": out}


def _do_link(cx: _Ctx, step: Dict[str, Any]) -> Dict[str, Any]:
    a, b, et = step["a"], step["b"], step["edge_type"]
    ga, gb = cx.read(a), cx.read(b)
    if ga is None or gb is None:
        return {"skip": "an endpoint is gone"}
    (ma, _), (mb, _) = ga, gb
    rev = cx.st._get_reverse_edge_type(et)
    fa = [r for r in ma.refs if r.get("id") == b]
    fb = [r for r in mb.refs if r.get("id") == a]
    for ref, want in ((fa, et), (fb, rev)):
        for r in ref:
            if r.get("edge_type") not in (want, "related_to"):     # the automatic similarity edge is replaced, as memory_link does
                return {"skip": f"a different edge already links {a} and {b} ({r.get('edge_type')!r}); not overwritten"}
    if all(r.get("edge_type") == want for ref, want in ((fa, et), (fb, rev)) for r in ref) and fa and fb:
        return {"skip": f"already done: the {et} edge exists on both sides"}
    if not (cx.sha_ok(a, ma, step) and cx.sha_ok(b, mb, step)):
        return {"skip": "an endpoint changed since it was judged (state sha mismatch)"}
    if cx.retired(a) or cx.retired(b):
        return {"skip": "an endpoint is retired"}
    why = cx.reserve("link")
    if why:
        return {"defer": why}
    cx.W.run("add_link", lambda: cx.st.add_link(cx.conn, a, b, edge_type=et))
    return {"applied": {"edge": et}}


_DO: Dict[str, Callable[[_Ctx, Dict[str, Any]], Dict[str, Any]]] = {
    "set_project": _do_set_project, "link": _do_link, "retag": _do_retag}


def apply_report(plan: Dict[str, Any], dec: Dict[str, Any], lim: Any, storage_mod: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
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
    done_ids: set = set()
    try:
        conn = W.run("open", lambda: storage_mod.connect(check_same_thread=False))
        cx = _Ctx(W, storage_mod, conn, lim)
        for st in sel["run"]:
            row = {"id": st["id"], "op": st["op"]}
            dep = [d for d in st.get("depends_on", []) if d not in done_ids]
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
            ambiguous.append({"op": d.op, "why": "in flight when the deadline passed; it may still commit"})
        # the steps never reached are deferred, not lost
        reached = {r["id"] for r in applied + skipped + deferred}
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
            out = apply_report(plan, dec, cfg.limits)
            path = result_path(args.plan)
            dream.write_report(str(path), out)
            print(json.dumps({"result": str(path), "aborted": out["aborted"], **out["counts"]}, indent=2))
            return 4 if out["aborted"] else 0
    except DreamError as e:
        print(f"memora-tidy: {e}", file=sys.stderr)
        return 2
