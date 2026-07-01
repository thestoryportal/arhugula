"""C-OD-32 `hitl.webhook.*` canonical namespace schema + WebhookDeliveryAuditPayload.

U-OD-53 — 4-OD-E canonical schemas cluster. Declares the 6-attribute
`hitl.webhook.*` span namespace canonical authority for the
`WebhookDeliveryComposer` emitter (runtime spec v1.13 §14.10 producer). Also
declares the `WebhookDeliveryAuditPayload` field-set used by the
`cp_audit_to_od_audit` converter at
`harness-cxa/src/harness_cxa/cp_audit_conversion.py` when the converter
encounters a `hitl_webhook:`-prefixed CP action_id (per CXA v2.6 §0.3 +
U-CP-72 discriminator-table extension to 8 prefixes).

**6 attributes across 2 span sites** per OD spec v1.8 §C-OD-32.1:

| Site                        | Attribute count |
|-----------------------------|-----------------|
| `hitl.webhook.deliver` outer | 3 (url_hash, delivery_attempts, idempotency_key) |
| `hitl.webhook.attempt`      | 3 (retry.attempt_number, status_code, attempt_latency_ms) |

`retry.attempt_number` is byte-exact reused from C-CP-03 §3.5 `retry.*`
canonical namespace; OD authority over `hitl.webhook.*` does not redefine the
retry namespace — the attribute appears at the `hitl.webhook.attempt` span
site per §C-OD-32.1 row 4.

**Pattern-P1 alignment** with runtime spec v1.13 §14.10.3 producer-side:
attribute names byte-exact match the §14.10.3 span emission table.

**Audit-ledger projection** per §C-OD-32.2: one audit row per delivery attempt
(NOT per delivery) — AC #4. The converter writes a `WebhookDeliveryAuditPayload`
via `hitl_webhook:` action_id prefix per CXA v2.6 §0.3 + U-CP-72 expansion.
Payload extends per §C-OD-24.6 CP-sourced sub-namespace discipline (`audit.cp.*`
tagging): the 4 `audit_cp_*` fields are the common CP-sourced field-set shared
with PauseResume / MCP-trust / Validator / OperatorBurden payloads at §30.2 /
§31.2 / §29.2 / §33.2.

**Sampling discipline.** Webhook spans head=1.0 (always-sampled — HITL delivery
audit-critical per §C-OD-32.3).

Note: per the U-OD-50 + U-OD-52 precedent, this payload class is a STANDALONE
Pydantic projection container that the converter uses to compose
`AuditPayload.audit_namespace_attrs` dict — literal Python
`@dataclass(frozen=True) class Foo(AuditPayload)` inheritance is NOT what the
spec requires; the §24.6 sub-namespace tagging discipline is what's preserved.

Authority: OD spec v1.8 §C-OD-32 (NEW at Closure Arc Phase A.5); plan unit
U-OD-53 (OD plan v2.14 §1 cluster 4-OD-E, preserved at v2.15).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict

# ----------------------------------------------------------------------------
# Span-site identifiers (2 sites per §C-OD-32.1)
# ----------------------------------------------------------------------------

SPAN_SITE_HITL_WEBHOOK_DELIVER: Final[str] = "hitl.webhook.deliver"
SPAN_SITE_HITL_WEBHOOK_ATTEMPT: Final[str] = "hitl.webhook.attempt"


# ----------------------------------------------------------------------------
# AttributeSpec carrier (mirrors U-OD-50 / U-OD-52 namespace shape)
# ----------------------------------------------------------------------------


class AttributeSpec(BaseModel):
    """One canonical-namespace span attribute declaration.

    Pattern-P1 alignment carrier — consumers verify byte-exact attribute name
    + value type + cardinality + span site against the OD canonical schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    """Byte-exact attribute name per §C-OD-32.1 + Pattern-P1 alignment with
    runtime spec v1.13 §14.10.3 producer site."""

    value_type: AttributeValueType
    """Value-type discriminator per `harness_core.AttributeValueType`."""

    cardinality: Cardinality
    """Cardinality classification per `harness_core.Cardinality`."""

    span_site: str
    """`SPAN_SITE_HITL_WEBHOOK_DELIVER` (outer) or
    `SPAN_SITE_HITL_WEBHOOK_ATTEMPT` (per-attempt) per §C-OD-32.1."""


# ----------------------------------------------------------------------------
# 6-attribute canonical schema (§C-OD-32.1 verbatim)
# ----------------------------------------------------------------------------


HITL_WEBHOOK_SPAN_NAMESPACE_SCHEMA: Mapping[str, AttributeSpec] = {
    # Outer span site — 3 attributes (cardinality 1 per delivery)
    "webhook.url_hash": AttributeSpec(
        attribute_name="webhook.url_hash",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_HITL_WEBHOOK_DELIVER,
    ),
    "webhook.delivery_attempts": AttributeSpec(
        attribute_name="webhook.delivery_attempts",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_HITL_WEBHOOK_DELIVER,
    ),
    "webhook.idempotency_key": AttributeSpec(
        attribute_name="webhook.idempotency_key",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_HITL_WEBHOOK_DELIVER,
    ),
    # Per-attempt span site — 3 attributes (cardinality 1 per attempt)
    "retry.attempt_number": AttributeSpec(
        attribute_name="retry.attempt_number",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_HITL_WEBHOOK_ATTEMPT,
    ),
    "webhook.status_code": AttributeSpec(
        attribute_name="webhook.status_code",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_HITL_WEBHOOK_ATTEMPT,
    ),
    "webhook.attempt_latency_ms": AttributeSpec(
        attribute_name="webhook.attempt_latency_ms",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.MEDIUM,
        span_site=SPAN_SITE_HITL_WEBHOOK_ATTEMPT,
    ),
}
"""The 6 `hitl.webhook.*` span attributes per §C-OD-32.1 verbatim.

Keyed by attribute name for O(1) Pattern-P1 alignment lookup at the
`cp_audit_to_od_audit` converter + at consumer-side downstream filtering.

`retry.attempt_number` is byte-exact reused from C-CP-03 §3.5 — OD's authority
over `hitl.webhook.*` does not redefine the retry namespace; the attribute
appears at the `hitl.webhook.attempt` span site per §C-OD-32.1 row 4.
"""


# ----------------------------------------------------------------------------
# WebhookDeliveryAuditPayload (§C-OD-32.2 audit-ledger projection)
# ----------------------------------------------------------------------------


class WebhookDeliveryAuditPayload(BaseModel):
    """Audit-ledger projection emitted per delivery attempt on a
    `hitl.webhook.deliver` outer span (§C-OD-32.2).

    AC #4 from OD plan v2.14 §1 U-OD-53: audit row per delivery attempt (NOT
    per delivery) — each attempt under the `hitl.webhook.attempt` span site
    composes one payload. `delivery_attempts` (outer-span attribute) carries
    the cumulative attempt count; `final_status_code` + `final_attempt_latency_ms`
    are populated by the last-attempt projection.

    Written by `cp_audit_to_od_audit` converter at
    `harness-cxa/src/harness_cxa/cp_audit_conversion.py` via `hitl_webhook:`
    action_id prefix per CXA v2.6 §0.3 + U-CP-72 expansion (8 prefixes).

    Extends the C-OD-24.6 CP-sourced sub-namespace discipline: the 4
    `audit_cp_*` fields are the common CP-sourced field-set; the 5 trailing
    fields are webhook-specific. At serialization the payload composes into
    `AuditPayload.audit_namespace_attrs` as `audit.cp.*` + `audit.hitl_webhook.*`
    sub-namespace keys.

    Note: per the U-OD-50 + U-OD-52 precedent, this class is a STANDALONE
    projection container that the converter uses to compose
    `AuditPayload.audit_namespace_attrs` dict — literal Python
    `@dataclass(frozen=True) class Foo(AuditPayload)` inheritance is NOT what
    the spec requires; the §24.6 sub-namespace tagging discipline is what's
    preserved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # CP-sourced inherited per §C-OD-24.6 sub-namespace discipline:
    audit_cp_action_id: str
    """f"hitl_webhook:{parent_action_id}:{idempotency_key}" per §C-OD-32.2 +
    CXA v2.6 §0.3 + U-CP-72 expansion."""

    audit_cp_response: str
    """`"delivered"` | `"failed"` per §C-OD-32.2."""

    audit_cp_timestamp: str
    """ISO-8601 OR "" at MVP per v1.7 §24.4 NOTE 8a-iii."""

    audit_cp_prior_event_hash: str
    """SHA-256 hex (64) OR "0"*64 at MVP."""

    # Webhook-specific fields per §C-OD-32.2:
    url_hash: str
    """SHA-256 hex (64) of the webhook URL — high-cardinality identity carrier."""

    delivery_attempts: int
    """Cumulative attempt count for this delivery (matches outer-span
    `webhook.delivery_attempts` attribute)."""

    idempotency_key: str
    """The webhook delivery idempotency key — partitions independent deliveries
    for the same parent_action_id at the converter."""

    final_status_code: int | None
    """HTTP status code on the last delivery attempt; None if no response was
    obtained (transport-layer failure with no HTTP exchange)."""

    final_attempt_latency_ms: int | None
    """Wall-clock latency of the last delivery attempt in milliseconds; None
    when `final_status_code` is None (no exchange completed)."""
