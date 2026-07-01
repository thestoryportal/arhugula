"""Tests for U-OD-24 — per-cell operator-burden eval dashboard binding scaling.

Test set per the U-OD-24 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_1.md §3.6.2 + v2.6 §3.6.2 additions). Every acceptance criterion
maps to >=1 test.

Acceptance criteria (C-OD-17 §17.3):
  #1 — EvalDashboardForm / AlignmentFloorAlertingPosture / HusainLoopBinding
       each enumerate exactly 3 values per §17.3.
  #2 — PER_CELL_EVAL_DASHBOARD_BINDINGS declares exactly 8 entries with
       per-cell-class binding per §17.3.
  #3 — solo cells compose with U-OD-27 TUI ring-buffer scoped queries.
  #4 — team cells bind alignment-floor to backend alerting.
  #5 — multi-tenant cells enforce per-tenant alignment-floor; no cross-tenant.
  #6 — consolidates_with_cost == True permissible per §16.3 + §17.3.
  #7 — Husain loop binding per §17.3 row 1: solo → ring-buffer self-curation;
       team + multi-tenant → cell-committed backend.
  #8 (v2.6) — run_husain_loop_at_cell returns the in-unit HusainLoopState.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    EXCLUDED_CELL,
    CellBindingViolation,
    CellID,
)
from harness_od.operator_burden_eval_dashboard_binding import (
    PER_CELL_EVAL_DASHBOARD_BINDINGS,
    AlignmentFloorAlertingPosture,
    CellEvalDashboardBinding,
    EvalDashboardForm,
    HusainLoopBinding,
    HusainLoopState,
    run_husain_loop_at_cell,
)
from harness_od.operator_burden_eval_primitives import OperatorBurdenEvalPrimitive


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


# --- acceptance #1 — three values per enum ---------------------------------


def test_eval_dashboard_form_three_values() -> None:
    """Acceptance #1 — EvalDashboardForm enumerates exactly 3 values."""
    assert len(EvalDashboardForm) == 3


def test_alignment_floor_alerting_three_values() -> None:
    """Acceptance #1 — AlignmentFloorAlertingPosture enumerates exactly 3."""
    assert len(AlignmentFloorAlertingPosture) == 3


def test_husain_loop_binding_three_values() -> None:
    """Acceptance #1 — HusainLoopBinding enumerates exactly 3 values."""
    assert len(HusainLoopBinding) == 3


# --- acceptance #2 — 8-entry binding map -----------------------------------


def test_per_cell_bindings_cardinality_eight() -> None:
    """Acceptance #2 — PER_CELL_EVAL_DASHBOARD_BINDINGS has exactly 8 entries."""
    assert len(PER_CELL_EVAL_DASHBOARD_BINDINGS) == 8
    assert set(PER_CELL_EVAL_DASHBOARD_BINDINGS) == set(ACTIVE_CELLS)


def test_excluded_cell_has_no_binding() -> None:
    """Acceptance #2 — the structurally-excluded cell has no binding."""
    assert EXCLUDED_CELL not in PER_CELL_EVAL_DASHBOARD_BINDINGS


def test_every_binding_keyed_to_its_cell() -> None:
    """Acceptance #2 — each binding's cell_id matches its map key."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        assert isinstance(binding, CellEvalDashboardBinding)
        assert binding.cell_id == cell


# --- acceptance #3 — solo cells TUI ring-buffer scoped queries -------------


def test_solo_cells_tui_ring_buffer_scoped() -> None:
    """Acceptance #3 — solo-developer cells → TUI ring-buffer scoped queries."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.SOLO_DEVELOPER:
            assert binding.dashboard_form is EvalDashboardForm.TUI_RING_BUFFER_SCOPED_QUERIES


# --- acceptance #4 — team cells named queries + alignment-floor alerting ---


def test_team_cells_named_with_alignment_floor_alerting() -> None:
    """Acceptance #4 — team-binding cells → named queries + backend alerting."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.TEAM_BINDING:
            assert (
                binding.dashboard_form
                is EvalDashboardForm.NAMED_DASHBOARD_QUERIES_WITH_ALIGNMENT_FLOOR_ALERTING
            )
            assert (
                binding.alignment_floor_alerting
                is AlignmentFloorAlertingPosture.ALIGNMENT_FLOOR_BOUND_TO_BACKEND_ALERTING
            )


# --- acceptance #5 — multi-tenant per-tenant separation, no cross-tenant ---


def test_multi_tenant_per_tenant_separation_compliance() -> None:
    """Acceptance #5 — multi-tenant cells → per-tenant separation + compliance."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE:
            assert (
                binding.dashboard_form
                is EvalDashboardForm.PER_TENANT_SEPARATION_WITH_COMPLIANCE_ATTESTATION_ALERTING
            )


def test_multi_tenant_no_cross_tenant_alignment_floor() -> None:
    """Acceptance #5 — multi-tenant cells enforce per-tenant alignment-floor."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE:
            assert (
                binding.alignment_floor_alerting
                is AlignmentFloorAlertingPosture.PER_TENANT_ALIGNMENT_FLOOR_NO_CROSS_TENANT
            )


# --- acceptance #6 — consolidation with cost permissible -------------------


def test_consolidation_with_cost_permissible() -> None:
    """Acceptance #6 — consolidates_with_cost == True for every binding."""
    for binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.values():
        assert binding.consolidates_with_cost is True


# --- acceptance #7 — Husain loop binding per §17.3 row 1 -------------------


def test_husain_loop_solo_self_curation() -> None:
    """Acceptance #7 — solo cells run the loop against the ring-buffer."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.SOLO_DEVELOPER:
            assert (
                binding.husain_loop_binding is HusainLoopBinding.RING_BUFFER_OPERATOR_SELF_CURATION
            )


def test_husain_loop_team_and_multi_tenant_backend_hosted() -> None:
    """Acceptance #7 — team + multi-tenant cells run the loop against backend."""
    for cell, binding in PER_CELL_EVAL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.TEAM_BINDING:
            assert binding.husain_loop_binding is HusainLoopBinding.BACKEND_HOSTED
        elif cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE:
            assert binding.husain_loop_binding is HusainLoopBinding.PER_TENANT_BACKEND_HOSTED


# --- acceptance #8 (v2.6) — in-unit HusainLoopState ------------------------


def test_husain_loop_state_declared_in_unit() -> None:
    """Acceptance #8 (v2.6) — HusainLoopState is the in-unit single-consumer type."""
    state = HusainLoopState(
        cell_id=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT),
        loop_binding=HusainLoopBinding.RING_BUFFER_OPERATOR_SELF_CURATION,
        primitive=OperatorBurdenEvalPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR,
        iteration_phase="manual-review",
    )
    assert isinstance(state, HusainLoopState)
    assert state.iteration_phase == "manual-review"


def test_run_husain_loop_returns_husain_loop_state() -> None:
    """Acceptance #8 (v2.6) — run_husain_loop_at_cell returns HusainLoopState."""
    cell = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
    state = run_husain_loop_at_cell(cell, OperatorBurdenEvalPrimitive.ROUTING_ACCURACY_HOLDOUT)
    assert isinstance(state, HusainLoopState)
    assert state.cell_id == cell
    assert state.loop_binding is HusainLoopBinding.BACKEND_HOSTED
    assert state.primitive is OperatorBurdenEvalPrimitive.ROUTING_ACCURACY_HOLDOUT
    assert state.iteration_phase == "manual-review"


def test_run_husain_loop_rejects_excluded_cell() -> None:
    """Acceptance #2 / #8 — the structurally-excluded cell is rejected."""
    with pytest.raises(CellBindingViolation):
        run_husain_loop_at_cell(
            EXCLUDED_CELL, OperatorBurdenEvalPrimitive.CACHE_HIT_RATE_ALIGNMENT_FLOOR
        )
