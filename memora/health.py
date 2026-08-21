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
from concurrent.futures import ThreadPoolExecutor
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
REFRESH_DEADLINE_S = _positive_seconds("MEMORA_HEALTH_TIMEOUT", "15", cap=300)
# Beyond this age a cached per-database result may no longer be reported READY.
MAX_STALENESS_S = _positive_seconds("MEMORA_HEALTH_MAX_STALE", "60", cap=3600)
if MAX_STALENESS_S < SNAPSHOT_TTL_S:
    # Otherwise there is a window where per-database readiness reports 503
    # "too stale" while no authorised request will schedule a refresh yet --
    # a signal the operator cannot act on and the server cannot clear.
    raise HealthConfigError(
        f"MEMORA_HEALTH_MAX_STALE ({MAX_STALENESS_S}) must be >= "
        f"MEMORA_HEALTH_TTL ({SNAPSHOT_TTL_S})"
    )

def _interval_seconds(env: str, default: str, *, cap: float) -> float:
    """Like _positive_seconds but 0 is legal and means "no periodic refresh"."""
    raw = os.getenv(env, default).strip() or default
    try:
        value = float(raw)
    except ValueError as exc:
        raise HealthConfigError(f"{env}={raw!r} is not a number") from exc
    if value != value or value < 0 or value > cap:
        raise HealthConfigError(f"{env}={raw!r} must be >= 0 and <= {cap}")
    return value


# How often the server refreshes readiness ON ITS OWN. Without this the
# snapshot is only ever as fresh as the last authorised caller -- and in the
# deployed shape (clients reach the container through a proxy, so no peer is
# loopback and no token need be set) there IS no authorised caller, so the
# alert surface reported "unknown" forever while every database was fine.
# 0 disables it, for tests and for anyone who wants poll-only behaviour.
REFRESH_INTERVAL_S = _interval_seconds("MEMORA_HEALTH_REFRESH_INTERVAL", "15", cap=3600)
if REFRESH_INTERVAL_S and (REFRESH_INTERVAL_S + REFRESH_DEADLINE_S) >= MAX_STALENESS_S:
    # The interval ALONE proves too little. A replacement verdict does not
    # arrive when the next cycle starts, it arrives up to a full deadline
    # later, so the worst case a healthy verdict must survive is
    # interval + deadline. interval=50/deadline=15/max_stale=60 satisfies
    # "interval < max_stale" and still ages evidence out before its
    # replacement can land, reporting "unknown" on a schedule while every
    # database is fine. Shipped defaults (15 + 15 < 60) are safe.
    raise HealthConfigError(
        f"MEMORA_HEALTH_REFRESH_INTERVAL ({REFRESH_INTERVAL_S}) + "
        f"MEMORA_HEALTH_TIMEOUT ({REFRESH_DEADLINE_S}) must be < "
        f"MEMORA_HEALTH_MAX_STALE ({MAX_STALENESS_S})"
    )
if REFRESH_INTERVAL_S and REFRESH_INTERVAL_S > SNAPSHOT_TTL_S:
    # Not an error, but stated rather than left to be discovered: between the
    # TTL and the next cycle a perfectly healthy snapshot reports stale=True
    # (~5s of every 15s cycle on the defaults). "stale" means "past its
    # refresh due date", NOT "no longer evidence" -- that is MAX_STALE, and it
    # is what the routes actually gate on. Raise the TTL to make it rare.
    pass

_PROCESS_START = time.monotonic()

_logger = __import__("logging").getLogger("memora.health")

_snapshot: Optional[Dict[str, Any]] = None
_snapshot_at: float = 0.0
_refresh_lock = threading.Lock()
# Ownership of the single refresh slot. A bare boolean could not say WHICH
# refresh held it: a superseded pass would clear a live pass's flag on its way
# out and let a third start. 0 means the slot is free.
_refresh_owner = 0
_refresh_seq = 0
# Bumped whenever the refresher is stopped. A refresh pass that began under a
# superseded generation must NOT publish: cancellation is asynchronous, so an
# already-running probe can otherwise land after its configuration is gone and
# overwrite the snapshot with verdicts nobody asked for.
_generation = 0


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


# One persistent pool. A `with ThreadPoolExecutor(...)` block calls
# shutdown(wait=True) on exit, so the previous version WAITED for the very
# probe it had just labelled timed-out -- a 0.5s deadline took 5.03s to
# return, and with a genuinely hung backend the single refresh slot stuck
# forever. The test asserted the payload but not the elapsed time, so it was
# vacuous for the property it claimed.
_probe_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="memora-health")
# name -> in-flight future. A name is never resubmitted while its previous
# probe is still running; shutdown(wait=False) alone would leak one thread per
# refresh against a permanently hung store.
_inflight: Dict[str, Any] = {}


def _probe_all(names) -> Dict[str, Dict[str, Any]]:
    """Probe every store concurrently, publishing at the deadline.

    Completed futures are merged immediately; unfinished names publish
    unknown/probe_timeout and their futures are RETAINED so a later refresh
    does not submit the same name again. Nothing waits on a stuck probe.
    """
    from concurrent.futures import wait

    labels = {(n or "(default)"): n for n in names}
    with _refresh_lock:
        for label, name in labels.items():
            fut = _inflight.get(label)
            if fut is not None and fut.done():
                _inflight.pop(label, None)
                fut = None
            if fut is None:
                _inflight[label] = _probe_pool.submit(_probe_one, name)
        pending = {label: _inflight[label] for label in labels}

    wait(list(pending.values()), timeout=REFRESH_DEADLINE_S)

    out: Dict[str, Dict[str, Any]] = {}
    with _refresh_lock:
        for label, fut in pending.items():
            if fut.done():
                _inflight.pop(label, None)
                try:
                    out[label] = fut.result()
                except Exception as exc:  # pragma: no cover - _probe_one catches
                    out[label] = {"status": "error", "error": type(exc).__name__}
            else:
                # Still running: report it, keep the future, do not resubmit.
                out[label] = {
                    "status": "unknown",
                    "reason": "probe_timeout",
                    "latency_ms": round(REFRESH_DEADLINE_S * 1000, 1),
                }
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


def _refresh_snapshot(owner: int, generation: int) -> None:
    """Run one refresh pass on behalf of a specific claim.

    Both the owner token and the generation are captured when the pass is
    CLAIMED, never here: this function starts whenever an executor thread
    picks it up, and a stop between claim and start would otherwise let a
    superseded pass read the NEW generation and publish anyway.
    """
    global _snapshot, _snapshot_at, _refresh_owner
    try:
        built = _build_snapshot()
        with _refresh_lock:
            if owner == _refresh_owner and generation == _generation:
                _snapshot, _snapshot_at = built, time.monotonic()
            else:
                _logger.debug("discarding superseded readiness refresh %d", owner)
    finally:
        with _refresh_lock:
            # Only the OWNER may free the slot. Clearing unconditionally let a
            # stale pass release a newer pass's claim.
            if _refresh_owner == owner:
                _refresh_owner = 0


async def readiness_payload_async(*, may_refresh: bool = True) -> Dict[str, Any]:
    """Cached, single-flight readiness. Never blocks the event loop.

    `may_refresh=False` for unauthorised callers: single-flight caps
    CONCURRENCY but an anonymous caller could still force one N-database
    refresh per TTL forever. An unauthorised request now reads whatever
    snapshot exists and never schedules work.
    """
    global _refresh_owner, _refresh_seq
    with _refresh_lock:
        snap, age = _snapshot, time.monotonic() - _snapshot_at
        need = snap is None or age > SNAPSHOT_TTL_S
        start = need and _refresh_owner == 0 and may_refresh
        if start:
            _refresh_seq += 1
            owner, generation = _refresh_seq, _generation
            _refresh_owner = owner

    if start:
        task = asyncio.get_running_loop().run_in_executor(
            None, _refresh_snapshot, owner, generation)
        if snap is None:
            try:
                await asyncio.wait_for(asyncio.shield(task), REFRESH_DEADLINE_S)
            except asyncio.TimeoutError:
                pass  # fall through and report what exists, if anything
    elif snap is None and may_refresh:
        # A refresh is ALREADY in flight -- started by the periodic refresher
        # or by a concurrent request -- so single-flight correctly refused to
        # start another. Without waiting for that one, adding the refresher
        # made the first readiness request after startup answer "unknown"
        # while the real answer was a second away: the refresher stole the
        # refresh this caller used to perform itself.
        deadline = time.monotonic() + REFRESH_DEADLINE_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            with _refresh_lock:
                if _snapshot is not None:
                    break

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


_refresher_task: Optional[Any] = None


async def _refresh_periodically() -> None:
    """Keep the snapshot fresh regardless of who is asking, or whether anyone is.

    Readiness used to refresh ONLY on an authorised request. Behind the
    deployment's proxy no caller is loopback and no token was set, so nothing
    could ever schedule a refresh and /health/db reported "unknown" forever --
    indistinguishable from every database being unreachable, which is the one
    thing it exists to tell you apart.
    """
    while True:
        try:
            await readiness_payload_async(may_refresh=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A refresher that dies on one bad pass leaves readiness frozen at
            # its last value, which reads as healthy. Log and keep the loop.
            _logger.warning("readiness refresh failed", exc_info=True)
        await asyncio.sleep(REFRESH_INTERVAL_S)


def ensure_refresher() -> bool:
    """Start the periodic refresh once, on the serving loop. Idempotent.

    Started lazily from the health routes rather than from a lifespan hook so
    it works identically under mcp.run() and under the #965 path router. The
    watchdog polls /health continuously, so in production it always starts.
    Returns True if a task is running afterwards.
    """
    global _refresher_task
    if REFRESH_INTERVAL_S <= 0:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    with _refresh_lock:
        task = _refresher_task
        # A task is only useful if it is alive AND belongs to the loop now
        # serving. A task from a previous loop reports not-done forever while
        # never running again, so trusting `done()` alone silently restores
        # the never-refreshes bug on any loop replacement.
        if task is not None and not task.done() and task.get_loop() is loop:
            return True
        if task is not None:
            # Replacing a dead or foreign-loop refresher. Its executor pass may
            # still be running and would otherwise publish into the snapshot
            # this loop is about to own, so retire it explicitly.
            _invalidate_locked()
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        # A crashed task is replaced, not inherited: leaving a dead refresher
        # in place would silently restore the never-refreshes bug.
        _refresher_task = loop.create_task(_refresh_periodically())
        return True


def _invalidate_locked() -> None:
    """Retire every in-flight refresh. Caller holds _refresh_lock.

    Bumping the generation fences publication; freeing the slot lets a new
    owner claim it (the retired pass can no longer publish OR clear, because
    both checks compare against values it can never match again). _inflight is
    dropped so a probe started under the OLD configuration is not reused for
    the new one purely because its database NAME matches -- a permanently hung
    'alpha' future outliving the config that created it. Abandoned probes hold
    pool threads until they return; that is bounded by the pool and happens
    only on stop/replace, never per refresh.
    """
    global _generation, _refresh_owner
    _generation += 1
    _refresh_owner = 0
    _inflight.clear()


def stop_refresher() -> None:
    """Cancel the periodic refresh, from any thread.

    A refresher outlives the app that started it unless something stops it: it
    keeps probing on its own loop and keeps writing into the module-global
    snapshot. That surfaced as an unrelated readiness test failing 2 runs in 3,
    because a previous server's refresher published verdicts gathered against a
    torn-down configuration.
    """
    global _refresher_task
    with _refresh_lock:
        task, _refresher_task = _refresher_task, None
        _invalidate_locked()
    if task is None:
        return
    try:
        task.get_loop().call_soon_threadsafe(task.cancel)
    except RuntimeError:
        # Loop already closed: the task cannot run again anyway.
        pass


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
        # Liveness itself stays database-free; this only starts the background
        # refresher, which is what makes readiness self-sustaining.
        ensure_refresher()
        return JSONResponse(liveness_payload())

    @mcp.custom_route("/health/db", methods=["GET"])
    async def _health_db(request):
        ensure_refresher()
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
        # The module doc recommends THIS route for workspace-specific probes,
        # so a deployment may never touch /health or aggregate /health/db. If
        # it did not start the refresher, such a deployment would sit at 503
        # unknown forever -- #996 again, on the route built for the job.
        ensure_refresher()
        name = request.path_params["name"]
        authorised = _is_authorised(request)

        # 404 is reserved for a name that is NOT CONFIGURED. A configured name
        # with no fresh result is "known but unproven" -- 503, not 404.
        try:
            registry = database_registry()
        except DatabaseRegistryError as exc:
            # A CONFIGURATION failure is not "this database does not exist".
            # Returning 404 would tell an operator the name is wrong when the
            # registry itself is broken.
            return JSONResponse(
                {"status": "unknown", "reason": "registry_error"}
                if not _is_authorised(request)
                else {"status": "unknown", "reason": "registry_error", "message": str(exc)},
                status_code=503,
            )
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
