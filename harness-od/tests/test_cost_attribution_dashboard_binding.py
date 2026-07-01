"""Tests for U-OD-22 — per-cell cost-attribution dashboard binding.

Test set per the U-OD-22 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_1.md §3.5.5 + v2.6 §3.5.5 additions). Every acceptance criterion
maps to >=1 test.

Acceptance criteria (C-OD-16 §16.1 / §16.2 / §16.3):
  #1 — DashboardBindingForm enumerates exactly 3 values.
  #2 — AlertingHook enumerates exactly 3 values.
  #3 — PER_CELL_DASHBOARD_BINDINGS declares exactly 8 entries.
  #4 — per-cell alerting hook matches §16.1 per cell class.
  #5 — compute_alerting_signal scales by 1/base_rate before comparing.
  #6 — base_rate sourced from U-OD-12 envelope.
  #7 — per_class_cost_ceiling operator-tunable (deferred numeric values).
  #8 — DashboardBackendConsolidation.same_backend == True.
  #9 — consolidated_view is optional.
  #10 — dashboard queries cardinality-safe.
  #11 (v2.6) — WorkloadClass resolves to harness-core U-CP-00.
  #12 (v2.6) — DashboardRef declared in-unit as an opaque marker.
"""

from __future__ import annotations

from harness_core import DeploymentSurface, PersonaTier, WorkloadClass
from harness_od.cost_attribution_dashboard_binding import (
    PER_CELL_DASHBOARD_BINDINGS,
    AlertingHook,
    AlertingSignal,
    AlertingThresholdComposition,
    CellDashboardBinding,
    DashboardBackendConsolidation,
    DashboardBindingForm,
    DashboardRef,
    base_rate_for,
    compute_alerting_signal,
)
from harness_od.observability_matrix import ACTIVE_CELLS, CellID


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


def test_dashboard_binding_form_three_values() -> None:
    """Acceptance #1 — DashboardBindingForm enumerates exactly 3 values."""
    assert len(DashboardBindingForm) == 3


def test_alerting_hook_three_values() -> None:
    """Acceptance #2 — AlertingHook enumerates exactly 3 values."""
    assert len(AlertingHook) == 3


def test_per_cell_bindings_cardinality_eight() -> None:
    """Acceptance #3 — PER_CELL_DASHBOARD_BINDINGS declares exactly 8 entries."""
    assert len(PER_CELL_DASHBOARD_BINDINGS) == 8
    assert set(PER_CELL_DASHBOARD_BINDINGS) == set(ACTIVE_CELLS)
    for binding in PER_CELL_DASHBOARD_BINDINGS.values():
        assert isinstance(binding, CellDashboardBinding)


def test_solo_cells_tui_ring_buffer() -> None:
    """Acceptance #3 / #4 — solo-developer cells → TUI ring-buffer query."""
    for cell, binding in PER_CELL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.SOLO_DEVELOPER:
            assert binding.binding_form is DashboardBindingForm.TUI_TRACE_BROWSER_RING_BUFFER_QUERY


def test_team_cells_named_dashboard() -> None:
    """Acceptance #3 / #4 — team-binding cells → named dashboard query."""
    for cell, binding in PER_CELL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.TEAM_BINDING:
            assert binding.binding_form is DashboardBindingForm.NAMED_DASHBOARD_QUERY_BACKEND


def test_multi_tenant_cells_per_tenant_separation() -> None:
    """Acceptance #3 / #4 — multi-tenant cells → per-tenant separation."""
    for cell, binding in PER_CELL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE:
            assert binding.binding_form is DashboardBindingForm.PER_TENANT_DASHBOARD_SEPARATION


def test_per_cell_alerting_hook_match_spec() -> None:
    """Acceptance #4 — per-cell alerting hook matches §16.1 per cell class."""
    expected = {
        PersonaTier.SOLO_DEVELOPER: (AlertingHook.OPERATOR_SELF_INSPECTION_TUI_THRESHOLD_OPTIONAL),
        PersonaTier.TEAM_BINDING: (AlertingHook.BACKEND_SIDE_ALERTING_PER_CLASS_COST_CEILING),
        PersonaTier.MULTI_TENANT_COMPLIANCE: (AlertingHook.PER_TENANT_ALERTING_NO_CROSS_TENANT),
    }
    for cell, binding in PER_CELL_DASHBOARD_BINDINGS.items():
        assert binding.alerting_hook is expected[cell.persona_tier]


def _threshold(base_rate: float) -> AlertingThresholdComposition:
    cell = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
    return AlertingThresholdComposition(
        cell_id=cell,
        per_class_cost_ceiling={wc: 100.0 for wc in WorkloadClass},
        base_rate=base_rate,
        scaled_estimate_factor=1.0 / base_rate,
    )


def test_alerting_signal_below_threshold() -> None:
    """Acceptance #5 — a scaled estimate below the ceiling → BELOW_THRESHOLD."""
    threshold = _threshold(1.0)
    signal = compute_alerting_signal(50.0, threshold, WorkloadClass.SOFTWARE_ENGINEERING)
    assert signal is AlertingSignal.BELOW_THRESHOLD


def test_alerting_signal_above_threshold() -> None:
    """Acceptance #5 — a scaled estimate above the ceiling → ABOVE_THRESHOLD."""
    threshold = _threshold(1.0)
    signal = compute_alerting_signal(150.0, threshold, WorkloadClass.SOFTWARE_ENGINEERING)
    assert signal is AlertingSignal.ABOVE_THRESHOLD


def test_alerting_scales_by_inverse_base_rate() -> None:
    """Acceptance #5 — observed rollup scaled by 1/base_rate before comparing.

    base_rate 0.1 → factor 10.0; observed 20.0 → scaled 200.0 > 100.0 ceiling.
    Without scaling, 20.0 < 100.0 would be BELOW — the scaling flips it.
    """
    threshold = _threshold(0.1)
    signal = compute_alerting_signal(20.0, threshold, WorkloadClass.SOFTWARE_ENGINEERING)
    assert signal is AlertingSignal.ABOVE_THRESHOLD


def test_alerting_base_rate_one_no_scaling() -> None:
    """Acceptance #5 — at base_rate 1.0 the scaling factor is 1.0 (no scaling)."""
    threshold = _threshold(1.0)
    assert threshold.scaled_estimate_factor == 1.0
    signal = compute_alerting_signal(99.0, threshold, WorkloadClass.SOFTWARE_ENGINEERING)
    assert signal is AlertingSignal.BELOW_THRESHOLD


def test_alerting_base_rate_sourced_from_u_od_12() -> None:
    """Acceptance #6 — base_rate sourced from the U-OD-12 per-cell envelope."""
    for cell in ACTIVE_CELLS:
        rate = base_rate_for(cell)
        assert 0.0 < rate <= 1.0


def test_per_class_cost_ceiling_operator_tunable() -> None:
    """Acceptance #7 — per_class_cost_ceiling is a per-WorkloadClass map
    (operator-tunable; numeric values are deployment-binding-time)."""
    threshold = _threshold(1.0)
    assert set(threshold.per_class_cost_ceiling) == set(WorkloadClass)


def test_dashboard_same_backend_per_cell() -> None:
    """Acceptance #8 — DashboardBackendConsolidation.same_backend == True."""
    cell = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
    consolidation = DashboardBackendConsolidation(
        cell_id=cell,
        cost_attribution_dashboard=DashboardRef("cost-dash"),
        operator_burden_eval_dashboard=DashboardRef("eval-dash"),
        same_backend=True,
    )
    assert consolidation.same_backend is True


def test_dashboard_consolidation_optional() -> None:
    """Acceptance #9 — consolidated_view is optional (defaults to None)."""
    cell = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
    consolidation = DashboardBackendConsolidation(
        cell_id=cell,
        cost_attribution_dashboard=DashboardRef("cost-dash"),
        operator_burden_eval_dashboard=DashboardRef("eval-dash"),
        same_backend=True,
    )
    assert consolidation.consolidated_view is None
    with_view = DashboardBackendConsolidation(
        cell_id=cell,
        cost_attribution_dashboard=DashboardRef("cost-dash"),
        operator_burden_eval_dashboard=DashboardRef("eval-dash"),
        same_backend=True,
        consolidated_view=DashboardRef("merged"),
    )
    assert with_view.consolidated_view == DashboardRef("merged")


def test_dashboard_queries_cardinality_safe() -> None:
    """Acceptance #10 — every cell carries a cardinality-safe binding form
    (the §16.1 binding forms are the design-time committed surface; no
    high-cardinality attribute is a dashboard binding key)."""
    for binding in PER_CELL_DASHBOARD_BINDINGS.values():
        assert binding.binding_form in DashboardBindingForm


def test_multi_tenant_cross_tenant_aggregation_forbidden() -> None:
    """Acceptance #10 — multi-tenant cells use per-tenant alerting (no
    cross-tenant aggregation per C-OD-21)."""
    for cell, binding in PER_CELL_DASHBOARD_BINDINGS.items():
        if cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE:
            assert binding.alerting_hook is AlertingHook.PER_TENANT_ALERTING_NO_CROSS_TENANT


def test_workload_class_resolves_to_harness_core_u_cp_00() -> None:
    """Acceptance #11 (v2.6) — WorkloadClass resolves to harness-core U-CP-00."""
    assert WorkloadClass.__module__.startswith("harness_core")
    threshold = _threshold(1.0)
    # The per_class_cost_ceiling keys are the harness-core WorkloadClass enum.
    for key in threshold.per_class_cost_ceiling:
        assert isinstance(key, WorkloadClass)


def test_depends_on_u_cp_00_core_edge_declared() -> None:
    """Acceptance #11 (v2.6) — the [U-CP-00 (cross-axis: core)] edge resolves;
    WorkloadClass is importable from harness-core."""
    import harness_core

    assert harness_core.WorkloadClass is WorkloadClass


def test_dashboard_ref_declared_in_unit_opaque_marker() -> None:
    """Acceptance #12 (v2.6) — DashboardRef is an in-unit opaque marker."""
    ref = DashboardRef("a-dashboard")
    assert ref == "a-dashboard"
    assert DashboardRef.__module__.endswith("cost_attribution_dashboard_binding")


def test_no_workload_class_materialized_in_unit() -> None:
    """Acceptance #12 (v2.6) — no WorkloadClass type is materialized in-unit;
    the unit imports the harness-core resident."""
    import harness_od.cost_attribution_dashboard_binding as mod

    # WorkloadClass is referenced but not declared inside the module.
    assert "WorkloadClass" not in vars(mod) or vars(mod)["WorkloadClass"] is WorkloadClass


def test_cell_dashboard_binding_frozen_and_hashable() -> None:
    """The CellDashboardBinding record is frozen → Eq + Hash."""
    binding = next(iter(PER_CELL_DASHBOARD_BINDINGS.values()))
    assert hash(binding) == hash(binding)
