"""Tests for U-CP-40 — persona-tier × engine-class HITL matrix.

Acceptance-criterion coverage (C-CP-18 §18.1 + §18.2):
  #1 PersonaTier 3 values (now U-CORE-01) -> test_persona_tier_imported_from_harness_core
  #2 SynchronyClass 4 values              -> test_synchrony_class_cardinality_four
  #2 (v2.6) single nominal PersonaTier    -> test_persona_engine_matrix_composes_single_nominal_type
  #3 HITL_MATRIX 15 entries               -> test_hitl_matrix_cardinality_fifteen
  #3 cells match spec                     -> test_matrix_cells_match_spec
  #4 team/multi pure-pattern EXCLUDED     -> test_team_binding_pure_pattern_excluded,
                                             test_multi_tenant_pure_pattern_excluded,
                                             test_exclusion_source_cites_c_cp_07
  #5 non-excluded cells have a primitive  -> test_non_excluded_cells_have_primitive
  #7 BOTH_BY_TIER at team/save-point      -> test_both_by_tier_at_team_save_point
  v2.6 no local PersonaTier enum          -> test_no_local_persona_tier_enum
"""

from __future__ import annotations

import harness_cp.persona_engine_hitl_matrix as matrix_mod
from harness_core import PersonaTier
from harness_cp.engine_class import EngineClass
from harness_cp.persona_engine_hitl_matrix import (
    HITL_MATRIX,
    HITLPrimitiveShape,
    SynchronyClass,
    matrix_cell_for,
)


def test_persona_tier_imported_from_harness_core() -> None:
    """Acceptance #1 / v2.6 — `PersonaTier` resolves to the harness-core enum."""
    assert len(PersonaTier) == 3
    assert {t.name for t in PersonaTier} == {
        "SOLO_DEVELOPER",
        "TEAM_BINDING",
        "MULTI_TENANT_COMPLIANCE",
    }


def test_no_local_persona_tier_enum() -> None:
    """v2.6 — the module does not re-declare a local `PersonaTier`."""
    assert not hasattr(matrix_mod, "PersonaTier") or (
        matrix_mod.PersonaTier is PersonaTier  # type: ignore[attr-defined]
    )


def test_persona_engine_matrix_composes_single_nominal_type() -> None:
    """v2.6 acceptance #2 — every cell's `persona_tier` is the harness-core type."""
    for cell in HITL_MATRIX:
        assert isinstance(cell.persona_tier, PersonaTier)


def test_synchrony_class_cardinality_four() -> None:
    """Acceptance #2 — `SynchronyClass` declares exactly four values."""
    assert len(SynchronyClass) == 4
    assert {s.name for s in SynchronyClass} == {
        "SYNC_BLOCKING",
        "DURABLE_ASYNC",
        "BOTH_BY_TIER",
        "EXCLUDED",
    }


def test_hitl_matrix_cardinality_fifteen() -> None:
    """Acceptance #3 — `HITL_MATRIX` declares exactly 15 entries (3 × 5)."""
    assert len(HITL_MATRIX) == 15
    pairs = {(c.persona_tier, c.engine_class) for c in HITL_MATRIX}
    assert len(pairs) == 15
    for tier in PersonaTier:
        for engine in EngineClass:
            assert (tier, engine) in pairs


def test_matrix_cells_match_spec() -> None:
    """Acceptance #3 — sampled cell synchrony classes match §18.1 verbatim."""
    assert (
        matrix_cell_for(
            PersonaTier.SOLO_DEVELOPER, EngineClass.EVENT_SOURCED_REPLAY
        ).synchrony_class
        is SynchronyClass.SYNC_BLOCKING
    )
    assert (
        matrix_cell_for(PersonaTier.SOLO_DEVELOPER, EngineClass.RECONCILER_LOOP).synchrony_class
        is SynchronyClass.DURABLE_ASYNC
    )
    assert (
        matrix_cell_for(
            PersonaTier.MULTI_TENANT_COMPLIANCE, EngineClass.WAL_SEGMENT
        ).synchrony_class
        is SynchronyClass.DURABLE_ASYNC
    )
    # Every HITL primitive shape is used somewhere in the matrix.
    used = {s for c in HITL_MATRIX for s in c.primary_primitive_shapes}
    assert used == set(HITLPrimitiveShape)


def test_team_binding_pure_pattern_excluded() -> None:
    """Acceptance #4 — (team-binding, pure-pattern) cell is excluded."""
    cell = matrix_cell_for(PersonaTier.TEAM_BINDING, EngineClass.PURE_PATTERN_NO_ENGINE)
    assert cell.is_excluded
    assert cell.synchrony_class is SynchronyClass.EXCLUDED


def test_multi_tenant_pure_pattern_excluded() -> None:
    """Acceptance #4 — (multi-tenant, pure-pattern) cell is excluded."""
    cell = matrix_cell_for(PersonaTier.MULTI_TENANT_COMPLIANCE, EngineClass.PURE_PATTERN_NO_ENGINE)
    assert cell.is_excluded
    assert cell.synchrony_class is SynchronyClass.EXCLUDED


def test_exclusion_source_cites_c_cp_07() -> None:
    """Acceptance #4 — excluded cells cite `C-CP-07 §7.2` (§18.2 inheritance)."""
    excluded = [c for c in HITL_MATRIX if c.is_excluded]
    assert len(excluded) == 2
    for c in excluded:
        assert c.exclusion_source == "C-CP-07 §7.2"
    for c in HITL_MATRIX:
        if not c.is_excluded:
            assert c.exclusion_source is None


def test_non_excluded_cells_have_primitive() -> None:
    """Acceptance #5 — every non-excluded cell carries >= 1 primitive shape."""
    for c in HITL_MATRIX:
        if not c.is_excluded:
            assert len(c.primary_primitive_shapes) >= 1
        else:
            assert c.primary_primitive_shapes == ()


def test_both_by_tier_at_team_save_point() -> None:
    """Acceptance #7 — (team-binding, save-point) uses BOTH_BY_TIER."""
    cell = matrix_cell_for(PersonaTier.TEAM_BINDING, EngineClass.SAVE_POINT_CHECKPOINT)
    assert cell.synchrony_class is SynchronyClass.BOTH_BY_TIER
    assert HITLPrimitiveShape.CLAUDE_CODE_PERMISSION_MODEL in (cell.primary_primitive_shapes)
