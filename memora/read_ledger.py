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
import contextvars
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

ENV_PATH = "MEMORA_READ_LEDGER"
ENV_MAX_BYTES = "MEMORA_READ_LEDGER_MAX_BYTES"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MIN_MAX_BYTES = 64 * 1024   # half of it must hold the longest possible line
QUEUE_MAX = 1000
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
_start_lock = threading.Lock()   # guards only the writer thread's creation
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
    return max(n, MIN_MAX_BYTES) if n > 0 else DEFAULT_MAX_BYTES


def _warn_once(what: str, exc: Optional[BaseException] = None) -> None:
    global _warned
    if not _warned:
        _warned = True
        log.warning("read ledger: %s%s (reads continue; warned once)", what,
                    f": {type(exc).__name__}: {exc}" if exc else "")


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


def _append(path: str, data: bytes, cap: int) -> None:
    """Append ``data``; the live file + ``<path>.1`` stay <= ``cap`` BYTES."""
    half = cap // 2
    old = path + ".1"
    if _size(old) > half:           # left over from a larger cap
        os.unlink(old)
    if _size(path) + len(data) > half:
        os.replace(path, old) if os.path.exists(path) else None
        if _size(old) > half:       # the live file was over the (lowered) cap
            os.unlink(old)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


_q: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=QUEUE_MAX)
_worker: Optional[threading.Thread] = None
_dropped = 0


def _run_worker() -> None:
    while True:
        item = _q.get()
        try:
            if item is None:
                continue
            path, data, cap = item
            _append(path, data, cap)
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail a read
            _warn_once("append failed", exc)
        finally:
            _q.task_done()


def _enqueue(path: str, data: bytes, cap: int) -> None:
    global _worker, _dropped
    if _worker is None or not _worker.is_alive():
        with _start_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run_worker, name="memora-read-ledger", daemon=True)
                _worker.start()
    try:
        _q.put_nowait((path, data, cap))
    except queue.Full:
        _dropped += 1
        _warn_once(f"writer queue full, dropping lines ({_dropped} so far)")


def flush(timeout: float = 10.0) -> bool:
    """Wait until every queued line is written (tests, shutdown)."""
    done = threading.Event()
    threading.Thread(target=lambda: (_q.join(), done.set()), daemon=True).start()
    return done.wait(timeout)


# What the tool saw BEFORE field projection / content projection dropped ids:
# the wrapper installs a holder, the tool calls note().
_CAPTURE: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "memora_read_ledger_capture", default=None)


def note(entries: Any) -> None:
    """Called by a read tool with its unprojected entries (list of memory
    dicts, {score, memory} dicts, or a memory dict); never raises."""
    try:
        holder = _CAPTURE.get()
        if holder is not None:
            holder["entries"] = entries if isinstance(entries, list) else [entries]
    except Exception:  # noqa: BLE001
        pass


def _entries(result: Any) -> List[Any]:
    """The memory-bearing entries of a (projected) response: the fallback when
    the tool did not note() its unprojected entries."""
    if not isinstance(result, dict):
        return []
    for key in ("results", "memories", "related"):
        v = result.get(key)
        if isinstance(v, list):
            return v
    m = result.get("memory")
    return [m] if isinstance(m, dict) else []


def _refs(entries: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in entries:
        m = e.get("memory", e) if isinstance(e, dict) else e
        if isinstance(m, dict) and isinstance(m.get("id"), int):
            ref: Dict[str, Any] = {"id": m["id"]}
            if isinstance(m.get("updated_at"), str):
                ref["v"] = m["updated_at"]
            out.append(ref)
        elif isinstance(m, int) and not isinstance(m, bool):
            out.append({"id": m})
    return out


def build_line(tool: str, args: Dict[str, Any], result: Any, latency_ms: float,
               store: Optional[str], caller: Optional[str], error: Optional[str] = None,
               entries: Optional[List[Any]] = None) -> str:
    refs = _refs(entries if entries is not None else _entries(result))
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
    row["count"] = count if isinstance(count, int) else len(refs)
    if len(refs) > MAX_IDS:
        row["truncated"] = len(refs)   # the true number of returned memories
        refs = refs[:MAX_IDS]
    row["memories"] = refs
    row["latency_ms"] = round(latency_ms, 2)
    err = error or (result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) else None)
    if err:
        row["error"] = err[:60]
    return json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"


def record(tool: str, args: Dict[str, Any], result: Any, latency_ms: float,
           store_fn: Callable[[], Optional[str]] = lambda: None,
           caller_fn: Callable[[], Optional[str]] = lambda: None,
           error: Optional[str] = None, entries: Optional[List[Any]] = None) -> None:
    """Queue one line for the writer thread; never raises, never blocks."""
    try:
        path = ledger_path()
        if not path:
            return
        line = build_line(tool, args, result, latency_ms, store_fn(), caller_fn(), error, entries)
        _enqueue(path, line.encode("utf-8"), _max_bytes())
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a read
        _warn_once("could not build a line", exc)


def ledgered(tool: str, store_fn: Callable[[], Optional[str]] = lambda: None,
             caller_fn: Callable[[], Optional[str]] = lambda: None):
    """Decorator for an async read tool; place it UNDER ``@mcp.tool()``."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            if not ledger_path():
                return await fn(*a, **kw)
            holder: Dict[str, Any] = {}
            token = _CAPTURE.set(holder)
            t0 = time.perf_counter()
            try:
                result = await fn(*a, **kw)
            except BaseException as exc:
                ms = (time.perf_counter() - t0) * 1000
                record(tool, _bound(sig, a, kw), None, ms, store_fn, caller_fn, type(exc).__name__)
                raise
            finally:
                _CAPTURE.reset(token)
            ms = (time.perf_counter() - t0) * 1000
            record(tool, _bound(sig, a, kw), result, ms, store_fn, caller_fn, entries=holder.get("entries"))
            return result
        return wrapper
    return deco


def _bound(sig: inspect.Signature, a: tuple, kw: dict) -> Dict[str, Any]:
    try:
        return dict(sig.bind_partial(*a, **kw).arguments)
    except TypeError:
        return dict(kw)
