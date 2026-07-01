"""Tests for U-OD-52 — C-OD-31 `mcp.trust.*` canonical namespace schema +
TrustEvaluationAuditPayload.

ACs from OD plan v2.14 §1 U-OD-52 (preserved at v2.15):
  AC #1 Schema declares 5 attributes per §C-OD-31.1
  AC #2 TrustEvaluationAuditPayload extends AuditPayload (via §24.6 sub-namespace
        discipline) with 5 trust-specific fields
  AC #3 Pattern-P1 byte-exact alignment with CP spec v1.10 §27.4
  AC #4 `audit_required` always True when audit row written (redundant carry
        for query convenience)
  AC #5 Unit test: schema verbatim match
"""

from __future__ import annotations

import pytest
from harness_core import AttributeValueType, Cardinality
from harness_od.mcp_trust_namespace import (
    MCP_TRUST_SPAN_NAMESPACE_SCHEMA,
    SPAN_SITE_MCP_TRUST_EVALUATE,
    TrustEvaluationAuditPayload,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# AC #1 + AC #5 — schema declares 5 attributes; row-by-row verbatim match
# ---------------------------------------------------------------------------


def test_schema_has_exactly_five_attributes() -> None:
    """AC #1 — exactly 5 attributes per §C-OD-31.1."""
    assert len(MCP_TRUST_SPAN_NAMESPACE_SCHEMA) == 5


def test_schema_has_single_span_site() -> None:
    """AC #1 — all 5 attributes on the `mcp.trust.evaluate` span site."""
    sites = {attr.span_site for attr in MCP_TRUST_SPAN_NAMESPACE_SCHEMA.values()}
    assert sites == {SPAN_SITE_MCP_TRUST_EVALUATE}


def test_schema_attribute_names_verbatim() -> None:
    """AC #5 — attribute names verbatim per §C-OD-31.1 table."""
    assert set(MCP_TRUST_SPAN_NAMESPACE_SCHEMA.keys()) == {
        "mcp.trust.server_name",
        "mcp.trust.primitive_kind",
        "mcp.trust.decision_reason",
        "mcp.trust.audit_required",
        "mcp.trust.tier_evaluated",
    }


def test_span_site_constant_value() -> None:
    """AC #5 — span-site constant has the byte-exact span-name value."""
    assert SPAN_SITE_MCP_TRUST_EVALUATE == "mcp.trust.evaluate"


# ---------------------------------------------------------------------------
# AC #3 — Pattern-P1 alignment: attribute types + cardinality per §C-OD-31.1
# ---------------------------------------------------------------------------


def test_server_name_is_string_medium() -> None:
    """AC #3 — `mcp.trust.server_name` is string + medium cardinality."""
    attr = MCP_TRUST_SPAN_NAMESPACE_SCHEMA["mcp.trust.server_name"]
    assert attr.value_type == AttributeValueType.STRING
    assert attr.cardinality == Cardinality.MEDIUM


def test_primitive_kind_is_enum_low() -> None:
    """AC #3 — `mcp.trust.primitive_kind` is enum + low (bounded-4) cardinality."""
    attr = MCP_TRUST_SPAN_NAMESPACE_SCHEMA["mcp.trust.primitive_kind"]
    assert attr.value_type == AttributeValueType.ENUM_REF
    assert attr.cardinality == Cardinality.LOW


def test_decision_reason_is_enum_low() -> None:
    """AC #3 — `mcp.trust.decision_reason` is enum + low (bounded-6) cardinality."""
    attr = MCP_TRUST_SPAN_NAMESPACE_SCHEMA["mcp.trust.decision_reason"]
    assert attr.value_type == AttributeValueType.ENUM_REF
    assert attr.cardinality == Cardinality.LOW


def test_audit_required_is_bool_low() -> None:
    """AC #3 — `mcp.trust.audit_required` is bool + low (binary) cardinality."""
    attr = MCP_TRUST_SPAN_NAMESPACE_SCHEMA["mcp.trust.audit_required"]
    assert attr.value_type == AttributeValueType.BOOL
    assert attr.cardinality == Cardinality.LOW


def test_tier_evaluated_is_enum_low() -> None:
    """AC #3 — `mcp.trust.tier_evaluated` is enum + low (bounded-4) cardinality."""
    attr = MCP_TRUST_SPAN_NAMESPACE_SCHEMA["mcp.trust.tier_evaluated"]
    assert attr.value_type == AttributeValueType.ENUM_REF
    assert attr.cardinality == Cardinality.LOW


# ---------------------------------------------------------------------------
# AC #2 + AC #4 — TrustEvaluationAuditPayload fields per §C-OD-31.2
# ---------------------------------------------------------------------------


def test_audit_payload_has_required_cp_sourced_fields() -> None:
    """AC #2 — TrustEvaluationAuditPayload has the 4 `audit_cp_*` CP-sourced
    sub-namespace fields per §C-OD-24.6 discipline."""
    payload = TrustEvaluationAuditPayload(
        audit_cp_action_id="mcp_trust:srv-a:tool",
        audit_cp_response="permitted",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        server_name="srv-a",
        primitive_kind="tool",
        decision_reason="unknown_server_tier_floor_pass",
        audit_required=True,
        tier_evaluated="level-2-sandbox-all",
    )
    assert payload.audit_cp_action_id == "mcp_trust:srv-a:tool"
    assert payload.audit_cp_response == "permitted"
    assert payload.audit_cp_timestamp == ""
    assert payload.audit_cp_prior_event_hash == "0" * 64


def test_audit_payload_has_five_trust_specific_fields() -> None:
    """AC #2 — payload extends with 5 mcp-trust-specific fields."""
    payload = TrustEvaluationAuditPayload(
        audit_cp_action_id="mcp_trust:srv-b:resource",
        audit_cp_response="denied",
        audit_cp_timestamp="2026-05-21T23:00:00Z",
        audit_cp_prior_event_hash="a" * 64,
        server_name="srv-b",
        primitive_kind="resource",
        decision_reason="explicit_deny",
        audit_required=True,
        tier_evaluated="level-0-refuse-remote",
    )
    assert payload.server_name == "srv-b"
    assert payload.primitive_kind == "resource"
    assert payload.decision_reason == "explicit_deny"
    assert payload.audit_required is True
    assert payload.tier_evaluated == "level-0-refuse-remote"


def test_audit_payload_audit_required_carries_true_per_ac4() -> None:
    """AC #4 — payload field `audit_required` always True when row written
    (redundant carry per §C-OD-31.2 + AC #4 query-convenience clause).

    The schema permits False but the converter discipline only writes a row
    when `audit_required=True` at emit-time. This test verifies the field
    accepts the True value (production discipline at converter side)."""
    payload = TrustEvaluationAuditPayload(
        audit_cp_action_id="mcp_trust:srv-c:prompt",
        audit_cp_response="permitted",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        server_name="srv-c",
        primitive_kind="prompt",
        decision_reason="explicit_allow",
        audit_required=True,
        tier_evaluated="level-3-allow-with-audit",
    )
    assert payload.audit_required is True


def test_audit_payload_rejects_extra_fields() -> None:
    """AC #2 — payload extra='forbid' (Pydantic v2 validation discipline)."""
    with pytest.raises(ValidationError):
        TrustEvaluationAuditPayload(  # type: ignore[call-arg]
            audit_cp_action_id="mcp_trust:srv-d:sampling",
            audit_cp_response="permitted",
            audit_cp_timestamp="",
            audit_cp_prior_event_hash="0" * 64,
            server_name="srv-d",
            primitive_kind="sampling",
            decision_reason="tier_floor_pass",
            audit_required=True,
            tier_evaluated="level-3-allow-with-audit",
            extra_field="nope",
        )


def test_audit_payload_is_frozen() -> None:
    """AC #2 — payload model is frozen (immutable per audit-record discipline)."""
    payload = TrustEvaluationAuditPayload(
        audit_cp_action_id="mcp_trust:srv-e:tool",
        audit_cp_response="permitted",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        server_name="srv-e",
        primitive_kind="tool",
        decision_reason="tier_floor_pass",
        audit_required=True,
        tier_evaluated="level-2-sandbox-all",
    )
    with pytest.raises(ValidationError):
        payload.server_name = "mutated"  # type: ignore[misc]
