"""Refuse requests the SDK would allocate a session for (memora #999).

THE HOLE. StreamableHTTPSessionManager._handle_stateful_request creates a
StreamableHTTPServerTransport, registers it in _server_instances and starts its
server task for ANY request carrying no Mcp-Session-Id -- BEFORE validating it.
Every check that could reject the request (Accept, Content-Type, JSON parse,
"is this actually an initialize") happens later, in streamable_http.py. So the
server pays for requests it refuses, and the payment is permanent: measured at
41.3 kB per rejected probe, which exhausted a 768 MB container in ~3.2 hours
under a liveness probe sending 88/min.

WHY METHOD ALONE IS NOT ENOUGH, and this is the correction to the first fix:
refusing session-less non-POST closes the bare-GET fuse but leaves the same
primitive behind a POST. A loop of `POST /mcp/x` with an empty or malformed
body allocates a session every time and is rejected only afterwards. The guard
must therefore establish that a session-less POST really is an InitializeRequest
BEFORE the manager sees it -- not merely that it is a POST.

AND VALIDATION ALONE IS STILL NOT ENOUGH: a flood of perfectly VALID initialize
requests legitimately creates sessions, and nothing expires them. So this
module also bounds admission, and the server sets an idle timeout on the
session manager. Three layers, because no single one of them closes the hole.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger("memora.session_guard")

_MCP_PREFIX = "/mcp"
_SESSION_HEADER = b"mcp-session-id"

# An initialize body is a few hundred bytes. Anything larger is not one, and
# buffering it to find that out is itself the cost we are trying to avoid.
MAX_INIT_BODY_BYTES = int(os.getenv("MEMORA_MAX_INIT_BODY_BYTES", "65536"))
# New sessions admitted per minute. 0 disables the bound. The default is far
# above any real workload (six workspaces reconnecting is a handful per minute)
# and far below what it takes to exhaust the container.
MAX_INIT_PER_MIN = int(os.getenv("MEMORA_MAX_INIT_PER_MIN", "120"))

_admissions: list[float] = []


def _admit() -> bool:
    """Sliding one-minute window over accepted initializations."""
    if MAX_INIT_PER_MIN <= 0:
        return True
    now = time.monotonic()
    cutoff = now - 60.0
    while _admissions and _admissions[0] < cutoff:
        _admissions.pop(0)
    if len(_admissions) >= MAX_INIT_PER_MIN:
        return False
    _admissions.append(now)
    return True


def _reset_admissions() -> None:
    """Test seam: the window is process-global, so tests must clear it."""
    _admissions.clear()


async def _respond(send: Callable, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def _has_session(scope) -> bool:
    for key, _ in scope.get("headers", ()):
        if key.lower() == _SESSION_HEADER:
            return True
    return False


def _under_mcp(path: str) -> bool:
    return path == _MCP_PREFIX or path.startswith(_MCP_PREFIX + "/")


def _is_initialize(body: bytes) -> bool:
    """Is this exactly a JSON-RPC `initialize` REQUEST?

    Deliberately strict. A notification (no id) does not create a session and
    must not be allowed to. A batch is not an initialize. Anything unparseable
    is not an initialize -- and finding that out here costs nothing, whereas
    finding it out inside the SDK costs a session.
    """
    try:
        parsed = json.loads(body)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    return parsed.get("method") == "initialize" and "id" in parsed


async def _buffer_body(receive: Callable, limit: int):
    """Read the whole body, then hand back a receive that replays it."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, None
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > limit:
            return b"", None          # too large to be an initialize
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    delivered = False

    async def replay():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return body, replay


def guard_sessions(inner: Any) -> Callable:
    """Wrap a stateful streamable-http app so refusals cost nothing.

    Applied to EVERY streamable-http deployment, not only the routed one: a
    single-database server reached through plain mcp.run() has exactly the same
    hole, and the first version of this fix protected only the registry path.
    """

    async def app(scope, receive, send):
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return

        path = scope.get("path", "")
        # /health and anything else that is not the MCP endpoint is none of
        # this guard's business -- the watchdog's liveness probe lives there.
        if not _under_mcp(path) or _has_session(scope):
            await inner(scope, receive, send)
            return

        # No session id from here down.
        if scope.get("method") != "POST":
            # 406 is preserved on purpose: clmux's sidebar reachability probe
            # reads it as "MCP is answering" (#969). Same answer, no session.
            await _respond(send, 406, "session required")
            return

        body, replay = await _buffer_body(receive, MAX_INIT_BODY_BYTES)
        if replay is None:
            if body is None:
                return                       # client disconnected
            await _respond(send, 413, "request too large")
            return
        if not _is_initialize(body):
            # The SDK would have allocated a session and THEN rejected this.
            await _respond(send, 400, "expected an initialize request")
            return
        if not _admit():
            logger.warning("refusing initialize: more than %d per minute", MAX_INIT_PER_MIN)
            await _respond(send, 429, "too many new sessions")
            return

        await inner(scope, replay, send)

    return app
