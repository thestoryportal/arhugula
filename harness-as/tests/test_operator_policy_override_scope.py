"""Tests for U-AS-12 — operator-policy override scope per persona-tier (C-AS-09 §9.4)."""

from __future__ import annotations

from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.operator_policy_override_scope import (
    OverrideScopeResult,
    override_scope,
)
from harness_as.sandbox_tier import BlastRadiusTier


def test_override_scope_solo_developer_permitted_at_all_cells() -> None:
    """Acceptance #2 — solo-developer → PERMITTED_APPEND_ONLY at every cell."""
    for surface in DeploymentSurface:
        for blast_radius in BlastRadiusTier:
            assert (
                override_scope(PersonaTier.SOLO_DEVELOPER, (surface, blast_radius))
                is OverrideScopeResult.PERMITTED_APPEND_ONLY
            )


def test_override_scope_team_binding_permitted_at_non_irreversible() -> None:
    """Acceptance #2 — team-binding at non-irreversible cells → PERMITTED_HASH_CHAINED."""
    non_irreversible = (
        BlastRadiusTier.READ_ONLY,
        BlastRadiusTier.LOCAL_MUTATION,
        BlastRadiusTier.EXTERNAL_REVERSIBLE,
    )
    for blast_radius in non_irreversible:
        assert (
            override_scope(
                PersonaTier.TEAM_BINDING,
                (DeploymentSurface.LOCAL_DEVELOPMENT, blast_radius),
            )
            is OverrideScopeResult.PERMITTED_HASH_CHAINED
        )


def test_override_scope_team_binding_prohibited_at_external_irreversible() -> None:
    """Acceptance #2 — team-binding at external-irreversible → PROHIBITED_BLAST_RADIUS_TIER."""
    for surface in DeploymentSurface:
        assert (
            override_scope(
                PersonaTier.TEAM_BINDING,
                (surface, BlastRadiusTier.EXTERNAL_IRREVERSIBLE),
            )
            is OverrideScopeResult.PROHIBITED_BLAST_RADIUS_TIER
        )


def test_override_scope_multi_tenant_compliance_prohibited_at_all_cells() -> None:
    """Acceptance #2 — multi-tenant-compliance → PROHIBITED_STRUCTURAL at every cell."""
    for surface in DeploymentSurface:
        for blast_radius in BlastRadiusTier:
            assert (
                override_scope(PersonaTier.MULTI_TENANT_COMPLIANCE, (surface, blast_radius))
                is OverrideScopeResult.PROHIBITED_STRUCTURAL
            )


def test_override_scope_total_function() -> None:
    """Acceptance #1 — override_scope is total over (PersonaTier, (surface, tier))."""
    for persona in PersonaTier:
        for surface in DeploymentSurface:
            for blast_radius in BlastRadiusTier:
                result = override_scope(persona, (surface, blast_radius))
                assert isinstance(result, OverrideScopeResult)
