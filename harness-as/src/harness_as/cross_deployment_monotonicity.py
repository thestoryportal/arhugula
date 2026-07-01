"""Cross-deployment sandbox-tier monotonicity — U-AS-15.

Implements C-AS-12 §12.4 (cross-deployment monotonicity contract) + C-AS-11
§11.4 (cross-deployment composition). Declares `persona_tier_traversal_ascends`,
`bridging_arc_effective_tier_raise`, `TierRaiseResult`,
`detect_tier_downgrade_governance_violation`, and `GovernanceViolation`.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-15 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §12.4 C-AS-12 + §11.4 C-AS-11; ADR-D2 v1.2 §1.6.

Depends on: U-AS-01 (`SandboxTier`); U-AS-04 (`DeploymentSurface`,
`PersonaTier`).

`bridging_arc_effective_tier_raise` takes the persona-tier floor as an injected
`PersonaTierFloor` callable — the §12.5 deferred clause places the
`persona_tier_floor` lookup table in CP session-3 territory; U-AS-15 consumes
it as a forward-declared interface (the U-AS-08 / U-AS-14 floor-injection
pattern), it does not implement it. The plan signatures' `from` parameter is
renamed `from_persona` (`from` is a Python keyword).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier

_PERSONA_RANK: dict[PersonaTier, int] = {p: rank for rank, p in enumerate(PersonaTier)}
_TIER_RANK: dict[SandboxTier, int] = {t: rank for rank, t in enumerate(SandboxTier)}

# A cell of the §12.4 monotonicity surface.
type Cell = tuple[DeploymentSurface, BlastRadiusTier]


class PersonaTierFloor(Protocol):
    """Forward-declared per-(persona, cell) sandbox-tier floor (C-AS-12 §12.5).

    The `persona_tier_floor` lookup table is CP session-3 territory (§12.5
    deferred clause); `bridging_arc_effective_tier_raise` consumes it as an
    injected interface.
    """

    def __call__(self, persona_tier: PersonaTier, cell: Cell) -> SandboxTier: ...


class TierRaiseResult(BaseModel):
    """Result of a bridging-arc effective-tier raise (C-AS-12 §12.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    old_floor: SandboxTier
    new_floor: SandboxTier
    raised_immediately: bool
    affected_workflows: int


class GovernanceViolationKind(StrEnum):
    """A cross-deployment governance-violation kind (C-AS-12 §12.4)."""

    TIER_DOWNGRADE_REQUIRES_CLASS_2_REVISION = "TIER_DOWNGRADE_REQUIRES_CLASS_2_REVISION"


class GovernanceViolation(BaseModel):
    """A detected cross-deployment governance violation (C-AS-12 §12.4 row 4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: GovernanceViolationKind
    cell: Cell
    from_tier: SandboxTier
    to_tier: SandboxTier


def persona_tier_traversal_ascends(from_persona: PersonaTier, to_persona: PersonaTier) -> bool:
    """True when the persona-tier traversal ascends (C-AS-12 §12.4).

    Ascends iff `to_persona` is strictly above `from_persona` in the
    SOLO_DEVELOPER < TEAM_BINDING < MULTI_TENANT_COMPLIANCE order (acceptance #1).
    """
    return _PERSONA_RANK[to_persona] > _PERSONA_RANK[from_persona]


def bridging_arc_effective_tier_raise(
    from_persona: PersonaTier,
    to_persona: PersonaTier,
    cell: Cell,
    in_flight_workflow_count: int,
    persona_tier_floor: PersonaTierFloor,
) -> TierRaiseResult:
    """Resolve the effective sandbox-tier raise across a persona bridging arc.

    Per C-AS-12 §12.4: when the persona tier ascends, the cell's
    `sandbox_tier_floor` raises monotonically (acceptance #2), and the raise is
    effective immediately for all in-flight workflows (acceptance #3). When the
    traversal does not ascend, there is no raise. No tier-equivalence below the
    destination floor (acceptance #4) — `new_floor` is never below `old_floor`.
    """
    old_floor = persona_tier_floor(from_persona, cell)
    to_floor = persona_tier_floor(to_persona, cell)
    ascends = persona_tier_traversal_ascends(from_persona, to_persona)
    raises = ascends and _TIER_RANK[to_floor] > _TIER_RANK[old_floor]
    new_floor = to_floor if raises else old_floor
    return TierRaiseResult(
        old_floor=old_floor,
        new_floor=new_floor,
        raised_immediately=raises,
        affected_workflows=in_flight_workflow_count if raises else 0,
    )


def detect_tier_downgrade_governance_violation(
    proposed_change: tuple[DeploymentSurface, BlastRadiusTier, SandboxTier, SandboxTier],
) -> GovernanceViolation | None:
    """Detect a runtime tier-downgrade governance violation (C-AS-12 §12.4 row 4).

    `proposed_change` is `(deployment_surface, blast_radius_tier, tier_old,
    tier_new)`. A strict decrease (`tier_new` below `tier_old`) is structurally
    prohibited at runtime — it requires an explicit Workflow §4.1.2 Class-2
    ADR-D2 revision. No change or an increase is not a violation.
    """
    deployment_surface, blast_radius_tier, tier_old, tier_new = proposed_change
    if _TIER_RANK[tier_new] >= _TIER_RANK[tier_old]:
        return None
    return GovernanceViolation(
        kind=GovernanceViolationKind.TIER_DOWNGRADE_REQUIRES_CLASS_2_REVISION,
        cell=(deployment_surface, blast_radius_tier),
        from_tier=tier_old,
        to_tier=tier_new,
    )
