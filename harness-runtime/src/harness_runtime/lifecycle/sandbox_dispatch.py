"""U-RT-16 — Sandbox-tier dispatch binding.

Per `Spec_Harness_Runtime_v1.md` v1.1 §4 (C-RT-04 `sandbox_dispatch`) and
Phase 2 Session 3 plan v2.1 §2 L3 U-RT-16.

Class 2 Protocol-stub concretization (L0 Tension): the runtime defines
`SandboxDispatchTable` since AS shipped the primitive functions
(`sandbox_tier`, `sandbox_tier_floor`, `provider_class_metadata`) but no
dispatch-table type.

Scope at L3:
- Build a tier → providers reverse lookup by walking the 6 `SandboxProviderClass`
  values and inverting their `tier_mapping: frozenset[SandboxTier]`. Every
  `SandboxTier` value gets at least one provider per C-AS-09 §9.2
  (acceptance #5: total over `SandboxProviderClass`).
- Expose `providers_for_tier(tier)` and `tier_floor(...)` pass-through;
  the latter wraps `harness_as.sandbox_tier_floor` so callers don't import
  AS directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from harness_as.sandbox_provider_class import (
    SandboxProviderClass,
    provider_class_metadata,
)
from harness_as.sandbox_tier import SandboxTier
from harness_as.sandbox_tier_floor import SandboxTierFloorResult, sandbox_tier_floor

__all__ = [
    "EmptyTierProvidersError",
    "SandboxDispatchTable",
    "materialize_sandbox_dispatch",
]


class EmptyTierProvidersError(LookupError):
    """Raised when a SandboxTier has no provider classes that back it.

    This violates C-AS-09 §9.2 acceptance #5 (total over `SandboxProviderClass`)
    and would surface only if the upstream metadata table is corrupted; pinned
    here as a runtime invariant assertion.
    """

    def __init__(self, tier: SandboxTier) -> None:
        super().__init__(f"no provider class backs sandbox tier {tier.value!r}")
        self.tier = tier


@dataclass(frozen=True)
class SandboxDispatchTable:
    """Tier → providers reverse lookup + floor delegate (C-AS-09 + C-AS-02)."""

    _by_tier: Mapping[SandboxTier, tuple[SandboxProviderClass, ...]]

    def providers_for_tier(self, tier: SandboxTier) -> tuple[SandboxProviderClass, ...]:
        """Return the provider classes that back `tier` (non-empty)."""
        providers = self._by_tier.get(tier, ())
        if not providers:
            raise EmptyTierProvidersError(tier)
        return providers

    @staticmethod
    def tier_floor(*args: object, **kwargs: object) -> SandboxTierFloorResult:
        """Pass-through to `harness_as.sandbox_tier_floor` (C-AS-02 §2.3).

        Re-exported here so dispatch callers depend only on this module.
        """
        return sandbox_tier_floor(*args, **kwargs)  # type: ignore[arg-type]


def materialize_sandbox_dispatch() -> SandboxDispatchTable:
    """Build the runtime's tier → providers dispatch table at stage 2 AS bootstrap.

    Walks the 6 `SandboxProviderClass` values, reads each `ProviderClassMetadata.
    tier_mapping`, and inverts to a `tier → tuple[providers]` map. Empty-tier
    invariant (acceptance #5) is asserted at construction; a missing provider
    surfaces as `EmptyTierProvidersError`.
    """
    by_tier: dict[SandboxTier, list[SandboxProviderClass]] = {tier: [] for tier in SandboxTier}
    for provider_class in SandboxProviderClass:
        metadata = provider_class_metadata(provider_class)
        for tier in metadata.tier_mapping:
            by_tier[tier].append(provider_class)

    # Assert C-AS-09 §9.2 acceptance #5: every tier has ≥1 provider.
    for tier, providers in by_tier.items():
        if not providers:
            raise EmptyTierProvidersError(tier)

    frozen: Mapping[SandboxTier, tuple[SandboxProviderClass, ...]] = MappingProxyType(
        {tier: tuple(providers) for tier, providers in by_tier.items()},
    )
    return SandboxDispatchTable(_by_tier=frozen)
