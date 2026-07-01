"""Tests for U-OD-25 — alignment-floor drift detection + emission shape.

Test set per the U-OD-25 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_1.md §3.6.3 + v2.6 §3.6.3 additions). Every acceptance criterion
maps to >=1 test.

Acceptance criteria (C-OD-18 §18.1 / §18.2):
  #1 — AlignmentFloorPrimitive enumerates exactly 4 values; 3 overlap with
       U-OD-23; JUDGE_HUMAN_COHENS_KAPPA anchored at c8-eval-engineer.
  #2 — each primitive carries operator-tunable threshold.
  #3 — DRIFT_DETECTED_EVENT_NAME byte-exact per §18.2.
  #4 — DRIFT_DETECTED_SAMPLING_HEAD_RATE == 1.0 (always-sampled).
  #5 — DriftDetectedEventAttributes declares exactly 4 attributes per §18.2.
  #6 — detect_drift returns Some below threshold, None otherwise.
  #7 — emit_drift_event emits at head=1.0; Err on missing attribute.
  #8 — operator-tunable thresholds deferred to deployment-binding time.
  #9 — re-baselining cycle deferred; this unit emits the drift signal only.
  #10 (v2.6) — SpanRef + EventEmission resolve to the U-OD-04 carrier.
"""

from __future__ import annotations

from harness_od.alignment_floor_drift_detection import (
    DRIFT_DETECTED_ATTRIBUTE_NAMES,
    DRIFT_DETECTED_EVENT_NAME,
    DRIFT_DETECTED_SAMPLING_HEAD_RATE,
    AlignmentFloorPrimitive,
    AlignmentFloorThreshold,
    DriftDetectedEventAttributes,
    ObservationWindow,
    ObservationWindowKind,
    detect_drift,
    emit_drift_event,
)
from harness_od.operator_burden_eval_primitives import OperatorBurdenEvalPrimitive
from harness_od.otel_genai_base import EventEmission, SpanRef
from opentelemetry.sdk.trace import TracerProvider


def _span() -> SpanRef:
    """An OTel-SDK span handle — the U-OD-04 `SpanRef` carrier substrate."""
    return TracerProvider().get_tracer("u-od-25-test").start_span("parent")


def _window() -> ObservationWindow:
    return ObservationWindow(kind=ObservationWindowKind.SAMPLE_WINDOW, sample_window_count=100)


def _threshold(primitive: AlignmentFloorPrimitive, value: float) -> AlignmentFloorThreshold:
    return AlignmentFloorThreshold(
        primitive=primitive,
        threshold_value=value,
        observation_window=_window(),
    )


# --- acceptance #1 — 4 primitives, 3 overlap, kappa anchored --------------


def test_alignment_floor_primitive_cardinality_four() -> None:
    """Acceptance #1 — AlignmentFloorPrimitive enumerates exactly 4 values."""
    assert len(AlignmentFloorPrimitive) == 4


def test_three_overlap_with_operator_burden_eval() -> None:
    """Acceptance #1 — 3 of 4 primitives overlap with U-OD-23's set."""
    drift_values = {p.value for p in AlignmentFloorPrimitive}
    eval_values = {p.value for p in OperatorBurdenEvalPrimitive}
    overlap = drift_values & eval_values
    assert overlap == {
        "cache_hit_rate_alignment_floor",
        "routing_accuracy_holdout",
        "sandbox_tier_routing_accuracy",
    }
    assert len(overlap) == 3


def test_judge_human_kappa_anchored_at_c8() -> None:
    """Acceptance #1 — JUDGE_HUMAN_COHENS_KAPPA is the non-overlapping primitive."""
    assert AlignmentFloorPrimitive.JUDGE_HUMAN_COHENS_KAPPA.value not in {
        p.value for p in OperatorBurdenEvalPrimitive
    }


# --- acceptance #2 / #8 — operator-tunable thresholds ----------------------


def test_each_primitive_carries_operator_tunable_threshold() -> None:
    """Acceptance #2 — each primitive can carry an operator-tunable threshold."""
    for primitive in AlignmentFloorPrimitive:
        threshold = _threshold(primitive, 0.9)
        assert threshold.primitive is primitive
        assert threshold.threshold_value == 0.9


def test_threshold_value_is_operator_tunable() -> None:
    """Acceptance #8 — threshold values are deployment-binding-time tunable."""
    low = _threshold(AlignmentFloorPrimitive.ROUTING_ACCURACY_HOLDOUT, 0.5)
    high = _threshold(AlignmentFloorPrimitive.ROUTING_ACCURACY_HOLDOUT, 0.99)
    assert low.threshold_value != high.threshold_value


# --- acceptance #3 — drift event name byte-exact ---------------------------


def test_drift_event_name_byte_exact() -> None:
    """Acceptance #3 — DRIFT_DETECTED_EVENT_NAME byte-exact per §18.2."""
    assert DRIFT_DETECTED_EVENT_NAME == "gen_ai.eval.alignment_floor.drift_detected"


# --- acceptance #4 — always-sampled head rate ------------------------------


def test_drift_always_sampled_head_one() -> None:
    """Acceptance #4 — DRIFT_DETECTED_SAMPLING_HEAD_RATE == 1.0."""
    assert DRIFT_DETECTED_SAMPLING_HEAD_RATE == 1.0


# --- acceptance #5 — exactly 4 attributes, names byte-exact ----------------


def test_drift_attributes_cardinality_four() -> None:
    """Acceptance #5 — DriftDetectedEventAttributes has exactly 4 fields."""
    assert len(DriftDetectedEventAttributes.model_fields) == 4
    assert len(DRIFT_DETECTED_ATTRIBUTE_NAMES) == 4


def test_drift_attribute_names_byte_exact() -> None:
    """Acceptance #5 — the four §18.2 attribute names byte-exact, in order."""
    assert DRIFT_DETECTED_ATTRIBUTE_NAMES == (
        "gen_ai.eval.primitive",
        "gen_ai.eval.alignment_floor.current",
        "gen_ai.eval.alignment_floor.threshold",
        "gen_ai.eval.alignment_floor.observation_window",
    )


# --- acceptance #6 — detect_drift below / above / at threshold -------------


def test_detect_drift_below_threshold_returns_some() -> None:
    """Acceptance #6 — detect_drift returns event attributes below threshold."""
    threshold = _threshold(AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR, 0.8)
    result = detect_drift(AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR, 0.7, threshold)
    assert isinstance(result, DriftDetectedEventAttributes)
    assert result.current_value == 0.7
    assert result.threshold == 0.8


def test_detect_drift_above_threshold_returns_none() -> None:
    """Acceptance #6 — detect_drift returns None above threshold."""
    threshold = _threshold(AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR, 0.8)
    result = detect_drift(AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR, 0.9, threshold)
    assert result is None


def test_detect_drift_at_threshold_returns_none() -> None:
    """Acceptance #6 — detect_drift returns None at exactly the threshold."""
    threshold = _threshold(AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR, 0.8)
    result = detect_drift(AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR, 0.8, threshold)
    assert result is None


# --- acceptance #7 — emit_drift_event accept / reject ----------------------


def test_emit_drift_event_complete_accept() -> None:
    """Acceptance #7 — emit_drift_event emits at head=1.0 with all attributes."""
    attrs = DriftDetectedEventAttributes(
        primitive=AlignmentFloorPrimitive.JUDGE_HUMAN_COHENS_KAPPA,
        current_value=0.6,
        threshold=0.8,
        observation_window=_window(),
    )
    emission = emit_drift_event(_span(), attrs)
    assert isinstance(emission, EventEmission)
    assert emission.event_name == DRIFT_DETECTED_EVENT_NAME
    assert emission.attribute_count == 4
    assert emission.sampled is True


def test_emit_drift_event_missing_attr_reject() -> None:
    """Acceptance #7 — a DriftDetectedEventAttributes cannot omit a required field.

    The §18.2 four-attribute completeness contract is enforced at the type
    boundary — `DriftDetectedEventAttributes` is frozen with `extra="forbid"`
    and all four fields required; omitting one raises a Pydantic
    `ValidationError`. A complete record always emits.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DriftDetectedEventAttributes(  # type: ignore[call-arg]
            primitive=AlignmentFloorPrimitive.ROUTING_ACCURACY_HOLDOUT,
            current_value=0.5,
            threshold=0.8,
        )


# --- acceptance #9 — drift signal only; re-baselining deferred -------------


def test_detect_drift_emits_signal_not_cycle() -> None:
    """Acceptance #9 — detect_drift returns the drift signal record only.

    Re-baselining cycle execution is deferred to the downstream
    c8-eval-engineer workflow per §18.1; this unit's `detect_drift` returns the
    `DriftDetectedEventAttributes` signal and nothing else.
    """
    threshold = _threshold(AlignmentFloorPrimitive.SANDBOX_TIER_ROUTING_ACCURACY, 0.9)
    result = detect_drift(AlignmentFloorPrimitive.SANDBOX_TIER_ROUTING_ACCURACY, 0.4, threshold)
    assert isinstance(result, DriftDetectedEventAttributes)


# --- acceptance #10 (v2.6) — U-OD-04 carrier resolution --------------------


def test_span_ref_param_resolves_to_u_od_04_carrier() -> None:
    """Acceptance #10 (v2.6) — emit_drift_event's parent_span_ref is U-OD-04 SpanRef."""
    span = _span()
    assert isinstance(span, SpanRef.__value__)  # type: ignore[attr-defined]


def test_event_emission_return_resolves_to_u_od_04_carrier() -> None:
    """Acceptance #10 (v2.6) — emit_drift_event returns the U-OD-04 EventEmission."""
    attrs = DriftDetectedEventAttributes(
        primitive=AlignmentFloorPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR,
        current_value=0.3,
        threshold=0.7,
        observation_window=_window(),
    )
    emission = emit_drift_event(_span(), attrs)
    assert isinstance(emission, EventEmission)
