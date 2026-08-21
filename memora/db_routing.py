"""Route an MCP request to a named database by URL path (memora #965 phase 2).

    http://host:8910/mcp/ob1  ->  CURRENT_DB = "ob1"
    http://host:8910/mcp      ->  the registry default

WHY THE PATH AND NOT A TOOL ARGUMENT. Every workspace already owns a URL in its
.mcp.json. Putting the selector there means zero tool-signature churn (an
optional `db` on 43 tools is 43 chances to miss one, and every miss silently
routes to the default -- a cross-database write), no tool-schema cost, and a
client that CANNOT forget to pass it: the binding is configuration, not agent
behaviour.

WHY A BARE ASGI SHIM AND NOT A STARLETTE ROUTE. FastMCP's streamable_http_app()
returns a Starlette app with ONE route at a fixed path. Starlette treats a
(request)-signature callable as an HTTP endpoint, so a raw ASGI callable would
need Mount; a shim is simpler than fighting the router, and delegating the
lifespan through unchanged keeps the session manager running.

THE BINDING IS STICKY PER SESSION, NOT PER REQUEST. Established by the phase 0
spike: a session opened on /mcp/alpha and reused against /mcp/beta still
resolves to alpha. That is the SAFER semantic -- a client cannot half-switch
databases mid-conversation, so there is no window where some calls in one
conversation reach a different store. It is asserted as a regression test
rather than inherited, because a future SDK change making the binding follow
the path per request would open exactly the cross-database window this feature
exists to prevent.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from .storage import CURRENT_DB, DatabaseRegistryError, backend_for, default_database_name

logger = logging.getLogger("memora.db_routing")

_MCP_PREFIX = "/mcp"


class NotAnMcpPath(Exception):
    """The path is not under the MCP route at all; delegate it unchanged."""


def parse_db_from_path(path: str) -> Optional[str]:
    """The database name in an /mcp/<db> path, or None for a bare /mcp.

    Matches on a PATH-COMPONENT boundary, not a string prefix. "/mcpbeta"
    prefix-matched to "beta" and "/mcp-other" to "-other", so any URL merely
    starting with the four characters "/mcp" became an alternate alias for a
    database route -- bypassing proxy or routing rules written for the
    canonical path.

    Raises NotAnMcpPath for anything outside the route, and rejects extra
    segments: /mcp/alpha/anything is not an alias for alpha.
    """
    if path == _MCP_PREFIX or path == _MCP_PREFIX + "/":
        return None
    if not path.startswith(_MCP_PREFIX + "/"):
        raise NotAnMcpPath(path)
    rest = path[len(_MCP_PREFIX) + 1:].rstrip("/")
    if not rest:
        return None
    if "/" in rest:
        raise NotAnMcpPath(path)
    return rest


async def _reject(send: Callable, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def make_router(inner: Any) -> Callable:
    """Wrap FastMCP's streamable-http app with /mcp/<db> selection."""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await inner(scope, receive, send)
            return
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return

        path = scope.get("path", "")
        try:
            name = parse_db_from_path(path)
        except NotAnMcpPath:
            # Not an MCP path at all (/health and friends). Never guard these.
            # Not our route. Delegate unchanged rather than rewriting it.
            await inner(scope, receive, send)
            return
        if name is None:
            # Bare /mcp: the registry default, resolved (and validated) by
            # current_backend(). No binding to set.
            await inner(scope, receive, send)
            return

        # An UNKNOWN database must fail CLOSED with a clear error, never fall
        # through to the default. Serving a store the caller did not ask for is
        # the worst outcome this feature can have -- and it would be silent.
        try:
            backend_for(name)
        except DatabaseRegistryError as exc:
            # GENERIC body. This router sits OUTSIDE the inner app's auth
            # middleware, so serialising the exception would let an
            # unauthenticated caller enumerate database names (the unknown-name
            # error lists the whole known set) and, for a misconfigured entry,
            # read URI/account detail out of parse_backend_uri. Details go to
            # the server log only.
            logger.warning("rejected MCP request for database %r: %s", name, exc)
            await _reject(send, 404, "unknown database")
            return

        # Rewrite the path so FastMCP's fixed route still matches.
        routed = dict(scope, path=_MCP_PREFIX, raw_path=_MCP_PREFIX.encode())
        token = CURRENT_DB.set(name)
        try:
            await inner(routed, receive, send)
        finally:
            CURRENT_DB.reset(token)

    return app


def routed_streamable_http_app(mcp: Any) -> Callable:
    """FastMCP's streamable-http app with database routing in front.

    The pre-session guard (#999) sits INSIDE the router, not outside it: the
    router resolves and validates the database first and rewrites the path to
    the exact /mcp the guard watches, so a request to an UNKNOWN database is
    refused before it can consume the guard's admission budget. Wrapping the
    outside let 120 requests to a nonexistent path block every legitimate
    workspace for a minute while creating zero sessions.
    """
    from .session_guard import guard_sessions, session_wiring

    return make_router(guard_sessions(mcp.streamable_http_app(), **session_wiring(mcp)))


def describe_routes() -> str:
    """One line naming what this server serves, for the startup log."""
    try:
        default = default_database_name()
    except DatabaseRegistryError:
        raise
    if default is None:
        return "single database (MEMORA_STORAGE_URI); /mcp"
    from .storage import database_registry
    names = sorted(database_registry())
    return f"databases {names}, default={default!r}; /mcp/<name> or /mcp"
