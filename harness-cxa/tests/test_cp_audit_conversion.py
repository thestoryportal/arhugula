"""Round-trip materializability tests for the CP→OD audit converter prototype.

**Validates the discovery prototype at `harness_cxa.cp_audit_conversion`.**
Not a production-wiring test — see module docstring + the discovery report at
`.harness/u_rt_59_fork_2_cp_to_od_audit_discovery.md`.

What these tests verify:
1. A `CPAuditLedgerEntry` converts to a well-formed signed `AuditLedgerEntry`.
2. All CP fields are projected into `audit_namespace_attrs` under the provisional
   `audit.cp.*` prefix.
3. Response-conditional hash fields appear iff populated (C-CP-16 §16.2).
4. `prior_event_hash` re-uses as `prior_entry_hash` (discovery assumption Q1).
5. The resulting entry chains correctly through `verify_hash_chain_integrity`
   (the OD ledger well-formedness check accepts the converted shape).
6. `entry_hash` is deterministic over identical inputs.
"""

from __future__ import annotations

import pytest
from harness_as.gate_level_composition import GateLevel
from harness_core import DeploymentSurface, PersonaTier
from harness_core.identity import ActionID
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry
from harness_cxa.cp_audit_conversion import (
    CP_AUDIT_NAMESPACE_PREFIX,
    cp_audit_to_od_audit,
)
from harness_od.audit_ledger_types import (
    AuditLedger,
    AuditLedgerEntry,
    SignatureAlgorithm,
    StateLedgerEntryRef,
)
from harness_od.multi_tenant_trace_separation_and_audit_ledger import (
    HashChainBreach,
    verify_hash_chain_integrity,
)
from harness_od.observability_matrix import CellID

_ZERO_HASH = "0" * 64
_PRIOR_HASH = "a" * 64


def _cp_entry(
    *,
    response: str = "approve",
    edited_proposal_hash: str | None = None,
    rejection_reason_hash: str | None = None,
    response_text_hash: str | None = None,
) -> CPAuditLedgerEntry:
    return CPAuditLedgerEntry(
        action_id=ActionID("action-test-1"),
        gate_level=GateLevel.ASK,
        response=response,
        edited_proposal_hash=edited_proposal_hash,
        rejection_reason_hash=rejection_reason_hash,
        response_text_hash=response_text_hash,
        timestamp="2026-05-20T18:30:00Z",
        prior_event_hash=_PRIOR_HASH,
    )


def test_converter_produces_well_formed_audit_ledger_entry() -> None:
    """A baseline CP entry converts to a populated OD AuditLedgerEntry."""
    cp_entry = _cp_entry()
    od_entry = cp_audit_to_od_audit(cp_entry, key_id="test-key")

    assert isinstance(od_entry, AuditLedgerEntry)
    assert od_entry.payload.prior_entry_hash == _PRIOR_HASH  # Q1 assumption
    assert od_entry.signature_attrs.audit_signature_key_id == "test-key"
    assert od_entry.signature_attrs.audit_signature_algorithm == SignatureAlgorithm.ED25519
    assert len(od_entry.entry_hash) == 64  # SHA-256 hex


def test_cp_fields_project_into_audit_cp_namespace() -> None:
    """All non-conditional CP fields land under the `audit.cp.*` prefix."""
    cp_entry = _cp_entry()
    od_entry = cp_audit_to_od_audit(cp_entry, key_id="test-key")
    attrs = od_entry.payload.audit_namespace_attrs

    assert attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.action_id"] == "action-test-1"
    assert attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.gate_level"] == "ask"
    assert attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.response"] == "approve"
    assert attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.timestamp"] == "2026-05-20T18:30:00Z"


@pytest.mark.parametrize(
    "response,field_name,field_value",
    [
        ("edit", "edited_proposal_hash", "e" * 64),
        ("reject", "rejection_reason_hash", "r" * 64),
        ("respond", "response_text_hash", "s" * 64),
    ],
)
def test_response_conditional_hash_fields_appear_iff_populated(
    response: str, field_name: str, field_value: str
) -> None:
    """Each conditional hash projects only when populated per C-CP-16 §16.2."""
    cp_entry = _cp_entry(response=response, **{field_name: field_value})
    od_entry = cp_audit_to_od_audit(cp_entry, key_id="test-key")
    attrs = od_entry.payload.audit_namespace_attrs

    assert attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.{field_name}"] == field_value
    # Other two conditional fields are absent.
    other_fields = {"edited_proposal_hash", "rejection_reason_hash", "response_text_hash"} - {
        field_name
    }
    for other in other_fields:
        assert f"{CP_AUDIT_NAMESPACE_PREFIX}.{other}" not in attrs


def test_approve_response_carries_no_conditional_hashes() -> None:
    """`approve` is the one response with no conditional hash field."""
    cp_entry = _cp_entry(response="approve")
    od_entry = cp_audit_to_od_audit(cp_entry, key_id="test-key")
    attrs = od_entry.payload.audit_namespace_attrs

    for field in ("edited_proposal_hash", "rejection_reason_hash", "response_text_hash"):
        assert f"{CP_AUDIT_NAMESPACE_PREFIX}.{field}" not in attrs


def test_caller_supplied_entry_core_overrides_synthesis() -> None:
    """Operator may pass a real IS ref instead of the synthesized opaque marker."""
    cp_entry = _cp_entry()
    real_ref = StateLedgerEntryRef("ledger-entry-real-12345")
    od_entry = cp_audit_to_od_audit(cp_entry, key_id="test-key", entry_core=real_ref)

    assert od_entry.payload.entry_core == real_ref


def test_synthesized_entry_core_uses_cp_action_id() -> None:
    """Default synthesis (Q2 Option c) marks entry_core as `cp-audit:<action_id>`."""
    cp_entry = _cp_entry()
    od_entry = cp_audit_to_od_audit(cp_entry, key_id="test-key")

    assert od_entry.payload.entry_core == "cp-audit:action-test-1"


def test_converter_output_chains_through_verify_hash_chain_integrity() -> None:
    """Two converted entries form a well-formed OD ledger that verifies cleanly."""
    cp_entry_1 = _cp_entry()
    od_entry_1 = cp_audit_to_od_audit(cp_entry_1, key_id="test-key")

    # Second entry's prior_event_hash links to the first's entry_hash.
    cp_entry_2 = CPAuditLedgerEntry(
        action_id=ActionID("action-test-2"),
        gate_level=GateLevel.AUTO,
        response="approve",
        timestamp="2026-05-20T18:31:00Z",
        prior_event_hash=od_entry_1.entry_hash,
    )
    od_entry_2 = cp_audit_to_od_audit(cp_entry_2, key_id="test-key")

    ledger = AuditLedger(
        entries=(od_entry_1, od_entry_2),
        cell_id=CellID(
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
        ),
    )
    verify_hash_chain_integrity(ledger)  # raises HashChainBreach if broken


def test_broken_chain_is_caught_by_verify() -> None:
    """Sanity check: a misaligned chain raises HashChainBreach."""
    cp_entry_1 = _cp_entry()
    od_entry_1 = cp_audit_to_od_audit(cp_entry_1, key_id="test-key")

    # Wrong prior_event_hash (does not link to entry_1.entry_hash).
    cp_entry_2 = CPAuditLedgerEntry(
        action_id=ActionID("action-test-2"),
        gate_level=GateLevel.AUTO,
        response="approve",
        timestamp="2026-05-20T18:31:00Z",
        prior_event_hash=_ZERO_HASH,
    )
    od_entry_2 = cp_audit_to_od_audit(cp_entry_2, key_id="test-key")

    ledger = AuditLedger(
        entries=(od_entry_1, od_entry_2),
        cell_id=CellID(
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
        ),
    )
    with pytest.raises(HashChainBreach):
        verify_hash_chain_integrity(ledger)


def test_entry_hash_is_deterministic_over_identical_inputs() -> None:
    """Same CP entry + same key → same entry_hash (interim convention is deterministic)."""
    cp_entry = _cp_entry()
    od_entry_a = cp_audit_to_od_audit(cp_entry, key_id="test-key")
    od_entry_b = cp_audit_to_od_audit(cp_entry, key_id="test-key")

    assert od_entry_a.entry_hash == od_entry_b.entry_hash


def test_empty_key_id_raises_value_error_from_sign_audit_entry() -> None:
    """The OD sign_audit_entry precondition propagates through the converter."""
    cp_entry = _cp_entry()
    with pytest.raises(ValueError, match="key_id is required"):
        cp_audit_to_od_audit(cp_entry, key_id="")
