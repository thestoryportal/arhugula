"""Tests for U-OD-23 — operator-burden eval primitives (C-OD-17 §17.1/§17.2).

Test set per the U-OD-23 §3.6.1 `Tests:` field — covers acceptance #1-#10.
"""

from __future__ import annotations

import pytest
from harness_od.operator_burden_eval_primitives import (
    EVAL_EMISSION_CONTRACT,
    EVAL_PRIMITIVE_DECLARATIONS,
    ComputationKind,
    EmissionContractViolation,
    OperatorBurdenEvalPrimitive,
    emit_eval_as_child_span,
    reject_span_event_only_emission,
)
from harness_od.otel_genai_base import ChildSpanRef
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

_BY_PRIMITIVE = {d.primitive: d for d in EVAL_PRIMITIVE_DECLARATIONS}


def _parent_span() -> Span:
    """An OTel-SDK parent span — the U-OD-04 `SpanRef` carrier substrate."""
    return TracerProvider().get_tracer("u-od-23-test").start_span("parent")


# --- acceptance #1 — five-primitive enum -----------------------------------


def test_eval_primitive_cardinality_five() -> None:
    """§17.1 — `OperatorBurdenEvalPrimitive` enumerates exactly 5 values."""
    assert len(OperatorBurdenEvalPrimitive) == 5


def test_eval_primitive_canonical_order() -> None:
    """§3.6.1 signature block — the 5 primitives in canonical declaration order."""
    assert tuple(OperatorBurdenEvalPrimitive) == (
        OperatorBurdenEvalPrimitive.EXPECTED_HITL_INVOCATIONS_PER_SESSION,
        OperatorBurdenEvalPrimitive.EXPECTED_SANDBOX_VIOLATIONS_PER_SESSION,
        OperatorBurdenEvalPrimitive.SANDBOX_TIER_ROUTING_ACCURACY,
        OperatorBurdenEvalPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR,
        OperatorBurdenEvalPrimitive.ROUTING_ACCURACY_HOLDOUT,
    )
    assert tuple(d.primitive for d in EVAL_PRIMITIVE_DECLARATIONS) == tuple(
        OperatorBurdenEvalPrimitive
    )


# --- acceptance #2 — five declarations with §17.1 content ------------------


def test_eval_declarations_cardinality_five() -> None:
    """§17.1 — `EVAL_PRIMITIVE_DECLARATIONS` declares exactly 5 entries."""
    assert len(EVAL_PRIMITIVE_DECLARATIONS) == 5
    assert len(_BY_PRIMITIVE) == 5


def test_hitl_source_c_cp_20_section_20_6() -> None:
    """§17.1 row 1 — HITL invocations primitive sources C-CP-20 §20.6,
    span class `hitl.invocation.responded`, ADR-D5 v1.3 §1.8."""
    decl = _BY_PRIMITIVE[OperatorBurdenEvalPrimitive.EXPECTED_HITL_INVOCATIONS_PER_SESSION]
    assert decl.declaration_site == "C-CP-20 §20.6"
    assert decl.source_span_class == "hitl.invocation.responded"
    assert decl.source_adr == "ADR-D5 v1.3 §1.8"


def test_sandbox_violations_source_c_as_15_section_15_4() -> None:
    """§17.1 row 2 — sandbox violations primitive sources C-AS-15 §15.4,
    span class `sandbox.violation`, ADR-D2 v1.1 §1.8."""
    decl = _BY_PRIMITIVE[OperatorBurdenEvalPrimitive.EXPECTED_SANDBOX_VIOLATIONS_PER_SESSION]
    assert decl.declaration_site == "C-AS-15 §15.4"
    assert decl.source_span_class == "sandbox.violation"
    assert decl.source_adr == "ADR-D2 v1.1 §1.8"


# --- acceptance #3 — cache-hit-rate formula byte-exact ---------------------


def test_cache_hit_rate_formula_byte_exact() -> None:
    """Acceptance #3 — `CACHE_HIT_RATE_ALIGNMENT_FLOOR.computation_formula` is
    the AS spec §14.2 / U-AS-31 canonical-attribute-name ratio, verbatim."""
    decl = _BY_PRIMITIVE[OperatorBurdenEvalPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR]
    assert decl.computation_formula == (
        "anthropic.cache_read_input_tokens / (anthropic.cache_read_input_tokens "
        "+ anthropic.cache_creation_input_tokens)"
    )


# --- acceptance #4 — sandbox-tier-routing meta-judge over 5-axis tunable ----


def test_sandbox_tier_routing_holdout_evaluable_true() -> None:
    """Acceptance #4 — `SANDBOX_TIER_ROUTING_ACCURACY` is a holdout-evaluable
    meta-judge over the T-perm-1 5-axis multiplicative tunable (§17.1 row 3)."""
    decl = _BY_PRIMITIVE[OperatorBurdenEvalPrimitive.SANDBOX_TIER_ROUTING_ACCURACY]
    assert decl.holdout_evaluable is True
    assert decl.computation_kind is ComputationKind.HOLDOUT_META_JUDGE_RATIO
    # the 5-axis multiplicative tunable per §17.1 row 3.
    assert len(decl.rollup_dimensions) == 5


def test_routing_accuracy_holdout_evaluable_true() -> None:
    """§17.1 row 5 — `ROUTING_ACCURACY_HOLDOUT` is holdout-evaluable."""
    decl = _BY_PRIMITIVE[OperatorBurdenEvalPrimitive.ROUTING_ACCURACY_HOLDOUT]
    assert decl.holdout_evaluable is True
    assert decl.computation_kind is ComputationKind.HOLDOUT_META_JUDGE_RATIO


# --- acceptance #2 — ComputationKind distribution (signature comment) ------


def test_counter_rollup_count_two() -> None:
    """§3.6.1 signature — `COUNTER_ROLLUP` covers primitives 1, 2."""
    counter = [
        d
        for d in EVAL_PRIMITIVE_DECLARATIONS
        if d.computation_kind is ComputationKind.COUNTER_ROLLUP
    ]
    assert len(counter) == 2


def test_holdout_meta_judge_count_two() -> None:
    """§3.6.1 signature — `HOLDOUT_META_JUDGE_RATIO` covers primitives 3, 5."""
    holdout = [
        d
        for d in EVAL_PRIMITIVE_DECLARATIONS
        if d.computation_kind is ComputationKind.HOLDOUT_META_JUDGE_RATIO
    ]
    assert len(holdout) == 2


def test_ratio_rollup_count_one() -> None:
    """§3.6.1 signature — `RATIO_ROLLUP` covers primitive 4."""
    ratio = [
        d for d in EVAL_PRIMITIVE_DECLARATIONS if d.computation_kind is ComputationKind.RATIO_ROLLUP
    ]
    assert len(ratio) == 1


# --- acceptance #5 / #8 — EvalEmissionContract -----------------------------


def test_eval_emission_child_span_required() -> None:
    """Acceptance #5 — `EVAL_EMISSION_CONTRACT.child_span_emission_required`
    is `True` per §17.2 verbatim."""
    assert EVAL_EMISSION_CONTRACT.child_span_emission_required is True
    assert EVAL_EMISSION_CONTRACT.span_volume_tradeoff_accepted is True


def test_eval_emission_applies_all_cells() -> None:
    """Acceptance #8 — `applies_at_all_cells` is `True` per §17.2 — the
    commitment binds every cell."""
    assert EVAL_EMISSION_CONTRACT.applies_at_all_cells is True


# --- acceptance #6 / #7 — emit / reject ------------------------------------


def test_emit_as_child_span_accept() -> None:
    """Acceptance #6 — `emit_eval_as_child_span` returns a `ChildSpanRef`
    (the OTel-SDK span handle carried at U-OD-04) for child-span emission."""
    child = emit_eval_as_child_span(
        _parent_span(),
        OperatorBurdenEvalPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR,
        0.85,
    )
    # ChildSpanRef aliases the OTel-SDK Span handle.
    assert isinstance(child, ChildSpanRef.__value__)


def test_reject_span_event_only() -> None:
    """Acceptance #7 — `reject_span_event_only_emission` raises
    `EmissionContractViolation` for span-event-only emission."""
    with pytest.raises(EmissionContractViolation):
        reject_span_event_only_emission(
            _parent_span(),
            OperatorBurdenEvalPrimitive.ROUTING_ACCURACY_HOLDOUT,
            0.9,
        )


# --- acceptance #9 / #10 — cross-axis edges + span-volume tradeoff ----------


def test_cross_axis_edges_to_as_and_cp() -> None:
    """Acceptance #9 — the AS + CP cross-axis declaration sites are recorded
    byte-exact on the primitive declarations (Pattern P1 alignment)."""
    sites = {d.declaration_site for d in EVAL_PRIMITIVE_DECLARATIONS if d.declaration_site}
    assert "C-CP-20 §20.6" in sites  # CP cross-axis
    assert "C-AS-15 §15.4" in sites  # AS cross-axis
    assert "C-AS-14 §14.2" in sites  # AS cross-axis
    # acceptance #10 — span-volume tradeoff accepted per c8-eval-engineer.
    assert EVAL_EMISSION_CONTRACT.span_volume_tradeoff_accepted is True
    assert "c8-eval-engineer" in EVAL_EMISSION_CONTRACT.rationale
