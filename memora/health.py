"""Health of the RUNNING server (memora #965 phase 4).

`memora.cli health` spawns a FRESH process: it proves a config can connect, not
that the live server can. Under the single-server shape that distinction stops
being pedantic -- one process serves every workspace.

  GET /health      LIVENESS. Is this process serving? Touches NO database and
                   takes no locks, so a slow store never makes the process look
                   dead. This is the ONLY signal a supervisor may restart on.

  GET /health/db   READINESS DIAGNOSTIC, per database. Always 200 with
                   status=ok|degraded. It is an ALERT surface, not a service
                   readiness probe: returning 503 because one store is degraded
                   would make a load balancer withdraw the whole process and
                   take healthy databases down with the broken one -- exactly
                   the shared blast radius this design exists to avoid.

  GET /health/db/{name}   PER-DATABASE readiness, 200 or 503. THIS is the one a
                   workspace-specific probe should use, because withdrawing on
                   it affects only that database.

PROBING IS OFF-LOOP, CACHED AND SINGLE-FLIGHT. A probe is real network I/O: D1
allows 30s per statement and S3 sync has retries and a file lock. Called
synchronously from an async route it would stall ALL MCP traffic, and an
unauthenticated caller could repeat that fanout at will. So probes run in a
worker thread, at most one refresh is in flight, and callers get the most recent
snapshot marked stale rather than spawning more work. A timeout cannot cancel
blocked I/O -- single-flight is what actually bounds the damage.

DETAIL IS PRIVILEGED. Names, counts and error text are inventory and
configuration. FastMCP's custom_route() is explicitly unauthenticated even when
MCP auth is configured, so the detailed body is served only to an authorised
caller (MEMORA_HEALTH_TOKEN) or over loopback; everyone else gets aggregate
status only.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import threading
import time
from typing import Any, Dict, Optional

from . import __version__ as memora_version
from .storage import (
    CURRENT_DB,
    DatabaseRegistryError,
    database_registry,
    default_database_name,
)

# How long a readiness snapshot may be served before a refresh is triggered.
SNAPSHOT_TTL_S = float(os.getenv("MEMORA_HEALTH_TTL", "10"))
# Overall bound on one refresh pass. Cannot cancel blocked I/O; it bounds how
# long a CALLER waits, while single-flight bounds how much work exists.
REFRESH_DEADLINE_S = float(os.getenv("MEMORA_HEALTH_TIMEOUT", "5"))

_PROCESS_START = time.monotonic()

_snapshot: Optional[Dict[str, Any]] = None
_snapshot_at: float = 0.0
_refresh_lock = threading.Lock()
_refreshing = False


def liveness_payload() -> Dict[str, Any]:
    """Is the process serving? Never touches a database, never blocks."""
    return {
        "status": "ok",
        "version": memora_version,
        "uptime_seconds": round(time.monotonic() - _PROCESS_START, 1),
    }


def _probe_one(name: Optional[str]) -> Dict[str, Any]:
    """Connect to one database and report it. Never raises.

    SELECT 1, not COUNT(*): readiness asks whether the store answers, and a
    row count is both costlier and unnecessary inventory to expose.
    """
    from .storage import connect

    started = time.time()
    token = CURRENT_DB.set(name) if name is not None else None
    try:
        conn = connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return {"status": "ok", "latency_ms": round((time.time() - started) * 1000, 1)}
    except Exception as exc:
        # Reported, never raised: one bad database must not make the endpoint
        # fail and hide the others. The message is kept for the LOG, and
        # stripped before it reaches an unauthorised caller.
        return {
            "status": "error",
            "error": type(exc).__name__,
            "message": str(exc)[:200],
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
    finally:
        if token is not None:
            CURRENT_DB.reset(token)


def _build_snapshot() -> Dict[str, Any]:
    try:
        registry = database_registry()
        default = default_database_name()
    except DatabaseRegistryError as exc:
        return {"status": "error", "message": str(exc), "databases": {}, "default_database": None}

    names = sorted(registry) if registry else [None]
    result = {(n or "(default)"): _probe_one(n) for n in names}
    degraded = [n for n, r in result.items() if r["status"] != "ok"]
    return {
        "status": "degraded" if degraded else "ok",
        "version": memora_version,
        "default_database": default,
        "degraded": degraded,
        "databases": result,
    }


def _refresh_snapshot() -> None:
    global _snapshot, _snapshot_at, _refreshing
    try:
        built = _build_snapshot()
        with _refresh_lock:
            _snapshot, _snapshot_at = built, time.monotonic()
    finally:
        with _refresh_lock:
            _refreshing = False


async def readiness_payload_async() -> Dict[str, Any]:
    """Cached, single-flight readiness. Never blocks the event loop."""
    global _refreshing
    with _refresh_lock:
        snap, age = _snapshot, time.monotonic() - _snapshot_at
        need = snap is None or age > SNAPSHOT_TTL_S
        start = need and not _refreshing
        if start:
            _refreshing = True

    if start:
        task = asyncio.get_running_loop().run_in_executor(None, _refresh_snapshot)
        if snap is None:
            # No snapshot at all: a caller must wait, but only up to the
            # deadline. Past it they are told so rather than hanging.
            try:
                await asyncio.wait_for(asyncio.shield(task), REFRESH_DEADLINE_S)
            except asyncio.TimeoutError:
                return {"status": "unknown", "reason": "probe_timeout", "databases": {}}

    with _refresh_lock:
        snap, age = _snapshot, time.monotonic() - _snapshot_at
    if snap is None:
        return {"status": "unknown", "reason": "probing", "databases": {}}
    out = dict(snap)
    out["age_seconds"] = round(age, 1)
    out["stale"] = age > SNAPSHOT_TTL_S
    return out


def readiness_payload() -> Dict[str, Any]:
    """Synchronous readiness, for the CLI and tests. Always probes."""
    return _build_snapshot()


def _is_authorised(request: Any) -> bool:
    """Detail is privileged: a shared secret, or a loopback peer."""
    token = os.getenv("MEMORA_HEALTH_TOKEN", "")
    if token:
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        if header.startswith(prefix) and hmac.compare_digest(header[len(prefix):], token):
            return True
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _redact(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate only: no names, no counts, no error text."""
    return {
        "status": payload.get("status", "unknown"),
        "degraded_count": len(payload.get("degraded", [])),
        "database_count": len(payload.get("databases", {})),
    }


def register_health_routes(mcp: Any) -> None:
    """Attach /health, /health/db and /health/db/{name} to FastMCP's HTTP app."""
    import logging

    from starlette.responses import JSONResponse

    logger = logging.getLogger("memora.health")

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_request):
        return JSONResponse(liveness_payload())

    @mcp.custom_route("/health/db", methods=["GET"])
    async def _health_db(request):
        payload = await readiness_payload_async()
        if payload.get("status") == "degraded":
            logger.warning("readiness degraded: %s", payload.get("degraded"))
        if not _is_authorised(request):
            return JSONResponse(_redact(payload))
        # ALWAYS 200. This is an alert surface: a 503 here would make a load
        # balancer withdraw the whole process because ONE store is degraded,
        # taking the healthy databases down with it.
        return JSONResponse(payload)

    @mcp.custom_route("/health/db/{name}", methods=["GET"])
    async def _health_db_one(request):
        name = request.path_params["name"]
        payload = await readiness_payload_async()
        entry = payload.get("databases", {}).get(name)
        if entry is None:
            return JSONResponse({"status": "unknown"}, status_code=404)
        ok = entry.get("status") == "ok"
        # Per-database readiness MAY 503: withdrawing on this affects only the
        # workspaces bound to this database.
        body = {"status": entry.get("status")} if not _is_authorised(request) else entry
        return JSONResponse(body, status_code=200 if ok else 503)
