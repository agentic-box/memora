"""Evidence for open D1 write intents. It never resolves anything.

docs/local-primary-implementation.md §1 "Reconciliation": every open intent
needs an operator (`reconcile --accept`). No intent carries a unique proof
of its effect -- INSERTs omit the AUTOINCREMENT id and identical rows can
already exist; a no-op UPDATE or a DELETE of an absent key cannot prove
"not applied" -- so memora-all only reads back what it can derive, at least
RECONCILE_MIN_AGE_S after the request was sent, through the SELECT-only D1
reader with the read token, and shows it (health, admin endpoint).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

from .intent_journal import RECONCILE_MIN_AGE_S
from .sql_classify import PRIMARY_KEYS

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIMARY_RETRIES = 5
PRIMARY_RETRY_DELAY_S = 0.2


def _evidence_query(rec: Dict[str, Any]):
    target = rec.get("target")
    if target not in PRIMARY_KEYS:
        return None, None, "the target table is not one reconciliation reads"
    keys, post = rec.get("keys"), rec.get("post_state")
    if keys and all(_IDENT.match(k) for k in keys):
        where = " AND ".join(f"{k} = ?" for k in keys)
        return f"SELECT * FROM {target} WHERE {where}", list(keys.values()), None
    if rec.get("sql", "").lstrip().upper().startswith("INSERT") and post:
        cols = [c for c, v in post.items() if _IDENT.match(c) and (v is None or isinstance(v, (str, int, float)))]
        if cols:
            where = " AND ".join(f"{c} IS ?" for c in cols)
            return f"SELECT * FROM {target} WHERE {where} LIMIT 5", [post[c] for c in cols], None
    return None, None, "no keys or values could be derived from the statement"


def gather_evidence(backend, journal, *, reader=None, now: Optional[float] = None,
                    sleep: Callable[[float], None] = time.sleep) -> Dict[int, Dict[str, Any]]:
    """Refresh journal.evidence for every open intent; return it."""
    now = time.time() if now is None else now
    out: Dict[int, Dict[str, Any]] = {}
    for rec in journal.open_intents():
        iid = int(rec["id"])
        age = now - float(rec.get("sent_at") or now)
        if age < RECONCILE_MIN_AGE_S:
            out[iid] = {"status": "waiting", "eligible_in_s": round(RECONCILE_MIN_AGE_S - age, 1)}
            continue
        sql, params, why = _evidence_query(rec)
        if sql is None:
            out[iid] = {"status": "no-evidence", "reason": why}
            continue
        try:
            if reader is None:
                from .backends import D1SelectOnlyConnection

                reader = D1SelectOnlyConnection.from_env(backend.account_id, backend.database_id)
            rows: List[Dict[str, Any]] = []
            primary = False
            for attempt in range(PRIMARY_RETRIES):
                rows, meta = reader.execute(sql, params)
                primary = bool((meta or {}).get("served_by_primary"))
                if primary:
                    break
                sleep(PRIMARY_RETRY_DELAY_S)
            out[iid] = {"status": "read", "query": sql, "rows": rows[:5], "row_count": len(rows),
                        "served_by_primary": primary, "read_at": now}
        except Exception as exc:  # evidence is advisory; report, never raise
            out[iid] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    journal.evidence.clear()
    journal.evidence.update(out)
    return out
