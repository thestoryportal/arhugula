"""`topology.*` + `subagent.*` span attribute namespace schemas — U-CP-31.

Implements C-CP-14 §14.2 — the two D4-anchored span-attribute namespace
declarations. Declares `TopologyAttributeSchema` + `TOPOLOGY_NAMESPACE_SCHEMA`
(10 attributes), `SubAgentAttributeSchema` + `SUBAGENT_NAMESPACE_SCHEMA`
(7 attributes), and `SubAgentResultStatus` (3-value enum).

This is a pure **declaration** unit — it declares the attribute-name tables and
their value-type/cardinality typing. No span is emitted here; span emission is
a downstream runtime concern. The `emitted_on` field carries the span name a
given attribute is emitted on, as a plain string (per the U-CP-31 plan
signature — `emitted_on : string // span name`).

The 10 `topology.*` and 7 `subagent.*` attribute names are the C-CP-14 §14.2
verbatim set per the U-CP-31 acceptance criteria; they are exported for D6
ingestion at U-CP-54 §24.1.A (rows 7 and 8).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-31 (preserved
verbatim through v2.6; `[U-CP-00b]` edge-add per v2.6 §0.10 — `AttributeValue-
Type`/`Cardinality` resolved from the U-CP-00b carrier); Spec_Control_Plane_
v1_2.md §14 C-CP-14 §14.2 (preserved verbatim into v1.3); ADR-D4 v1.1;
ADR-D6 v1.2.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.schema_attribute_enums import AttributeValueType, Cardinality


class SubAgentResultStatus(StrEnum):
    """The 4 terminal sub-agent result statuses (C-CP-14 §14.2).

    Cardinality 4 — the `subagent.result_status` attribute domain. `PAUSED` was
    added at B-HIERARCHICAL-PAUSE (R-FS-1, runtime spec §14.7.2 step 7 child-PAUSED
    mapping): a recursive child sub-workflow returning `RunStatus.PAUSED` is surfaced
    (not swallowed) so the parent fan-out captures its cursor + pauses; the
    `subagent.span` records `paused` for that distinct fourth outcome (child-pause was
    not anticipated by the v1 cardinality-3 schema — the same gap the arc closes).
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CASCADE_CANCELLED = "cascade-cancelled"
    PAUSED = "paused"


class TopologyAttributeSchema(BaseModel):
    """One `topology.*` span attribute (C-CP-14 §14.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    emitted_on: str
    """The span name this attribute is emitted on (plan signature: `string`)."""


class SubAgentAttributeSchema(BaseModel):
    """One `subagent.*` span attribute (C-CP-14 §14.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    emitted_on: str
    """The span name this attribute is emitted on (plan signature: `string`)."""


_VT = AttributeValueType
_C = Cardinality

# The topology.* attributes are emitted on the topology-dispatch span.
_TOPOLOGY_SPAN = "topology.dispatch"
# The subagent.* attributes are emitted on the per-sub-agent span.
_SUBAGENT_SPAN = "subagent.execution"


# --- topology.* namespace (C-CP-14 §14.2 — 10 attributes) -------------------

TOPOLOGY_NAMESPACE_SCHEMA: tuple[TopologyAttributeSchema, ...] = tuple(
    TopologyAttributeSchema(
        attribute_name=name,
        value_type=vt,
        cardinality=card,
        emitted_on=_TOPOLOGY_SPAN,
    )
    for name, vt, card in (
        ("topology.pattern", _VT.ENUM_REF, _C.LOW),
        ("topology.fan_out_cap", _VT.INT, _C.MEDIUM),
        ("topology.cascade_policy", _VT.ENUM_REF, _C.LOW),
        ("topology.workload_class", _VT.ENUM_REF, _C.LOW),
        ("topology.concurrent_token_budget_at_dispatch", _VT.INT, _C.HIGH),
        ("topology.results_collected", _VT.INT, _C.MEDIUM),
        ("topology.results_failed", _VT.INT, _C.MEDIUM),
        ("topology.cascade_applied", _VT.BOOL, _C.LOW),
        ("topology.synthesis_token_budget", _VT.INT, _C.HIGH),
        ("topology.cascade_decision_audit_ledger_id", _VT.STRING, _C.PER_REQUEST),
    )
)
"""The 10 `topology.*` attributes per C-CP-14 §14.2 verbatim."""


# --- subagent.* namespace (C-CP-14 §14.2 — 7 attributes) --------------------

SUBAGENT_NAMESPACE_SCHEMA: tuple[SubAgentAttributeSchema, ...] = tuple(
    SubAgentAttributeSchema(
        attribute_name=name,
        value_type=vt,
        cardinality=card,
        emitted_on=_SUBAGENT_SPAN,
    )
    for name, vt, card in (
        ("subagent.span.id", _VT.STRING, _C.PER_REQUEST),
        ("subagent.parent_span_id", _VT.STRING, _C.PER_REQUEST),
        ("subagent.result_status", _VT.ENUM_REF, _C.LOW),
        ("subagent.request_blocked_by_budget", _VT.BOOL, _C.LOW),
        ("subagent.tokens_in", _VT.INT, _C.HIGH),
        ("subagent.tokens_out", _VT.INT, _C.HIGH),
        ("subagent.cached_tokens_in", _VT.INT, _C.HIGH),
    )
)
"""The 7 `subagent.*` attributes per C-CP-14 §14.2 verbatim."""
