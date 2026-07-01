"""Operator-burden eval primitive + tail-keep rules — U-CP-51.

Implements C-CP-21 §21.3 (the operator-burden eval primitive + the HITL-span
tail-keep rules).

Declares the `OperatorBurdenEval` record, the `TailKeepRule` record + the
`HITL_TAIL_KEEP_RULES` constant, and the `compute_operator_burden` /
`evaluate_tail_keep` functions. The eval primitive is **passive observation** —
it never modifies workflow execution (acceptance #6).

`Duration` is rendered as `int` (milliseconds — the §21.3 vocabulary; the
concrete `Duration` value type is deferred per spec, the same precedent as
`hitl_placement.py`'s `timeout`). `Span` is the opaque OD-axis span handle —
typed `Any` via the `TailKeepPredicate = Callable[[Any], bool]` alias.

**v2.4-conformance note.** The plan v2.1 acceptance #2 for the
`hitl.gate.evaluated` tail-keep rule references `audit.gate.computed_level >
GATE_NONE` — a plan-invented `audit.gate.*` attribute that the v2.4
verbatim-divergence conformance pass DISSOLVED at U-CP-46 (it is not in CP spec
§20.4). The `hitl.gate.evaluated` rule's keep-predicate is therefore re-anchored
to the conformed §20.6 `hitl.gate.required` boolean (keep when a gate was
required); the `hitl.policy.overridden` rule keeps its always-keep semantics
but is noted as referencing a span the conformed §20.6 schema does not carry
(plan-invented — kept as a descriptive rule, flagged Class 3).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.8 U-CP-51 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §21 C-CP-21 §21.3;
Implementation_Plan_Control_Plane_v2_4.md U-CP-46 v2.4-conformance (audit.gate.*
dissolution).
"""

from __future__ import annotations

from typing import Any

from harness_core import WorkflowID
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import TailKeepPredicate
from harness_cp.hitl_response_palette import HITLResponse

#: `Duration` — millisecond integer (C-CP-21 §21.3 vocabulary; concrete value
#: type deferred per spec — the `hitl_placement.py` `timeout` precedent).
type Duration = int


class OperatorBurdenEval(BaseModel):
    """An operator-burden evaluation over a time window (C-CP-21 §21.3).

    Four fields verbatim per §21.3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocations_per_workflow: int
    """Total HITL invocations divided by distinct workflow count."""

    responses_per_class: dict[HITLResponse, int]
    """Per-response-class counts; cardinality bounded at 4 (the `HITLResponse`
    enum cardinality)."""

    avg_response_latency_ms: float
    workflow_throughput_impact_ms: int
    """Wall-clock displacement attributable to HITL waiting — `(sum of
    latencies) / wallclock duration`."""


class TailKeepRule(BaseModel):
    """One HITL-span tail-keep rule (C-CP-21 §21.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_name: str
    keep_predicate: TailKeepPredicate
    rationale: str


def _keep_gate_when_required(span: Any) -> bool:
    """Keep `hitl.gate.evaluated` iff a gate was required (C-CP-21 §21.3).

    Re-anchored to the v2.4-conformed §20.6 `hitl.gate.required` boolean — the
    v2.1 rule referenced the dissolved `audit.gate.computed_level` attribute.
    Skips spans where no gate was required (reduces high-volume noise).
    """
    return bool(getattr(span, "hitl_gate_required", False))


def _always_keep(span: Any) -> bool:
    """Always-keep predicate — every span of the rule's class is retained."""
    _ = span
    return True


HITL_TAIL_KEEP_RULES: tuple[TailKeepRule, ...] = (
    TailKeepRule(
        span_name="hitl.gate.evaluated",
        keep_predicate=_keep_gate_when_required,
        rationale=(
            "Keep when a HITL gate was required; skip when no gate triggered "
            "(reduces high-volume noise). Re-anchored to the v2.4-conformed "
            "§20.6 hitl.gate.required boolean — the v2.1 rule referenced the "
            "dissolved audit.gate.computed_level attribute (C-CP-21 §21.3)."
        ),
    ),
    TailKeepRule(
        span_name="hitl.invocation.responded",
        keep_predicate=_always_keep,
        rationale=(
            "Always-keep — every operator response is retained for "
            "operator-burden analysis (C-CP-21 §21.3)."
        ),
    ),
    TailKeepRule(
        span_name="hitl.policy.overridden",
        keep_predicate=_always_keep,
        rationale=(
            "Always-keep — override evidence is retained for audit (C-CP-21 "
            "§21.3). Note: the v2.4-conformed §20.6 HITL span schema does not "
            "carry a hitl.policy.overridden span; the rule is descriptive "
            "(plan-invented span name, flagged Class 3)."
        ),
    ),
)
"""The 3 HITL-span tail-keep rules, C-CP-21 §21.3."""


def compute_operator_burden(workflow_id: WorkflowID, time_window: Duration) -> OperatorBurdenEval:
    """Compute the operator-burden evaluation over a time window (§21.3).

    Aggregates HITL invocations + responses + latencies over `time_window`;
    `invocations_per_workflow` divides total invocations by the distinct
    workflow count (acceptance #3). The eval is passive observation — it never
    modifies workflow execution (acceptance #6). This is the eval-primitive
    surface; the concrete span aggregation composes against the OD-axis span
    store at integration time.
    """
    _ = (workflow_id, time_window)
    raise NotImplementedError(
        "compute_operator_burden aggregates HITL spans over the time window; "
        "the CP plan U-CP-51 unit declares the eval-primitive surface "
        "(C-CP-21 §21.3) — span aggregation composes against the OD span store."
    )


def evaluate_tail_keep(span: Any) -> bool:
    """Evaluate the tail-keep decision for a HITL span (C-CP-21 §21.3).

    Resolves the span's `HITL_TAIL_KEEP_RULES` rule by span name and applies
    its keep-predicate. A span whose name matches no rule is not kept by the
    HITL tail-keep discipline (it falls to the base sampling rate).
    """
    span_name = getattr(span, "span_name", None)
    for rule in HITL_TAIL_KEEP_RULES:
        if rule.span_name == span_name:
            return rule.keep_predicate(span)
    return False
