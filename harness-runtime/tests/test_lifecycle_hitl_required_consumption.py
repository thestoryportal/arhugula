"""U-RT-90 unit tests — `_hitl_required` 4-axis consumption per Reading B.

Covers runtime spec v1.22 §14.8.2 step 4c — thin wrapper around CP-axis
`harness_cp.gate_level_rule.hitl_required` (4-axis materialized {per_tool_gate_level,
blast_radius, persona_tier, mcp_trust} per CP spec v1.35 §19.1.2 / U-CP-98 — these
tests isolate the blast/persona axes via the L3 no-floor mcp default).
"""

from __future__ import annotations

from harness_as import BlastRadiusTier
from harness_core import PersonaTier
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.gate_level_rule import GateLevel, GateLevelInput
from harness_runtime.lifecycle.hitl_required_consumption import (
    evaluate_hitl_required,
)


def _input(
    persona: PersonaTier = PersonaTier.SOLO_DEVELOPER,
    blast_radius: BlastRadiusTier = BlastRadiusTier.READ_ONLY,
    per_tool: GateLevel = GateLevel.AUTO,
) -> GateLevelInput:
    """Construct a GateLevelInput conformed to v2.20 spec-canonical 4-axis.

    Per CP spec v1.15 §19.1.1.1 — fields: per_tool_gate_level (degenerate
    direct value), persona_tier, blast_radius_tier, mcp_trust_tier. These tests
    isolate the blast/persona consumption, so the helper feeds the L3 no-floor
    default (CP spec v1.35 §19.1.2 / U-CP-98 — AUTO contributes nothing to max();
    MCP_TRUST is now a composed axis, exercised in the CP gate_level_rule tests).
    """
    return GateLevelInput(
        per_tool_gate_level=per_tool,
        persona_tier=persona,
        blast_radius_tier=blast_radius,
        mcp_trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    )


class TestEvaluateHitlRequired:
    """4-axis composition coverage per C-CP-19 §19.1 + §19.4 (AC #2)."""

    def test_read_only_blast_radius_alone_does_not_force_hitl_unless_persona_floor_applies(
        self,
    ) -> None:
        """READ_ONLY blast → AUTO; but persona floor for all 3 tiers = ASK → True."""
        # All 3 persona tiers map to ASK per PERSONA_TIER_GATE_LEVEL_FLOOR.
        # So max(blast=AUTO, persona=ASK) = ASK → True.
        result = evaluate_hitl_required(_input(blast_radius=BlastRadiusTier.READ_ONLY))
        assert result is True

    def test_external_irreversible_blast_radius_forces_hitl(self) -> None:
        """EXTERNAL_IRREVERSIBLE → ASK; combined with persona ASK → True."""
        result = evaluate_hitl_required(_input(blast_radius=BlastRadiusTier.EXTERNAL_IRREVERSIBLE))
        assert result is True

    def test_all_persona_tiers_yield_hitl_required(self) -> None:
        """Per PERSONA_TIER_GATE_LEVEL_FLOOR all 3 tiers map to ASK → True."""
        for persona in (
            PersonaTier.SOLO_DEVELOPER,
            PersonaTier.TEAM_BINDING,
            PersonaTier.MULTI_TENANT_COMPLIANCE,
        ):
            result = evaluate_hitl_required(_input(persona=persona))
            assert result is True, f"persona={persona!r} did not yield True"

    def test_local_mutation_blast_radius_with_solo_developer(self) -> None:
        """LOCAL_MUTATION → ASK; max(ASK, ASK) = ASK → True."""
        result = evaluate_hitl_required(
            _input(
                persona=PersonaTier.SOLO_DEVELOPER,
                blast_radius=BlastRadiusTier.LOCAL_MUTATION,
            )
        )
        assert result is True


class TestPureFunctionGuarantee:
    """AC #3 — idempotent + no module state."""

    def test_same_input_same_output(self) -> None:
        """Calling twice yields identical results."""
        input_a = _input()
        result1 = evaluate_hitl_required(input_a)
        result2 = evaluate_hitl_required(input_a)
        assert result1 == result2

    def test_no_input_mutation(self) -> None:
        """Input GateLevelInput is frozen (Pydantic v2 frozen=True); cannot be mutated."""
        input_a = _input()
        # Per CP-axis gate_level_rule GateLevelInput.model_config: frozen=True.
        # Calling evaluate_hitl_required must not raise (the function reads only).
        result = evaluate_hitl_required(input_a)
        assert isinstance(result, bool)
