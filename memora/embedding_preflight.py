"""Deploy preflight for embeddings (E1b, leader 7981):

    python -m memora.embedding_preflight

Run in the NEW image, with memora-all's environment and data volume, before
the old container is stopped. For every store in MEMORA_DATABASES it runs,
read-only, the SAME decision the server's search makes under this image's
embedding model (MEMORA_EMBEDDING_MODEL): the embedding integrity status
(orphan rows, unknown encodings, a rebuild in progress, a model or
representation mismatch, a missing model record) and the search gate
(review 8010: no separate re-implementation that could say ok where the
search refuses). A store whose search would refuse fails the preflight;
an unrecorded store with compatible vectors passes (reported "model
unrecorded"; memory_verify_integrity(record_model=true) records it).

Local stores are opened with connect_read_only() ONLY (never connect(): a
live primary's lock belongs to the running memora-all, and its WAL is read
through that writer's sidecars); d1:// stores through the read token
(MEMORA_D1_READ_TOKEN[_FILE]). No store is written, no lock is taken (the
service lock belongs to memora-all). A dense backend gets ONE probe
embedding to learn the current dimension.

Output: one JSON line. Exit 0: every store searchable. Exit 2: at least one
store would refuse searches, or could not be checked (fail closed); each is
named with the operator fix.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional

REBUILD_FIX = ("after the deploy, run the explicit memory_rebuild_embeddings tool on this store (it rewrites every "
               "embedding; on a live primary each one replicates to D1) -- or deploy with the embedding model its "
               "vectors were made with")


class _Row(tuple):
    """A result row readable by position and by column name, as sqlite3.Row
    is -- what the integrity code expects."""

    def __new__(cls, cols: List[str], values: List[Any]):
        row = super().__new__(cls, values)
        row._index = {c: i for i, c in enumerate(cols)}
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._index)


class _Cursor:
    def __init__(self, rows: List[_Row]):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class D1ReadConnection:
    """A read-only, DB-API-shaped view of a D1 database through the read
    token (D1SelectOnlyConnection refuses anything but a SELECT), so the
    SAME integrity status and search gate the server runs can run here."""

    def __init__(self, account_id: str, database_id: str):
        from .backends import D1SelectOnlyConnection
        from .local_primary import D1Reader, read_token

        self.account_id, self.database_id = account_id, database_id
        self._reader = D1Reader(D1SelectOnlyConnection(account_id, database_id, read_token()))

    def execute(self, sql: str, params=()):
        rows = self._reader.rows(sql, list(params or ()))
        out = []
        for r in rows:
            cols = list(r.keys())
            out.append(_Row(cols, [r[c] for c in cols]))
        return _Cursor(out)

    def close(self) -> None:
        return None


def open_read_only(spec: str):
    """A read-only connection for one registry entry: connect_read_only()
    for a local store (never connect()), the read token for d1://."""
    from .backends import LocalSQLiteBackend

    if spec.startswith("d1://"):
        account_id, database_id = spec[len("d1://"):].split("/", 1)
        return D1ReadConnection(account_id, database_id)
    if "://" in spec and not spec.startswith("file://"):
        raise ValueError(f"cannot check a {spec.split('://', 1)[0]}:// store read-only")
    path = spec[len("file://"):] if spec.startswith("file://") else spec
    return LocalSQLiteBackend(path).connect_read_only()


def check_store(conn, current_model: str, probe_dim: Callable[[], Optional[int]]) -> Dict[str, Any]:
    """Would semantic search on this store work under current_model? The
    SAME decision the server makes (review 8010): the integrity status
    (orphans, unknown encodings, a rebuild in progress, a model or
    representation mismatch, a missing model) and the search gate; for an
    unrecorded dense store, the query dimension via one probe."""
    from .embeddings import get_embedding_integrity_status, get_stored_embedding_model, invalidate_embedding_integrity_cache
    from .storage import SearchUnavailable, _read_only_search_gate

    invalidate_embedding_integrity_cache(conn)
    integrity = dict(get_embedding_integrity_status(conn, current_model))
    audit = integrity.get("audit") or {}
    stored = get_stored_embedding_model(conn)
    out: Dict[str, Any] = {"recorded": stored, "vectors": dict(audit.get("reps") or {}),
                           "integrity": integrity.get("reason")}
    try:
        _read_only_search_gate(conn, integrity, current_model)
    except SearchUnavailable as exc:
        fix = REBUILD_FIX if "model_mismatch" in str(exc) else (
            "repair the store's embedding integrity first (memory_verify_integrity names the rows), then "
            "rebuild explicitly if needed")
        return {**out, "ok": False, "state": f"search would refuse: {exc}", "fix": fix}
    dim = integrity.get("unrecorded_dimension")
    if dim is not None and probe_dim() != dim:
        return {**out, "ok": False, "fix": REBUILD_FIX,
                "state": f"model unrecorded: vectors dense:{dim}, but {current_model} now produces dense:{probe_dim()}"}
    if not integrity.get("mismatch"):
        return {**out, "ok": True, "state": "recorded model matches" if stored else "searchable"}
    if stored is None:
        return {**out, "ok": True, "state": "model unrecorded (compatible)",
                "fix": "optional: memory_verify_integrity(record_model=true) records the current model"}
    return {**out, "ok": True, "state": "recorded model matches"}


def _probe_dim(current_model: str) -> Callable[[], Optional[int]]:
    cache: Dict[str, Optional[int]] = {}

    def probe() -> Optional[int]:
        if "dim" not in cache:
            from .embeddings import compute_embedding

            vec = compute_embedding("memora embedding preflight probe", None, [], current_model)
            cache["dim"] = len(vec) if vec else None
        return cache["dim"]
    return probe


def run(current_model: Optional[str] = None, registry: Optional[Dict[str, str]] = None,
        opener: Callable[[str], Any] = open_read_only) -> Dict[str, Any]:
    from . import storage

    current_model = current_model or storage.EMBEDDING_MODEL
    registry = registry if registry is not None else storage.database_registry()
    probe = _probe_dim(current_model)
    stores: Dict[str, Any] = {}
    for name in sorted(registry):
        token = storage.CURRENT_DB.set(name)
        conn = None
        try:
            conn = opener(registry[name])
            stores[name] = check_store(conn, current_model, probe)
        except Exception as exc:  # fail closed: an unchecked store refuses the deploy
            stores[name] = {"ok": False, "state": f"could not be checked: {type(exc).__name__}: {str(exc)[:200]}",
                            "fix": "make the store readable to this image (read token, data volume), then retry"}
        finally:
            if conn is not None:
                conn.close()
            storage.CURRENT_DB.reset(token)
    return {"ok": all(s["ok"] for s in stores.values()), "model": current_model, "stores": stores}


def main() -> int:
    report = run()
    print(json.dumps(report, sort_keys=True))
    if not report["ok"]:
        for name, s in report["stores"].items():
            if not s["ok"]:
                print(f"embedding preflight: store {name!r} would refuse semantic search: {s['state']}. "
                      f"Fix: {s['fix']}", file=sys.stderr)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
