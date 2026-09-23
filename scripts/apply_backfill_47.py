#!/usr/bin/env python3
"""Issue #47 backfill APPLY: act on the user-approved rows of a preview file
(scripts/preview_backfill_47.py) -- and on nothing else.

Usage:
  python scripts/apply_backfill_47.py --preview approved.json [--db NAME] [--dry-run] [--report out.json]

Input: the preview must carry an "approval" block. Only rows with
approved == true AND status == "proposed" are considered; a considered row
whose proposal is missing, or whose target project is not configured for the
store (MEMORA_PROJECTS), is refused ("refused"). Everything else is ignored.

Per row, just before writing, the memory is re-read from the store:
  - gone -> "skipped-missing"; import-pending (an unfinished import) ->
    "skipped-pending"; retired (tombstoned) or superseded -> "skipped-retired";
  - already at the target (metadata.project == target and its typed tags
    already re-prefixed) -> "already-applied" (so a re-run is idempotent);
  - otherwise it must still match the preview's stored state -- section,
    subsection, tags, metadata.project, metadata.type and the content
    preview (the first 120 characters; plus content_sha256 when the preview
    recorded it) -- else "skipped-stale".
The write is ONE normal update_memory(conn, id, metadata={"project": target})
(metadata merged, not replaced): memora's own rules then apply exactly as for
any edit -- project validation, re-prefixing the memory's own typed tags to
<target>/<kind> (storage._retarget_typed_tags), the tag allowlist, and the
embedding refresh. All other tags, the content and every other metadata key
are left as they are -- except that, as on ANY edit through update_memory,
the metadata is stored in memora's canonical form (hierarchy.path derived
from section/subsection is added if absent). It is then read back and
verified (metadata.project, tags, section) -> "applied", or "failed" with
the error.

SECTION: not changed. Every proposal's target equals the stored section by
construction (a contradiction is proposed only when all non-typed evidence,
the section included, agrees; a keyword-only row's target IS its section),
and update_memory does not reassign sections; a considered row whose section
differs from its target is refused instead.

Nothing else is modified: no deletions, no supersessions, no link changes.
On D1 every row is its own autocommitted update, so the report is per row
and truthful; the whole run holds the store's import lease (as an import
does), proving ownership before each row. --dry-run performs every check on a
read-only connection (no schema setup, no lease) and writes nothing.
Local SQLite and D1 stores only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from preview_backfill_47 import refuse_unsupported_store  # noqa: E402  (no side effects)

PREVIEW_CHARS = 120


def load_approved(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("approval"), dict):
        raise SystemExit(f"{path}: no approval block -- refusing (only an approved preview can be applied)")
    rows = []
    for group in ("contradictions", "keyword_only"):
        for row in data.get(group) or []:
            if row.get("approved") is True and row.get("status") == "proposed":
                rows.append(dict(row, group=group))
    return rows


def _expected_tags(storage, tags: List[str], target: str, metadata: Dict[str, Any]) -> List[str]:
    return storage._retarget_typed_tags(list(tags), target, metadata)


def _current(storage, conn, memory_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT id, content, metadata, tags FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return None
    raw_meta = row[2]
    try:
        meta = json.loads(raw_meta) if raw_meta else {}
    except (TypeError, ValueError):
        meta = {}
    tags, _ok = storage._parse_tags_json(row[3], memory_id)
    return {"content": row[1] or "", "raw_metadata": raw_meta,
            "metadata": meta if isinstance(meta, dict) else {}, "tags": tags if isinstance(tags, list) else []}


def assess_row(storage, conn, row: Dict[str, Any], known: List[str]) -> Dict[str, Any]:
    """The decision for one approved row, with no write."""
    mid = int(row["id"])
    out: Dict[str, Any] = {"id": mid, "group": row["group"]}
    proposal = row.get("proposal") or {}
    target = proposal.get("set_metadata_project")
    stored = row.get("stored") or {}
    out["target"] = target
    if not target:
        return dict(out, outcome="refused", detail="proposal missing")
    if target not in known:
        return dict(out, outcome="refused", detail=f"target {target!r} is not configured for this store")
    if stored.get("section") != target:
        return dict(out, outcome="refused", detail="the stored section differs from the target")
    cur = _current(storage, conn, mid)
    if cur is None:
        return dict(out, outcome="skipped-missing", detail="no such memory")
    if storage._import_pending(cur["raw_metadata"]):
        return dict(out, outcome="skipped-pending", detail="an unfinished import still marks this row")
    if mid in storage._retired_ids_among(conn, [mid]):
        return dict(out, outcome="skipped-retired", detail="tombstoned")
    if mid in storage._superseded_ids_batch(conn, [mid]):
        return dict(out, outcome="skipped-retired", detail="superseded")
    meta, tags = cur["metadata"], cur["tags"]
    out["before"] = {"metadata_project": meta.get("project"), "tags": list(tags), "section": meta.get("section")}
    if meta.get("project") == target and _expected_tags(storage, tags, target, meta) == list(tags):
        return dict(out, outcome="already-applied", detail="metadata.project and typed tags already at the target")
    checks = {
        "section": (meta.get("section"), stored.get("section")),
        "subsection": (meta.get("subsection"), stored.get("subsection")),
        "tags": (list(tags), list(stored.get("tags") or [])),
        "metadata.project": (meta.get("project"), stored.get("metadata_project")),
        "type": (meta.get("type"), stored.get("type")),
        "content": (cur["content"][:PREVIEW_CHARS], row.get("preview")),
    }
    if stored.get("content_sha256"):
        checks["content_sha256"] = (hashlib.sha256(cur["content"].encode("utf-8")).hexdigest(),
                                    stored["content_sha256"])
    changed = [k for k, (now, then) in checks.items() if now != then]
    if changed:
        return dict(out, outcome="skipped-stale", detail=f"changed since the preview: {', '.join(changed)}")
    out["after"] = {"metadata_project": target, "tags": _expected_tags(storage, tags, target, meta),
                    "section": meta.get("section")}
    return dict(out, outcome="would-apply")


def apply_row(storage, conn, decision: Dict[str, Any]) -> Dict[str, Any]:
    mid, target = decision["id"], decision["target"]
    try:
        storage.update_memory(conn, mid, metadata={"project": target})
        conn.commit()
    except Exception as exc:
        return dict(decision, outcome="failed", detail=f"{type(exc).__name__}: {exc}")
    cur = _current(storage, conn, mid)
    if cur is None:
        return dict(decision, outcome="failed", detail="the memory is gone after the update")
    got = {"metadata_project": cur["metadata"].get("project"), "tags": list(cur["tags"]),
           "section": cur["metadata"].get("section")}
    if got != decision["after"]:
        return dict(decision, outcome="failed", detail=f"read back {got} != expected {decision['after']}")
    return dict(decision, outcome="applied", detail="updated")


def run(preview_path: Path, db: Optional[str], dry_run: bool) -> Dict[str, Any]:
    refuse_unsupported_store(db)
    from memora import storage
    from memora.backends import D1Connection

    rows = load_approved(preview_path)
    token = storage.CURRENT_DB.set(db) if db else None
    results: List[Dict[str, Any]] = []
    try:
        known = list(storage.configured_projects(db) if db else storage.configured_projects())
        conn = storage.connect_without_schema() if dry_run else storage.connect()
        lease = None
        try:
            if not dry_run and isinstance(conn, D1Connection):
                lease = storage._ImportLease(conn, uuid.uuid4().hex)
                lease.acquire()  # busy -> ImportLeaseBusyError: nothing written
            for index, row in enumerate(rows):
                decision = assess_row(storage, conn, row, known)
                if decision["outcome"] != "would-apply" or dry_run:
                    results.append(decision)
                    continue
                if lease is not None:
                    try:
                        lease.fence()
                    except storage.ImportLeaseLostError as exc:
                        results.append(dict(decision, outcome="failed", detail=f"import lease lost: {exc}"))
                        results += [dict(id=int(r["id"]), group=r["group"], outcome="not-attempted",
                                         detail="the run stopped: import lease lost") for r in rows[index + 1:]]
                        break
                results.append(apply_row(storage, conn, decision))
        finally:
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    pass
            conn.close()
    finally:
        if token is not None:
            storage.CURRENT_DB.reset(token)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return {"summary": {"preview": str(preview_path), "dry_run": dry_run, "approved_rows": len(rows),
                        "outcomes": counts},
            "rows": results}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", required=True, type=Path, help="the approved preview JSON")
    ap.add_argument("--db", help="registry store name (MEMORA_DATABASES)")
    ap.add_argument("--dry-run", action="store_true", help="every check, no write")
    ap.add_argument("--report", type=Path, help="write the JSON report here")
    args = ap.parse_args(argv)
    report = run(args.preview, args.db, args.dry_run)
    for r in report["rows"]:
        line = f"#{r['id']} [{r['group']}] {r['outcome']}: {r.get('detail', '')}"
        if r["outcome"] in ("would-apply", "applied") and r.get("after"):
            line += f"  tags {r['before']['tags']} -> {r['after']['tags']}; metadata.project " \
                    f"{r['before']['metadata_project']!r} -> {r['after']['metadata_project']!r}"
        print(line)
    print(("DRY RUN (nothing written): " if args.dry_run else "") + json.dumps(report["summary"]["outcomes"]))
    if args.report:
        args.report.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return 1 if any(r["outcome"] == "failed" for r in report["rows"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
