"""Tests for U-OD-02 — per-cell backend class + candidate witness columns.

Test set per the U-OD-02 §3.1.2 (v2.8) `Tests:` field — covers acceptance
#1-#8 against C-OD-02 §2.1 / §2.2 / §2.3. acc #3/#7 conformed to the v2.8 D-1
set-valued `backend_class` signature.
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
from harness_od.per_cell_backend_class import (
    PER_CELL_BACKEND_BINDINGS,
    BackendClass,
    enumerate_candidates,
    select_backend_class,
)

_SOLO = PersonaTier.SOLO_DEVELOPER
_TEAM = PersonaTier.TEAM_BINDING
_MTC = PersonaTier.MULTI_TENANT_COMPLIANCE
_LOCAL = DeploymentSurface.LOCAL_DEVELOPMENT
_SELF = DeploymentSurface.SELF_HOSTED_SERVER
_CLOUD = DeploymentSurface.MANAGED_CLOUD


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    return CellID(persona_tier=pt, deployment_surface=ds)


# --- acc #1 ----------------------------------------------------------------
def test_backend_class_cardinality_seven() -> None:
    """`BackendClass` enumerates exactly 7 distinct values per §2.1."""
    assert len(BackendClass) == 7
    assert set(BackendClass) == {
        BackendClass.OTEL_ONLY,
        BackendClass.DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE,
        BackendClass.DEDICATED_LLM_OBS_PLATFORM_MULTI_NODE,
        BackendClass.CLOUD_NATIVE_LLM_OBS_PLATFORM,
        BackendClass.OTEL_TO_VENDOR,
        BackendClass.SELF_HOSTED_MULTI_TENANT_LLM_OBS_PLATFORM,
        BackendClass.VENDOR_MANAGED_MULTI_TENANT_LLM_OBS_OR_CLOUD_NATIVE_MANAGED_AGENT_RUNTIME,
    }


# --- acc #2 ----------------------------------------------------------------
def test_per_cell_bindings_cardinality_eight() -> None:
    """`PER_CELL_BACKEND_BINDINGS` declares exactly 8 entries — one per ACTIVE cell."""
    assert len(PER_CELL_BACKEND_BINDINGS) == 8
    assert set(PER_CELL_BACKEND_BINDINGS) == set(ACTIVE_CELLS)


# --- acc #3 — per-cell backend class set, byte-exact per §2.1 --------------
def test_cell_1_backend_class_singleton_otel_only() -> None:
    """cell-1 → `{OTEL_ONLY}` per §2.1."""
    assert select_backend_class(_cell(_SOLO, _LOCAL)) == frozenset({BackendClass.OTEL_ONLY})


def test_cell_2_backend_class_singleton_dedicated_single_node() -> None:
    """cell-2 → `{DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE}` per §2.1."""
    assert select_backend_class(_cell(_SOLO, _SELF)) == frozenset(
        {BackendClass.DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE}
    )


def test_cell_3_backend_class_singleton_cloud_native() -> None:
    """cell-3 → `{CLOUD_NATIVE_LLM_OBS_PLATFORM}` per §2.1."""
    assert select_backend_class(_cell(_SOLO, _CLOUD)) == frozenset(
        {BackendClass.CLOUD_NATIVE_LLM_OBS_PLATFORM}
    )


def test_cell_4_alternation_otel_or_dedicated_single_node() -> None:
    """cell-4 → 2-element disjunction set per §2.1 design-time-flexible row."""
    assert select_backend_class(_cell(_TEAM, _LOCAL)) == frozenset(
        {
            BackendClass.OTEL_ONLY,
            BackendClass.DEDICATED_LLM_OBS_PLATFORM_SINGLE_NODE,
        }
    )


def test_cell_5_alternation_dedicated_multi_node_or_otel_to_vendor() -> None:
    """cell-5 → 2-element disjunction set per §2.1 design-time-flexible row."""
    assert select_backend_class(_cell(_TEAM, _SELF)) == frozenset(
        {
            BackendClass.DEDICATED_LLM_OBS_PLATFORM_MULTI_NODE,
            BackendClass.OTEL_TO_VENDOR,
        }
    )


def test_cell_6_backend_class_singleton_cloud_native() -> None:
    """cell-6 → `{CLOUD_NATIVE_LLM_OBS_PLATFORM}` per §2.1."""
    assert select_backend_class(_cell(_TEAM, _CLOUD)) == frozenset(
        {BackendClass.CLOUD_NATIVE_LLM_OBS_PLATFORM}
    )


def test_cell_7_backend_class_singleton_self_hosted_multi_tenant() -> None:
    """cell-7 → `{SELF_HOSTED_MULTI_TENANT_LLM_OBS_PLATFORM}` per §2.1."""
    assert select_backend_class(_cell(_MTC, _SELF)) == frozenset(
        {BackendClass.SELF_HOSTED_MULTI_TENANT_LLM_OBS_PLATFORM}
    )


def test_cell_8_backend_class_singleton_vendor_managed_multi_tenant_or_managed_agent_runtime() -> (
    None
):
    """cell-8 → `{VENDOR_MANAGED_MULTI_TENANT_...}` per §2.1."""
    assert select_backend_class(_cell(_MTC, _CLOUD)) == frozenset(
        {BackendClass.VENDOR_MANAGED_MULTI_TENANT_LLM_OBS_OR_CLOUD_NATIVE_MANAGED_AGENT_RUNTIME}
    )


# --- acc #4 ----------------------------------------------------------------
def test_enumerate_candidates_per_cell_nonempty() -> None:
    """Per-cell `candidates` carries a non-empty witness column (acc #4 / #6)."""
    for cell in ACTIVE_CELLS:
        candidates = enumerate_candidates(cell)
        assert len(candidates) >= 1
        for witness in candidates:
            assert witness.candidate_name
            assert witness.vendor_class
            assert witness.deployment_form


# --- acc #5 ----------------------------------------------------------------
def test_select_backend_class_excluded_cell_returns_err() -> None:
    """`select_backend_class(EXCLUDED_CELL)` raises `CellBindingViolation` (acc #5)."""
    with pytest.raises(CellBindingViolation):
        select_backend_class(EXCLUDED_CELL)
    with pytest.raises(CellBindingViolation):
        enumerate_candidates(EXCLUDED_CELL)


# --- acc #7 — cell-class commitment invariant per §2.3 ---------------------
def test_backend_class_set_nonempty_all_cells() -> None:
    """Every ACTIVE cell carries a non-empty `backend_class` set (§2.3, acc #7)."""
    for cell in ACTIVE_CELLS:
        assert len(select_backend_class(cell)) >= 1


def test_backend_class_set_cardinality_one_or_two_all_cells() -> None:
    """Every ACTIVE cell's set has cardinality 1 or 2 (§2.3, acc #7).

    Singleton for the six committed cells (1/2/3/6/7/8); 2-element for the two
    design-time-flexible cells (cell-4, cell-5).
    """
    cardinality_two = {_cell(_TEAM, _LOCAL), _cell(_TEAM, _SELF)}
    for cell in ACTIVE_CELLS:
        size = len(select_backend_class(cell))
        if cell in cardinality_two:
            assert size == 2
        else:
            assert size == 1
