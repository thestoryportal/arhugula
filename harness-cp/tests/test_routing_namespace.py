"""Tests for U-CP-01 — `routing.*` namespace + per-attribute schema (C-CP-01 §1.4).

Acceptance-criterion coverage:
  #1 4-attribute set per §1.4   -> test_routing_namespace_cardinality_four,
                                   test_routing_attributes_match_spec_verbatim
  #2 inherits the LLM inference span  -> test_routing_inherits_from_llm_inference
  #3 no independent sampling    -> test_no_independent_sampling_discipline
  #4 D6 ingestion out of scope  -> structural (no emission/ingestion surface)
"""

from __future__ import annotations

from harness_cp.routing_namespace import ROUTING_NAMESPACE_SCHEMA

# The 4 §1.4 attribute names, verbatim from Spec_Control_Plane_v1_2.md §1.4.
_SPEC_ATTRIBUTES = {
    "routing.provider",
    "routing.model",
    "routing.layer",
    "routing.binding_rationale",
}


def test_routing_namespace_cardinality_four() -> None:
    """Acceptance #1 — exactly four `routing.*` attributes."""
    assert len(ROUTING_NAMESPACE_SCHEMA) == 4


def test_routing_attributes_match_spec_verbatim() -> None:
    """Acceptance #1 — attribute names match C-CP-01 §1.4 verbatim."""
    assert {a.attribute_name for a in ROUTING_NAMESPACE_SCHEMA} == _SPEC_ATTRIBUTES


def test_routing_inherits_from_llm_inference() -> None:
    """Acceptance #2 — every attribute cites the LLM inference span parent.

    Per AS spec v1.7 §14.1 alias-term convention: the spec-side parent-anchor
    is "the LLM inference span" (alias for the span opened by the runtime LLM
    dispatcher composer per OD spec v1.12 §C-OD-04 §4.1, runtime name
    `{operation} {model}`). Pre-v1.7 the literal `llm.inference` was used;
    R3 apply-pass refactored to alias term per
    `.harness/class_1_fork_genai_span_name_four_way_drift.md` §7.4.3.
    """
    for attr in ROUTING_NAMESPACE_SCHEMA:
        assert "the LLM inference span" in attr.inherited_from
        assert "OTel GenAI semconv 1.41.0" in attr.inherited_from


def test_no_independent_sampling_discipline() -> None:
    """Acceptance #3 — the namespace carries no sampling field of its own.

    `routing.*` inherits sampling from the parent span per C-CP-24 §24.1.C;
    `RoutingAttributeSchema` declares no sampling-rate / disposition field.
    """
    fields = set(ROUTING_NAMESPACE_SCHEMA[0].__class__.model_fields)
    assert fields == {
        "attribute_name",
        "value_type",
        "cardinality",
        "inherited_from",
    }
    assert not any("sampl" in f for f in fields)
