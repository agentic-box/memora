"""Copy memories from one store of memora-all to another (Item 95).

    scripts/copy_memories.py --source memora --dest acebar --tag acebar            # dry-run: plan.json
    scripts/copy_memories.py --source memora --dest acebar --plan plan.json --apply

Design (docs/add-store-runbook.md "The copy"):

* The SOURCE is only ever read, through MCP read tools. The DESTINATION is only
  ever written through its running server (memory_create / memory_link /
  memory_update: the tools of the production `leader` profile; memory_import is
  not in it), so the destination's write gate, outbox triggers and replication
  stay in force. The tool never opens a store file: memora-all holds the
  stores' primary locks. No API sets created_at: a copy's created_at is the
  copy time, and the source's is kept in metadata.original_created_at.
* A dry-run captures the complete selected rows (full content, metadata with
  images, tags, created_at, links) into a plan file and a digest. `--apply`
  copies from that saved plan and refuses if a fresh read of the source no
  longer matches its digest.
* Idempotency lives in the destination: every copied row carries
  metadata.memora_copy = {store, id}. The rows are created ONE AT A TIME and the
  destination is re-listed (ALL lineage, not only active rows) before each
  create; exactly one destination row per source id is required afterwards.
  Before each create a write-ahead record is made durable; a create whose
  outcome is unknown is never retried: with no marker the tool refuses to
  create that row again until an operator has proven the old request dead.
* One run at a time per destination (flock on <state-dir>/<dest>.lock held
  for the whole operation) plus a durable state file, so a rerun repairs every
  later phase (provenance map, metadata remap, links) even with nothing left
  to import. Links are created LAST.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

PROV_KEY = "memora_copy"
ORIG_KEY = "original_created_at"   # the source row's created_at (the copy's own is the copy time)
PLAN_VERSION = 1
STATE_VERSION = 1
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# memory_link's EDGE_TYPES and the reciprocal type memory_link(bidirectional)
# stores on the other end (storage._get_reverse_edge_type).
FORWARD_TYPES = ("references", "implements", "supersedes", "extends", "contradicts", "related_to")
RECIPROCAL = {"references": "referenced_by", "implements": "implemented_by", "supersedes": "superseded_by",
              "extends": "extended_by", "contradicts": "contradicts", "related_to": "related_to"}
REVERSE_OF = {"referenced_by": "references", "implemented_by": "implements", "superseded_by": "supersedes",
              "extended_by": "extends"}

# Metadata keys memora itself writes that hold a store-local memory id:
# merge_memories writes metadata.merged_from = <source memory id>. Handled by an
# exact per-key remap AFTER the destination ids exist. No other key is
# rewritten, and no integer is ever remapped by value.
KNOWN_ID_KEYS = ("merged_from",)
# Names that look like references to another memory but that this tool does not
# know how to remap: reported, and apply refuses unless --allow-unknown-refs.
_REF_NAME = re.compile(r"(^|_)(id|ids|parent|predecessor|successor|root|ref|refs|supersedes|superseded_by|"
                       r"related|source_memory|target_memory)$", re.I)
_REF_IGNORE = {PROV_KEY, "import_attempt", "absorb_nonce", "absorb_operation_key", "images", "hierarchy",
               "indexing", "document_key", "document_version"}


class CopyError(RuntimeError):
    """A refusal or failure; the message says what to do."""


class EndpointUncertain(CopyError):
    """A write call whose outcome is unknown (timeout, dropped connection)."""


# ------------------------------------------------------------------ endpoints

class Endpoint:
    """One store of memora-all: MCP tool calls and /health/db."""

    store: str

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError


class McpEndpoint(Endpoint):
    """memora-all over HTTP: <base>/mcp/<store> and <base>/health/db/<store>."""

    def __init__(self, base_url: str, store: str, health_token: str, *, timeout: float = 600.0):
        from .endpoint_check import _Mcp, urllib_fetch

        self.base = base_url.rstrip("/")
        self.store = store
        self.health_token = health_token
        self.timeout = timeout

        def fetch(url, method, headers, body):
            return urllib_fetch(url, method, headers, body, timeout=timeout)

        self._fetch = fetch
        self._mcp = _Mcp(fetch, f"{self.base}/mcp/{store}")
        self._ready = False

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        from .endpoint_check import EndpointCheckFailed

        try:
            if not self._ready:
                self._mcp.initialize()
                self._ready = True
            return self._mcp.call(tool, args)
        except EndpointCheckFailed as exc:
            raise CopyError(f"{tool} on {self.store}: {exc.step}: {exc.detail}")
        except (OSError, urllib.error.URLError, ValueError) as exc:  # timeout, reset, bad body
            raise EndpointUncertain(f"{tool} on {self.store}: outcome unknown ({exc})")

    def health(self) -> Dict[str, Any]:
        status, _h, raw = self._fetch(f"{self.base}/health/db/{self.store}", "GET",
                                      {"Authorization": f"Bearer {self.health_token}"}, None)
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {}
        body["http"] = status
        return body


# ------------------------------------------------------------------ selection and plan

def _digest(rows: Sequence[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(list(rows), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _need(resp: Dict[str, Any], tool: str, store: str) -> Dict[str, Any]:
    if not isinstance(resp, dict) or resp.get("error"):
        raise CopyError(f"{tool} on {store} failed: {resp!r}"[:400])
    return resp


def read_source(src: Endpoint, tag: str, follow: str) -> List[Dict[str, Any]]:
    """The complete selected rows (full content; metadata incl. images; tags;
    created_at; stored links), ordered by id. Read tools only."""
    listing = _need(src.call("memory_list", {"tags_any": [tag], "limit": -1, "content_mode": "full",
                                             "follow": follow, "fields": ["id"]}), "memory_list", src.store)
    ids = sorted({int(m["id"]) for m in listing.get("memories", [])})
    rows: List[Dict[str, Any]] = []
    for mid in ids:
        rec = _need(src.call("memory_get", {"memory_id": mid, "include_images": True, "follow": "all"}),
                    "memory_get", src.store).get("memory") or {}
        if int(rec.get("id", -1)) != mid:
            raise CopyError(f"memory_get({mid}) on {src.store} returned another row; refusing")
        rows.append({
            "id": mid, "content": rec.get("content"), "metadata": rec.get("metadata"),
            "tags": list(rec.get("tags") or []), "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
            "related": sorted(({"id": int(r["id"]), "edge_type": r.get("edge_type"), "score": r.get("score")}
                               for r in (rec.get("related") or [])),
                              key=lambda r: (r["id"], str(r["edge_type"]))),
        })
    return rows


def source_total(src: Endpoint) -> int:
    listing = _need(src.call("memory_list", {"limit": -1, "content_mode": "preview", "follow": "all",
                                             "fields": ["id"]}), "memory_list", src.store)
    return len(listing.get("memories", []))


def read_markers(dest: Endpoint, src_store: str) -> Dict[int, List[int]]:
    """{source id: [destination ids]} over ALL destination rows, every
    lineage state (follow=all)."""
    listing = _need(dest.call("memory_list", {"limit": -1, "content_mode": "full", "follow": "all"}),
                    "memory_list", dest.store)
    out: Dict[int, List[int]] = {}
    for m in listing.get("memories", []):
        prov = (m.get("metadata") or {}).get(PROV_KEY)
        if isinstance(prov, dict) and prov.get("store") == src_store and isinstance(prov.get("id"), int):
            out.setdefault(prov["id"], []).append(int(m["id"]))
    return out


def _unknown_refs(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reference-shaped metadata keys this tool cannot remap. Top-level keys
    only; a key is reference-shaped when its NAME says so and its value is an
    int or a list of ints. Nothing is rewritten by value."""
    found = []
    for r in rows:
        for key, val in (r.get("metadata") or {}).items():
            if key in KNOWN_ID_KEYS or key in _REF_IGNORE or not _REF_NAME.search(key):
                continue
            ints = isinstance(val, int) and not isinstance(val, bool)
            ints = ints or (isinstance(val, list) and val and all(isinstance(x, int) and not isinstance(x, bool)
                                                                   for x in val))
            if ints:
                found.append({"id": r["id"], "key": key, "value": val})
    return found


def analyse_links(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The directed edges among the selected rows, as memory_link calls.

    forward_paired   (a, b, type): both a->b (type) and b->a (reciprocal) are
                     stored: one memory_link(bidirectional=True).
    forward_only     (a, b, type): a->b stored without its reciprocal:
                     memory_link(bidirectional=False).
    unrepresentable  edges memory_link cannot create: a reciprocal-typed edge
                     whose forward edge is not stored, or an unknown type.
    external         edges with an end outside the selection (dropped).
    """
    selected = {r["id"] for r in rows}
    stored: Dict[Tuple[int, int], Set[str]] = {}
    external = 0
    score_changed = 0
    for r in rows:
        for e in r.get("related") or []:
            if e["id"] not in selected:
                external += 1
                continue
            stored.setdefault((r["id"], e["id"]), set()).add(e["edge_type"])
            if e.get("score") not in (None, 1, 1.0):
                score_changed += 1
    paired: List[Tuple[int, int, str]] = []
    only: List[Tuple[int, int, str]] = []
    bad: List[Dict[str, Any]] = []
    for (a, b), types in sorted(stored.items()):
        for t in sorted(types):
            if t in FORWARD_TYPES:
                symmetric = RECIPROCAL[t] == t
                if symmetric and a > b and t in stored.get((b, a), set()):
                    continue  # the pair is created once, from the lower id
                if RECIPROCAL[t] in stored.get((b, a), set()):
                    paired.append((a, b, t))
                else:
                    only.append((a, b, t))
            elif t in REVERSE_OF:
                if REVERSE_OF[t] not in stored.get((b, a), set()):
                    bad.append({"from": a, "to": b, "edge_type": t, "why": "reciprocal edge without its forward edge"})
            else:
                bad.append({"from": a, "to": b, "edge_type": t, "why": "edge type memory_link cannot create"})
    return {"forward_paired": paired, "forward_only": only, "unrepresentable": bad,
            "external_dropped": external, "score_normalised": score_changed}


def build_plan(src: Endpoint, dest: Endpoint, *, tag: str, follow: str, project: str,
               now: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Dry-run: read-only on both sides. Returns the plan (rows + report)."""
    _check_names(src, dest)
    rows = read_source(src, tag, follow)
    markers = read_markers(dest, src.store)
    selected = {r["id"] for r in rows}
    collisions = [r["id"] for r in rows if {PROV_KEY, ORIG_KEY} & set(r.get("metadata") or {})]
    links = analyse_links(rows)
    merged = {r["id"]: r["metadata"]["merged_from"] for r in rows
              if isinstance((r.get("metadata") or {}).get("merged_from"), int)}
    report = {
        "selected": len(rows), "ids": sorted(selected),
        "already_copied": sorted(i for i in selected if i in markers),
        "would_copy": sorted(i for i in selected if i not in markers),
        "created_at_becomes_copy_time": True,
        "note": "no API sets created_at: each copy's created_at is the copy time; the source's is kept in "
                f"metadata.{ORIG_KEY}",
        "duplicate_markers": {str(k): v for k, v in markers.items() if len(v) > 1},
        "provenance_collisions": collisions,
        "unknown_refs": _unknown_refs(rows),
        "merged_from": {"remap": sorted(i for i, t in merged.items() if t in selected),
                        "dropped": sorted(i for i, t in merged.items() if t not in selected)},
        "links": {"forward_paired": len(links["forward_paired"]), "forward_only": len(links["forward_only"]),
                  "unrepresentable": links["unrepresentable"], "external_dropped": links["external_dropped"],
                  "score_normalised": links["score_normalised"]},
    }
    return {"version": PLAN_VERSION, "source": src.store, "dest": dest.store, "tag": tag, "follow": follow,
            "project": project, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
            "source_total": source_total(src), "digest": _digest(rows), "rows": rows, "report": report}


def _check_names(src: Endpoint, dest: Endpoint) -> None:
    for ep in (src, dest):
        if not NAME_RE.fullmatch(ep.store or ""):
            raise CopyError(f"store name {ep.store!r} must match {NAME_RE.pattern}")
    if src.store == dest.store:
        raise CopyError("source and destination are the same store; refusing")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: Dict[str, Any]) -> None:
    """Atomic 0600 replacement, durable: the file AND its directory are fsynced
    (and the parent too when the directory is created here), so a record made
    before a request is on disk when the request leaves."""
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    if created:
        _fsync_dir(path.parent.parent)


def load_plan(path: Path) -> Dict[str, Any]:
    try:
        plan = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CopyError(f"cannot read the plan {path}: {exc}")
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION or "rows" not in plan:
        raise CopyError(f"{path} is not a copy plan of this version")
    if _digest(plan["rows"]) != plan.get("digest"):
        raise CopyError(f"{path} was modified after the dry-run (its rows do not match its digest)")
    return plan


# ------------------------------------------------------------------ apply

@dataclass
class ApplyOptions:
    state_dir: Path
    allow_unrepresentable: bool = False
    allow_unknown_refs: bool = False
    unknown_wait_s: float = 60.0      # how long an unknown create outcome is polled for its marker
    poll_s: float = 5.0
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = lambda msg: None


class _Lock:
    """flock on <state-dir>/<dest>.lock for the whole operation."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise CopyError(f"another copy into this destination is running (lock {self.path}); nothing was changed")
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _state_path(opts: ApplyOptions, src: str, dest: str) -> Path:
    return opts.state_dir / f"{src}-{dest}.json"


def _load_state(path: Path, plan: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "digest": plan["digest"], "phase": "new", "map": {}}
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # never treat an unreadable record as "no attempts": that would forget an ambiguous create
        raise CopyError(f"the state file {path} is unreadable or corrupt ({exc}); it may record a create attempt "
                        "of unknown outcome. Inspect it by hand; nothing was changed")
    if not isinstance(state, dict) or not isinstance(state.get("attempts", {}), dict):
        raise CopyError(f"the state file {path} has an unexpected shape; inspect it by hand; nothing was changed")
    if state.get("digest") != plan["digest"]:
        raise CopyError(f"{path} belongs to another plan (digest differs): inspect it; a different selection "
                        "must not resume this run; nothing was changed")
    return state


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    write_json(path, dict(state, version=STATE_VERSION))


def _entry_metadata(row: Dict[str, Any], src_store: str) -> Dict[str, Any]:
    meta = dict(row.get("metadata") or {})
    for key in KNOWN_ID_KEYS:
        meta.pop(key, None)  # re-set in the remap phase, never copied as a stale source id
    meta[PROV_KEY] = {"store": src_store, "id": row["id"]}
    meta[ORIG_KEY] = row["created_at"]
    return meta


def _await_marker(dest: Endpoint, src_store: str, src_id: int, opts: ApplyOptions) -> Optional[int]:
    """After a create whose outcome is unknown: poll for the row's marker.
    Returns the destination id when EXACTLY one marker appeared, None when none
    did within the wait. This only reconciles a quick completion; it never
    authorises another create."""
    polls = max(1, int(opts.unknown_wait_s / opts.poll_s))
    for i in range(polls + 1):
        ids = read_markers(dest, src_store).get(src_id, [])
        if len(ids) > 1:
            raise CopyError(f"duplicate provenance markers for source id {src_id} in {dest.store}: {ids}")
        if ids:
            return ids[0]
        if i < polls:
            opts.sleep(opts.poll_s)
    return None


AMBIGUOUS_HELP = ("A create whose outcome is unknown can still complete on the server: ordinary reruns will NOT "
                  "create it again, however long ago it was. To re-create it deliberately, first prove the old "
                  "request is dead (restart the destination's server), confirm the marker is absent in the "
                  "destination, then delete that source id from \"attempts\" in the state file "
                  "(docs/add-store-runbook.md, \"The copy\").")


def _create_missing(dest: Endpoint, src_store: str, rows: List[Dict[str, Any]], project: str,
                    opts: ApplyOptions, state: Dict[str, Any], save: Callable[[], None]) -> Dict[str, int]:
    """Create the rows the destination does not hold yet, one at a time, in
    ascending source id, fail-closed.

    A write-ahead record (state["attempts"][id] = {"state": "sent"}) is made
    durable BEFORE every create and is resolved to "done" only when the
    response carries an integer id AND a fresh listing shows exactly that one
    marker. Anything else (timeout, crash before or after the send, no id, an
    error, an acknowledged create whose marker is not visible, a different id)
    leaves it "sent". On a resume a "sent" row with exactly one marker is
    reconciled; with none, the run REFUSES before creating anything (zero
    creates): elapsed time and stable reads prove nothing about a request that
    may still be queued on the server."""
    attempts: Dict[str, Dict[str, Any]] = state.setdefault("attempts", {})
    stats = {"created": 0, "reconciled": 0}
    markers = read_markers(dest, src_store)
    dup = {k: v for k, v in markers.items() if len(v) > 1}
    if dup:
        raise CopyError(f"duplicate provenance markers in {dest.store}: {dup}; refusing to continue")
    ambiguous_absent = sorted(int(k) for k, a in attempts.items() if a.get("state") == "sent" and int(k) not in markers)
    if ambiguous_absent:
        raise CopyError(f"source id(s) {ambiguous_absent} have a create attempt of unknown outcome and no marker in "
                        f"{dest.store}; refusing to create anything (zero creates). {AMBIGUOUS_HELP}")
    for r in rows:
        key = str(r["id"])
        markers = read_markers(dest, src_store)
        ids = markers.get(r["id"], [])
        if len(ids) > 1:
            raise CopyError(f"duplicate provenance markers for source id {r['id']} in {dest.store}: {ids}")
        if ids:
            if attempts.get(key, {}).get("state") == "sent":
                attempts[key] = {"state": "done", "dest_id": ids[0], "reconciled": True}
                save()
                stats["reconciled"] += 1
            continue
        attempts[key] = {"state": "sent"}
        save()                                               # durable BEFORE the request leaves
        args = {"content": r["content"], "tags": list(r["tags"]), "metadata": _entry_metadata(r, src_store),
                "project": project, "suggest_similar": False, "response_mode": "minimal"}
        try:
            resp = dest.call("memory_create", args)
        except EndpointUncertain as exc:
            opts.log(f"create of source id {r['id']}: outcome unknown ({exc}); waiting for its marker")
            found = _await_marker(dest, src_store, r["id"], opts)
            if found is None:
                raise CopyError(f"the outcome of creating source id {r['id']} in {dest.store} is unknown and its "
                                f"marker did not appear within {opts.unknown_wait_s:.0f}s; nothing more was created. "
                                f"{AMBIGUOUS_HELP}")
            attempts[key] = {"state": "done", "dest_id": found, "reconciled": True}
            save()
            stats["reconciled"] += 1
            continue
        new_id = (resp.get("memory") or {}).get("id") if isinstance(resp, dict) else None
        if isinstance(resp, dict) and resp.get("error"):
            raise CopyError(f"memory_create for source id {r['id']} returned an error: {resp!r}"[:400]
                            + f". The attempt stays recorded as ambiguous. {AMBIGUOUS_HELP}")
        if not isinstance(new_id, int) or isinstance(new_id, bool):
            raise CopyError(f"memory_create for source id {r['id']} returned no id: {resp!r}"[:400]
                            + f". The attempt stays recorded as ambiguous. {AMBIGUOUS_HELP}")
        after = read_markers(dest, src_store).get(r["id"], [])
        if after != [new_id]:
            raise CopyError(f"memory_create returned id {new_id} for source id {r['id']} but the destination lists "
                            f"markers {after}; refusing to create again. {AMBIGUOUS_HELP}")
        attempts[key] = {"state": "done", "dest_id": new_id}
        save()
        stats["created"] += 1
    return stats


def _dest_records(dest: Endpoint, ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    out = {}
    for did in ids:
        rec = _need(dest.call("memory_get", {"memory_id": did, "include_images": True, "follow": "all"}),
                    "memory_get", dest.store).get("memory") or {}
        out[did] = rec
    return out


def _verify_fidelity(rows: Sequence[Dict[str, Any]], mapping: Dict[int, int], recs: Dict[int, Dict[str, Any]],
                     project: str) -> List[str]:
    problems = []
    for r in rows:
        rec = recs[mapping[r["id"]]]
        if rec.get("content") != r["content"]:
            problems.append(f"{r['id']}: content differs")
        missing_tags = set(r["tags"]) - set(rec.get("tags") or [])
        if missing_tags:
            problems.append(f"{r['id']}: tags lost {sorted(missing_tags)}")
        dmeta = rec.get("metadata") or {}
        for k, v in (r.get("metadata") or {}).items():
            if k in KNOWN_ID_KEYS:
                continue
            if dmeta.get(k) != v:
                problems.append(f"{r['id']}: metadata[{k!r}] differs")
        if dmeta.get(PROV_KEY, {}).get("id") != r["id"]:
            problems.append(f"{r['id']}: provenance marker missing")
        if dmeta.get(ORIG_KEY) != r["created_at"]:
            problems.append(f"{r['id']}: {ORIG_KEY} is {dmeta.get(ORIG_KEY)!r}, not {r['created_at']!r}")
        if dmeta.get("project") != project:
            problems.append(f"{r['id']}: project is {dmeta.get('project')!r}, not {project!r}")
    return problems


def _edges(rec: Dict[str, Any]) -> Set[Tuple[int, str]]:
    return {(int(e["id"]), e["edge_type"]) for e in (rec.get("related") or [])}


def apply_plan(plan: Dict[str, Any], src: Endpoint, dest: Endpoint, opts: ApplyOptions) -> Dict[str, Any]:
    _check_names(src, dest)
    if plan["source"] != src.store or plan["dest"] != dest.store:
        raise CopyError(f"the plan is for {plan['source']} -> {plan['dest']}, not {src.store} -> {dest.store}")
    rows: List[Dict[str, Any]] = plan["rows"]
    project: str = plan["project"]
    report = plan["report"]
    if report["provenance_collisions"]:
        raise CopyError(f"selected rows already carry metadata.{PROV_KEY} or metadata.{ORIG_KEY}: "
                        f"{report['provenance_collisions']}; "
                        "refusing (no silent overwrite)")
    if report["unknown_refs"] and not opts.allow_unknown_refs:
        raise CopyError(f"reference-shaped metadata this tool cannot remap: {report['unknown_refs']}; pass "
                        "--allow-unknown-refs to copy them unchanged (they will point at source ids)")
    links = analyse_links(rows)
    if links["unrepresentable"] and not opts.allow_unrepresentable:
        raise CopyError(f"edges memory_link cannot create: {links['unrepresentable']}; pass "
                        "--allow-unrepresentable to copy without them (they are listed in the manifest)")
    health = dest.health()
    if health.get("http") != 200 or health.get("status") != "ok":
        raise CopyError(f"/health/db/{dest.store} is not ok ({health.get('http')}, {health.get('status')!r})")
    freeze = (health.get("freeze") or {}).get("state")
    if freeze not in (None, "open"):
        raise CopyError(f"{dest.store} is {freeze}: thaw it (after the clean barrier compare) before copying into it")

    with _Lock(opts.state_dir / f"{dest.store}.lock"):
        state_path = _state_path(opts, src.store, dest.store)
        state = _load_state(state_path, plan)

        # The source must still be what the dry-run saw.
        fresh = read_source(src, plan["tag"], plan["follow"])
        if _digest(fresh) != plan["digest"]:
            raise CopyError("the source selection, content or links changed since the dry-run (concurrent change): "
                            "run the dry-run again; nothing was changed")
        before_total = source_total(src)

        state["phase"] = "creating"
        _save_state(state_path, state)
        pre_existing = len(read_markers(dest, src.store))
        _create_missing(dest, src.store, rows, project, opts, state, lambda: _save_state(state_path, state))

        markers = read_markers(dest, src.store)
        mapping: Dict[int, int] = {}
        for r in rows:
            ids = markers.get(r["id"], [])
            if len(ids) != 1:
                raise CopyError(f"source id {r['id']} has {len(ids)} destination rows (expected exactly 1): {ids}")
            mapping[r["id"]] = ids[0]
        state.update(phase="mapped", map={str(k): v for k, v in mapping.items()})
        _save_state(state_path, state)

        recs = _dest_records(dest, sorted(mapping.values()))
        problems = _verify_fidelity(rows, mapping, recs, project)
        if problems:
            raise CopyError("the copied rows differ from the source: " + "; ".join(problems[:10]))

        # --- metadata references (exact keys, after the ids exist)
        dropped_refs = []
        for r in rows:
            target = (r.get("metadata") or {}).get("merged_from")
            if not isinstance(target, int):
                continue
            new_id = mapping[r["id"]]
            if target in mapping:
                if (recs[new_id].get("metadata") or {}).get("merged_from") != mapping[target]:
                    _need(dest.call("memory_update", {"memory_id": new_id,
                                                      "metadata": {"merged_from": mapping[target]}}),
                          "memory_update", dest.store)
            else:
                dropped_refs.append({"id": r["id"], "key": "merged_from", "value": target})
        state["phase"] = "remapped"
        _save_state(state_path, state)

        # --- links LAST, so nothing above can erase them
        recs = _dest_records(dest, sorted(mapping.values()))
        created = existing = 0
        for kind, edges in (("paired", links["forward_paired"]), ("only", links["forward_only"])):
            for a, b, t in edges:
                na, nb = mapping[a], mapping[b]
                have = _edges(recs[na])
                want_rev = (na, RECIPROCAL[t]) in _edges(recs[nb])
                if (nb, t) in have and (kind == "only" or want_rev):
                    existing += 1
                    continue
                _need(dest.call("memory_link", {"from_id": na, "to_id": nb, "edge_type": t,
                                                "bidirectional": kind == "paired"}), "memory_link", dest.store)
                created += 1
        state["phase"] = "linked"
        _save_state(state_path, state)

        # --- verify the graph shape: every source edge exists, with its type and direction
        recs = _dest_records(dest, sorted(mapping.values()))
        missing_edges = []
        for kind, edges in (("paired", links["forward_paired"]), ("only", links["forward_only"])):
            for a, b, t in edges:
                na, nb = mapping[a], mapping[b]
                if (nb, t) not in _edges(recs[na]):
                    missing_edges.append({"from": a, "to": b, "edge_type": t})
                if kind == "paired" and (na, RECIPROCAL[t]) not in _edges(recs[nb]):
                    missing_edges.append({"from": b, "to": a, "edge_type": RECIPROCAL[t]})
        if missing_edges:
            raise CopyError(f"edges missing at the destination after the link phase: {missing_edges[:10]}")
        copied_dest = set(mapping.values())
        wanted = {(mapping[a], mapping[b], t) for a, b, t in links["forward_paired"] + links["forward_only"]}
        wanted |= {(mapping[b], mapping[a], RECIPROCAL[t]) for a, b, t in links["forward_paired"]}
        extra = sorted((did, peer, t) for did, rec in recs.items() for peer, t in _edges(rec)
                       if peer in copied_dest and (did, peer, t) not in wanted)

        after = read_source(src, plan["tag"], plan["follow"])
        after_total = source_total(src)
        source_changed = _digest(after) != plan["digest"] or after_total != before_total
        manifest = {
            "source": src.store, "dest": dest.store, "digest": plan["digest"], "map": state["map"],
            "links": {"created": created, "already_present": existing,
                      "unrepresentable": links["unrepresentable"], "external_dropped": links["external_dropped"],
                      "score_normalised": links["score_normalised"],
                      "recomputed_extra_edges": [list(e) for e in extra]},
            "dropped_metadata_refs": dropped_refs, "source_changed_during_run": source_changed,
        }
        write_json(opts.state_dir / f"{src.store}-{dest.store}.manifest.json", manifest)
        state["phase"] = "done"
        _save_state(state_path, state)
        return {"ok": not source_changed, "copied": len(mapping), "already_present": pre_existing,
                "manifest": str(opts.state_dir / f"{src.store}-{dest.store}.manifest.json"), **manifest["links"],
                "dropped_metadata_refs": len(dropped_refs), "source_changed_during_run": source_changed}
