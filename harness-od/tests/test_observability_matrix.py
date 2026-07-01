"""Tests for U-OD-01 — 9-cell observability matrix (C-OD-01).

Test set per the U-OD-01 `Tests:` field. v2.6 declaration-site conversion: the
`test_persona_tier_*` / `test_deployment_surface_*` in-unit-declaration tests
are removed (the enums moved to U-CORE-01 and are tested there); the five v2.6
conversion tests are added.
"""

from __future__ import annotations

import inspect

import harness_core
import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od import observability_matrix
from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    EXCLUDED_CELL,
    EXCLUDED_CELL_RATIONALE,
    CellBindingViolation,
    CellID,
    CellStatus,
    cell_status,
    reject_excluded_cell,
)

# Acceptance #7 — EXCLUDED rationale per §1.4, byte-exact.
_SPEC_RATIONALE = (
    "compliance-readiness foundational primitives (tenant isolation, "
    "encryption-at-rest with vendor-managed key custody, retention controls) "
    "are incompatible with single-developer-machine deployment"
)


def test_cell_id_product_nine() -> None:
    """Acceptance #3 — CellID is the PersonaTier x DeploymentSurface 9-cell product."""
    cells = {
        CellID(persona_tier=pt, deployment_surface=ds)
        for pt in PersonaTier
        for ds in DeploymentSurface
    }
    assert len(cells) == 9


def test_active_cells_cardinality_eight() -> None:
    """Acceptance #4 — ACTIVE_CELLS has cardinality 8."""
    assert len(ACTIVE_CELLS) == 8


def test_excluded_cell_byte_exact() -> None:
    """Acceptance #4 — EXCLUDED_CELL is (MULTI_TENANT_COMPLIANCE, LOCAL_DEVELOPMENT)."""
    assert EXCLUDED_CELL == CellID(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert EXCLUDED_CELL not in ACTIVE_CELLS


def test_cell_status_excluded() -> None:
    """Acceptance #5 — the excluded cell reports EXCLUDED."""
    assert cell_status(EXCLUDED_CELL) is CellStatus.EXCLUDED


def test_cell_status_active_others() -> None:
    """Acceptance #5 — all 8 other cells report ACTIVE."""
    for cell in ACTIVE_CELLS:
        assert cell_status(cell) is CellStatus.ACTIVE


def test_reject_excluded_cell_returns_err() -> None:
    """Acceptance #6 — reject_excluded_cell raises for the EXCLUDED cell."""
    with pytest.raises(CellBindingViolation):
        reject_excluded_cell(EXCLUDED_CELL)


def test_reject_active_cell_returns_ok() -> None:
    """Acceptance #6 — reject_excluded_cell returns None for any ACTIVE cell."""
    for cell in ACTIVE_CELLS:
        assert reject_excluded_cell(cell) is None


def test_cell_id_eq_and_hash_stable() -> None:
    """Acceptance #9 — CellID is Eq + Hash over its two fields."""
    a = CellID(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
    )
    b = CellID(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
    )
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_excluded_rationale_byte_exact() -> None:
    """Acceptance #7 — EXCLUDED rationale matches §1.4 (acceptance #7) verbatim."""
    assert EXCLUDED_CELL_RATIONALE == _SPEC_RATIONALE


def test_cell_id_serialization_round_trip() -> None:
    """Acceptance #9 — CellID is stable under serialization."""
    cell = CellID(
        persona_tier=PersonaTier.TEAM_BINDING,
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
    )
    assert CellID.model_validate_json(cell.model_dump_json()) == cell


# --- v2.6 declaration-site-conversion tests ---------------------------------


def test_deployment_surface_imported_from_harness_core() -> None:
    """v2.6 conversion — `DeploymentSurface` resolves to the U-CORE-01 carrier."""
    assert observability_matrix.DeploymentSurface is harness_core.DeploymentSurface
    assert DeploymentSurface.__module__ == "harness_core.deployment_surface"


def test_persona_tier_imported_from_harness_core() -> None:
    """v2.6 conversion — `PersonaTier` resolves to the U-CORE-01 carrier."""
    assert observability_matrix.PersonaTier is harness_core.PersonaTier
    assert PersonaTier.__module__ == "harness_core.persona_tier"


def test_cell_id_fields_resolve_to_harness_core_enums() -> None:
    """v2.6 conversion — `CellID`'s fields resolve to the imported carriers."""
    cell = CellID(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
    )
    assert type(cell.persona_tier) is harness_core.PersonaTier
    assert type(cell.deployment_surface) is harness_core.DeploymentSurface


def test_depends_on_u_core_01_core_edge_declared() -> None:
    """v2.6 conversion — U-OD-01 takes its `[U-CORE-01 (cross-axis: core)]`
    edge: the observability-matrix module sources both enums from
    `harness-core`, not from any OD-local declaration."""
    assert observability_matrix.PersonaTier is harness_core.PersonaTier
    assert observability_matrix.DeploymentSurface is harness_core.DeploymentSurface


def test_no_local_deployment_surface_or_persona_tier_declaration() -> None:
    """v2.6 conversion — U-OD-01 does NOT redeclare the cross-cutting enums;
    a local `class PersonaTier`/`class DeploymentSurface` would be a defect."""
    source = inspect.getsource(observability_matrix)
    assert "class PersonaTier" not in source
    assert "class DeploymentSurface" not in source
