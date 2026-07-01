"""Operator-policy override scope per persona-tier — U-AS-12.

Implements C-AS-09 §9.4 (operator-policy override scope per persona tier);
C-AS-12 §12.2 reference. Declares the `OverrideScopeResult` enum and the
total `override_scope` function keying a persona tier + a proposed override
target cell to its cell-default override scope.

Authority: Implementation_Plan_Action_Surface_v1_2.md §5.2 U-AS-12 (FINALIZED
at R3.1 — Q-R3-4 resolved Reading A: acceptance-criteria wording fix only, no
signature/input/body change vs the v1 body); Spec_Action_Surface_v1.md §9.4
C-AS-09; ADR-D2 v1.2 §1.5.2 + §1.6; ADR-D5 v1.3 §1.4 + §1.5.2.

Reading A (Q-R3-4): spec §9.4 reads "Permitted at non-compliance cells" for
the solo-developer persona. For the solo-developer persona every cell is
treated as a non-compliance cell, so the override scope is total over
`(DeploymentSurface, BlastRadiusTier)` — no `cell_compliance_status` input is
required (Reading B's signature growth is foreclosed).

Plan-grade dependency note: U-AS-12 `Depends on [U-AS-04]` (PersonaTier +
DeploymentSurface discriminators). `BlastRadiusTier` is the second component
of the proposed-cell tuple and is carried by U-AS-01; U-AS-01 is an L0
foundational unit transitively in-cone of every AS unit. The missing explicit
`[U-AS-01]` edge is a plan dependency-declaration gap (Class 3 informational),
not a fork — the type is available and the edge is trivially satisfied.
"""

from __future__ import annotations

from enum import StrEnum

from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.sandbox_tier import BlastRadiusTier


class OverrideScopeResult(StrEnum):
    """Per-cell operator-policy override scope outcome (C-AS-09 §9.4).

    Closed at cardinality 4 — the two PERMITTED poles carry the audit-ledger
    shape (append-only vs hash-chained); the two PROHIBITED poles carry the
    prohibition locus (structural vs blast-radius-tier).
    """

    PERMITTED_APPEND_ONLY = "PERMITTED_APPEND_ONLY"
    PERMITTED_HASH_CHAINED = "PERMITTED_HASH_CHAINED"
    PROHIBITED_STRUCTURAL = "PROHIBITED_STRUCTURAL"
    PROHIBITED_BLAST_RADIUS_TIER = "PROHIBITED_BLAST_RADIUS_TIER"


def override_scope(
    persona_tier: PersonaTier,
    proposed_cell: tuple[DeploymentSurface, BlastRadiusTier],
) -> OverrideScopeResult:
    """Resolve the operator-policy override scope for a persona tier + cell.

    Total over `(PersonaTier, (DeploymentSurface, BlastRadiusTier))` per
    C-AS-09 §9.4 (acceptance #1):

    - ``solo-developer`` → ``PERMITTED_APPEND_ONLY``. Spec §9.4 reads
      "Permitted at non-compliance cells"; for this persona every cell is a
      non-compliance cell, so the scope is total over the cell space.
    - ``team-binding`` + ``EXTERNAL_IRREVERSIBLE`` →
      ``PROHIBITED_BLAST_RADIUS_TIER`` (spec §9.4: team-binding "Permitted
      only at non-`external-irreversible` cells").
    - ``team-binding`` + any other blast-radius tier →
      ``PERMITTED_HASH_CHAINED`` (audit-ledger entry hash-chained per
      C-IS-06).
    - ``multi-tenant-compliance`` → ``PROHIBITED_STRUCTURAL`` at any cell
      (spec §9.4: "Structurally prohibited at any cell").
    """
    _deployment_surface, blast_radius_tier = proposed_cell

    match persona_tier:
        case PersonaTier.SOLO_DEVELOPER:
            return OverrideScopeResult.PERMITTED_APPEND_ONLY
        case PersonaTier.TEAM_BINDING:
            if blast_radius_tier is BlastRadiusTier.EXTERNAL_IRREVERSIBLE:
                return OverrideScopeResult.PROHIBITED_BLAST_RADIUS_TIER
            return OverrideScopeResult.PERMITTED_HASH_CHAINED
        case PersonaTier.MULTI_TENANT_COMPLIANCE:
            return OverrideScopeResult.PROHIBITED_STRUCTURAL
