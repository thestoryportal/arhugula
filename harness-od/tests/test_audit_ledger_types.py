"""Tests for U-OD-00 — OD-local audit-ledger composition types (C-OD-14 §14.5).

Test set per the U-OD-00 `Tests:` field (OD plan v2.7 §3.0). Covers acceptance
#1-#8, including the three v2.7 carrier-defect fixes (D-1 `SignatureAlgorithm`
move, D-2 `[U-OD-01]` edge, D-3 `AuditSignatureAttributes` 4-attribute set).
"""

from __future__ import annotations

import harness_core
from harness_core import DeploymentSurface, PersonaTier
from harness_od.audit_ledger_types import (
    AuditLedger,
    AuditLedgerEntry,
    AuditPayload,
    AuditSignatureAttributes,
    SignatureAlgorithm,
    StateLedgerEntryRef,
    compute_entry_hash,
)
from harness_od.observability_matrix import CellID

_AUDIT_TYPES = (
    AuditPayload,
    AuditLedgerEntry,
    AuditLedger,
    AuditSignatureAttributes,
    SignatureAlgorithm,
)


def _payload(prior_hash: str) -> AuditPayload:
    return AuditPayload(
        entry_core=StateLedgerEntryRef("entry-ref-1"),
        audit_namespace_attrs={"audit.actor": "operator"},
        prior_entry_hash=prior_hash,
    )


def _sig() -> AuditSignatureAttributes:
    return AuditSignatureAttributes(
        audit_signature_value="sig-value",
        audit_signature_algorithm=SignatureAlgorithm.ED25519,
        audit_signature_key_id="key-1",
        audit_signature_key_period="2026-Q2",
    )


def _entry(prior_hash: str, entry_hash: str) -> AuditLedgerEntry:
    return AuditLedgerEntry(
        payload=_payload(prior_hash), signature_attrs=_sig(), entry_hash=entry_hash
    )


_MULTI_TENANT_CELL = CellID(
    persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
    deployment_surface=DeploymentSurface.MANAGED_CLOUD,
)


# --- acceptance #1-#3 — field counts ----------------------------------------


def test_audit_payload_three_fields() -> None:
    """Acceptance #1 — AuditPayload declares exactly three fields."""
    assert set(AuditPayload.model_fields) == {
        "entry_core",
        "audit_namespace_attrs",
        "prior_entry_hash",
    }


def test_audit_ledger_entry_three_fields() -> None:
    """Acceptance #2 — AuditLedgerEntry declares exactly three fields."""
    assert set(AuditLedgerEntry.model_fields) == {
        "payload",
        "signature_attrs",
        "entry_hash",
    }


def test_audit_ledger_two_fields() -> None:
    """Acceptance #3 — AuditLedger declares exactly two fields."""
    assert set(AuditLedger.model_fields) == {"entries", "cell_id"}


def test_audit_ledger_hash_chain_link_invariant() -> None:
    """Acceptance #3 — a well-formed ledger satisfies the hash-chain link
    predicate `entries[i].payload.prior_entry_hash == entries[i-1].entry_hash`;
    a broken ledger does not. (U-OD-00 declares the shape; U-OD-30 verifies.)"""
    e1 = _entry(prior_hash="genesis", entry_hash="hash-1")
    e2 = _entry(prior_hash="hash-1", entry_hash="hash-2")
    well_formed = AuditLedger(entries=(e1, e2), cell_id=_MULTI_TENANT_CELL)
    assert all(
        well_formed.entries[i].payload.prior_entry_hash == well_formed.entries[i - 1].entry_hash
        for i in range(1, len(well_formed.entries))
    )

    broken = AuditLedger(
        entries=(e1, _entry(prior_hash="WRONG", entry_hash="hash-2")),
        cell_id=_MULTI_TENANT_CELL,
    )
    assert not all(
        broken.entries[i].payload.prior_entry_hash == broken.entries[i - 1].entry_hash
        for i in range(1, len(broken.entries))
    )


# --- acceptance #4 — OD-local residence -------------------------------------


def test_audit_types_od_local_not_harness_core() -> None:
    """Acceptance #4 — the audit types reside in the OD package, not harness-core."""
    for t in _AUDIT_TYPES:
        assert t.__module__ == "harness_od.audit_ledger_types"
        assert not t.__module__.startswith("harness_core")


def test_audit_types_not_imported_from_is_axis() -> None:
    """Acceptance #4 — no audit type is an IS-axis import; the IS surface is the
    `StateLedgerEntryRef` opaque marker only."""
    for t in _AUDIT_TYPES:
        assert not t.__module__.startswith("harness_is")
    assert StateLedgerEntryRef.__module__ == "harness_od.audit_ledger_types"


def test_state_ledger_entry_ref_is_opaque_marker() -> None:
    """Acceptance #4 — StateLedgerEntryRef is an opaque `str`-newtype marker."""
    assert getattr(StateLedgerEntryRef, "__supertype__") is str  # noqa: B009
    ref = StateLedgerEntryRef("x")
    assert ref == "x"


# --- acceptance #5 — no spec extension --------------------------------------


def test_audit_payload_no_field_beyond_c_od_14_section_14_5() -> None:
    """Acceptance #5 — AuditPayload carries no field beyond C-OD-14 §14.5."""
    assert set(AuditPayload.model_fields) == {
        "entry_core",
        "audit_namespace_attrs",
        "prior_entry_hash",
    }


# --- acceptance #6 — AuditSignatureAttributes at U-OD-00 --------------------


def test_audit_signature_attributes_declared_at_u_od_00() -> None:
    """Acceptance #6 — AuditSignatureAttributes is declared at U-OD-00."""
    assert AuditSignatureAttributes.__module__ == "harness_od.audit_ledger_types"


def test_audit_signature_attributes_four_canonical_attributes() -> None:
    """Acceptance #5 / v2.7 D-3 — AuditSignatureAttributes carries exactly the
    4 canonical `audit.signature.*` attributes; no `signed_at_unix_ns`."""
    assert set(AuditSignatureAttributes.model_fields) == {
        "audit_signature_value",
        "audit_signature_algorithm",
        "audit_signature_key_id",
        "audit_signature_key_period",
    }
    assert "signed_at_unix_ns" not in AuditSignatureAttributes.model_fields


# --- acceptance #7 — SignatureAlgorithm (v2.7 D-1) --------------------------


def test_signature_algorithm_three_values_byte_exact() -> None:
    """Acceptance #7 — SignatureAlgorithm declares exactly 3 values, byte-exact
    with OD spec §21.2 (`ed25519 | ecdsa-p256 | rsa-pss-2048`)."""
    assert len(SignatureAlgorithm) == 3
    assert {a.value for a in SignatureAlgorithm} == {
        "ed25519",
        "ecdsa-p256",
        "rsa-pss-2048",
    }


# --- acceptance #8 — [U-OD-01] edge (v2.7 D-2) ------------------------------


def test_audit_ledger_cell_id_resolves_to_u_od_01() -> None:
    """Acceptance #8 — AuditLedger.cell_id resolves to U-OD-01's CellID."""
    assert CellID.__module__ == "harness_od.observability_matrix"
    ledger = AuditLedger(entries=(), cell_id=_MULTI_TENANT_CELL)
    assert type(ledger.cell_id) is CellID
    # CellID composes the harness-core carriers (re-point sweep already landed).
    assert type(ledger.cell_id.persona_tier) is harness_core.PersonaTier


# ---------------------------------------------------------------------------
# C-OD-24.5 — `compute_entry_hash` canonical helper (F2-04 absorption, v1.7)
# ---------------------------------------------------------------------------


def test_compute_entry_hash_byte_equivalent_to_canonical_recipe() -> None:
    """C-OD-24.5 canonical helper byte-equivalence anchor.

    Recipe per ADR-D5 v1.4 §1.4.1 + OD spec v1.7 C-OD-24.5: SHA-256 over
    `payload.model_dump_json()`. This test crystallizes the recipe with a
    literal expected hex string so any future canonicalization drift
    (Pydantic dump change, model field reorder, ConfigDict mutation) breaks
    this test BEFORE the cxa converter round-trip tests; the literal IS the
    spec contract at HEAD.

    Pre-computed at 2026-05-20 (Pydantic v2; Python 3.12 stdlib `hashlib`):
        compute_entry_hash(AuditPayload(
            entry_core="dispatch:abc:0",
            audit_namespace_attrs={
                "audit.cp.action_id": "abc",
                "audit.cp.gate_level": "workflow",
            },
            prior_entry_hash="0" * 64,
        )) == "3567132e039dd0e6e47c9a3258ebddcdf56626ba5c0e06ef29256e6d25998490"
    """
    payload = AuditPayload(
        entry_core=StateLedgerEntryRef("dispatch:abc:0"),
        audit_namespace_attrs={
            "audit.cp.action_id": "abc",
            "audit.cp.gate_level": "workflow",
        },
        prior_entry_hash="0" * 64,
    )
    expected = "3567132e039dd0e6e47c9a3258ebddcdf56626ba5c0e06ef29256e6d25998490"
    assert compute_entry_hash(payload) == expected


def test_compute_entry_hash_deterministic_across_invocations() -> None:
    """C-OD-24.5 determinism — same input → same output (no nonce, no time)."""
    payload = AuditPayload(
        entry_core=StateLedgerEntryRef("dispatch:x:7"),
        audit_namespace_attrs={"audit.cp.action_id": "x"},
        prior_entry_hash="a" * 64,
    )
    assert compute_entry_hash(payload) == compute_entry_hash(payload)


def test_compute_entry_hash_returns_64_char_hex() -> None:
    """C-OD-24.5 output shape — hex-encoded SHA-256 (64 chars, lowercase hex)."""
    payload = AuditPayload(
        entry_core=StateLedgerEntryRef("dispatch:y:0"),
        audit_namespace_attrs={},
        prior_entry_hash="0" * 64,
    )
    h = compute_entry_hash(payload)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_compute_entry_hash_distinguishes_distinct_payloads() -> None:
    """C-OD-24.5 collision-resistance smoke — different payload → different hash."""
    p1 = AuditPayload(
        entry_core=StateLedgerEntryRef("dispatch:p1:0"),
        audit_namespace_attrs={"audit.cp.action_id": "p1"},
        prior_entry_hash="0" * 64,
    )
    p2 = AuditPayload(
        entry_core=StateLedgerEntryRef("dispatch:p2:0"),
        audit_namespace_attrs={"audit.cp.action_id": "p2"},
        prior_entry_hash="0" * 64,
    )
    assert compute_entry_hash(p1) != compute_entry_hash(p2)
