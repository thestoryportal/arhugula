"""U-RT-16 — `materialize_sandbox_dispatch` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L3:
- Each declared tier resolves a provider.
- Floor enforced (tier_floor pass-through to harness_as.sandbox_tier_floor).
- Fail-class typed (EmptyTierProvidersError on invariant violation).
"""

from __future__ import annotations

import pytest
from harness_as.sandbox_provider_class import SandboxProviderClass
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import (
    SandboxTierFloorOutcome,
    SandboxTierFloorResult,
    ToolMetadata,
)
from harness_core.deployment_surface import DeploymentSurface
from harness_runtime.lifecycle.sandbox_dispatch import (
    EmptyTierProvidersError,
    SandboxDispatchTable,
    materialize_sandbox_dispatch,
)

# ---------------------------------------------------------------------------
# Each declared tier resolves a provider (plan AC).
# ---------------------------------------------------------------------------


def test_materialize_returns_dispatch_table() -> None:
    """`materialize_sandbox_dispatch` builds a `SandboxDispatchTable`."""
    table = materialize_sandbox_dispatch()
    assert isinstance(table, SandboxDispatchTable)


def test_every_sandbox_tier_has_at_least_one_provider() -> None:
    """C-AS-09 §9.2 acceptance #5: total over `SandboxProviderClass`."""
    table = materialize_sandbox_dispatch()
    for tier in SandboxTier:
        providers = table.providers_for_tier(tier)
        assert len(providers) >= 1, f"tier {tier.value} has no providers"


def test_provider_resolution_returns_typed_providers() -> None:
    """`providers_for_tier` returns `SandboxProviderClass` values."""
    table = materialize_sandbox_dispatch()
    providers = table.providers_for_tier(SandboxTier.TIER_1_PROCESS)
    for provider in providers:
        assert isinstance(provider, SandboxProviderClass)


# ---------------------------------------------------------------------------
# Floor enforced (plan AC) — pass-through to AS sandbox_tier_floor.
# ---------------------------------------------------------------------------


def test_tier_floor_pass_through_returns_result() -> None:
    """`tier_floor` delegates to `harness_as.sandbox_tier_floor`."""
    tool = ToolMetadata(
        forces_computer_use=False, forces_code_execution=False, is_deterministic_inhouse=False
    )
    result = SandboxDispatchTable.tier_floor(
        tool,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        BlastRadiusTier.READ_ONLY,
        None,
        None,
    )
    assert isinstance(result, SandboxTierFloorResult)
    assert result.outcome is SandboxTierFloorOutcome.RESOLVED


def test_tier_floor_forces_full_vm_on_computer_use() -> None:
    """C-AS-02 §2.3 row 1: forcing condition → TIER_4_FULL_VM."""
    tool = ToolMetadata(
        forces_computer_use=True, forces_code_execution=False, is_deterministic_inhouse=False
    )
    result = SandboxDispatchTable.tier_floor(
        tool,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        BlastRadiusTier.READ_ONLY,
        None,
        None,
    )
    assert result.tier is SandboxTier.TIER_4_FULL_VM


# ---------------------------------------------------------------------------
# Fail-class typed (plan AC).
# ---------------------------------------------------------------------------


def test_empty_tier_providers_error_is_lookup_error() -> None:
    """`EmptyTierProvidersError` subclasses `LookupError` for catch discipline."""
    assert issubclass(EmptyTierProvidersError, LookupError)


def test_empty_tier_providers_carries_tier() -> None:
    """The error carries the offending tier for diagnostics."""
    err = EmptyTierProvidersError(SandboxTier.TIER_3_MICROVM)
    assert err.tier is SandboxTier.TIER_3_MICROVM


# ---------------------------------------------------------------------------
# Table surface.
# ---------------------------------------------------------------------------


def test_dispatch_table_is_frozen() -> None:
    """`SandboxDispatchTable` is a frozen dataclass."""
    table = materialize_sandbox_dispatch()
    with pytest.raises((AttributeError, Exception)):
        table._by_tier = {}  # type: ignore[misc]
