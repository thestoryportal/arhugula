"""Tests for U-CP-20 — per-resumption observable behavior (C-CP-08 §8.3).

Acceptance-criterion coverage (CP plan v2.2 amendment):
  #1 5 entries per §8.3        -> test_per_resumption_observable_behavior_cardinality_five
  #2 workflow.resumption span  -> test_emits_workflow_resumption_span,
                                  test_engine_class_required_attribute,
                                  test_engine_replay_disposition_required_attribute
  #3 f2_join_path per-engine   -> test_f2_join_path_per_engine_class
  #4 continuity per kind       -> test_continuity_guarantee_per_kind
  #5 F2-12 carry-forward CLOSED -> test_f2_12_carry_forward_closed_at_v2_2
"""

from __future__ import annotations

from harness_cp.f2_substrate_join_discipline import F2JoinKind
from harness_cp.per_resumption_observable_behavior import (
    F2_12_CARRY_FORWARD_CLOSED,
    PER_RESUMPTION_OBSERVABLE_BEHAVIOR,
    ContinuityGuarantee,
)
from harness_cp.resumption_kind import ResumptionKind


def test_per_resumption_observable_behavior_cardinality_five() -> None:
    """#1 — exactly five entries, one per ResumptionKind."""
    assert len(PER_RESUMPTION_OBSERVABLE_BEHAVIOR) == 5
    assert {e.resumption_kind for e in PER_RESUMPTION_OBSERVABLE_BEHAVIOR} == set(ResumptionKind)


def test_emits_workflow_resumption_span() -> None:
    """#2 — every entry emits the workflow.resumption span."""
    for e in PER_RESUMPTION_OBSERVABLE_BEHAVIOR:
        assert e.emits_span == "workflow.resumption"


def test_engine_class_required_attribute() -> None:
    """#2 — engine.class is a required attribute on every entry."""
    for e in PER_RESUMPTION_OBSERVABLE_BEHAVIOR:
        assert "engine.class" in e.required_attributes


def test_engine_replay_disposition_required_attribute() -> None:
    """#2 — engine.replay_disposition is required (v2.2 amendment)."""
    for e in PER_RESUMPTION_OBSERVABLE_BEHAVIOR:
        assert "engine.replay_disposition" in e.required_attributes


def test_f2_join_path_per_engine_class() -> None:
    """#3 — f2_join_path is a valid F2JoinKind carried from U-CP-18."""
    for e in PER_RESUMPTION_OBSERVABLE_BEHAVIOR:
        assert isinstance(e.f2_join_path, F2JoinKind)


def test_continuity_guarantee_per_kind() -> None:
    """#4 — ContinuityGuarantee discriminates across the five kinds 1:1."""
    by_kind = {
        e.resumption_kind: e.observable_continuity for e in PER_RESUMPTION_OBSERVABLE_BEHAVIOR
    }
    assert by_kind[ResumptionKind.ENGINE_REPLAY] is ContinuityGuarantee.EXACT_REPLAY
    assert by_kind[ResumptionKind.SAVE_POINT_RESUME] is ContinuityGuarantee.CHECKPOINT_RESTORE
    assert by_kind[ResumptionKind.JOURNAL_RESUME] is ContinuityGuarantee.REPLAY_FROM_LEDGER
    assert by_kind[ResumptionKind.RECONCILER_CONVERGE] is ContinuityGuarantee.RECONVERGE
    assert by_kind[ResumptionKind.SEGMENT_REPLAY] is ContinuityGuarantee.WAL_REPLAY
    # 5 distinct guarantees.
    assert len(set(by_kind.values())) == 5


def test_f2_12_carry_forward_closed_at_v2_2() -> None:
    """#5 — the F2-12 carry-forward is CLOSED at this unit."""
    assert F2_12_CARRY_FORWARD_CLOSED is True
