"""Tests for U-CP-25 — workload x engine-class 2D matrix (C-CP-11 §11.3/§11.4).

Acceptance-criterion coverage:
  #1 20 cells (4x5)              -> test_workload_engine_matrix_cardinality_twenty
  #2 composes U-CP-23 + U-CP-24  -> test_matrix_composes_with_u_cp_23_defaults,
                                    test_matrix_composes_with_u_cp_24_overlay,
                                    test_excluded_cells_marked
  #3 lookup_cell deterministic   -> test_lookup_cell_deterministic
  #4 d4_tunable composition      -> test_d4_tunable_composition
"""

from __future__ import annotations

from harness_core import PersonaTier, WorkloadClass
from harness_cp.engine_class import EngineClass
from harness_cp.per_engine_class_topology_overlay import overlay_for
from harness_cp.per_workload_class_topology import PER_WORKLOAD_CLASS_TOPOLOGY
from harness_cp.topology_pattern import CascadePolicy
from harness_cp.workload_engine_class_matrix import (
    WORKLOAD_ENGINE_MATRIX,
    d4_tunable,
    lookup_cell,
)


def test_workload_engine_matrix_cardinality_twenty() -> None:
    """#1 — exactly 20 cells (4 workload classes x 5 engine classes)."""
    assert len(WORKLOAD_ENGINE_MATRIX) == 20
    pairs = {(c.workload_class, c.engine_class) for c in WORKLOAD_ENGINE_MATRIX}
    assert len(pairs) == 20


def test_matrix_composes_with_u_cp_23_defaults() -> None:
    """#2 — each cell's topology_pattern is the U-CP-23 per-workload default."""
    defaults = {c.workload_class: c.default_pattern for c in PER_WORKLOAD_CLASS_TOPOLOGY}
    for cell in WORKLOAD_ENGINE_MATRIX:
        assert cell.topology_pattern is defaults[cell.workload_class]


def test_matrix_composes_with_u_cp_24_overlay() -> None:
    """#2 — each cell's reading + cascade come from the U-CP-24 overlay."""
    for cell in WORKLOAD_ENGINE_MATRIX:
        overlay = overlay_for(cell.engine_class)
        assert cell.t_perm_3_reading is overlay.t_perm_3_reading
        assert cell.cascade_enforcement_mechanism is overlay.cascade_enforcement_mechanism


def test_excluded_cells_marked() -> None:
    """#2 — only pipeline-automation x pure-pattern-no-engine is inadmissible."""
    for cell in WORKLOAD_ENGINE_MATRIX:
        pair = (cell.workload_class, cell.engine_class)
        if pair == (WorkloadClass.PIPELINE_AUTOMATION, EngineClass.PURE_PATTERN_NO_ENGINE):
            assert cell.cell_admissible is False
        else:
            assert cell.cell_admissible is True


def test_lookup_cell_deterministic() -> None:
    """#3 — lookup_cell returns the same cell for the same pair."""
    a = lookup_cell(WorkloadClass.RESEARCH, EngineClass.WAL_SEGMENT)
    b = lookup_cell(WorkloadClass.RESEARCH, EngineClass.WAL_SEGMENT)
    assert a == b
    assert a.workload_class is WorkloadClass.RESEARCH
    assert a.engine_class is EngineClass.WAL_SEGMENT


def test_d4_tunable_composition() -> None:
    """#4 — d4_tunable returns the §11.4 4-tuple; persona drives cascade_policy."""
    cell = lookup_cell(WorkloadClass.SOFTWARE_ENGINEERING, EngineClass.SAVE_POINT_CHECKPOINT)
    solo = d4_tunable(cell, PersonaTier.SOLO_DEVELOPER)
    team = d4_tunable(cell, PersonaTier.TEAM_BINDING)
    multi = d4_tunable(cell, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert solo.cascade_policy is CascadePolicy.PROCEED
    assert team.cascade_policy is CascadePolicy.PAUSE
    assert multi.cascade_policy is CascadePolicy.CASCADE_CANCEL
    assert solo.topology_fault_handling is cell.t_perm_3_reading
    assert solo.workload_class is cell.workload_class
    assert solo.topology_pattern is cell.topology_pattern
