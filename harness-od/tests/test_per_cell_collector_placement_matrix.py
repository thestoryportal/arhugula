"""Tests for U-OD-28 — per-cell OTLP collector placement matrix.

Every U-OD-28 v2.9 §3.7.2 acceptance criterion maps to >=1 test below.
Authority: Implementation_Plan_Operational_Discipline_v2_9.md §3.7.2;
Spec_Operational_Discipline_v1_4.md §20.1 + §20.2.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.local_first_otlp_collector import (
    BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
)
from harness_od.observability_matrix import ACTIVE_CELLS, CellID
from harness_od.per_cell_collector_placement_matrix import (
    PER_CELL_COLLECTOR_PLACEMENT,
    CollectorPlacement,
    EmissionModeViolation,
    PerCellPlacement,
    assert_async_emission_universality,
    collector_placement,
)


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


_CELL_1 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
_CELL_2 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_3 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.MANAGED_CLOUD)
_CELL_4 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT)
_CELL_5 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_6 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.MANAGED_CLOUD)
_CELL_7 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_8 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD)


# --- acc #1 — CollectorPlacement 7-value enum ------------------------------


def test_collector_placement_cardinality_seven() -> None:
    """acc #1 — `CollectorPlacement` enumerates exactly 7 values per §20.1."""
    assert len(list(CollectorPlacement)) == 7


def test_collector_placement_members_byte_exact_per_v1_4_section_20_1() -> None:
    """acc #1 — the 7 values are byte-exact with v1.4 §20.1 verbatim."""
    assert {p.value for p in CollectorPlacement} == {
        "IN_PROCESS",
        "SELF_HOSTED_BACKEND_COLLECTOR",
        "SIDECAR",
        "VENDOR_PIPELINE",
        "SIDECAR_WITH_PER_TENANT_ROUTING",
        "PER_TENANT_COLLECTOR_INSTANCE",
        "VENDOR_MANAGED_COLLECTOR",
    }


# --- acc #2 — PER_CELL_COLLECTOR_PLACEMENT cardinality 8 -------------------


def test_per_cell_placement_cardinality_eight() -> None:
    """acc #2 — exactly 8 entries, one per ACTIVE cell."""
    assert len(PER_CELL_COLLECTOR_PLACEMENT) == 8
    assert set(PER_CELL_COLLECTOR_PLACEMENT) == set(ACTIVE_CELLS)


# --- acc #3 — per-cell placement_classes byte-exact with §20.1 table -------


def test_cell_1_placement_singleton_in_process() -> None:
    """acc #3 — cell-1 -> {IN_PROCESS}."""
    assert collector_placement(_CELL_1) == frozenset({CollectorPlacement.IN_PROCESS})


def test_cell_2_placement_alt_route_in_process_or_self_hosted_backend() -> None:
    """acc #3 — cell-2 -> {IN_PROCESS, SELF_HOSTED_BACKEND_COLLECTOR}."""
    assert collector_placement(_CELL_2) == frozenset(
        {
            CollectorPlacement.IN_PROCESS,
            CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
        }
    )


def test_cell_3_placement_singleton_vendor_pipeline() -> None:
    """acc #3 — cell-3 -> {VENDOR_PIPELINE}."""
    assert collector_placement(_CELL_3) == frozenset({CollectorPlacement.VENDOR_PIPELINE})


def test_cell_4_placement_alt_route_in_process_or_self_hosted_backend() -> None:
    """acc #3 — cell-4 -> {IN_PROCESS, SELF_HOSTED_BACKEND_COLLECTOR}."""
    assert collector_placement(_CELL_4) == frozenset(
        {
            CollectorPlacement.IN_PROCESS,
            CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
        }
    )


def test_cell_5_placement_singleton_sidecar() -> None:
    """acc #3 — cell-5 -> {SIDECAR}."""
    assert collector_placement(_CELL_5) == frozenset({CollectorPlacement.SIDECAR})


def test_cell_6_placement_singleton_vendor_pipeline() -> None:
    """acc #3 — cell-6 -> {VENDOR_PIPELINE}."""
    assert collector_placement(_CELL_6) == frozenset({CollectorPlacement.VENDOR_PIPELINE})


def test_cell_7_placement_alt_route_sidecar_routing_or_per_tenant_instance() -> None:
    """acc #3 — cell-7 -> {SIDECAR_WITH_PER_TENANT_ROUTING, PER_TENANT_COLLECTOR_INSTANCE}."""
    assert collector_placement(_CELL_7) == frozenset(
        {
            CollectorPlacement.SIDECAR_WITH_PER_TENANT_ROUTING,
            CollectorPlacement.PER_TENANT_COLLECTOR_INSTANCE,
        }
    )


def test_cell_8_placement_singleton_vendor_managed() -> None:
    """acc #3 — cell-8 -> {VENDOR_MANAGED_COLLECTOR}."""
    assert collector_placement(_CELL_8) == frozenset({CollectorPlacement.VENDOR_MANAGED_COLLECTOR})


def test_placement_classes_set_nonempty_all_cells() -> None:
    """acc #3 — each cell's placement_classes is a non-empty set."""
    for placement in PER_CELL_COLLECTOR_PLACEMENT.values():
        assert len(placement.placement_classes) >= 1


def test_placement_classes_cardinality_one_or_two_all_cells() -> None:
    """acc #3 — |placement_classes| in {1, 2}; 2 only at alt-route cells 2/4/7."""
    alt_route = {_CELL_2, _CELL_4, _CELL_7}
    for cell, placement in PER_CELL_COLLECTOR_PLACEMENT.items():
        card = len(placement.placement_classes)
        assert card in {1, 2}
        assert (card == 2) == (cell in alt_route)


# --- acc #4 — emission_mode async universal --------------------------------


def test_emission_mode_async_universal() -> None:
    """acc #4 — emission_mode == BATCH_SPAN_PROCESSOR_ASYNC at every entry."""
    for placement in PER_CELL_COLLECTOR_PLACEMENT.values():
        assert placement.emission_mode == "BATCH_SPAN_PROCESSOR_ASYNC"


# --- acc #5 — emission window/batch inherit from U-OD-27 -------------------


def test_emission_window_inherits_from_u_od_27() -> None:
    """acc #5 — emission_window == U-OD-27 BATCH_SPAN_PROCESSOR_WINDOW_SECONDS."""
    for placement in PER_CELL_COLLECTOR_PLACEMENT.values():
        assert placement.emission_window == BATCH_SPAN_PROCESSOR_WINDOW_SECONDS


def test_emission_batch_inherits_from_u_od_27() -> None:
    """acc #5 — emission_batch == U-OD-27 BATCH_SPAN_PROCESSOR_BATCH_SIZE."""
    for placement in PER_CELL_COLLECTOR_PLACEMENT.values():
        assert placement.emission_batch == BATCH_SPAN_PROCESSOR_BATCH_SIZE


# --- acc #6 — assert_async_emission_universality ---------------------------


def test_assert_async_universality_reject_sync() -> None:
    """acc #6 — Err(EmissionModeViolation) when emission mode deviates.

    The `Literal` field forbids a non-async value at construction time;
    `model_construct` bypasses validation to exercise the function's reject arm.
    """
    deviant = PerCellPlacement.model_construct(
        cell_id=_CELL_1,
        placement_classes=frozenset({CollectorPlacement.IN_PROCESS}),
        emission_mode="SYNC_BLOCKING",  # type: ignore[arg-type]
        emission_window=BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
        emission_batch=BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    )
    with pytest.raises(EmissionModeViolation):
        assert_async_emission_universality(deviant)


def test_assert_async_universality_accept_all_cells() -> None:
    """acc #6 — every declared cell passes the universality assertion."""
    for placement in PER_CELL_COLLECTOR_PLACEMENT.values():
        assert assert_async_emission_universality(placement) is None


# --- acc #7 — vendor endpoint deferred -------------------------------------


def test_specific_vendor_endpoint_deferred() -> None:
    """acc #7 — the matrix commits placement class(es), not endpoint URLs.

    `PerCellPlacement` carries no endpoint-URL field — deployment-binding-time
    endpoint configuration is deferred per §20.1 'Deferred to implementation
    discretion'. The model forbids extra fields, so no URL can be smuggled in.
    """
    assert set(PerCellPlacement.model_fields) == {
        "cell_id",
        "placement_classes",
        "emission_mode",
        "emission_window",
        "emission_batch",
    }


# --- acc #8 — per-tenant placement at cells 7/8 ----------------------------


def test_per_tenant_placement_at_cells_7_8() -> None:
    """acc #8 — cells 7/8 encode per-tenant separation at OTLP-collector level."""
    per_tenant_aware = {
        CollectorPlacement.SIDECAR_WITH_PER_TENANT_ROUTING,
        CollectorPlacement.PER_TENANT_COLLECTOR_INSTANCE,
        CollectorPlacement.VENDOR_MANAGED_COLLECTOR,
    }
    assert collector_placement(_CELL_7) <= per_tenant_aware
    assert collector_placement(_CELL_8) <= per_tenant_aware
