"""Tests for U-AS-10 — 12-cell deployment matrix + cell-selection lookup (C-AS-09 §9)."""

from __future__ import annotations

from harness_as.deployment_matrix import (
    DEPLOYMENT_MATRIX,
    DeploymentMatrixCell,
    lookup_cell,
    lookup_cell_with_forcing,
)
from harness_as.discriminators import DeploymentSurface
from harness_as.forced_tier_resolution import ToolContext
from harness_as.sandbox_provider_class import SandboxProviderClass
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier

_TIER_ORDER = list(SandboxTier)
_BLAST_ORDER = (
    BlastRadiusTier.READ_ONLY,
    BlastRadiusTier.LOCAL_MUTATION,
    BlastRadiusTier.EXTERNAL_REVERSIBLE,
    BlastRadiusTier.EXTERNAL_IRREVERSIBLE,
)
_NO_FORCING = ToolContext(computer_use_bound=False, code_execution_beta_invoked=False)


def test_deployment_matrix_cardinality_twelve() -> None:
    """Acceptance #1 — DEPLOYMENT_MATRIX declares exactly 12 cells (3 x 4)."""
    assert len(DEPLOYMENT_MATRIX) == 12
    assert len(DeploymentSurface) * len(BlastRadiusTier) == 12


def _assert_row(
    surface: DeploymentSurface, expected: tuple[tuple[SandboxTier, SandboxProviderClass], ...]
) -> None:
    for blast_radius, (tier, provider) in zip(_BLAST_ORDER, expected, strict=True):
        cell = DEPLOYMENT_MATRIX[(surface, blast_radius)]
        assert cell.sandbox_tier is tier
        assert cell.provider_class is provider


def test_deployment_matrix_local_development_row_per_spec() -> None:
    """Acceptance #2 — local-development row transcribes §9.1."""
    _assert_row(
        DeploymentSurface.LOCAL_DEVELOPMENT,
        (
            (SandboxTier.TIER_1_PROCESS, SandboxProviderClass.LANGUAGE_LEVEL),
            (SandboxTier.TIER_2_CONTAINER, SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT),
            (SandboxTier.TIER_3_MICROVM, SandboxProviderClass.CONTAINER),
            (SandboxTier.TIER_4_FULL_VM, SandboxProviderClass.MICROVM_FIRECRACKER),
        ),
    )


def test_deployment_matrix_self_hosted_server_row_per_spec() -> None:
    """Acceptance #2 — self-hosted-server row transcribes §9.1."""
    _assert_row(
        DeploymentSurface.SELF_HOSTED_SERVER,
        (
            (SandboxTier.TIER_1_PROCESS, SandboxProviderClass.LANGUAGE_LEVEL),
            (SandboxTier.TIER_2_CONTAINER, SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT),
            (SandboxTier.TIER_3_MICROVM, SandboxProviderClass.CONTAINER),
            (SandboxTier.TIER_4_FULL_VM, SandboxProviderClass.MICROVM_FIRECRACKER),
        ),
    )


def test_deployment_matrix_managed_cloud_row_per_spec() -> None:
    """Acceptance #2 — managed-cloud row transcribes §9.1."""
    _assert_row(
        DeploymentSurface.MANAGED_CLOUD,
        (
            (SandboxTier.TIER_1_PROCESS, SandboxProviderClass.LANGUAGE_LEVEL),
            (SandboxTier.TIER_2_CONTAINER, SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT),
            (SandboxTier.TIER_3_MICROVM, SandboxProviderClass.CONTAINER),
            (SandboxTier.TIER_4_FULL_VM, SandboxProviderClass.FULL_VM),
        ),
    )


def test_deployment_matrix_provider_classes_all_in_sandbox_provider_class_enum() -> None:
    """v1.1 AC — every cell provider_class is a SandboxProviderClass member."""
    for cell in DEPLOYMENT_MATRIX.values():
        assert cell.provider_class in SandboxProviderClass


def test_lookup_cell_total_function() -> None:
    """Acceptance #4 — lookup_cell is total over (DeploymentSurface, BlastRadiusTier)."""
    for surface in DeploymentSurface:
        for blast_radius in BlastRadiusTier:
            assert isinstance(lookup_cell(surface, blast_radius), DeploymentMatrixCell)


def test_lookup_cell_sandbox_tier_monotonic_by_blast_radius() -> None:
    """§9.1 — per surface, sandbox_tier is non-decreasing across blast-radius tiers."""
    for surface in DeploymentSurface:
        ranks = [
            _TIER_ORDER.index(lookup_cell(surface, blast_radius).sandbox_tier)
            for blast_radius in _BLAST_ORDER
        ]
        assert ranks == sorted(ranks)


def test_lookup_cell_with_forcing_computer_use_resolves_to_external_irreversible() -> None:
    """Acceptance #5 — computer-use forcing resolves to the external-irreversible cell."""
    ctx = ToolContext(computer_use_bound=True, code_execution_beta_invoked=False)
    for surface in DeploymentSurface:
        cell = lookup_cell_with_forcing(surface, BlastRadiusTier.READ_ONLY, ctx)
        assert cell == lookup_cell(surface, BlastRadiusTier.EXTERNAL_IRREVERSIBLE)


def test_lookup_cell_with_forcing_code_execution_resolves_to_external_irreversible() -> None:
    """Acceptance #5 — code-execution forcing resolves to the external-irreversible cell."""
    ctx = ToolContext(computer_use_bound=False, code_execution_beta_invoked=True)
    for surface in DeploymentSurface:
        cell = lookup_cell_with_forcing(surface, BlastRadiusTier.READ_ONLY, ctx)
        assert cell == lookup_cell(surface, BlastRadiusTier.EXTERNAL_IRREVERSIBLE)


def test_lookup_cell_with_forcing_no_forcing_matches_lookup_cell() -> None:
    """Acceptance #5 — with no forcing, lookup_cell_with_forcing matches lookup_cell."""
    for surface in DeploymentSurface:
        for blast_radius in BlastRadiusTier:
            assert lookup_cell_with_forcing(surface, blast_radius, _NO_FORCING) == lookup_cell(
                surface, blast_radius
            )
