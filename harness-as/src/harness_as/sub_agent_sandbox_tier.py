"""Sub-agent sandbox-tier monotonic-ascension — U-AS-09.

Implements C-AS-11 §11.1-§11.5 (sub-agent sandbox-tier monotonic-ascension
contract). Declares `sub_agent_sandbox_tier` (the `max()` ascension function),
`SubAgentBoundaryViolation`, and the tier-downgrade detector.

Authority: Implementation_Plan_Action_Surface_v1_2.md §5.2 U-AS-09 (FINALIZED
at R3.1 — Q-R3-1 G-1: `sub_agent_sandbox_tier` conformed to the AS spec v1.2
§11.1 6-parameter outer form + canonical 5-arg inner `sandbox_tier_floor`
call); Spec_Action_Surface_v1.md §11 C-AS-11; ADR-D2 v1.2; ADR-D5 v1.3 §1.5.2.

Depends on: U-AS-01 (`SandboxTier`); U-AS-04 (`DeploymentSurface`,
`MCPTransport`); U-AS-06 (`ToolMetadata`, `MCPServer`, `sandbox_tier_floor`).

`detect_sub_agent_tier_downgrade` gains an optional `registry_override_active`
argument over the plan's 2-parameter signature — implementation-grade
parameter threading: it lets the detector emit the
`REGISTRY_OVERRIDE_WITH_TIER_DOWNGRADE` variant the plan's `SubAgentBoundaryViolation`
enum declares, and demonstrates §11.3 (the D4 override clause does NOT extend
to sandbox monotonicity — a downgrade under a registry override is still a
violation).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.discriminators import DeploymentSurface, MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import (
    MCPServer,
    SandboxTierFloorOutcome,
    ToolMetadata,
    sandbox_tier_floor,
)

# Tier-monotonic rank — definition order is ascending isolation strength.
_TIER_RANK: dict[SandboxTier, int] = {tier: rank for rank, tier in enumerate(SandboxTier)}


class SubAgentBoundaryViolationKind(StrEnum):
    """A sub-agent sandbox-boundary violation kind (C-AS-11 §11.2-§11.5)."""

    TIER_DOWNGRADE_ATTEMPTED = "TIER_DOWNGRADE_ATTEMPTED"
    REGISTRY_OVERRIDE_WITH_TIER_DOWNGRADE = "REGISTRY_OVERRIDE_WITH_TIER_DOWNGRADE"


class SubAgentBoundaryViolation(BaseModel):
    """A detected sub-agent sandbox-boundary violation (C-AS-11 §11.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SubAgentBoundaryViolationKind
    parent: SandboxTier
    attempted_child: SandboxTier


def sub_agent_sandbox_tier(
    parent_sandbox_tier: SandboxTier,
    tool: ToolMetadata,
    blast_radius: BlastRadiusTier,
    mcp_transport: MCPTransport | None,
    deployment_surface: DeploymentSurface,
    mcp_server: MCPServer | None,
) -> SandboxTier:
    """Resolve a sub-agent's sandbox tier by monotonic ascension (C-AS-11 §11.1).

    The AS-spec-v1.2 §11.1 form:
    `max(parent_sandbox_tier, sandbox_tier_floor(tool, deployment_surface,
    blast_radius, mcp_transport, mcp_server))`. The sub-agent tier is always at
    or above the parent tier (§11.2 row 1); other floors do not reset at
    sub-agent dispatch. A `REFUSE` from the inner `sandbox_tier_floor` (a
    refuse-remote tool) is out of contract for sub-agent dispatch — the tool
    would have been refused before dispatch — and raises `ValueError`.
    """
    stf = sandbox_tier_floor(tool, deployment_surface, blast_radius, mcp_transport, mcp_server)
    if stf.outcome is SandboxTierFloorOutcome.REFUSE:
        raise ValueError(
            "sandbox_tier_floor returned REFUSE — a refuse-remote tool cannot "
            "reach sub-agent dispatch"
        )
    assert stf.tier is not None  # a RESOLVED outcome carries a tier
    if _TIER_RANK[parent_sandbox_tier] >= _TIER_RANK[stf.tier]:
        return parent_sandbox_tier
    return stf.tier


def detect_sub_agent_tier_downgrade(
    parent_sandbox_tier: SandboxTier,
    proposed_child_tier: SandboxTier,
    registry_override_active: bool = False,
) -> SubAgentBoundaryViolation | None:
    """Detect a sub-agent tier-downgrade boundary violation (C-AS-11 §11.2-§11.5).

    Returns a violation when `proposed_child_tier` is below the parent tier —
    a structurally-prohibited downgrade (§11.2 row 2). Per §11.3, the D4
    override clause does NOT extend to sandbox monotonicity: a downgrade under
    a registry-scoped override is still a violation
    (`REGISTRY_OVERRIDE_WITH_TIER_DOWNGRADE`); without an override it is
    `TIER_DOWNGRADE_ATTEMPTED`. Returns `None` when the proposed tier is at or
    above the parent.
    """
    if _TIER_RANK[proposed_child_tier] >= _TIER_RANK[parent_sandbox_tier]:
        return None
    kind = (
        SubAgentBoundaryViolationKind.REGISTRY_OVERRIDE_WITH_TIER_DOWNGRADE
        if registry_override_active
        else SubAgentBoundaryViolationKind.TIER_DOWNGRADE_ATTEMPTED
    )
    return SubAgentBoundaryViolation(
        kind=kind, parent=parent_sandbox_tier, attempted_child=proposed_child_tier
    )
