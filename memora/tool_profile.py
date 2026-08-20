"""MEMORA_TOOL_PROFILE — expose a subset of the 43 MCP tools per deployment.

All 43 tools register unconditionally via ``@mcp.tool()`` in ``server.py``.
This module prunes the registered tools down to the active profile so a
gated tool is GENUINELY ABSENT — missing from ``tools/list`` AND
undispatchable.

Profile membership is DATA here. Editing the leader/agent boundary is a
one-line change to the frozensets below, not a sweep of 43 decorators and
not a scatter of conditionals across the tool definitions.

Profiles (see memora issue #981):

* ``full``    (default, all registered tools): every existing direct-stdio
  deployment is byte-for-byte unchanged.
* ``leader``  (18): the agent set plus section/document/tag/delete/digest.
* ``agent``   (12): the read/create surface a worker agent needs.

``memory_list`` is deliberately excluded from both reduced profiles: issue
#973 measures it at 163-174s vs ``memory_list_compact``'s 0.22s on the
live 836-memory store. Fixing ``memory_list`` is a separate task.

PRIVATE-IMPLEMENTATION COMPATIBILITY (load-bearing, not cosmetic)
------------------------------------------------------------------
The prune deletes entries from FastMCP's PRIVATE ``_tool_manager._tools``
dict. ``tools/list`` and ``call_tool`` route through that dict in every
mcp 1.x release memora pins (``mcp>=1.0.0,<1.28``; see pyproject.toml),
but a compatible-looking minor bump could keep the dict while moving
listing or dispatch to another registry — the guard would then fail OPEN
silently, reporting ``exposed_tools=12`` while every gated maintenance
tool stayed callable. To catch that at startup, ``apply_tool_profile``
runs ``attest_tool_profile``: it enumerates the ACTUAL ``server.list_tools()``
result and probes a gated name through the ACTUAL ``server.call_tool()``
dispatch path, failing closed if either disagrees with the prune. The
attestation is the runtime backstop; the version pin is the static guard.

SUPPORTED PROFILED PATH
-----------------------
``memora.server.main()`` is the sole supported profiled serving path: it
resolves the profile BEFORE any side effect, prunes, attests, then serves.
A direct embedder that imports ``memora.server.mcp`` and calls ``mcp.run()``
themselves BYPASSES profiling entirely (the global ``mcp`` still holds all
43 tools). Embedders who want profiling must call ``apply_tool_profile``
themselves or use ``main()``. This is deliberately not a server factory:
the attestation must run in the same process that serves, so a factory
that returned a pre-built server would just move the obligation, not remove it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger("memora.tool_profile")

# agent (12): read/create surface a worker agent needs.
AGENT_TOOLS: frozenset[str] = frozenset({
    "memory_absorb",
    "memory_semantic_search",
    "memory_hybrid_search",
    "memory_list_compact",
    "memory_get",
    "memory_related",
    "memory_link",
    "memory_stats",
    "memory_create",
    "memory_create_issue",
    "memory_create_todo",
    "memory_update",
})

# leader (18): agent set plus section/document/tag/delete/digest.
LEADER_TOOLS: frozenset[str] = AGENT_TOOLS | frozenset({
    "memory_create_section",
    "memory_store_document",
    "memory_get_document",
    "memory_tags",
    "memory_delete",
    "memory_digest",
})

# `full` is NOT a fixed list — it is every tool actually registered on the
# server at apply time. Adding a new ``@mcp.tool()`` therefore exposes it
# under ``full`` automatically; reduced profiles opt in explicitly.

VALID_PROFILES = ("full", "leader", "agent")


class ToolProfileError(ValueError):
    """Raised when ``MEMORA_TOOL_PROFILE`` is set to an unknown value, when
    the server lacks a prunable tool registry, or when the startup
    attestation finds the installed MCP SDK does not route listing/dispatch
    through the pruned registry (private-implementation drift)."""


def resolve_tool_profile(value: Optional[str] = None) -> str:
    """Return the active profile name.

    ``value=None`` reads ``MEMORA_TOOL_PROFILE`` from the environment.
    Unset (or empty) -> ``"full"``: every existing direct-stdio deployment
    is byte-for-byte unchanged. A known value (``"full"``/``"leader"``/
    ``"agent"``) is returned verbatim. An UNKNOWN value raises
    ``ToolProfileError`` naming the valid values — it NEVER silently falls
    back to ``full``. A typo that silently re-exposes
    ``memory_rebuild_embeddings`` / ``memory_delete_batch`` to every worker
    is precisely the failure this feature exists to prevent; fail closed.
    """
    if value is None:
        value = os.environ.get("MEMORA_TOOL_PROFILE")
    if value is None or value == "":
        return "full"
    if value in VALID_PROFILES:
        return value
    raise ToolProfileError(
        f"unknown MEMORA_TOOL_PROFILE={value!r}; valid values: "
        f"{', '.join(VALID_PROFILES)}"
    )


def profile_tool_names(profile: str, registered: Any) -> frozenset[str]:
    """Return the set of tool names the profile allows, given the names
    actually registered on the server.

    ``full`` -> every registered name (so the count tracks reality, not a
    hand-maintained constant). ``leader``/``agent`` -> their fixed sets
    intersected with the registered names, so a name in the set that no
    ``@mcp.tool()`` ever defined cannot keep a ghost entry (and a real
    tool misspelled out of the set is pruned — fail-safe: under-expose,
    never over-expose).
    """
    registered_names = frozenset(registered)
    if profile == "full":
        return registered_names
    if profile == "leader":
        return LEADER_TOOLS & registered_names
    if profile == "agent":
        return AGENT_TOOLS & registered_names
    # Unreachable: resolve_tool_profile guards the valid set.
    raise ToolProfileError(f"unknown profile {profile!r}")


async def _attest_tool_profile(
    server: Any,
    allowed: frozenset[str],
    gated_probe: Optional[str],
) -> None:
    """Runtime backstop against private-implementation drift in the MCP SDK.

    Goes through the ACTUAL protocol path — ``server.list_tools()`` (the
    ``tools/list`` handler) and ``server.call_tool()`` (the ``call_tool``
    handler) — NOT the private ``_tool_manager._tools`` dict, and confirms
    both agree with the prune. If a future mcp release routes listing or
    dispatch elsewhere while keeping the dict, this raises and the server
    refuses to start (fail closed) rather than reporting a misleading
    ``exposed_tools=`` count.

    DISPATCH PROBE SAFETY (P1b): the gated-name probe must NEVER execute a
    real tool, even under drift. The probe is chosen BEFORE pruning from the
    gated tools that have at least one REQUIRED argument (see
    ``apply_tool_profile``), so ``call_tool(probe, {})`` fails at pydantic
    argument VALIDATION (missing required arg) BEFORE the tool function body
    runs — it raises a validation ``ToolError``, not an execution. Under
    drift (dispatch intact), this validation error does NOT match the
    unknown-tool signal, so the attestation fails correctly without ever
    having executed the tool. If no gated tool has required args, the
    gated-name probe is skipped (the listing + unknown-name probe still
    guard the path); a log note is emitted.

    UNKNOWN-TOOL DISCRIMINATOR (P1): FastMCP raises ``ToolError`` for BOTH
    an unknown tool (``"Unknown tool: <name>"``) AND a tool that exists but
    fails validation/execution (``"Error executing tool <name>: ..."``).
    Accepting any ``ToolError`` as "gated" is vacuous: under drift, a gated
    tool with required args raises a VALIDATION ``ToolError`` on empty args
    and the attestation would wrongly pass. We match the ``"Unknown tool"``
    signal specifically (the exact prefix from
    ``ToolManager.call_tool``); anything else — including a validation
    error — FAILS the attestation, because it does not prove the tool is
    gated.
    """
    listed = {t.name for t in await server.list_tools()}
    if listed != allowed:
        raise ToolProfileError(
            "profile attestation failed: tools/list returned "
            f"{sorted(listed)} but profile allows {sorted(allowed)}; "
            "the installed MCP SDK does not route tools/list through the "
            "pruned _tool_manager._tools registry — pin mcp to a "
            "compatible release (see pyproject.toml)"
        )
    # Establish the unknown-tool signal via a name that cannot exist in any
    # registry. This probe can NEVER execute (no tool by that name), so it
    # is always safe.
    sentinel = "__memora_tool_profile_not_a_tool__"
    sentinel_error = await _capture_dispatch_error(server, sentinel)
    if sentinel_error is None or not _is_unknown_tool_signal(sentinel_error, sentinel):
        raise ToolProfileError(
            "profile attestation failed: call_tool("
            f"{sentinel!r}) did not raise the expected unknown-tool "
            f"signal (got {sentinel_error!r}); the installed MCP SDK's "
            "dispatch path does not reject absent names as expected"
        )
    # Probe a gated name through the ACTUAL dispatch path. Under the prune
    # (no drift), this raises the SAME unknown-tool signal at the get_tool
    # lookup — before tool.run — so nothing is dispatched. Under drift
    # (dispatch intact), the gated tool is found and either executes (no
    # required args — avoided by probe choice) or raises a validation error
    # (required args — the chosen probe shape); either way the signal
    # differs from unknown-tool and the attestation fails.
    if gated_probe is not None:
        probe_error = await _capture_dispatch_error(server, gated_probe)
        if probe_error is None or not _is_unknown_tool_signal(probe_error, gated_probe):
            raise ToolProfileError(
                f"profile attestation failed: gated tool {gated_probe!r} is "
                f"still dispatchable via call_tool (got {probe_error!r}); the "
                "installed MCP SDK does not route dispatch through the pruned "
                "_tool_manager._tools registry"
            )
    else:
        logger.info(
            "tool_profile attestation: no gated tool with required args found; "
            "gated-name dispatch probe skipped (listing + unknown-name probe "
            "still guard the path)"
        )


async def _capture_dispatch_error(server: Any, name: str) -> Optional[Any]:
    """Call ``server.call_tool(name, {})`` and return the ToolError it
    raises, or None if it did not raise (the tool executed / returned)."""
    try:
        await server.call_tool(name, {})
    except _ToolError as e:
        return e
    return None


def _is_unknown_tool_signal(err: Any, expected_name: str) -> bool:
    """True if ``err`` is FastMCP's unknown-tool signal — the exact message
    raised by ``ToolManager.call_tool`` for a name missing from ``_tools``:
    ``"Unknown tool: <name>"``. A validation/execution error raises a
    DIFFERENT ``ToolError`` (``"Error executing tool <name>: ..."``) and
    must NOT be accepted as evidence of gating.
    """
    msg = str(err)
    return msg.startswith("Unknown tool:") and expected_name in msg


def _choose_gated_probe(
    tools: dict[str, Any],
    allowed: frozenset[str],
) -> Optional[str]:
    """Pick a gated tool name that has at least one REQUIRED argument, so
    ``call_tool(name, {})`` fails at pydantic validation (missing required
    arg) BEFORE the tool function body runs — guaranteeing the probe never
    executes even under drift (P1b). Returns None if no gated tool has
    required args (the gated-name probe is then skipped)."""
    for name, tool in tools.items():
        if name in allowed:
            continue
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict) and params.get("required"):
            return name
    return None


def apply_tool_profile(server: Any, profile: Optional[str] = None) -> int:
    """Prune the server's registered tools down to the active profile.

    Removes gated tools from ``server._tool_manager._tools`` so they are
    absent from BOTH ``tools/list`` and ``call_tool`` dispatch, then runs
    ``_attest_tool_profile`` through the ACTUAL public listing/dispatch
    path to confirm the installed SDK honours the prune (fails closed on
    private-implementation drift). Returns the number of tools remaining.
    Logs the active profile and exposed tool count at startup so a running
    deployment is self-describing.
    """
    resolved = resolve_tool_profile(profile)
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    if not isinstance(tools, dict):
        raise ToolProfileError(
            "server has no _tool_manager._tools dict to prune"
        )
    registered = list(tools.keys())
    allowed = profile_tool_names(resolved, registered)
    # Choose the gated dispatch probe BEFORE pruning — after prune the tool
    # objects (and their schemas) are gone. The probe must have required
    # args so the empty-args probe fails at validation, not execution.
    gated_probe = _choose_gated_probe(tools, allowed)
    for name in registered:
        if name not in allowed:
            del tools[name]
    exposed = len(tools)
    # Runtime backstop: confirm the public protocol path honours the prune.
    # asyncio.run is safe here — main() calls this before mcp.run(), so no
    # event loop is running yet.
    asyncio.run(_attest_tool_profile(server, allowed, gated_probe))
    _log_startup(resolved, exposed, len(registered))
    return exposed


def _log_startup(profile: str, exposed: int, registered: int) -> None:
    suffix = f" (of {registered} registered)" if profile != "full" else ""
    msg = f"MEMORA_TOOL_PROFILE={profile} exposed_tools={exposed}{suffix}"
    print(msg, file=sys.stderr)
    logger.info(msg)


# Imported lazily so the module imports cleanly even if the MCP SDK's
# exception path moves; the attestation needs it but module import does not.
def _get_tool_error() -> type:
    from mcp.server.fastmcp.exceptions import ToolError
    return ToolError


_ToolError = _get_tool_error()
