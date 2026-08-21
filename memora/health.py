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

class HealthConfigError(RuntimeError):
    """Health tuning is unusable. Raised at import; never silently defaulted."""


def _positive_seconds(env: str, default: str, *, cap: float) -> float:
    raw = os.getenv(env, default).strip() or default
    try:
        value = float(raw)
    except ValueError as exc:
        raise HealthConfigError(f"{env}={raw!r} is not a number") from exc
    if not (value > 0) or value != value or value in (float("inf"),) or value > cap:
        # Zero or negative TTL would make every request trigger a refresh,
        # turning the endpoint back into the unbounded fanout it was.
        raise HealthConfigError(f"{env}={raw!r} must be > 0 and <= {cap}")
    return value


# How long a readiness snapshot may be served before a refresh is triggered.
SNAPSHOT_TTL_S = _positive_seconds("MEMORA_HEALTH_TTL", "10", cap=3600)
# Bound on one refresh pass and on each individual store probe.
REFRESH_DEADLINE_S = _positive_seconds("MEMORA_HEALTH_TIMEOUT", "5", cap=300)
# Beyond this age a cached per-database result may no longer be reported READY.
MAX_STALENESS_S = _positive_seconds("MEMORA_HEALTH_MAX_STALE", "60", cap=3600)

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


def _probe_all(names) -> Dict[str, Dict[str, Any]]:
    """Probe every store CONCURRENTLY with a per-store deadline.

    Sequential probing meant a hung alpha hid beta entirely -- the exact
    property this endpoint claims. Each store gets its own future and its own
    deadline, and a store that does not answer in time is published as
    unknown/timeout while the finished ones stay visible.

    A timeout cannot cancel blocked I/O, so the executor is bounded and the
    caller never waits on the stuck future again; it is left to finish and be
    discarded rather than resubmitted indefinitely.
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    out: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(names)))) as pool:
        futures = {pool.submit(_probe_one, n): (n or "(default)") for n in names}
        done, not_done = wait(futures, timeout=REFRESH_DEADLINE_S)
        for fut in done:
            label = futures[fut]
            try:
                out[label] = fut.result()
            except Exception as exc:  # pragma: no cover - _probe_one catches
                out[label] = {"status": "error", "error": type(exc).__name__}
        for fut in not_done:
            out[futures[fut]] = {
                "status": "unknown",
                "reason": "probe_timeout",
                "latency_ms": round(REFRESH_DEADLINE_S * 1000, 1),
            }
            fut.cancel()
    return out


def _build_snapshot() -> Dict[str, Any]:
    try:
        registry = database_registry()
        default = default_database_name()
    except DatabaseRegistryError as exc:
        return {"status": "error", "message": str(exc), "databases": {}, "default_database": None}

    names = sorted(registry) if registry else [None]
    result = _probe_all(names)
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


async def readiness_payload_async(*, may_refresh: bool = True) -> Dict[str, Any]:
    """Cached, single-flight readiness. Never blocks the event loop.

    `may_refresh=False` for unauthorised callers: single-flight caps
    CONCURRENCY but an anonymous caller could still force one N-database
    refresh per TTL forever. An unauthorised request now reads whatever
    snapshot exists and never schedules work.
    """
    global _refreshing
    with _refresh_lock:
        snap, age = _snapshot, time.monotonic() - _snapshot_at
        need = snap is None or age > SNAPSHOT_TTL_S
        start = need and not _refreshing and may_refresh
        if start:
            _refreshing = True

    if start:
        task = asyncio.get_running_loop().run_in_executor(None, _refresh_snapshot)
        if snap is None:
            try:
                await asyncio.wait_for(asyncio.shield(task), REFRESH_DEADLINE_S)
            except asyncio.TimeoutError:
                pass  # fall through and report what exists, if anything

    with _refresh_lock:
        snap, age = _snapshot, time.monotonic() - _snapshot_at
    if snap is None:
        return {"status": "unknown", "reason": "probing", "databases": {},
                "default_database": None, "degraded": []}
    out = dict(snap)
    out["age_seconds"] = round(age, 1)
    out["stale"] = age > SNAPSHOT_TTL_S
    out["too_stale"] = age > MAX_STALENESS_S
    if out["too_stale"]:
        # Beyond max staleness the cached verdicts are not evidence any more.
        out["status"] = "unknown"
    return out


def readiness_payload() -> Dict[str, Any]:
    """Synchronous readiness, for the CLI and tests. Always probes."""
    return _build_snapshot()


def _is_authorised(request: Any) -> bool:
    """Detail is privileged: a shared secret, or a loopback peer."""
    token = os.getenv("MEMORA_HEALTH_TOKEN", "")
    if token:
        header = request.headers.get("authorization", "") or ""
        prefix = "Bearer "
        if header.startswith(prefix):
            presented = header[len(prefix):]
            try:
                # compare_digest RAISES on non-ASCII str operands; a malformed
                # header must be a refusal, not a 500.
                if hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
                    return True
            except (TypeError, UnicodeError):
                return False
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
        authorised = _is_authorised(request)
        payload = await readiness_payload_async(may_refresh=authorised)
        if payload.get("status") == "degraded":
            logger.warning("readiness degraded: %s", payload.get("degraded"))
        if not authorised:
            return JSONResponse(_redact(payload))
        # ALWAYS 200. This is an alert surface: a 503 here would make a load
        # balancer withdraw the whole process because ONE store is degraded,
        # taking the healthy databases down with it.
        return JSONResponse(payload)

    @mcp.custom_route("/health/db/{name}", methods=["GET"])
    async def _health_db_one(request):
        name = request.path_params["name"]
        authorised = _is_authorised(request)

        # 404 is reserved for a name that is NOT CONFIGURED. A configured name
        # with no fresh result is "known but unproven" -- 503, not 404.
        try:
            registry = database_registry()
        except DatabaseRegistryError:
            registry = {}
        known = name in registry or (not registry and name == "(default)")

        payload = await readiness_payload_async(may_refresh=authorised)
        entry = payload.get("databases", {}).get(name)

        if entry is None:
            if not known:
                return JSONResponse({"status": "unknown"}, status_code=404)
            body = {"status": "unknown", "reason": payload.get("reason", "unproven")}
            return JSONResponse(body, status_code=503)

        # A cached OK past max staleness is no longer evidence: a hung refresh
        # would otherwise keep a dead store reporting 200 forever.
        ok = entry.get("status") == "ok" and not payload.get("too_stale")
        if authorised:
            body = dict(entry)
            body["stale"] = bool(payload.get("stale"))
            body["age_seconds"] = payload.get("age_seconds")
        else:
            body = {"status": entry.get("status") if ok else "unknown"}
        return JSONResponse(body, status_code=200 if ok else 503)
