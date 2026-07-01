"""T-perm-3 three-layer composition + per-cell reading + outer-harness boundary — U-CP-53.

Implements C-CP-23 §23.1 (the F1/D1/D4 three-layer orthogonal composition),
§23.2 (the 20-cell per-cell T-perm-3 reading table), §23.3 (runtime-fault
dispatch by reading), and §23.4 (the deterministic outer-harness boundary
declaration — the closure of the CP plan's architectural commitment).

Declares:
  - `LayerOwner` — the layer that owns fault recovery for a cell ({HARNESS,
    ENGINE, CONTROL_LOOP}; promoted from the v2.6 §0.11.4 inline-comment enum).
  - `RuntimeFault` — the runtime-fault classes the dispatcher handles
    (promoted from the v2.6 §0.11.4 ellipsis inline-comment enum; values
    completed against C-CP-23 §23.3).
  - `F1LayerState` / `D1LayerState` / `D4LayerState` / `TPerm3LayerComposition`
    — the three orthogonal layer states + their composition.
  - `PerCellReadingKind` — the 3 §23.2 per-cell readings.
  - `PerCellTPerm3Reading` + `PER_CELL_T_PERM_3_READINGS` — the §23.2 20-cell
    (4 workload × 5 engine) table.
  - `DeterministicOuterHarnessBoundary` — the §23.4 / ADD §5.3.3 boundary
    declaration: the probabilistic core + the 5 deterministic outer-harness
    primitives.
  - `compose_t_perm_3` / `read_per_cell_t_perm_3` / `handle_runtime_fault` —
    the composition + reading + fault-dispatch functions.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-53 (preserved
verbatim through v2.9; v2.6 §0.11.4 promoted `LayerOwner` + `RuntimeFault`
inline-comment enums); Spec_Control_Plane_v1_2.md §23 C-CP-23 §23.1-§23.4
(preserved verbatim into v1.3); Architectural_Design_Document_v1_3.md §5.3.3.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass
from harness_cp.f2_substrate_join_discipline import F2JoinKind
from harness_cp.per_engine_class_topology_overlay import (
    CascadeEnforcementMechanism,
    TopologyFaultHandling,
)
from harness_cp.resumption_kind import ResumptionKind
from harness_cp.routing_layer import RoutingLayer
from harness_cp.topology_pattern import TopologyPattern


class LayerOwner(StrEnum):
    """The layer that owns fault recovery for a T-perm-3 cell (C-CP-23 §23.2).

    Promoted from the v2.6 §0.11.4 inline-comment enum `{HARNESS, ENGINE,
    CONTROL_LOOP}` — fully enumerated, promoted directly.
    """

    HARNESS = "harness"
    ENGINE = "engine"
    CONTROL_LOOP = "control-loop"


class RuntimeFault(StrEnum):
    """The runtime-fault classes the §23.3 dispatcher handles.

    Promoted from the v2.6 §0.11.4 ellipsis inline-comment enum; the value set
    is completed against C-CP-23 §23.3 — one fault class per §23.3 dispatch
    branch (the harness-composed, engine-native, and reconciler-driven
    recovery paths).
    """

    LEASE_LOST = "lease-lost"
    """Harness-side recovery: lease re-acquisition + dedup + resumption."""

    ENGINE_CANCELLATION = "engine-cancellation"
    """Engine-native cancellation propagates; harness observes topology."""

    RECONCILER_DIVERGENCE = "reconciler-divergence"
    """CRD reconciler reconverges; harness emits topology spans only."""


class F1LayerState(BaseModel):
    """The F1 routing/fallback layer state (C-CP-23 §23.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_routing_layer: RoutingLayer
    fall_through_active: bool
    cross_family_active: bool
    cache_state_lost: bool


class D1LayerState(BaseModel):
    """The D1 engine-class layer state (C-CP-23 §23.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: EngineClass
    f2_join_kind: F2JoinKind
    resumption_kind: ResumptionKind


class D4LayerState(BaseModel):
    """The D4 topology layer state (C-CP-23 §23.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topology_pattern: TopologyPattern
    cascade_enforcement_mechanism: CascadeEnforcementMechanism
    t_perm_3_reading: TopologyFaultHandling


class TPerm3LayerComposition(BaseModel):
    """The orthogonal F1/D1/D4 three-layer composition (C-CP-23 §23.1).

    The three layer states are carried independently — no layer collapses into
    another (acceptance #1). `composition_admissible` is the orthogonality
    invariant: the product space is admissible without cross-layer constraints
    (acceptance #12).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    f1_layer_state: F1LayerState
    d1_layer_state: D1LayerState
    d4_layer_state: D4LayerState
    composition_admissible: bool


class PerCellReadingKind(StrEnum):
    """The 3 per-cell T-perm-3 readings (C-CP-23 §23.2, verbatim)."""

    ABOVE_ENGINE_HARNESS_COMPOSES = "above-engine-harness-composes"
    BELOW_ENGINE_HARNESS_AUTHORS_TOPOLOGY = "below-engine-harness-authors-topology"
    RECONCILER_CONTROL_LOOP_OWNS_RECONVERGENCE = "reconciler-control-loop-owns-reconvergence"


class PerCellTPerm3Reading(BaseModel):
    """One cell of the §23.2 20-cell per-cell T-perm-3 reading table.

    Keyed on (workload_class, engine_class). The non-collapsing invariant
    (acceptance #10): each cell has exactly one reading.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_class: WorkloadClass
    engine_class: EngineClass
    t_perm_3_reading: PerCellReadingKind
    active_layer_owner: LayerOwner


def _reading_for(engine: EngineClass) -> tuple[PerCellReadingKind, LayerOwner]:
    """Map an engine class to its §23.2 per-cell reading + active layer owner.

    Per C-CP-23 §23.2 verbatim: `EVENT_SOURCED_REPLAY` → below-engine (engine
    owns); `SAVE_POINT_CHECKPOINT` / `PURE_PATTERN_NO_ENGINE` / `WAL_SEGMENT`
    → above-engine (harness composes); `RECONCILER_LOOP` → reconciler
    (control-loop owns reconvergence). The reading is engine-keyed — every
    workload in a given engine column carries the same reading.
    """
    if engine is EngineClass.EVENT_SOURCED_REPLAY:
        return (
            PerCellReadingKind.BELOW_ENGINE_HARNESS_AUTHORS_TOPOLOGY,
            LayerOwner.ENGINE,
        )
    if engine is EngineClass.RECONCILER_LOOP:
        return (
            PerCellReadingKind.RECONCILER_CONTROL_LOOP_OWNS_RECONVERGENCE,
            LayerOwner.CONTROL_LOOP,
        )
    # SAVE_POINT_CHECKPOINT / PURE_PATTERN_NO_ENGINE / WAL_SEGMENT.
    return (PerCellReadingKind.ABOVE_ENGINE_HARNESS_COMPOSES, LayerOwner.HARNESS)


PER_CELL_T_PERM_3_READINGS: tuple[PerCellTPerm3Reading, ...] = tuple(
    PerCellTPerm3Reading(
        workload_class=workload,
        engine_class=engine,
        t_perm_3_reading=_reading_for(engine)[0],
        active_layer_owner=_reading_for(engine)[1],
    )
    for workload in WorkloadClass
    for engine in EngineClass
)
"""The §23.2 per-cell T-perm-3 reading table — exactly 20 cells (4 workload ×
5 engine). Non-collapsing: each cell has exactly one reading (acceptance #10).

Per-deployment cell exclusion (acceptance #5 — `PURE_PATTERN_NO_ENGINE` at
`self-hosted-server` / `managed-cloud`, `RECONCILER_LOOP` at
`local-development`) is a *deployment-surface* concern, owned by U-CP-16's
`exclusion_reasons` map; the §23.2 reading table is keyed on
(workload, engine) and carries no deployment axis, so all 20 cells carry a
reading. Deployment-conditioned admissibility is resolved at U-CP-16, not in
this record.
"""


class DeterministicOuterHarnessBoundary(BaseModel):
    """The §23.4 / ADD §5.3.3 deterministic outer-harness boundary declaration.

    The closure of the CP plan's architectural commitment (acceptance #17):
    everything outside the probabilistic core is deterministic. The 5
    deterministic primitives are a byte-exact enumeration (acceptance #11).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    probabilistic_core_surface: str
    deterministic_primitives: tuple[str, ...]
    boundary_contract: str


DETERMINISTIC_OUTER_HARNESS_BOUNDARY: DeterministicOuterHarnessBoundary = (
    DeterministicOuterHarnessBoundary(
        probabilistic_core_surface="infer(...) per U-CP-03 (LLM inference)",
        deterministic_primitives=(
            "chain-advancement (U-CP-09 cross-family fallback)",
            "cascade-enforcement (U-CP-25 D4 tunable)",
            "retry-mechanics (U-CP-07 retry.* namespace; harness-anchored)",
            "breaker-mechanics (harness.breaker.* substrate-anchored at C9 per U-CP-07)",
            "hitl-escalation (U-CP-47 + U-CP-48 staircase + U-CP-49 pause/resume)",
        ),
        boundary_contract=(
            "Everything outside the probabilistic core infer(...) surface is "
            "deterministic (ADD §5.3.3 / C-CP-23 §23.4). The 5 deterministic "
            "outer-harness primitives are a byte-exact enumeration; addition or "
            "removal is a Workflow §4.1.2 Class-2 D1+D4+F1 revision."
        ),
    )
)
"""The §23.4 deterministic outer-harness boundary — 5 deterministic primitives
(acceptance #7/#11)."""


class FaultHandlingDisposition(BaseModel):
    """The dispatch disposition of a runtime fault (C-CP-23 §23.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fault: RuntimeFault
    reading: PerCellReadingKind
    recovery_owner: LayerOwner
    recovery_delegate: str
    """The unit(s) the §23.3 dispatcher delegates recovery to."""


def compose_t_perm_3(
    f1_layer_state: F1LayerState,
    d1_layer_state: D1LayerState,
    d4_layer_state: D4LayerState,
) -> TPerm3LayerComposition:
    """Compose the F1/D1/D4 three orthogonal layers (C-CP-23 §23.1).

    The three layer states are carried independently — no layer collapses
    (acceptance #1). The composition is deterministic given inputs and
    binding-time, not runtime (acceptance #9/#15). `composition_admissible` is
    `True` for all valid layer-state tuples — the orthogonal contract
    (acceptance #2/#12): the product space is admissible without cross-layer
    constraints.
    """
    return TPerm3LayerComposition(
        f1_layer_state=f1_layer_state,
        d1_layer_state=d1_layer_state,
        d4_layer_state=d4_layer_state,
        composition_admissible=True,
    )


def read_per_cell_t_perm_3(workload: WorkloadClass, engine: EngineClass) -> PerCellTPerm3Reading:
    """Read the §23.2 per-cell T-perm-3 reading for a (workload, engine) cell.

    Inherits the U-CP-24 per-engine overlay (acceptance #4); the non-collapsing
    invariant holds — exactly one reading per cell.
    """
    for cell in PER_CELL_T_PERM_3_READINGS:
        if cell.workload_class is workload and cell.engine_class is engine:
            return cell
    raise KeyError(f"no T-perm-3 cell for ({workload.value}, {engine.value})")


def handle_runtime_fault(
    fault: RuntimeFault, composition: TPerm3LayerComposition
) -> FaultHandlingDisposition:
    """Dispatch a runtime fault by the T-perm-3 reading (C-CP-23 §23.3).

    Dispatches by the D4 layer's `t_perm_3_reading`:
      - `ABOVE_ENGINE` → harness composes lease re-acquisition + dedup +
        resumption (via U-CP-09, U-CP-20).
      - `BELOW_ENGINE` → engine-native cancellation propagates; harness becomes
        a topology-author observer (via U-CP-24 overlay).
      - `RECONCILER` → CRD reconciler reconverges; harness emits topology spans
        only.
    The dispatcher delegates recovery; it does not implement recovery itself
    (acceptance #14).
    """
    tfh = composition.d4_layer_state.t_perm_3_reading
    if tfh is TopologyFaultHandling.ABOVE_ENGINE:
        return FaultHandlingDisposition(
            fault=fault,
            reading=PerCellReadingKind.ABOVE_ENGINE_HARNESS_COMPOSES,
            recovery_owner=LayerOwner.HARNESS,
            recovery_delegate="U-CP-09 + U-CP-20 (lease re-acquisition + dedup + resumption)",
        )
    if tfh is TopologyFaultHandling.BELOW_ENGINE:
        return FaultHandlingDisposition(
            fault=fault,
            reading=PerCellReadingKind.BELOW_ENGINE_HARNESS_AUTHORS_TOPOLOGY,
            recovery_owner=LayerOwner.ENGINE,
            recovery_delegate="U-CP-24 overlay (engine-native cancellation; harness observes)",
        )
    return FaultHandlingDisposition(
        fault=fault,
        reading=PerCellReadingKind.RECONCILER_CONTROL_LOOP_OWNS_RECONVERGENCE,
        recovery_owner=LayerOwner.CONTROL_LOOP,
        recovery_delegate="U-CP-25 CRD reconciler (reconvergence; harness emits topology spans)",
    )
