"""Item 258: the tidy retrieval check (docs/tidy-design.md section 9, docs/tidy.md "Measuring").

``memora-tidy-eval --db COPY [--queries FILE]`` runs a fixed query set through the search the MCP tools use
(``storage.hybrid_search_scored``, ``follow="active"``, top 5), unscoped and, for a query with a project,
scoped by it, and reports recall@5 and MRR. Run it on a copy before and after a tidy apply, then
``memora-tidy-eval --compare before.json after.json``.

Read-only by construction: the copy is opened through ``dream.open_readonly_file`` (``mode=ro``), the search
is called with ``read_only=True`` (no rebuild, no vector repair), and this module never calls the MCP tool
wrapper, so the read ledger is not written. Hybrid search itself never bumps ``access_count`` or
``last_accessed`` (only ``get_memory(track_access=True)`` does, and nothing here calls it). No LLM is called;
the query embedding uses the store's embedding environment, exactly like a search.

Determinism: the same copy and queries give the same output. Ranks are the search path's own order. Inside
the search, the semantic leg orders by score, then ``created_at``, then id; the keyword leg orders by
``created_at``; fusion stable-sorts the UNROUNDED fused scores (the printed score is rounded), so a tie keeps
the order in which the legs listed the memory. All of that is a function of the stored rows. Nothing here
re-sorts, shuffles or samples. The report carries no timestamp except ``created``, which ``--compare``
ignores.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import dream

DEFAULT_QUERIES = "~/.config/memora/tidy-queries.json"
TOP_K = 5
REPORT_VERSION = 1


class EvalError(Exception):
    pass


# --------------------------------------------------------------------------- queries file


def load_queries(path: str) -> List[Dict[str, Any]]:
    p = Path(path).expanduser()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise EvalError(f"cannot read the queries file {p}: {e.strerror or e}")
    except ValueError as e:
        raise EvalError(f"the queries file {p} is not JSON: {e}")
    items = raw.get("queries") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise EvalError(f'the queries file {p} needs {{"queries": [...]}} with at least one query')
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, it in enumerate(items):
        where = f"queries[{i}]"
        if not isinstance(it, dict):
            raise EvalError(f"{where} is not an object")
        q, project, expect = it.get("q"), it.get("project"), it.get("expect")
        digest = it.get("digest", False)
        if not isinstance(q, str) or not q.strip():
            raise EvalError(f"{where}.q must be a non-empty string")
        if project is not None and (not isinstance(project, str) or not project.strip()):
            raise EvalError(f"{where}.project must be a non-empty string or null")
        if not isinstance(expect, list) or any(not isinstance(x, int) or isinstance(x, bool) for x in expect):
            raise EvalError(f"{where}.expect must be a list of memory ids")
        if not isinstance(digest, bool):
            raise EvalError(f"{where}.digest must be true or false")
        if digest and project is None:
            raise EvalError(f"{where}: digest:true needs a project (the digest is per project)")
        if not expect and not digest:
            raise EvalError(f"{where}: expect is empty and digest is not set; the query checks nothing")
        key = (q, project)
        if key in seen:
            raise EvalError(f"{where} repeats an earlier (q, project) pair; --compare keys on that pair")
        seen.add(key)
        out.append({"q": q, "project": project, "expect": list(dict.fromkeys(expect)), "digest": digest})
    return out


# --------------------------------------------------------------------------- the checks


def forward_successors(conn: Any) -> Dict[int, List[int]]:
    """old id -> ids of memories holding a forward-only ``supersedes`` edge to it (no ``superseded_by`` on the
    old one). Storage's follow="active" does not hide such an old memory, but the tidy helpers
    (``tidy.retired_ids``) treat it as retired, so both it and its successor count as a hit."""
    mems = dream.load_memories(conn)
    live = {m.id for m in mems}
    out: Dict[int, List[int]] = {}
    for m in mems:
        for ref in m.refs:
            if ref.get("edge_type") == "supersedes" and ref.get("id") in live and ref.get("id") != m.id:
                out.setdefault(ref["id"], []).append(m.id)
    return out


def accepted_ids(conn: Any, expected: int, fwd: Optional[Dict[int, List[int]]] = None) -> List[int]:
    """The ids that count as a hit for an expected id: itself when live, else its live successors (the leaves
    of its ``superseded_by`` chain, section 9), following forward-only ``supersedes`` edges too (see
    ``forward_successors``). A retired memory with no successor accepts nothing."""
    from . import storage  # noqa: PLC0415
    fwd = forward_successors(conn) if fwd is None else fwd
    seen: set = set()
    stack = [expected]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(storage._resolve_latest(conn, n))
        stack.extend(fwd.get(n, []))
    return sorted(n for n in seen if storage._resolve_latest(conn, n) == [n])


def live_digest_ids(conn: Any, project: str) -> List[int]:
    from . import tidy  # noqa: PLC0415
    mems = dream.load_memories(conn)
    gone = tidy.retired_ids(conn, mems)
    return sorted(m.id for m in tidy.live_digests(mems, project) if m.id not in gone)


def _search(conn: Any, query: str, project: Optional[str]) -> List[int]:
    from . import storage  # noqa: PLC0415
    if not storage._query_embedding(query):
        raise EvalError("the query embedding is unavailable (check the store's embedding environment); "
                        "refusing to report keyword-only results as a retrieval measurement")
    items = storage.hybrid_search_scored(
        conn, query, top_k=TOP_K, min_score=0.0, follow="active", project=project,
        read_only=True, auto_rebuild=False,
    )
    return [it["memory"]["id"] for it in items]


def _rank(ids: Sequence[int], accept: set) -> Optional[int]:
    for r, mid in enumerate(ids, start=1):
        if mid in accept:
            return r
    return None


def run_query(conn: Any, qd: Dict[str, Any], fwd: Optional[Dict[int, List[int]]] = None) -> Dict[str, Any]:
    accept = set()
    for e in qd["expect"]:
        accept.update(accepted_ids(conn, e, fwd))
    res: Dict[str, Any] = {"q": qd["q"], "project": qd["project"], "expect": qd["expect"],
                           "digest_asked": qd["digest"], "accepted": sorted(accept)}
    legs = {"unscoped": None}
    if qd["project"]:
        legs["scoped"] = qd["project"]
    for leg, project in legs.items():
        ids = _search(conn, qd["q"], project)
        if qd["expect"]:
            rank = _rank(ids, accept)
            res[leg] = {"hit": rank is not None, "rank": rank, "ids": ids}
        else:
            res[leg] = {"hit": None, "rank": None, "ids": ids}
    if qd["digest"]:
        digests = live_digest_ids(conn, qd["project"])
        leg = res["scoped"]
        res["digest"] = {"live": digests, "first": (leg["ids"][0] in digests) if digests and leg["ids"] else
                         (False if digests else None)}
    else:
        res["digest"] = None
    return res


def _agg(rows: Sequence[Dict[str, Any]], leg: str) -> Dict[str, Any]:
    rel = [r[leg] for r in rows if leg in r and r[leg]["hit"] is not None]
    n = len(rel)
    if not n:
        return {"n": 0, "hits": 0, "recall_at_5": None, "mrr": None}
    hits = sum(1 for x in rel if x["hit"])
    mrr = sum(1.0 / x["rank"] for x in rel if x["rank"]) / n
    return {"n": n, "hits": hits, "recall_at_5": round(hits / n, 4), "mrr": round(mrr, 4)}


def evaluate(conn: Any, queries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    fwd = forward_successors(conn)
    rows = [run_query(conn, q, fwd) for q in queries]
    dig = [r["digest"]["first"] for r in rows if r["digest"] is not None]
    return {
        "version": REPORT_VERSION,
        "top_k": TOP_K,
        "follow": "active",
        "queries": rows,
        "totals": {
            "unscoped": _agg(rows, "unscoped"),
            "scoped": _agg(rows, "scoped"),
            "digest_first": {"asked": len(dig), "true": sum(1 for d in dig if d is True),
                             "false": sum(1 for d in dig if d is False),
                             "n_a": sum(1 for d in dig if d is None)},
        },
    }


# --------------------------------------------------------------------------- summary and compare


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def summary(rep: Dict[str, Any]) -> str:
    t = rep["totals"]
    lines = []
    for leg in ("unscoped", "scoped"):
        a = t[leg]
        lines.append(f"{leg:9s} recall@5 {_pct(a['recall_at_5'])} ({a['hits']}/{a['n']})  MRR {_pct(a['mrr'])}")
    d = t["digest_first"]
    lines.append(f"digest first: {d['true']} true, {d['false']} false, {d['n_a']} n/a (of {d['asked']} asked)")
    for r in rep["queries"]:
        cells = []
        for leg in ("unscoped", "scoped"):
            if leg in r:
                x = r[leg]
                cells.append(f"{leg} " + ("-" if x["hit"] is None else f"rank {x['rank']}" if x["hit"] else "MISS"))
        lines.append("  " + "; ".join(cells) + "  " + _short(r["q"]))
    return "\n".join(lines)


def _short(q: str, n: int = 50) -> str:
    return q if len(q) <= n else q[: n - 1] + "…"


def _key(r: Dict[str, Any]) -> str:
    return json.dumps([r["q"], r["project"]])


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Per-query deltas (b relative to a) and total deltas. A query in only one report is listed apart."""
    ia = {_key(r): r for r in a["queries"]}
    ib = {_key(r): r for r in b["queries"]}
    deltas = []
    for k in ia:
        if k not in ib:
            continue
        ra, rb = ia[k], ib[k]
        if (ra["expect"], ra.get("digest_asked")) != (rb["expect"], rb.get("digest_asked")):
            raise EvalError(f"the criteria changed between the reports for query {_short(ra['q'])!r} "
                            f"(expect or digest differs); a before/after comparison needs the same queries file")
        d: Dict[str, Any] = {"q": ra["q"], "project": ra["project"]}
        for leg in ("unscoped", "scoped"):
            if leg in ra and leg in rb:
                xa, xb = ra[leg], rb[leg]
                d[leg] = {"hit": [xa["hit"], xb["hit"]], "rank": [xa["rank"], xb["rank"]],
                          "change": _change(xa, xb)}
        if ra.get("digest") and rb.get("digest"):
            d["digest_first"] = [ra["digest"]["first"], rb["digest"]["first"]]
        deltas.append(d)
    tot = {}
    for leg in ("unscoped", "scoped"):
        ta, tb = a["totals"][leg], b["totals"][leg]
        tot[leg] = {m: [ta[m], tb[m], _sub(tb[m], ta[m])] for m in ("recall_at_5", "mrr")}
    return {"queries": deltas, "totals": tot,
            "only_before": [ia[k]["q"] for k in ia if k not in ib],
            "only_after": [ib[k]["q"] for k in ib if k not in ia]}


def _sub(x: Optional[float], y: Optional[float]) -> Optional[float]:
    return None if x is None or y is None else round(x - y, 4)


def _change(xa: Dict[str, Any], xb: Dict[str, Any]) -> str:
    if xa["hit"] is None or xb["hit"] is None:
        return "n/a"
    if xa["hit"] and not xb["hit"]:
        return "lost"
    if xb["hit"] and not xa["hit"]:
        return "gained"
    if not xa["hit"]:
        return "miss"
    if xb["rank"] < xa["rank"]:
        return "better"
    return "worse" if xb["rank"] > xa["rank"] else "same"


def compare_text(c: Dict[str, Any]) -> str:
    lines = []
    for leg in ("unscoped", "scoped"):
        t = c["totals"][leg]
        lines.append(f"{leg:9s} recall@5 {_pct(t['recall_at_5'][0])} -> {_pct(t['recall_at_5'][1])} "
                     f"({_signed(t['recall_at_5'][2])})  MRR {_pct(t['mrr'][0])} -> {_pct(t['mrr'][1])} "
                     f"({_signed(t['mrr'][2])})")
    for d in c["queries"]:
        cells = [f"{leg} {d[leg]['change']} (rank {d[leg]['rank'][0]} -> {d[leg]['rank'][1]})"
                 for leg in ("unscoped", "scoped") if leg in d]
        if "digest_first" in d:
            cells.append(f"digest first {d['digest_first'][0]} -> {d['digest_first'][1]}")
        lines.append("  " + "; ".join(cells) + "  " + _short(d["q"]))
    for k, label in (("only_before", "only in the first report"), ("only_after", "only in the second report")):
        for q in c[k]:
            lines.append(f"  {label}: {_short(q)}")
    return "\n".join(lines)


def _signed(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.3f}"


# --------------------------------------------------------------------------- CLI


def _write_json(path: Optional[str], obj: Dict[str, Any]) -> None:
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    if not path:
        sys.stdout.write(text)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _load_report(path: str) -> Dict[str, Any]:
    try:
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise EvalError(f"cannot read the report {path}: {e}")
    bad = EvalError(f"{path} is not a memora-tidy-eval report")
    if not isinstance(rep, dict) or rep.get("version") != REPORT_VERSION:
        raise bad
    if not isinstance(rep.get("queries"), list) or not isinstance(rep.get("totals"), dict):
        raise bad
    try:
        for r in rep["queries"]:
            if not (isinstance(r["q"], str) and isinstance(r["expect"], list) and "project" in r
                    and isinstance(r.get("digest_asked"), bool)):
                raise bad
            for leg in ("unscoped", "scoped"):
                if leg in r and not (isinstance(r[leg], dict) and {"hit", "rank"} <= set(r[leg])):
                    raise bad
            if not isinstance(r["unscoped"], dict) or (r.get("digest") is not None and not isinstance(r["digest"], dict)):
                raise bad
        for leg in ("unscoped", "scoped"):
            if not {"recall_at_5", "mrr"} <= set(rep["totals"][leg]):
                raise bad
    except (KeyError, TypeError):
        raise bad
    return rep


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="memora-tidy-eval", description=(
        "Retrieval check for the tidy pass (read-only; docs/tidy.md 'Measuring'). Runs a fixed query set "
        "against a READ-ONLY COPY of a store and reports recall@5 and MRR."))
    ap.add_argument("--db", help="a READ-ONLY copy of a store (a sqlite file)")
    ap.add_argument("--queries", help=f"queries file (default {DEFAULT_QUERIES}); keep it out of git")
    ap.add_argument("--out", help="write the JSON report here (0600); default stdout")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="print the deltas between two reports")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.compare:
            c = compare(_load_report(args.compare[0]), _load_report(args.compare[1]))
            if args.out:
                _write_json(args.out, c)
            else:
                _write_json(None, c)
            print(compare_text(c), file=sys.stderr)
            return 0
        if not args.db:
            raise EvalError("no store copy: pass --db (the eval never opens the live store)")
        queries = load_queries(args.queries or DEFAULT_QUERIES)
        try:
            conn = dream.open_readonly_file(args.db)
        except dream.DreamError as e:
            raise EvalError(str(e))
        with contextlib.closing(conn):
            try:
                rep = evaluate(conn, queries)
            except EvalError:
                raise
            except Exception as e:  # SearchUnavailable (model mismatch, integrity fault), a missing table ...
                raise EvalError(f"search failed: {type(e).__name__}: {e}")
        rep["created"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_json(args.out, rep)
        print(summary(rep), file=sys.stderr)
        return 0
    except EvalError as e:
        print(f"memora-tidy-eval: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
