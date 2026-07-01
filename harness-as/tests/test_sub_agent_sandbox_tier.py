"""Tests for U-AS-09 — sub-agent sandbox-tier monotonic ascension (C-AS-11 §11)."""

from __future__ import annotations

import inspect

from harness_as.discriminators import DeploymentSurface
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import ToolMetadata
from harness_as.sub_agent_sandbox_tier import (
    SubAgentBoundaryViolationKind,
    detect_sub_agent_tier_downgrade,
    sub_agent_sandbox_tier,
)

_PLAIN = ToolMetadata(
    is_deterministic_inhouse=False,
    forces_computer_use=False,
    forces_code_execution=False,
)
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT


def _tier(parent: SandboxTier, blast_radius: BlastRadiusTier) -> SandboxTier:
    return sub_agent_sandbox_tier(parent, _PLAIN, blast_radius, None, _SURFACE, None)


def test_sub_agent_tier_at_or_above_parent() -> None:
    """Acceptance #1 — the sub-agent tier is always at or above the parent."""
    for parent in SandboxTier:
        for blast_radius in BlastRadiusTier:
            result = _tier(parent, blast_radius)
            assert SandboxTier.__members__  # sanity
            assert list(SandboxTier).index(result) >= list(SandboxTier).index(parent)


def test_sub_agent_tier_max_of_two_floors() -> None:
    """Acceptance #4 — the tier is max(parent, sandbox_tier_floor)."""
    # parent tier-2, blast-radius external-irreversible → floor tier-4 wins.
    assert (
        _tier(SandboxTier.TIER_2_CONTAINER, BlastRadiusTier.EXTERNAL_IRREVERSIBLE)
        is SandboxTier.TIER_4_FULL_VM
    )


def test_sub_agent_tier_parent_wins_when_floor_lower() -> None:
    """Acceptance #4 — the parent tier wins when the floor is lower."""
    assert (
        _tier(SandboxTier.TIER_3_MICROVM, BlastRadiusTier.READ_ONLY) is SandboxTier.TIER_3_MICROVM
    )


def test_sub_agent_tier_downgrade_detected() -> None:
    """Acceptance #2 — a proposed child below the parent is a detected downgrade."""
    violation = detect_sub_agent_tier_downgrade(
        SandboxTier.TIER_3_MICROVM, SandboxTier.TIER_1_PROCESS
    )
    assert violation is not None
    assert violation.kind is SubAgentBoundaryViolationKind.TIER_DOWNGRADE_ATTEMPTED


def test_sub_agent_tier_at_or_above_no_violation() -> None:
    """Acceptance #2 — a proposed child at or above the parent is no violation."""
    assert (
        detect_sub_agent_tier_downgrade(SandboxTier.TIER_2_CONTAINER, SandboxTier.TIER_3_MICROVM)
        is None
    )


def test_sub_agent_tier_d4_override_does_not_extend() -> None:
    """Acceptance #3 — a downgrade under a registry override is still a violation."""
    violation = detect_sub_agent_tier_downgrade(
        SandboxTier.TIER_3_MICROVM,
        SandboxTier.TIER_1_PROCESS,
        registry_override_active=True,
    )
    assert violation is not None
    assert violation.kind is SubAgentBoundaryViolationKind.REGISTRY_OVERRIDE_WITH_TIER_DOWNGRADE


def test_sub_agent_tier_pure_function() -> None:
    """Acceptance — sub_agent_sandbox_tier is pure: equal inputs, equal output."""
    a = _tier(SandboxTier.TIER_2_CONTAINER, BlastRadiusTier.EXTERNAL_REVERSIBLE)
    b = _tier(SandboxTier.TIER_2_CONTAINER, BlastRadiusTier.EXTERNAL_REVERSIBLE)
    assert a is b


def test_sub_agent_sandbox_tier_signature_is_six_param_per_spec_11_1() -> None:
    """Acceptance — sub_agent_sandbox_tier is the spec §11.1 6-parameter form."""
    params = list(inspect.signature(sub_agent_sandbox_tier).parameters)
    assert params == [
        "parent_sandbox_tier",
        "tool",
        "blast_radius",
        "mcp_transport",
        "deployment_surface",
        "mcp_server",
    ]
