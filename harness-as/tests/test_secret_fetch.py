"""Tests for U-AS-20 — fetch_secret + SecretRef + tier-resolution table (C-AS-05 §5)."""

from __future__ import annotations

import inspect

from harness_as.sandbox_tier import SandboxTier
from harness_as.secret_fetch import (
    TIER_RESOLUTION_TABLE,
    SecretRef,
    SecretResolutionMechanism,
    SecretScope,
    TPerm2Pole,
    fetch_secret,
    tier_resolution_mechanism,
)

_SCOPE = SecretScope(name="anthropic-api")


def test_secret_ref_no_value_accessor_api() -> None:
    """Acceptance #2 — SecretRef opaque: no value-accessor API (§5.4 row 1)."""
    ref = fetch_secret("token", _SCOPE, SandboxTier.TIER_1_PROCESS)
    assert set(SecretRef.model_fields) == {"name", "scope", "tier"}
    for accessor in ("value", "secret", "reveal", "get_value", "resolve"):
        assert not hasattr(ref, accessor)


def test_secret_ref_lifetime_bounded_to_sandbox() -> None:
    """Acceptance #3 — SecretRef carries the sandbox-tier lifetime anchor (§5.4 row 2)."""
    for tier in SandboxTier:
        ref = fetch_secret("token", _SCOPE, tier)
        assert ref.tier is tier


def test_secret_ref_no_cross_sandbox_sharing() -> None:
    """Acceptance #3 — refs at different sandbox tiers are distinct; SecretRef frozen."""
    ref_3 = fetch_secret("token", _SCOPE, SandboxTier.TIER_3_MICROVM)
    ref_4 = fetch_secret("token", _SCOPE, SandboxTier.TIER_4_FULL_VM)
    assert ref_3 != ref_4
    assert SecretRef.model_config.get("frozen") is True


def test_tier_resolution_table_cardinality_four() -> None:
    """Acceptance #5 — TIER_RESOLUTION_TABLE declares exactly 4 entries (§5.2)."""
    assert len(TIER_RESOLUTION_TABLE) == 4
    assert {row.tier for row in TIER_RESOLUTION_TABLE} == set(SandboxTier)


def test_tier_resolution_mechanism_per_spec_row_by_row() -> None:
    """Acceptance #5 — each tier maps to its §5.2 mechanism, total over SandboxTier."""
    expected = {
        SandboxTier.TIER_1_PROCESS: SecretResolutionMechanism.ENV_VAR_AT_SANDBOX_STARTUP,
        SandboxTier.TIER_2_CONTAINER: (
            SecretResolutionMechanism.CONTAINER_ENV_VAR_WITH_KEYRING_HANDLES
        ),
        SandboxTier.TIER_3_MICROVM: SecretResolutionMechanism.IN_SANDBOX_HTTP_BOOTSTRAP_TOKEN,
        SandboxTier.TIER_4_FULL_VM: (
            SecretResolutionMechanism.IN_SANDBOX_HTTP_WITH_ROTATION_REFRESH
        ),
    }
    for tier, mechanism in expected.items():
        assert tier_resolution_mechanism(tier).mechanism is mechanism


def test_tier_1_process_pole_is_c2_within_turn() -> None:
    """Acceptance #6 — tier-1-process expresses the C2 within-turn-snapshot pole."""
    assert (
        tier_resolution_mechanism(SandboxTier.TIER_1_PROCESS).pole_expressed
        is TPerm2Pole.C2_WITHIN_TURN_SNAPSHOT
    )


def test_tier_3_microvm_pole_is_c3_across_turn() -> None:
    """Acceptance #6 — tier-3-microvm expresses the C3 across-turn-fresh-fetch pole."""
    assert (
        tier_resolution_mechanism(SandboxTier.TIER_3_MICROVM).pole_expressed
        is TPerm2Pole.C3_ACROSS_TURN_FRESH_FETCH
    )


def test_fetch_secret_signature_is_three_param() -> None:
    """Acceptance #1 — fetch_secret signature is the 3-parameter (name, scope, tier) form."""
    params = list(inspect.signature(fetch_secret).parameters)
    assert params == ["name", "scope", "tier"]
    ref = fetch_secret("token", _SCOPE, SandboxTier.TIER_2_CONTAINER)
    assert isinstance(ref, SecretRef)


def test_secret_scope_field_set_explicit_not_ellipsis() -> None:
    """Acceptance #8 — SecretScope declares an explicit field set, not a `{ ... }` ellipsis."""
    assert set(SecretScope.model_fields) == {"name"}
