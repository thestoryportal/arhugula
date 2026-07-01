"""Per-cell cost-attribution dashboard binding + alerting composition — U-OD-22.

Implements C-OD-16 §16.1 (per-cell dashboard binding signature), §16.2
(alerting threshold composition), §16.3 (dashboard composition with the
operator-burden eval primitive).

`DashboardBindingForm` / `AlertingHook` enumerate the 3 per-cell-class binding
forms + alerting hooks (§16.1). `PER_CELL_DASHBOARD_BINDINGS` declares one
binding per ACTIVE cell. `AlertingThresholdComposition` composes the per-class
cost ceiling with the per-cell base-rate sampling; `compute_alerting_signal`
scales the observed cost rollup by `1 / base_rate` before comparing to the
ceiling (§16.2 — unbiased cost estimation at sub-1.0 sampled rates).
`DashboardBackendConsolidation` records the same-backend cost / operator-burden
dashboard pairing (§16.3).

`WorkloadClass` resolves to the landed `harness-core` U-CP-00 resident (v2.6
M-2 — `[U-CP-00 (cross-axis: core)]` edge). Per the carrier-map disposition-3
row this is a `harness-core` import, not an outbound CXA OD→CP edge; no
`WorkloadClass` type is materialized in-unit. `DashboardRef` is declared
in-unit as an opaque single-consumer marker (v2.6 M-1).

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.5.5 U-OD-22
(v2.6 M-1 + M-2 revision — `WorkloadClass` re-pointed to the landed
`harness-core` U-CP-00 carrier; `DashboardRef` declared in-unit; all v2.1
surfaces preserved verbatim from v2.1 §3.5.5);
Spec_Operational_Discipline_v1_2.md §16 C-OD-16 §16.1 / §16.2 / §16.3
(preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.5.

Depends on: [U-OD-01, U-OD-12, U-OD-18, U-OD-19, U-OD-21,
U-CP-00 (cross-axis: core)] — `WorkloadClass` imported from `harness-core`
(landed U-CP-00); the `(cross-axis: core)` edge is a shared-substrate import,
not a 7c-deferred CXA edge.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NewType

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_od.base_rate_set_and_envelope import PER_CELL_BASE_RATE_ENVELOPE
from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    CellID,
    reject_excluded_cell,
)

__all__ = [
    "PER_CELL_DASHBOARD_BINDINGS",
    "AlertingHook",
    "AlertingSignal",
    "AlertingThresholdComposition",
    "CellDashboardBinding",
    "DashboardBackendConsolidation",
    "DashboardBindingForm",
    "DashboardRef",
    "compute_alerting_signal",
]


#: v2.6 M-1 — `DashboardRef` is an opaque handle to a per-cell dashboard
#: surface (a TUI ring-buffer query view, or a named backend dashboard, per
#: `DashboardBindingForm`). Single OD consumer (U-OD-22) → declared in-unit,
#: not a carrier unit. Resolved to the per-cell backend's dashboarding model at
#: deployment-binding time.
DashboardRef = NewType("DashboardRef", str)


class DashboardBindingForm(StrEnum):
    """The 3 per-cell-class dashboard binding forms (C-OD-16 §16.1 — acc #1)."""

    TUI_TRACE_BROWSER_RING_BUFFER_QUERY = "TUI_TRACE_BROWSER_RING_BUFFER_QUERY"
    """solo-developer cells — TUI trace-browser scoped query against the
    sqlite ring-buffer per C-OD-19; no separate dashboard layer."""

    NAMED_DASHBOARD_QUERY_BACKEND = "NAMED_DASHBOARD_QUERY_BACKEND"
    """team-binding cells — named dashboard query against the cell-committed
    backend."""

    PER_TENANT_DASHBOARD_SEPARATION = "PER_TENANT_DASHBOARD_SEPARATION"
    """multi-tenant-compliance cells — per-tenant dashboard separation;
    cross-tenant aggregation forbidden per C-OD-21."""


class AlertingHook(StrEnum):
    """The 3 per-cell-class alerting hooks (C-OD-16 §16.1 — acc #2)."""

    OPERATOR_SELF_INSPECTION_TUI_THRESHOLD_OPTIONAL = (
        "OPERATOR_SELF_INSPECTION_TUI_THRESHOLD_OPTIONAL"
    )
    """solo-developer cells — operator-self-inspection; alerting optional via
    TUI threshold annotation."""

    BACKEND_SIDE_ALERTING_PER_CLASS_COST_CEILING = "BACKEND_SIDE_ALERTING_PER_CLASS_COST_CEILING"
    """team-binding cells — backend-side alerting bound to the per-class cost
    ceiling threshold."""

    PER_TENANT_ALERTING_NO_CROSS_TENANT = "PER_TENANT_ALERTING_NO_CROSS_TENANT"
    """multi-tenant-compliance cells — per-tenant alerting; cross-tenant
    aggregation forbidden."""


class CellDashboardBinding(BaseModel):
    """One ACTIVE cell's cost-attribution dashboard binding (C-OD-16 §16.1).

    Frozen → `Eq` + `Hash`, stable under serialization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: the §16.1 dashboard binding form for the cell class.
    binding_form: DashboardBindingForm
    #: the §16.1 alerting hook for the cell class.
    alerting_hook: AlertingHook
    #: §16.3 — the cell MAY consolidate cost + operator-burden eval dashboards.
    consolidates_with_operator_burden_eval: bool


class AlertingThresholdComposition(BaseModel):
    """The alerting threshold composition for one cell (C-OD-16 §16.2).

    Frozen → `Eq`. `per_class_cost_ceiling` is operator-tunable per Persona §6
    (deferred per §16.3 — specific numeric values are deployment-binding-time).
    `scaled_estimate_factor` is `1.0 / base_rate` — the §16.2 unbiased-cost
    scaling factor at sub-1.0 sampled rates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: the per-workload-class cost ceiling (operator-tunable per Persona §6).
    #: `WorkloadClass` is the landed `harness-core` U-CP-00 enum (v2.6 M-2).
    per_class_cost_ceiling: dict[WorkloadClass, float]
    #: the §10.3 per-cell base-rate (sourced from U-OD-12).
    base_rate: float
    #: the §16.2 unbiased-cost scaling factor, `1.0 / base_rate`.
    scaled_estimate_factor: float


class AlertingSignal(StrEnum):
    """The alerting comparison outcome (C-OD-16 §16.2 — acc #5)."""

    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    ABOVE_THRESHOLD = "ABOVE_THRESHOLD"


class DashboardBackendConsolidation(BaseModel):
    """The same-backend cost / operator-burden dashboard pairing (§16.3).

    Frozen → `Eq`. `same_backend` is `True` per §16.3 — the cost-attribution
    dashboard and the operator-burden eval dashboard bind to the same per-cell
    backend. `consolidated_view` is optional — implementations MAY consolidate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: the cost-attribution dashboard handle (in-unit `DashboardRef`).
    cost_attribution_dashboard: DashboardRef
    #: the operator-burden eval dashboard handle (U-OD-24 surface).
    operator_burden_eval_dashboard: DashboardRef
    #: §16.3 — cost + operator-burden dashboards bind to the same backend.
    same_backend: bool
    #: optional consolidated view, when the backend's model permits it.
    consolidated_view: DashboardRef | None = None


def _binding_for(cell: CellID) -> CellDashboardBinding:
    """Construct the §16.1 binding for `cell` from its persona-tier class.

    solo-developer cells → TUI ring-buffer; team-binding cells → named
    dashboard query; multi-tenant-compliance cells → per-tenant separation
    (C-OD-16 §16.1 — acc #3 / #4).
    """
    from harness_core import PersonaTier

    if cell.persona_tier is PersonaTier.SOLO_DEVELOPER:
        form = DashboardBindingForm.TUI_TRACE_BROWSER_RING_BUFFER_QUERY
        hook = AlertingHook.OPERATOR_SELF_INSPECTION_TUI_THRESHOLD_OPTIONAL
    elif cell.persona_tier is PersonaTier.TEAM_BINDING:
        form = DashboardBindingForm.NAMED_DASHBOARD_QUERY_BACKEND
        hook = AlertingHook.BACKEND_SIDE_ALERTING_PER_CLASS_COST_CEILING
    else:  # MULTI_TENANT_COMPLIANCE
        form = DashboardBindingForm.PER_TENANT_DASHBOARD_SEPARATION
        hook = AlertingHook.PER_TENANT_ALERTING_NO_CROSS_TENANT
    return CellDashboardBinding(
        cell_id=cell,
        binding_form=form,
        alerting_hook=hook,
        consolidates_with_operator_burden_eval=True,
    )


#: The per-cell cost-attribution dashboard bindings — exactly 8 entries, one
#: per ACTIVE cell (C-OD-16 §16.1; acc #3). solo cells → TUI ring-buffer;
#: team cells → named dashboard queries; multi-tenant cells → per-tenant
#: separation.
PER_CELL_DASHBOARD_BINDINGS: dict[CellID, CellDashboardBinding] = {
    cell: _binding_for(cell) for cell in ACTIVE_CELLS
}


def compute_alerting_signal(
    observed_cost_rollup: float,
    threshold: AlertingThresholdComposition,
    workload_class: WorkloadClass,
) -> AlertingSignal:
    """Compare a scaled cost rollup to the per-class ceiling (C-OD-16 §16.2).

    Per §16.2 verbatim: the observed cost rollup is scaled by `1 / base_rate`
    for unbiased cost estimation at sub-1.0 sampled rates, then compared to the
    per-workload-class cost ceiling (acc #5). At `base_rate == 1.0` the scaling
    factor is `1.0` — no scaling (acc — `test_alerting_base_rate_one_no_scaling`).

    Returns `ABOVE_THRESHOLD` when the scaled estimate strictly exceeds the
    ceiling, `BELOW_THRESHOLD` otherwise. `workload_class` selects the ceiling
    from `threshold.per_class_cost_ceiling` (the landed `harness-core`
    `WorkloadClass` enum, v2.6 M-2).
    """
    scaled_estimate = observed_cost_rollup * threshold.scaled_estimate_factor
    ceiling = threshold.per_class_cost_ceiling[workload_class]
    if scaled_estimate > ceiling:
        return AlertingSignal.ABOVE_THRESHOLD
    return AlertingSignal.BELOW_THRESHOLD


def base_rate_for(cell: CellID) -> float:
    """Return the §10.3 per-cell base-rate for `cell`, sourced from U-OD-12.

    Raises `CellBindingViolation` for the EXCLUDED cell (acc #6 — `base_rate`
    is sourced from `PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate`).
    """
    reject_excluded_cell(cell)
    return PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate


# Cardinality sanity-pin per acc #3 — one binding per ACTIVE cell.
assert set(PER_CELL_DASHBOARD_BINDINGS) == set(ACTIVE_CELLS), (
    "PER_CELL_DASHBOARD_BINDINGS must cover exactly the 8 ACTIVE cells"
)
