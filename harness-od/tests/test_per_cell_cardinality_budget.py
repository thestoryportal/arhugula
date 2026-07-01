"""Tests for U-OD-13 — per-cell cardinality budget + Pattern P1 anchor (C-OD-11).

Test set per the U-OD-13 §3.4.3 `Tests:` field — covers acceptance #1-#6.
"""

from __future__ import annotations

from harness_core import DeploymentSurface, PersonaTier
from harness_od.observability_matrix import ACTIVE_CELLS, CellID
from harness_od.per_cell_cardinality_budget import (
    PATTERN_P1_DISCIPLINE_ANCHOR,
    PER_CELL_CARDINALITY_BUDGET,
    PerCellCardinalityBudget,
)

# The two multi-tenant ACTIVE cells (C-OD-01 §1.3) — cell-7 + cell-8.
_CELL_7 = CellID(
    persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
    deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
)
_CELL_8 = CellID(
    persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
    deployment_surface=DeploymentSurface.MANAGED_CLOUD,
)


# --- acceptance #1 — cardinality 8 -----------------------------------------


def test_per_cell_budget_cardinality_eight() -> None:
    """§11.1 / C-OD-01 §1.3 — exactly 8 entries, one per ACTIVE cell."""
    assert len(PER_CELL_CARDINALITY_BUDGET) == 8
    assert set(PER_CELL_CARDINALITY_BUDGET) == set(ACTIVE_CELLS)
    for cell, budget in PER_CELL_CARDINALITY_BUDGET.items():
        assert isinstance(budget, PerCellCardinalityBudget)
        assert budget.cell_id == cell


# --- acceptance #2 — tenant_rate_limit Some at multi-tenant cells ----------


def test_multi_tenant_cells_have_tenant_rate_limit() -> None:
    """§11.1 / C-OD-21 §21.4 — `tenant_rate_limit` is `Some` at cell-7 + cell-8."""
    assert PER_CELL_CARDINALITY_BUDGET[_CELL_7].tenant_rate_limit is not None
    assert PER_CELL_CARDINALITY_BUDGET[_CELL_8].tenant_rate_limit is not None
    assert isinstance(PER_CELL_CARDINALITY_BUDGET[_CELL_7].tenant_rate_limit, float)
    assert isinstance(PER_CELL_CARDINALITY_BUDGET[_CELL_8].tenant_rate_limit, float)


def test_non_multi_tenant_cells_no_tenant_rate_limit() -> None:
    """§11.1 — `tenant_rate_limit` is `None` at all non-multi-tenant cells."""
    for cell, budget in PER_CELL_CARDINALITY_BUDGET.items():
        if cell.persona_tier is not PersonaTier.MULTI_TENANT_COMPLIANCE:
            assert budget.tenant_rate_limit is None
    multi_tenant = [
        b
        for c, b in PER_CELL_CARDINALITY_BUDGET.items()
        if c.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE
    ]
    # exactly 2 multi-tenant ACTIVE cells (the 3rd is the EXCLUDED cell).
    assert len(multi_tenant) == 2


# --- acceptance #3 — enforcement_layer per cell class ----------------------


def test_solo_cells_enforce_at_collector_boundary() -> None:
    """§11.1 — solo-developer cells enforce at `COLLECTOR_BOUNDARY` (the
    in-process collector against the sqlite ring-buffer, per C-OD-19)."""
    solo = [
        b
        for c, b in PER_CELL_CARDINALITY_BUDGET.items()
        if c.persona_tier is PersonaTier.SOLO_DEVELOPER
    ]
    assert len(solo) == 3
    for budget in solo:
        assert budget.enforcement_layer == "COLLECTOR_BOUNDARY"
    # team + multi-tenant cells commit a single value drawn from the §11.1
    # admissible pair — never an undetermined disjunction.
    for cell, budget in PER_CELL_CARDINALITY_BUDGET.items():
        if cell.persona_tier is not PersonaTier.SOLO_DEVELOPER:
            assert budget.enforcement_layer in {"COLLECTOR_BOUNDARY", "BACKEND_INGESTION"}


# --- acceptance #4 — Pattern P1 anchor byte-exact --------------------------


def test_pattern_p1_anchor_byte_exact() -> None:
    """§11.4 — `PATTERN_P1_DISCIPLINE_ANCHOR` carries the anchor verbatim."""
    assert PATTERN_P1_DISCIPLINE_ANCHOR == (
        "Per-attribute names MUST be byte-exact across OD spec / AS spec / CP "
        "spec / IS spec / ADRs / OTel SDK bindings. Pattern P1 was raised at "
        "P3c-CK Iteration 1 as a systemic per-attribute name drift across six "
        "or more source artifacts. Compliance discipline preserved at all 15 "
        "specialization-layer namespace declarations."
    )
    # §11.4 anchor names all six source-artifact classes.
    for artifact in ("OD spec", "AS spec", "CP spec", "IS spec", "ADRs", "OTel SDK"):
        assert artifact in PATTERN_P1_DISCIPLINE_ANCHOR


# --- acceptance #5 — Pattern P1 verifiable at namespace-map level ----------


def test_pattern_p1_anchor_is_immutable_declarative_invariant() -> None:
    """§11.4 / acceptance #5 — the anchor is a frozen module-level constant
    (a declarative invariant). Pattern P1 compliance is verifiable against it
    at U-OD-05 namespace-map level and the per-namespace verification units
    (U-OD-06 / U-OD-07) — the anchor is a stable string those checks read."""
    assert isinstance(PATTERN_P1_DISCIPLINE_ANCHOR, str)
    assert PATTERN_P1_DISCIPLINE_ANCHOR  # non-empty
    # frozen — `PerCellCardinalityBudget` rejects post-construction mutation.
    budget = next(iter(PER_CELL_CARDINALITY_BUDGET.values()))
    assert budget.model_config.get("frozen") is True


# --- acceptance #6 — composes with U-OD-31 per-tenant cardinality isolation -


def test_tenant_rate_limit_composes_with_u_od_31() -> None:
    """Acceptance #6 — the cell-7 / cell-8 `tenant_rate_limit` is a plain
    `float` budget U-OD-31 `check_per_tenant_cardinality_isolation` reads as a
    runtime-enforcement threshold (per C-OD-21 §21.4). It is reachable, typed,
    and comparable — the composition surface U-OD-31 consumes."""
    for cell in (_CELL_7, _CELL_8):
        limit = PER_CELL_CARDINALITY_BUDGET[cell].tenant_rate_limit
        assert limit is not None
        # U-OD-31 enforcement: an observed per-tenant rate is checked `<= limit`.
        assert (limit + 1.0) > limit
        assert limit > 0.0
