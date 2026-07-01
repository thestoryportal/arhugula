"""Tests for U-AS-14 — 5-axis gate-level multiplicative composition (C-AS-12 §12)."""

from __future__ import annotations

from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.gate_level_composition import (
    GateLevel,
    gate_level,
    tier_to_gate_level_floor,
)
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_composition import CallSiteContext, TaintState
from harness_as.sandbox_tier_floor import MCPServer, ToolMetadata
from harness_as.tool_contract import ToolContract

_PLAIN_META = ToolMetadata(
    is_deterministic_inhouse=False,
    forces_computer_use=False,
    forces_code_execution=False,
)


class _StubFloors:
    """A stub `GateLevelFloorInterfaces` returning fixed gate levels."""

    def __init__(self, level: GateLevel) -> None:
        self._level = level

    def per_tool_gate_level(self, tool: ToolContract) -> GateLevel:
        return self._level

    def blast_radius_gate_floor(self, tool: ToolContract) -> GateLevel:
        return self._level

    def per_mcp_server_trust_floor(self, mcp_server: MCPServer | None) -> GateLevel:
        return self._level

    def persona_tier_floor(self, persona_tier: PersonaTier) -> GateLevel:
        return self._level


def _tool() -> ToolContract:
    return ToolContract(
        name="t",
        description="t",
        input_schema={},
        output_schema={},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )


def _ctx(blast_radius_tier: BlastRadiusTier) -> CallSiteContext:
    return CallSiteContext(
        taint_state=TaintState.UNTAINTED,
        mcp_server=None,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        blast_radius_tier=blast_radius_tier,
        mcp_transport=None,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        computer_use_bound=False,
        code_execution_beta_invoked=False,
    )


def test_gate_level_enum_cardinality_three() -> None:
    """Acceptance #2 — GateLevel carries exactly three values."""
    assert {g.value for g in GateLevel} == {"auto", "ask", "deny"}


def test_gate_level_ordering_auto_lt_ask_lt_deny() -> None:
    """Acceptance #3 — the gate-level ordering is AUTO < ASK < DENY."""
    order = list(GateLevel)
    assert order == [GateLevel.AUTO, GateLevel.ASK, GateLevel.DENY]


def test_tier_to_gate_level_floor_monotone() -> None:
    """Acceptance #6 — tier_to_gate_level_floor is monotone over SandboxTier."""
    gates = [tier_to_gate_level_floor(t) for t in SandboxTier]
    ranks = [list(GateLevel).index(g) for g in gates]
    assert ranks == sorted(ranks)
    assert tier_to_gate_level_floor(SandboxTier.TIER_4_FULL_VM) is GateLevel.DENY


def test_gate_level_composition_max_of_five_axes() -> None:
    """Acceptance #1 — the resolved gate is the max() of the five axes."""
    floors = _StubFloors(GateLevel.AUTO)
    # blast-radius external-irreversible → sandbox_tier_floor tier-4 → DENY axis.
    result = gate_level(_tool(), _PLAIN_META, _ctx(BlastRadiusTier.EXTERNAL_IRREVERSIBLE), floors)
    assert result is GateLevel.DENY


def test_gate_level_composition_deny_wins() -> None:
    """Acceptance #3 — a DENY floor wins over lower axes."""
    floors = _StubFloors(GateLevel.DENY)
    result = gate_level(_tool(), _PLAIN_META, _ctx(BlastRadiusTier.READ_ONLY), floors)
    assert result is GateLevel.DENY


def test_gate_level_composition_no_suppression() -> None:
    """Acceptance #4 — a single ASK axis lifts an otherwise-AUTO composition."""
    floors = _StubFloors(GateLevel.AUTO)
    # blast-radius external-reversible → tier-3 → ASK sandbox axis.
    result = gate_level(_tool(), _PLAIN_META, _ctx(BlastRadiusTier.EXTERNAL_REVERSIBLE), floors)
    assert result is GateLevel.ASK


def test_gate_level_composition_with_cp_floor_stubs_reduces_to_d2_axes() -> None:
    """Acceptance — AUTO stub floors reduce the max() to the D2 sandbox axis."""
    floors = _StubFloors(GateLevel.AUTO)
    result = gate_level(_tool(), _PLAIN_META, _ctx(BlastRadiusTier.READ_ONLY), floors)
    assert result is GateLevel.AUTO


def test_gate_level_composition_pure_given_floors() -> None:
    """Acceptance #9 — pure given floors: equal inputs, equal output."""
    floors = _StubFloors(GateLevel.ASK)
    ctx = _ctx(BlastRadiusTier.READ_ONLY)
    assert gate_level(_tool(), _PLAIN_META, ctx, floors) is gate_level(
        _tool(), _PLAIN_META, ctx, floors
    )
