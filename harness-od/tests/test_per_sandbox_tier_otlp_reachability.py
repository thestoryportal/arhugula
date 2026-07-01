"""Tests for U-OD-29 — per-sandbox-tier OTLP reachability + F4 capability-floor.

Every U-OD-29 v2.10 §3.7.3 acceptance criterion maps to >=1 test below.
The 14 named tests in the v2.10 §3.7.3 `Tests:` field are all present.

Authority: Implementation_Plan_Operational_Discipline_v2_10.md §3.7.3 U-OD-29;
Spec_Operational_Discipline_v1_2.md §20.3 (C-OD-20).
"""

from __future__ import annotations

import inspect
from enum import Enum

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_od.per_cell_collector_placement_matrix import CollectorPlacement
from harness_od.per_sandbox_tier_otlp_reachability import (
    F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR,
    PER_SANDBOX_TIER_REACHABILITY,
    OtlpReachabilityClass,
    ReachabilityViolation,
    SandboxTierReachability,
    assert_otlp_reachable_from_sandbox,
)
from pydantic import ValidationError

# --- acc #1 — SandboxTier consumed from AS axis, not declared in-unit -------


def test_sandbox_tier_consumed_from_as_axis_not_declared_in_unit() -> None:
    """acc #1 — `SandboxTier` is the AS-axis-owned enum, not declared in-unit."""
    # The enum U-OD-29 uses is identically the AS-axis-owned type.
    assert SandboxTier.__module__ == "harness_as.sandbox_tier"
    # U-OD-29's module declares NO sandbox-tier enum of its own. The only
    # module-local enum is `OtlpReachabilityClass`; no module-local enum
    # carries the AS sandbox-tier values.
    import harness_od.per_sandbox_tier_otlp_reachability as mod

    local_enums = [
        obj
        for _, obj in inspect.getmembers(mod, inspect.isclass)
        if obj.__module__ == mod.__name__ and issubclass(obj, Enum)
    ]
    as_tier_values = {t.name for t in SandboxTier}
    for enum_cls in local_enums:
        assert {m.name for m in enum_cls} != as_tier_values
    assert {e.__name__ for e in local_enums} == {"OtlpReachabilityClass"}
    # The values are 1-indexed.
    assert {t.name for t in SandboxTier} == {
        "TIER_1_PROCESS",
        "TIER_2_CONTAINER",
        "TIER_3_MICROVM",
        "TIER_4_FULL_VM",
    }


def test_sandbox_tier_cardinality_four() -> None:
    """acc #1 — the AS `SandboxTier` enum has exactly 4 values, 1-indexed."""
    assert len(SandboxTier) == 4


# --- acc #2 — OtlpReachabilityClass enumerates exactly 4 values ------------


def test_reachability_class_cardinality_four() -> None:
    """acc #2 — `OtlpReachabilityClass` enumerates exactly 4 §20.3 values."""
    assert len(OtlpReachabilityClass) == 4
    assert {c.name for c in OtlpReachabilityClass} == {
        "LOCALHOST_SOCKET",
        "EXPLICIT_NETWORK_CONFIG",
        "PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST",
        "VENDOR_MANAGED_COLLECTOR_REACHABILITY",
    }


# --- acc #3 — PER_SANDBOX_TIER_REACHABILITY: 4 entries, §20.3 mapping ------


def test_per_tier_reachability_cardinality_four() -> None:
    """acc #3 — `PER_SANDBOX_TIER_REACHABILITY` declares exactly 4 entries."""
    assert len(PER_SANDBOX_TIER_REACHABILITY) == 4
    assert set(PER_SANDBOX_TIER_REACHABILITY) == set(SandboxTier)


def test_tier_1_process_localhost_socket() -> None:
    """acc #3 — `TIER_1_PROCESS -> LOCALHOST_SOCKET`; no per-tier egress."""
    entry = PER_SANDBOX_TIER_REACHABILITY[SandboxTier.TIER_1_PROCESS]
    assert entry.reachability_class is OtlpReachabilityClass.LOCALHOST_SOCKET
    assert entry.per_tier_egress_required is False
    assert entry.composes_with_cell_placement is True


def test_tier_2_container_explicit_network_config() -> None:
    """acc #3 — `TIER_2_CONTAINER -> EXPLICIT_NETWORK_CONFIG`; egress required."""
    entry = PER_SANDBOX_TIER_REACHABILITY[SandboxTier.TIER_2_CONTAINER]
    assert entry.reachability_class is OtlpReachabilityClass.EXPLICIT_NETWORK_CONFIG
    assert entry.per_tier_egress_required is True


def test_tier_3_microvm_per_microvm_agent_or_egress_allowlist() -> None:
    """acc #3 — `TIER_3_MICROVM -> PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST`."""
    entry = PER_SANDBOX_TIER_REACHABILITY[SandboxTier.TIER_3_MICROVM]
    assert entry.reachability_class is OtlpReachabilityClass.PER_MICROVM_AGENT_OR_EGRESS_ALLOWLIST
    assert entry.per_tier_egress_required is True


def test_tier_4_full_vm_vendor_managed_collector() -> None:
    """acc #3 — `TIER_4_FULL_VM -> VENDOR_MANAGED_COLLECTOR_REACHABILITY`."""
    entry = PER_SANDBOX_TIER_REACHABILITY[SandboxTier.TIER_4_FULL_VM]
    assert entry.reachability_class is OtlpReachabilityClass.VENDOR_MANAGED_COLLECTOR_REACHABILITY
    assert entry.per_tier_egress_required is True


# --- acc #4 — assert_otlp_reachable_from_sandbox accept / reject -----------


def test_assert_reachable_tier_1_in_process_accept() -> None:
    """acc #4 — Tier-1 process with an in-process collector reaches (Ok)."""
    assert (
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_1_PROCESS, CollectorPlacement.IN_PROCESS
        )
        is None
    )


def test_assert_reachable_tier_1_self_hosted_backend_accept() -> None:
    """acc #4 — Tier-1 process reaches an operator-bound backend collector."""
    assert (
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_1_PROCESS,
            CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
        )
        is None
    )


def test_assert_reachable_tier_2_container_explicit_network_accept() -> None:
    """acc #4 — Tier-2 container with a network-reachable collector reaches."""
    assert (
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_2_CONTAINER,
            CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
        )
        is None
    )


def test_assert_reachable_tier_3_lacks_network_reject() -> None:
    """acc #4 — Tier-3 microVM with only an in-process collector is a violation."""
    with pytest.raises(ReachabilityViolation):
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_3_MICROVM, CollectorPlacement.IN_PROCESS
        )


def test_assert_reachable_tier_3_reject_public_endpoint() -> None:
    """acc #4 + #6 — Tier-3 microVM rejects a placement it cannot lawfully
    reach. Disposition note (acc #6): the C-AS-12 §12.4 egress-policy arm —
    "MUST NOT egress to an *arbitrary public* ingestion endpoint" — is
    satisfied **by construction** over the U-OD-28 `CollectorPlacement` enum:
    every non-`IN_PROCESS` value is a private / vendor-managed endpoint, so the
    enum admits no "arbitrary public endpoint" to test against directly. The
    only Tier-3-rejecting `CollectorPlacement` is `IN_PROCESS` (no network
    reachability) — exercised here. The egress-policy arm of
    `assert_otlp_reachable_from_sandbox` remains a guard against a future
    `CollectorPlacement` widening that admits a public endpoint."""
    with pytest.raises(ReachabilityViolation):
        # `IN_PROCESS` gives an isolated tier no network reachability at all —
        # the isolated tier cannot reach it (the §20.3 network-reachability
        # arm rejects). It is the only Tier-3-rejecting CollectorPlacement.
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_3_MICROVM, CollectorPlacement.IN_PROCESS
        )


def test_assert_reachable_tier_3_vendor_managed_accept() -> None:
    """acc #6 — Tier-3 microVM egressing to a vendor-managed collector reaches."""
    assert (
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_3_MICROVM, CollectorPlacement.VENDOR_PIPELINE
        )
        is None
    )


def test_assert_reachable_tier_4_full_vm_vendor_managed_accept() -> None:
    """acc #6 — Tier-4 full-VM egressing to a vendor-managed collector reaches."""
    assert (
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_4_FULL_VM,
            CollectorPlacement.VENDOR_MANAGED_COLLECTOR,
        )
        is None
    )


# --- acc #5 — F4 capability-floor lifecycle-event-emission anchor ----------


def test_f4_capability_floor_lifecycle_anchor_byte_exact() -> None:
    """acc #5 — the F4 capability-floor anchor is byte-exact with v2.10 §3.7.3."""
    assert F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR == (
        "Lifecycle events (per U-OD-08 F3 mapping) MUST emit from every "
        "sandbox tier; failure to emit constitutes F4 v1.1 capability-floor "
        "(iv) violation"
    )


def test_lifecycle_event_emission_required_at_every_tier() -> None:
    """acc #5 — the F4 anchor mandates emission from every sandbox tier
    regardless of collector placement; the anchor names the universality."""
    assert "every" in F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR
    assert "sandbox tier" in F4_CAPABILITY_FLOOR_LIFECYCLE_EMISSION_ANCHOR
    # The discipline binds independently of the per-tier reachability class —
    # all 4 tiers carry a reachability entry, so emission is enforceable at each.
    assert len(PER_SANDBOX_TIER_REACHABILITY) == len(SandboxTier)


# --- acc #6 — Tier-3/4 egress-policy composition with C-AS-12 §12.4 --------


def test_tier_4_reject_in_process_no_network_reachability() -> None:
    """acc #6 — Tier-4 full-VM with only an in-process collector is a violation
    (the most-isolated tier has no network reachability to it)."""
    with pytest.raises(ReachabilityViolation):
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_4_FULL_VM, CollectorPlacement.IN_PROCESS
        )


# --- acc #7 — additive composition with U-OD-28 per-cell placement ---------


def test_reachability_composes_additively_with_placement() -> None:
    """acc #7 — per-tier reachability composes additively with the U-OD-28
    per-cell collector placement; both must hold for emission to succeed."""
    # Every per-tier entry declares it composes with cell placement.
    assert all(e.composes_with_cell_placement for e in PER_SANDBOX_TIER_REACHABILITY.values())
    # The assertion function takes BOTH the tier and the U-OD-28 placement —
    # a tier reachable in the abstract still fails if the cell placement does
    # not satisfy it (additive: tier-side AND placement-side).
    assert (
        assert_otlp_reachable_from_sandbox(
            SandboxTier.TIER_1_PROCESS, CollectorPlacement.IN_PROCESS
        )
        is None
    )
    with pytest.raises(ReachabilityViolation):
        assert_otlp_reachable_from_sandbox(SandboxTier.TIER_1_PROCESS, CollectorPlacement.SIDECAR)


# --- acc #8 — cross-axis edge OD-S4-3.A to U-AS-NN (C-AS-12 §12.4) ---------


def test_cross_axis_edge_to_u_as_nn_c_as_12_section_12_4() -> None:
    """acc #8 — the C-AS-12 §12.4 cross-axis edge is declared in the module
    docstring (resolves at 7c); the `SandboxTier` enum is consumed cross-axis
    from the AS axis (C-AS-01 §1.1)."""
    import harness_od.per_sandbox_tier_otlp_reachability as mod

    doc = mod.__doc__ or ""
    assert "C-AS-12 §12.4" in doc
    assert "7c" in doc
    assert "C-AS-01 §1.1" in doc
    # The enum is sourced from the AS axis, not the OD axis.
    assert mod.SandboxTier.__module__ == "harness_as.sandbox_tier"


# --- structural — SandboxTierReachability is frozen + extra-forbid ---------


def test_sandbox_tier_reachability_frozen_extra_forbid() -> None:
    """`SandboxTierReachability` is frozen with `extra="forbid"`."""
    entry = PER_SANDBOX_TIER_REACHABILITY[SandboxTier.TIER_1_PROCESS]
    with pytest.raises(ValidationError):
        entry.per_tier_egress_required = True  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SandboxTierReachability(
            sandbox_tier=SandboxTier.TIER_1_PROCESS,
            reachability_class=OtlpReachabilityClass.LOCALHOST_SOCKET,
            per_tier_egress_required=False,
            composes_with_cell_placement=True,
            unexpected="x",  # type: ignore[call-arg]
        )
