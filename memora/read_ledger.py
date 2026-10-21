"""Read telemetry for the memora effectiveness ledger (Item 118).

OFF unless ``MEMORA_READ_LEDGER=<path>`` names a file. When on, every call of
a read tool (memory_hybrid_search, memory_semantic_search, memory_list,
memory_get, memory_related) appends ONE JSON line:

    ts, caller, store, project, tool, query_hash, query (<= 200 chars),
    memories [{id, v}], count, latency_ms[, error]

``v`` is the memory's ``updated_at`` when the response carries it. Memory
content, metadata, tags, tokens and request headers other than the caller
label are never written: a line is built from the fields above only.

The append is best effort. It runs after the read finished (latency is the
read's own), is a single small write, and any failure is swallowed: one
warning, then silence. The file is size-capped (``MEMORA_READ_LEDGER_MAX_BYTES``,
default 20 MiB) with ONE rotation to ``<path>.1``.

The report side lives in ``scripts/memora-ledger``; docs/read-ledger.md.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

ENV_PATH = "MEMORA_READ_LEDGER"
ENV_MAX_BYTES = "MEMORA_READ_LEDGER_MAX_BYTES"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
QUERY_CHARS = 200
MAX_IDS = 200
READ_TOOLS = (
    "memory_hybrid_search",
    "memory_semantic_search",
    "memory_list",
    "memory_get",
    "memory_related",
)

log = logging.getLogger("memora.read_ledger")
_lock = threading.Lock()
_warned = False


def ledger_path(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    env = os.environ if env is None else env
    return (env.get(ENV_PATH) or "").strip() or None


def _max_bytes(env: Optional[Dict[str, str]] = None) -> int:
    env = os.environ if env is None else env
    try:
        n = int(env.get(ENV_MAX_BYTES, ""))
    except ValueError:
        return DEFAULT_MAX_BYTES
    return n if n > 0 else DEFAULT_MAX_BYTES


def _warn_once(exc: BaseException) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning("read ledger disabled for this error, reads continue: %s: %s",
                    type(exc).__name__, exc)


def _append(path: str, line: str, cap: int) -> None:
    with _lock:
        try:
            if os.path.getsize(path) + len(line) > cap // 2:
                os.replace(path, path + ".1")
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def _entries(result: Any) -> List[Any]:
    """The memory-bearing entries of a read tool's response."""
    if not isinstance(result, dict):
        return []
    for key in ("results", "memories", "related"):
        v = result.get(key)
        if isinstance(v, list):
            return v
    m = result.get("memory")
    return [m] if isinstance(m, dict) else []


def _refs(result: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in _entries(result):
        m = e.get("memory", e) if isinstance(e, dict) else e
        if isinstance(m, dict) and isinstance(m.get("id"), int):
            ref: Dict[str, Any] = {"id": m["id"]}
            if isinstance(m.get("updated_at"), str):
                ref["v"] = m["updated_at"]
            out.append(ref)
        elif isinstance(m, int) and not isinstance(m, bool):
            out.append({"id": m})
    return out[:MAX_IDS]


def build_line(tool: str, args: Dict[str, Any], result: Any, latency_ms: float,
               store: Optional[str], caller: Optional[str], error: Optional[str] = None) -> str:
    refs = _refs(result)
    count = result.get("count") if isinstance(result, dict) else None
    row: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "tool": tool,
        "store": store,
    }
    if caller:
        row["caller"] = caller[:80]
    q = args.get("query")
    if isinstance(q, str) and q:
        row["query_hash"] = hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]
        row["query"] = q[:QUERY_CHARS]
    mf = args.get("metadata_filters")
    if isinstance(mf, dict) and isinstance(mf.get("project"), str):
        row["project"] = mf["project"][:80]
    if isinstance(args.get("memory_id"), int):
        row["requested_id"] = args["memory_id"]
    row["memories"] = refs
    row["count"] = count if isinstance(count, int) else len(refs)
    row["latency_ms"] = round(latency_ms, 2)
    err = error or (result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) else None)
    if err:
        row["error"] = err[:60]
    return json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"


def record(tool: str, args: Dict[str, Any], result: Any, latency_ms: float,
           store_fn: Callable[[], Optional[str]] = lambda: None,
           caller_fn: Callable[[], Optional[str]] = lambda: None,
           error: Optional[str] = None) -> None:
    """Append one line; never raises."""
    try:
        path = ledger_path()
        if not path:
            return
        line = build_line(tool, args, result, latency_ms, store_fn(), caller_fn(), error)
        _append(path, line, _max_bytes())
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a read
        _warn_once(exc)


def ledgered(tool: str, store_fn: Callable[[], Optional[str]] = lambda: None,
             caller_fn: Callable[[], Optional[str]] = lambda: None):
    """Decorator for an async read tool; place it UNDER ``@mcp.tool()``."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            if not ledger_path():
                return await fn(*a, **kw)
            t0 = time.perf_counter()
            try:
                result = await fn(*a, **kw)
            except BaseException as exc:
                ms = (time.perf_counter() - t0) * 1000
                record(tool, _bound(sig, a, kw), None, ms, store_fn, caller_fn, type(exc).__name__)
                raise
            ms = (time.perf_counter() - t0) * 1000
            record(tool, _bound(sig, a, kw), result, ms, store_fn, caller_fn)
            return result
        return wrapper
    return deco


def _bound(sig: inspect.Signature, a: tuple, kw: dict) -> Dict[str, Any]:
    try:
        return dict(sig.bind_partial(*a, **kw).arguments)
    except TypeError:
        return dict(kw)
