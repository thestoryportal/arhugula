"""Tests for U-OD-26 — eval-vs-runtime-gate distinction via `gen_ai.eval.kind`.

Each test maps to a U-OD-26 acceptance criterion (C-OD-18 §18.3).
"""

from __future__ import annotations

import pytest
from harness_od.eval_vs_runtime_gate import (
    EVAL_KIND_ATTRIBUTE_NAME,
    EVAL_SPAN_SHAPES,
    EvalKindDiscriminator,
    EvalShapeViolation,
    EvalSpanRouting,
    SamplingPostureF18,
    classify_eval_span,
    validate_eval_span_routing,
)
from harness_od.otel_genai_base import SpanAttributes, SpanRef
from opentelemetry.sdk.trace import TracerProvider


def _span() -> SpanRef:
    """An OTel-SDK span handle — the U-OD-04 `SpanRef` carrier substrate."""
    return TracerProvider().get_tracer("u-od-26-test").start_span("parent")


def test_eval_kind_cardinality_two() -> None:
    """Acceptance #1 — `EvalKindDiscriminator` enumerates exactly 2 values."""
    assert len(list(EvalKindDiscriminator)) == 2


def test_eval_kind_attribute_name_byte_exact() -> None:
    """Acceptance #2 — `EVAL_KIND_ATTRIBUTE_NAME` is `gen_ai.eval.kind` byte-exact."""
    assert EVAL_KIND_ATTRIBUTE_NAME == "gen_ai.eval.kind"


def test_eval_kind_member_values_byte_exact() -> None:
    """Acceptance #1 — member values are the §18.3 set verbatim."""
    assert EvalKindDiscriminator.INLINE_GATE.value == "inline_gate"
    assert EvalKindDiscriminator.OFFLINE_JUDGE.value == "offline_judge"


def test_eval_span_shapes_cardinality_two() -> None:
    """Acceptance #3 — `EVAL_SPAN_SHAPES` declares exactly 2 entries."""
    assert len(EVAL_SPAN_SHAPES) == 2
    assert set(EVAL_SPAN_SHAPES) == set(EvalKindDiscriminator)


def test_inline_gate_shape_per_section_18_3_row_1() -> None:
    """Acceptance #4 — the `INLINE_GATE` shape matches §18.3 row 1."""
    shape = EVAL_SPAN_SHAPES[EvalKindDiscriminator.INLINE_GATE]
    assert shape.sampling_posture is SamplingPostureF18.ALWAYS_SAMPLED_IF_FAILURE_BASE_RATE_IF_PASS
    assert shape.source_declaration_ref == "C-CP-21 §21.5"
    assert shape.failure_routing == "C-CP-21 §21.6 + C-AS-04 §4.2"


def test_offline_judge_shape_per_section_18_3_row_2() -> None:
    """Acceptance #5 — the `OFFLINE_JUDGE` shape matches §18.3 row 2."""
    shape = EVAL_SPAN_SHAPES[EvalKindDiscriminator.OFFLINE_JUDGE]
    assert shape.sampling_posture is SamplingPostureF18.SEPARATE_CHILD_SPAN_PER_U_OD_23
    assert shape.source_declaration_ref == "U-OD-23 (C-OD-17 §17.2)"
    assert shape.failure_routing is None


# --- classify_eval_span -----------------------------------------------------


def test_classify_inline_gate_recognized() -> None:
    """Acceptance #6 — `classify_eval_span` recognizes `inline_gate`."""
    attrs: SpanAttributes = {EVAL_KIND_ATTRIBUTE_NAME: "inline_gate"}
    assert classify_eval_span(attrs) is EvalKindDiscriminator.INLINE_GATE


def test_classify_offline_judge_recognized() -> None:
    """Acceptance #6 — `classify_eval_span` recognizes `offline_judge`."""
    attrs: SpanAttributes = {EVAL_KIND_ATTRIBUTE_NAME: "offline_judge"}
    assert classify_eval_span(attrs) is EvalKindDiscriminator.OFFLINE_JUDGE


def test_classify_absent_returns_none() -> None:
    """Acceptance #6 — `classify_eval_span` returns `None` when the attr is absent."""
    attrs: SpanAttributes = {"gen_ai.operation.name": "chat"}
    assert classify_eval_span(attrs) is None


def test_classify_unrecognized_value_returns_none() -> None:
    """Acceptance #6 — an unrecognized discriminator value returns `None`."""
    attrs: SpanAttributes = {EVAL_KIND_ATTRIBUTE_NAME: "not_a_valid_kind"}
    assert classify_eval_span(attrs) is None


def test_classify_span_attributes_param_resolves_to_u_od_04_carrier() -> None:
    """Acceptance #10 (v2.6) — `attrs` is the U-OD-04 `SpanAttributes` alias."""
    # SpanAttributes aliases the OTel attribute map (Mapping[str, AttributeValue]).
    attrs: SpanAttributes = {EVAL_KIND_ATTRIBUTE_NAME: "inline_gate"}
    assert classify_eval_span(attrs) is EvalKindDiscriminator.INLINE_GATE


# --- validate_eval_span_routing ---------------------------------------------


def test_validate_inline_gate_with_validator_fail_accept() -> None:
    """Acceptance #7 — a conformant `inline_gate` routing is accepted."""
    routing = EvalSpanRouting(
        emitted_as_child_span=False,
        has_validator_fail_attributes=True,
        has_operator_burden_eval_reference=False,
    )
    assert validate_eval_span_routing(EvalKindDiscriminator.INLINE_GATE, _span(), routing) is None


def test_validate_inline_gate_as_child_span_reject() -> None:
    """Acceptance #7 — an `inline_gate` emitted as a child span is rejected."""
    routing = EvalSpanRouting(
        emitted_as_child_span=True,
        has_validator_fail_attributes=True,
        has_operator_burden_eval_reference=False,
    )
    with pytest.raises(EvalShapeViolation):
        validate_eval_span_routing(EvalKindDiscriminator.INLINE_GATE, _span(), routing)


def test_validate_inline_gate_lacking_validator_fail_reject() -> None:
    """Acceptance #7 — an `inline_gate` lacking `validator.fail.*` is rejected."""
    routing = EvalSpanRouting(
        emitted_as_child_span=False,
        has_validator_fail_attributes=False,
        has_operator_burden_eval_reference=False,
    )
    with pytest.raises(EvalShapeViolation):
        validate_eval_span_routing(EvalKindDiscriminator.INLINE_GATE, _span(), routing)


def test_validate_offline_judge_as_child_span_accept() -> None:
    """Acceptance #7 — a conformant `offline_judge` routing is accepted."""
    routing = EvalSpanRouting(
        emitted_as_child_span=True,
        has_validator_fail_attributes=False,
        has_operator_burden_eval_reference=True,
    )
    assert validate_eval_span_routing(EvalKindDiscriminator.OFFLINE_JUDGE, _span(), routing) is None


def test_validate_offline_judge_as_span_event_reject() -> None:
    """Acceptance #7 — an `offline_judge` NOT emitted as a child span is rejected."""
    routing = EvalSpanRouting(
        emitted_as_child_span=False,
        has_validator_fail_attributes=False,
        has_operator_burden_eval_reference=True,
    )
    with pytest.raises(EvalShapeViolation):
        validate_eval_span_routing(EvalKindDiscriminator.OFFLINE_JUDGE, _span(), routing)


def test_validate_offline_judge_lacking_eval_reference_reject() -> None:
    """Acceptance #7 — an `offline_judge` lacking an eval-primitive ref is rejected."""
    routing = EvalSpanRouting(
        emitted_as_child_span=True,
        has_validator_fail_attributes=False,
        has_operator_burden_eval_reference=False,
    )
    with pytest.raises(EvalShapeViolation):
        validate_eval_span_routing(EvalKindDiscriminator.OFFLINE_JUDGE, _span(), routing)


def test_distinction_non_mergeable() -> None:
    """Acceptance #8 — no single routing satisfies both inline_gate AND offline_judge.

    inline_gate requires `emitted_as_child_span == False`; offline_judge
    requires `emitted_as_child_span == True`. The two invariants are disjoint
    on that field, so no routing conforms to both shapes.
    """
    for emitted_as_child in (True, False):
        routing = EvalSpanRouting(
            emitted_as_child_span=emitted_as_child,
            has_validator_fail_attributes=True,
            has_operator_burden_eval_reference=True,
        )
        inline_ok = True
        offline_ok = True
        try:
            validate_eval_span_routing(EvalKindDiscriminator.INLINE_GATE, _span(), routing)
        except EvalShapeViolation:
            inline_ok = False
        try:
            validate_eval_span_routing(EvalKindDiscriminator.OFFLINE_JUDGE, _span(), routing)
        except EvalShapeViolation:
            offline_ok = False
        assert not (inline_ok and offline_ok)
