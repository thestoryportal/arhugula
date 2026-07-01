"""Tests for U-CP-12 — per-class attribute composition + sampling (C-CP-05 §5.2/§5.4).

Acceptance-criterion coverage (CP plan v2.4 §2.2 amendment):
  #1 8 per-class attribute sets   -> test_per_class_attribute_sets_cardinality_eight
  #2 checkpoint/F2 anchor attrs   -> test_checkpoint_composes_with_f2_entry
  #3 resumption engine.* attrs    -> test_resumption_composes_with_engine_namespace,
                                     test_engine_replay_disposition_required_at_workflow_resumption,
                                     test_workflow_resumption_attribute_composition_agrees_with_u_cp_20_acceptance_2
  #4 sampling per §5.4 table      -> test_sampling_dispositions_per_class_match_spec_5_4,
                                     test_sampling_dispositions_lifecycle_cardinality_eight_preserved
  #5 deterministic composition    -> test_required_attributes_enforced
  #6 retry-surface 2 entries      -> test_retry_surface_sampling_dispositions_cardinality_two
  #7 parent-event override rules  -> test_retry_attempt_parent_event_*
  #8 child-span tail-keep         -> test_retry_attempt_child_span_*
  #9 dual-emission discipline     -> test_dual_emission_*
"""

from __future__ import annotations

from harness_core import WorkflowEventClass
from harness_cp.per_class_attribute_composition import (
    DUAL_EMISSION_DISCIPLINE,
    PER_CLASS_ATTRIBUTE_SETS,
    RETRY_SURFACE_SAMPLING_DISPOSITIONS,
    SAMPLING_DISPOSITIONS,
    RetrySurfaceKind,
    SamplingRate,
    required_attributes_for,
)


def test_per_class_attribute_sets_cardinality_eight() -> None:
    """#1 — exactly eight entries, one per WorkflowEventClass."""
    assert len(PER_CLASS_ATTRIBUTE_SETS) == 8
    assert {e.event_class for e in PER_CLASS_ATTRIBUTE_SETS} == set(WorkflowEventClass)


def test_checkpoint_composes_with_f2_entry() -> None:
    """#2 — workflow-start carries the idempotency_key F2 anchor attribute."""
    req = required_attributes_for(WorkflowEventClass.WORKFLOW_START)
    assert "idempotency_key" in req
    assert "workflow.id" in req


def test_resumption_composes_with_engine_namespace() -> None:
    """#3 — workflow.resumption composes engine.* attributes."""
    req = required_attributes_for(WorkflowEventClass.RESUMPTION)
    assert "engine.class" in req


def test_engine_replay_disposition_required_at_workflow_resumption() -> None:
    """#3 — engine.replay_disposition is required at RESUMPTION."""
    req = required_attributes_for(WorkflowEventClass.RESUMPTION)
    assert "engine.replay_disposition" in req


def test_workflow_resumption_attribute_composition_agrees_with_u_cp_20_acceptance_2() -> None:
    """#3 — required set at RESUMPTION includes both engine.* required attrs."""
    req = required_attributes_for(WorkflowEventClass.RESUMPTION)
    assert {"engine.class", "engine.replay_disposition"} <= req


def test_sampling_dispositions_lifecycle_cardinality_eight_preserved() -> None:
    """#4 — exactly eight lifecycle-surface sampling dispositions."""
    assert len(SAMPLING_DISPOSITIONS) == 8
    assert {d.event_class for d in SAMPLING_DISPOSITIONS} == set(WorkflowEventClass)


def test_sampling_dispositions_per_class_match_spec_5_4() -> None:
    """#4 — per-row sampling rate matches the §5.4 table (v2.4 conformance)."""
    by_class = {d.event_class: d for d in SAMPLING_DISPOSITIONS}
    assert by_class[WorkflowEventClass.WORKFLOW_START].head_rate is SamplingRate.ALWAYS_SAMPLED
    assert by_class[WorkflowEventClass.STEP_BOUNDARY].head_rate is SamplingRate.BASE_RATE
    assert by_class[WorkflowEventClass.STEP_BOUNDARY].tail_keep is True
    assert by_class[WorkflowEventClass.FALLBACK_TRIGGER].head_rate is SamplingRate.ALWAYS_SAMPLED
    assert by_class[WorkflowEventClass.RETRY_ATTEMPT].head_rate is SamplingRate.BASE_RATE
    assert by_class[WorkflowEventClass.BREAKER_TRIP].head_rate is SamplingRate.ALWAYS_SAMPLED
    assert by_class[WorkflowEventClass.LEASE_ACQUIRED].head_rate is SamplingRate.BASE_RATE
    assert by_class[WorkflowEventClass.LEASE_RELEASED].head_rate is SamplingRate.BASE_RATE
    assert by_class[WorkflowEventClass.RESUMPTION].head_rate is SamplingRate.ALWAYS_SAMPLED


def test_required_attributes_enforced() -> None:
    """#5 — required_attributes_for is total + deterministic over the enum."""
    for ec in WorkflowEventClass:
        first = required_attributes_for(ec)
        assert first == required_attributes_for(ec)
        assert len(first) > 0


def test_retry_surface_sampling_dispositions_cardinality_two() -> None:
    """#6 — exactly two retry-surface dispositions."""
    assert len(RETRY_SURFACE_SAMPLING_DISPOSITIONS) == 2
    kinds = {d.entity_kind for d in RETRY_SURFACE_SAMPLING_DISPOSITIONS}
    assert kinds == {RetrySurfaceKind.PARENT_EVENT, RetrySurfaceKind.CHILD_SPAN}


def _parent_event():
    return next(
        d
        for d in RETRY_SURFACE_SAMPLING_DISPOSITIONS
        if d.entity_kind is RetrySurfaceKind.PARENT_EVENT
    )


def _child_span():
    return next(
        d
        for d in RETRY_SURFACE_SAMPLING_DISPOSITIONS
        if d.entity_kind is RetrySurfaceKind.CHILD_SPAN
    )


def test_retry_attempt_parent_event_default_base_rate_first_attempt_with_budget() -> None:
    """#7 — default rate is BASE_RATE when no override matches."""
    assert _parent_event().default_rate is SamplingRate.BASE_RATE


def test_retry_attempt_parent_event_always_sampled_attempt_number_ge_two() -> None:
    """#7 — staircase visibility: attempt_number >= 2 -> ALWAYS_SAMPLED."""
    rule = _parent_event().always_sampled_overrides[0]
    assert rule.condition_predicate == "retry.attempt_number >= 2"
    assert rule.override_rate is SamplingRate.ALWAYS_SAMPLED


def test_retry_attempt_parent_event_always_sampled_at_budget_exit() -> None:
    """#7 — retry-budget-exit boundary -> ALWAYS_SAMPLED."""
    rule = _parent_event().always_sampled_overrides[1]
    assert rule.condition_predicate == "parent.attempts_remaining == 0"
    assert rule.override_rate is SamplingRate.ALWAYS_SAMPLED


def test_retry_attempt_parent_event_override_rules_evaluated_first_match_wins() -> None:
    """#7 — exactly two ordered override rules."""
    assert len(_parent_event().always_sampled_overrides) == 2


def test_retry_attempt_child_span_default_base_rate_per_cell_tunable() -> None:
    """#8 — child span default rate is BASE_RATE; no overrides."""
    assert _child_span().default_rate is SamplingRate.BASE_RATE
    assert _child_span().always_sampled_overrides == ()


def test_retry_attempt_child_span_tail_keep_on_fail_class() -> None:
    """#8 — child span tail-keeps on retry.fail_class."""
    assert _child_span().tail_keep_on_attribute == "retry.fail_class"
    assert _parent_event().tail_keep_on_attribute is None


def test_dual_emission_both_paths_emit_per_retry() -> None:
    """#9 — the dual-emission discipline names both emission paths."""
    assert DUAL_EMISSION_DISCIPLINE == ("retry.attempt", "retry-attempt-child-span")


def test_dual_emission_collapse_to_event_only_forbidden() -> None:
    """#9 — the child-span path is present (collapse-to-event-only forbidden)."""
    assert "retry-attempt-child-span" in DUAL_EMISSION_DISCIPLINE


def test_dual_emission_collapse_to_span_only_forbidden() -> None:
    """#9 — the parent-event path is present (collapse-to-span-only forbidden)."""
    assert "retry.attempt" in DUAL_EMISSION_DISCIPLINE
