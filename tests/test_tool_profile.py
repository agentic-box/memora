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


class TestAttestationUsesLowlevelHandlers:
    """The startup attestation exercises the LOW-LEVEL registered MCP request
    handlers (server._mcp_server.request_handlers[ListToolsRequest] /
    [CallToolRequest]) — the actual dispatch callable real client requests
    use — NOT the FastMCP.list_tools / FastMCP.call_tool Python helpers.
    In mcp 1.27.0 those helpers are what _setup_handlers registers, so the
    paths coincide; a future SDK could register a different callable while
    leaving the helpers intact. These tests prove the attestation goes
    through the lowlevel handlers (monkeypatching the helpers must NOT
    affect the attestation) and fails closed on drift."""

    def test_faking_fastmcp_helpers_does_not_affect_attestation(self, _clean_env, monkeypatch):
        # NON-VACUITY for the lowlevel path: if the attestation used the
        # FastMCP helpers, faking them to lie would flip the result. It
        # does NOT — the attestation reads the lowlevel handlers, so faking
        # the helpers is inert. This proves the attestation is NOT routed
        # through the helpers.
        snap = dict(server.mcp._tool_manager._tools)

        async def fake_list_tools(self_inner):
            from mcp.types import Tool as MCPTool
            return [MCPTool(name=n, inputSchema={}) for n in snap.keys()]

        async def fake_call_tool(self_inner, name, arguments):
            raise ToolError(f"Unknown tool: {name}")

        monkeypatch.setattr(type(server.mcp), "list_tools", fake_list_tools, raising=True)
        monkeypatch.setattr(type(server.mcp), "call_tool", fake_call_tool, raising=True)
        # With the helpers faked to lie, the attestation still PASSES
        # (because it does not use them) under the real installed SDK:
        assert apply_tool_profile(server.mcp, "agent") == 12
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)

    def test_listing_drift_fails_closed(self, _clean_env, monkeypatch):
        # Drift via the LOWLEVEL handler: the registered ListToolsRequest
        # handler ignores the prune and returns all 43 names. The
        # attestation must catch the mismatch and raise.
        snap = dict(server.mcp._tool_manager._tools)
        all_names = frozenset(snap.keys())

        async def fake_list_handler(req):
            from mcp.types import ListToolsResult, ServerResult
            from mcp.types import Tool as MCPTool
            return ServerResult(ListToolsResult(
                tools=[MCPTool(name=n, inputSchema={}) for n in all_names]
            ))

        from mcp.types import ListToolsRequest
        monkeypatch.setitem(
            server.mcp._mcp_server.request_handlers,
            ListToolsRequest,
            fake_list_handler,
        )
        with pytest.raises(ToolProfileError, match="tools/list"):
            apply_tool_profile(server.mcp, "agent")
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)

    def test_dispatch_drift_fails_closed(self, _clean_env, monkeypatch):
        # REAL drift shape: the lowlevel ListToolsRequest handler reflects
        # the prune (returns only allowed), but the lowlevel CallToolRequest
        # handler is LEFT INTACT (all 43 dispatchable). This is the exact
        # SDK-drift scenario — a future SDK that routes listing and dispatch
        # to different callables. Confirmed red at a45668a (helper-based
        # attestation did not catch this); GREEN after the lowlevel fix.
        import asyncio

        from mcp.types import ListToolsRequest, ListToolsResult, ServerResult
        from mcp.types import Tool as MCPTool

        from memora.tool_profile import (
            _attest_tool_profile,
            _choose_gated_probe,
            profile_tool_names,
        )

        snap = dict(server.mcp._tool_manager._tools)
        allowed = profile_tool_names("agent", list(snap.keys()))
        gated_probe = _choose_gated_probe(snap, allowed)

        # Fake the LOWLEVEL list handler to reflect the prune; leave the
        # lowlevel call handler INTACT (dispatch through the real unpruned
        # _tool_manager with all 43 tools).
        async def fake_list_handler(req):
            return ServerResult(ListToolsResult(
                tools=[MCPTool(name=n, inputSchema={}) for n in allowed]
            ))

        monkeypatch.setitem(
            server.mcp._mcp_server.request_handlers,
            ListToolsRequest,
            fake_list_handler,
        )
        # Do NOT prune _tools — leave all 43 dispatchable (drift).
        with pytest.raises(ToolProfileError, match="still dispatchable"):
            asyncio.run(_attest_tool_profile(server.mcp, "agent", allowed, gated_probe))
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)

    def test_attestation_rejects_non_unknown_error_as_gated(self, _clean_env, monkeypatch):
        # The discriminator: a gated tool that returns a NON-unknown-tool
        # error (e.g. validation) must FAIL the attestation, not be accepted
        # as "gated." Goes through the lowlevel call handler.
        from mcp.types import CallToolRequest, CallToolResult, ServerResult, TextContent
        snap = dict(server.mcp._tool_manager._tools)

        async def fake_call_handler(req):
            name = req.params.name
            if name.startswith("__memora_tool_profile_not_a_tool"):
                return ServerResult(CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                    isError=True,
                ))
            # Drift: gated name is found, raises a VALIDATION error (not unknown-tool).
            return ServerResult(CallToolResult(
                content=[TextContent(type="text", text=f"Error executing tool {name}: 1 validation error")],
                isError=True,
            ))

        monkeypatch.setitem(
            server.mcp._mcp_server.request_handlers,
            CallToolRequest,
            fake_call_handler,
        )
        with pytest.raises(ToolProfileError, match="still dispatchable"):
            apply_tool_profile(server.mcp, "agent")
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)

    def test_exact_match_not_substring(self, _clean_env, monkeypatch):
        # Item 4: "Unknown tool: wrapper-for-memory_create_section" must NOT
        # be accepted as gating "memory_create_section" — exact equality,
        # not startswith + substring.
        from mcp.types import CallToolRequest, CallToolResult, ServerResult, TextContent
        snap = dict(server.mcp._tool_manager._tools)

        async def fake_call_handler(req):
            name = req.params.name
            if name.startswith("__memora_tool_profile_not_a_tool"):
                return ServerResult(CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                    isError=True,
                ))
            # A message that contains the gated name but is NOT the exact
            # unknown-tool signal — prefix/substring match would wrongly accept.
            return ServerResult(CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: wrapper-for-{name}")],
                isError=True,
            ))

        monkeypatch.setitem(
            server.mcp._mcp_server.request_handlers,
            CallToolRequest,
            fake_call_handler,
        )
        with pytest.raises(ToolProfileError, match="still dispatchable"):
            apply_tool_profile(server.mcp, "agent")
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)

    def test_gated_probe_has_required_args_so_cannot_execute(self, _clean_env):
        # P1b: the gated dispatch probe must have required args so
        # call_tool(probe, {}) fails at argument VALIDATION (missing required
        # arg) BEFORE the tool body runs — never executing the tool, even
        # under drift. Confirm the chosen probe has a non-empty `required`
        # list in its schema.
        from memora.tool_profile import _choose_gated_probe, profile_tool_names
        snap = dict(server.mcp._tool_manager._tools)
        allowed = profile_tool_names("agent", list(snap.keys()))
        probe = _choose_gated_probe(snap, allowed)
        assert probe is not None, "agent profile must gate out at least one tool with required args"
        params = snap[probe].parameters
        assert isinstance(params, dict) and params.get("required"), (
            f"gated probe {probe!r} must have required args so the empty-args "
            "probe fails at validation, not execution"
        )
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)

    def test_choose_gated_probe_skips_no_required_args_tools(self):
        # Non-vacuous P1b test: if the FIRST gated tool has NO required args
        # (e.g. memory_list — would execute on empty args, 163s hang), the
        # chooser MUST skip it and return a gated tool WITH required args.
        # A mutation that returns gated[0] unconditionally (the a45668a P1b
        # bug) would return the no-args tool here and this test fails.
        from memora.tool_profile import _choose_gated_probe

        class FakeTool:
            def __init__(self, required):
                self.parameters = {"required": required} if required else {}

        # Order matters: first gated tool has NO required args, second does.
        fake_tools = {
            "memory_list": FakeTool(required=[]),  # no required args — would execute
            "memory_create_section": FakeTool(required=["content"]),  # safe
        }
        allowed = frozenset()  # both are gated
        probe = _choose_gated_probe(fake_tools, allowed)
        assert probe == "memory_create_section", (
            f"_choose_gated_probe must skip no-required-args tools and return "
            f"one with required args; got {probe!r}"
        )

    def test_attestation_passes_on_compatible_sdk(self, _clean_env):
        # Sanity: with the REAL installed SDK (lowlevel handlers honour the
        # prune), apply_tool_profile does NOT raise under any profile.
        snap = dict(server.mcp._tool_manager._tools)
        assert apply_tool_profile(server.mcp, "agent") == 12
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)
        assert apply_tool_profile(server.mcp, "leader") == 18
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)
        assert apply_tool_profile(server.mcp, "full") == 43
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)


class TestNoSafeProbeFailsClosed:
    """Item 3: a reduced profile with gated tools but NO gated tool carrying
    schema.required cannot establish a non-executing dispatch probe — the
    dispatch backstop is silently skipped, reintroducing the fail-open class.
    apply_tool_profile MUST raise ToolProfileError in that case. `full` is
    the only legitimate no-gated-probe case (no gated tools exist)."""

    def test_reduced_profile_with_no_required_arg_gated_tools_raises(self, _clean_env, monkeypatch):
        # Construct a server whose gated tools all have NO required args.
        # We monkeypatch _tools with a fake set where the only gated tool
        # has no required args, then call apply_tool_profile.
        from memora.tool_profile import ToolProfileError

        class FakeTool:
            def __init__(self, required):
                self.parameters = {"required": required} if required else {}

        snap = dict(server.mcp._tool_manager._tools)
        # Allowed = agent set (has required-arg tools, all KEPT); gated = a
        # single no-required-arg tool. We replace _tools entirely.
        fake_tools = {
            "memory_absorb": FakeTool(required=["content"]),  # in agent set, kept
            "memory_list": FakeTool(required=[]),  # gated, NO required args
        }
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(fake_tools)
        try:
            with pytest.raises(ToolProfileError, match="no non-executing dispatch probe"):
                apply_tool_profile(server.mcp, "agent")
        finally:
            server.mcp._tool_manager._tools.clear()
            server.mcp._tool_manager._tools.update(snap)

    def test_full_profile_with_no_gated_tools_does_not_raise(self, _clean_env):
        # `full` is the only legitimate no-gated-probe case: no gated tools
        # exist, so no probe is needed. apply_tool_profile must NOT raise.
        snap = dict(server.mcp._tool_manager._tools)
        assert apply_tool_profile(server.mcp, "full") == 43  # all kept, no gated
        server.mcp._tool_manager._tools.clear()
        server.mcp._tool_manager._tools.update(snap)


class TestInvalidProfileAbortsBeforeSideEffects:
    """An invalid MEMORA_TOOL_PROFILE must abort BEFORE any side effect:
    no DB connect/prewarm, no graph server thread, no mcp.run. The earlier
    --no-graph test MASKED the ordering bug (it skipped start_graph_server
    unconditionally); these tests run WITHOUT --no-graph and assert the
    side-effect functions are never reached for an invalid value."""

    def test_invalid_profile_aborts_before_connect_graph_and_run(
        self, _clean_env, monkeypatch
    ):
        calls: list[str] = []

        # Monkeypatch the three side-effect entry points in server's
        # namespace. connect is `from .storage import connect` -> server.connect.
        def fake_connect():
            calls.append("connect")
            raise AssertionError("connect must not run for an invalid profile")

        def fake_start_graph_server(host, port):
            calls.append("start_graph_server")

        def fake_run(transport=None):
            calls.append("mcp.run")

        monkeypatch.setattr(server, "connect", fake_connect)
        monkeypatch.setattr(server, "start_graph_server", fake_start_graph_server)
        monkeypatch.setattr(server.mcp, "run", fake_run)
        _clean_env.setenv("MEMORA_TOOL_PROFILE", "bogus")

        # NOT passing --no-graph: graph startup is the default path.
        with pytest.raises(SystemExit) as exc:
            server.main(["--host", "127.0.0.1", "--port", "0"])
        assert exc.value.code == 2, f"expected exit 2, got {exc.value.code}"
        assert calls == [], (
            f"invalid profile must abort before side effects, but ran: {calls}"
        )

    def test_valid_profile_still_runs_side_effects(self, _clean_env, monkeypatch):
        # Contrast: a VALID profile must still reach connect (prewarm) and
        # would reach mcp.run — the early validation must not short-circuit
        # the happy path. We stub mcp.run to avoid actually serving.
        calls: list[str] = []

        class _FakeConn:
            def close(self):
                calls.append("close")

        def fake_connect():
            calls.append("connect")
            return _FakeConn()

        def fake_start_graph_server(host, port):
            calls.append("start_graph_server")

        def fake_run(transport=None):
            calls.append("mcp.run")

        monkeypatch.setattr(server, "connect", fake_connect)
        monkeypatch.setattr(server, "start_graph_server", fake_start_graph_server)
        monkeypatch.setattr(server.mcp, "run", fake_run)
        _clean_env.setenv("MEMORA_TOOL_PROFILE", "agent")

        server.main(["--host", "127.0.0.1", "--port", "0"])
        assert "connect" in calls, "valid profile must still prewarm the DB"
        assert "mcp.run" in calls, "valid profile must still serve"
