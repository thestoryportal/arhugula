"""7 `audit.*` attrs + per-persona-tier emission table + HITL-span schema — U-CP-46.

Implements C-CP-20 §20.4 (the 7 `audit.*` span attributes), §20.5 (the
per-persona-tier emission discipline), and §20.6 (the 4-span HITL-event span
schema).

Declares `AUDIT_NAMESPACE_SCHEMA` (7 entries, §20.4 verbatim),
`HITL_SPAN_NAMESPACE_SCHEMA` (4 entries — one §20.6 span each, with its
per-span attribute list), and `PERSONA_TIER_AUDIT_EMISSION` (3 entries, §20.5
verbatim).

This unit lands against the **v2.4-conformed body** — the v2.1/v2.3 body
carried a plan-invented `audit.gate.*` / `audit.policy.*` namespace; the v2.4
verbatim-divergence conformance pass dissolved it and conformed all three
schemas to the cited spec sections (`.harness/verbatim_audit_cp_plan.md` §4A
cluster resolution). Schema declaration is purely descriptive — emission
mechanics are owned by the OD plan Session 4 D6.

Authority: Implementation_Plan_Control_Plane_v2_4.md §2.7 U-CP-46 (v2.4
verbatim-divergence amendment); Spec_Control_Plane_v1_2.md §20 C-CP-20
§20.4/§20.5/§20.6; ADR-D5 v1.3 §1.4.1 + §1.8.
"""

from __future__ import annotations

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.schema_attribute_enums import AttributeValueType, Cardinality


class AuditAttributeSchema(BaseModel):
    """One `audit.*` span-attribute schema entry (C-CP-20 §20.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    always_emitted_at: str
    """The persona-tier emission condition per §20.4."""


AUDIT_NAMESPACE_SCHEMA: tuple[AuditAttributeSchema, ...] = (
    AuditAttributeSchema(
        attribute_name="audit.signature.sha256",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.PER_REQUEST,
        always_emitted_at=(
            "multi-tenant-compliance (§20.1 row 3); absent at solo-developer; "
            "opt-in at team-binding"
        ),
    ),
    AuditAttributeSchema(
        attribute_name="audit.signature.prior_hash",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.PER_REQUEST,
        always_emitted_at=(
            "team-binding (§20.1 row 2) and multi-tenant-compliance (row 3); "
            "absent at solo-developer"
        ),
    ),
    AuditAttributeSchema(
        attribute_name="audit.actor.id",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.MEDIUM,
        always_emitted_at="all three persona tiers",
    ),
    AuditAttributeSchema(
        attribute_name="audit.signature.value",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.PER_REQUEST,
        always_emitted_at="team-binding (opt-in) + multi-tenant-compliance (always)",
    ),
    AuditAttributeSchema(
        attribute_name="audit.signature.algorithm",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        always_emitted_at="when audit.signature.value emitted",
    ),
    AuditAttributeSchema(
        attribute_name="audit.signature.key_id",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.LOW,
        always_emitted_at="when audit.signature.value emitted",
    ),
    AuditAttributeSchema(
        attribute_name="audit.signature.key_period",
        value_type=AttributeValueType.INT,
        cardinality=Cardinality.LOW,
        always_emitted_at="when audit.signature.value emitted",
    ),
)
"""The 7 `audit.*` span attributes, C-CP-20 §20.4 verbatim."""


class HITLSpanSchema(BaseModel):
    """One §20.6 HITL-event span schema entry — a span name + its attributes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_name: str
    span_attributes: tuple[str, ...]
    """The per-span attribute list per C-CP-20 §20.6."""


HITL_SPAN_NAMESPACE_SCHEMA: tuple[HITLSpanSchema, ...] = (
    HITLSpanSchema(
        span_name="hitl.gate.evaluated",
        span_attributes=(
            "hitl.gate.level",
            "hitl.gate.persona_tier",
            "hitl.gate.required",
        ),
    ),
    HITLSpanSchema(
        span_name="hitl.invocation.opened",
        span_attributes=(
            "hitl.gate.level",
            "hitl.invocation.placement",
            "hitl.invocation.handoff_context_size_bytes",
            "hitl.invocation.audit_ledger_entry_id",
        ),
    ),
    HITLSpanSchema(
        span_name="hitl.invocation.responded",
        span_attributes=(
            "hitl.response.class",
            "hitl.response.latency_ms",
            "hitl.response.summary_hash",
        ),
    ),
    HITLSpanSchema(
        span_name="hitl.invocation.timed_out",
        span_attributes=(
            "hitl.timeout.duration_ms",
            "hitl.timeout.degradation_mode_applied",
        ),
    ),
)
"""The 4 HITL-event span schemas, C-CP-20 §20.6 verbatim."""


class PersonaTierEmissionRow(BaseModel):
    """One per-persona-tier audit-emission row (C-CP-20 §20.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    emitted_audit_attributes: frozenset[str]
    """The attributes always emitted at this tier per §20.5."""

    optional_audit_attributes: frozenset[str]
    """The attributes optionally emitted at this tier (signature-posture opt-in)."""


_SIGNATURE_OPTIONAL: frozenset[str] = frozenset(
    {
        "audit.signature.sha256",
        "audit.signature.value",
        "audit.signature.algorithm",
        "audit.signature.key_id",
        "audit.signature.key_period",
    }
)

PERSONA_TIER_AUDIT_EMISSION: tuple[PersonaTierEmissionRow, ...] = (
    PersonaTierEmissionRow(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        emitted_audit_attributes=frozenset({"audit.actor.id"}),
        optional_audit_attributes=frozenset(),
    ),
    PersonaTierEmissionRow(
        persona_tier=PersonaTier.TEAM_BINDING,
        emitted_audit_attributes=frozenset({"audit.actor.id", "audit.signature.prior_hash"}),
        optional_audit_attributes=_SIGNATURE_OPTIONAL,
    ),
    PersonaTierEmissionRow(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        emitted_audit_attributes=frozenset(a.attribute_name for a in AUDIT_NAMESPACE_SCHEMA),
        optional_audit_attributes=frozenset(),
    ),
)
"""The 3 per-persona-tier emission rows, C-CP-20 §20.5 verbatim. Emission is
monotonic along solo-developer -> team-binding -> multi-tenant-compliance."""
