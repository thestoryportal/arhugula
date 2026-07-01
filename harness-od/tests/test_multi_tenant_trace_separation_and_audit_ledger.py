"""Tests for U-OD-30 — per-tenant trace separation + cryptographic audit ledger.

Every materializable U-OD-30 acceptance criterion maps to >=1 test below. ACs
#11 (always-sampled audit ledger — composes with U-OD-11) and #12 (cross-axis
IS/CP edges) resolve at sub-phase 7c and are documented in the module docstring
rather than tested here — they are not within-axis materializable surfaces.
Authority: Implementation_Plan_Operational_Discipline_v2_7.md §3.7.4 (delta
chain over v2.6/v2.5/v2.1 §3.7.4); Spec_Operational_Discipline_v1_2.md §21.
"""

from __future__ import annotations

from typing import Any

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.audit_ledger_types import (
    AuditLedger,
    AuditLedgerEntry,
    AuditPayload,
    AuditSignatureAttributes,
    SignatureAlgorithm,
    StateLedgerEntryRef,
)
from harness_od.multi_tenant_trace_separation_and_audit_ledger import (
    AUDIT_SIGNATURE_REQUIRED_AT_TIER_5_LEDGER,
    PER_TENANT_SEPARATION_BINDINGS,
    HashChainBreach,
    PerTenantSeparation,
    TenantIdMissingViolation,
    TenantSeparationStrategy,
    assert_tenant_id_on_every_span_at_multi_tenant_cells,
    sign_audit_entry,
    verify_hash_chain_integrity,
)
from harness_od.observability_matrix import CellID


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


_CELL_1 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
_CELL_7 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_8 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD)


class _FakeSpan:
    """A minimal OTel-span stand-in carrying an attribute map.

    `SpanRef` is a type-alias of the OTel-SDK span handle; the live span is
    wired at a Phase-2 composition root. This stand-in exercises the
    attribute-read path of `assert_tenant_id_on_every_span_at_multi_tenant_cells`.
    """

    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = attributes


def _payload(prior_hash: str) -> AuditPayload:
    return AuditPayload(
        entry_core=StateLedgerEntryRef("entry-core-ref"),
        audit_namespace_attrs={"audit.actor": "operator"},
        prior_entry_hash=prior_hash,
    )


def _entry(prior_hash: str, entry_hash: str) -> AuditLedgerEntry:
    return AuditLedgerEntry(
        payload=_payload(prior_hash),
        signature_attrs=AuditSignatureAttributes(
            audit_signature_value="sig",
            audit_signature_algorithm=SignatureAlgorithm.ED25519,
            audit_signature_key_id="key-1",
            audit_signature_key_period="2026-Q2",
        ),
        entry_hash=entry_hash,
    )


# --- acc #1 — TenantSeparationStrategy 2 values ----------------------------


def test_tenant_separation_strategy_cardinality_two() -> None:
    """acc #1 — `TenantSeparationStrategy` enumerates exactly 2 values."""
    assert len(list(TenantSeparationStrategy)) == 2


# --- acc #2 — PER_TENANT_SEPARATION_BINDINGS only at cells 7/8 -------------


def test_per_tenant_separation_only_at_multi_tenant_cells() -> None:
    """acc #2 — exactly 2 entries, cell-7 and cell-8 only."""
    assert len(PER_TENANT_SEPARATION_BINDINGS) == 2
    assert set(PER_TENANT_SEPARATION_BINDINGS) == {_CELL_7, _CELL_8}


# --- acc #3 — per-cell strategy --------------------------------------------


def test_cell_7_self_hosted_strategy() -> None:
    """acc #3 — cell-7 -> PER_TENANT_OTLP_COLLECTOR_ROUTING (self-hosted)."""
    assert (
        PER_TENANT_SEPARATION_BINDINGS[_CELL_7].strategy
        == TenantSeparationStrategy.PER_TENANT_OTLP_COLLECTOR_ROUTING
    )


def test_cell_8_managed_cloud_strategy() -> None:
    """acc #3 — cell-8 -> PER_TENANT_BACKEND_PARTITION (managed-cloud)."""
    assert (
        PER_TENANT_SEPARATION_BINDINGS[_CELL_8].strategy
        == TenantSeparationStrategy.PER_TENANT_BACKEND_PARTITION
    )


# --- acc #4 — tenant_id_attribute byte-exact -------------------------------


def test_tenant_id_attribute_byte_exact() -> None:
    """acc #4 — tenant_id_attribute == 'tenant.id' at every binding."""
    for binding in PER_TENANT_SEPARATION_BINDINGS.values():
        assert binding.tenant_id_attribute == "tenant.id"


# --- acc #5 — cross_tenant_aggregation_forbidden ---------------------------


def test_cross_tenant_aggregation_forbidden() -> None:
    """acc #5 — cross_tenant_aggregation_forbidden == True at every binding."""
    for binding in PER_TENANT_SEPARATION_BINDINGS.values():
        assert binding.cross_tenant_aggregation_forbidden is True


# --- acc #6 — SignatureAlgorithm 3 values (U-OD-00 carrier) ----------------


def test_signature_algorithm_cardinality_three() -> None:
    """acc #6 — `SignatureAlgorithm` enumerates exactly 3 values (U-OD-00)."""
    assert len(list(SignatureAlgorithm)) == 3


def test_signature_algorithm_names_byte_exact() -> None:
    """acc #6 — the 3 values are byte-exact with §21.2 / ADR-D5 §1.4.1."""
    assert {a.value for a in SignatureAlgorithm} == {
        "ed25519",
        "ecdsa-p256",
        "rsa-pss-2048",
    }


# --- acc #7 — AuditSignatureAttributes 4 attributes (U-OD-00 carrier) ------


def test_audit_signature_attributes_cardinality_four() -> None:
    """acc #7 — `AuditSignatureAttributes` declares exactly 4 attributes."""
    assert set(AuditSignatureAttributes.model_fields) == {
        "audit_signature_value",
        "audit_signature_algorithm",
        "audit_signature_key_id",
        "audit_signature_key_period",
    }


# --- acc #8 — sign_audit_entry ---------------------------------------------


def test_sign_audit_entry_complete() -> None:
    """acc #8 — sign_audit_entry produces all 4 fields per algo selection."""
    sig = sign_audit_entry(_payload("h0"), "key-1", SignatureAlgorithm.ED25519)
    assert isinstance(sig, AuditSignatureAttributes)
    assert sig.audit_signature_algorithm == SignatureAlgorithm.ED25519
    assert sig.audit_signature_key_id == "key-1"
    assert sig.audit_signature_value
    assert sig.audit_signature_key_period


def test_sign_audit_entry_missing_key_id_reject() -> None:
    """acc #8 — missing key_id rejected at function precondition."""
    with pytest.raises(ValueError, match="key_id is required"):
        sign_audit_entry(_payload("h0"), "", SignatureAlgorithm.ED25519)


# --- acc #9 — verify_hash_chain_integrity ----------------------------------


def test_verify_hash_chain_intact_accept() -> None:
    """acc #9 — verify_hash_chain_integrity returns None for an intact chain."""
    ledger = AuditLedger(
        entries=(
            _entry("genesis", "h1"),
            _entry("h1", "h2"),
            _entry("h2", "h3"),
        ),
        cell_id=_CELL_7,
    )
    assert verify_hash_chain_integrity(ledger) is None


def test_verify_hash_chain_broken_reject() -> None:
    """acc #9 — Err(HashChainBreach) when a hash-chain link is broken."""
    ledger = AuditLedger(
        entries=(
            _entry("genesis", "h1"),
            _entry("WRONG", "h2"),
        ),
        cell_id=_CELL_7,
    )
    with pytest.raises(HashChainBreach):
        verify_hash_chain_integrity(ledger)


def test_verify_hash_chain_single_entry_accept() -> None:
    """acc #9 — a single-entry ledger is trivially well-formed."""
    ledger = AuditLedger(entries=(_entry("genesis", "h1"),), cell_id=_CELL_8)
    assert verify_hash_chain_integrity(ledger) is None


# --- acc #10 — assert_tenant_id_on_every_span_at_multi_tenant_cells --------


def test_assert_tenant_id_present_accept() -> None:
    """acc #10 — a span carrying tenant.id at cell-7 passes."""
    span = _FakeSpan({"tenant.id": "tenant-a"})
    assert (
        assert_tenant_id_on_every_span_at_multi_tenant_cells(span, _CELL_7)  # type: ignore[arg-type]
        is None
    )


def test_assert_tenant_id_missing_reject_at_cell_7() -> None:
    """acc #10 — Err(TenantIdMissingViolation) at cell-7 without tenant.id."""
    span = _FakeSpan({})
    with pytest.raises(TenantIdMissingViolation):
        assert_tenant_id_on_every_span_at_multi_tenant_cells(span, _CELL_7)  # type: ignore[arg-type]


def test_assert_tenant_id_missing_reject_at_cell_8() -> None:
    """acc #10 — Err(TenantIdMissingViolation) at cell-8 without tenant.id."""
    span = _FakeSpan({})
    with pytest.raises(TenantIdMissingViolation):
        assert_tenant_id_on_every_span_at_multi_tenant_cells(span, _CELL_8)  # type: ignore[arg-type]


def test_assert_tenant_id_not_required_at_non_multi_tenant_cell() -> None:
    """acc #10 — the tenant.id invariant applies only at cells 7/8."""
    span = _FakeSpan({})
    assert (
        assert_tenant_id_on_every_span_at_multi_tenant_cells(span, _CELL_1)  # type: ignore[arg-type]
        is None
    )


# --- §21.2 Tier-5 audit-signature requirement ------------------------------


def test_audit_signature_required_at_tier_5_ledger() -> None:
    """§21.2 + C-IS-14 §14.2 — audit-signature attestation required at Tier-5."""
    assert AUDIT_SIGNATURE_REQUIRED_AT_TIER_5_LEDGER is True


# --- acc #13 — algorithm selection deferred --------------------------------


def test_specific_algorithm_selection_deferred() -> None:
    """acc #13 — operators select within the 3-algorithm admissible set.

    `sign_audit_entry` accepts any of the 3 admissible algorithms — the
    specific selection is a deployment-binding-time operator choice per §21.2.
    """
    for algo in SignatureAlgorithm:
        sig = sign_audit_entry(_payload("h0"), "key-1", algo)
        assert sig.audit_signature_algorithm == algo


def test_per_tenant_separation_is_frozen() -> None:
    """PerTenantSeparation is a frozen, extra-forbidding record."""
    binding = PER_TENANT_SEPARATION_BINDINGS[_CELL_7]
    assert isinstance(binding, PerTenantSeparation)
    with pytest.raises(Exception):
        binding.cross_tenant_aggregation_forbidden = False  # type: ignore[misc]
