"""Deploy preflight for embeddings (E1b, leader 7981):

    python -m memora.embedding_preflight

Run in the NEW image, with memora-all's environment and data volume, before
the old container is stopped. For every store in MEMORA_DATABASES it reads
-- read-only -- the recorded embedding model and the representations of the
stored vectors, and decides whether semantic search would work under this
image's embedding model (MEMORA_EMBEDDING_MODEL):

- recorded model, same backend/model/representation (the endpoint host is
  ignored, E1): ok;
- no recorded model (written before E1) and vectors of the kind -- and, for
  a dense model, the dimension -- the current model produces: ok (served,
  reported "model unrecorded"; memory_verify_integrity(record_model=true)
  records it);
- anything else: the store would refuse searches -> the deploy refuses.

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


def _local_rows(backend) -> Callable[[str], List[Dict[str, Any]]]:
    def rows(sql: str) -> List[Dict[str, Any]]:
        conn = backend.connect_read_only()
        try:
            cur = conn.execute(sql)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    return rows


def _d1_rows(account_id: str, database_id: str) -> Callable[[str], List[Dict[str, Any]]]:
    from .backends import D1SelectOnlyConnection
    from .local_primary import D1Reader, read_token

    reader = D1Reader(D1SelectOnlyConnection(account_id, database_id, read_token()))
    return lambda sql: reader.rows(sql)


def store_reader(spec: str) -> Callable[[str], List[Dict[str, Any]]]:
    """A read-only SELECT function for one registry entry."""
    from .backends import LocalSQLiteBackend

    if spec.startswith("d1://"):
        account_id, database_id = spec[len("d1://"):].split("/", 1)
        return _d1_rows(account_id, database_id)
    if "://" in spec and not spec.startswith("file://"):
        raise ValueError(f"cannot check a {spec.split('://', 1)[0]}:// store read-only")
    path = spec[len("file://"):] if spec.startswith("file://") else spec
    return _local_rows(LocalSQLiteBackend(path))


def vector_reps(rows: Callable[[str], List[Dict[str, Any]]]) -> Dict[str, int]:
    """The audit's representation keys (dense:N, dense, sparse, other) with counts."""
    out: Dict[str, int] = {}
    for r in rows("SELECT representation, dimension, COUNT(*) AS n FROM memories_embeddings "
                  "WHERE embedding IS NOT NULL GROUP BY representation, dimension"):
        rep, dim, n = r["representation"], r["dimension"], int(r["n"])
        key = f"dense:{dim}" if rep == "dense" and dim is not None else (rep or "unknown")
        out[key] = out.get(key, 0) + n
    return out


def recorded_model(rows: Callable[[str], List[Dict[str, Any]]]) -> Optional[str]:
    got = rows("SELECT value FROM memories_meta WHERE key = 'embedding_model'")
    return got[0]["value"] if got else None


def check_store(reps: Dict[str, int], stored: Optional[str], current_model: str,
                probe_dim: Callable[[], Optional[int]]) -> Dict[str, Any]:
    """Would semantic search on this store work under current_model?"""
    from .embeddings import _model_mismatch_for_reps, unrecorded_compatibility

    kinds = sorted(k for k, n in reps.items() if n and k != "empty")
    out: Dict[str, Any] = {"recorded": stored, "vectors": {k: reps[k] for k in kinds}}
    if not kinds:
        return {**out, "ok": True, "state": "no vectors"}
    if len(kinds) > 1 or any(k not in ("sparse",) and not k.startswith("dense") for k in kinds):
        return {**out, "ok": False, "state": "mixed or unknown vectors", "fix": REBUILD_FIX}
    if stored is None:
        ok, dim, why = unrecorded_compatibility(reps, current_model)
        if ok and dim is not None:
            current = probe_dim()
            if current != dim:
                ok, why = False, f"{why}, but {current_model} now produces dense:{current}"
        if ok:
            return {**out, "ok": True, "state": "model unrecorded (compatible)",
                    "fix": "optional: memory_verify_integrity(record_model=true) records the current model"}
        return {**out, "ok": False, "state": f"model unrecorded and incompatible: {why}", "fix": REBUILD_FIX}
    if _model_mismatch_for_reps(reps, stored, current_model):
        return {**out, "ok": False, "state": f"recorded model differs from {current_model}", "fix": REBUILD_FIX}
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
        reader_for: Callable[[str], Callable[[str], List[Dict[str, Any]]]] = store_reader) -> Dict[str, Any]:
    from . import storage

    current_model = current_model or storage.EMBEDDING_MODEL
    registry = registry if registry is not None else storage.database_registry()
    probe = _probe_dim(current_model)
    stores: Dict[str, Any] = {}
    for name in sorted(registry):
        try:
            rows = reader_for(registry[name])
            stores[name] = check_store(vector_reps(rows), recorded_model(rows), current_model, probe)
        except Exception as exc:  # fail closed: an unchecked store refuses the deploy
            stores[name] = {"ok": False, "state": f"could not be checked: {type(exc).__name__}: {str(exc)[:200]}",
                            "fix": "make the store readable to this image (read token, data volume), then retry"}
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
