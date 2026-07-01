"""C-OD-29 `validator.*` canonical namespace schema + ValidatorEscalationAuditPayload.

U-OD-50 — 4-OD-E canonical schemas cluster. Declares the 11-attribute
`validator.*` span namespace canonical authority for the `ValidatorFramework`
emitter homed at CP (per the D6 ingestion pattern: CP emits, OD ratifies).
Also declares the `ValidatorEscalationAuditPayload` field-set used by the
`cp_audit_to_od_audit` converter at `harness-cxa/src/harness_cxa/cp_audit_conversion.py`
when the converter encounters a `validator:`-prefixed CP action_id (per CXA
v2.6 §0.3 discriminator table).

**11 attributes across 4 span sites** per OD spec v1.8 §C-OD-29.1:

| Site                       | Attribute count |
|----------------------------|-----------------|
| `validator.evaluate` outer | 3               |
| `validator.fail` event     | 4               |
| `validator.revalidation`   | 2               |
| `validator.escalation`     | 2               |

(§C-OD-29.1 footer cites "5 at validator.fail" — minor internal-arithmetic
drift; the table itself enumerates 4 + the §29.1 header authoritatively says
"11 attributes". This impl honors the 4 + 11. Class 3 drift filed in commit.)

**Pattern-P1 alignment** with CP spec v1.10 §25.5 producer-side: attribute
names byte-exact match the §25.5 span emission table; consumers MAY
disambiguate ESCALATE_HITL via `validator.outcome` per §29.1 + the bijective
mapping at CP plan v2.16 U-CP-60.

**Audit-ledger projection** per §C-OD-29.2: when `validator.outcome ∈
{ESCALATE, PERMANENT_FAIL, OPERATOR_BURDEN_EXCEEDED}`, the converter writes a
`ValidatorEscalationAuditPayload` via `validator:` action_id prefix per CXA
v2.6 §0.3. The payload extends per C-OD-24.6 CP-sourced sub-namespace
discipline (`audit.cp.*` tagging) — the 4 `audit_cp_*` fields are the common
CP-sourced field-set shared with PauseResume / MCP-trust / HITL-webhook /
operator-burden audit payloads at §30.2 / §31.2 / §32.2 / §33.2.

Authority: OD spec v1.8 §C-OD-29 (NEW at Closure Arc Phase A.5); plan unit
U-OD-50 (OD plan v2.14 §1 cluster 4-OD-E).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict

# ----------------------------------------------------------------------------
# Span-site identifiers (4 sites per §C-OD-29.1)
# ----------------------------------------------------------------------------

SPAN_SITE_VALIDATOR_EVALUATE: Final[str] = "validator.evaluate"
SPAN_SITE_VALIDATOR_FAIL: Final[str] = "validator.fail"
SPAN_SITE_VALIDATOR_REVALIDATION: Final[str] = "validator.revalidation"
SPAN_SITE_VALIDATOR_ESCALATION: Final[str] = "validator.escalation"


# ----------------------------------------------------------------------------
# AttributeSpec carrier (per U-OD-50 plan signature)
# ----------------------------------------------------------------------------


class AttributeSpec(BaseModel):
    """One canonical-namespace span attribute declaration.

    Pattern-P1 alignment carrier — consumers verify byte-exact attribute name
    + value type + cardinality + span site against the OD canonical schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    """Byte-exact attribute name per §C-OD-29.1 + Pattern-P1 alignment with
    CP spec v1.10 §25.5 producer site."""

    value_type: AttributeValueType
    """Value-type discriminator per `harness_core.AttributeValueType`."""

    cardinality: Cardinality
    """Cardinality classification per `harness_core.Cardinality`."""

    span_site: str
    """One of the 4 span-site constants (`SPAN_SITE_VALIDATOR_*`)."""


# ----------------------------------------------------------------------------
# 11-attribute canonical schema (§C-OD-29.1 verbatim)
# ----------------------------------------------------------------------------


VALIDATOR_SPAN_NAMESPACE_SCHEMA: Mapping[str, AttributeSpec] = {
    # --- validator.evaluate outer (3 attrs) ---
    "step.id": AttributeSpec(
        attribute_name="step.id",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_VALIDATOR_EVALUATE,
    ),
    "validator.outcome": AttributeSpec(
        attribute_name="validator.outcome",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_VALIDATOR_EVALUATE,
    ),
    "validator.burden_count_cumulative": AttributeSpec(
        attribute_name="validator.burden_count_cumulative",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_VALIDATOR_EVALUATE,
    ),
    # --- validator.fail event (4 attrs) ---
    "validator.fail.class": AttributeSpec(
        attribute_name="validator.fail.class",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_VALIDATOR_FAIL,
    ),
    "validator.fail.detail_hash": AttributeSpec(
        attribute_name="validator.fail.detail_hash",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_VALIDATOR_FAIL,
    ),
    "validator.fail.next_action": AttributeSpec(
        attribute_name="validator.fail.next_action",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_VALIDATOR_FAIL,
    ),
    "validator.fail.escalation_owed": AttributeSpec(
        attribute_name="validator.fail.escalation_owed",
        value_type=AttributeValueType.BOOL,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_VALIDATOR_FAIL,
    ),
    # --- validator.revalidation event (2 attrs) ---
    "validator.revalidation.payload_size_bytes": AttributeSpec(
        attribute_name="validator.revalidation.payload_size_bytes",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_VALIDATOR_REVALIDATION,
    ),
    "validator.revalidation.attempt_number": AttributeSpec(
        attribute_name="validator.revalidation.attempt_number",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_VALIDATOR_REVALIDATION,
    ),
    # --- validator.escalation event (2 attrs) ---
    "validator.escalation.parent_hitl_span_id": AttributeSpec(
        attribute_name="validator.escalation.parent_hitl_span_id",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.HIGH,
        span_site=SPAN_SITE_VALIDATOR_ESCALATION,
    ),
    "validator.escalation.fail_class": AttributeSpec(
        attribute_name="validator.escalation.fail_class",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_VALIDATOR_ESCALATION,
    ),
}
"""The 11 `validator.*` span attributes per §C-OD-29.1 verbatim.

Keyed by attribute name for O(1) Pattern-P1 alignment lookup at the
`cp_audit_to_od_audit` converter + at consumer-side downstream filtering.
"""


# ----------------------------------------------------------------------------
# ValidatorEscalationAuditPayload (§C-OD-29.2 audit-ledger projection)
# ----------------------------------------------------------------------------


class ValidatorEscalationAuditPayload(BaseModel):
    """Audit-ledger projection emitted when validator.outcome ∈
    {ESCALATE, PERMANENT_FAIL, OPERATOR_BURDEN_EXCEEDED} (§C-OD-29.2).

    Written by `cp_audit_to_od_audit` converter at
    `harness-cxa/src/harness_cxa/cp_audit_conversion.py` via `validator:`
    action_id prefix per CXA v2.6 §0.3 discriminator table.

    Extends the C-OD-24.6 CP-sourced sub-namespace discipline: the 4
    `audit_cp_*` fields are the common CP-sourced field-set; the 4 trailing
    fields are validator-specific. At serialization the payload composes into
    `AuditPayload.audit_namespace_attrs` as `audit.cp.*` + `audit.validator.*`
    sub-namespace keys.

    Note: §C-OD-29.2 declares this as "extending AuditPayload" in spec sample
    code, but the v1.8 change-note explicitly says "no field-projection table
    change at §24" — so this class is a STANDALONE projection container that
    the converter uses to compose AuditPayload.audit_namespace_attrs dict.
    Literal Python `class Foo(AuditPayload)` inheritance is NOT what the spec
    requires; the §24.6 sub-namespace tagging discipline is what's preserved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # CP-sourced inherited per §C-OD-24.6 sub-namespace discipline:
    audit_cp_action_id: str
    """f"validator:{parent_action_id}:{fail_class.value}" per CXA v2.6 §0.3."""

    audit_cp_response: str
    """ValidatorOutcome enum value (per CP spec v1.10 §25.2)."""

    audit_cp_timestamp: str
    """ISO-8601 OR "" at MVP per v1.7 §24.4 NOTE 8a-iii."""

    audit_cp_prior_event_hash: str
    """SHA-256 hex (64) OR "0"*64 at MVP."""

    # Validator-specific fields per §C-OD-29.2:
    validator_fail_class: str
    """ValidatorFailClass enum value (NEW at C-CP-25; not the C-CP-21
    ValidatorRetryExitClass — see CP plan v2.16 §1)."""

    validator_fail_detail_hash: str
    """SHA-256 hex (64) of fail-reason text per CP spec v1.10 §25.8 deferred-
    to-discretion shape."""

    validator_next_action: str
    """ValidatorNextAction enum value (PROCEED / RETRY / ESCALATE_HITL / ABORT)."""

    validator_escalation_owed: bool
    """True when next_action == ESCALATE_HITL."""
