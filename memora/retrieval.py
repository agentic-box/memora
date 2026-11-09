"""Retrieval v2 (Item 260): a ranked keyword leg, exact identifier lookup, and a
weighted fusion of them with the dense leg.

The default engine of ``memory_hybrid_search`` (Item 262; ``engine="v1"`` still
selects the unchanged ``storage.hybrid_search``). Everything here is read-only: SELECTs against ``memories`` and
``memories_fts``. The FTS index is never written, rebuilt or given a helper
table (no fts5vocab), so it stays byte-identical.

Why a new keyword leg: the v1 keyword query is an FTS5 implicit AND of every
query word, ordered by created_at -- a long brief matches nothing. Here the
query is an OR of normalised, IDF-capped terms ranked by bm25().
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import storage

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Query normalisation

# English function words plus words that appear in nearly every code-related
# note. Dropping them keeps the OR query selective; the IDF cap would remove
# most of them anyway, but a short query has no cap pressure.
_STOPWORDS = frozenset(
    """
    a about above after again all also am an and any are as at be because been
    before being below between both but by can could did do does doing down
    during each few for from further had has have having he her here hers him
    his how i if in into is it its itself just me more most my no nor not now
    of off on once only or other our out over own same she should so some such
    than that the their them then there these they this those through to too
    under until up very was we were what when where which while who whom why
    will with would you your yours
    def class import return self none true false str int dict list set tuple
    bool print args kwargs init elif else try except finally pass lambda yield
    py src tmp todo etc eg ie vs via per use used using like new get
    """.split()
)

_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)

DEFAULT_MAX_TERMS = 48
DEFAULT_BM25_WEIGHTS = (1.0, 0.3, 0.6)  # content, metadata, tags
_MAX_IDENTIFIERS = 40
_PER_IDENTIFIER_ROWS = 200
_IN_CHUNK = 500
_FTS_MAX_ROWS = 5000  # same bound as storage._SCAN_CAP


def normalize_terms(query: str) -> List[str]:
    """Lowercased, FTS-safe terms of a query: alphanumeric runs only (so no
    FTS operator or quote survives), stopwords and 1-char terms dropped,
    duplicates removed in first-seen order."""
    seen: Dict[str, None] = {}
    for raw in _TERM_RE.findall((query or "").lower()):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        seen.setdefault(raw, None)
    return list(seen)


def _fts_ready(conn: sqlite3.Connection) -> bool:
    return storage._fts_enabled(conn)


def _doc_freq(conn: sqlite3.Connection, term: str) -> int:
    try:
        row = conn.execute(
            "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH ?",
            (f'"{term}"',),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def select_terms(
    conn: sqlite3.Connection, terms: Sequence[str], max_terms: int
) -> List[str]:
    """Cap ``terms`` at the ``max_terms`` highest-IDF ones (lowest document
    frequency). Terms absent from the index match nothing and are dropped
    first. Nothing is read when the list already fits."""
    terms = list(terms)
    if max_terms <= 0 or len(terms) <= max_terms or not _fts_ready(conn):
        return terms[:max_terms] if max_terms > 0 else terms
    scored: List[Tuple[int, int, int, str]] = []
    for pos, term in enumerate(terms):
        df = _doc_freq(conn, term)
        if df <= 0:
            continue
        scored.append((df, -len(term), pos, term))
    scored.sort()
    keep = {t for _, _, _, t in scored[:max_terms]}
    return [t for t in terms if t in keep]  # stable, first-seen order


# --------------------------------------------------------------------------
# Shared SQL pieces

_COLS = (
    "m.id, m.content, m.metadata, m.tags, m.created_at, m.updated_at, "
    "m.importance, m.last_accessed, m.access_count"
)


def _date_clause(date_from: Optional[str], date_to: Optional[str]) -> Tuple[str, List[Any]]:
    clause, params = "", []
    if date_from:
        clause += " AND m.created_at >= ?"
        params.append(storage._parse_date_filter(date_from))
    if date_to:
        clause += " AND m.created_at <= ?"
        params.append(storage._parse_date_filter(date_to))
    clause += storage._not_import_pending_sql("m.metadata")
    return clause, params


def _post_filter(
    rows: Iterable[Any],
    metadata_filters: Optional[Dict[str, Any]],
    tags_any: Optional[List[str]],
    tags_all: Optional[List[str]],
    tags_none: Optional[List[str]],
    project: Optional[str],
) -> List[Dict[str, Any]]:
    validated = storage._validate_metadata_filters(metadata_filters)
    return [
        rec
        for rec in (storage._serialise_row(r) for r in rows)
        if storage._records_pass_post_sql_filters(
            rec, validated, tags_any, tags_all, tags_none, None, project
        )
    ]


def _has_post_filters(metadata_filters, tags_any, tags_all, tags_none, project) -> bool:
    return bool(metadata_filters or tags_any or tags_all or tags_none or project)


# --------------------------------------------------------------------------
# Lexical leg

def lexical_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 30,
    metadata_filters: Optional[Dict[str, Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    project: Optional[str] = None,
    max_terms: int = DEFAULT_MAX_TERMS,
    bm25_weights: Sequence[float] = DEFAULT_BM25_WEIGHTS,
) -> List[Dict[str, Any]]:
    """Memories matching ANY of the query's terms, best first.

    With FTS: ranked by bm25() over (content, metadata, tags) with
    ``bm25_weights``. Without FTS (or when FTS errors): ranked by how many
    distinct terms matched (LIKE), newest first on ties. Returns
    ``[{"memory", "score"}]``; score is higher-is-better (negated bm25, or the
    matched-term count)."""
    terms = select_terms(conn, normalize_terms(query), max_terms)
    if not terms or limit <= 0:
        return []
    date_clause, date_params = _date_clause(date_from, date_to)
    post = _has_post_filters(metadata_filters, tags_any, tags_all, tags_none, project)
    sql_limit = _FTS_MAX_ROWS if post else limit

    if _fts_ready(conn):
        weights = ", ".join(repr(float(w)) for w in tuple(bm25_weights)[:3])
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = conn.execute(
                f"""
                SELECT {_COLS}, bm25(memories_fts, {weights}) AS lex_score
                FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid
                WHERE memories_fts MATCH ?{date_clause}
                ORDER BY lex_score ASC, m.id DESC
                LIMIT ?
                """,
                (match, *date_params, sql_limit),
            ).fetchall()
            scores = [-float(r["lex_score"]) for r in rows]
        except sqlite3.OperationalError:
            rows, scores = _like_rank(conn, terms, date_clause, date_params, sql_limit)
    else:
        rows, scores = _like_rank(conn, terms, date_clause, date_params, sql_limit)

    out: List[Dict[str, Any]] = []
    for row, score in zip(rows, scores):
        [rec] = _post_filter([row], metadata_filters, tags_any, tags_all, tags_none, project) or [None]
        if rec is None:
            continue
        out.append({"memory": rec, "score": score})
        if len(out) >= limit:
            break
    return out


def _like_rank(
    conn: sqlite3.Connection,
    terms: Sequence[str],
    date_clause: str,
    date_params: List[Any],
    sql_limit: int,
) -> Tuple[List[Any], List[float]]:
    """FTS-disabled keyword ranking: count of distinct terms matched."""
    per_term = "(CASE WHEN (m.content LIKE ? OR m.tags LIKE ? OR m.metadata LIKE ?) THEN 1 ELSE 0 END)"
    score_expr = " + ".join(per_term for _ in terms)
    like_params: List[Any] = []
    for t in terms:
        like_params.extend([f"%{t}%"] * 3)
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT {_COLS}, ({score_expr}) AS lex_score
            FROM memories m
            WHERE 1=1{date_clause}
        ) WHERE lex_score > 0
        ORDER BY lex_score DESC, created_at DESC, id DESC
        LIMIT ?
        """,
        (*like_params, *date_params, sql_limit),
    ).fetchall()
    return rows, [float(r["lex_score"]) for r in rows]


# --------------------------------------------------------------------------
# Identifier leg

_ITEM_RE = re.compile(r"\bitem\s*#?\s*(\d{1,7})\b", re.IGNORECASE)
_HASH_NUM_RE = re.compile(r"(?<![\w&])#(\d{1,7})\b")
_MEMID_RE = re.compile(r"\bm(\d{1,7})\b")
_VERSION_RE = re.compile(r"\bv\d+(?:\.\d+){1,4}\b")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_PATH_RE = re.compile(r"[\w\-]+(?:/[\w\-.]+)+|[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z][A-Za-z0-9]{0,5}\b")
_SNAKE_RE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")
_CAMEL_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b|\b(?:[A-Z][a-z0-9]+){2,}\b")
_DOTTED_RE = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")

# kind -> exactness tier; a metadata/id hit outranks a phrase hit.
_EXACT_META = 2
_EXACT_PHRASE = 1


def extract_identifiers(query: str) -> Dict[str, Any]:
    """Pull identifiers out of free text.

    Returns ``{"items": [int], "memory_ids": [int], "phrases": [str],
    "shas": [str]}``. ``phrases`` are lowercased token sequences (joined by
    single spaces) to be matched as FTS phrases: unicode61 splits ``_`` and
    ``.`` and ``/``, so ``src/a/b.zig`` is the phrase ``src a b zig``."""
    text = query or ""
    items: Dict[int, None] = {}
    for m in _ITEM_RE.finditer(text):
        items.setdefault(int(m.group(1)), None)
    for m in _HASH_NUM_RE.finditer(text):
        items.setdefault(int(m.group(1)), None)
    mids: Dict[int, None] = {}
    for m in _MEMID_RE.finditer(text):
        mids.setdefault(int(m.group(1)), None)

    shas: Dict[str, None] = {}
    for m in _SHA_RE.finditer(text):
        s = m.group(0)
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            shas.setdefault(s, None)

    phrases: Dict[str, None] = {}

    def add_phrase(raw: str) -> None:
        toks = _TERM_RE.findall(raw.lower())
        if len(toks) >= 2 or (len(toks) == 1 and len(toks[0]) >= 4):
            phrases.setdefault(" ".join(toks), None)

    for rx in (_VERSION_RE, _SNAKE_RE, _DOTTED_RE, _PATH_RE):
        for m in rx.finditer(text):
            add_phrase(m.group(0))
    for m in _CAMEL_RE.finditer(text):
        add_phrase(m.group(0))
    # Words already captured as shas/ids/items must not also be phrases.
    for s in shas:
        phrases.pop(s, None)
    return {
        "items": list(items),
        "memory_ids": list(mids),
        "phrases": list(phrases),
        "shas": list(shas),
    }


def _item_rowids(
    conn: sqlite3.Connection, n: int, date_clause: str, date_params: List[Any], cap: int
) -> List[int]:
    # metadata.item only (item records); never metadata.item_number, which is
    # a document-fragment plan-step number. The item may be stored as an int
    # or a numeric string.
    rows = conn.execute(
        """
        SELECT m.id FROM memories m
        WHERE CASE WHEN json_valid(m.metadata)
                   THEN json_extract(m.metadata, '$.item') END IN (?, ?){date_clause}
        ORDER BY m.id DESC LIMIT ?
        """.replace("{date_clause}", date_clause),
        (n, str(n), *date_params, cap),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _phrase_rowids(
    conn: sqlite3.Connection, phrase: str, prefix: bool,
    date_clause: str, date_params: List[Any], cap: int,
) -> List[int]:
    if _fts_ready(conn):
        q = f'"{phrase}"' + ("*" if prefix else "")
        try:
            rows = conn.execute(
                "SELECT memories_fts.rowid FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.rowid "
                f"WHERE memories_fts MATCH ?{date_clause} "
                "ORDER BY memories_fts.rowid DESC LIMIT ?",
                (q, *date_params, cap),
            ).fetchall()
            return [int(r[0]) for r in rows]
        except sqlite3.OperationalError:
            pass
    like = f"%{phrase}%" if prefix else f"%{phrase.replace(' ', '%')}%"
    rows = conn.execute(
        "SELECT m.id FROM memories m WHERE (m.content LIKE ? OR m.tags LIKE ? OR m.metadata LIKE ?)"
        f"{date_clause} ORDER BY m.id DESC LIMIT ?",
        (like, like, like, *date_params, cap),
    ).fetchall()
    return [int(r[0]) for r in rows]


def identifier_lookup(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 30,
    metadata_filters: Optional[Dict[str, Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    project: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Memories that contain an identifier named in ``query``.

    ``Item N`` / ``#N`` resolve to ``metadata.item == N``; ``mNNN`` to the
    memory with that id; paths, snake/camel/dotted identifiers and version
    tags to an FTS phrase; a 7-40 char hex sha to an FTS prefix. Ranked by
    exactness (metadata/id hit above phrase hit), then by how many distinct
    identifiers the memory matches. Returns ``[{"memory", "score",
    "matched"}]``."""
    ids = extract_identifiers(query)
    if limit <= 0:
        return []
    date_clause, date_params = _date_clause(date_from, date_to)
    # Date and import-pending filters are applied in the candidate SQL, before
    # the per-identifier cap. Metadata/tag/project filters need the parsed row,
    # so with one of them set the raw cap is the wide safety bound and the
    # candidates are filtered in rank order below (never cap-then-filter).
    post = _has_post_filters(metadata_filters, tags_any, tags_all, tags_none, project)
    cap = _FTS_MAX_ROWS if post else _PER_IDENTIFIER_ROWS
    hits: Dict[int, List[int]] = {}  # memory id -> [best exactness, n matched]

    # id -> filtered record (None = filtered out), shared with the final pass.
    recs_by_id: Dict[int, Optional[Dict[str, Any]]] = {}

    def eligible(rowids: List[int]) -> List[int]:
        """Rowids (newest first) that pass every filter, at most
        _PER_IDENTIFIER_ROWS of them. Filtering happens BEFORE the cap, so an
        eligible row deeper than the cap's worth of raw matches is still found."""
        if not post:
            return rowids
        keep: List[int] = []
        for i in range(0, len(rowids), _IN_CHUNK):
            chunk = [r for r in rowids[i : i + _IN_CHUNK] if r not in recs_by_id]
            if chunk:
                marks = ",".join("?" for _ in chunk)
                fetched = {
                    int(row["id"]): row
                    for row in conn.execute(
                        f"SELECT {_COLS} FROM memories m WHERE m.id IN ({marks}){date_clause}",
                        (*chunk, *date_params),
                    ).fetchall()
                }
                for rid in chunk:
                    row = fetched.get(rid)
                    got = _post_filter([row], metadata_filters, tags_any, tags_all, tags_none, project) if row is not None else []
                    recs_by_id[rid] = got[0] if got else None
            keep.extend(r for r in rowids[i : i + _IN_CHUNK] if recs_by_id.get(r) is not None)
            if len(keep) >= _PER_IDENTIFIER_ROWS:
                return keep[:_PER_IDENTIFIER_ROWS]
        return keep

    def credit(rowids: Iterable[int], exact: int) -> None:
        rowids = list(rowids)
        if len(rowids) >= cap:
            logger.debug("identifier lookup scanned its raw candidate bound (%d); deeper matches omitted", cap)
        rowids = eligible(rowids)
        for rid in set(rowids):
            cur = hits.setdefault(rid, [0, 0])
            cur[0] = max(cur[0], exact)
            cur[1] += 1

    budget = _MAX_IDENTIFIERS
    for n in ids["items"][:budget]:
        credit(_item_rowids(conn, n, date_clause, date_params, cap), _EXACT_META)
    budget -= len(ids["items"][:budget])
    if budget > 0 and ids["memory_ids"]:
        wanted = ids["memory_ids"][:budget]
        marks = ",".join("?" for _ in wanted)
        found = conn.execute(f"SELECT id FROM memories WHERE id IN ({marks})", tuple(wanted)).fetchall()
        credit((int(r[0]) for r in found), _EXACT_META)
        budget -= len(wanted)
    for phrase in ids["phrases"][: max(budget, 0)]:
        credit(_phrase_rowids(conn, phrase, False, date_clause, date_params, cap), _EXACT_PHRASE)
    budget -= len(ids["phrases"][: max(budget, 0)])
    for sha in ids["shas"][: max(budget, 0)]:
        credit(_phrase_rowids(conn, sha, True, date_clause, date_params, cap), _EXACT_PHRASE)
    if not hits:
        return []

    ranked = sorted(hits.items(), key=lambda kv: (-kv[1][0], -kv[1][1], -kv[0]))
    out: List[Dict[str, Any]] = []
    for i in range(0, len(ranked), _IN_CHUNK):
        chunk = ranked[i : i + _IN_CHUNK]
        need = [rid for rid, _ in chunk if rid not in recs_by_id]
        if need:
            marks = ",".join("?" for _ in need)
            fetched = {
                int(row["id"]): row
                for row in conn.execute(
                    f"SELECT {_COLS} FROM memories m WHERE m.id IN ({marks}){date_clause}",
                    (*need, *date_params),
                ).fetchall()
            }
            for rid in need:
                row = fetched.get(rid)
                got = _post_filter([row], metadata_filters, tags_any, tags_all, tags_none, project) if row is not None else []
                recs_by_id[rid] = got[0] if got else None
        for rid, (exact, count) in chunk:  # rank order is preserved
            rec = recs_by_id.get(rid)
            if rec is None:
                continue
            out.append({"memory": rec, "score": exact * 1000.0 + count, "matched": count})
            if len(out) >= limit:
                return out
    return out


# --------------------------------------------------------------------------
# Fusion

def _paragraphs(query: str, min_chars: int, cap: int) -> List[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", query or "")]
    return [p for p in parts if len(p) > min_chars][:cap]


def weighted_rrf(
    legs: Sequence[Tuple[float, Sequence[int]]], rrf_k: float
) -> Dict[int, float]:
    """Sum of ``weight / (rrf_k + rank)`` (rank from 1) over each ranked id
    list. A zero-weight leg contributes nothing."""
    scores: Dict[int, float] = {}
    for weight, ranked in legs:
        if weight <= 0:
            continue
        for rank, mid in enumerate(ranked, start=1):
            scores[mid] = scores.get(mid, 0.0) + weight / (rrf_k + rank)
    return scores


def search_v2(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 10,
    min_score: float = 0.0,
    metadata_filters: Optional[Dict[str, Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags_any: Optional[List[str]] = None,
    tags_all: Optional[List[str]] = None,
    tags_none: Optional[List[str]] = None,
    follow: Optional[str] = None,
    project: Optional[str] = None,
    read_only: bool = False,
    coverage: Optional[Dict[str, Any]] = None,
    # knobs
    dense_depth: int = 100,
    lexical_depth: int = 100,
    identifier_depth: int = 50,
    rrf_k: float = 20.0,
    dense_weight: float = 1.0,
    lexical_weight: float = 1.5,
    identifier_weight: float = 1.0,
    decompose: bool = False,
    decompose_min_chars: int = 60,
    decompose_max: int = 6,
    decompose_weight: float = 0.5,
    max_terms: int = DEFAULT_MAX_TERMS,
    bm25_weights: Sequence[float] = DEFAULT_BM25_WEIGHTS,
) -> List[Dict[str, Any]]:
    """Dense + lexical + identifier results fused by weighted RRF.

    Returns ``[{"score", "memory", "cosine"}]`` like
    ``storage.hybrid_search_scored`` (cosine is the whole-query dense leg's,
    None when only another leg found the memory). ``follow`` is applied after
    fusion, as v1 does. Read-only: never writes memories, embeddings or FTS."""
    if not query or not query.strip():
        return []
    for name, val in (
        ("dense_weight", dense_weight), ("lexical_weight", lexical_weight),
        ("identifier_weight", identifier_weight), ("decompose_weight", decompose_weight),
    ):
        if not (isinstance(val, (int, float)) and math.isfinite(val) and val >= 0):
            raise ValueError(f"{name} must be a finite number >= 0")
    if not (math.isfinite(rrf_k) and rrf_k > 0):
        raise ValueError("rrf_k must be a finite number > 0")
    if min(dense_depth, lexical_depth, identifier_depth, max_terms) < 1 or top_k < 1:
        raise ValueError("depths, max_terms and top_k must be >= 1")
    filt = dict(
        metadata_filters=metadata_filters, date_from=date_from, date_to=date_to,
        tags_any=tags_any, tags_all=tags_all, tags_none=tags_none, project=project,
    )
    memories: Dict[int, Dict[str, Any]] = {}
    cosine: Dict[int, float] = {}
    legs: List[Tuple[float, List[int]]] = []

    def dense(text: str, track_cosine: bool) -> List[int]:
        res = storage.semantic_search(
            conn, text, top_k=dense_depth, min_score=None, read_only=read_only,
            coverage=coverage if track_cosine else None, **filt,
        )
        ids: List[int] = []
        for r in res:
            mem = r.get("memory", r)
            memories.setdefault(mem["id"], mem)
            if track_cosine:
                cosine[mem["id"]] = r.get("score", 0.0)
            ids.append(mem["id"])
        return ids

    # A leg with weight 0 is skipped entirely (no query, no embedding call).
    if identifier_weight > 0:
        ident = identifier_lookup(conn, query, limit=identifier_depth, **filt)
        legs.append((identifier_weight, [h["memory"]["id"] for h in ident]))
        for h in ident:
            memories.setdefault(h["memory"]["id"], h["memory"])

    if dense_weight > 0:
        legs.append((dense_weight, dense(query, True)))

    if lexical_weight > 0:
        lex = lexical_search(
            conn, query, limit=lexical_depth, max_terms=max_terms,
            bm25_weights=bm25_weights, **filt,
        )
        legs.append((lexical_weight, [h["memory"]["id"] for h in lex]))
        for h in lex:
            memories.setdefault(h["memory"]["id"], h["memory"])

    if decompose and decompose_weight > 0 and dense_weight > 0:
        paras = _paragraphs(query, decompose_min_chars, decompose_max)
        if len(paras) >= 2:
            for p in paras:
                legs.append((decompose_weight, dense(p, False)))

    scores = weighted_rrf(legs, rrf_k)
    # Equal fused scores: newer memory (higher id) first.
    order = sorted(scores, key=lambda i: (-scores[i], -i))
    results: List[Dict[str, Any]] = []
    for mid in order:
        if scores[mid] < min_score:
            continue
        results.append({
            "score": round(scores[mid], 6),
            "memory": memories[mid],
            "cosine": cosine.get(mid),
        })

    if follow:
        results = storage.apply_follow(conn, results, follow, is_search=True)
        if follow == "full_history":
            results = results[: top_k * 3]
        else:
            results = results[:top_k]
    else:
        results = results[:top_k]
    return results
