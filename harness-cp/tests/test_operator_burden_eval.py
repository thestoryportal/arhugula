"""Tests for U-CP-51 — operator-burden eval + tail-keep rules (C-CP-21 §21.3).

Acceptance-criterion coverage:
  #1 OperatorBurdenEval 4 fields -> test_operator_burden_eval_four_fields
  #2 tail-keep rules 3 entries   -> test_tail_keep_rules_cardinality_three
  #2 gate-evaluated keep rule    -> test_gate_evaluated_keep_when_required
  #2 invocation.responded keep   -> test_invocation_responded_always_keep
  #2 policy.overridden keep      -> test_policy_overridden_always_keep
  #4 responses_per_class card 4  -> test_responses_per_class_cardinality_four
  #6 eval is passive             -> test_compute_operator_burden_surface
"""

from __future__ import annotations

import pytest
from harness_core import WorkflowID
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.operator_burden_eval import (
    HITL_TAIL_KEEP_RULES,
    OperatorBurdenEval,
    compute_operator_burden,
    evaluate_tail_keep,
)


class _Span:
    def __init__(self, name: str, gate_required: bool = False) -> None:
        self.span_name = name
        self.hitl_gate_required = gate_required


def _rule(name: str):
    return next(r for r in HITL_TAIL_KEEP_RULES if r.span_name == name)


def test_operator_burden_eval_four_fields() -> None:
    """#1 — OperatorBurdenEval declares exactly four fields per §21.3."""
    assert set(OperatorBurdenEval.model_fields) == {
        "invocations_per_workflow",
        "responses_per_class",
        "avg_response_latency_ms",
        "workflow_throughput_impact_ms",
    }


def test_tail_keep_rules_cardinality_three() -> None:
    """#2 — HITL_TAIL_KEEP_RULES declares exactly three entries."""
    assert len(HITL_TAIL_KEEP_RULES) == 3
    assert {r.span_name for r in HITL_TAIL_KEEP_RULES} == {
        "hitl.gate.evaluated",
        "hitl.invocation.responded",
        "hitl.policy.overridden",
    }


def test_gate_evaluated_keep_when_required() -> None:
    """#2 — hitl.gate.evaluated kept iff a gate was required."""
    rule = _rule("hitl.gate.evaluated")
    assert rule.keep_predicate(_Span("hitl.gate.evaluated", gate_required=True))
    assert not rule.keep_predicate(_Span("hitl.gate.evaluated", gate_required=False))
    # via evaluate_tail_keep dispatch
    assert evaluate_tail_keep(_Span("hitl.gate.evaluated", gate_required=True))


def test_invocation_responded_always_keep() -> None:
    """#2 — hitl.invocation.responded is always-keep."""
    assert _rule("hitl.invocation.responded").keep_predicate(_Span("hitl.invocation.responded"))
    assert evaluate_tail_keep(_Span("hitl.invocation.responded"))


def test_policy_overridden_always_keep() -> None:
    """#2 — hitl.policy.overridden is always-keep (override audit evidence)."""
    assert _rule("hitl.policy.overridden").keep_predicate(_Span("hitl.policy.overridden"))


def test_responses_per_class_cardinality_four() -> None:
    """#4 — responses_per_class is bounded at the 4 HITLResponse values."""
    eval_ = OperatorBurdenEval(
        invocations_per_workflow=2,
        responses_per_class={r: 1 for r in HITLResponse},
        avg_response_latency_ms=12.5,
        workflow_throughput_impact_ms=300,
    )
    assert len(eval_.responses_per_class) == 4
    assert set(eval_.responses_per_class) == set(HITLResponse)


def test_compute_operator_burden_surface() -> None:
    """#6 — compute_operator_burden declares the passive eval-primitive surface."""
    with pytest.raises(NotImplementedError):
        compute_operator_burden(WorkflowID("w0"), 60_000)
    # An unmatched span name is not kept by the HITL tail-keep discipline.
    assert evaluate_tail_keep(_Span("workflow.start")) is False
