"""Sandbox-tier type declaration + metadata table — U-AS-01.

Implements C-AS-01 §1.1 (tier-set enumeration) + §1.2 (tier-label stability
invariant). Declares the 4-tier `SandboxTier` enum, the `MechanismClass` and
`BlastRadiusTier` enums, the per-tier metadata table, and the tier-monotonic
ordering predicate.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-01;
Spec_Action_Surface_v1.md §1 C-AS-01; ADR-F4 v1.1 §Decision (four-tier
sandbox-isolation tier-set).

`SandboxTier` is the *structural* attribute (stable across mechanism-class
swap per ADR-F4 v1.1 §Consequences (a)); it is deliberately distinct from
`sandbox.tech`, the swap-friendly discriminator — `sandbox.tech` is NOT
modelled here (acceptance #6).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict


class SandboxTier(StrEnum):
    """The 4 sandbox-isolation tiers (C-AS-01 §1.1). Cardinality fixed at 4."""

    TIER_1_PROCESS = "tier-1-process"
    TIER_2_CONTAINER = "tier-2-container"
    TIER_3_MICROVM = "tier-3-microvm"
    TIER_4_FULL_VM = "tier-4-full-vm"


class MechanismClass(StrEnum):
    """Isolation-mechanism class per tier (C-AS-01 §1.1)."""

    LANGUAGE_LEVEL_PLUS_FS_ACL = "LANGUAGE_LEVEL_PLUS_FS_ACL"
    PROCESS_ISOLATION_SECCOMP_NS = "PROCESS_ISOLATION_SECCOMP_NS"
    SHARED_KERNEL_CONTAINER_OR_GVISOR_OR_KATA = "SHARED_KERNEL_CONTAINER_OR_GVISOR_OR_KATA"
    HARDWARE_VIRT_MICROVM_OR_FULL_VM = "HARDWARE_VIRT_MICROVM_OR_FULL_VM"


class BlastRadiusTier(StrEnum):
    """Blast-radius capability tier (C-AS-01 §1.1 capability column)."""

    READ_ONLY = "READ_ONLY"
    LOCAL_MUTATION = "LOCAL_MUTATION"
    EXTERNAL_REVERSIBLE = "EXTERNAL_REVERSIBLE"
    EXTERNAL_IRREVERSIBLE = "EXTERNAL_IRREVERSIBLE"


class SandboxTierMetadata(BaseModel):
    """Registered metadata for one sandbox tier (C-AS-01 §1.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: SandboxTier
    label: str
    mechanism_class: MechanismClass
    capability_lower_bound: BlastRadiusTier
    """Lower bound of operations the tier accommodates (C-AS-01 §1.2)."""


_TIER_METADATA: Mapping[SandboxTier, SandboxTierMetadata] = MappingProxyType(
    {
        SandboxTier.TIER_1_PROCESS: SandboxTierMetadata(
            tier=SandboxTier.TIER_1_PROCESS,
            label="Tier 1 minimal isolation",
            mechanism_class=MechanismClass.LANGUAGE_LEVEL_PLUS_FS_ACL,
            capability_lower_bound=BlastRadiusTier.READ_ONLY,
        ),
        SandboxTier.TIER_2_CONTAINER: SandboxTierMetadata(
            tier=SandboxTier.TIER_2_CONTAINER,
            label="Tier 2 process isolation",
            mechanism_class=MechanismClass.PROCESS_ISOLATION_SECCOMP_NS,
            capability_lower_bound=BlastRadiusTier.LOCAL_MUTATION,
        ),
        SandboxTier.TIER_3_MICROVM: SandboxTierMetadata(
            tier=SandboxTier.TIER_3_MICROVM,
            label="Tier 3 container isolation",
            mechanism_class=MechanismClass.SHARED_KERNEL_CONTAINER_OR_GVISOR_OR_KATA,
            capability_lower_bound=BlastRadiusTier.EXTERNAL_REVERSIBLE,
        ),
        SandboxTier.TIER_4_FULL_VM: SandboxTierMetadata(
            tier=SandboxTier.TIER_4_FULL_VM,
            label="Tier 4 VM isolation",
            mechanism_class=MechanismClass.HARDWARE_VIRT_MICROVM_OR_FULL_VM,
            capability_lower_bound=BlastRadiusTier.EXTERNAL_IRREVERSIBLE,
        ),
    }
)

# Tier-monotonic rank — definition order is ascending isolation strength.
_TIER_RANK: Mapping[SandboxTier, int] = MappingProxyType(
    {tier: rank for rank, tier in enumerate(SandboxTier)}
)


def tier_metadata(t: SandboxTier) -> SandboxTierMetadata:
    """Return the metadata row for a sandbox tier (C-AS-01 §1.1)."""
    return _TIER_METADATA[t]


def is_tier_at_or_above(candidate: SandboxTier, floor: SandboxTier) -> bool:
    """True if `candidate` is at or above `floor` in tier-monotonic order.

    Ordering TIER_1 < TIER_2 < TIER_3 < TIER_4 (C-AS-01 §1.2). Reflexive —
    a tier is at-or-above itself.
    """
    return _TIER_RANK[candidate] >= _TIER_RANK[floor]
