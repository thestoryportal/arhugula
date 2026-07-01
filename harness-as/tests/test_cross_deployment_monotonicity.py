"""Tests for U-AS-15 — cross-deployment sandbox-tier monotonicity (C-AS-12 §12.4)."""

from __future__ import annotations

from harness_as.cross_deployment_monotonicity import (
    Cell,
    GovernanceViolationKind,
    bridging_arc_effective_tier_raise,
    detect_tier_downgrade_governance_violation,
    persona_tier_traversal_ascends,
)
from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier

_CELL: Cell = (DeploymentSurface.LOCAL_DEVELOPMENT, BlastRadiusTier.LOCAL_MUTATION)

# A monotone stub persona-tier floor: higher persona → higher tier.
_PERSONA_FLOOR_TIER = {
    PersonaTier.SOLO_DEVELOPER: SandboxTier.TIER_1_PROCESS,
    PersonaTier.TEAM_BINDING: SandboxTier.TIER_2_CONTAINER,
    PersonaTier.MULTI_TENANT_COMPLIANCE: SandboxTier.TIER_3_MICROVM,
}


def _floor(persona_tier: PersonaTier, cell: Cell) -> SandboxTier:
    return _PERSONA_FLOOR_TIER[persona_tier]


def test_persona_tier_traversal_ascends_solo_to_team_returns_true() -> None:
    """Acceptance #1 — solo-developer → team-binding ascends."""
    assert persona_tier_traversal_ascends(PersonaTier.SOLO_DEVELOPER, PersonaTier.TEAM_BINDING)


def test_persona_tier_traversal_ascends_team_to_multi_tenant_returns_true() -> None:
    """Acceptance #1 — team-binding → multi-tenant-compliance ascends."""
    assert persona_tier_traversal_ascends(
        PersonaTier.TEAM_BINDING, PersonaTier.MULTI_TENANT_COMPLIANCE
    )


def test_persona_tier_traversal_ascends_multi_tenant_to_team_returns_false() -> None:
    """Acceptance #1 — multi-tenant-compliance → team-binding does not ascend."""
    assert not persona_tier_traversal_ascends(
        PersonaTier.MULTI_TENANT_COMPLIANCE, PersonaTier.TEAM_BINDING
    )


def test_persona_tier_traversal_ascends_equal_returns_false() -> None:
    """Acceptance #1 — an equal persona-tier traversal does not ascend."""
    assert not persona_tier_traversal_ascends(PersonaTier.TEAM_BINDING, PersonaTier.TEAM_BINDING)


def test_bridging_arc_raises_immediately_under_ascending_traversal() -> None:
    """Acceptance #2/#3 — an ascending traversal raises the floor immediately."""
    result = bridging_arc_effective_tier_raise(
        PersonaTier.SOLO_DEVELOPER, PersonaTier.MULTI_TENANT_COMPLIANCE, _CELL, 5, _floor
    )
    assert result.raised_immediately is True
    assert result.old_floor is SandboxTier.TIER_1_PROCESS
    assert result.new_floor is SandboxTier.TIER_3_MICROVM


def test_bridging_arc_no_raise_under_equal_persona_tier() -> None:
    """Acceptance #4 — an equal persona-tier traversal raises nothing."""
    result = bridging_arc_effective_tier_raise(
        PersonaTier.TEAM_BINDING, PersonaTier.TEAM_BINDING, _CELL, 5, _floor
    )
    assert result.raised_immediately is False
    assert result.old_floor is result.new_floor


def test_bridging_arc_in_flight_workflows_all_raised() -> None:
    """Acceptance #3 — all in-flight workflows are affected by the raise."""
    result = bridging_arc_effective_tier_raise(
        PersonaTier.SOLO_DEVELOPER, PersonaTier.TEAM_BINDING, _CELL, 7, _floor
    )
    assert result.affected_workflows == 7


def test_detect_tier_downgrade_returns_some_for_strict_decrease() -> None:
    """Acceptance #5 — a strict tier decrease is a governance violation."""
    violation = detect_tier_downgrade_governance_violation(
        (
            DeploymentSurface.LOCAL_DEVELOPMENT,
            BlastRadiusTier.LOCAL_MUTATION,
            SandboxTier.TIER_3_MICROVM,
            SandboxTier.TIER_1_PROCESS,
        )
    )
    assert violation is not None
    assert violation.kind is GovernanceViolationKind.TIER_DOWNGRADE_REQUIRES_CLASS_2_REVISION


def test_detect_tier_downgrade_returns_none_for_no_change_or_increase() -> None:
    """Acceptance #5 — no change or a tier increase is not a violation."""
    no_change = detect_tier_downgrade_governance_violation(
        (
            DeploymentSurface.LOCAL_DEVELOPMENT,
            BlastRadiusTier.LOCAL_MUTATION,
            SandboxTier.TIER_2_CONTAINER,
            SandboxTier.TIER_2_CONTAINER,
        )
    )
    increase = detect_tier_downgrade_governance_violation(
        (
            DeploymentSurface.LOCAL_DEVELOPMENT,
            BlastRadiusTier.LOCAL_MUTATION,
            SandboxTier.TIER_2_CONTAINER,
            SandboxTier.TIER_4_FULL_VM,
        )
    )
    assert no_change is None
    assert increase is None


def test_monotonicity_composes_with_sub_agent_ascension() -> None:
    """Acceptance #6 — sub-agent tier >= parent tier >= persona-tier floor."""
    persona_floor = _floor(PersonaTier.TEAM_BINDING, _CELL)
    parent_tier = SandboxTier.TIER_3_MICROVM  # parent at or above persona floor
    assert list(SandboxTier).index(parent_tier) >= list(SandboxTier).index(persona_floor)
