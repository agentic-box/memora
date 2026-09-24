"""The /api/graph payload, assembled exactly as the Pages viewer assembles it
(G4, leader 8094): a port of memora-graph/functions/api/graph.ts
(onRequestGet) and functions/api/_lineage.ts, so memora-all and Pages serve
the same nodes, edges (with edge_type/score/directed), lineage, duplicates,
flags and clusters for the same data. The shared index.html and
force-graph.html then behave the same on both builds.

build_graph_payload() is a pure function over the rows graph.ts reads
(memories, memories_crossrefs, tombstone_components, tombstones, each in the
order D1 returns them). It follows graph.ts statement by statement, keeping
its iteration orders (JS Map/Set insertion order), its numbers (IEEE doubles;
Math.log1p through a table of V8's values where Python's libm differs) and
its strings (lengths and slices in UTF-16 code units, JS whitespace, JS
String() of non-string keys).

Parity means identical output for every input on which Pages itself works
(leader 8135). The differences, all deliberate:

- Rows an unfinished import still marks (metadata ``import_attempt``) are
  not read (graph/data.py): they are not memories yet, and memora-all's
  other APIs hide them too (leader 8129). Pages lists them.
- Pages defects, where this port gives the correct payload instead
  (pages_defects() names them for an input; follow-up PG1 fixes them in
  memora-graph):
  * A tag, section, issue/TODO status, component or category named like an
    Object.prototype property (``constructor``, ``toString``, ``__proto__``,
    ...). graph.ts keeps those maps in plain objects: the key is "already
    there", inherited, so the node gets no tag colour and the .push() on the
    inherited function throws -- /api/graph answers 500. On a document
    fragment's tag (?docs=1), which is never mapped, it does not throw but
    silently leaves the tag out of tagColors (and shifts later tags'
    colours).
  * Malformed values where graph.ts throws (metadata ``null`` on a plain
    memory, tags that are an object or null, a non-string subsection): this
    port treats the value as absent.
"""
from __future__ import annotations

import bisect
import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

TAG_COLORS = [
    "#a855f7", "#c084fc", "#d8b4fe", "#9333ea",
    "#7c3aed", "#8b5cf6", "#a78bfa", "#c4b5fd",
]
ISSUE_STATUS_COLORS = {"open": "#ff7b72", "closed:complete": "#7ee787", "closed:not_planned": "#8b949e"}
TODO_STATUS_COLORS = {"open": "#58a6ff", "closed:complete": "#7ee787", "closed:not_planned": "#8b949e"}
DUPLICATE_THRESHOLD = 0.85
CLUSTER_COLORS = [
    "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#cc5de8", "#20c997", "#339af0",
    "#f06595", "#a9e34b", "#22b8cf", "#845ef7",
]

# 0.5 + Math.min(2.5, Math.log1p(c) * 0.8) as V8 computes it, for c < 22
# (from c = 22 on it is exactly 3). Python's math.log1p differs from V8's
# in the last bit for c = 2 and c = 13.
_V8_MASS = [
    0.5, 1.0545177444479563, 1.3788898309344877, 1.6090354888959124, 1.7875503299472804,
    1.933407575382444, 2.0567281192442506, 2.163553233343869, 2.257779661868976,
    2.342068074395237, 2.4183162182386964, 2.4879253198304, 2.5519594859692294,
    2.6112458636922073, 2.666440160881768, 2.718070977791825, 2.766570675244973,
    2.8122974063169317, 2.8555511833331524, 2.8965858188431928, 2.9356179501787385,
    2.972833962686653,
]


def _node_mass(connections: int) -> float:
    return _V8_MASS[connections] if connections < len(_V8_MASS) else 3


def _node_size(connections: int) -> int:
    # Math.floor(Math.log1p(c) * 8): the same integer as V8 for every c
    # checked (0..200000); it saturates at 28 from c = 33.
    return 12 + min(28, math.floor(math.log1p(connections) * 8))


# ------------------------------------------------------------------ JS semantics

class _Undefined:
    __slots__ = ()

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = _Undefined()

# JS WhiteSpace + LineTerminator (what String.prototype.trim and \s match).
_JS_WS = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003"
    "\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_HEADING_RE = re.compile("^#+[" + re.escape(_JS_WS) + "]*")
_LABEL_CHARS_RE = re.compile(r"[\n#*_`\[\]]")


def _truthy(v: Any) -> bool:
    if v is None or v is UNDEFINED or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    if isinstance(v, str):
        return v != ""
    return True  # objects and arrays, even empty ones


def _or(a: Any, b: Any) -> Any:
    return a if _truthy(a) else b


def _num(v: Any) -> bool:
    """typeof v === "number" (NaN and Infinity included)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def js_number_str(v: float) -> str:
    """Number.prototype.toString() for a double."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        v = float(v) if abs(v) >= 2 ** 53 else v
        if isinstance(v, int):
            return str(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    if v == 0:
        return "0"
    sign = "-" if v < 0 else ""
    mantissa, _, exp = repr(abs(v)).partition("e")
    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part).lstrip("0")
    point = len(int_part) + (int(exp) if exp else 0)  # decimal exponent n
    if int_part == "0":
        stripped = frac_part.lstrip("0")
        point = -(len(frac_part) - len(stripped)) + (int(exp) if exp else 0)
        digits = stripped
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    n = point
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    es = ("+" if e >= 0 else "-") + str(abs(e))
    return sign + (digits[0] + ("." + digits[1:] if k > 1 else "")) + "e" + es


def js_str(v: Any) -> str:
    """String(v), as a property key or in a template literal."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if v is UNDEFINED:
        return "undefined"
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return js_number_str(v)
    if isinstance(v, list):
        return ",".join("" if x is None or x is UNDEFINED else js_str(x) for x in v)
    return "[object Object]"


def _get(obj: Any, key: str) -> Any:
    """obj.key / obj?.key for a parsed JSON value."""
    if isinstance(obj, dict):
        return obj.get(key, UNDEFINED)
    if isinstance(obj, (list, str)) and key == "length":
        return _u16len(obj) if isinstance(obj, str) else len(obj)
    return UNDEFINED


def _index(obj: Any, i: int) -> Any:
    """obj[i] for a parsed JSON value."""
    if isinstance(obj, list):
        return obj[i] if 0 <= i < len(obj) else UNDEFINED
    if isinstance(obj, str):
        u = _u16slice(obj, i, i + 1)
        return u if u else UNDEFINED
    if isinstance(obj, dict):
        return obj.get(str(i), UNDEFINED)
    return UNDEFINED


def _iter(obj: Any) -> List[Any]:
    """for (const x of obj): arrays by element, strings by code point.
    (A plain object or null throws in JS; here it iterates nothing.)"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, str):
        return list(obj)
    return []


def _u16(s: str) -> bytes:
    return s.encode("utf-16-le", "surrogatepass")


def _u16_order(s: str) -> bytes:
    """A sort key in JS string order (UTF-16 code units): big-endian code
    units compare bytewise in the same order (review 8133 P2)."""
    return s.encode("utf-16-be", "surrogatepass")


def _u16len(s: str) -> int:
    return len(_u16(s)) // 2


def _u16slice(s: str, start: int, end: Optional[int] = None) -> str:
    """s.slice(start, end) counted in UTF-16 code units (may split a pair)."""
    b = _u16(s)
    return b[2 * start: None if end is None else 2 * end].decode("utf-16-le", "surrogatepass")


def _trim(s: str) -> str:
    return s.strip(_JS_WS)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def _js_int(text: str) -> Any:
    """A JSON integer as the double JSON.parse makes of it: exact up to
    2**53, rounded above, Infinity past the double range (review 8101: an
    over-long integer is a non-finite number, not an OverflowError)."""
    value = int(text)
    if abs(value) <= 2 ** 53:
        return value
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _json_parse(raw: Any) -> Any:
    """JSON.parse(raw): raises ValueError where JSON.parse throws."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("not text")
    text = raw if isinstance(raw, str) else js_str(raw)
    return json.loads(text, parse_constant=_reject_constant, parse_int=_js_int)


def parse_json(raw: Any, default: Any) -> Any:
    """graph.ts parseJson: the default for a falsy or unparsable value."""
    if not _truthy(raw):
        return default
    try:
        return _json_parse(raw)
    except (TypeError, ValueError, RecursionError):
        return default


# ------------------------------------------------------------------ _lineage.ts

def parse_related_payload(raw: Any) -> Tuple[bool, Any]:
    """_lineage.ts parseRelatedPayload: (True, entries) or (False, reason)."""
    if raw is None or raw == "":
        return True, []
    try:
        parsed = _json_parse(raw)
    except (TypeError, ValueError, RecursionError):
        return False, "invalid_json"
    if not isinstance(parsed, list):
        return False, "not_array"
    entries = []
    for item in parsed:
        if not _truthy(item) or not isinstance(item, (dict, list)):
            return False, "entry_not_object"
        ref_id = _get(item, "id")
        if not _num(ref_id) or not math.isfinite(ref_id):
            return False, "entry_bad_id"
        score = _get(item, "score")
        edge_type = _get(item, "edge_type")
        entries.append({
            "id": _canon_number(ref_id),
            "score": _canon_number(score) if _num(score) and math.isfinite(score) else UNDEFINED,
            "edge_type": edge_type if isinstance(edge_type, str) else UNDEFINED,
        })
    return True, entries


def _canon_number(v: Any) -> Any:
    """One Python value per JS number: 27.0 and 27 are the same number."""
    if isinstance(v, float) and v.is_integer() and abs(v) <= 2 ** 53:
        return int(v)
    return v


class _Lineage:
    def __init__(self) -> None:
        self.superseded_by: Dict[Any, Dict[Any, None]] = {}  # Map<number, Set<number>>
        self.supersedes_map: Dict[Any, Dict[Any, None]] = {}
        self.supersedes_edges: List[Dict[str, Any]] = []
        self.conflicts: List[Dict[str, Any]] = []
        self.authority_unknown: Dict[Any, None] = {}


def _add_pair(maps: _Lineage, newer, older, score, edge_scores: Dict[str, Any]) -> None:
    if newer == older:
        maps.authority_unknown[newer] = None
        return
    maps.supersedes_map.setdefault(newer, {})[older] = None
    maps.superseded_by.setdefault(older, {})[newer] = None
    key = f"{js_str(newer)}->{js_str(older)}"
    if key in edge_scores:
        prev = edge_scores[key]
        if prev != score:
            mx = max(prev, score)
            edge_scores[key] = mx
            for e in maps.supersedes_edges:
                if e["from"] == newer and e["to"] == older:
                    e["score"] = mx
                    break
            maps.conflicts.append({"a": min(newer, older), "b": max(newer, older),
                                   "kind": "score_mismatch", "scores": [prev, score]})
        return
    edge_scores[key] = score
    maps.supersedes_edges.append({"from": newer, "to": older, "score": score})


def _strongly_connected_components(nodes: Iterable[Any], outs: Dict[Any, List[Any]]) -> List[List[Any]]:
    """_lineage.ts stronglyConnectedComponents (recursive Tarjan), run with an
    explicit stack: the same visiting order, so the same components in the
    same order, without Python's recursion limit."""
    index = 0
    indices: Dict[Any, int] = {}
    lowlink: Dict[Any, int] = {}
    on_stack: set = set()
    stack: List[Any] = []
    sccs: List[List[Any]] = []
    for root in nodes:
        if root in indices:
            continue
        indices[root] = lowlink[root] = index
        index += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(outs.get(root, [])))]
        while work:
            v, it = work[-1]
            descended = False
            for w in it:
                if w not in indices:
                    indices[w] = lowlink[w] = index
                    index += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(outs.get(w, []))))
                    descended = True
                    break
                if w in on_stack:
                    lowlink[v] = min(lowlink[v], indices[w])
            if descended:
                continue
            if lowlink[v] == indices[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                sccs.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[v])
    return sccs


def _finalize_lineage_conflicts(maps: _Lineage) -> None:
    score_seen: set = set()
    score_conflicts = []
    for c in maps.conflicts:
        if c["kind"] != "score_mismatch":
            continue
        key = f"sm-{js_str(c['a'])}-{js_str(c['b'])}"
        if key in score_seen:
            continue
        score_seen.add(key)
        score_conflicts.append(c)

    outs: Dict[Any, List[Any]] = {}
    nodes: Dict[Any, None] = {}
    for e in maps.supersedes_edges:
        nodes[e["from"]] = None
        nodes[e["to"]] = None
        outs.setdefault(e["from"], []).append(e["to"])
    cycle_conflicts = []
    for comp in _strongly_connected_components(nodes, outs):
        if len(comp) < 2:
            continue
        ordered = sorted(comp)
        cycle_conflicts.append({"a": ordered[0], "b": ordered[-1], "kind": "cycle", "scores": [],
                                "members": ordered})
    self_conflicts = [c for c in maps.conflicts if c["kind"] == "self_cycle"]
    maps.conflicts = self_conflicts + score_conflicts + cycle_conflicts

    undirected: Dict[Any, Dict[Any, None]] = {}
    for e in maps.supersedes_edges:
        undirected.setdefault(e["from"], {})
        undirected.setdefault(e["to"], {})
        undirected[e["from"]][e["to"]] = None
        undirected[e["to"]][e["from"]] = None
    seen: set = set()
    for start in undirected:
        if start in seen:
            continue
        todo = [start]
        comp = []
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.append(n)
            todo.extend(undirected.get(n, {}))
        leaves = [i for i in comp if i not in maps.superseded_by]
        if len(leaves) > 1:
            for leaf in leaves:
                maps.authority_unknown[leaf] = None


def build_lineage_maps(crossrefs: Dict[Any, List[Dict[str, Any]]]) -> _Lineage:
    maps = _Lineage()
    edge_scores: Dict[str, Any] = {}
    for memory_id, refs in crossrefs.items():
        for ref in refs or []:
            if not _num(ref["id"]):
                continue
            score = ref["score"] if _num(ref["score"]) else 1
            edge_type = ref["edge_type"]
            if ref["id"] == memory_id:
                if edge_type in ("supersedes", "superseded_by"):
                    maps.authority_unknown[memory_id] = None
                    maps.conflicts.append({"a": memory_id, "b": memory_id, "kind": "self_cycle", "scores": [score]})
                continue
            if edge_type == "supersedes":
                _add_pair(maps, memory_id, ref["id"], score, edge_scores)
            elif edge_type == "superseded_by":
                _add_pair(maps, ref["id"], memory_id, score, edge_scores)
    _finalize_lineage_conflicts(maps)
    return maps


def classify_retirement_query_error(err: BaseException, table: str) -> str:
    msg = str(err).lower()
    if table.lower() in msg and ("no such table" in msg or "no such column" in msg):
        return "absent"
    return "operational"


_ASYMMETRIC = {
    "references": ("references", False), "referenced_by": ("references", True),
    "implements": ("implements", False), "implemented_by": ("implements", True),
    "extends": ("extends", False), "extended_by": ("extends", True),
}


def normalize_association_ref(memory_id, ref) -> Optional[Dict[str, Any]]:
    if not _num(ref["id"]) or ref["id"] == memory_id:
        return None
    raw_type = _or(ref["edge_type"], "related_to")
    if raw_type in ("supersedes", "superseded_by"):
        return None
    if raw_type in _ASYMMETRIC:
        edge_type, reverse = _ASYMMETRIC[raw_type]
        a, b = (ref["id"], memory_id) if reverse else (memory_id, ref["id"])
        return {"from": a, "to": b, "edge_type": edge_type, "directed": True}
    lo, hi = min(memory_id, ref["id"]), max(memory_id, ref["id"])
    edge_type = "contradicts" if raw_type == "contradicts" else "related_to"
    return {"from": lo, "to": hi, "edge_type": edge_type, "directed": False}


def build_association_edges(crossrefs: Dict[Any, List[Dict[str, Any]]], min_score: float) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for memory_id, refs in crossrefs.items():
        for ref in refs or []:
            norm = normalize_association_ref(memory_id, ref)
            if norm is None:
                continue
            score = ref["score"] if _num(ref["score"]) else 0
            if norm["edge_type"] == "related_to" and score <= min_score:
                continue
            if norm["directed"]:
                key = f"dir-{norm['edge_type']}-{js_str(norm['from'])}->{js_str(norm['to'])}"
            else:
                key = f"und-{norm['edge_type']}-{js_str(norm['from'])}-{js_str(norm['to'])}"
            existing = best.get(key)
            if existing is None:
                best[key] = {"from": norm["from"], "to": norm["to"], "edge_type": norm["edge_type"],
                             "score": score, "directed": norm["directed"]}
            elif score > existing["score"]:
                existing["score"] = score
    return list(best.values())


def partition_lineage_edges(edges, node_ids) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    drawable, dangling = [], []
    for e in edges:
        from_ok, to_ok = e["from"] in node_ids, e["to"] in node_ids
        if from_ok and to_ok:
            drawable.append(e)
        else:
            missing = "both" if not from_ok and not to_ok else ("from" if not from_ok else "to")
            dangling.append({"from": e["from"], "to": e["to"], "missing": missing})
    return drawable, dangling


# ------------------------------------------------------------------ clusters (graph.ts)

def louvain_communities(adj: Dict[Any, Dict[Any, float]], min_community_size: int = 3) -> Dict[Any, int]:
    """graph.ts louvainCommunities: single-level local moves, at most 50
    sweeps, nodes and neighbours in insertion order, then communities of at
    least ``min_community_size`` numbered from 0 in order of first
    appearance. The same arithmetic in the same order.

    Pages recomputes every community's total strength before each node's
    move (a pass over all nodes); here a total is summed over its members in
    node order -- the same additions -- and cached until one of its members
    moves.
    """
    node_list = list(adj.keys())
    if not node_list:
        return {}
    community: Dict[Any, Any] = {n: n for n in node_list}

    m2 = 0
    for neighbors in adj.values():
        for w in neighbors.values():
            m2 += w
    if m2 == 0:
        return community  # as Pages: every node its own community, unfiltered

    strength: Dict[Any, float] = {}
    for n in node_list:
        s = 0
        for w in adj[n].values():
            s += w
        strength[n] = s

    index = {n: i for i, n in enumerate(node_list)}
    members: Dict[Any, List[int]] = {n: [index[n]] for n in node_list}
    totals: Dict[Any, float] = {}

    def total(c) -> float:
        t = totals.get(c)
        if t is None:
            t = 0
            for i in members.get(c, ()):
                t += strength[node_list[i]]
            totals[c] = t
        return t

    improved = True
    iterations = 0
    while improved and iterations < 50:
        improved = False
        iterations += 1
        for node in node_list:
            current = community[node]
            ki = strength[node]
            comm_weights: Dict[Any, float] = {}
            for neighbor, w in adj[node].items():
                nc = community[neighbor]
                comm_weights[nc] = comm_weights.get(nc, 0) + w
            ki_in = comm_weights.get(current, 0)
            sigma_tot = total(current) - ki
            remove_loss = ki_in / m2 - (sigma_tot * ki) / (m2 * m2)
            best_gain = 0
            best = current
            for target, ki_target in comm_weights.items():
                if target == current:
                    continue
                sigma_target = total(target)
                gain = ki_target / m2 - (sigma_target * ki) / (m2 * m2) - remove_loss
                if gain > best_gain:
                    best_gain = gain
                    best = target
            if best != current:
                community[node] = best
                members[current].remove(index[node])
                bisect.insort(members.setdefault(best, []), index[node])
                totals.pop(current, None)
                totals.pop(best, None)
                improved = True

    counts: Dict[Any, int] = {}
    for c in community.values():
        counts[c] = counts.get(c, 0) + 1
    renumber: Dict[Any, int] = {}
    for c in dict.fromkeys(community.values()):
        if counts[c] >= min_community_size:
            renumber[c] = len(renumber)
    return {n: renumber[c] for n, c in community.items() if c in renumber}


def build_cluster_data(crossrefs: Dict[Any, List[Dict[str, Any]]], memory_ids: List[Any],
                       min_score: float = 0.5, min_cluster_size: int = 3) -> Dict[str, Any]:
    """graph.ts buildClusterData."""
    empty: Dict[str, Any] = {"clusterToNodes": {}, "clusterColors": {}, "clusterMeta": {}}
    if len(memory_ids) < min_cluster_size:
        return empty
    id_set = set(memory_ids)
    adj: Dict[Any, Dict[Any, float]] = {i: {} for i in memory_ids}
    for mem_id, refs in crossrefs.items():
        if mem_id not in id_set:
            continue
        for ref in refs:
            score = ref["score"] if _num(ref["score"]) else 0
            if score < min_score or ref["id"] not in id_set:
                continue
            adj[mem_id][ref["id"]] = score
            adj[ref["id"]][mem_id] = score

    grouped: Dict[str, List[Any]] = {}
    for node_id, cluster_id in louvain_communities(adj, min_cluster_size).items():
        grouped.setdefault(js_str(cluster_id), []).append(node_id)
    cluster_to_nodes = {k: grouped[k] for k in _js_key_order(grouped)}
    cluster_colors: Dict[str, str] = {}
    cluster_meta: Dict[str, Dict[str, Any]] = {}
    for i, cid in enumerate(cluster_to_nodes):
        cluster_colors[cid] = CLUSTER_COLORS[i % len(CLUSTER_COLORS)]
        cluster_meta[cid] = {"size": len(cluster_to_nodes[cid]), "label": f"Cluster {js_number_str(_parse_int(cid) + 1)}"}
    return {"clusterToNodes": cluster_to_nodes, "clusterColors": cluster_colors, "clusterMeta": cluster_meta}


_ARRAY_INDEX_RE = re.compile(r"^(0|[1-9][0-9]*)$")


def _js_key_order(keys: Iterable[str]) -> List[str]:
    """Object.keys order: array-index keys ascending, then the others in
    insertion order."""
    keys = list(keys)
    idx = [k for k in keys if _ARRAY_INDEX_RE.match(k) and int(k) < 2 ** 32 - 1]
    rest = [k for k in keys if k not in set(idx)]
    return sorted(idx, key=int) + rest


def _parse_int(s: str) -> float:
    m = re.match(r"^[" + re.escape(_JS_WS) + r"]*([+-]?[0-9]+)", s)
    return int(m.group(1)) if m else math.nan


# ------------------------------------------------------------------ Pages defects

# Object.prototype's own property names: graph.ts finds these "in" any plain
# object, so a key named like one breaks its maps (see the module docstring).
OBJECT_PROTOTYPE_KEYS = frozenset({
    "constructor", "__defineGetter__", "__defineSetter__", "hasOwnProperty", "__lookupGetter__",
    "__lookupSetter__", "isPrototypeOf", "propertyIsEnumerable", "toString", "valueOf", "__proto__",
    "toLocaleString",
})


def pages_defects(memory_rows: List[Dict[str, Any]], *, include_docs: bool, limit: int) -> List[str]:
    """The inputs on which graph.ts itself misbehaves (so no parity is
    owed): each selected memory whose tag, section, status, component or
    category is an Object.prototype name. Empty for every other input."""
    found: List[str] = []

    def check(mid, what, key):
        if js_str(key) in OBJECT_PROTOTYPE_KEYS:
            found.append(f"#{js_str(mid)} {what} {js_str(key)!r}")

    def eligible(m) -> bool:
        meta = parse_json(m.get("metadata"), {})
        return not _is_type(meta, "section") and not (_is_type(meta, "document_fragment") and not include_docs)

    def created(m) -> str:
        c = _or(m.get("created_at"), "")
        return c if isinstance(c, str) else js_str(c)

    selected = sorted((m for m in memory_rows if eligible(m)),
                      key=lambda m: (_u16_order(created(m)), m["id"]), reverse=True)[:limit]
    for m in selected:
        meta = parse_json(m.get("metadata"), {})
        tags = parse_json(m.get("tags"), [])
        check(m["id"], "primary tag", _or(_index(tags, 0), "untagged"))
        if _is_type(meta, "document_fragment"):
            continue
        for tag in _iter(tags):
            check(m["id"], "tag", tag)
        if _is_type(meta, "issue"):
            check(m["id"], "issue status", _issue_status(meta))
            check(m["id"], "component", _or(_get(meta, "component"), "uncategorized"))
        elif _is_type(meta, "todo"):
            check(m["id"], "todo status", _todo_status(meta))
            check(m["id"], "category", _or(_get(meta, "category"), "uncategorized"))
        else:
            path = _get(_get(meta, "hierarchy"), "path")
            section = path[0] if isinstance(path, list) and path else _or(_get(meta, "section"), "Uncategorized")
            check(m["id"], "section", section)
    return found


# ------------------------------------------------------------------ graph.ts onRequestGet

def _is_type(meta: Any, name: str) -> bool:
    return _get(meta, "type") == name


def _issue_status(meta: Any) -> Any:
    status = _or(_get(meta, "status"), "open")
    if status == "resolved":
        return "closed:complete"
    if status == "wontfix":
        return "closed:not_planned"
    if status == "in_progress":
        return "open"
    if status == "closed":
        return f"closed:{js_str(_or(_get(meta, 'closed_reason'), 'complete'))}"
    return status


def _todo_status(meta: Any) -> Any:
    status = _or(_get(meta, "status"), "open")
    if status == "completed":
        return "closed:complete"
    if status == "blocked":
        return "closed:not_planned"
    if status == "in_progress":
        return "open"
    if status == "closed":
        return f"closed:{js_str(_or(_get(meta, 'closed_reason'), 'complete'))}"
    return status


def _push(mapping: Dict[str, List[Any]], key: Any, value: Any) -> None:
    mapping.setdefault(js_str(key), []).append(value)


def build_graph_payload(
    memory_rows: List[Dict[str, Any]],
    crossref_rows: Optional[List[Tuple[Any, Any]]],
    retirement: Dict[str, Any],
    *,
    include_docs: bool,
    limit: int,
    min_score: float = 0.40,
) -> Dict[str, Any]:
    """graph.ts onRequestGet after the database reads.

    memory_rows: ``SELECT id, content, metadata, tags, created_at, updated_at
    FROM memories`` as dicts, in the order the store returns them.
    crossref_rows: ``SELECT memory_id, related FROM memories_crossrefs`` as
    (memory_id, related) pairs, or None when that query failed.
    retirement: {"ids": [memory_id, ...] from tombstone_components then
    tombstones, in row order; "available": False when either query failed
    for a reason other than a missing table}.
    """
    if not memory_rows:
        return {"error": "no_memories", "message": "No memories to visualize"}
    memories = memory_rows

    crossrefs: Dict[Any, List[Dict[str, Any]]] = {}
    crossrefs_available = True
    lineage_available = True
    degraded_reason: Optional[str] = None
    corrupt_rows: List[Any] = []
    if crossref_rows is None:
        lineage_available = crossrefs_available = False
        degraded_reason = "crossrefs_query_failed"
    else:
        for memory_id, related in crossref_rows:
            ok, parsed = parse_related_payload(related)
            if not ok:
                lineage_available = crossrefs_available = False
                degraded_reason = degraded_reason or f"corrupt_crossref:{parsed}"
                corrupt_rows.append(memory_id)
                continue
            crossrefs[memory_id] = parsed

    retired_ids: Dict[Any, None] = {}
    for rid in retirement.get("ids", []):
        if _num(rid):
            retired_ids[_canon_number(rid)] = None
    if not retirement.get("available", True):
        lineage_available = crossrefs_available = False
        degraded_reason = degraded_reason or "retirement_query_failed"

    lineage = build_lineage_maps(crossrefs) if lineage_available else _Lineage()
    for rid in retired_ids:
        if math.isfinite(rid):
            lineage.authority_unknown[rid] = None
    superseded_by = lineage.superseded_by
    supersedes_map = lineage.supersedes_map

    assoc_edges: List[Dict[str, Any]] = []
    if crossrefs_available:
        for ae in build_association_edges(crossrefs, min_score):
            assoc_edges.append({"id": 0, "from": ae["from"], "to": ae["to"], "edge_type": ae["edge_type"],
                                "score": ae["score"], "directed": ae["directed"]})

    def eligible(m) -> bool:
        meta = parse_json(m.get("metadata"), {})
        if _is_type(meta, "section"):
            return False
        return not (_is_type(meta, "document_fragment") and not include_docs)

    eligible_memories = [m for m in memories if eligible(m)]
    total = len(eligible_memories)
    truncated = total > limit

    def created(m) -> str:
        c = _or(m.get("created_at"), "")
        return c if isinstance(c, str) else js_str(c)

    # created_at DESC, then id DESC (a stable sort, as Array.prototype.sort)
    memories = sorted(eligible_memories, key=lambda m: (_u16_order(created(m)), m["id"]), reverse=True)[:limit]

    def is_dup_excluded(meta) -> bool:
        return _is_type(meta, "section") or _is_type(meta, "document_fragment") or _is_type(meta, "document_root")

    memory_ids = {m["id"] for m in memories if not is_dup_excluded(parse_json(m.get("metadata"), None))}
    duplicate_ids: Dict[Any, None] = {}
    duplicate_pair_keys: set = set()
    for m in memories:
        meta = parse_json(m.get("metadata"), {})
        if is_dup_excluded(meta):
            continue
        for ref in crossrefs.get(m["id"], []):
            et = ref["edge_type"]
            if et is not UNDEFINED and et is not None and et != "related_to":
                continue
            if not _num(ref["score"]):
                continue
            if ref["score"] >= 0.9999:
                continue
            if ref["id"] == m["id"]:
                continue
            if ref["score"] >= DUPLICATE_THRESHOLD and ref["id"] in memory_ids:
                a, b = min(m["id"], ref["id"]), max(m["id"], ref["id"])
                duplicate_pair_keys.add(f"{js_str(a)}-{js_str(b)}")
                duplicate_ids[m["id"]] = None
                duplicate_ids[ref["id"]] = None

    tag_colors: Dict[str, str] = {}
    for m in memories:
        tags = parse_json(m.get("tags"), [])
        primary = js_str(_or(_index(tags, 0), "untagged"))
        if primary not in tag_colors:
            tag_colors[primary] = TAG_COLORS[len(tag_colors) % len(TAG_COLORS)]

    root_by_doc_key: Dict[str, Any] = {}
    for m in memories:
        meta = parse_json(m.get("metadata"), {})
        if _is_type(meta, "document_root") and isinstance(_get(meta, "document_key"), str):
            root_by_doc_key[_get(meta, "document_key")] = m["id"]

    nodes: List[Dict[str, Any]] = []
    for m in memories:
        meta = parse_json(m.get("metadata"), {})
        if _is_type(meta, "section"):
            continue
        is_frag = _is_type(meta, "document_fragment")
        if is_frag and not include_docs:
            continue
        tags = parse_json(m.get("tags"), [])
        primary = js_str(_or(_index(tags, 0), "untagged"))
        content = m.get("content")
        content = content if isinstance(content, str) else js_str(content)

        first_line = _u16slice(_trim(_HEADING_RE.sub("", content.split("\n")[0], count=1)), 0, 60)
        headline = first_line.replace('"', "'").replace("\\", "")
        label = _trim(_LABEL_CHARS_RE.sub(" ", _u16slice(content, 0, 35))).replace('"', "'").replace("\\", "")

        type_label = " - Issue" if _is_type(meta, "issue") else (" - TODO" if _is_type(meta, "todo") else "")
        is_superseded = lineage_available and m["id"] in superseded_by
        is_retired = m["id"] in retired_ids
        authority_unknown = (not lineage_available) or m["id"] in lineage.authority_unknown
        superseded_by_ids = list(superseded_by[m["id"]]) if is_superseded else None
        supersedes_ids = list(supersedes_map[m["id"]]) if lineage_available and m["id"] in supersedes_map else None
        if authority_unknown:
            lineage_label = " - AUTHORITY UNKNOWN"
        elif is_superseded:
            lineage_label = " - SUPERSEDED"
        else:
            lineage_label = " - supersedes older" if supersedes_ids else ""

        node: Dict[str, Any] = {
            "id": m["id"],
            "label": label + "..." if _u16len(label) > 35 else label,
            "title": f"#{js_str(m['id'])}{type_label}{lineage_label}\n{headline}",
            "color": "#484f58" if authority_unknown else tag_colors[primary],
            "size": max(8, math.floor(12 * 0.7)) if is_superseded or authority_unknown else 12,
            "mass": 0.5,
        }
        if is_superseded:
            node["superseded"] = True
        if authority_unknown:
            node["authority_unknown"] = True
        if is_retired:
            node["retired"] = True
        if superseded_by_ids is not None:
            node["superseded_by"] = superseded_by_ids
        if supersedes_ids is not None:
            node["supersedes"] = supersedes_ids

        if _is_type(meta, "issue"):
            node["shape"] = "dot"
            node["color"] = ISSUE_STATUS_COLORS.get(js_str(_issue_status(meta))) or ISSUE_STATUS_COLORS["open"]
            if _get(meta, "severity") == "critical":
                node["borderWidth"] = 4
        if _is_type(meta, "todo"):
            node["shape"] = "dot"
            node["color"] = TODO_STATUS_COLORS.get(js_str(_todo_status(meta))) or TODO_STATUS_COLORS["open"]
            if _get(meta, "priority") == "high":
                node["borderWidth"] = 4
        if m["id"] in duplicate_ids:
            node["color"] = {"background": node["color"] if isinstance(node["color"], str) else "#a855f7",
                             "border": "#f85149"}
            node["borderWidth"] = 3
        if is_frag:
            node["frag"] = True
            node["shape"] = "dot"
            node["color"] = "#58a6ff"
            node["size"] = 9
            node["mass"] = 0.4
            heading = _get(meta, "section_heading")
            heading = heading if isinstance(heading, str) else ""
            dk = _get(meta, "document_key")
            dk = dk if isinstance(dk, str) else ""
            node["title"] = f"#{js_str(m['id'])} - fragment\n{_or(heading, dk)}"
            node["label"] = ""
        nodes.append(node)

    doc_edges: List[Dict[str, Any]] = []
    if include_docs:
        by_doc: Dict[str, List[Dict[str, Any]]] = {}
        doc_seen: set = set()
        for m in memories:
            meta = parse_json(m.get("metadata"), {})
            if not _is_type(meta, "document_fragment"):
                continue
            dk = _get(meta, "document_key")
            dk = dk if isinstance(dk, str) else ""
            if not dk:
                continue
            ordinal = _get(meta, "ordinal")
            by_doc.setdefault(dk, []).append({"id": m["id"], "ord": ordinal if _num(ordinal) else 0})

        def add_edge(a, b) -> None:
            if a == b:
                return
            k = f"doc-{js_str(min(a, b))}-{js_str(max(a, b))}"
            if k not in doc_seen:
                doc_seen.add(k)
                doc_edges.append({"id": 0, "from": a, "to": b, "edge_type": "document", "directed": False})

        for dk, frags in by_doc.items():
            frags.sort(key=lambda f: f["ord"])
            root_id = root_by_doc_key.get(dk)
            if root_id is not None:
                for f in frags:
                    add_edge(f["id"], root_id)
            else:
                for i in range(1, len(frags)):
                    add_edge(frags[i - 1]["id"], frags[i]["id"])

    tag_to_nodes: Dict[str, List[Any]] = {}
    section_to_nodes: Dict[str, List[Any]] = {}
    subsection_to_nodes: Dict[str, List[Any]] = {}
    status_to_nodes: Dict[str, List[Any]] = {}
    issue_category_to_nodes: Dict[str, List[Any]] = {}
    todo_status_to_nodes: Dict[str, List[Any]] = {}
    todo_category_to_nodes: Dict[str, List[Any]] = {}
    node_timestamps: Dict[str, Any] = {}
    dates: List[Any] = []
    for m in memories:
        meta = parse_json(m.get("metadata"), {})
        tags = parse_json(m.get("tags"), [])
        if _is_type(meta, "section") or _is_type(meta, "document_fragment"):
            continue
        for tag in _iter(tags):
            _push(tag_to_nodes, tag, m["id"])
        issue, todo = _is_type(meta, "issue"), _is_type(meta, "todo")
        if issue:
            _push(status_to_nodes, _issue_status(meta), m["id"])
            _push(issue_category_to_nodes, _or(_get(meta, "component"), "uncategorized"), m["id"])
        if todo:
            _push(todo_status_to_nodes, _todo_status(meta), m["id"])
            _push(todo_category_to_nodes, _or(_get(meta, "category"), "uncategorized"), m["id"])
        if not issue and not todo:
            path = _get(_get(meta, "hierarchy"), "path")
            parts: List[Any] = []
            if isinstance(path, list) and path:
                section = path[0]
                parts = path[1:]
            else:
                section = _or(_get(meta, "section"), "Uncategorized")
                subsection = _get(meta, "subsection")
                if isinstance(subsection, str) and subsection:
                    parts = subsection.split("/")
            _push(section_to_nodes, section, m["id"])
            for i in range(len(parts)):
                # parts.slice(0, i + 1).join("/"): null/undefined join as ""
                partial = "/".join("" if p is None or p is UNDEFINED else js_str(p) for p in parts[: i + 1])
                _push(subsection_to_nodes, f"{js_str(section)}/{partial}", m["id"])
        if _truthy(m.get("created_at")):
            node_timestamps[js_str(m["id"])] = m["created_at"]
            dates.append(m["created_at"])
    min_date = max_date = ""
    if dates:
        dates.sort(key=lambda d: _u16_order(js_str(d)))
        min_date, max_date = dates[0], dates[-1]

    node_ids = [n["id"] for n in nodes]
    node_id_set = set(node_ids)
    cluster_data = build_cluster_data(crossrefs, node_ids, 0.4, 3)

    drawable_lineage, dangling = partition_lineage_edges(lineage.supersedes_edges, node_id_set)
    drawable_assoc = [e for e in assoc_edges if e["from"] in node_id_set and e["to"] in node_id_set]
    drawable_docs = [e for e in doc_edges if e["from"] in node_id_set and e["to"] in node_id_set]
    final_edges: List[Dict[str, Any]] = []
    for le in drawable_lineage:
        final_edges.append({"id": len(final_edges), "from": le["from"], "to": le["to"],
                            "edge_type": "supersedes", "score": le["score"], "directed": True})
    for e in drawable_assoc + drawable_docs:
        final_edges.append({**e, "id": len(final_edges)})

    connection_counts: Dict[Any, int] = {}
    for edge in final_edges:
        connection_counts[edge["from"]] = connection_counts.get(edge["from"], 0) + 1
        connection_counts[edge["to"]] = connection_counts.get(edge["to"], 0) + 1
    for n in nodes:
        connections = connection_counts.get(n["id"], 0)
        size = _node_size(connections)
        if n.get("superseded") or n.get("authority_unknown"):
            n["size"] = max(8, math.floor(size * 0.7))
        elif not n.get("frag"):
            n["size"] = size
        if not n.get("frag"):
            n["mass"] = _node_mass(connections)

    superseded_ids = [i for i in superseded_by if i in node_id_set] if lineage_available else None

    payload: Dict[str, Any] = {
        "nodes": nodes,
        "edges": final_edges,
        "tagColors": tag_colors,
        "tagToNodes": tag_to_nodes,
        "sectionToNodes": section_to_nodes,
        "subsectionToNodes": subsection_to_nodes,
        "statusToNodes": status_to_nodes,
        "issueCategoryToNodes": issue_category_to_nodes,
        "todoStatusToNodes": todo_status_to_nodes,
        "todoCategoryToNodes": todo_category_to_nodes,
        "duplicateIds": list(duplicate_ids) if crossrefs_available else None,
        "duplicatePairCount": len(duplicate_pair_keys) if crossrefs_available else None,
        "crossrefsAvailable": crossrefs_available,
        "lineageAvailable": lineage_available,
        "lineageDegradedReason": degraded_reason,
    }
    if corrupt_rows:
        payload["corruptCrossrefRows"] = corrupt_rows
    payload["supersededIds"] = superseded_ids
    payload["supersededCount"] = len(superseded_ids) if lineage_available else None
    payload["retiredIds"] = list(retired_ids)
    payload["supersedesEdges"] = (
        [{"from": e["from"], "to": e["to"], "score": e["score"]} for e in drawable_lineage]
        if lineage_available else None
    )
    if lineage_available and dangling:
        payload["lineageDangling"] = dangling
    if lineage_available and lineage.conflicts:
        payload["lineageConflicts"] = lineage.conflicts
    payload.update({
        "nodeTimestamps": node_timestamps,
        "minDate": min_date,
        "maxDate": max_date,
        "truncated": truncated,
        "total": total,
        "clusterToNodes": cluster_data["clusterToNodes"] if crossrefs_available else {},
        "clusterColors": cluster_data["clusterColors"] if crossrefs_available else {},
        "clusterMeta": cluster_data["clusterMeta"] if crossrefs_available else {},
    })
    return payload
