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

``memory_list`` is in ``leader`` but not ``agent``. It was excluded from both
while it cost 163-174s on a D1 store against ``memory_list_compact``'s 0.22s;
#973 fixed that (now ~1.1s) and it returned to ``leader``. It stays out of
``agent`` because a worker's read surface is deliberately narrow, not because
of speed.

PRIVATE-IMPLEMENTATION COMPATIBILITY (load-bearing, not cosmetic)
------------------------------------------------------------------
The prune deletes entries from FastMCP's PRIVATE ``_tool_manager._tools``
dict. ``mcp`` is pinned to the audited minor (``mcp>=1.27,<1.28``) in
pyproject.toml — the range that was actually verified, not 27 untested
minor lines. The pin is the static guard.

The runtime backstop is the startup **attestation**, which exercises the
LOW-LEVEL registered MCP request handlers
(``server._mcp_server.request_handlers[ListToolsRequest]`` and
``...[CallToolRequest]``) — the actual dispatch callable the server
registers for real MCP requests — NOT the ``FastMCP.list_tools`` /
``FastMCP.call_tool`` Python helper methods. In mcp 1.27.0 those helpers
happen to be what ``_setup_handlers`` registers, so the paths coincide
TODAY, but a future release could register a different dispatch callable
while leaving the helpers intact. The lowlevel handler is what real client
requests hit, so attesting through it catches that drift; the pin makes
the attestation a backstop rather than relying on unverified internals.

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
    # Restored to `leader` once #973 landed: memory_list was excluded purely
    # because it cost 163-174s on a D1 store against memory_list_compact's
    # 0.22s. It is now ~1.1s. It stays OUT of `agent` -- not for speed, but
    # because a worker's read surface is deliberately narrow and
    # memory_list_compact covers it.
    "memory_list",
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


# The unknown-tool signal FastMCP's ToolManager raises for a name missing
# from _tools (ToolManager.call_tool, tool_manager.py:91). The lowlevel
# call_tool handler catches that and returns a CallToolResult(isError=True)
# whose first TextContent.text is exactly this string. Exact equality, not
# a prefix/substring match: "Unknown tool: wrapper-for-memory_create_section"
# must NOT be accepted as gating "memory_create_section".
_UNKNOWN_TOOL_PREFIX = "Unknown tool: "


async def _attest_tool_profile(
    server: Any,
    profile: str,
    allowed: frozenset[str],
    gated_probe: Optional[str],
) -> None:
    """Runtime backstop against private-implementation drift in the MCP SDK.

    Exercises the LOW-LEVEL registered MCP request handlers — the actual
    dispatch callables the server registers for real client requests
    (``server._mcp_server.request_handlers[ListToolsRequest]`` and
    ``...[CallToolRequest]``) — NOT the ``FastMCP.list_tools`` /
    ``FastMCP.call_tool`` Python helper methods. The lowlevel handler is
    what real MCP requests hit, so attesting through it catches a future
    SDK that registers a different dispatch callable while leaving the
    helpers intact. If listing or dispatch disagrees with the prune, this
    raises and the server refuses to start (fail closed).

    DISPATCH PROBE SAFETY (P1b): the gated-name probe must NEVER execute a
    real tool, even under drift. The probe is chosen BEFORE pruning from the
    gated tools that have at least one REQUIRED argument (see
    ``apply_tool_profile``), so the empty-args probe fails at argument
    VALIDATION (missing required arg) BEFORE the tool function body runs.
    Under drift (dispatch intact), the probe is found and either executes
    (no required args — avoided by probe choice) or raises a validation
    error (required args — the chosen shape); either way the lowlevel
    handler returns a result whose error text is NOT the exact unknown-tool
    signal, so the attestation fails without having executed the probe.

    NO-SAFE-PROBE (item 3): a reduced profile (leader/agent) with gated
    tools but NO gated tool carrying schema.required cannot establish a
    non-executing dispatch probe — the only dispatch check is silently
    skipped, reintroducing the fail-open class via a membership/schema edit.
    That MUST raise (fail closed). ``full`` is the only legitimate
    no-gated-probe case (no gated tools exist).
    """
    lowlevel = _get_lowlevel_handlers(server)
    listed = await _lowlevel_list_tool_names(lowlevel)
    if listed != allowed:
        raise ToolProfileError(
            "profile attestation failed: the registered tools/list handler "
            f"returned {sorted(listed)} but profile {profile!r} allows "
            f"{sorted(allowed)}; the installed MCP SDK does not route "
            "tools/list through the pruned _tool_manager._tools registry — "
            "pin mcp to a compatible release (see pyproject.toml)"
        )
    # Sentinel probe: a name we have just confirmed is ABSENT from the
    # observed listing (not a hard-coded string that a future tool could be
    # named, which would then be EXECUTED during full-profile startup).
    # This probe can never execute (no such tool in any registry), so it is
    # always safe.
    sentinel = _choose_sentinel(listed)
    if not _is_unknown_tool(await _lowlevel_call_tool(lowlevel, sentinel, {}), sentinel):
        raise ToolProfileError(
            "profile attestation failed: the registered call_tool handler "
            f"did not return the exact unknown-tool signal for {sentinel!r} "
            "(an absent name); the installed MCP SDK's dispatch path does "
            "not reject absent names as expected"
        )
    # Gated dispatch probe: a real gated name must produce the SAME exact
    # unknown-tool signal (rejected at lookup, before tool.run). Under drift
    # the gated tool is found and either executes or raises a different
    # error — neither matches the unknown-tool signal, so attestation fails.
    if gated_probe is not None:
        gated_result = await _lowlevel_call_tool(lowlevel, gated_probe, {})
        if not _is_unknown_tool(gated_result, gated_probe):
            raise ToolProfileError(
                f"profile attestation failed: gated tool {gated_probe!r} is "
                "still dispatchable through the registered call_tool handler "
                f"(result {gated_result!r}); the installed MCP SDK does not "
                "route dispatch through the pruned _tool_manager._tools "
                "registry"
            )


def _choose_sentinel(listed_names: frozenset[str]) -> str:
    """Return a tool name that is ABSENT from the just-observed listing.

    Not a hard-coded string: a future tool actually named the hard-coded
    sentinel would be EXECUTED during full-profile startup. We confirm
    absence against the listing we just observed."""
    candidate = "__memora_tool_profile_not_a_tool__"
    suffix = 0
    while candidate in listed_names:
        suffix += 1
        candidate = f"__memora_tool_profile_not_a_tool_{suffix}__"
    return candidate


def _is_unknown_tool(call_result: Any, expected_name: str) -> bool:
    """True if the lowlevel call_tool handler returned the EXACT unknown-tool
    signal for ``expected_name``: a ``CallToolResult`` with ``isError=True``
    whose first text content is exactly
    ``"Unknown tool: {expected_name}"``.

    Exact equality, not startswith + substring: today
    ``"Unknown tool: wrapper-for-memory_create_section"`` would be wrongly
    accepted as gating ``memory_create_section`` under a prefix match. Exact
    equality also makes the sentinel/gated pairing compare genuinely
    identical signal shapes.
    """
    expected = f"{_UNKNOWN_TOOL_PREFIX}{expected_name}"
    root = getattr(call_result, "root", call_result)
    is_error = getattr(root, "isError", False)
    content = getattr(root, "content", None) or []
    text = content[0].text if content and hasattr(content[0], "text") else None
    return bool(is_error) and text == expected


async def _lowlevel_list_tool_names(lowlevel: Any) -> frozenset[str]:
    """Invoke the registered ListToolsRequest handler and return the tool
    names it actually reports (the real tools/list response shape)."""
    from mcp.types import ListToolsRequest
    result = await lowlevel.list_tools(ListToolsRequest(method="tools/list"))
    root = getattr(result, "root", result)
    tools = getattr(root, "tools", None) or []
    return frozenset(t.name for t in tools)


async def _lowlevel_call_tool(lowlevel: Any, name: str, arguments: dict) -> Any:
    """Invoke the registered CallToolRequest handler for ``name`` and return
    its raw ServerResult (the real tools/call response shape). The lowlevel
    handler catches exceptions internally and returns a
    CallToolResult(isError=True) rather than raising."""
    from mcp.types import CallToolRequest
    return await lowlevel.call_tool(
        CallToolRequest(method="tools/call", params={"name": name, "arguments": arguments})
    )


class _LowlevelHandlers:
    """Binds the registered lowlevel MCP request handlers from
    ``server._mcp_server.request_handlers``. Captured once at attest time
    so the attestation invokes the SAME dispatch callable real client
    requests use, not the FastMCP Python helpers."""

    def __init__(self, list_tools: Any, call_tool: Any) -> None:
        self.list_tools = list_tools
        self.call_tool = call_tool


def _get_lowlevel_handlers(server: Any) -> _LowlevelHandlers:
    """Fetch the registered lowlevel request handlers from the MCP server.
    Raises ToolProfileError if the structure is absent (incompatible SDK)."""
    mcp_server = getattr(server, "_mcp_server", None)
    handlers = getattr(mcp_server, "request_handlers", None)
    if not isinstance(handlers, dict):
        raise ToolProfileError(
            "server has no _mcp_server.request_handlers dict to attest "
            "through (incompatible MCP SDK)"
        )
    from mcp.types import CallToolRequest, ListToolsRequest
    list_handler = handlers.get(ListToolsRequest)
    call_handler = handlers.get(CallToolRequest)
    if list_handler is None or call_handler is None:
        raise ToolProfileError(
            "the MCP server has not registered lowlevel tools/list or "
            "tools/call handlers (incompatible MCP SDK)"
        )
    return _LowlevelHandlers(list_handler, call_handler)


def _choose_gated_probe(
    tools: dict[str, Any],
    allowed: frozenset[str],
) -> Optional[str]:
    """Pick a gated tool name that has at least one REQUIRED argument, so
    ``call_tool(name, {})`` fails at argument VALIDATION (missing required
    arg) BEFORE the tool function body runs — guaranteeing the probe never
    executes even under drift (P1b). Returns None if no gated tool has
    required args (caller raises for reduced profiles — see apply_tool_profile)."""
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
    ``_attest_tool_profile`` through the LOW-LEVEL registered MCP request
    handlers (the actual dispatch callable real client requests use) to
    confirm the installed SDK honours the prune (fails closed on
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
    # Item 3: a reduced profile with gated tools but no safe (required-args)
    # probe cannot establish the dispatch backstop — fail closed. `full` is
    # the only legitimate no-gated-probe case (no gated tools exist).
    if resolved != "full" and gated_probe is None and len(allowed) < len(registered):
        raise ToolProfileError(
            f"profile {resolved!r} gates {len(registered) - len(allowed)} tool(s) "
            "but none has a required argument, so no non-executing dispatch "
            "probe can be established; add a required-arg tool to the gated "
            "set or use profile 'full'"
        )
    for name in registered:
        if name not in allowed:
            del tools[name]
    exposed = len(tools)
    # Runtime backstop: confirm the lowlevel protocol path honours the prune.
    # asyncio.run is safe here — main() calls this before mcp.run(), so no
    # event loop is running yet.
    asyncio.run(_attest_tool_profile(server, resolved, allowed, gated_probe))
    _log_startup(resolved, exposed, len(registered))
    return exposed


def _log_startup(profile: str, exposed: int, registered: int) -> None:
    suffix = f" (of {registered} registered)" if profile != "full" else ""
    msg = f"MEMORA_TOOL_PROFILE={profile} exposed_tools={exposed}{suffix}"
    print(msg, file=sys.stderr)
    logger.info(msg)
