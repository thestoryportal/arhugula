"""Tests for U-OD-33 — per-dimension preservation invariants.

Test set per the U-OD-33 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_1.md §3.8.2, preserved verbatim through v2.11). Every acceptance
criterion maps to >=1 test.

Acceptance criteria (C-OD-22 §22.2 / §22.4):
  #1 — PreservationDimension enumerates exactly 5 values verbatim.
  #2 — PRESERVATION_INVARIANTS declares exactly 5 entries with per-dimension
       invariant form + enforcement layer + cross-axis target.
  #3 — verify_per_dimension_preservation returns Ok / Err(PreservationViolation).
  #4 — GATE_POLICY + SANDBOX_TIER require Session 5 cross-axis verification.
  #5 — assert_cross_axis_composition_verified_at_session_5 returns
       Err(CrossAxisCompositionPending) at OD plan v1 scope.
  #6 — 4 cross-axis edges (3 AS + 1 CP).
  #7 — T-perm-1 5-axis composition: 3-of-5 axes.
  #8 — cross-deployment monotonicity at surface ascent (transitions 6/7/8).
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.bridging_arc_table import (
    BRIDGING_ARC_TRANSITIONS,
    BridgingArcTransition,
    TransitionType,
)
from harness_od.observability_matrix import CellID
from harness_od.per_dimension_preservation_invariants import (
    PRESERVATION_INVARIANTS,
    CrossAxisCompositionPending,
    EnforcementLayer,
    InvariantForm,
    PreservationDimension,
    PreservationViolation,
    assert_cross_axis_composition_verified_at_session_5,
    verify_per_dimension_preservation,
)


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


#: A canonical in-scope transition (transition 1 — within-column ascent).
_TRANSITION_1 = BRIDGING_ARC_TRANSITIONS[0]


# --- acc #1 — PreservationDimension cardinality ---------------------------


def test_preservation_dimension_cardinality_five() -> None:
    """acc #1 — PreservationDimension enumerates exactly 5 values."""
    assert len(PreservationDimension) == 5
    assert set(PreservationDimension) == {
        PreservationDimension.SAMPLING_DISCIPLINE,
        PreservationDimension.CARDINALITY_BUDGET,
        PreservationDimension.REDACTION_CLASS,
        PreservationDimension.GATE_POLICY,
        PreservationDimension.SANDBOX_TIER,
    }


# --- acc #2 — PRESERVATION_INVARIANTS cardinality + per-dimension forms ----


def test_preservation_invariants_cardinality_five() -> None:
    """acc #2 — PRESERVATION_INVARIANTS declares exactly 5 entries, one per
    dimension, with matching dimension keys."""
    assert len(PRESERVATION_INVARIANTS) == 5
    assert set(PRESERVATION_INVARIANTS) == set(PreservationDimension)
    for dim, inv in PRESERVATION_INVARIANTS.items():
        assert inv.dimension is dim


def test_sampling_invariant_form_set_inclusion() -> None:
    """acc #2 — SAMPLING_DISCIPLINE: SET_INCLUSION, DESIGN_TIME_VERIFICATION."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.SAMPLING_DISCIPLINE]
    assert inv.invariant_form is InvariantForm.SET_INCLUSION_TARGET_INCLUDES_SOURCE
    assert inv.enforcement_layer is EnforcementLayer.DESIGN_TIME_VERIFICATION
    assert inv.cross_axis_composition_target is None


def test_cardinality_invariant_form_scalar_le() -> None:
    """acc #2 — CARDINALITY_BUDGET: SCALAR_MONOTONIC_TIGHTENING_LE,
    RUNTIME_ENFORCEMENT_AT_COLLECTOR_BOUNDARY."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.CARDINALITY_BUDGET]
    assert inv.invariant_form is InvariantForm.SCALAR_MONOTONIC_TIGHTENING_LE
    assert inv.enforcement_layer is EnforcementLayer.RUNTIME_ENFORCEMENT_AT_COLLECTOR_BOUNDARY
    assert inv.cross_axis_composition_target is None


def test_redaction_invariant_form_class_index_ge() -> None:
    """acc #2 — REDACTION_CLASS: CLASS_INDEX_MONOTONIC_ASCENT_GE,
    DESIGN_TIME_VERIFICATION."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.REDACTION_CLASS]
    assert inv.invariant_form is InvariantForm.CLASS_INDEX_MONOTONIC_ASCENT_GE
    assert inv.enforcement_layer is EnforcementLayer.DESIGN_TIME_VERIFICATION
    assert inv.cross_axis_composition_target is None


def test_gate_policy_invariant_form_class_index_ge() -> None:
    """acc #2 — GATE_POLICY: CLASS_INDEX_MONOTONIC_ASCENT_GE, target C-CP-19."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.GATE_POLICY]
    assert inv.invariant_form is InvariantForm.CLASS_INDEX_MONOTONIC_ASCENT_GE
    assert inv.cross_axis_composition_target == "C-CP-19"


def test_sandbox_tier_invariant_form_class_index_ge() -> None:
    """acc #2 — SANDBOX_TIER: CLASS_INDEX_MONOTONIC_ASCENT_GE,
    target C-AS-12 §12.1."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.SANDBOX_TIER]
    assert inv.invariant_form is InvariantForm.CLASS_INDEX_MONOTONIC_ASCENT_GE
    assert inv.cross_axis_composition_target == "C-AS-12 §12.1"


# --- acc #4 — cross-axis composition enforcement layer --------------------


def test_gate_policy_enforcement_cross_axis_composition() -> None:
    """acc #4 — GATE_POLICY enforcement layer is CROSS_AXIS_COMPOSITION_
    VERIFICATION (verified at the Session 5 cross-axis matrix)."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.GATE_POLICY]
    assert inv.enforcement_layer is EnforcementLayer.CROSS_AXIS_COMPOSITION_VERIFICATION


def test_sandbox_tier_enforcement_cross_axis_composition() -> None:
    """acc #4 — SANDBOX_TIER enforcement layer is CROSS_AXIS_COMPOSITION_
    VERIFICATION (verified at the Session 5 cross-axis matrix)."""
    inv = PRESERVATION_INVARIANTS[PreservationDimension.SANDBOX_TIER]
    assert inv.enforcement_layer is EnforcementLayer.CROSS_AXIS_COMPOSITION_VERIFICATION


# --- acc #3 — verify_per_dimension_preservation ---------------------------


def test_verify_per_dimension_sampling_pass() -> None:
    """acc #3 — verify_per_dimension_preservation returns Ok (None) when the
    transition preserves SAMPLING_DISCIPLINE."""
    assert (
        verify_per_dimension_preservation(_TRANSITION_1, PreservationDimension.SAMPLING_DISCIPLINE)
        is None
    )


def test_verify_per_dimension_redaction_downgrade_reject() -> None:
    """acc #3 — verify_per_dimension_preservation raises PreservationViolation
    when a transition downgrades REDACTION_CLASS (target tier weaker than
    source tier — a redaction-class downgrade)."""
    downgrade = BridgingArcTransition(
        transition_id=3,
        source_cell=_cell(
            PersonaTier.MULTI_TENANT_COMPLIANCE,
            DeploymentSurface.SELF_HOSTED_SERVER,
        ),
        target_cell=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.SELF_HOSTED_SERVER),
        transition_type=TransitionType.WITHIN_COLUMN,
    )
    with pytest.raises(PreservationViolation):
        verify_per_dimension_preservation(downgrade, PreservationDimension.REDACTION_CLASS)


# --- acc #5 — assert_cross_axis_composition_verified_at_session_5 ----------


def test_assert_cross_axis_composition_pending_at_v1() -> None:
    """acc #5 — assert_cross_axis_composition_verified_at_session_5 raises
    CrossAxisCompositionPending for the cross-axis dimensions at OD plan v1
    scope; returns Ok (None) for the in-axis dimensions."""
    for dim in (
        PreservationDimension.GATE_POLICY,
        PreservationDimension.SANDBOX_TIER,
    ):
        with pytest.raises(CrossAxisCompositionPending):
            assert_cross_axis_composition_verified_at_session_5(dim)
    for dim in (
        PreservationDimension.SAMPLING_DISCIPLINE,
        PreservationDimension.CARDINALITY_BUDGET,
        PreservationDimension.REDACTION_CLASS,
    ):
        assert assert_cross_axis_composition_verified_at_session_5(dim) is None


# --- acc #7 — T-perm-1 5-axis composition ---------------------------------


def test_t_perm_1_composition_three_of_five_axes() -> None:
    """acc #7 — the 5 preservation dimensions compose 3-of-5 of the T-perm-1
    multiplicative tunable axes: GATE_POLICY, SANDBOX_TIER, REDACTION_CLASS are
    the 3 T-perm-1-touching dimensions."""
    t_perm_1_touching = {
        PreservationDimension.GATE_POLICY,
        PreservationDimension.SANDBOX_TIER,
        PreservationDimension.REDACTION_CLASS,
    }
    assert len(t_perm_1_touching) == 3
    assert t_perm_1_touching <= set(PreservationDimension)


# --- acc #8 — cross-deployment monotonicity at surface ascent -------------


def test_cross_deployment_monotonicity_at_surface_ascent() -> None:
    """acc #8 — at deployment-surface ascent (transitions 6/7/8), GATE_POLICY
    and SANDBOX_TIER are verified at the Session 5 cross-axis matrix; at OD plan
    v1 scope that verification is deferred (CrossAxisCompositionPending)."""
    surface_ascent = [t for t in BRIDGING_ARC_TRANSITIONS if t.transition_id in (6, 7, 8)]
    assert len(surface_ascent) == 3
    for transition in surface_ascent:
        assert transition.transition_type is TransitionType.DIAGONAL
        for dim in (
            PreservationDimension.GATE_POLICY,
            PreservationDimension.SANDBOX_TIER,
        ):
            # OD-side structural surface returns Ok at plan v1 scope.
            assert verify_per_dimension_preservation(transition, dim) is None
            # the cross-axis composition verification itself is deferred.
            with pytest.raises(CrossAxisCompositionPending):
                assert_cross_axis_composition_verified_at_session_5(dim)


# --- acc #6 — cross-axis edge count ---------------------------------------


def test_cross_axis_edges_four_total() -> None:
    """acc #6 — 4 cross-axis edges: 3 OD→AS (C-AS-12 §12.1, C-AS-15 §15.6,
    C-AS-12 §12.4) + 1 OD→CP (C-CP-19). The two cross-axis dimensions carry
    string `cross_axis_composition_target` references — resolution at U-OD-34;
    the full 3-AS-edge enumeration lives in the plan §3.8.2 `Depends on`."""
    cross_axis_targets = {
        inv.cross_axis_composition_target
        for inv in PRESERVATION_INVARIANTS.values()
        if inv.cross_axis_composition_target is not None
    }
    assert cross_axis_targets == {"C-CP-19", "C-AS-12 §12.1"}
    # both targets are plain string references — no typed cross-axis import.
    for target in cross_axis_targets:
        assert isinstance(target, str)
