"""Tests for U-CP-53 — T-perm-3 three-layer composition (C-CP-23 §23.1-§23.4).

Acceptance-criterion coverage:
  #1 three orthogonal layers              -> test_compose_t_perm_3_three_orthogonal_layers
  #2 composition admissible               -> test_composition_admissible_for_valid_tuples
  #3 20-cell readings + per-engine map    -> test_per_cell_readings_cardinality_twenty,
                                             test_event_sourced_below_engine,
                                             test_save_point_above_engine,
                                             test_reconciler_loop_control_loop,
                                             test_wal_segment_above_engine
  #6 runtime-fault dispatch by reading    -> test_fault_above_engine_dispatches_to_harness,
                                             test_fault_below_engine_engine_native,
                                             test_fault_reconciler_crd_reconverges
  #7/#11 deterministic boundary 5 prims   -> test_deterministic_outer_harness_five_primitives,
                                             test_probabilistic_core_only_infer_surface
  #10 non-collapsing invariant            -> test_non_collapsing_invariant
  #15 composition deterministic           -> test_composition_deterministic
"""

from __future__ import annotations

from harness_core import WorkloadClass
from harness_cp.engine_class import EngineClass
from harness_cp.f2_substrate_join_discipline import F2JoinKind
from harness_cp.per_engine_class_topology_overlay import (
    CascadeEnforcementMechanism,
    TopologyFaultHandling,
)
from harness_cp.resumption_kind import ResumptionKind
from harness_cp.routing_layer import RoutingLayer
from harness_cp.t_perm_3_composition import (
    DETERMINISTIC_OUTER_HARNESS_BOUNDARY,
    PER_CELL_T_PERM_3_READINGS,
    D1LayerState,
    D4LayerState,
    F1LayerState,
    LayerOwner,
    PerCellReadingKind,
    RuntimeFault,
    compose_t_perm_3,
    handle_runtime_fault,
    read_per_cell_t_perm_3,
)
from harness_cp.topology_pattern import TopologyPattern


def _f1() -> F1LayerState:
    return F1LayerState(
        current_routing_layer=RoutingLayer.DECLARATIVE,
        fall_through_active=False,
        cross_family_active=False,
        cache_state_lost=False,
    )


def _d1() -> D1LayerState:
    return D1LayerState(
        engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
        f2_join_kind=F2JoinKind.HARNESS_OVERLAY_LEDGER,
        resumption_kind=ResumptionKind.SAVE_POINT_RESUME,
    )


def _d4(reading: TopologyFaultHandling = TopologyFaultHandling.ABOVE_ENGINE) -> D4LayerState:
    return D4LayerState(
        topology_pattern=TopologyPattern.ORCHESTRATOR_WORKERS,
        cascade_enforcement_mechanism=CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
        t_perm_3_reading=reading,
    )


def test_compose_t_perm_3_three_orthogonal_layers() -> None:
    """#1 — composition carries all three layer states independently."""
    comp = compose_t_perm_3(_f1(), _d1(), _d4())
    assert comp.f1_layer_state == _f1()
    assert comp.d1_layer_state == _d1()
    assert comp.d4_layer_state == _d4()


def test_composition_admissible_for_valid_tuples() -> None:
    """#2 — composition_admissible is True for valid layer-state tuples."""
    assert compose_t_perm_3(_f1(), _d1(), _d4()).composition_admissible is True


def test_per_cell_readings_cardinality_twenty() -> None:
    """#3 — exactly 20 cells (4 workload × 5 engine)."""
    assert len(PER_CELL_T_PERM_3_READINGS) == 20
    keys = {(c.workload_class, c.engine_class) for c in PER_CELL_T_PERM_3_READINGS}
    assert len(keys) == 20


def test_event_sourced_below_engine() -> None:
    """#3 — EVENT_SOURCED_REPLAY cells → below-engine; owner ENGINE."""
    for w in WorkloadClass:
        cell = read_per_cell_t_perm_3(w, EngineClass.EVENT_SOURCED_REPLAY)
        assert cell.t_perm_3_reading is PerCellReadingKind.BELOW_ENGINE_HARNESS_AUTHORS_TOPOLOGY
        assert cell.active_layer_owner is LayerOwner.ENGINE


def test_save_point_above_engine() -> None:
    """#3 — SAVE_POINT_CHECKPOINT cells → above-engine; owner HARNESS."""
    cell = read_per_cell_t_perm_3(
        WorkloadClass.SOFTWARE_ENGINEERING, EngineClass.SAVE_POINT_CHECKPOINT
    )
    assert cell.t_perm_3_reading is PerCellReadingKind.ABOVE_ENGINE_HARNESS_COMPOSES
    assert cell.active_layer_owner is LayerOwner.HARNESS


def test_wal_segment_above_engine() -> None:
    """#3 — WAL_SEGMENT cells → above-engine; owner HARNESS."""
    cell = read_per_cell_t_perm_3(WorkloadClass.RESEARCH, EngineClass.WAL_SEGMENT)
    assert cell.t_perm_3_reading is PerCellReadingKind.ABOVE_ENGINE_HARNESS_COMPOSES
    assert cell.active_layer_owner is LayerOwner.HARNESS


def test_pure_pattern_above_engine() -> None:
    """#3 — PURE_PATTERN_NO_ENGINE cells → above-engine; owner HARNESS."""
    cell = read_per_cell_t_perm_3(
        WorkloadClass.CONTENT_CREATION, EngineClass.PURE_PATTERN_NO_ENGINE
    )
    assert cell.t_perm_3_reading is PerCellReadingKind.ABOVE_ENGINE_HARNESS_COMPOSES
    assert cell.active_layer_owner is LayerOwner.HARNESS


def test_reconciler_loop_control_loop() -> None:
    """#3 — RECONCILER_LOOP cells → reconciler; owner CONTROL_LOOP."""
    cell = read_per_cell_t_perm_3(WorkloadClass.PIPELINE_AUTOMATION, EngineClass.RECONCILER_LOOP)
    assert cell.t_perm_3_reading is PerCellReadingKind.RECONCILER_CONTROL_LOOP_OWNS_RECONVERGENCE
    assert cell.active_layer_owner is LayerOwner.CONTROL_LOOP


def test_non_collapsing_invariant() -> None:
    """#10 — each cell has exactly one reading."""
    for cell in PER_CELL_T_PERM_3_READINGS:
        # The reading is a single PerCellReadingKind value.
        assert isinstance(cell.t_perm_3_reading, PerCellReadingKind)


def test_fault_above_engine_dispatches_to_harness() -> None:
    """#6 — ABOVE_ENGINE fault → harness composes recovery."""
    comp = compose_t_perm_3(_f1(), _d1(), _d4(TopologyFaultHandling.ABOVE_ENGINE))
    disp = handle_runtime_fault(RuntimeFault.LEASE_LOST, comp)
    assert disp.recovery_owner is LayerOwner.HARNESS
    assert "U-CP-09" in disp.recovery_delegate


def test_fault_below_engine_engine_native() -> None:
    """#6 — BELOW_ENGINE fault → engine-native cancellation."""
    comp = compose_t_perm_3(_f1(), _d1(), _d4(TopologyFaultHandling.BELOW_ENGINE))
    disp = handle_runtime_fault(RuntimeFault.ENGINE_CANCELLATION, comp)
    assert disp.recovery_owner is LayerOwner.ENGINE
    assert "U-CP-24" in disp.recovery_delegate


def test_fault_reconciler_crd_reconverges() -> None:
    """#6 — RECONCILER fault → CRD reconciler reconverges."""
    comp = compose_t_perm_3(_f1(), _d1(), _d4(TopologyFaultHandling.RECONCILER))
    disp = handle_runtime_fault(RuntimeFault.RECONCILER_DIVERGENCE, comp)
    assert disp.recovery_owner is LayerOwner.CONTROL_LOOP
    assert "reconciler" in disp.recovery_delegate.lower()


def test_deterministic_outer_harness_five_primitives() -> None:
    """#7/#11 — the deterministic boundary declares exactly 5 primitives."""
    assert len(DETERMINISTIC_OUTER_HARNESS_BOUNDARY.deterministic_primitives) == 5


def test_probabilistic_core_only_infer_surface() -> None:
    """#7 — the probabilistic core is the infer(...) surface only."""
    assert "infer(" in DETERMINISTIC_OUTER_HARNESS_BOUNDARY.probabilistic_core_surface


def test_composition_deterministic() -> None:
    """#15 — composition is deterministic given inputs."""
    a = compose_t_perm_3(_f1(), _d1(), _d4())
    b = compose_t_perm_3(_f1(), _d1(), _d4())
    assert a == b


def test_layer_owner_three_values() -> None:
    """LayerOwner is the promoted 3-value enum {HARNESS, ENGINE, CONTROL_LOOP}."""
    assert {m.value for m in LayerOwner} == {"harness", "engine", "control-loop"}


def test_runtime_fault_promoted_enum() -> None:
    """RuntimeFault is the promoted enum — one class per §23.3 dispatch branch."""
    assert len(RuntimeFault) == 3
