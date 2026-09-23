#!/usr/bin/env python3
"""Issue #47 backfill PREVIEW with an explicit approval list. READ-ONLY.

Writes NOTHING and creates nothing; it may REFUSE. A local SQLite store opens
read-only (mode=ro, or immutable for a WAL file with no sidecars); a local
WAL store WITH sidecars is in use by a writer process and is refused (exit
non-zero: stop the server, or use --db against D1). D1 opens through a raw
connection with no schema pass. S3 cloud stores are refused (their connect
can sync the local cache). SELECTs only. Its
output is a preview FILE the user reviews: every row carries
"approved": false, and a later apply step (a separate item) may act only on
rows the user flipped to true.

Two groups, per memory (import-pending rows are skipped):

  contradictions  The stored section (a project, assigned by the removed
                  keyword heuristics) differs from the project its TAGS gave
                  under the old rule, where a typed tag (<p>/issues, todos,
                  sections, documents, knowledge) counted as project evidence
                  -- e.g. a clmux issue whose only memora tag is the old
                  default memora/issues. The target project is proposed from
                  NON-TYPED evidence only: metadata.project, non-typed
                  "<project>/..." tags, and the stored section. Evidence that
                  disagrees, or none at all, is "needs-human".

  keyword_only    No explicit project (no metadata.project, no non-typed
                  project tag) but a stored section naming a configured
                  project. Proposed only where that section came from a
                  NON-TYPED source (the old content keyword detector gives
                  the same project); where the section is explained by a
                  typed tag alone, or by nothing found, "needs-human".

A proposal declares the project with metadata.project (the strongest
explicit marker, touching no tags and no tag policy) and re-prefixes the
memory's own typed tags to it. As an ALTERNATIVE the file also lists a
"<section>/<subsection>" marker tag where a subsection exists; the apply
step uses whichever the reviewer approves.

Usage:
  python scripts/preview_backfill_47.py --out preview.json [--db NAME] [--markdown preview.md]
Set MEMORA_PROJECTS as the server runs with it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

UNSUPPORTED_STORE = ("the preview supports local SQLite and D1 stores only: {uri!r} is refused "
                     "(an S3 cloud store's backend syncs a local cache; nothing was opened)")


def metadata_sha256(raw_metadata: Optional[str]) -> str:
    """Fingerprint of a memory's FULL stored metadata: sha256 of its JSON
    re-serialised canonically (sorted keys, no whitespace); malformed JSON
    hashes as its raw text, none as the empty string. Shared with the apply
    step (scripts/apply_backfill_47.py), which recomputes it."""
    import hashlib

    text = raw_metadata or ""
    if text:
        try:
            text = json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            pass
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def carry_approval(preview: Dict[str, Any], old: Dict[str, Any], old_path: str) -> List[str]:
    """Copy approved=true from an older approved preview onto THIS preview,
    mechanically: only for a memory id that OLD approved with status
    proposed AND that this preview also proposes with the identical target,
    identical proposal.retag_typed and identical stored section. Every other
    row stays unapproved (back to the user). Returns a per-row table."""
    if not isinstance(old.get("approval"), dict):
        raise SystemExit(f"{old_path}: no approval block to carry")
    new_rows = {r["id"]: r for g in ("contradictions", "keyword_only") for r in preview.get(g, [])}
    lines, carried, dropped = [], 0, 0
    for group in ("contradictions", "keyword_only"):
        for o in old.get(group) or []:
            if not (o.get("approved") is True and o.get("status") == "proposed"):
                continue
            n = new_rows.get(o["id"])
            op, np_ = o.get("proposal") or {}, (n or {}).get("proposal") or {}
            if n is None:
                reason = "not in the new preview"
            elif n.get("status") != "proposed":
                reason = f"new status {n.get('status')!r}"
            elif np_.get("set_metadata_project") != op.get("set_metadata_project"):
                reason = f"target {op.get('set_metadata_project')!r} -> {np_.get('set_metadata_project')!r}"
            elif np_.get("retag_typed") != op.get("retag_typed"):
                reason = "retag_typed differs"
            elif (n.get("stored") or {}).get("section") != (o.get("stored") or {}).get("section"):
                reason = "stored section differs"
            else:
                reason = None
            if reason is None:
                n["approved"] = True
                carried += 1
                lines.append(f"#{o['id']}\tcarried\t{op.get('set_metadata_project')}")
            else:
                dropped += 1
                lines.append(f"#{o['id']}\tdropped\t{reason}")
    preview["approval"] = dict(old["approval"], carried_from=old_path, carried=carried, dropped=dropped)
    lines.append(f"carried {carried}, dropped {dropped} (dropped rows stay unapproved: back to the user)")
    return lines


def configured_uri(db_name: Optional[str]) -> Optional[str]:
    """The store URI the preview would open, from the environment TEXT only
    -- no backend is resolved or constructed (a cloud backend's constructor
    already creates its cache directory and an S3 client). None: the legacy
    local database (MEMORA_DB_PATH or the default path)."""
    import os

    raw = os.getenv("MEMORA_DATABASES", "").strip()
    if raw:
        registry = json.loads(raw)
        name = db_name or os.getenv("MEMORA_DEFAULT_DB", "").strip() or (
            next(iter(registry)) if len(registry) == 1 else None)
        if name not in registry:
            raise SystemExit(f"unknown store {name!r} (MEMORA_DATABASES: {sorted(registry)})")
        return str(registry[name])
    if db_name:
        raise SystemExit("--db needs MEMORA_DATABASES")
    return os.getenv("MEMORA_STORAGE_URI") or None


def refuse_unsupported_store(db_name: Optional[str]) -> None:
    """Allow only a local path, file://, or d1://; refuse s3:// and anything
    else BEFORE any backend exists."""
    uri = configured_uri(db_name)
    if uri is None or "://" not in uri or uri.startswith(("file://", "d1://")):
        return
    raise SystemExit(UNSUPPORTED_STORE.format(uri=uri))


def _bootstrap(db_name: Optional[str]) -> None:
    """Refuse an unsupported store, pin the import-time backend, THEN import
    memora (whose storage module builds a backend at import)."""
    global storage, legacy, CloudSQLiteBackend, LocalSQLiteBackend
    import os

    refuse_unsupported_store(db_name)
    pinned = "memora.storage" not in sys.modules  # already imported: nothing left to pin
    saved = os.environ.get("MEMORA_STORAGE_URI")
    if pinned:
        pin_import_time_backend(db_name)
    try:
        import _legacy_project_detection as _legacy
        from memora import storage as _storage
        from memora.backends import CloudSQLiteBackend as _Cloud, LocalSQLiteBackend as _Local
    finally:
        # The pin only has to hold for that first import; leave the caller's
        # environment as it was.
        if pinned:
            if saved is None:
                os.environ.pop("MEMORA_STORAGE_URI", None)
            else:
                os.environ["MEMORA_STORAGE_URI"] = saved

    storage, legacy, CloudSQLiteBackend, LocalSQLiteBackend = _storage, _legacy, _Cloud, _Local


def pin_import_time_backend(db_name: Optional[str]) -> None:
    """memora.storage builds a module-level backend from MEMORA_STORAGE_URI
    at IMPORT, whatever the registry selects. With a registry present, the
    registry's selection is what the preview reads, so MEMORA_STORAGE_URI is
    set to that selected (already allowed) URI for this process: the
    import-time backend is then the selected local or D1 store, never an S3
    cloud one (whose constructor creates its cache directory)."""
    import os

    if os.getenv("MEMORA_DATABASES", "").strip():
        os.environ["MEMORA_STORAGE_URI"] = configured_uri(db_name)



# memora and the legacy detector are imported by _bootstrap() inside main(),
# AFTER the store is checked and the import-time backend pinned: importing
# this module has no side effects.
storage = legacy = CloudSQLiteBackend = LocalSQLiteBackend = None


class StoreInUse(SystemExit):
    pass


def open_read_only():
    """A connection that cannot write and creates nothing -- or a refusal.

    Local SQLite: a WAL database with a -wal or -shm present has a writer in
    another process (e.g. the memora server): reading it would need its
    locks, and if that writer closed between our check and our open the read
    would recreate its sidecars. So it is REFUSED. With no sidecars it opens
    immutable (no locks, nothing created); a rollback-journal database opens
    mode=ro. D1 and others: a raw connection with no schema pass.
    Guarantee: creates nothing, may refuse.
    """
    backend = storage.current_backend()
    if isinstance(backend, CloudSQLiteBackend):  # belt and braces: refused earlier by URI
        raise SystemExit(UNSUPPORTED_STORE.format(uri=getattr(backend, "cloud_url", "s3")))
    if isinstance(backend, LocalSQLiteBackend):
        path = backend.db_path
        if not path.is_file():
            raise SystemExit(f"no database at {path}")
        with open(path, "rb") as fh:
            header = fh.read(20)  # only the header, never the whole database
        wal = len(header) >= 20 and (header[18] == 2 or header[19] == 2)
        if wal and (Path(f"{path}-wal").exists() or Path(f"{path}-shm").exists()):
            raise StoreInUse(
                f"store in use by a writer ({path} has WAL sidecars); stop the server, "
                "or use --db against D1")
        params = "mode=ro&immutable=1" if wal else "mode=ro"
        return sqlite3.connect(f"file:{quote(str(path.resolve()))}?{params}", uri=True)
    return backend.connect()  # D1 and others: a raw connection, no schema pass


def _old_tag_projects(tags: List[str], known: List[str]) -> set:
    """The configured projects the tags named under the OLD rule (typed tags counted)."""
    return {t.split("/", 1)[0] for t in tags if isinstance(t, str) and t.split("/", 1)[0] in known}


def _non_typed_tag_projects(tags: List[str], known: List[str]) -> set:
    return {
        t.split("/", 1)[0] for t in tags
        if isinstance(t, str) and "/" in t and t.split("/", 1)[0] in known
        and storage._typed_tag_kind(t) is None
    }


def _typed_retag(tags: List[str], target: str, metadata: Dict[str, Any]) -> Dict[str, str]:
    own = storage._existing_system_tags(tags, metadata)
    return {t: storage.project_tag(target, storage._typed_tag_kind(t)) for t in own
            if t != storage.project_tag(target, storage._typed_tag_kind(t))}


def assess(memory_id: int, content: str, metadata: Dict[str, Any], tags: List[str],
           known: List[str]) -> Optional[Dict[str, Any]]:
    section = metadata.get("section") if isinstance(metadata.get("section"), str) else None
    subsection = metadata.get("subsection") if isinstance(metadata.get("subsection"), str) else None
    meta_project = metadata.get("project") if metadata.get("project") in known else None
    non_typed = _non_typed_tag_projects(tags, known)
    old_tags = _old_tag_projects(tags, known)
    old_explicit = next(iter(old_tags)) if len(old_tags) == 1 else None
    new_project = storage._resolve_project(None, tags, metadata, strict=False)
    row: Dict[str, Any] = {
        "id": memory_id,
        "preview": content[:120],
        "stored": {"section": section, "subsection": subsection, "tags": list(tags),
                   "metadata_project": metadata.get("project"), "type": metadata.get("type"),
                   # lets the apply step detect ANY content change since this preview
                   "content_sha256": __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()},
        "approved": False,
    }

    if section in known and old_explicit and old_explicit != section:
        row["group"] = "contradictions"
        evidence = []
        if meta_project:
            evidence.append({"source": "metadata.project", "project": meta_project})
        evidence += [{"source": "non-typed tag", "project": p} for p in sorted(non_typed)]
        evidence.append({"source": "section", "project": section})
        row["evidence"] = evidence
        targets = {e["project"] for e in evidence}
        if len(targets) != 1:
            row.update(status="needs-human", reason=f"non-typed evidence disagrees: {sorted(targets)}")
            return row
        target = targets.pop()
    elif section in known and new_project is None:
        row["group"] = "keyword_only"
        keyword = legacy._detect_project(content, None, [])
        if keyword == section:
            row["evidence"] = [{"source": "content keywords", "project": keyword}]
            target = section
        elif old_explicit == section:
            row["evidence"] = [{"source": "typed tag only", "project": old_explicit}]
            row.update(status="needs-human",
                       reason="the section is explained only by a typed tag (the old default), not by content")
            return row
        else:
            row["evidence"] = [{"source": "none found", "project": None}]
            row.update(status="needs-human", reason="no non-typed source explains the stored section")
            return row
    else:
        return None

    row["status"] = "proposed"
    row["proposal"] = {
        "set_metadata_project": target,
        "retag_typed": _typed_retag(tags, target, metadata),
        "alternative_marker_tag": (
            f"{target}/{subsection}" if subsection and storage._typed_tag_kind(f"{target}/{subsection}") is None
            else None
        ),
    }
    return row


def build_preview(conn, known: List[str]) -> Dict[str, Any]:
    rows = conn.execute("SELECT id, content, metadata, tags, updated_at FROM memories ORDER BY id").fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {"contradictions": [], "keyword_only": []}
    for memory_id, content, raw_meta, raw_tags, updated_at in rows:
        if storage._import_pending(raw_meta):
            continue
        try:
            metadata = json.loads(raw_meta) if raw_meta else {}
        except (TypeError, ValueError):
            metadata = {}
        tags, _ok = storage._parse_tags_json(raw_tags, memory_id)
        row = assess(int(memory_id), content or "", metadata if isinstance(metadata, dict) else {},
                     tags if isinstance(tags, list) else [], known)
        if row is not None:
            row["stored"]["metadata_sha256"] = metadata_sha256(raw_meta)
            row["stored"]["updated_at"] = updated_at
            out[row.pop("group")].append(row)
    summary = {
        "kind": "issue #47 backfill PREVIEW (read-only; nothing was written). Flip approved to true "
                "for rows to apply; the apply step is a separate item.",
        "scanned": len(rows),
        "configured_projects": known,
    }
    for group, items in out.items():
        summary[group] = {"total": len(items),
                          "proposed": sum(1 for r in items if r["status"] == "proposed"),
                          "needs_human": sum(1 for r in items if r["status"] == "needs-human")}
    return {"summary": summary, **out}


def to_markdown(preview: Dict[str, Any]) -> str:
    s = preview["summary"]
    lines = [f"# Issue 47 backfill preview (read-only)", "", s["kind"], "",
             f"Scanned {s['scanned']}; configured projects: {', '.join(s['configured_projects']) or 'none'}.", ""]
    for group in ("contradictions", "keyword_only"):
        g = s[group]
        lines += [f"## {group}: {g['total']} ({g['proposed']} proposed, {g['needs_human']} needs-human)", "",
                  "| id | status | target / reason | evidence | typed retag | preview |", "|---|---|---|---|---|---|"]
        for r in preview[group]:
            prop = r.get("proposal") or {}
            target = prop.get("set_metadata_project") or r.get("reason", "")
            ev = "; ".join(f"{e['source']}={e['project']}" for e in r.get("evidence", []))
            retag = ", ".join(f"{a}->{b}" for a, b in (prop.get("retag_typed") or {}).items()) or "-"
            prev = r["preview"].replace("|", "/").replace("\n", " ")[:80]
            lines.append(f"| {r['id']} | {r['status']} | {target} | {ev} | {retag} | {prev} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="registry store name (MEMORA_DATABASES)")
    ap.add_argument("--out", required=True, help="preview JSON file to write (the approval list)")
    ap.add_argument("--markdown", help="also write a markdown table here")
    ap.add_argument("--carry-approval", metavar="OLD.json",
                    help="carry approvals from an older approved preview (see carry_approval)")
    args = ap.parse_args(argv)
    _bootstrap(args.db)  # refuse or pin FIRST, then import memora

    token = storage.CURRENT_DB.set(args.db) if args.db else None
    try:
        known = list(storage.configured_projects(args.db) if args.db else storage.configured_projects())
        conn = open_read_only()
        try:
            preview = build_preview(conn, known)
        finally:
            conn.close()
    finally:
        if token is not None:
            storage.CURRENT_DB.reset(token)
    if args.carry_approval:
        old = json.loads(Path(args.carry_approval).read_text())
        for line in carry_approval(preview, old, args.carry_approval):
            print(line)
    Path(args.out).write_text(json.dumps(preview, indent=1, ensure_ascii=False) + "\n")
    if args.markdown:
        Path(args.markdown).write_text(to_markdown(preview) + "\n")
    s = preview["summary"]
    print(f"PREVIEW (read-only) written to {args.out}: scanned {s['scanned']}; "
          f"contradictions {s['contradictions']}; keyword_only {s['keyword_only']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
