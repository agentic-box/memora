#!/usr/bin/env python3
"""Issue #47 backfill APPLY: act on the user-approved rows of a preview file
(scripts/preview_backfill_47.py) -- and on nothing else.

Usage:
  python scripts/apply_backfill_47.py --preview approved.json [--db NAME]
      [--expect-count N] [--dry-run] [--allow-skips] [--report out.json]

1. ARTIFACT (checked before any store connection): an "approval" block with
   non-empty approved_by, at and rule; unique memory ids; the rows considered
   are exactly those with approved == true AND status == "proposed" (their
   count is printed, and must equal --expect-count when given); every
   considered row carries a proposal with a target, the fingerprints
   stored.content_sha256 (full content) and stored.metadata_sha256 (full
   canonical stored metadata), and a proposal.retag_typed equal to the
   re-prefixing recomputed here from the preview's stored tags and target.
   Any failure refuses the whole run. A target not configured for the store
   (MEMORA_PROJECTS), or a stored section that differs from the target,
   refuses that row ("refused").

2. PER ROW, inside one protected section -- on local SQLite a BEGIN IMMEDIATE
   transaction around the re-read, the checks and the write; on D1 the
   store's import lease, fenced right before the re-read -- the memory is
   re-read and classified:
     skipped-missing / skipped-pending (import marker) / skipped-retired
     (tombstoned or superseded);
     canonicalization-diff: the update would change more than
       {metadata.project -> target, the memory's own typed tags re-prefixed
       <target>/<kind>, hierarchy.path added when absent} -- e.g. legacy
       tasks/done or hierarchy forms, or images (which the write path would
       process) -- refused, never written;
     already-applied: project, tags and metadata already at the target AND
       the derived state current (FTS entry equal to the row, embedding
       present and equal to a recomputation for the current row -- cosine
       >= 0.9999 --, an "update" action row at or after updated_at);
     repair: project/tags already applied but some derived state lags ->
       update_memory is run again with force_reindex;
     skipped-stale: the row no longer matches the preview (section,
       subsection, tags, metadata.project, type, content_sha256,
       metadata_sha256);
     otherwise it is written.
   The write is ONE update_memory(conn, id, metadata={"project": target},
   expected_row=<the row just read>): memora's own rules apply as on any
   edit (project validation, typed-tag re-prefix, allowlist, embedding and
   FTS refresh, action row), and expected_row makes the UPDATE statement
   itself conditional on the row being unchanged, not tombstoned and not
   superseded -- a concurrent absorb/update in between is never
   overwritten ("raced"). After the write the FULL row is read back and
   diffed against the snapshot: only metadata (== the expected), tags (==
   the expected), updated_at, the embedding, the FTS entry, one action row
   and at most one event row (only when the event-trigger tag is present)
   may differ, else "failed". SECTION is never changed (every proposal's
   target equals its section).

3. FAIL-STOP: the first "failed" or "raced" row stops the run; the rest are
   "not-attempted". On D1 each statement autocommits, so a stop can leave a
   row part-written (row updated, FTS or embedding not); a re-run classifies
   it as "repair", completes it and reports it "repaired". On SQLite the transaction rolls back.
   A lost import lease also stops the run.

4. EXIT 0 only when every considered row is applied, repaired or
   already-applied; --allow-skips also accepts skipped-* (not failed, raced,
   refused, canonicalization-diff or not-attempted). --dry-run performs
   every check on a read-only connection (no schema setup, no lease), prints
   the expected diff per row, and writes nothing.

REPORTED OUTCOMES (the complete vocabulary):
  applied, repaired, already-applied      -- success
  would-apply, would-repair               -- --dry-run only (success)
  skipped-missing, skipped-pending,
  skipped-retired, skipped-stale          -- not written; accepted only with --allow-skips
  refused, canonicalization-diff          -- not written; never accepted
  raced, failed                           -- the run stops here
  not-attempted                           -- after a stop
("repair" in section 2 is the classification; its reported outcome is
"repaired", or "would-repair" in a dry run.)

Nothing else is modified: no deletions, no supersessions, no link or section
changes. Local SQLite and D1 stores only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import preview_backfill_47 as preview  # noqa: E402  (importing it has no side effects)

OK = {"applied", "repaired", "already-applied"}
SKIPS = {"skipped-missing", "skipped-pending", "skipped-retired", "skipped-stale"}
DRY_OK = {"would-apply", "would-repair", "already-applied"}
STOPPERS = {"failed", "raced"}
COSINE_EQUAL = 0.9999


class Refused(SystemExit):
    pass


# --- 1. the artifact -------------------------------------------------------------

def validate_artifact(data: Any, storage, expect_count: Optional[int]) -> List[Dict[str, Any]]:
    """The considered rows, or Refused (before any store connection)."""
    problems: List[str] = []
    approval = data.get("approval") if isinstance(data, dict) else None
    if not isinstance(approval, dict):
        raise Refused("refused: the preview has no approval block (only an approved preview can be applied)")
    for key in ("approved_by", "at", "rule"):
        if not (isinstance(approval.get(key), str) and approval[key].strip()):
            problems.append(f"approval.{key} missing or empty")
    ids: Dict[Any, int] = {}
    considered = []
    for group in ("contradictions", "keyword_only"):
        for row in data.get(group) or []:
            ids[row.get("id")] = ids.get(row.get("id"), 0) + 1
            if row.get("approved") is True and row.get("status") == "proposed":
                considered.append(dict(row, group=group))
    problems += [f"memory id {mid!r} appears {n} times" for mid, n in ids.items() if n > 1]
    print(f"considered rows (approved and proposed): {len(considered)}")
    if expect_count is not None and len(considered) != expect_count:
        problems.append(f"{len(considered)} considered rows, --expect-count {expect_count}")
    for row in considered:
        mid, stored, prop = row.get("id"), row.get("stored") or {}, row.get("proposal") or {}
        target = prop.get("set_metadata_project")
        if not isinstance(target, str) or not target:
            problems.append(f"#{mid}: no proposal target")
            continue
        for key in ("content_sha256", "metadata_sha256"):
            if not stored.get(key):
                problems.append(f"#{mid}: the preview lacks stored.{key} (regenerate it; --carry-approval)")
        tags = list(stored.get("tags") or [])
        meta = {"type": stored.get("type")}
        recomputed = {t: storage.project_tag(target, storage._typed_tag_kind(t))
                      for t in storage._existing_system_tags(tags, meta)
                      if t != storage.project_tag(target, storage._typed_tag_kind(t))}
        if prop.get("retag_typed") != recomputed:
            problems.append(f"#{mid}: proposal.retag_typed {prop.get('retag_typed')} != recomputed {recomputed}")
    if problems:
        raise Refused("refused before connecting:\n  " + "\n  ".join(problems))
    return considered


# --- 2. per-row state --------------------------------------------------------------

def snapshot(storage, conn, mid: int) -> Optional[Dict[str, Any]]:
    """Everything a write could touch, for this memory."""
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
    if row is None:
        return None
    mem = {k: row[k] for k in row.keys()}
    emb = conn.execute("SELECT * FROM memories_embeddings WHERE memory_id = ?", (mid,)).fetchone()
    fts = None
    fts_enabled = storage._fts_enabled(conn)
    if fts_enabled:
        f = conn.execute("SELECT content, metadata, tags FROM memories_fts WHERE rowid = ?", (mid,)).fetchone()
        fts = tuple(f) if f is not None else None
    crossref = conn.execute("SELECT related FROM memories_crossrefs WHERE memory_id = ?", (mid,)).fetchone()
    actions = conn.execute("SELECT action, timestamp FROM memories_actions WHERE memory_id = ? ORDER BY id",
                           (mid,)).fetchall()
    events = conn.execute("SELECT COUNT(*) FROM memories_events WHERE memory_id = ?", (mid,)).fetchone()[0]
    return {
        "memory": mem,
        "embedding": {k: emb[k] for k in emb.keys()} if emb is not None else None,
        "fts": fts,
        "fts_enabled": fts_enabled,
        "related": crossref[0] if crossref is not None else None,
        "actions": [tuple(a) for a in actions],
        "events": int(events),
    }


def _parsed(snap) -> Tuple[Dict[str, Any], List[str]]:
    raw = snap["memory"].get("metadata")
    try:
        meta = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        meta = {"__unparseable__": raw}
    tags = json.loads(snap["memory"].get("tags") or "[]")
    return (meta if isinstance(meta, dict) else {"__unparseable__": raw}), (tags if isinstance(tags, list) else [])


def expected_after(storage, meta: Dict[str, Any], tags: List[str], target: str):
    """(expected metadata, expected tags, list of scope violations) for the
    update, computed WITHOUT update_memory's side effects (no image upload)."""
    violations: List[str] = []
    if "__unparseable__" in meta:
        return None, None, ["metadata is not a JSON object"]
    if "images" in meta:
        violations.append("metadata.images present (the write path would process images)")
    try:
        new_meta = storage._build_metadata_dict(dict(storage._present_metadata(dict(meta)) or {}, project=target))
    except ValueError as exc:
        return None, None, [f"metadata cannot be canonicalised: {exc}"]
    for key in sorted(set(meta) | set(new_meta)):
        old, new = meta.get(key, "<absent>"), new_meta.get(key, "<absent>")
        if old == new:
            continue
        if key == "project" and new == target:
            continue
        if key == "hierarchy" and old == "<absent>":
            continue
        violations.append(f"metadata.{key}: {old!r} -> {new!r}")
    new_tags = storage._retarget_typed_tags(list(tags), target, meta)
    if len(new_tags) != len(tags):
        violations.append(f"tags {tags} -> {new_tags} (not a pure prefix change)")
    else:
        for old, new in zip(tags, new_tags):
            if old != new and not (storage._typed_tag_kind(old) and storage._typed_tag_kind(old) == storage._typed_tag_kind(new)
                                   and new == storage.project_tag(target, storage._typed_tag_kind(old))):
                violations.append(f"tag {old!r} -> {new!r}")
    return new_meta, new_tags, violations


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else (1.0 if not a and not b else 0.0)


def derived_lag(storage, snap) -> List[str]:
    """What of the derived state does not match the row (empty: all current)."""
    lag: List[str] = []
    mem = snap["memory"]
    if snap["fts_enabled"]:
        want = (mem["content"], mem.get("metadata") or "", mem.get("tags") or "")
        if snap["fts"] != want:
            lag.append("fts")
    emb = snap["embedding"]
    meta, tags = _parsed(snap)
    if emb is None or not emb.get("embedding"):
        lag.append("embedding missing")
    else:
        stored = storage._json_to_embedding(emb["embedding"])
        fresh = storage._compute_embedding(mem["content"], storage._present_metadata(meta) if meta else None, tags)
        if _cosine(stored or {}, fresh or {}) < COSINE_EQUAL:
            lag.append("embedding stale")
    updated = mem.get("updated_at") or ""
    if not any(action == "update" and (ts or "") >= updated for action, ts in snap["actions"]):
        lag.append("action row")
    return lag


def assess(storage, conn, row: Dict[str, Any], known: List[str]) -> Dict[str, Any]:
    mid = int(row["id"])
    stored, target = row.get("stored") or {}, row["proposal"]["set_metadata_project"]
    out: Dict[str, Any] = {"id": mid, "group": row["group"], "target": target}
    if target not in known:
        return dict(out, outcome="refused", detail=f"target {target!r} is not configured for this store")
    if stored.get("section") != target:
        return dict(out, outcome="refused", detail="the stored section differs from the target")
    snap = snapshot(storage, conn, mid)
    if snap is None:
        return dict(out, outcome="skipped-missing", detail="no such memory")
    out["snapshot"] = snap
    if storage._import_pending(snap["memory"].get("metadata")):
        return dict(out, outcome="skipped-pending", detail="an unfinished import still marks this row")
    if mid in storage._retired_ids_among(conn, [mid]):
        return dict(out, outcome="skipped-retired", detail="tombstoned")
    if mid in storage._superseded_ids_batch(conn, [mid]):
        return dict(out, outcome="skipped-retired", detail="superseded")
    meta, tags = _parsed(snap)
    new_meta, new_tags, violations = expected_after(storage, meta, tags, target)
    if violations:
        return dict(out, outcome="canonicalization-diff", detail="; ".join(violations))
    out["expected"] = {"metadata": new_meta, "tags": new_tags}
    out["diff"] = {k: [meta.get(k), new_meta.get(k)] for k in sorted(set(meta) | set(new_meta))
                   if meta.get(k) != new_meta.get(k)}
    if tags != new_tags:
        out["diff"]["tags"] = [tags, new_tags]
    if meta == new_meta and tags == new_tags:
        lag = derived_lag(storage, snap)
        if not lag:
            return dict(out, outcome="already-applied", detail="project, tags and derived state current")
        return dict(out, outcome="repair", detail="applied, but lagging: " + ", ".join(lag))
    checks = {
        "section": (meta.get("section"), stored.get("section")),
        "subsection": (meta.get("subsection"), stored.get("subsection")),
        "tags": (tags, list(stored.get("tags") or [])),
        "metadata.project": (meta.get("project"), stored.get("metadata_project")),
        "type": (meta.get("type"), stored.get("type")),
        "content_sha256": (hashlib.sha256((snap["memory"]["content"] or "").encode("utf-8")).hexdigest(),
                           stored.get("content_sha256")),
        "metadata_sha256": (preview.metadata_sha256(snap["memory"].get("metadata")), stored.get("metadata_sha256")),
    }
    changed = [k for k, (now, then) in checks.items() if now != then]
    if changed:
        return dict(out, outcome="skipped-stale", detail="changed since the preview: " + ", ".join(changed))
    return dict(out, outcome="apply", detail="")


# --- 3. the write --------------------------------------------------------------------

def verify_write(storage, conn, decision) -> Optional[str]:
    """None when the full row changed exactly as allowed, else what differs."""
    before, mid = decision["snapshot"], decision["id"]
    after = snapshot(storage, conn, mid)
    if after is None:
        return "the memory is gone after the write"
    problems = []
    meta, tags = _parsed(after)
    if meta != decision["expected"]["metadata"]:
        problems.append(f"metadata {meta} != expected {decision['expected']['metadata']}")
    if tags != decision["expected"]["tags"]:
        problems.append(f"tags {tags} != expected {decision['expected']['tags']}")
    for col, value in before["memory"].items():
        if col in ("metadata", "tags", "updated_at"):
            continue
        if after["memory"].get(col) != value:
            problems.append(f"memories.{col} changed")
    if after["related"] != before["related"]:
        problems.append("crossrefs changed")
    new_actions = after["actions"][len(before["actions"]):]
    if after["actions"][:len(before["actions"])] != before["actions"] or [a for a, _ in new_actions] != ["update"]:
        problems.append(f"action rows: {new_actions}")
    event_tag = storage.EVENT_TRIGGER_TAG in tags
    if after["events"] - before["events"] not in ((0, 1) if event_tag else (0,)):
        problems.append(f"event rows +{after['events'] - before['events']}")
    lag = derived_lag(storage, after)
    if lag:
        problems.append("derived state lagging: " + ", ".join(lag))
    return "; ".join(problems) or None


def write(storage, conn, decision) -> Dict[str, Any]:
    snap, mid = decision["snapshot"], decision["id"]
    guard = {
        "content": snap["memory"]["content"], "metadata": snap["memory"].get("metadata"),
        "tags": snap["memory"].get("tags"), "updated_at": snap["memory"].get("updated_at"),
        "related": snap["related"],
    }
    repairing = decision["outcome"] == "repair"
    try:
        storage.update_memory(conn, mid, metadata={"project": decision["target"]},
                              expected_row=guard, force_reindex=repairing)
    except storage.ConcurrentUpdateError as exc:
        _rollback(conn)
        return dict(decision, outcome="raced", detail=f"guarded UPDATE matched no row: {exc}")
    except Exception as exc:
        _rollback(conn)
        return dict(decision, outcome="failed", detail=f"write step failed: {type(exc).__name__}: {exc}")
    problem = verify_write(storage, conn, decision)
    if problem:
        return dict(decision, outcome="failed", detail=f"read-back: {problem}")
    if repairing:
        return dict(decision, outcome="repaired", detail=f"re-written and verified ({decision['detail']})")
    return dict(decision, outcome="applied", detail="written and verified")


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


# --- run ----------------------------------------------------------------------------

def run(preview_path: Path, db: Optional[str], dry_run: bool, expect_count: Optional[int]) -> Dict[str, Any]:
    data = json.loads(preview_path.read_text())
    preview._bootstrap(db)  # refuse a non-local, non-D1 store; pin; import memora
    storage = preview.storage
    from memora.backends import D1Connection

    considered = validate_artifact(data, storage, expect_count)  # before any connection
    token = storage.CURRENT_DB.set(db) if db else None
    results: List[Dict[str, Any]] = []
    try:
        known = list(storage.configured_projects(db) if db else storage.configured_projects())
        conn = storage.connect_without_schema() if dry_run else storage.connect()
        d1 = isinstance(conn, D1Connection)
        lease = None
        try:
            if not dry_run and d1:
                lease = storage._ImportLease(conn, uuid.uuid4().hex)
                lease.acquire()
            for index, row in enumerate(considered):
                if lease is not None:
                    try:
                        lease.fence()
                    except storage.ImportLeaseLostError as exc:
                        results.append({"id": int(row["id"]), "group": row["group"], "outcome": "failed",
                                        "detail": f"import lease lost: {exc}"})
                        results += [{"id": int(r["id"]), "group": r["group"], "outcome": "not-attempted",
                                     "detail": "the run stopped earlier"} for r in considered[index + 1:]]
                        break
                if not dry_run and not d1:
                    conn.execute("BEGIN IMMEDIATE")  # re-read, checks and write: one transaction
                decision = assess(storage, conn, row, known)
                if decision["outcome"] in ("apply", "repair") and not dry_run:
                    result = write(storage, conn, decision)
                else:
                    if not dry_run and not d1:
                        _rollback(conn)  # nothing to write: end the transaction
                    if dry_run and decision["outcome"] in ("apply", "repair"):
                        decision["outcome"] = "would-" + decision["outcome"]
                    result = decision
                results.append(result)
                if result["outcome"] in STOPPERS:
                    results += [{"id": int(r["id"]), "group": r["group"], "outcome": "not-attempted",
                                 "detail": f"the run stopped at #{result['id']}"} for r in considered[index + 1:]]
                    break
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
        r.pop("snapshot", None)
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return {"summary": {"preview": str(preview_path), "dry_run": dry_run, "considered": len(considered),
                        "outcomes": counts}, "rows": results}


def exit_status(report: Dict[str, Any], allow_skips: bool) -> int:
    ok = set(DRY_OK if report["summary"]["dry_run"] else OK)
    if allow_skips:
        ok |= SKIPS
    return 0 if all(r["outcome"] in ok for r in report["rows"]) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", required=True, type=Path, help="the approved preview JSON")
    ap.add_argument("--db", help="registry store name (MEMORA_DATABASES)")
    ap.add_argument("--expect-count", type=int, help="the number of approved+proposed rows expected")
    ap.add_argument("--dry-run", action="store_true", help="every check, no write")
    ap.add_argument("--allow-skips", action="store_true", help="skipped-* rows do not fail the run")
    ap.add_argument("--report", type=Path, help="write the JSON report here")
    args = ap.parse_args(argv)
    report = run(args.preview, args.db, args.dry_run, args.expect_count)
    for r in report["rows"]:
        line = f"#{r['id']} [{r['group']}] {r['outcome']}: {r.get('detail', '')}"
        if r.get("diff"):
            line += "  diff " + json.dumps(r["diff"], ensure_ascii=False)
        print(line)
    status = exit_status(report, args.allow_skips)
    if args.allow_skips and any(r["outcome"] in SKIPS for r in report["rows"]):
        print("!!! --allow-skips: skipped rows are accepted for the exit status !!!")
    print(("DRY RUN (nothing written): " if args.dry_run else "") + json.dumps(report["summary"]["outcomes"])
          + f"  exit {status}")
    if args.report:
        args.report.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
