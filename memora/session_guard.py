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
# HARD CEILING on concurrent sessions. A creation RATE plus an idle timeout is
# not a bound: the manager refreshes a known session's idle deadline BEFORE
# validating the request, so an attacker who keeps the session ids can hold
# every one of them alive with cheap rejected requests -- about two per second
# for a thousand sessions -- and grow without limit at the creation rate. Only
# a ceiling checked before allocation actually bounds it. 0 disables.
MAX_SESSIONS = _int_env("MEMORA_MAX_SESSIONS", "128", minimum=0)

# Admissions are request-SCOPED tokens, not bare timestamps. Popping "the
# newest" refunded whichever request happened to be last, which under
# concurrency is not the one being refunded.
_admissions: list[tuple[float, int]] = []
_admission_seq = 0
# Capacity reserved by requests that have passed the ceiling check but whose
# session does not exist yet. Without it the check is a read followed by an
# await, and N concurrent initializes all observe the same pre-burst count and
# all allocate -- codex reproduced exactly that with cap=1 and two requests.
_pending = 0


def _now() -> float:
    import time
    return time.monotonic()


def _admit():
    """Charge one slot in the sliding one-minute window; return its token.

    Returns None when the window is full. The token identifies THIS request's
    charge so it can be refunded specifically.

    Charged only once a request is fully eligible to reach the manager --
    never for one the router or path check is about to refuse. Charging
    earlier let 120 requests to an unknown path exhaust the window and block
    every legitimate workspace for 60s while creating zero sessions.
    """
    global _admission_seq
    if MAX_INIT_PER_MIN <= 0:
        return _UNLIMITED
    now = _now()
    cutoff = now - 60.0
    while _admissions and _admissions[0][0] < cutoff:
        _admissions.pop(0)
    if len(_admissions) >= MAX_INIT_PER_MIN:
        return None
    _admission_seq += 1
    token = (now, _admission_seq)
    _admissions.append(token)
    return token


_UNLIMITED = (0.0, -1)   # sentinel: nothing was charged, nothing to refund


def _reset_admissions() -> None:
    """Test seam: the window and reservations are process-global."""
    global _pending
    _admissions.clear()
    _pending = 0


def _refund(token) -> None:
    """Give back the slot THIS request charged.

    Downstream can still refuse after the guard -- authentication being the
    important case -- and those refusals must not consume the budget that
    authorised clients need. The token is removed by identity: refunding "the
    most recent admission" gave back somebody else's slot whenever two
    requests overlapped.
    """
    if token is _UNLIMITED or token is None:
        return
    try:
        _admissions.remove(token)
    except ValueError:
        pass  # already aged out of the window


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


def guard_sessions(inner: Any, *, session_count: Callable | None = None,
                   security: Any = None) -> Callable:
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

        # TRANSPORT SECURITY FIRST, because that is the SDK's real order:
        # TransportSecurityMiddleware.validate_request runs at
        # streamable_http.py:382 and checks Content-Type and, with DNS-rebinding
        # protection enabled (FastMCP's default on a loopback bind), Host and
        # Origin. A fully typed initialize with Host: evil.example passed the
        # earlier guard, allocated, then took a 421 and stayed retained.
        if security is not None:
            from starlette.requests import Request

            async def _no_body():
                return {"type": "http.request", "body": b"", "more_body": False}

            error = await security.validate_request(Request(scope, _no_body), is_post=True)
            if error is not None:
                await error(scope, receive, send)
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
        # HARD CEILING, RESERVED ATOMICALLY. Everything from the read of
        # session_count() to the increment of _pending runs with no await, so
        # concurrent initializes cannot all observe the same pre-burst count.
        # The SDK's own _session_creation_lock is taken far too late to help:
        # every request has already passed this point by then.
        global _pending
        reserved = False
        # Called even when the ceiling is disabled: session_count() is also
        # what purges terminated transports, and that garbage must not depend
        # on the cap being switched on.
        active = session_count() if session_count is not None else None
        if MAX_SESSIONS > 0 and active is not None:
            if active + _pending >= MAX_SESSIONS:
                logger.warning("refusing initialize: %d sessions open, %d pending",
                               active, _pending)
                await _respond(send, 503, "session capacity reached")
                return
            _pending += 1
            reserved = True

        try:
            # Charged after the ceiling: only a request that is fully eligible
            # to reach the session manager consumes rate capacity.
            token = _admit()
            if token is None:
                logger.warning("refusing initialize: more than %d per minute",
                               MAX_INIT_PER_MIN)
                await _respond(send, 429, "too many new sessions")
                return

            # Attribute the outcome to THIS request rather than to a global
            # count delta: a delta cannot tell "my session" from "someone
            # else's, created meanwhile". The SDK stamps mcp-session-id on the
            # initialize response, so the response itself is the evidence.
            established = False

            async def watch(message):
                nonlocal established, reserved
                global _pending
                if message["type"] == "http.response.start":
                    status = message.get("status", 0)
                    headers = message.get("headers", ()) or ()
                    has_id = any(k.lower() == _SESSION_HEADER for k, _ in headers)
                    established = 200 <= status < 300 and has_id
                    if established and reserved:
                        # Release the RESERVATION as soon as the session is
                        # real. inner() does not return until the initialize
                        # SSE stream closes, which can be the whole life of
                        # the session -- holding the reservation that long
                        # would count the same session twice and shrink
                        # capacity for everyone else.
                        _pending -= 1
                        reserved = False
                await send(message)

            await inner(scope, replay, watch)
            if not established:
                # Refused downstream -- authentication, most likely -- so no
                # session exists and the slot goes back.
                _refund(token)
        finally:
            if reserved:
                _pending -= 1

    return app


def session_wiring(mcp: Any) -> dict:
    """The live-server hooks the guard needs from a FastMCP instance.

    session_count reads the manager's instance map. That map is private and
    there is no public count, but the ceiling is worthless without it -- and
    the alternative, tracking our own count, would drift from the truth the
    moment the SDK reaped or terminated a session. security reuses the SDK's
    OWN configured middleware rather than a second copy of its rules.
    """
    from mcp.server.transport_security import TransportSecurityMiddleware

    manager = mcp.session_manager
    settings = getattr(mcp.settings, "transport_security", None)
    def live_sessions() -> int:
        """Purge terminated transports, then report what is left.

        Ignoring terminated entries in the COUNT was not enough: on DELETE the
        SDK terminates the transport and its cleanup deletes the map entry
        only when the transport is NOT terminated, so every initialize+DELETE
        cycle left another transport strongly referenced. Excluding them from
        the count merely made that growth invisible to the ceiling -- a client
        could repeat the cycle for the life of the process and the map would
        grow without bound. They have to be REMOVED, not overlooked.

        Purging here is safe and stays atomic: it is synchronous with no
        await, and the SDK's own cleanup guards its `del` with a membership
        check, so removing an entry first cannot make it raise.
        """
        instances = manager._server_instances
        dead = [sid for sid, t in instances.items()
                if getattr(t, "is_terminated", False)]
        for sid in dead:
            instances.pop(sid, None)
        return len(instances)

    return {
        "session_count": live_sessions,
        "security": TransportSecurityMiddleware(settings),
    }


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
