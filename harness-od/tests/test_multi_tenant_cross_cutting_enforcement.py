"""Tests for U-OD-31 — multi-tenant cross-cutting enforcement composition.

Every U-OD-31 acceptance criterion (#1-#11) maps to >=1 test below.
Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.7.5 (M-1
revision over v2.1 §3.7.5); Spec_Operational_Discipline_v1_2.md §21.4 / §21.5.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.cost_attribution_dashboard_binding import AlertingSignal
from harness_od.multi_tenant_cross_cutting_enforcement import (
    CROSS_TENANT_AGGREGATION_PROHIBITION,
    CardinalityCounters,
    CrossTenantAggregationViolation,
    DashboardQuery,
    PerTenantAlertingViolation,
    PerTenantCardinalityViolation,
    PreCollectorRedactionViolation,
    assert_per_tenant_alerting_isolation,
    assert_per_tenant_cardinality_isolation,
    assert_pre_collector_redaction_applied,
    reject_cross_tenant_query,
)
from harness_od.observability_matrix import CellID
from harness_od.redaction_gradient import ContentCapturePosture


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


_CELL_1 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
_CELL_5 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_7 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_8 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD)

#: An empty OTel attribute bag — `SpanAttributes` is a Mapping alias.
_EMPTY_ATTRS: dict[str, object] = {}


def _counters(observed: int) -> CardinalityCounters:
    return CardinalityCounters(
        tenant_id="tenant-a",
        observed_series=observed,
        observation_window="1h",
    )


def _query(cell: CellID, scope: str | None, dims: set[str]) -> DashboardQuery:
    return DashboardQuery(
        cell_id=cell,
        tenant_id_scope=scope,
        aggregation_dims=frozenset(dims),
        queried_attributes=frozenset({"gen_ai.operation.name"}),
    )


# --- acc #1 — forbidden_surfaces cardinality 5 -----------------------------


def test_forbidden_surfaces_cardinality_five() -> None:
    """acc #1 — exactly 5 forbidden cross-tenant rollup surfaces per §21.5."""
    assert len(CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces) == 5


def test_forbidden_surface_names_byte_exact() -> None:
    """acc #1 — the 5 forbidden surface names byte-exact per §21.5 verbatim."""
    assert CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces == frozenset(
        {
            "cost.rollup.cross_tenant",
            "operator_burden_eval.rollup.cross_tenant",
            "alignment_floor.rollup.cross_tenant",
            "drift_detection.rollup.cross_tenant",
            "dashboard_query.cross_tenant_dimension",
        }
    )


# --- acc #2 — enforcement_layer --------------------------------------------


def test_enforcement_layer_dashboard_query_time() -> None:
    """acc #2 — enforcement_layer == DASHBOARD_QUERY_CONSTRUCTION_TIME."""
    assert (
        CROSS_TENANT_AGGREGATION_PROHIBITION.enforcement_layer
        == "DASHBOARD_QUERY_CONSTRUCTION_TIME"
    )


# --- acc #3 / #8 — pre-collector redaction ---------------------------------


def test_pre_collector_redaction_at_cell_7_required() -> None:
    """acc #3 — cell-7 with non-eval-grade posture is rejected."""
    with pytest.raises(PreCollectorRedactionViolation):
        assert_pre_collector_redaction_applied(
            _EMPTY_ATTRS,  # type: ignore[arg-type]
            _CELL_7,
            ContentCapturePosture.OPERATOR_SELF_REDACT,
        )


def test_pre_collector_redaction_at_cell_8_required() -> None:
    """acc #3 — cell-8 with non-eval-grade posture is rejected."""
    with pytest.raises(PreCollectorRedactionViolation):
        assert_pre_collector_redaction_applied(
            _EMPTY_ATTRS,  # type: ignore[arg-type]
            _CELL_8,
            ContentCapturePosture.REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY,
        )


def test_pre_collector_redaction_at_non_multi_tenant_not_required() -> None:
    """acc #3 — the pre-collector mandate applies only at cells 7/8."""
    assert (
        assert_pre_collector_redaction_applied(
            _EMPTY_ATTRS,  # type: ignore[arg-type]
            _CELL_1,
            ContentCapturePosture.OPERATOR_SELF_REDACT,
        )
        is None
    )


def test_pre_collector_redaction_eval_grade_posture_accepted() -> None:
    """acc #8 — cell-7 with PRE_COLLECTOR_EVAL_GRADE_PIPELINE posture passes."""
    assert (
        assert_pre_collector_redaction_applied(
            _EMPTY_ATTRS,  # type: ignore[arg-type]
            _CELL_7,
            ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE,
        )
        is None
    )


# --- acc #4 / #9 — reject_cross_tenant_query -------------------------------


def test_reject_cross_tenant_query_missing_tenant_scope() -> None:
    """acc #4 — a multi-tenant-cell query lacking tenant scope is rejected."""
    with pytest.raises(CrossTenantAggregationViolation):
        reject_cross_tenant_query(_query(_CELL_7, None, set()))


def test_reject_cross_tenant_query_multi_tenant_aggregation() -> None:
    """acc #4 — a query aggregating over tenant.id is rejected."""
    with pytest.raises(CrossTenantAggregationViolation):
        reject_cross_tenant_query(_query(_CELL_8, "tenant-a", {"tenant.id"}))


def test_accept_per_tenant_scoped_query() -> None:
    """acc #4 — a tenant-scoped query without tenant.id aggregation passes."""
    assert reject_cross_tenant_query(_query(_CELL_7, "tenant-a", {"gen_ai.operation.name"})) is None


def test_reject_cross_tenant_query_non_multi_tenant_cell_accepts() -> None:
    """acc #9 — the prohibition applies only at multi-tenant cells."""
    assert reject_cross_tenant_query(_query(_CELL_5, None, set())) is None


# --- acc #5 — per-tenant cardinality isolation -----------------------------


def test_per_tenant_cardinality_isolation_within_limit() -> None:
    """acc #5 — observed cardinality within the tenant rate limit passes."""
    assert assert_per_tenant_cardinality_isolation("tenant-a", _CELL_7, _counters(500)) is None


def test_per_tenant_cardinality_isolation_exceeds_limit_reject() -> None:
    """acc #5 — observed cardinality above the tenant rate limit is rejected."""
    with pytest.raises(PerTenantCardinalityViolation):
        assert_per_tenant_cardinality_isolation("tenant-a", _CELL_8, _counters(5000))


# --- acc #6 — per-tenant alerting isolation --------------------------------


def test_per_tenant_alerting_with_tenant_id_accept() -> None:
    """acc #6 — an alerting signal with a tenant.id binding passes."""
    assert assert_per_tenant_alerting_isolation(AlertingSignal.ABOVE_THRESHOLD, "tenant-a") is None


def test_per_tenant_alerting_without_tenant_id_reject() -> None:
    """acc #6 — an alerting signal lacking a tenant.id binding is rejected."""
    with pytest.raises(PerTenantAlertingViolation):
        assert_per_tenant_alerting_isolation(AlertingSignal.BELOW_THRESHOLD, "")


# --- acc #7 — composition surfaces -----------------------------------------


def test_forbidden_surfaces_cover_three_composition_axes() -> None:
    """acc #7 — cost / eval / alignment-floor / drift rollups all forbidden."""
    surfaces = CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces
    assert "cost.rollup.cross_tenant" in surfaces
    assert "operator_burden_eval.rollup.cross_tenant" in surfaces
    assert "alignment_floor.rollup.cross_tenant" in surfaces
    assert "drift_detection.rollup.cross_tenant" in surfaces


# --- acc #1 §21.5 surface coverage — the 4 forbidden rollup classes --------


def test_cross_tenant_cost_rollup_rejected() -> None:
    """§21.5 — the cross-tenant cost rollup surface is forbidden."""
    assert "cost.rollup.cross_tenant" in CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces


def test_cross_tenant_eval_rollup_rejected() -> None:
    """§21.5 — the cross-tenant operator-burden eval rollup is forbidden."""
    assert (
        "operator_burden_eval.rollup.cross_tenant"
        in CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces
    )


def test_cross_tenant_alignment_floor_rollup_rejected() -> None:
    """§21.5 — the cross-tenant alignment-floor rollup is forbidden."""
    assert (
        "alignment_floor.rollup.cross_tenant"
        in CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces
    )


def test_cross_tenant_drift_rollup_rejected() -> None:
    """§21.5 — the cross-tenant drift detection rollup is forbidden."""
    assert (
        "drift_detection.rollup.cross_tenant"
        in CROSS_TENANT_AGGREGATION_PROHIBITION.forbidden_surfaces
    )


# --- acc #10 / #11 — in-unit single-consumer records -----------------------


def test_dashboard_query_declared_in_unit() -> None:
    """acc #11 — `DashboardQuery` is the in-unit single-consumer record."""
    fields = set(DashboardQuery.model_fields)
    assert fields == {
        "cell_id",
        "tenant_id_scope",
        "aggregation_dims",
        "queried_attributes",
    }


def test_cardinality_counters_declared_in_unit() -> None:
    """acc #11 — `CardinalityCounters` is the in-unit single-consumer record."""
    fields = set(CardinalityCounters.model_fields)
    assert fields == {"tenant_id", "observed_series", "observation_window"}
