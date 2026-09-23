"""Classify one SQL statement: read, mutation (with its target), ddl, or unknown.

docs/local-primary-implementation.md §2.9 ("Which statements are mirrored")
and §1 (freeze gate). One parser backs the write gate, the D1 freeze, the
SELECT-only reader and, later, the shadow classifier, so they cannot disagree
about what a read is.

The main statement decides, not the first keyword: leading whitespace and
comments are skipped, and a `WITH [RECURSIVE] name[(cols)] AS [NOT]
[MATERIALIZED] ( … )` prefix is skipped to reach it (CTE bodies are
SELECT-only in SQLite). More than one top-level statement is `unknown`.

PRAGMA is `read` only for the whitelisted read-only forms; every other PRAGMA
(optimize, wal_checkpoint, any `=` or setter form) is `ddl`-like.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

READ = "read"
MUTATION = "mutation"
DDL = "ddl"
TXN = "txn"
UNKNOWN = "unknown"

# Read-only PRAGMA forms (plan §2.9 P1-3). memora itself uses table_info and
# database_list; the rest are listed for tools. journal_mode and user_version
# are read only in their bare form (no argument, no `=`).
PRAGMA_READ_WITH_ARG = frozenset({
    "table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list",
})
PRAGMA_READ_BARE = frozenset({
    "database_list", "integrity_check", "quick_check", "journal_mode", "user_version",
})

# Primary keys of the tables whose effects reconciliation can read back.
PRIMARY_KEYS: Dict[str, Tuple[str, ...]] = {
    "memories": ("id",),
    "memories_embeddings": ("memory_id",),
    "memories_crossrefs": ("memory_id",),
    "tombstones": ("content_hash", "memory_id"),
    "tombstone_components": ("memory_id",),
    "memories_actions": ("id",),
    "memories_meta": ("key",),
}

_TXN_START = frozenset({"BEGIN"})
_TXN_END = frozenset({"COMMIT", "END", "ROLLBACK", "RELEASE"})


@dataclass(frozen=True)
class Classified:
    kind: str                 # read | mutation | ddl | txn | unknown
    main: str                 # main keyword, upper-case ("" when empty)
    target: Optional[str] = None
    txn_end: bool = False     # COMMIT / END / ROLLBACK / RELEASE

    @property
    def is_read(self) -> bool:
        return self.kind == READ


@dataclass
class _Tok:
    kind: str   # word | str | ident | punct | param | num
    text: str


def _tokenize(sql: str) -> List[_Tok]:
    """Tokens of `sql`, comments dropped. Raises ValueError on an unterminated
    literal or comment (treated as unknown by the caller)."""
    out: List[_Tok] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise ValueError("unterminated block comment")
            i = j + 2
        elif c in "'\"`[":
            close = "]" if c == "[" else c
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise ValueError("unterminated literal")
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        buf.append(close)
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            out.append(_Tok("str" if c == "'" else "ident", "".join(buf)))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            out.append(_Tok("word", sql[i:j]))
            i = j
        elif c.isdigit():
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            out.append(_Tok("num", sql[i:j]))
            i = j
        elif c in "?:@$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            out.append(_Tok("param", sql[i:j]))
            i = j
        else:
            if sql.startswith(("<=", ">=", "<>", "!=", "==", "||"), i):
                out.append(_Tok("punct", sql[i:i + 2]))
                i += 2
            else:
                out.append(_Tok("punct", c))
                i += 1
    return out


def _upper(tok: Optional[_Tok]) -> str:
    return tok.text.upper() if tok is not None and tok.kind == "word" else ""


def _name(tok: Optional[_Tok]) -> Optional[str]:
    if tok is None or tok.kind not in ("word", "ident"):
        return None
    return tok.text


def _split_statements(toks: List[_Tok]) -> List[List[_Tok]]:
    """Top-level statements. A `CREATE … TRIGGER … BEGIN … END` body keeps its
    inner `;` (they end the body's statements, not the CREATE)."""
    stmts, cur, depth, in_body, cases = [], [], 0, False, 0
    for t in toks:
        if t.kind == "punct" and t.text == "(":
            depth += 1
        elif t.kind == "punct" and t.text == ")":
            depth -= 1
        w = t.text.upper() if t.kind == "word" else ""
        if w == "BEGIN" and cur and _upper(cur[0]) == "CREATE" and any(_upper(x) == "TRIGGER" for x in cur):
            in_body = True
        elif w == "CASE" and in_body:
            cases += 1
        elif w == "END" and in_body:
            if cases:
                cases -= 1  # CASE … END inside the body
            else:
                in_body = False
        if t.kind == "punct" and t.text == ";" and depth == 0 and not in_body:
            if cur:
                stmts.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts


def _skip_with(toks: List[_Tok]) -> Optional[int]:
    """Index of the main statement after a WITH prefix, or None if malformed."""
    i = 1
    if _upper(toks[i] if i < len(toks) else None) == "RECURSIVE":
        i += 1
    while True:
        if _name(toks[i] if i < len(toks) else None) is None:
            return None
        i += 1
        if i < len(toks) and toks[i].kind == "punct" and toks[i].text == "(":
            i = _skip_parens(toks, i)
            if i is None:
                return None
        if _upper(toks[i] if i < len(toks) else None) != "AS":
            return None
        i += 1
        if _upper(toks[i] if i < len(toks) else None) == "NOT":
            i += 1
        if _upper(toks[i] if i < len(toks) else None) == "MATERIALIZED":
            i += 1
        if not (i < len(toks) and toks[i].kind == "punct" and toks[i].text == "("):
            return None
        i = _skip_parens(toks, i)
        if i is None:
            return None
        if i < len(toks) and toks[i].kind == "punct" and toks[i].text == ",":
            i += 1
            continue
        return i


def _skip_parens(toks: List[_Tok], i: int) -> Optional[int]:
    depth = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "punct" and t.text == "(":
            depth += 1
        elif t.kind == "punct" and t.text == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _pragma_kind(stmt: List[_Tok]) -> str:
    # PRAGMA [schema.]name [= value | (arg)]
    i = 1
    name = _name(stmt[i] if i < len(stmt) else None)
    if name is None:
        return UNKNOWN
    i += 1
    if i < len(stmt) and stmt[i].kind == "punct" and stmt[i].text == ".":
        name = _name(stmt[i + 1] if i + 1 < len(stmt) else None)
        if name is None:
            return UNKNOWN
        i += 2
    name = name.lower()
    rest = stmt[i:]
    if not rest:
        return READ if name in PRAGMA_READ_BARE else DDL
    if rest[0].kind == "punct" and rest[0].text == "(" and name in PRAGMA_READ_WITH_ARG:
        return READ
    return DDL


def _target_after(stmt: List[_Tok], i: int) -> Optional[str]:
    """Table name at stmt[i] (skipping an optional `schema.`)."""
    name = _name(stmt[i] if i < len(stmt) else None)
    if name is None:
        return None
    if i + 2 < len(stmt) and stmt[i + 1].kind == "punct" and stmt[i + 1].text == ".":
        return _name(stmt[i + 2])
    return name


def classify_statement(sql: str) -> Classified:
    try:
        toks = _tokenize(sql)
    except ValueError:
        return Classified(UNKNOWN, "")
    stmts = _split_statements(toks)
    if not stmts:
        return Classified(READ, "")  # empty / comment-only: sqlite executes nothing
    if len(stmts) > 1:
        return Classified(UNKNOWN, "MULTI")
    stmt = stmts[0]
    first = _upper(stmt[0])
    i = 0
    if first == "WITH":
        j = _skip_with(stmt)
        if j is None or j >= len(stmt):
            return Classified(UNKNOWN, "WITH")
        i = j
    main = _upper(stmt[i])
    if main in ("SELECT", "VALUES"):
        return Classified(READ, main)
    if main == "EXPLAIN" and first != "WITH":
        return Classified(READ, main)
    if main == "PRAGMA" and first != "WITH":
        return Classified(_pragma_kind(stmt), main)
    if main in ("INSERT", "REPLACE"):
        j = i + 1
        if main == "INSERT" and _upper(stmt[j] if j < len(stmt) else None) == "OR":
            j += 2
        if _upper(stmt[j] if j < len(stmt) else None) != "INTO":
            return Classified(UNKNOWN, main)
        target = _target_after(stmt, j + 1)
        return Classified(MUTATION if target else UNKNOWN, main, target)
    if main == "UPDATE":
        j = i + 1
        if _upper(stmt[j] if j < len(stmt) else None) == "OR":
            j += 2
        target = _target_after(stmt, j)
        return Classified(MUTATION if target else UNKNOWN, main, target)
    if main == "DELETE":
        if _upper(stmt[i + 1] if i + 1 < len(stmt) else None) != "FROM":
            return Classified(UNKNOWN, main)
        target = _target_after(stmt, i + 2)
        return Classified(MUTATION if target else UNKNOWN, main, target)
    if first != "WITH":
        if main in ("CREATE", "ALTER", "DROP", "REINDEX", "VACUUM", "ANALYZE", "ATTACH", "DETACH"):
            return Classified(DDL, main)
        if main in _TXN_START or main == "SAVEPOINT":
            return Classified(TXN, main)
        if main in _TXN_END:
            return Classified(TXN, main, txn_end=True)
    return Classified(UNKNOWN, main)


def is_select_only(sql: str) -> bool:
    """True only for one SELECT (optionally behind a WITH prefix): what the
    SELECT-only D1 reader accepts. EXPLAIN, PRAGMA and VALUES are refused."""
    c = classify_statement(sql)
    if c.kind != READ or c.main != "SELECT":
        return False
    try:
        toks = _tokenize(sql)
    except ValueError:
        return False
    return not any(t.kind == "word" and t.text.upper() == "RETURNING" for t in toks)


# ---------------------------------------------------------------- effects


def params_digest(params: Optional[Sequence[Any]]) -> str:
    payload = json.dumps(list(params or ()), default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_effect(sql: str, params: Optional[Sequence[Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(target, keys, post_state) for the simple statement shapes memora uses.

    keys: {pk_col: value} when every pk column is bound by a plain `?`.
    post_state: {column: value} for INSERT column lists and `SET col = ?`.
    Anything the parser cannot map positionally yields None for that part:
    the intent is still recorded, just without evidence to read back."""
    c = classify_statement(sql)
    if c.kind != MUTATION:
        return c.target, None, None
    params = list(params or ())
    try:
        toks = _split_statements(_tokenize(sql))[0]
    except (ValueError, IndexError):
        return c.target, None, None
    pk = PRIMARY_KEYS.get((c.target or "").lower())
    words = [t.text.upper() if t.kind == "word" else None for t in toks]
    try:
        if c.main in ("INSERT", "REPLACE"):
            lp = next(k for k, t in enumerate(toks) if t.kind == "punct" and t.text == "(")
            rp = _skip_parens(toks, lp) - 1
            cols = [t.text for t in toks[lp + 1:rp] if t.kind in ("word", "ident")]
            v = words.index("VALUES", rp)
            vlp = v + 1
            vrp = _skip_parens(toks, vlp) - 1
            vals = [t for t in toks[vlp + 1:vrp] if not (t.kind == "punct" and t.text == ",")]
            if len(vals) != len(cols) or any(t.kind != "param" or t.text not in ("?",) for t in vals):
                return c.target, None, None
            if len(params) < len(cols):
                return c.target, None, None
            post = dict(zip(cols, params[:len(cols)]))
            keys = {k: post[k] for k in pk} if pk and all(k in post for k in pk) else None
            return c.target, keys, post
        if c.main == "UPDATE":
            s = words.index("SET")
            w = words.index("WHERE") if "WHERE" in words else len(toks)
            post: Dict[str, Any] = {}
            pi = 0
            k = s + 1
            while k < w:
                col = _name(toks[k])
                if col is None or not (toks[k + 1].kind == "punct" and toks[k + 1].text == "="):
                    return c.target, None, None
                val = toks[k + 2]
                if val.kind == "param" and val.text == "?":
                    post[col] = params[pi]
                    pi += 1
                    k += 3
                else:
                    return c.target, None, None
                if k < w and toks[k].kind == "punct" and toks[k].text == ",":
                    k += 1
            keys = _where_keys(toks, w, params, pi, pk)
            return c.target, keys, post
        if c.main == "DELETE":
            w = words.index("WHERE") if "WHERE" in words else None
            if w is None:
                return c.target, None, None
            return c.target, _where_keys(toks, w, params, 0, pk), None
    except (StopIteration, ValueError, IndexError, TypeError):
        return c.target, None, None
    return c.target, None, None


def _where_keys(toks, w, params, pi, pk) -> Optional[Dict[str, Any]]:
    """`WHERE a = ? AND b = ? …`: the pk columns, when each is `col = ?` among
    the leading conjuncts. Any other shape yields None."""
    if not pk or w is None:
        return None
    keys: Dict[str, Any] = {}
    k = w + 1
    while k + 2 < len(toks) + 0 and k < len(toks):
        col = _name(toks[k])
        if col is None or k + 2 >= len(toks) or not (toks[k + 1].kind == "punct" and toks[k + 1].text == "="):
            break
        val = toks[k + 2]
        if not (val.kind == "param" and val.text == "?"):
            break
        if pi >= len(params):
            return None
        keys[col] = params[pi]
        pi += 1
        k += 3
        if k < len(toks) and _upper(toks[k]) == "AND":
            k += 1
            continue
        break
    return {c: keys[c] for c in pk} if all(c in keys for c in pk) else None
