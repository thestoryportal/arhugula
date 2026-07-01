"""Tests for U-OD-54 — C-OD-33 `hitl.operator_burden.*` canonical namespace
schema + OperatorBurdenAuditPayload.

ACs from OD plan v2.14 §1 U-OD-54 (preserved at v2.15):
  AC #1 Schema declares 4 attributes per §C-OD-33.1
  AC #2 OperatorBurdenAuditPayload extends AuditPayload (via §24.6
        sub-namespace discipline) with 5 burden-specific fields
  AC #3 Pattern-P1 byte-exact alignment with runtime spec v1.13 §14.10.3
  AC #4 degradation_mode populated when degrade=true
  AC #5 Unit test: schema verbatim match
"""

from __future__ import annotations

import pytest
from harness_core import AttributeValueType, Cardinality
from harness_od.hitl_operator_burden_namespace import (
    HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA,
    SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED,
    AttributeSpec,
    OperatorBurdenAuditPayload,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# AC #1 + AC #5 — schema declares 4 attributes; row-by-row verbatim match
# ---------------------------------------------------------------------------


def test_schema_has_exactly_four_attributes() -> None:
    """AC #1 — exactly 4 attributes per §C-OD-33.1."""
    assert len(HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA) == 4


def test_schema_has_single_span_site() -> None:
    """AC #1 — all 4 attributes on the `hitl.operator_burden.evaluated` site."""
    sites = {attr.span_site for attr in HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA.values()}
    assert sites == {SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED}


def test_schema_attribute_names_verbatim() -> None:
    """AC #5 — attribute names verbatim per §C-OD-33.1 table."""
    assert set(HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA.keys()) == {
        "hitl.operator_burden.cumulative_invocations",
        "hitl.operator_burden.window_ms",
        "hitl.operator_burden.persona_tier",
        "hitl.operator_burden.degrade",
    }


def test_span_site_constant_value() -> None:
    """AC #5 — span-site constant has the byte-exact span-name value."""
    assert SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED == "hitl.operator_burden.evaluated"


# ---------------------------------------------------------------------------
# AC #3 — Pattern-P1 alignment: attribute types + cardinality per §C-OD-33.1
# ---------------------------------------------------------------------------


def test_cumulative_invocations_is_int_medium() -> None:
    """AC #3 — `hitl.operator_burden.cumulative_invocations` is int + medium."""
    attr = HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA["hitl.operator_burden.cumulative_invocations"]
    assert attr.value_type == AttributeValueType.INT
    assert attr.cardinality == Cardinality.MEDIUM


def test_window_ms_is_int_low() -> None:
    """AC #3 — `hitl.operator_burden.window_ms` is int + low cardinality."""
    attr = HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA["hitl.operator_burden.window_ms"]
    assert attr.value_type == AttributeValueType.INT
    assert attr.cardinality == Cardinality.LOW


def test_persona_tier_is_enum_low() -> None:
    """AC #3 — `hitl.operator_burden.persona_tier` is enum + low (bounded-4)."""
    attr = HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA["hitl.operator_burden.persona_tier"]
    assert attr.value_type == AttributeValueType.ENUM_REF
    assert attr.cardinality == Cardinality.LOW


def test_degrade_is_bool_low() -> None:
    """AC #3 — `hitl.operator_burden.degrade` is bool + low (binary)."""
    attr = HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA["hitl.operator_burden.degrade"]
    assert attr.value_type == AttributeValueType.BOOL
    assert attr.cardinality == Cardinality.LOW


# ---------------------------------------------------------------------------
# AC #2 + AC #4 — OperatorBurdenAuditPayload fields per §C-OD-33.2
# ---------------------------------------------------------------------------


def test_audit_payload_has_required_cp_sourced_fields() -> None:
    """AC #2 — payload has the 4 `audit_cp_*` CP-sourced sub-namespace fields
    per §C-OD-24.6 discipline."""
    payload = OperatorBurdenAuditPayload(
        audit_cp_action_id="operator_burden:wf-1:1716422400000",
        audit_cp_response="burden_evaluated",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        cumulative_invocations=12,
        window_ms=60_000,
        persona_tier="senior",
        degrade=False,
        degradation_mode=None,
    )
    assert payload.audit_cp_action_id == "operator_burden:wf-1:1716422400000"
    assert payload.audit_cp_response == "burden_evaluated"
    assert payload.audit_cp_timestamp == ""
    assert payload.audit_cp_prior_event_hash == "0" * 64


def test_audit_payload_has_five_burden_specific_fields() -> None:
    """AC #2 — payload extends with 5 burden-specific fields per §C-OD-33.2."""
    payload = OperatorBurdenAuditPayload(
        audit_cp_action_id="operator_burden:wf-2:1716422460000",
        audit_cp_response="burden_degraded",
        audit_cp_timestamp="2026-05-22T12:01:00Z",
        audit_cp_prior_event_hash="a" * 64,
        cumulative_invocations=50,
        window_ms=120_000,
        persona_tier="junior",
        degrade=True,
        degradation_mode="suppress_optional_prompts",
    )
    assert payload.cumulative_invocations == 50
    assert payload.window_ms == 120_000
    assert payload.persona_tier == "junior"
    assert payload.degrade is True
    assert payload.degradation_mode == "suppress_optional_prompts"


def test_audit_payload_degradation_mode_present_when_degrade_true() -> None:
    """AC #4 — degradation_mode populated when degrade=true."""
    payload = OperatorBurdenAuditPayload(
        audit_cp_action_id="operator_burden:wf-3:1716422520000",
        audit_cp_response="burden_degraded",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        cumulative_invocations=100,
        window_ms=60_000,
        persona_tier="senior",
        degrade=True,
        degradation_mode="batch_consent",
    )
    assert payload.degrade is True
    assert payload.degradation_mode == "batch_consent"
    assert payload.degradation_mode is not None


def test_audit_payload_degradation_mode_none_when_degrade_false() -> None:
    """AC #4 — degradation_mode None when degrade=false (non-degrade path)."""
    payload = OperatorBurdenAuditPayload(
        audit_cp_action_id="operator_burden:wf-4:1716422580000",
        audit_cp_response="burden_evaluated",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        cumulative_invocations=3,
        window_ms=60_000,
        persona_tier="senior",
        degrade=False,
        degradation_mode=None,
    )
    assert payload.degrade is False
    assert payload.degradation_mode is None


def test_audit_payload_rejects_extra_fields() -> None:
    """AC #2 — payload extra='forbid' (Pydantic v2 validation discipline)."""
    with pytest.raises(ValidationError):
        OperatorBurdenAuditPayload(  # type: ignore[call-arg]
            audit_cp_action_id="operator_burden:wf-5:1716422640000",
            audit_cp_response="burden_evaluated",
            audit_cp_timestamp="",
            audit_cp_prior_event_hash="0" * 64,
            cumulative_invocations=1,
            window_ms=60_000,
            persona_tier="senior",
            degrade=False,
            degradation_mode=None,
            extra_field="nope",
        )


def test_audit_payload_is_frozen() -> None:
    """AC #2 — payload model is frozen (immutable per audit-record discipline)."""
    payload = OperatorBurdenAuditPayload(
        audit_cp_action_id="operator_burden:wf-6:1716422700000",
        audit_cp_response="burden_evaluated",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        cumulative_invocations=1,
        window_ms=60_000,
        persona_tier="senior",
        degrade=False,
        degradation_mode=None,
    )
    with pytest.raises(ValidationError):
        payload.degrade = True  # type: ignore[misc]


def test_attribute_spec_rejects_extra_fields() -> None:
    """AttributeSpec extra='forbid' invariant carries with namespace module."""
    with pytest.raises(ValidationError):
        AttributeSpec(  # type: ignore[call-arg]
            attribute_name="hitl.operator_burden.degrade",
            value_type=AttributeValueType.BOOL,
            cardinality=Cardinality.LOW,
            span_site=SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED,
            extra_field="nope",
        )
