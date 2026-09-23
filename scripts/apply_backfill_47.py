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
   transaction around the re-read, the checks, the write AND the read-back
   verification; on D1 the store's import lease, fenced right before the
   re-read -- the memory is re-read and classified, in this order:
     skipped-missing / skipped-pending (import marker) / skipped-retired
       (tombstoned, or superseded -- including by a forward-only supersedes
       edge, which lives only in the superseding memory's crossrefs);
     skipped-stale if the full content no longer hashes to the preview's
       content_sha256 -- checked FIRST, whatever the project or tags;
     canonicalization-diff: the update of the PREVIEW's stored metadata and
       tags would change more than {metadata.project -> target, the memory's
       own typed tags re-prefixed <target>/<kind>, hierarchy.path added when
       absent} -- e.g. legacy tasks/done or hierarchy forms, or images (which
       the write path would process) -- refused, never written;
     the current row must then be EXACTLY the preview's state (full metadata
       hash and tags -> it is written) or EXACTLY the expected post-write
       state (-> already-applied when the derived state is current, else
       repair: update_memory again with force_reindex); anything else is
       skipped-stale. A row at the target that changed otherwise is never
       reindexed.
   Derived state (derived_lag): FTS entry equal to the row (SQLite); an
   embedding row with encoding_source "python" and the representation and
   dimension of a vector computed now for the row (the embeddings table
   records no model identifier) that matches it (cosine >= 0.9999); the
   newest action row by id an "update" at or after updated_at (timestamps
   are second-resolution). This is biased toward "repair" and may
   over-trigger -- float noise in a remote model's vectors, or any later
   action -- at the cost of a harmless idempotent reindex.
   KNOWN LIMITATION (heuristic derived-state check, accepted): a same-second
   action plus a near-identical stale vector can pass as current; a later
   link action or provider variance can over-trigger repair, which rewrites
   updated_at and an action row. There is no durable progress marker.
   The write is ONE update_memory(conn, id, metadata={"project": target},
   expected_row=<the row just read>, commit=False): memora's own rules apply
   as on any edit (project validation, typed-tag re-prefix, allowlist,
   embedding and FTS refresh, action row), and expected_row makes the UPDATE
   statement itself conditional on the row being unchanged, not tombstoned
   and not superseded (crossrefs value, and no supersedes edge to it in any
   crossrefs row) -- a concurrent change in between is never overwritten
   ("raced"). Then the FULL row is read back and diffed against the
   snapshot: only metadata (== the expected), tags (== the expected),
   updated_at, the embedding, the FTS entry, one action row and at most one
   event row (only with the event-trigger tag) may differ, else "failed"; a
   verification that raises is "verify-error". SECTION is never changed.

3. FAIL-STOP: the first "failed", "raced", "verify-error" or "uncertain" row
   stops the run -- any exception in a row's lease fence, BEGIN, assessment
   or write is caught per row ("failed" before the write, "uncertain" during
   it); the rest are "not-attempted". On SQLite the transaction is rolled
   back (nothing written). On D1 each statement has already committed, so a
   stop can leave a row written-but-unverified or part-written (row
   updated, FTS or embedding not); a re-run classifies it through the same
   assessment (already-applied, or repair -> "repaired"), never re-applying
   blindly. A lost import lease also stops the run.

4. EXIT 0 only when every considered row is applied, repaired or
   already-applied, with no summary.error; --allow-skips also accepts
   skipped-* (not failed, raced, verify-error, uncertain, refused,
   canonicalization-diff or not-attempted).
   REPORT: its destination is checked (directory exists, a probe file can be
   created and removed) BEFORE any store connection, else the run is
   refused. It is then written on every CLASSIFIED path: also when the
   artifact or the store (unknown, unsupported) is refused, the store cannot
   be opened or the import lease cannot be taken (summary.error, zero rows
   attempted). If the final write still fails, the full report is printed
   to stdout after a "REPORT WRITE FAILED" line, exit 1.
   INTERRUPTION (KeyboardInterrupt or any other BaseException) is the stated
   exception: the current row is marked "uncertain" (in its write) or
   "failed", the rest "not-attempted", summary.error "interrupted: <type>";
   the report is written best-effort and the exception re-raised. The lease
   (held from before its acquire, released owner-qualified) and the SQLite
   transaction are still cleaned up in finally.
   --dry-run performs every check on a read-only connection (no schema
   setup, no lease), prints the expected diff per row, and writes nothing.

REPORTED OUTCOMES (the complete vocabulary):
  applied, repaired, already-applied      -- success
  would-apply, would-repair               -- --dry-run only (success)
  skipped-missing, skipped-pending,
  skipped-retired, skipped-stale          -- not written; accepted only with --allow-skips
  refused, canonicalization-diff          -- not written; never accepted
  raced, failed, verify-error, uncertain  -- the run stops here ("uncertain":
                                             an unexpected error during the write,
                                             so it is unknown whether it landed)
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
import os
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
STOPPERS = {"failed", "raced", "verify-error", "uncertain"}
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
        if not isinstance(stored.get("metadata"), dict):
            problems.append(f"#{mid}: the preview lacks stored.metadata (regenerate it; --carry-approval)")
        elif stored.get("metadata_sha256") and preview.metadata_sha256(
                json.dumps(stored["metadata"])) != stored["metadata_sha256"]:
            problems.append(f"#{mid}: stored.metadata does not hash to stored.metadata_sha256")
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
    """What of the derived state does not match the row (empty: all current).

    Biased toward "repair" on any doubt -- repair is an idempotent reindex:
      - FTS (SQLite only; D1 has none): the entry must equal the row;
      - embedding: the row must exist, encoding_source "python", and carry
        the representation and dimension of a vector computed NOW for the
        current row (the embeddings table records no model identifier; the
        store's model is store-level metadata), and that vector must match
        the stored one (cosine >= 0.9999);
      - action: the newest action row for the memory (by id: the
        timestamps are second-resolution and name no write) must be an
        "update" at or after updated_at.
    It may over-trigger (float noise in a remote model's vectors, or any
    later action such as a link making "update" not the newest): the cost is
    a harmless reindex."""
    from memora.embeddings import _vector_representation

    lag: List[str] = []
    mem = snap["memory"]
    if snap["fts_enabled"]:
        want = (mem["content"], mem.get("metadata") or "", mem.get("tags") or "")
        if snap["fts"] != want:
            lag.append("fts")
    emb = snap["embedding"]
    meta, tags = _parsed(snap)
    fresh = storage._compute_embedding(mem["content"], storage._present_metadata(meta) if meta else None, tags)
    rep = _vector_representation(fresh or {})
    want_repr, want_dim = ("dense", int(rep.split(":", 1)[1])) if rep.startswith("dense:") else (rep, None)
    if emb is None or not emb.get("embedding"):
        lag.append("embedding missing")
    else:
        if emb.get("encoding_source") != "python" or emb.get("representation") != want_repr \
                or emb.get("dimension") != want_dim:
            lag.append(f"embedding provenance ({emb.get('representation')}/{emb.get('dimension')}/"
                       f"{emb.get('encoding_source')}, want {want_repr}/{want_dim}/python)")
        stored = storage._json_to_embedding(emb["embedding"])
        if _cosine(stored or {}, fresh or {}) < COSINE_EQUAL:
            lag.append("embedding stale")
    newest = snap["actions"][-1] if snap["actions"] else None
    if newest is None or newest[0] != "update" or (newest[1] or "") < (mem.get("updated_at") or ""):
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
    sources = storage._superseding_edge_sources(conn, mid)
    if sources:
        return dict(out, outcome="skipped-retired", detail=f"superseded (supersedes edge from {sources})")
    # 1. Content first: any change since the preview is stale, whatever the
    #    project or tags look like.
    content_hash = hashlib.sha256((snap["memory"]["content"] or "").encode("utf-8")).hexdigest()
    if content_hash != stored.get("content_sha256"):
        return dict(out, outcome="skipped-stale", detail="changed since the preview: content_sha256")
    # 2. The expected post-write state, from the PREVIEW's own full metadata.
    pre_meta, pre_tags = dict(stored["metadata"]), list(stored.get("tags") or [])
    new_meta, new_tags, violations = expected_after(storage, pre_meta, pre_tags, target)
    if violations:
        return dict(out, outcome="canonicalization-diff", detail="; ".join(violations))
    out["expected"] = {"metadata": new_meta, "tags": new_tags}
    out["diff"] = {k: [pre_meta.get(k), new_meta.get(k)] for k in sorted(set(pre_meta) | set(new_meta))
                   if pre_meta.get(k) != new_meta.get(k)}
    if pre_tags != new_tags:
        out["diff"]["tags"] = [pre_tags, new_tags]
    # 3. The current row must be EXACTLY the preview's state (-> write) or
    #    EXACTLY the expected post-write state (-> already applied / repair).
    meta, tags = _parsed(snap)
    at_pre = (preview.metadata_sha256(snap["memory"].get("metadata")) == stored.get("metadata_sha256")
              and tags == pre_tags)
    at_post = meta == new_meta and tags == new_tags
    if at_post and not at_pre:
        lag = derived_lag(storage, snap)
        if not lag:
            return dict(out, outcome="already-applied", detail="project, tags and derived state current")
        return dict(out, outcome="repair", detail="applied, but lagging: " + ", ".join(lag))
    if not at_pre:
        changed = [name for name, ok in (
            ("metadata_sha256", preview.metadata_sha256(snap["memory"].get("metadata")) == stored.get("metadata_sha256")),
            ("tags", tags == pre_tags)) if not ok]
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


def write(storage, conn, decision, *, transactional: bool) -> Dict[str, Any]:
    """The guarded update, then the full read-back verification. On local
    SQLite both run inside the caller's BEGIN IMMEDIATE transaction, which is
    committed only after a clean verification (else rolled back: nothing
    written). On D1 every statement has already committed: a verification
    that fails or errors reports the row as written but not verified."""
    snap, mid = decision["snapshot"], decision["id"]
    guard = {
        "content": snap["memory"]["content"], "metadata": snap["memory"].get("metadata"),
        "tags": snap["memory"].get("tags"), "updated_at": snap["memory"].get("updated_at"),
        "related": snap["related"],
    }
    repairing = decision["outcome"] == "repair"
    try:
        storage.update_memory(conn, mid, metadata={"project": decision["target"]},
                              expected_row=guard, force_reindex=repairing, commit=False)
    except storage.ConcurrentUpdateError as exc:
        _rollback(conn)
        return dict(decision, outcome="raced", detail=f"guarded UPDATE matched no row: {exc}")
    except Exception as exc:
        _rollback(conn)
        return dict(decision, outcome="failed", detail=f"write step failed: {type(exc).__name__}: {exc}")
    try:
        problem = verify_write(storage, conn, decision)
    except Exception as exc:
        if transactional:
            _rollback(conn)
            return dict(decision, outcome="verify-error",
                        detail=f"verification raised {type(exc).__name__}: {exc}; rolled back (nothing written)")
        return dict(decision, outcome="verify-error",
                    detail=f"verification raised {type(exc).__name__}: {exc}; WRITTEN, not verified "
                           "(a re-run reassesses the row)")
    if problem:
        if transactional:
            _rollback(conn)
            return dict(decision, outcome="failed", detail=f"read-back: {problem}; rolled back")
        return dict(decision, outcome="failed", detail=f"read-back: {problem}")
    conn.commit()
    if repairing:
        return dict(decision, outcome="repaired", detail=f"re-written and verified ({decision['detail']})")
    return dict(decision, outcome="applied", detail="written and verified")


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


# --- run ----------------------------------------------------------------------------

def _stopped(rows: List[Dict[str, Any]], detail: str) -> List[Dict[str, Any]]:
    return [{"id": int(r["id"]), "group": r["group"], "outcome": "not-attempted", "detail": detail} for r in rows]


def _finalize(report: Dict[str, Any]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for r in report["rows"]:
        r.pop("snapshot", None)
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    report["summary"]["outcomes"] = counts
    return report


def _write_report(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _report_text(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n"


def preflight_report(path: Path) -> Optional[str]:
    """None when the report can be written (its directory exists and accepts
    a probe file, created and removed), else why not -- checked BEFORE any
    store connection, so a run never writes rows it cannot report."""
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.is_dir():
        return f"the report directory {parent} does not exist"
    probe = parent / f".{path.name}.probe-{uuid.uuid4().hex}"
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        probe.unlink()
    except OSError as exc:
        return f"the report directory {parent} is not writable: {exc}"
    return None


def run(preview_path: Path, db: Optional[str], dry_run: bool, expect_count: Optional[int],
        report_path: Optional[Path] = None) -> Dict[str, Any]:
    """Returns a report for every CLASSIFIED path -- a setup refusal (unknown or
    unsupported store, refused artifact), a store or lease failure, any
    per-row error -- recorded in summary.error, with every row not reached
    reported as "not-attempted".

    INTERRUPTION (KeyboardInterrupt or any other BaseException): the current
    row is marked "uncertain" if it was in its write stage, else "failed";
    the rest "not-attempted"; summary.error = "interrupted: <type>"; the
    report is written best-effort to report_path; and the exception is
    re-raised. The finally blocks still release the lease and roll back the
    SQLite transaction."""
    report: Dict[str, Any] = {"summary": {"preview": str(preview_path), "dry_run": dry_run, "considered": 0,
                                          "error": None, "refused": False, "outcomes": {}},
                              "rows": []}
    results: List[Dict[str, Any]] = report["rows"]
    considered: List[Dict[str, Any]] = []
    storage = None
    token = conn = lease = primary_fd = None
    transactional = False
    current = {"row": None, "stage": "setup"}
    try:
        data = json.loads(preview_path.read_text())
        try:
            preview._bootstrap(db)  # refuse a non-local, non-D1 store; pin; import memora
        except Refused:
            raise
        except SystemExit as exc:  # the preview's own refusal (unsupported or unknown store)
            raise Refused(f"refused: {exc.code}")
        storage = preview.storage
        from memora.backends import D1Connection

        considered = validate_artifact(data, storage, expect_count)  # before any connection
        report["summary"]["considered"] = len(considered)
        token = storage.CURRENT_DB.set(db) if db else None
        known = list(storage.configured_projects(db) if db else storage.configured_projects())
        if not dry_run:
            # A live local primary is written only by memora-all, which holds
            # its primary lock (docs/local-primary-implementation.md §1 M10):
            # take the lock or refuse.
            from memora.backends import LocalSQLiteBackend, StoreLockedError, acquire_primary_lock

            backend = storage.backend_for(db) if db else storage.STORAGE_BACKEND
            if isinstance(backend, LocalSQLiteBackend) and backend.live_primary:
                try:
                    primary_fd = acquire_primary_lock(backend.db_path)
                except StoreLockedError as exc:
                    raise Refused(f"refused: {exc}; apply through memora-all's API or stop it first")
        conn = storage.connect_without_schema() if dry_run else storage.connect()
        d1 = isinstance(conn, D1Connection)
        transactional = not dry_run and not d1
        if not dry_run and d1:
            # Held BEFORE acquire: an acquire that inserted its row and then
            # failed (read-back, interruption) is still released in finally;
            # the release is owner-qualified, a no-op if nothing was inserted.
            lease = storage._ImportLease(conn, uuid.uuid4().hex)
            lease.acquire()
        for index, row in enumerate(considered):
            current.update(row=row, stage="lease fence")
            try:
                if lease is not None:
                    lease.fence()
                current["stage"] = "BEGIN IMMEDIATE"
                if transactional:
                    conn.execute("BEGIN IMMEDIATE")  # re-read, checks, write, read-back: one transaction
                current["stage"] = "assessment"
                decision = assess(storage, conn, row, known)
                if decision["outcome"] in ("apply", "repair") and not dry_run:
                    current["stage"] = "write"
                    result = write(storage, conn, decision, transactional=transactional)
                else:
                    if transactional:
                        _rollback(conn)  # nothing to write: end the transaction
                    if dry_run and decision["outcome"] in ("apply", "repair"):
                        decision["outcome"] = "would-" + decision["outcome"]
                    result = decision
            except Exception as exc:
                if transactional:
                    _rollback(conn)
                lost = storage is not None and isinstance(exc, storage.ImportLeaseLostError)
                result = {"id": int(row["id"]), "group": row["group"],
                          # during the write it is unknown whether it landed (D1 commits per statement)
                          "outcome": "uncertain" if current["stage"] == "write" else "failed",
                          "detail": (f"import lease lost: {exc}" if lost
                                     else f"{current['stage']} raised {type(exc).__name__}: {exc}")}
            results.append(result)
            current["row"] = None
            if result["outcome"] in STOPPERS:
                results += _stopped(considered[index + 1:], f"the run stopped at #{result['id']}")
                break
    except Refused as exc:
        report["summary"].update(error=str(exc.code), refused=True)
    except Exception as exc:
        report["summary"]["error"] = f"{type(exc).__name__}: {exc}"
        results += _stopped(considered[len(results):], f"the run could not start or continue: {exc}")
    except BaseException as exc:  # interruption: record, write best-effort, re-raise
        row = current["row"]
        if row is not None:
            results.append({"id": int(row["id"]), "group": row["group"],
                            "outcome": "uncertain" if current["stage"] == "write" else "failed",
                            "detail": f"interrupted ({type(exc).__name__}) in {current['stage']}"})
        results += _stopped(considered[len(results):], f"interrupted: {type(exc).__name__}")
        report["summary"]["error"] = f"interrupted: {type(exc).__name__}"
        _finalize(report)
        if report_path is not None:
            try:
                _write_report(report_path, _report_text(report))
            except BaseException:
                print("REPORT WRITE FAILED (interrupted run):\n" + _report_text(report))
        raise
    finally:
        if lease is not None:
            try:
                lease.release()
            except Exception:
                pass
        if conn is not None:
            if transactional:
                _rollback(conn)
            try:
                conn.close()
            except Exception:
                pass
        if token is not None:
            try:
                storage.CURRENT_DB.reset(token)
            except Exception:
                pass
        if primary_fd is not None:
            os.close(primary_fd)
    return _finalize(report)


def exit_status(report: Dict[str, Any], allow_skips: bool) -> int:
    if report["summary"].get("error"):
        return 1
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
    ap.add_argument("--report", type=Path, help="write the JSON report here (preflighted before connecting)")
    args = ap.parse_args(argv)
    if args.report is not None:
        problem = preflight_report(args.report)
        if problem:
            raise SystemExit(f"refused before connecting: {problem}")
    report = run(args.preview, args.db, args.dry_run, args.expect_count, report_path=args.report)
    for r in report["rows"]:
        line = f"#{r['id']} [{r['group']}] {r['outcome']}: {r.get('detail', '')}"
        if r.get("diff"):
            line += "  diff " + json.dumps(r["diff"], ensure_ascii=False)
        print(line)
    status = exit_status(report, args.allow_skips)
    if args.report is not None:
        try:
            _write_report(args.report, _report_text(report))
        except OSError as exc:
            print(f"REPORT WRITE FAILED ({args.report}: {exc}); the full report follows:")
            print(_report_text(report))
            status = 1
    if args.allow_skips and any(r["outcome"] in SKIPS for r in report["rows"]):
        print("!!! --allow-skips: skipped rows are accepted for the exit status !!!")
    print(("DRY RUN (nothing written): " if args.dry_run else "") + json.dumps(report["summary"]["outcomes"])
          + f"  exit {status}")
    if report["summary"]["refused"]:
        raise SystemExit(report["summary"]["error"])  # exit 1, with the reason, after the report
    if report["summary"]["error"]:
        print(f"ERROR: {report['summary']['error']}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
