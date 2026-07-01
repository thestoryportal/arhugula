"""Tests for U-AS-08 — sandbox_tier max() composition (C-AS-02 §2.1-§2.2,§2.5)."""

from __future__ import annotations

from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.forced_tier_resolution import ForcedTierCause
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_composition import (
    AssignedTierReason,
    CallSiteContext,
    SandboxTierCompositionOutcome,
    TaintState,
    sandbox_tier,
)
from harness_as.sandbox_tier_floor import MCPServer, MCPServerTrustLevel, ToolMetadata
from harness_as.tool_contract import ToolContract

_PLAIN_META = ToolMetadata(
    is_deterministic_inhouse=False,
    forces_computer_use=False,
    forces_code_execution=False,
)


class _StubFloors:
    """A stub `FloorInterfaces` returning fixed tiers (acceptance #7 / #-stub)."""

    def __init__(self, mcp_tier: SandboxTier, op_tier: SandboxTier) -> None:
        self._mcp = mcp_tier
        self._op = op_tier

    def mcp_server_trust_tier_floor(self, mcp_server: MCPServer | None) -> SandboxTier:
        return self._mcp

    def operator_policy_floor(self, persona_tier: PersonaTier) -> SandboxTier:
        return self._op


def _tool(minimum_tier: SandboxTier) -> ToolContract:
    return ToolContract(
        name="t",
        description="t",
        input_schema={},
        output_schema={},
        minimum_tier=minimum_tier,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )


def _ctx(
    *,
    blast_radius_tier: BlastRadiusTier = BlastRadiusTier.READ_ONLY,
    mcp_server: MCPServer | None = None,
    computer_use_bound: bool = False,
    code_execution_beta_invoked: bool = False,
) -> CallSiteContext:
    return CallSiteContext(
        taint_state=TaintState.UNTAINTED,
        mcp_server=mcp_server,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        blast_radius_tier=blast_radius_tier,
        mcp_transport=None,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        computer_use_bound=computer_use_bound,
        code_execution_beta_invoked=code_execution_beta_invoked,
    )


def test_sandbox_tier_composition_max_of_five_floors() -> None:
    """Acceptance #1 — the resolved tier is the max() of the five floors."""
    floors = _StubFloors(SandboxTier.TIER_3_MICROVM, SandboxTier.TIER_1_PROCESS)
    result = sandbox_tier(_tool(SandboxTier.TIER_1_PROCESS), _PLAIN_META, _ctx(), floors)
    assert result.outcome is SandboxTierCompositionOutcome.RESOLVED
    assert result.tier is SandboxTier.TIER_3_MICROVM
    assert result.assigned_tier_reason is AssignedTierReason.MCP_SERVER_TRUST_FLOOR


def test_sandbox_tier_composition_forced_tier_precedence() -> None:
    """Acceptance #2 — a computer-use forcing condition wins with a ForcedTierCause."""
    floors = _StubFloors(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_1_PROCESS)
    result = sandbox_tier(
        _tool(SandboxTier.TIER_1_PROCESS),
        _PLAIN_META,
        _ctx(computer_use_bound=True),
        floors,
    )
    assert result.outcome is SandboxTierCompositionOutcome.RESOLVED
    assert result.tier is SandboxTier.TIER_4_FULL_VM
    assert result.forced_tier_cause is ForcedTierCause.COMPUTER_USE_BOUND
    assert result.assigned_tier_reason is None


def test_sandbox_tier_composition_refuse_propagates() -> None:
    """Acceptance #3 — a sandbox_tier_floor REFUSE propagates to the composition."""
    floors = _StubFloors(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_1_PROCESS)
    result = sandbox_tier(
        _tool(SandboxTier.TIER_1_PROCESS),
        _PLAIN_META,
        _ctx(mcp_server=MCPServer(server_id="s", trust_level=MCPServerTrustLevel.L0_REFUSE_REMOTE)),
        floors,
    )
    assert result.outcome is SandboxTierCompositionOutcome.REFUSE
    assert result.tier is None


def test_sandbox_tier_composition_minimum_tier_floor_when_others_lower() -> None:
    """Acceptance #4 — a high contract minimum_tier wins when other floors are lower."""
    floors = _StubFloors(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_1_PROCESS)
    result = sandbox_tier(_tool(SandboxTier.TIER_4_FULL_VM), _PLAIN_META, _ctx(), floors)
    assert result.tier is SandboxTier.TIER_4_FULL_VM
    assert result.assigned_tier_reason is AssignedTierReason.CONTRACT_MINIMUM


def test_sandbox_tier_composition_assigned_tier_reason_at_tie() -> None:
    """Acceptance #4 — at a tie, the higher-precedence floor is named."""
    # sandbox_tier_floor (read-only → tier-1) and operator_policy_floor both at
    # tier-2; SANDBOX_TIER_FLOOR... here operator wins the precedence at a tie.
    floors = _StubFloors(SandboxTier.TIER_2_CONTAINER, SandboxTier.TIER_2_CONTAINER)
    result = sandbox_tier(
        _tool(SandboxTier.TIER_1_PROCESS),
        _PLAIN_META,
        _ctx(blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION),
        floors,
    )
    # blast_radius_floor(local-mutation)=tier-2, sandbox_tier_floor=tier-2,
    # mcp=tier-2, operator=tier-2 — SANDBOX_TIER_FLOOR has top precedence.
    assert result.tier is SandboxTier.TIER_2_CONTAINER
    assert result.assigned_tier_reason is AssignedTierReason.SANDBOX_TIER_FLOOR


def test_sandbox_tier_composition_pure_given_floors() -> None:
    """Acceptance #7 — pure: identical inputs yield an equal result."""
    floors = _StubFloors(SandboxTier.TIER_2_CONTAINER, SandboxTier.TIER_1_PROCESS)
    tool, ctx = _tool(SandboxTier.TIER_1_PROCESS), _ctx()
    assert sandbox_tier(tool, _PLAIN_META, ctx, floors) == sandbox_tier(
        tool, _PLAIN_META, ctx, floors
    )


def test_sandbox_tier_composition_with_stub_floors_reduces_to_d2_axes() -> None:
    """Acceptance — stub floors at tier-1 reduce the max() to the D2 floors."""
    floors = _StubFloors(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_1_PROCESS)
    result = sandbox_tier(
        _tool(SandboxTier.TIER_1_PROCESS),
        _PLAIN_META,
        _ctx(blast_radius_tier=BlastRadiusTier.EXTERNAL_REVERSIBLE),
        floors,
    )
    # blast_radius_floor(external-reversible) = tier-3 dominates.
    assert result.tier is SandboxTier.TIER_3_MICROVM


def test_assigned_tier_reason_members_match_spec_15_2_verbatim() -> None:
    """v1.1 AC — AssignedTierReason is the spec §15.2 seven-member set."""
    assert {r.value for r in AssignedTierReason} == {
        "CONTRACT_MINIMUM",
        "BLAST_RADIUS_FLOOR",
        "MCP_SERVER_TRUST_FLOOR",
        "OPERATOR_POLICY_FLOOR",
        "SANDBOX_TIER_FLOOR",
        "PERSONA_TIER_FLOOR",
        "SUB_AGENT_MONOTONIC_ASCENSION",
    }


def test_taint_state_declared() -> None:
    """v1.1 AC — TaintState is declared as the CallSiteContext field carrier."""
    assert set(TaintState) == {TaintState.UNTAINTED, TaintState.TAINTED}


def test_mcp_server_consumed_from_u_as_06() -> None:
    """v1.1 AC — MCPServer (re-homed to U-AS-06) is the CallSiteContext field type."""
    assert MCPServer.__module__ == "harness_as.sandbox_tier_floor"
