"""`routing.*` span-attribute namespace + per-attribute schema — U-CP-01.

Implements C-CP-01 §1.4 (run-event attribution at call surface). Declares the
`RoutingAttributeSchema` record and the 4-entry `ROUTING_NAMESPACE_SCHEMA`.

The `routing.*` attributes attach to the LLM inference span (per AS spec v1.7
§14.1 alias-term convention; actual runtime span-name `{operation} {model}`
per OD spec v1.12 §C-OD-04 §4.1) emitted per AS C-AS-14 §14.2 (`anthropic.*`
namespace + cross-family analogs) and are namespace-rooted at `routing.*` per
the OTel GenAI semconv extension. The namespace carries **no independent
sampling discipline** — it inherits from the parent LLM inference span per
C-CP-24 §24.1.C. D6 ingestion is out of scope
at this unit; OD plan Session 4 ingests via the U-CP-54 namespace export
manifest.

The `value_type` / `cardinality` discriminators resolve to `harness-core`'s
`AttributeValueType` / `Cardinality` (carrier U-CP-00b, re-homed to
`harness-core` per the U-AS-31 fork); the inline enums declared at this unit's
v2.1/v2.3 Signatures block were stripped at v2.6 §0.10. `cardinality` is a
sanctioned plan-internal characterization field (operator decision Q-R4-2 —
keep, no spec edit).

Authority: Implementation_Plan_Control_Plane_v2_4.md §2.1 U-CP-01 (v2.4 §4A
verbatim-divergence conformance — 4-attribute set conformed to spec §1.4;
v2.6 §0.10 strips the inline enums, `Depends on: [U-CP-00b]`; v2.7 U-CP-00b
narrowed to the 2 utility enums); Spec_Control_Plane_v1_2.md §1 C-CP-01 §1.4
(preserved verbatim into v1.3); ADR-F1 v1.2 §Decision + §Consequences (a).
"""

from __future__ import annotations

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class RoutingAttributeSchema(BaseModel):
    """One `routing.*` span attribute (C-CP-01 §1.4 attribute table row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    """Sanctioned plan-internal characterization field per Q-R4-2 (keep, no
    spec edit) — §1.4 prose characterizes attribute cardinality."""

    inherited_from: str
    """Parent-span citation — every `routing.*` attribute inherits from the
    LLM inference span (per AS spec v1.7 §14.1 alias-term convention; actual
    runtime span-name format `{operation} {model}` per OD spec v1.12
    §C-OD-04 §4.1, byte-exact to OTel GenAI semconv 1.41.0). (acceptance #2)."""


_LLM_INFERENCE_PARENT = (
    "the LLM inference span per OTel GenAI semconv 1.41.0 "
    "(AS spec v1.7 §14.1 alias-term; OD spec v1.12 §C-OD-04 §4.1 format owner)"
)

# --- Registry population (spec C-CP-01 §1.4 attribute table) ----------------

ROUTING_NAMESPACE_SCHEMA: tuple[RoutingAttributeSchema, ...] = (
    RoutingAttributeSchema(
        attribute_name="routing.provider",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        inherited_from=_LLM_INFERENCE_PARENT,
    ),
    RoutingAttributeSchema(
        attribute_name="routing.model",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.LOW,
        inherited_from=_LLM_INFERENCE_PARENT,
    ),
    RoutingAttributeSchema(
        attribute_name="routing.layer",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        inherited_from=_LLM_INFERENCE_PARENT,
    ),
    RoutingAttributeSchema(
        attribute_name="routing.binding_rationale",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.PER_REQUEST,
        inherited_from=_LLM_INFERENCE_PARENT,
    ),
)
"""The 4 `routing.*` attributes per C-CP-01 §1.4 verbatim — `routing.provider`,
`routing.model`, `routing.layer`, `routing.binding_rationale`."""
