"""Tests for MEMORA_TOOL_PROFILE (memora issue #981).

These tests use the REAL ``memora.server.mcp`` server, on which all 43
``@mcp.tool()`` decorators have registered their tools at import time.
Every "tool X is absent under profile agent/leader" assertion is paired
with the SAME tool being present under ``full`` — so the assertion flips
when the profile flips and is not vacuously satisfied by a fixture that
never registered X in the first place.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import memora.server as server
from memora.tool_profile import (
    AGENT_TOOLS,
    LEADER_TOOLS,
    ToolProfileError,
    apply_tool_profile,
    profile_tool_names,
    resolve_tool_profile,
)


@pytest.fixture(autouse=True)
def _restore_tools():
    """apply_tool_profile MUTATES the global ``mcp._tool_manager._tools``
    dict (prunes gated tools). Snapshot before each test and restore after
    so tests are independent and the global server is left with all 43."""
    mgr = server.mcp._tool_manager
    snapshot = dict(mgr._tools)
    yield
    mgr._tools.clear()
    mgr._tools.update(snapshot)


@pytest.fixture
def _clean_env(monkeypatch):
    """Ensure MEMORA_TOOL_PROFILE is unset for tests that rely on the
    default, and provide a helper to set it."""
    monkeypatch.delenv("MEMORA_TOOL_PROFILE", raising=False)
    return monkeypatch


def _tool_names() -> set[str]:
    return set(server.mcp._tool_manager._tools.keys())


def _count() -> int:
    return len(server.mcp._tool_manager._tools)


# A destructive maintenance tool that MUST be gated out of every reduced
# profile. Its absence under agent/leader is the whole point of #981.
GATED_TOOL = "memory_rebuild_embeddings"
# A tool that lives in leader but NOT agent — proves the leader/agent
# boundary, not just "reduced vs full".
LEADER_ONLY_TOOL = "memory_digest"
# A tool in the agent set — must SURVIVE pruning under agent.
AGENT_TOOL = "memory_absorb"


class TestProfileCounts:
    def test_full_exposes_all_43(self, _clean_env):
        n = apply_tool_profile(server.mcp, "full")
        assert n == 43, f"full must expose all 43 registered tools, got {n}"
        assert _count() == 43

    def test_leader_exposes_exactly_18(self, _clean_env):
        n = apply_tool_profile(server.mcp, "leader")
        assert n == 18, f"leader must expose exactly 18 tools, got {n}"
        assert _count() == 18

    def test_agent_exposes_exactly_12(self, _clean_env):
        n = apply_tool_profile(server.mcp, "agent")
        assert n == 12, f"agent must expose exactly 12 tools, got {n}"
        assert _count() == 12

    def test_default_env_unset_is_full(self, _clean_env):
        # No env, no explicit profile -> "full" (the default; existing
        # direct-stdio deployments are byte-for-byte unchanged).
        assert resolve_tool_profile() == "full"
        n = apply_tool_profile(server.mcp)  # reads env (unset) -> full
        assert n == 43, f"unset env must default to full (43), got {n}"


class TestGatedToolGenuinelyAbsent:
    """A gated-out tool must be missing from tools/list AND undispatchable.
    Each absent-under-reduced assertion is paired with present-under-full
    so the test would FAIL under profile=full (non-vacuous)."""

    def test_gated_tool_is_present_under_full(self, _clean_env):
        # The flip: under full the gated tool MUST be present. If this
        # assertion held while the "absent under agent" one also held
        # vacuously (fixture never registered the tool), both would pass
        # — so we assert present-under-full explicitly to break that.
        apply_tool_profile(server.mcp, "full")
        assert GATED_TOOL in _tool_names(), (
            f"{GATED_TOOL} must be registered under full (else the "
            "absent-under-agent assertion is vacuous)"
        )
        assert server.mcp._tool_manager.get_tool(GATED_TOOL) is not None

    def test_gated_tool_absent_from_listing_under_agent(self, _clean_env):
        apply_tool_profile(server.mcp, "agent")
        assert GATED_TOOL not in _tool_names(), (
            f"{GATED_TOOL} must be absent from tools/list under agent"
        )

    def test_gated_tool_undispatchable_under_agent(self, _clean_env):
        apply_tool_profile(server.mcp, "agent")
        with pytest.raises(ToolError, match=f"Unknown tool: {GATED_TOOL}"):
            asyncio.run(
                server.mcp._tool_manager.call_tool(GATED_TOOL, {})
            )

    def test_gated_tool_absent_from_listing_under_leader(self, _clean_env):
        apply_tool_profile(server.mcp, "leader")
        assert GATED_TOOL not in _tool_names(), (
            f"{GATED_TOOL} must be absent from tools/list under leader"
        )

    def test_gated_tool_undispatchable_under_leader(self, _clean_env):
        apply_tool_profile(server.mcp, "leader")
        with pytest.raises(ToolError, match=f"Unknown tool: {GATED_TOOL}"):
            asyncio.run(
                server.mcp._tool_manager.call_tool(GATED_TOOL, {})
            )


class TestLeaderAgentBoundary:
    """The leader/agent boundary is not just "reduced vs full": a tool in
    leader but not agent must survive leader pruning and be gated under
    agent; an agent tool must survive both."""

    def test_leader_only_tool_present_under_leader(self, _clean_env):
        apply_tool_profile(server.mcp, "leader")
        assert LEADER_ONLY_TOOL in _tool_names(), (
            f"{LEADER_ONLY_TOOL} must be present under leader"
        )

    def test_leader_only_tool_absent_under_agent(self, _clean_env):
        apply_tool_profile(server.mcp, "agent")
        assert LEADER_ONLY_TOOL not in _tool_names(), (
            f"{LEADER_ONLY_TOOL} must be gated out under agent"
        )
        with pytest.raises(ToolError, match=f"Unknown tool: {LEADER_ONLY_TOOL}"):
            asyncio.run(
                server.mcp._tool_manager.call_tool(LEADER_ONLY_TOOL, {})
            )

    def test_agent_tool_survives_agent_profile(self, _clean_env):
        apply_tool_profile(server.mcp, "agent")
        assert AGENT_TOOL in _tool_names(), (
            f"{AGENT_TOOL} must survive pruning under agent"
        )
        assert server.mcp._tool_manager.get_tool(AGENT_TOOL) is not None

    def test_memory_list_excluded_from_reduced_profiles(self, _clean_env):
        # memory_list is deliberately excluded (#973: 163-174s vs
        # memory_list_compact's 0.22s). Assert it is NOT in either set.
        assert "memory_list" not in AGENT_TOOLS
        assert "memory_list" not in LEADER_TOOLS
        apply_tool_profile(server.mcp, "leader")
        assert "memory_list" not in _tool_names()
        apply_tool_profile(server.mcp, "agent")
        assert "memory_list" not in _tool_names()


class TestInvalidProfileFailsClosed:
    def test_unknown_value_raises_naming_valid_values(self, _clean_env):
        with pytest.raises(ToolProfileError) as exc:
            resolve_tool_profile("bogus")
        msg = str(exc.value)
        assert "bogus" in msg
        for v in ("full", "leader", "agent"):
            assert v in msg, f"error message must name valid value {v!r}: {msg!r}"

    def test_unknown_value_does_not_silently_fall_back_to_full(self, _clean_env):
        # The core fail-closed contract: a typo must not become "full".
        with pytest.raises(ToolProfileError):
            resolve_tool_profile("ful")  # near-miss typo of "full"

    def test_main_refuses_to_start_on_unknown_profile(self, _clean_env, capsys):
        # main() must exit non-zero before mcp.run() on an unknown value,
        # with a message naming the valid values. --no-graph avoids
        # spawning the graph server; the invalid profile short-circuits
        # before mcp.run() so this never blocks.
        _clean_env.setenv("MEMORA_TOOL_PROFILE", "bogus")
        with pytest.raises(SystemExit) as exc:
            server.main(["--no-graph"])
        assert exc.value.code == 2, f"expected exit code 2, got {exc.value.code}"
        err = capsys.readouterr().err
        assert "MEMORA_TOOL_PROFILE" in err
        for v in ("full", "leader", "agent"):
            assert v in err, f"stderr must name valid value {v!r}: {err!r}"


class TestProfileMembershipIsData:
    """The profile sets are DATA: leader is agent + 6 explicit additions;
    profile_tool_names(full) tracks the actually-registered tools."""

    def test_leader_is_agent_plus_six_additions(self):
        assert LEADER_TOOLS - AGENT_TOOLS == frozenset({
            "memory_create_section",
            "memory_store_document",
            "memory_get_document",
            "memory_tags",
            "memory_delete",
            "memory_digest",
        })

    def test_full_tracks_registered_not_a_constant(self, _clean_env):
        registered = _tool_names()
        assert profile_tool_names("full", registered) == frozenset(registered)

    def test_agent_intersected_with_registered(self, _clean_env):
        # A name in AGENT_TOOLS that no @mcp.tool() defined must not keep a
        # ghost entry; the result is the intersection with registered.
        registered = _tool_names()
        assert profile_tool_names("agent", registered) == (AGENT_TOOLS & frozenset(registered))
