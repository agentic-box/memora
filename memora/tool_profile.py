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
    registered: list[str],
) -> None:
    """Runtime backstop against private-implementation drift in the MCP SDK.

    Goes through the ACTUAL protocol path — ``server.list_tools()`` (the
    ``tools/list`` handler) and ``server.call_tool()`` (the ``call_tool``
    handler) — NOT the private ``_tool_manager._tools`` dict, and confirms
    both agree with the prune. If a future mcp release routes listing or
    dispatch elsewhere while keeping the dict, this raises and the server
    refuses to start (fail closed) rather than reporting a misleading
    ``exposed_tools=`` count.
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
    gated = [n for n in registered if n not in allowed]
    if gated:
        probe = gated[0]
        try:
            await server.call_tool(probe, {})
        except _ToolError:
            pass  # good — gated name is genuinely undispatchable
        else:
            raise ToolProfileError(
                f"profile attestation failed: gated tool {probe!r} is still "
                "dispatchable via call_tool; the installed MCP SDK does not "
                "route dispatch through the pruned _tool_manager._tools "
                "registry"
            )


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
    for name in registered:
        if name not in allowed:
            del tools[name]
    exposed = len(tools)
    # Runtime backstop: confirm the public protocol path honours the prune.
    # asyncio.run is safe here — main() calls this before mcp.run(), so no
    # event loop is running yet.
    asyncio.run(_attest_tool_profile(server, allowed, registered))
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
