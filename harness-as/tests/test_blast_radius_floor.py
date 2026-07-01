"""Tests for U-AS-05 — blast_radius_floor default mapping (C-AS-02 §2.4)."""

from __future__ import annotations

from harness_as.blast_radius_floor import blast_radius_floor
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier


def test_blast_radius_floor_read_only_to_tier_1() -> None:
    """Acceptance #2 — read-only → tier-1-process."""
    assert blast_radius_floor(BlastRadiusTier.READ_ONLY) is SandboxTier.TIER_1_PROCESS


def test_blast_radius_floor_local_mutation_to_tier_2() -> None:
    """Acceptance #2 — local-mutation → tier-2-container."""
    assert blast_radius_floor(BlastRadiusTier.LOCAL_MUTATION) is SandboxTier.TIER_2_CONTAINER


def test_blast_radius_floor_external_reversible_to_tier_3() -> None:
    """Acceptance #2 — external-reversible → tier-3-microvm."""
    assert blast_radius_floor(BlastRadiusTier.EXTERNAL_REVERSIBLE) is SandboxTier.TIER_3_MICROVM


def test_blast_radius_floor_external_irreversible_to_tier_4() -> None:
    """Acceptance #2 — external-irreversible → tier-4-full-vm."""
    assert blast_radius_floor(BlastRadiusTier.EXTERNAL_IRREVERSIBLE) is SandboxTier.TIER_4_FULL_VM


def test_blast_radius_floor_total_function() -> None:
    """Acceptance #1 — mapping is total; every BlastRadiusTier value resolves."""
    for tier in BlastRadiusTier:
        result = blast_radius_floor(tier)
        assert isinstance(result, SandboxTier)
