"""Tests for U-OD-53 — C-OD-32 `hitl.webhook.*` canonical namespace schema +
WebhookDeliveryAuditPayload.

ACs from OD plan v2.14 §1 U-OD-53 (preserved at v2.15):
  AC #1 Schema declares 6 attributes per §C-OD-32.1 (3 outer + 3 per-attempt
        + retry.attempt_number reused from C-CP-03 §3.5)
  AC #2 WebhookDeliveryAuditPayload extends AuditPayload (via §24.6
        sub-namespace discipline) with 5 webhook-specific fields
  AC #3 Pattern-P1 byte-exact alignment with runtime spec v1.13 §14.10.3
  AC #4 Audit row per delivery attempt (not per delivery)
  AC #5 Unit test: schema verbatim match
"""

from __future__ import annotations

import pytest
from harness_core import AttributeValueType, Cardinality
from harness_od.hitl_webhook_namespace import (
    HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA,
    SPAN_SITE_HITL_WEBHOOK_ATTEMPT,
    SPAN_SITE_HITL_WEBHOOK_DELIVER,
    AttributeSpec,
    WebhookDeliveryAuditPayload,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# AC #1 + AC #5 — schema declares 6 attributes; row-by-row verbatim match
# ---------------------------------------------------------------------------


def test_schema_has_exactly_six_attributes() -> None:
    """AC #1 — exactly 6 attributes per §C-OD-32.1."""
    assert len(HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA) == 6


def test_schema_attribute_names_verbatim() -> None:
    """AC #5 — attribute names verbatim per §C-OD-32.1 table."""
    assert set(HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA.keys()) == {
        "webhook.url_hash",
        "webhook.delivery_attempts",
        "webhook.idempotency_key",
        "retry.attempt_number",
        "webhook.status_code",
        "webhook.attempt_latency_ms",
    }


def test_schema_has_two_span_sites() -> None:
    """AC #1 — 2 span sites: `hitl.webhook.deliver` outer + `hitl.webhook.attempt`."""
    sites = {attr.span_site for attr in HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA.values()}
    assert sites == {
        SPAN_SITE_HITL_WEBHOOK_DELIVER,
        SPAN_SITE_HITL_WEBHOOK_ATTEMPT,
    }


def test_outer_site_has_three_attributes() -> None:
    """AC #1 — 3 attributes on the `hitl.webhook.deliver` outer span site."""
    outer = [
        attr
        for attr in HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA.values()
        if attr.span_site == SPAN_SITE_HITL_WEBHOOK_DELIVER
    ]
    assert len(outer) == 3
    assert {a.attribute_name for a in outer} == {
        "webhook.url_hash",
        "webhook.delivery_attempts",
        "webhook.idempotency_key",
    }


def test_per_attempt_site_has_three_attributes() -> None:
    """AC #1 — 3 attributes on the `hitl.webhook.attempt` per-attempt site
    (including reused `retry.attempt_number` from C-CP-03 §3.5)."""
    per_attempt = [
        attr
        for attr in HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA.values()
        if attr.span_site == SPAN_SITE_HITL_WEBHOOK_ATTEMPT
    ]
    assert len(per_attempt) == 3
    assert {a.attribute_name for a in per_attempt} == {
        "retry.attempt_number",
        "webhook.status_code",
        "webhook.attempt_latency_ms",
    }


def test_span_site_constants_verbatim() -> None:
    """AC #5 — span-site constants have byte-exact span-name values."""
    assert SPAN_SITE_HITL_WEBHOOK_DELIVER == "hitl.webhook.deliver"
    assert SPAN_SITE_HITL_WEBHOOK_ATTEMPT == "hitl.webhook.attempt"


# ---------------------------------------------------------------------------
# AC #3 — Pattern-P1 alignment: attribute types + cardinality per §C-OD-32.1
# ---------------------------------------------------------------------------


def test_url_hash_is_string() -> None:
    """AC #3 — `webhook.url_hash` is string (sha256 hex per §C-OD-32.1)."""
    attr = HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA["webhook.url_hash"]
    assert attr.value_type == AttributeValueType.STRING


def test_delivery_attempts_is_int() -> None:
    """AC #3 — `webhook.delivery_attempts` is int."""
    attr = HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA["webhook.delivery_attempts"]
    assert attr.value_type == AttributeValueType.INT


def test_idempotency_key_is_string() -> None:
    """AC #3 — `webhook.idempotency_key` is string."""
    attr = HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA["webhook.idempotency_key"]
    assert attr.value_type == AttributeValueType.STRING


def test_retry_attempt_number_is_int() -> None:
    """AC #3 — `retry.attempt_number` is int (reused from C-CP-03 §3.5)."""
    attr = HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA["retry.attempt_number"]
    assert attr.value_type == AttributeValueType.INT
    assert attr.cardinality == Cardinality.LOW


def test_status_code_is_int() -> None:
    """AC #3 — `webhook.status_code` is int (HTTP status, low cardinality)."""
    attr = HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA["webhook.status_code"]
    assert attr.value_type == AttributeValueType.INT
    assert attr.cardinality == Cardinality.LOW


def test_attempt_latency_ms_is_int() -> None:
    """AC #3 — `webhook.attempt_latency_ms` is int."""
    attr = HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA["webhook.attempt_latency_ms"]
    assert attr.value_type == AttributeValueType.INT


# ---------------------------------------------------------------------------
# AC #2 + AC #4 — WebhookDeliveryAuditPayload fields per §C-OD-32.2
# ---------------------------------------------------------------------------


def test_audit_payload_has_required_cp_sourced_fields() -> None:
    """AC #2 — payload has the 4 `audit_cp_*` CP-sourced sub-namespace fields
    per §C-OD-24.6 discipline."""
    payload = WebhookDeliveryAuditPayload(
        audit_cp_action_id="hitl_webhook:gate-42:idem-1",
        audit_cp_response="delivered",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        url_hash="a" * 64,
        delivery_attempts=1,
        idempotency_key="idem-1",
        final_status_code=200,
        final_attempt_latency_ms=42,
    )
    assert payload.audit_cp_action_id == "hitl_webhook:gate-42:idem-1"
    assert payload.audit_cp_response == "delivered"
    assert payload.audit_cp_timestamp == ""
    assert payload.audit_cp_prior_event_hash == "0" * 64


def test_audit_payload_has_five_webhook_specific_fields() -> None:
    """AC #2 — payload extends with 5 webhook-specific fields per §C-OD-32.2."""
    payload = WebhookDeliveryAuditPayload(
        audit_cp_action_id="hitl_webhook:gate-43:idem-2",
        audit_cp_response="failed",
        audit_cp_timestamp="2026-05-22T12:00:00Z",
        audit_cp_prior_event_hash="b" * 64,
        url_hash="c" * 64,
        delivery_attempts=3,
        idempotency_key="idem-2",
        final_status_code=500,
        final_attempt_latency_ms=1200,
    )
    assert payload.url_hash == "c" * 64
    assert payload.delivery_attempts == 3
    assert payload.idempotency_key == "idem-2"
    assert payload.final_status_code == 500
    assert payload.final_attempt_latency_ms == 1200


def test_audit_payload_accepts_none_for_final_attempt_fields() -> None:
    """AC #2 — `final_status_code` + `final_attempt_latency_ms` accept None
    when no HTTP exchange completed (transport-layer failure)."""
    payload = WebhookDeliveryAuditPayload(
        audit_cp_action_id="hitl_webhook:gate-44:idem-3",
        audit_cp_response="failed",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        url_hash="d" * 64,
        delivery_attempts=2,
        idempotency_key="idem-3",
        final_status_code=None,
        final_attempt_latency_ms=None,
    )
    assert payload.final_status_code is None
    assert payload.final_attempt_latency_ms is None


def test_audit_payload_action_id_carries_idempotency_key_per_ac4() -> None:
    """AC #4 — audit row per delivery attempt: action_id format embeds the
    idempotency_key so concurrent retries are addressable per-attempt at the
    converter; §C-OD-32.2 action_id recipe
    f"hitl_webhook:{parent_action_id}:{idempotency_key}"."""
    payload = WebhookDeliveryAuditPayload(
        audit_cp_action_id="hitl_webhook:parent-99:idem-final",
        audit_cp_response="delivered",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        url_hash="e" * 64,
        delivery_attempts=1,
        idempotency_key="idem-final",
        final_status_code=204,
        final_attempt_latency_ms=15,
    )
    assert payload.idempotency_key in payload.audit_cp_action_id


def test_audit_payload_rejects_extra_fields() -> None:
    """AC #2 — payload extra='forbid' (Pydantic v2 validation discipline)."""
    with pytest.raises(ValidationError):
        WebhookDeliveryAuditPayload(  # type: ignore[call-arg]
            audit_cp_action_id="hitl_webhook:gate-45:idem-x",
            audit_cp_response="delivered",
            audit_cp_timestamp="",
            audit_cp_prior_event_hash="0" * 64,
            url_hash="f" * 64,
            delivery_attempts=1,
            idempotency_key="idem-x",
            final_status_code=200,
            final_attempt_latency_ms=10,
            extra_field="nope",
        )


def test_audit_payload_is_frozen() -> None:
    """AC #2 — payload model is frozen (immutable per audit-record discipline)."""
    payload = WebhookDeliveryAuditPayload(
        audit_cp_action_id="hitl_webhook:gate-46:idem-y",
        audit_cp_response="delivered",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        url_hash="0" * 64,
        delivery_attempts=1,
        idempotency_key="idem-y",
        final_status_code=200,
        final_attempt_latency_ms=10,
    )
    with pytest.raises(ValidationError):
        payload.url_hash = "mutated"  # type: ignore[misc]


def test_attribute_spec_rejects_extra_fields() -> None:
    """AttributeSpec extra='forbid' invariant carries with namespace module."""
    with pytest.raises(ValidationError):
        AttributeSpec(  # type: ignore[call-arg]
            attribute_name="webhook.url_hash",
            value_type=AttributeValueType.STRING,
            cardinality=Cardinality.HIGH,
            span_site=SPAN_SITE_HITL_WEBHOOK_DELIVER,
            extra_field="nope",
        )
