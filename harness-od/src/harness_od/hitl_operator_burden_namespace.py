"""C-OD-33 `hitl.operator_burden.*` canonical namespace schema +
OperatorBurdenAuditPayload.

U-OD-54 — 4-OD-E canonical schemas cluster. Declares the 4-attribute
`hitl.operator_burden.*` span namespace canonical authority for the
`OperatorBurdenEvaluator` emitter (runtime spec v1.13 §14.10 producer). Also
declares the `OperatorBurdenAuditPayload` field-set used by the
`cp_audit_to_od_audit` converter at
`harness-cxa/src/harness_cxa/cp_audit_conversion.py` when the converter
encounters an `operator_burden:`-prefixed CP action_id (per CXA v2.6 §0.3 +
U-CP-72 discriminator-table extension to 8 prefixes).

**4 attributes on the `hitl.operator_burden.evaluated` span site** per OD spec
v1.8 §C-OD-33.1:

| Attribute                                          | Type   | Cardinality |
|----------------------------------------------------|--------|-------------|
| `hitl.operator_burden.cumulative_invocations`      | int    | medium      |
| `hitl.operator_burden.window_ms`                   | int    | low         |
| `hitl.operator_burden.persona_tier`                | enum   | low (4)     |
| `hitl.operator_burden.degrade`                     | bool   | low (2)     |

**Pattern-P1 alignment** with runtime spec v1.13 §14.10.3 producer-side:
attribute names byte-exact match the §14.10.3 span emission table.

**Audit-ledger projection** per §C-OD-33.2: the converter writes an
`OperatorBurdenAuditPayload` via `operator_burden:` action_id prefix per CXA
v2.6 §0.3 + U-CP-72 expansion. Payload extends per §C-OD-24.6 CP-sourced
sub-namespace discipline (`audit.cp.*` tagging): the 4 `audit_cp_*` fields are
the common CP-sourced field-set shared with PauseResume / MCP-trust / Validator
/ WebhookDelivery payloads at §30.2 / §31.2 / §29.2 / §32.2.

**AC #4 — degradation_mode populated when degrade=true.** The payload's
`degradation_mode: str | None` field is None when `degrade=False`; populated
with the active degradation mode identifier when `degrade=True`. The converter
discipline enforces this at write-time.

**Sampling discipline.** Burden evaluations head=1.0 only on `degrade=true`;
otherwise head=0.1 (per runtime spec v1.13 §14.10.3 — tail-keep on degradation).

Note: per the U-OD-50 + U-OD-52 precedent, this payload class is a STANDALONE
Pydantic projection container that the converter uses to compose
`AuditPayload.audit_namespace_attrs` dict — literal Python
`@dataclass(frozen=True) class Foo(AuditPayload)` inheritance is NOT what the
spec requires; the §24.6 sub-namespace tagging discipline is what's preserved.

Authority: OD spec v1.8 §C-OD-33 (NEW at Closure Arc Phase A.5); plan unit
U-OD-54 (OD plan v2.14 §1 cluster 4-OD-E, preserved at v2.15).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict

# ----------------------------------------------------------------------------
# Span-site identifier (1 site per §C-OD-33.1)
# ----------------------------------------------------------------------------

SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED: Final[str] = "hitl.operator_burden.evaluated"


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
    """Byte-exact attribute name per §C-OD-33.1 + Pattern-P1 alignment with
    runtime spec v1.13 §14.10.3 producer site."""

    value_type: AttributeValueType
    """Value-type discriminator per `harness_core.AttributeValueType`."""

    cardinality: Cardinality
    """Cardinality classification per `harness_core.Cardinality`."""

    span_site: str
    """`SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED` — only 1 span site
    per §C-OD-33.1."""


# ----------------------------------------------------------------------------
# 4-attribute canonical schema (§C-OD-33.1 verbatim)
# ----------------------------------------------------------------------------


HITL_OPERATOR_BURDEN_SPAN_NAMESPACE_SCHEMA: Mapping[str, AttributeSpec] = {
    "hitl.operator_burden.cumulative_invocations": AttributeSpec(
        attribute_name="hitl.operator_burden.cumulative_invocations",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.MEDIUM,
        span_site=SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED,
    ),
    "hitl.operator_burden.window_ms": AttributeSpec(
        attribute_name="hitl.operator_burden.window_ms",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED,
    ),
    "hitl.operator_burden.persona_tier": AttributeSpec(
        attribute_name="hitl.operator_burden.persona_tier",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED,
    ),
    "hitl.operator_burden.degrade": AttributeSpec(
        attribute_name="hitl.operator_burden.degrade",
        value_type=AttributeValueType.BOOL,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_HITL_OPERATOR_BURDEN_EVALUATED,
    ),
}
"""The 4 `hitl.operator_burden.*` span attributes per §C-OD-33.1 verbatim.

Keyed by attribute name for O(1) Pattern-P1 alignment lookup at the
`cp_audit_to_od_audit` converter + at consumer-side downstream filtering.
"""


# ----------------------------------------------------------------------------
# OperatorBurdenAuditPayload (§C-OD-33.2 audit-ledger projection)
# ----------------------------------------------------------------------------


class OperatorBurdenAuditPayload(BaseModel):
    """Audit-ledger projection emitted on a `hitl.operator_burden.evaluated`
    span (§C-OD-33.2).

    Written by `cp_audit_to_od_audit` converter at
    `harness-cxa/src/harness_cxa/cp_audit_conversion.py` via `operator_burden:`
    action_id prefix per CXA v2.6 §0.3 + U-CP-72 expansion (8 prefixes).

    AC #4 from OD plan v2.14 §1 U-OD-54: `degradation_mode` is populated when
    `degrade=True`. The converter discipline enforces the (degrade=True ⇒
    degradation_mode is not None) invariant at write-time; the schema permits
    None at construction so non-degrade evaluations can compose the payload
    without manufacturing a placeholder mode.

    Extends the C-OD-24.6 CP-sourced sub-namespace discipline: the 4
    `audit_cp_*` fields are the common CP-sourced field-set; the 5 trailing
    fields are burden-specific. At serialization the payload composes into
    `AuditPayload.audit_namespace_attrs` as `audit.cp.*` + `audit.operator_burden.*`
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
    """f"operator_burden:{workflow_id}:{window_end_epoch_ms}" per §C-OD-33.2 +
    CXA v2.6 §0.3 + U-CP-72 expansion."""

    audit_cp_response: str
    """`"burden_evaluated"` | `"burden_degraded"` per §C-OD-33.2."""

    audit_cp_timestamp: str
    """ISO-8601 OR "" at MVP per v1.7 §24.4 NOTE 8a-iii."""

    audit_cp_prior_event_hash: str
    """SHA-256 hex (64) OR "0"*64 at MVP."""

    # Burden-specific fields per §C-OD-33.2:
    cumulative_invocations: int
    """Count of HITL invocations in the evaluation window (matches span
    `hitl.operator_burden.cumulative_invocations` attribute)."""

    window_ms: int
    """Evaluation window width in milliseconds (matches span
    `hitl.operator_burden.window_ms` attribute)."""

    persona_tier: str
    """PersonaTier enum value (matches span `hitl.operator_burden.persona_tier`
    attribute)."""

    degrade: bool
    """True iff the evaluator decided to degrade the operator surface in this
    window; matches span `hitl.operator_burden.degrade` attribute."""

    degradation_mode: str | None
    """Populated when `degrade=True` per AC #4; None when degrade=False. The
    active degradation mode identifier consumed by downstream HITL placement
    composers (e.g. CP plan v2.16 U-CP-NN). At MVP the mode-identifier value
    space is operator-defined and not enum-constrained at this carrier."""
