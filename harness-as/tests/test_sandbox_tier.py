"""Tests for U-AS-01 — sandbox-tier type declaration (C-AS-01 §1.1, §1.2)."""

from __future__ import annotations

from harness_as.sandbox_tier import (
    BlastRadiusTier,
    SandboxTier,
    is_tier_at_or_above,
    tier_metadata,
)

# Tier identifiers byte-exact per Spec_Action_Surface_v1.md §1.1.
_SPEC_TIER_IDS = {
    "tier-1-process",
    "tier-2-container",
    "tier-3-microvm",
    "tier-4-full-vm",
}

# capability_lower_bound mapping per acceptance #4.
_SPEC_CAPABILITY: dict[SandboxTier, BlastRadiusTier] = {
    SandboxTier.TIER_1_PROCESS: BlastRadiusTier.READ_ONLY,
    SandboxTier.TIER_2_CONTAINER: BlastRadiusTier.LOCAL_MUTATION,
    SandboxTier.TIER_3_MICROVM: BlastRadiusTier.EXTERNAL_REVERSIBLE,
    SandboxTier.TIER_4_FULL_VM: BlastRadiusTier.EXTERNAL_IRREVERSIBLE,
}

_ASCENDING = [
    SandboxTier.TIER_1_PROCESS,
    SandboxTier.TIER_2_CONTAINER,
    SandboxTier.TIER_3_MICROVM,
    SandboxTier.TIER_4_FULL_VM,
]


def test_sandbox_tier_enum_cardinality_four() -> None:
    """Acceptance #1 + #3 — exactly 4 tiers; a 5th would fail this audit."""
    assert len(SandboxTier) == 4


def test_sandbox_tier_identifier_strings_kebab_case_byte_exact() -> None:
    """Acceptance #1 — tier identifier strings byte-exact kebab-case."""
    assert {t.value for t in SandboxTier} == _SPEC_TIER_IDS


def test_blast_radius_tier_enum_cardinality_four() -> None:
    """Acceptance #2 — BlastRadiusTier carries exactly 4 values."""
    assert len(BlastRadiusTier) == 4


def test_tier_metadata_table_complete() -> None:
    """Acceptance #4 — tier_metadata returns a row for every tier."""
    for tier in SandboxTier:
        assert tier_metadata(tier).tier is tier


def test_tier_metadata_capability_lower_bound_per_spec() -> None:
    """Acceptance #4 — capability_lower_bound mapped per spec §1.1."""
    for tier, capability in _SPEC_CAPABILITY.items():
        assert tier_metadata(tier).capability_lower_bound is capability


def test_is_tier_at_or_above_monotonic_ascending() -> None:
    """Acceptance #5 — tier-monotonic ordering TIER_1 < ... < TIER_4."""
    for lower_rank, lower in enumerate(_ASCENDING):
        for higher_rank, higher in enumerate(_ASCENDING):
            expected = higher_rank >= lower_rank
            assert is_tier_at_or_above(higher, lower) is expected


def test_is_tier_at_or_above_reflexive() -> None:
    """Acceptance #5 — a tier is at-or-above itself."""
    for tier in SandboxTier:
        assert is_tier_at_or_above(tier, tier) is True
