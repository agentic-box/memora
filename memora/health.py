"""Health of the RUNNING server (memora #965 phase 4).

`memora.cli health` spawns a FRESH process: it proves a config can connect, not
that the live server can. Under the single-server shape that distinction stops
being pedantic — one process serves every workspace, so "is it up" and "which
databases can it actually reach" become the operator's only warning before
agents go quiet.

Two endpoints, deliberately different:

  GET /health      LIVENESS. Is this process serving? Answers without touching
                   any database, so a store being down never makes the server
                   look dead. Cheap enough for a sidebar to poll.

  GET /health/db   READINESS, per database. Actually connects to each
                   registered store and reports it individually. A slow or
                   broken D1 shows up as that database degraded, not as a
                   server outage.

Splitting them is the point. Collapsing liveness and readiness is what makes a
supervisor restart a healthy process because one remote store is slow -- and a
restart takes memory from EVERY workspace at once.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from . import __version__ as memora_version
from .storage import (
    CURRENT_DB,
    DatabaseRegistryError,
    database_registry,
    default_database_name,
)

# A per-database probe must not hang the whole readiness report.
DB_PROBE_TIMEOUT_S = 5.0


def liveness_payload() -> Dict[str, Any]:
    """Is the process serving? Never touches a database."""
    return {
        "status": "ok",
        "version": memora_version,
        "pid_uptime_hint": time.time(),
    }


def _probe_one(name: str | None) -> Dict[str, Any]:
    """Connect to one database and report it. Never raises."""
    from .storage import connect

    started = time.time()
    token = CURRENT_DB.set(name) if name is not None else None
    try:
        conn = connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            count = row[0] if row else 0
        finally:
            conn.close()
        return {
            "status": "ok",
            "memory_count": count,
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
    except Exception as exc:
        # A broken store is reported, not raised: one bad database must not
        # make the readiness endpoint itself fail, or the operator learns
        # nothing about the others.
        return {
            "status": "error",
            "error": type(exc).__name__,
            "message": str(exc)[:200],
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
    finally:
        if token is not None:
            CURRENT_DB.reset(token)


def readiness_payload() -> Dict[str, Any]:
    """Per-database readiness. Degraded if any store is unreachable."""
    try:
        registry = database_registry()
        default = default_database_name()
    except DatabaseRegistryError as exc:
        return {"status": "error", "message": str(exc), "databases": {}}

    if not registry:
        result = {"(default)": _probe_one(None)}
    else:
        result = {name: _probe_one(name) for name in sorted(registry)}

    degraded = [n for n, r in result.items() if r["status"] != "ok"]
    return {
        "status": "degraded" if degraded else "ok",
        "version": memora_version,
        "default_database": default,
        "degraded": degraded,
        "databases": result,
    }


def register_health_routes(mcp: Any) -> None:
    """Attach /health and /health/db to FastMCP's HTTP app."""
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_request):
        return JSONResponse(liveness_payload())

    @mcp.custom_route("/health/db", methods=["GET"])
    async def _health_db(_request):
        payload = readiness_payload()
        # 503 when any database is unreachable, so a probe that only reads the
        # status CODE still learns the truth.
        code = 200 if payload["status"] == "ok" else 503
        return JSONResponse(payload, status_code=code)
