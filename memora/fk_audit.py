"""Local foreign-key parity (docs/local-primary-implementation.md §9 (x)).

D1 enforces foreign keys, so D1's ON DELETE CASCADE removes a memory's
memories_embeddings, memories_crossrefs and memories_events rows with it.
Local SQLite runs with foreign keys off unless a connection turns them on.
A live primary (and the L9a shadow file) therefore enables them on every
writer -- but only once an audit of the existing data finds no orphan: a
child row whose parent is missing would make the store diverge from D1 at
the next cascade, and with enforcement on some statements on it would fail.

The audit is SQLite's own `PRAGMA foreign_key_check`, the exact check that
enforcement applies, over every table that declares a foreign key; it reads
only. Each violation is reported by child table with the missing parent key
values.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

MAX_IDS = 200  # parent key values listed per table; the count is always exact


def fk_orphans(conn: sqlite3.Connection) -> Dict[str, Any]:
    """{"clean": bool, "orphans": {child table: {"parent", "column", "count",
    "ids"}}, "checked": [tables that declare a foreign key]}."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    checked: List[str] = []
    fks: Dict[str, Dict[int, Any]] = {}
    for t in tables:
        rows = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
        if rows:
            checked.append(t)
            # id, seq, table, from, to, ...: one entry per (fk id, column)
            fks[t] = {int(r[0]): (r[2], r[3]) for r in rows}
    orphans: Dict[str, Dict[str, Any]] = {}
    for child, rowid, parent, fkid in conn.execute("PRAGMA foreign_key_check").fetchall():
        ref_parent, column = fks.get(child, {}).get(int(fkid), (parent, None))
        entry = orphans.setdefault(child, {"parent": ref_parent, "column": column, "count": 0, "ids": []})
        entry["count"] += 1
        if column and rowid is not None and len(entry["ids"]) < MAX_IDS:
            row = conn.execute(f'SELECT "{column}" FROM "{child}" WHERE rowid = ?', (rowid,)).fetchone()
            if row is not None and row[0] not in entry["ids"]:
                entry["ids"].append(row[0])
    for entry in orphans.values():
        entry["ids"].sort(key=lambda v: (str(type(v)), v))
    return {"clean": not orphans, "orphans": orphans, "checked": checked}


def refusal_reason(result: Dict[str, Any]) -> str:
    """The one-line reason a store with orphans is refused (health shows it)."""
    parts = [f"{t} {e['count']} (missing {e['parent']}.{e['column'] or '?'}: "
             f"{', '.join(str(i) for i in e['ids'][:10])}{', ...' if e['count'] > 10 else ''})"
             for t, e in sorted(result["orphans"].items())]
    return ("fk_audit: foreign-key orphans in existing data: " + "; ".join(parts)
            + " -- run `local_primary.py fk-audit`, repair, then restart (plan §9 x)")
