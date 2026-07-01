"""Tests for U-CP-37 — 4-response HITL palette (C-CP-16 §16.1-§16.4).

Acceptance-criterion coverage:
  #1 4 values per §16.1        -> test_hitl_response_cardinality_four,
                                  test_response_values_match_spec_verbatim
  #2 cell applicability ALL    -> test_semantics_cell_applicability_all
  #3 per-response audit shapes -> test_per_response_audit_shapes_match_spec,
                                  test_approve_audit_required_fields,
                                  test_edit_audit_carries_edited_proposal_hash,
                                  test_reject_audit_rejection_reason_optional,
                                  test_respond_audit_required_response_text_hash
  #4 3 palette invariants      -> test_palette_invariants_cardinality_three
  #5 hitl.response.class attr  -> test_hitl_response_class_cardinality_bounded_four,
                                  test_emitted_on_hitl_invocation_responded
  #6 prior_event_hash chains   -> test_prior_event_hash_in_every_audit_shape
  #7 RESPOND vs REJECT         -> test_respond_distinct_from_reject
"""

from __future__ import annotations

from harness_core import Cardinality
from harness_cp.hitl_response_palette import (
    HITL_RESPONSE_CLASS_ATTRIBUTE,
    HITL_RESPONSE_SEMANTICS,
    PALETTE_INVARIANTS,
    PER_RESPONSE_AUDIT_ENTRY_SHAPES,
    AuditFieldName,
    CellApplicabilityScope,
    HITLResponse,
)

_SPEC_RESPONSES = {"approve", "edit", "reject", "respond"}


def _shape(response: HITLResponse):
    return next(s for s in PER_RESPONSE_AUDIT_ENTRY_SHAPES if s.response == response)


def test_hitl_response_cardinality_four() -> None:
    """Acceptance #1 — exactly four palette values."""
    assert len(HITLResponse) == 4


def test_response_values_match_spec_verbatim() -> None:
    """Acceptance #1 — palette values match C-CP-16 §16.1 verbatim."""
    assert {r.value for r in HITLResponse} == _SPEC_RESPONSES


def test_semantics_cell_applicability_all() -> None:
    """Acceptance #2 — all four semantics carry `ALL_CELLS` applicability."""
    assert len(HITL_RESPONSE_SEMANTICS) == 4
    for sem in HITL_RESPONSE_SEMANTICS:
        assert sem.cell_applicability == CellApplicabilityScope.ALL_CELLS


def test_per_response_audit_shapes_match_spec() -> None:
    """Acceptance #3 — four per-response audit entry shapes per §16.2."""
    assert len(PER_RESPONSE_AUDIT_ENTRY_SHAPES) == 4
    assert {s.response for s in PER_RESPONSE_AUDIT_ENTRY_SHAPES} == set(HITLResponse)


def test_approve_audit_required_fields() -> None:
    """Acceptance #3 — `approve` requires the §16.2 base 5-field set."""
    assert _shape(HITLResponse.APPROVE).required_fields == frozenset(
        {
            AuditFieldName.ACTION_ID,
            AuditFieldName.GATE_LEVEL,
            AuditFieldName.RESPONSE,
            AuditFieldName.TIMESTAMP,
            AuditFieldName.PRIOR_EVENT_HASH,
        }
    )
    assert _shape(HITLResponse.APPROVE).optional_fields == frozenset()


def test_edit_audit_carries_edited_proposal_hash() -> None:
    """Acceptance #3 — `edit` adds required `edited_proposal_hash`."""
    assert AuditFieldName.EDITED_PROPOSAL_HASH in _shape(HITLResponse.EDIT).required_fields


def test_reject_audit_rejection_reason_optional() -> None:
    """Acceptance #3 — `reject` adds *optional* `rejection_reason_hash`."""
    shape = _shape(HITLResponse.REJECT)
    assert AuditFieldName.REJECTION_REASON_HASH in shape.optional_fields
    assert AuditFieldName.REJECTION_REASON_HASH not in shape.required_fields


def test_respond_audit_required_response_text_hash() -> None:
    """Acceptance #3 — `respond` adds *required* `response_text_hash`."""
    assert AuditFieldName.RESPONSE_TEXT_HASH in _shape(HITLResponse.RESPOND).required_fields


def test_palette_invariants_cardinality_three() -> None:
    """Acceptance #4 — three §16.3 palette invariants."""
    assert len(PALETTE_INVARIANTS) == 3
    names = {inv.invariant_name for inv in PALETTE_INVARIANTS}
    assert names == {
        "palette_completeness",
        "synchrony_class_does_not_narrow_palette",
        "pre_hitl_escalation_may_narrow_palette",
    }


def test_hitl_response_class_cardinality_bounded_four() -> None:
    """Acceptance #5 — `hitl.response.class` is bounded at 4 (the palette)."""
    assert HITL_RESPONSE_CLASS_ATTRIBUTE.attribute_name == "hitl.response.class"
    assert HITL_RESPONSE_CLASS_ATTRIBUTE.cardinality == Cardinality.LOW
    assert len(HITLResponse) == 4


def test_emitted_on_hitl_invocation_responded() -> None:
    """Acceptance #5 — emitted on the `hitl.invocation.responded` event."""
    assert HITL_RESPONSE_CLASS_ATTRIBUTE.emitted_on == "hitl.invocation.responded"


def test_prior_event_hash_in_every_audit_shape() -> None:
    """Acceptance #6 — `prior_event_hash` chains in every per-response shape.

    Chain-link construction itself delegates to U-IS-09 (cross-axis IS).
    """
    for shape in PER_RESPONSE_AUDIT_ENTRY_SHAPES:
        assert AuditFieldName.PRIOR_EVENT_HASH in shape.required_fields


def test_respond_distinct_from_reject() -> None:
    """Acceptance #7 — `respond` (continue dialogue) is distinct from `reject`."""
    assert HITLResponse.RESPOND != HITLResponse.REJECT
    respond_sem = next(s for s in HITL_RESPONSE_SEMANTICS if s.response == HITLResponse.RESPOND)
    reject_sem = next(s for s in HITL_RESPONSE_SEMANTICS if s.response == HITLResponse.REJECT)
    assert "without action" in respond_sem.semantic
    assert "Cancel" in reject_sem.semantic
