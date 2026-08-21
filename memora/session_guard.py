"""Refuse requests the SDK would allocate a session for (memora #999).

THE HOLE. StreamableHTTPSessionManager._handle_stateful_request creates a
StreamableHTTPServerTransport, registers it in _server_instances and starts
its server task for ANY request carrying no Mcp-Session-Id -- BEFORE
validating it. Every check that could reject the request happens afterwards,
in streamable_http.py: Accept (:452), Content-Type (:456), generic JSON-RPC
(:469-483), then typed InitializeRequest inside ServerSession. Each of those
rejections leaves the newly registered transport behind. Measured at 41.3 kB
per rejected probe, that exhausted a 768 MB container in ~3.2 hours under a
liveness probe sending 88/min.

THE INVARIANT THIS MODULE ESTABLISHES: a request that the SDK is going to
reject never reaches the SDK. That is stronger than "rate limit the damage",
and it is the only version that holds when the bounds below are disabled.

Getting there took three corrections, recorded because each was a real hole:
  - refusing session-less non-POST closes the bare-GET fuse and leaves the
    identical primitive behind a POST with an empty body;
  - checking that the body merely "looks like" an initialize still admits
    requests the SDK rejects on headers or on typed validation, e.g.
    {"method":"initialize","id":1} with no jsonrpc, or id: null;
  - so acceptance here mirrors the SDK's OWN checks, in the SDK's order,
    using the SDK's OWN types -- not an approximation of them.

Two bounds sit on top for what prevalidation cannot refuse: a flood of
perfectly VALID initializes creates real sessions, and a client that
handshakes correctly and vanishes leaves one behind forever.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Callable

logger = logging.getLogger("memora.session_guard")

_MCP_PATH = "/mcp"
_SESSION_HEADER = b"mcp-session-id"
_CONTENT_TYPE_JSON = "application/json"
_ACCEPT_SSE = "text/event-stream"


class SessionGuardConfigError(RuntimeError):
    """Guard tuning is unusable. Raised at import; never silently defaulted."""


def _int_env(name: str, default: str, *, minimum: int) -> int:
    raw = os.getenv(name, default).strip() or default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SessionGuardConfigError(f"{name}={raw!r} is not a whole number") from exc
    if value < minimum:
        # A negative bound silently disabled the limiter while reading as if
        # it were set -- the documented way to disable it is 0.
        raise SessionGuardConfigError(f"{name}={raw!r} must be >= {minimum}")
    return value


# An initialize body is a few hundred bytes. Anything larger is not one, and
# buffering it to find that out is itself the cost being avoided.
MAX_INIT_BODY_BYTES = _int_env("MEMORA_MAX_INIT_BODY_BYTES", "65536", minimum=1024)
# New sessions admitted per minute. 0 disables. Well above any real workload
# (six workspaces reconnecting is a handful) and far below exhaustion.
MAX_INIT_PER_MIN = _int_env("MEMORA_MAX_INIT_PER_MIN", "120", minimum=0)

_admissions: list[float] = []


def _now() -> float:
    import time
    return time.monotonic()


def _admit() -> bool:
    """Sliding one-minute window over ADMITTED initializations.

    Charged only once a request is fully eligible to reach the manager --
    never for one the router or path check is about to refuse. Charging
    earlier let 120 requests to an unknown path exhaust the window and block
    every legitimate workspace for 60s while creating zero sessions.
    """
    if MAX_INIT_PER_MIN <= 0:
        return True
    now = _now()
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


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _has_session(scope) -> bool:
    for key, _ in scope.get("headers", ()):
        if key.lower() == _SESSION_HEADER:
            return True
    return False


def _headers_acceptable(scope) -> bool:
    """Mirror the SDK's Accept and Content-Type checks, in its order.

    The SDK requires BOTH application/json and text/event-stream on a POST
    (json-response mode requires only json, and is not what memora runs).
    Content-Type is matched on the media type alone, parameters stripped,
    exactly as _check_content_type does.
    """
    accept = [p.strip() for p in _header(scope, b"accept").split(",")]
    has_json = any(p.split(";")[0].strip() == _CONTENT_TYPE_JSON for p in accept)
    has_sse = any(p.split(";")[0].strip() == _ACCEPT_SSE for p in accept)
    if not (has_json and has_sse):
        return False
    ctype = _header(scope, b"content-type").split(";")[0]
    return any(p.strip() == _CONTENT_TYPE_JSON for p in ctype.split(","))


def _is_initialize(body: bytes) -> bool:
    """Would the SDK accept this as an InitializeRequest?

    Uses the SDK's own models rather than a hand-rolled shape check: generic
    JSONRPCMessage must resolve to a JSONRPCRequest, and typed ClientRequest
    must resolve to an InitializeRequest. This is what makes the guard's
    acceptance set identical to the SDK's rather than merely similar --
    {"method":"initialize","id":1} and id: null both look right and are both
    rejected downstream, after allocation.

    Protocol negotiation is preserved: an unsupported-but-well-formed
    protocolVersion validates here and the server negotiates it, as it should.
    """
    from mcp.types import ClientRequest, InitializeRequest, JSONRPCMessage, JSONRPCRequest

    try:
        parsed = json.loads(body)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False  # a batch is not an initialize; the SDK refuses it too
    try:
        message = JSONRPCMessage.model_validate(parsed)
    except Exception:
        return False
    if not isinstance(message.root, JSONRPCRequest):
        return False
    try:
        request = ClientRequest.model_validate(parsed)
    except Exception:
        return False
    return isinstance(request.root, InitializeRequest)


async def _buffer_body(receive: Callable, limit: int):
    """Read the whole body, then hand back a receive that replays it.

    After the replayed body the wrapper DELEGATES to the original receive.
    Returning a synthetic empty http.request forever instead made SSE's
    disconnect listener hot-loop on immediately-ready messages and never
    observe the real http.disconnect.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, None
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > limit:
            return b"", None
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
        return await receive()

    return body, replay


def guard_sessions(inner: Any) -> Callable:
    """Wrap a stateful streamable-http app so refusals cost nothing.

    Guards the EXACT /mcp endpoint. In registry deployments this sits INSIDE
    the database router, so a request to an unknown database is refused (and
    charged nothing) before it gets here; in single-store deployments a path
    other than /mcp is left for Starlette to reject. Wrapping the outside
    instead let a request that could never create a session consume the
    admission budget for everyone.

    Applied to every streamable-http deployment, not only the routed one: a
    single-database server reached through plain mcp.run() has the same hole.
    """

    async def app(scope, receive, send):
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return

        # Only the MCP endpoint itself. /health and any other route -- and any
        # path this app does not own -- are none of the guard's business.
        if scope.get("path") != _MCP_PATH or _has_session(scope):
            await inner(scope, receive, send)
            return

        if scope.get("method") != "POST":
            # 406 preserved on purpose: clmux's sidebar reachability probe
            # reads it as "MCP is answering" (#969). Same answer, no session.
            await _respond(send, 406, "session required")
            return

        if not _headers_acceptable(scope):
            await _respond(send, 406, "client must accept application/json and text/event-stream")
            return

        body, replay = await _buffer_body(receive, MAX_INIT_BODY_BYTES)
        if replay is None:
            if body is None:
                return                       # client disconnected
            await _respond(send, 413, "request too large")
            return
        if not _is_initialize(body):
            await _respond(send, 400, "expected an initialize request")
            return
        # Charged LAST: only a request that is fully eligible to reach the
        # session manager consumes capacity.
        if not _admit():
            logger.warning("refusing initialize: more than %d per minute", MAX_INIT_PER_MIN)
            await _respond(send, 429, "too many new sessions")
            return

        await inner(scope, replay, send)

    return app


def idle_timeout_seconds() -> float:
    """Validated MEMORA_SESSION_IDLE_TIMEOUT. 0 disables reaping."""
    raw = os.getenv("MEMORA_SESSION_IDLE_TIMEOUT", "1800").strip() or "1800"
    try:
        value = float(raw)
    except ValueError as exc:
        raise SessionGuardConfigError(
            f"MEMORA_SESSION_IDLE_TIMEOUT={raw!r} is not a number") from exc
    if math.isnan(value) or math.isinf(value) or value < 0:
        # NaN silently disabled reaping while reading as configured.
        raise SessionGuardConfigError(
            f"MEMORA_SESSION_IDLE_TIMEOUT={raw!r} must be a finite number >= 0")
    return value
