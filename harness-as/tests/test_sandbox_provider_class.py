"""Tests for U-AS-11 — SandboxProviderClass enum + metadata (C-AS-09 §9.2)."""

from __future__ import annotations

from harness_as.sandbox_provider_class import (
    ClassCardinality,
    SandboxProviderClass,
    provider_class_metadata,
)
from harness_as.sandbox_tier import SandboxTier


def test_sandbox_provider_class_cardinality_six() -> None:
    """Acceptance #1, #2 — taxonomy closed at exactly six classes."""
    assert len(SandboxProviderClass) == 6


def test_sandbox_provider_class_identifier_strings_kebab_case_byte_exact() -> None:
    """Acceptance #1 — identifier strings are byte-exact kebab-case."""
    assert SandboxProviderClass.LANGUAGE_LEVEL.value == "language-level"
    assert SandboxProviderClass.FILESYSTEM_OVERLAY_WORKTREE.value == "filesystem-overlay-worktree"
    assert (
        SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT.value
        == "process-ulimit-bubblewrap-seatbelt"
    )
    assert SandboxProviderClass.CONTAINER.value == "container"
    assert SandboxProviderClass.MICROVM_FIRECRACKER.value == "microvm-firecracker"
    assert SandboxProviderClass.FULL_VM.value == "full-vm"


def test_provider_class_metadata_table_complete() -> None:
    """Acceptance #5 — provider_class_metadata is total over the enum."""
    for c in SandboxProviderClass:
        meta = provider_class_metadata(c)
        assert meta.provider_class is c
        assert meta.mechanism_description


def test_provider_class_metadata_tier_mapping_per_spec() -> None:
    """Acceptance #3 — per-class tier_mapping matches spec §9.2 column 4."""
    assert provider_class_metadata(SandboxProviderClass.LANGUAGE_LEVEL).tier_mapping == (
        frozenset({SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_2_CONTAINER})
    )
    assert provider_class_metadata(
        SandboxProviderClass.FILESYSTEM_OVERLAY_WORKTREE
    ).tier_mapping == frozenset({SandboxTier.TIER_2_CONTAINER})
    assert provider_class_metadata(
        SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT
    ).tier_mapping == frozenset({SandboxTier.TIER_2_CONTAINER})


def test_container_class_maps_only_to_tier_3() -> None:
    """Acceptance #3 — container → {tier-3-microvm} exactly."""
    assert provider_class_metadata(SandboxProviderClass.CONTAINER).tier_mapping == (
        frozenset({SandboxTier.TIER_3_MICROVM})
    )


def test_microvm_firecracker_class_maps_only_to_tier_4() -> None:
    """Acceptance #3 — microvm-firecracker → {tier-4-full-vm} exactly."""
    assert provider_class_metadata(
        SandboxProviderClass.MICROVM_FIRECRACKER
    ).tier_mapping == frozenset({SandboxTier.TIER_4_FULL_VM})
    assert provider_class_metadata(SandboxProviderClass.FULL_VM).tier_mapping == (
        frozenset({SandboxTier.TIER_4_FULL_VM})
    )


def test_provider_class_cardinality_open_for_every_class() -> None:
    """Acceptance #4 — every class carries OPEN cardinality."""
    for c in SandboxProviderClass:
        assert provider_class_metadata(c).cardinality is ClassCardinality.OPEN
