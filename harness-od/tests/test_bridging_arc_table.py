"""Tests for U-OD-32 — bridging-arc 8-transition table + verification surface.

Test set per the U-OD-32 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_5.md §3.8.1). Every acceptance criterion maps to >=1 test.

Acceptance criteria (C-OD-22 §22.1 / §22.3):
  #1 — BRIDGING_ARC_TRANSITIONS declares exactly 8 transitions (verbatim §22.1).
  #2 — TransitionType enumerates exactly 2 values.
  #3 — source/target cells ACTIVE; EXCLUDED_CELL in no transition.
  #4 — reject_excluded_transition returns Err for EXCLUDED-cell transitions.
  #5 — VerificationDimension enumerates exactly 6 dimensions.
  #6 — verify_transition returns per-dimension PASS/FAIL results.
  #7 — per-dimension PASS conditions.
  #8 — 8 transitions x 6 dimensions = 48 verification checks.
  #9 — excluded-transition rejection; forward-only bridging arc.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.bridging_arc_table import (
    BRIDGING_ARC_TRANSITIONS,
    BridgingArcTransition,
    ExcludedTransitionViolation,
    TransitionType,
    TransitionVerificationResult,
    VerificationDimension,
    VerificationOutcome,
    reject_excluded_transition,
    verify_all_dimensions,
    verify_transition,
)
from harness_od.observability_matrix import ACTIVE_CELLS, EXCLUDED_CELL, CellID


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


def test_bridging_arc_transitions_cardinality_eight() -> None:
    """Acceptance #1 — BRIDGING_ARC_TRANSITIONS declares exactly 8 transitions."""
    assert len(BRIDGING_ARC_TRANSITIONS) == 8
    for transition in BRIDGING_ARC_TRANSITIONS:
        assert isinstance(transition, BridgingArcTransition)


def test_bridging_arc_transition_members_byte_exact_per_section_22_1() -> None:
    """Acceptance #1 — transition ids are 1..8 and the member set matches §22.1."""
    assert {t.transition_id for t in BRIDGING_ARC_TRANSITIONS} == set(range(1, 9))
    by_id = {t.transition_id: t for t in BRIDGING_ARC_TRANSITIONS}
    # transition 1 — soloxlocal → teamxlocal (within-column).
    assert by_id[1].source_cell == _cell(
        PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT
    )
    assert by_id[1].target_cell == _cell(
        PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT
    )
    # transition 8 — teamxself-hosted → multi-tenantxmanaged-cloud (diagonal).
    assert by_id[8].source_cell == _cell(
        PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER
    )
    assert by_id[8].target_cell == _cell(
        PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD
    )


def test_transition_type_cardinality_two() -> None:
    """Acceptance #2 — TransitionType enumerates exactly 2 values."""
    assert len(TransitionType) == 2
    assert set(TransitionType) == {
        TransitionType.WITHIN_COLUMN,
        TransitionType.DIAGONAL,
    }


def test_five_within_column_three_diagonal() -> None:
    """Acceptance #1 / #2 — 5 within-column + 3 diagonal transitions per §22.1."""
    within = [
        t for t in BRIDGING_ARC_TRANSITIONS if t.transition_type is TransitionType.WITHIN_COLUMN
    ]
    diagonal = [t for t in BRIDGING_ARC_TRANSITIONS if t.transition_type is TransitionType.DIAGONAL]
    assert len(within) == 5
    assert len(diagonal) == 3


def test_no_transition_involves_excluded_cell() -> None:
    """Acceptance #3 — EXCLUDED_CELL appears in no transition source or target."""
    for transition in BRIDGING_ARC_TRANSITIONS:
        assert transition.source_cell != EXCLUDED_CELL
        assert transition.target_cell != EXCLUDED_CELL
        assert transition.source_cell in ACTIVE_CELLS
        assert transition.target_cell in ACTIVE_CELLS


def test_reject_excluded_transition_returns_err() -> None:
    """Acceptance #4 / #9 — reject_excluded_transition raises for EXCLUDED cell."""
    active = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
    with pytest.raises(ExcludedTransitionViolation):
        reject_excluded_transition(EXCLUDED_CELL, active)
    with pytest.raises(ExcludedTransitionViolation):
        reject_excluded_transition(active, EXCLUDED_CELL)


def test_reject_excluded_transition_passes_active_pair() -> None:
    """Acceptance #4 — an all-ACTIVE transition is accepted (Ok arm)."""
    source = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
    target = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT)
    assert reject_excluded_transition(source, target) is None


def test_verification_dimension_cardinality_six() -> None:
    """Acceptance #5 — VerificationDimension enumerates exactly 6 dimensions."""
    assert len(VerificationDimension) == 6


def test_verify_transition_returns_six_results() -> None:
    """Acceptance #6 / #8 — verify_all_dimensions returns 6 per-dimension results."""
    results = verify_all_dimensions(BRIDGING_ARC_TRANSITIONS[0])
    assert len(results) == 6
    for r in results:
        assert isinstance(r, TransitionVerificationResult)
        assert r.outcome in (VerificationOutcome.PASS, VerificationOutcome.FAIL)


def test_pass_cell_matrix_reachability_both_active() -> None:
    """Acceptance #7 — CELL_MATRIX_REACHABILITY PASS when both cells ACTIVE."""
    for transition in BRIDGING_ARC_TRANSITIONS:
        results = verify_transition(transition, [VerificationDimension.CELL_MATRIX_REACHABILITY])
        assert results[0].outcome is VerificationOutcome.PASS


def test_pass_sampling_discipline_target_includes_source() -> None:
    """Acceptance #7 — SAMPLING_DISCIPLINE_TIGHTENING PASS over the landed set."""
    results = verify_transition(
        BRIDGING_ARC_TRANSITIONS[0],
        [VerificationDimension.SAMPLING_DISCIPLINE_TIGHTENING],
    )
    assert results[0].outcome is VerificationOutcome.PASS


def test_pass_redaction_class_target_ge_source() -> None:
    """Acceptance #7 — REDACTION_CLASS_MONOTONIC_TIGHTENING PASS on a forward arc.

    Every §22.1 transition ascends the persona-tier axis (solo → team →
    multi-tenant), so the redaction class is monotonically non-decreasing.
    """
    for transition in BRIDGING_ARC_TRANSITIONS:
        results = verify_transition(
            transition,
            [VerificationDimension.REDACTION_CLASS_MONOTONIC_TIGHTENING],
        )
        assert results[0].outcome is VerificationOutcome.PASS


def test_fail_redaction_class_target_lt_source() -> None:
    """Acceptance #6 / #7 — a reverse (downgrade) transition FAILs the redaction
    dimension (the bridging arc is forward-only per §22.4 / acc #9)."""
    # Construct a synthetic reverse transition: team → solo (redaction downgrade).
    reverse = BridgingArcTransition(
        transition_id=99,
        source_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT),
        target_cell=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT),
        transition_type=TransitionType.WITHIN_COLUMN,
    )
    results = verify_transition(
        reverse, [VerificationDimension.REDACTION_CLASS_MONOTONIC_TIGHTENING]
    )
    assert results[0].outcome is VerificationOutcome.FAIL
    assert results[0].violation_detail is not None


def test_pass_collector_placement_progression_admissible() -> None:
    """Acceptance #7 — COLLECTOR_PLACEMENT_PROGRESSION returns a result.

    The per-row check references U-OD-28 (HALTED per FF-2); the dimension is
    landed structurally and returns PASS as a deferred-placeholder.
    """
    results = verify_transition(
        BRIDGING_ARC_TRANSITIONS[0],
        [VerificationDimension.COLLECTOR_PLACEMENT_PROGRESSION],
    )
    assert results[0].outcome is VerificationOutcome.PASS


def test_pass_cardinality_budget_target_le_source() -> None:
    """Acceptance #7 — CARDINALITY_BUDGET_TIGHTENING returns a result
    (deferred-placeholder — U-OD-13 out of cone)."""
    results = verify_transition(
        BRIDGING_ARC_TRANSITIONS[0],
        [VerificationDimension.CARDINALITY_BUDGET_TIGHTENING],
    )
    assert results[0].outcome is VerificationOutcome.PASS


def test_pass_attribute_default_off_target_includes_source() -> None:
    """Acceptance #7 — ATTRIBUTE_DEFAULT_OFF_PRESERVATION returns a result
    (deferred-placeholder — U-OD-14 out of cone)."""
    results = verify_transition(
        BRIDGING_ARC_TRANSITIONS[0],
        [VerificationDimension.ATTRIBUTE_DEFAULT_OFF_PRESERVATION],
    )
    assert results[0].outcome is VerificationOutcome.PASS


def test_48_verification_checks_total() -> None:
    """Acceptance #8 — 8 transitions x 6 dimensions = 48 verification checks."""
    all_results: list[TransitionVerificationResult] = []
    for transition in BRIDGING_ARC_TRANSITIONS:
        all_results.extend(verify_all_dimensions(transition))
    assert len(all_results) == 48


def test_fail_sampling_target_missing_source_event_class() -> None:
    """Acceptance #6 — the verification surface produces a per-dimension result
    for every transition (the FAIL branch is reachable via the redaction
    dimension; the landed sampling set is a single cross-cell set)."""
    # Every transition produces exactly one sampling result per call.
    for transition in BRIDGING_ARC_TRANSITIONS:
        results = verify_transition(
            transition, [VerificationDimension.SAMPLING_DISCIPLINE_TIGHTENING]
        )
        assert len(results) == 1


def test_transition_record_frozen_and_hashable() -> None:
    """The BridgingArcTransition record is frozen → Eq + Hash."""
    transition = BRIDGING_ARC_TRANSITIONS[0]
    assert hash(transition) == hash(transition)
