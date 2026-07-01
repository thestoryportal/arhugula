"""Multi-tenant cross-cutting enforcement composition — U-OD-31.

Implements C-OD-21 §21.4 (per-tenant cardinality isolation + per-tenant
alerting), §21.5 (cross-tenant aggregation prohibition — no cross-tenant cost
rollups, operator-burden eval rollups, alignment-floor rollups, or drift
detection rollups).

`CrossTenantAggregationProhibition` declares the 5 forbidden cross-tenant
rollup surfaces + the dashboard-query-construction-time enforcement layer.
`assert_pre_collector_redaction_applied` enforces the §21.3 pre-collector
eval-grade redaction pipeline at multi-tenant cells.
`reject_cross_tenant_query` rejects unscoped / cross-tenant-aggregating
dashboard queries at construction time. `assert_per_tenant_cardinality_isolation`
checks observed per-tenant cardinality against the U-OD-13 tenant rate limit.
`assert_per_tenant_alerting_isolation` enforces the tenant.id binding on
alerting signals at multi-tenant cells.

`DashboardQuery` and `CardinalityCounters` are declared in-unit — each has
exactly one OD consumer (U-OD-31); per OD plan v2.6 §0.7 / R5 Cluster-3
single-consumer types are declared in-unit, not at a carrier unit. Both are
faithful factor-outs of C-OD-21 §21.4 / §21.5 — not design extensions.

Carrier resolution. `SpanAttributes` resolves to the U-OD-04 OTel-handle alias
family (`otel_genai_base`) via the within-axis `[U-OD-04]` edge.
`ContentCapturePosture` resolves to U-OD-15's redaction-gradient carrier
(`redaction_gradient`); `AlertingSignal` to U-OD-22's dashboard-binding carrier
(`cost_attribution_dashboard_binding`); `PerCellCardinalityBudget` /
`tenant_rate_limit` to U-OD-13 (`per_cell_cardinality_budget`); `CellID` to
U-OD-01 (`observability_matrix`).

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.7.5 U-OD-31
(v2.6 M-1 revision — `SpanAttributes` re-pointed to U-OD-04; in-unit
`DashboardQuery` + `CardinalityCounters` declarations added); v2.1 §3.7.5 (base
unit body — preserved verbatim except the M-1 delta);
Spec_Operational_Discipline_v1_2.md §21 C-OD-21 §21.4 / §21.5 (preserved
verbatim into v1.4 per v1.4 §0).

Depends on: [U-OD-13, U-OD-14, U-OD-15, U-OD-16, U-OD-22, U-OD-24, U-OD-25,
U-OD-30, U-OD-04] (all within-axis).
"""

from __future__ import annotations

from typing import Literal

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict, Field

from harness_od.cost_attribution_dashboard_binding import AlertingSignal
from harness_od.observability_matrix import CellID
from harness_od.otel_genai_base import SpanAttributes
from harness_od.per_cell_cardinality_budget import PER_CELL_CARDINALITY_BUDGET
from harness_od.redaction_gradient import ContentCapturePosture

__all__ = [
    "CROSS_TENANT_AGGREGATION_PROHIBITION",
    "CardinalityCounters",
    "CrossTenantAggregationProhibition",
    "CrossTenantAggregationViolation",
    "DashboardQuery",
    "PerTenantAlertingViolation",
    "PerTenantCardinalityViolation",
    "PreCollectorRedactionViolation",
    "assert_per_tenant_alerting_isolation",
    "assert_per_tenant_cardinality_isolation",
    "assert_pre_collector_redaction_applied",
    "reject_cross_tenant_query",
]


# --- §0.8 inline error arms ------------------------------------------------


class PreCollectorRedactionViolation(Exception):  # noqa: N818 — U-OD-31 plan signature verbatim
    """Raised when pre-collector redaction is not applied at a multi-tenant cell.

    The `Result<(), PreCollectorRedactionViolation>` error arm of
    `assert_pre_collector_redaction_applied` (C-OD-21 §21.3).
    """


class CrossTenantAggregationViolation(Exception):  # noqa: N818 — U-OD-31 plan signature verbatim
    """Raised when a dashboard query aggregates across tenants (C-OD-21 §21.5).

    The `Result<(), CrossTenantAggregationViolation>` error arm of
    `reject_cross_tenant_query`.
    """


class PerTenantCardinalityViolation(Exception):  # noqa: N818 — U-OD-31 plan signature verbatim
    """Raised when per-tenant cardinality exceeds the U-OD-13 tenant rate limit.

    The `Result<(), PerTenantCardinalityViolation>` error arm of
    `assert_per_tenant_cardinality_isolation` (C-OD-21 §21.4).
    """


class PerTenantAlertingViolation(Exception):  # noqa: N818 — U-OD-31 plan signature verbatim
    """Raised when an alerting signal lacks a tenant.id binding (C-OD-21 §21.4).

    The `Result<(), PerTenantAlertingViolation>` error arm of
    `assert_per_tenant_alerting_isolation`.
    """


# --- §21.5 cross-tenant aggregation prohibition ----------------------------


class CrossTenantAggregationProhibition(BaseModel):
    """The cross-tenant aggregation prohibition surface (C-OD-21 §21.5).

    `forbidden_surfaces` is the 5-surface set of cross-tenant rollups forbidden
    at multi-tenant cells per §21.5 verbatim. `enforcement_layer` is the
    dashboard-query-construction-time enforcement point — spans MAY carry
    tenant.id, queries MUST scope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    forbidden_surfaces: frozenset[str]
    enforcement_layer: Literal["DASHBOARD_QUERY_CONSTRUCTION_TIME"]


#: The §21.5 cross-tenant aggregation prohibition — the 5 forbidden cross-tenant
#: rollup surfaces, enforced at dashboard-query-construction time.
CROSS_TENANT_AGGREGATION_PROHIBITION: CrossTenantAggregationProhibition = (
    CrossTenantAggregationProhibition(
        forbidden_surfaces=frozenset(
            {
                "cost.rollup.cross_tenant",
                "operator_burden_eval.rollup.cross_tenant",
                "alignment_floor.rollup.cross_tenant",
                "drift_detection.rollup.cross_tenant",
                "dashboard_query.cross_tenant_dimension",
            }
        ),
        enforcement_layer="DASHBOARD_QUERY_CONSTRUCTION_TIME",
    )
)


# --- in-unit single-consumer records (v2.6 M-1) ----------------------------


class DashboardQuery(BaseModel):
    """A query constructed against the per-cell dashboard surface (§21.5).

    Inspected at construction time for cross-tenant scope violations.
    `tenant_id_scope` is `None` when unscoped (rejected at multi-tenant cells).
    Single OD consumer (U-OD-31) — declared in-unit per OD plan v2.6 §0.7 /
    R5 Cluster-3. Faithful factor-out of the §21.5 dashboard-query-construction-
    time enforcement concept.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_id: CellID
    #: `None` == unscoped (rejected at multi-tenant cells).
    tenant_id_scope: str | None
    #: dimensions the query aggregates over.
    aggregation_dims: frozenset[str]
    #: attribute names referenced (cardinality-safe per U-OD-14).
    queried_attributes: frozenset[str]


class CardinalityCounters(BaseModel):
    """Observed per-tenant cardinality counts (C-OD-21 §21.4).

    Checked against the U-OD-13 `tenant_rate_limit` per §21.4. Single OD
    consumer (U-OD-31) — declared in-unit per OD plan v2.6 §0.7 / R5 Cluster-3.
    Faithful factor-out of the §21.4 per-tenant cardinality isolation concept.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    #: observed distinct attribute-value series for this tenant.
    observed_series: int = Field(ge=0)
    #: the window over which the count was taken.
    observation_window: str


# --- multi-tenant cell set -------------------------------------------------


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    """Construct a `CellID` — local helper mirroring `per_cell_backend_class`."""
    return CellID(persona_tier=pt, deployment_surface=ds)


_CELL_7 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_8 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD)

#: The multi-tenant cells — pre-collector redaction + per-tenant isolation
#: apply only here (C-OD-21 §21.3).
_MULTI_TENANT_CELLS: frozenset[CellID] = frozenset({_CELL_7, _CELL_8})


# --- §21.3 pre-collector redaction enforcement -----------------------------


def assert_pre_collector_redaction_applied(
    span_attrs: SpanAttributes,
    cell_id: CellID,
    posture: ContentCapturePosture,
) -> None:
    """Assert pre-collector redaction is applied at multi-tenant cells (§21.3).

    Returns `None` (the `Ok(())` arm) at non-multi-tenant cells (the
    pre-collector mandate applies only at cells 7/8). Raises
    `PreCollectorRedactionViolation` (the `Err` arm) when, at cell-7 or cell-8,
    the content-capture posture is not the `PRE_COLLECTOR_EVAL_GRADE_PIPELINE`
    posture — §21.3 mandates pre-collector redaction at attribute-set time,
    before the BatchSpanProcessor buffer.

    `span_attrs` is the OTel attribute bag (`SpanAttributes`, U-OD-04 carrier);
    the live attribute-bag inspection (content-bearing attributes per the
    U-OD-14 cardinality-prohibited / U-OD-15 default-off sets) is wired at a
    Phase-2 composition root. The library surface enforces the posture mandate.
    """
    del span_attrs  # live attribute-bag content inspection is a Phase-2 root
    if cell_id not in _MULTI_TENANT_CELLS:
        return None
    if posture is not ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE:
        raise PreCollectorRedactionViolation(
            f"pre-collector redaction not applied at multi-tenant cell "
            f"{cell_id.persona_tier} x {cell_id.deployment_surface}: posture="
            f"{posture.value} is not PRE_COLLECTOR_EVAL_GRADE_PIPELINE "
            f"(C-OD-21 §21.3)"
        )
    return None


# --- §21.5 cross-tenant query rejection ------------------------------------


def reject_cross_tenant_query(query: DashboardQuery) -> None:
    """Reject an unscoped / cross-tenant-aggregating dashboard query (§21.5).

    Returns `None` (the `Ok(())` arm) at non-multi-tenant cells, or when a
    multi-tenant-cell query is tenant-scoped and does not aggregate over the
    `tenant.id` dimension. Raises `CrossTenantAggregationViolation` (the `Err`
    arm) when a multi-tenant-cell query lacks a `tenant_id_scope` OR aggregates
    over the `tenant.id` dimension. Enforcement is hard-fail at construction
    time, not a logged warning (§21.5).
    """
    if query.cell_id not in _MULTI_TENANT_CELLS:
        return None
    if query.tenant_id_scope is None:
        raise CrossTenantAggregationViolation(
            f"cross-tenant query rejected at multi-tenant cell "
            f"{query.cell_id.persona_tier} x {query.cell_id.deployment_surface}: "
            f"query lacks a tenant_id_scope (C-OD-21 §21.5)"
        )
    if "tenant.id" in query.aggregation_dims:
        raise CrossTenantAggregationViolation(
            "cross-tenant query rejected: query aggregates over the "
            "'tenant.id' dimension at a multi-tenant cell (C-OD-21 §21.5)"
        )
    return None


# --- §21.4 per-tenant cardinality isolation --------------------------------


def assert_per_tenant_cardinality_isolation(
    tenant_id: str,
    cell_id: CellID,
    observed: CardinalityCounters,
) -> None:
    """Assert observed per-tenant cardinality is within the U-OD-13 limit (§21.4).

    Returns `None` (the `Ok(())` arm) when `cell_id` is not a multi-tenant
    cell, when the cell carries no per-tenant rate limit, or when
    `observed.observed_series` is within `tenant_rate_limit`. Raises
    `PerTenantCardinalityViolation` (the `Err` arm) when observed per-tenant
    cardinality exceeds the `tenant_rate_limit` from the U-OD-13 per-cell
    cardinality budget.
    """
    if cell_id not in _MULTI_TENANT_CELLS:
        return None
    budget = PER_CELL_CARDINALITY_BUDGET.get(cell_id)
    if budget is None or budget.tenant_rate_limit is None:
        return None
    if observed.observed_series > budget.tenant_rate_limit:
        raise PerTenantCardinalityViolation(
            f"per-tenant cardinality isolation violated for tenant "
            f"{tenant_id!r} at cell {cell_id.persona_tier} x "
            f"{cell_id.deployment_surface}: observed_series="
            f"{observed.observed_series} exceeds tenant_rate_limit="
            f"{budget.tenant_rate_limit} (C-OD-21 §21.4 / C-OD-11 §11.1)"
        )
    return None


# --- §21.4 per-tenant alerting isolation -----------------------------------


def assert_per_tenant_alerting_isolation(
    alerting_signal: AlertingSignal,
    tenant_id: str,
) -> None:
    """Assert an alerting signal carries a tenant.id binding (C-OD-21 §21.4).

    Returns `None` (the `Ok(())` arm) when `tenant_id` is a non-empty tenant
    identifier — the alerting signal is bound to a tenant. Raises
    `PerTenantAlertingViolation` (the `Err` arm) when the alerting signal lacks
    a tenant.id binding at a multi-tenant cell. `alerting_signal` is the
    U-OD-22 comparison outcome (`AlertingSignal`); at multi-tenant cells every
    alerting signal MUST be tenant-scoped.
    """
    del alerting_signal  # the signal value is tenant-agnostic; the binding is tenant_id
    if not tenant_id:
        raise PerTenantAlertingViolation(
            "alerting signal lacks a tenant.id binding at a multi-tenant cell "
            "(C-OD-21 §21.4 — per-tenant alerting isolation)"
        )
    return None
