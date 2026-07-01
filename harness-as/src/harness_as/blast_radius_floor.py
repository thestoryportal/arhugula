"""Blast-radius default sandbox-tier floor mapping — U-AS-05.

Implements C-AS-02 §2.4 (`blast_radius_floor` enum table). Maps each
`BlastRadiusTier` value to its *default* `sandbox_tier_floor` — the second of
the five `max()`-composed floor inputs at C-AS-02 §2.3 / §2.5.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-05;
Spec_Action_Surface_v1.md §2.4 C-AS-02; ADR-D2 v1.1 §1.1 (four-tier
blast-radius taxonomy).

The mapping is a *default* (acceptance #3): the runtime-resolved tier is the
`max()` of this floor with forcing-condition overrides (U-AS-02) and the
upstream `sandbox_tier_floor` lookup (U-AS-06). This module owns only the
blast-radius default row; it does not compose.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier

# C-AS-02 §2.4 table — blast-radius tier → default sandbox_tier_floor.
_BLAST_RADIUS_FLOOR: Mapping[BlastRadiusTier, SandboxTier] = MappingProxyType(
    {
        BlastRadiusTier.READ_ONLY: SandboxTier.TIER_1_PROCESS,
        BlastRadiusTier.LOCAL_MUTATION: SandboxTier.TIER_2_CONTAINER,
        BlastRadiusTier.EXTERNAL_REVERSIBLE: SandboxTier.TIER_3_MICROVM,
        BlastRadiusTier.EXTERNAL_IRREVERSIBLE: SandboxTier.TIER_4_FULL_VM,
    }
)


def blast_radius_floor(tier: BlastRadiusTier) -> SandboxTier:
    """Return the default sandbox-tier floor for a blast-radius tier.

    Pure, deterministic, total over `BlastRadiusTier` (C-AS-02 §2.4;
    acceptance #1, #4). Per-row mapping is verbatim from the §2.4 table:
    read-only → tier-1-process, local-mutation → tier-2-container,
    external-reversible → tier-3-microvm, external-irreversible →
    tier-4-full-vm.

    This is a *default* floor (acceptance #3) — subject to override from
    forcing conditions (U-AS-02) and the upstream `sandbox_tier_floor`
    composition (U-AS-06).
    """
    return _BLAST_RADIUS_FLOOR[tier]
