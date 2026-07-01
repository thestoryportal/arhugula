"""Workload-class x engine-class 2D matrix + D4 multiplicative tunable — U-CP-25.

Implements C-CP-11 §11.3 (the 2D matrix workload-class x D1-engine-class with
per-cell T-perm-3 reading) and §11.4 (the T-perm-3 D4-layer multiplicative
tunable specialization).

Declares:
  - `WorkloadEngineMatrixCell` — one cell of the 4 x 5 matrix.
  - `WORKLOAD_ENGINE_MATRIX` — exactly 20 cells.
  - `D4MultiplicativeTunable` — the §11.4 `(topology_fault_handling,
    workload_class, topology_pattern, cascade_policy)` 4-tuple.
  - `lookup_cell` / `d4_tunable` — deterministic accessors.

**Per-cell composition.** Each cell composes:
  - `topology_pattern` — the U-CP-23 per-workload-class default pattern;
  - `t_perm_3_reading` + `cascade_enforcement_mechanism` — the U-CP-24
    per-engine-class topology overlay (per-cell reading is determined by the
    engine-class column, per §11.3).
  - `cell_admissible` — `False` only where §11.3 reads "excluded" — i.e.
    `pipeline-automation x pure-pattern-no-engine` ("excluded for durable pole
    at scale" per C-CP-07 §7.2). §11.3 introduces no other exclusions.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.4 U-CP-25 (v2.6 §0.12
`[U-CP-00]` edge-add — CLEARED edge-add-only); Spec_Control_Plane_v1_2.md §11
C-CP-11 §11.3 + §11.4 (preserved verbatim into v1.3); ADR-D4 v1.1 §1.4 + §1.6.
"""

from __future__ import annotations

from harness_core import PersonaTier, WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass
from harness_cp.per_engine_class_topology_overlay import (
    CascadeEnforcementMechanism,
    TopologyFaultHandling,
    overlay_for,
)
from harness_cp.per_workload_class_topology import PER_WORKLOAD_CLASS_TOPOLOGY
from harness_cp.topology_pattern import CascadePolicy, TopologyPattern


class WorkloadEngineMatrixCell(BaseModel):
    """One cell of the workload-class x engine-class 2D matrix (C-CP-11 §11.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_class: WorkloadClass
    engine_class: EngineClass
    topology_pattern: TopologyPattern
    cascade_enforcement_mechanism: CascadeEnforcementMechanism
    t_perm_3_reading: TopologyFaultHandling
    cell_admissible: bool
    """`False` when (workload, engine) is structurally excluded per §11.3 —
    only `pipeline-automation x pure-pattern-no-engine` (durable-pole-at-scale
    exclusion inherited from C-CP-07 §7.2)."""


class D4MultiplicativeTunable(BaseModel):
    """The §11.4 D4-layer multiplicative tunable 4-tuple."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topology_fault_handling: TopologyFaultHandling
    workload_class: WorkloadClass
    topology_pattern: TopologyPattern
    cascade_policy: CascadePolicy


# --- §11.3 structural-exclusion set -----------------------------------------
# §11.3 reads "excluded" for exactly one cell; all others admissible.
_EXCLUDED_CELLS: frozenset[tuple[WorkloadClass, EngineClass]] = frozenset(
    {(WorkloadClass.PIPELINE_AUTOMATION, EngineClass.PURE_PATTERN_NO_ENGINE)}
)

_DEFAULT_PATTERN_BY_WORKLOAD: dict[WorkloadClass, TopologyPattern] = {
    c.workload_class: c.default_pattern for c in PER_WORKLOAD_CLASS_TOPOLOGY
}


# --- Matrix population (4 workload classes x 5 engine classes = 20 cells) ----

WORKLOAD_ENGINE_MATRIX: tuple[WorkloadEngineMatrixCell, ...] = tuple(
    WorkloadEngineMatrixCell(
        workload_class=wc,
        engine_class=ec,
        topology_pattern=_DEFAULT_PATTERN_BY_WORKLOAD[wc],
        cascade_enforcement_mechanism=overlay_for(ec).cascade_enforcement_mechanism,
        t_perm_3_reading=overlay_for(ec).t_perm_3_reading,
        cell_admissible=(wc, ec) not in _EXCLUDED_CELLS,
    )
    for wc in WorkloadClass
    for ec in EngineClass
)
"""The 20-cell workload-class x engine-class 2D matrix per C-CP-11 §11.3."""

_CELL_BY_PAIR: dict[tuple[WorkloadClass, EngineClass], WorkloadEngineMatrixCell] = {
    (c.workload_class, c.engine_class): c for c in WORKLOAD_ENGINE_MATRIX
}


def lookup_cell(workload: WorkloadClass, engine: EngineClass) -> WorkloadEngineMatrixCell:
    """Return the matrix cell for a (workload, engine) pair. Deterministic."""
    return _CELL_BY_PAIR[(workload, engine)]


def d4_tunable(
    cell: WorkloadEngineMatrixCell, persona_tier: PersonaTier
) -> D4MultiplicativeTunable:
    """Return the §11.4 D4 multiplicative tunable for a cell + persona tier.

    Persona tier influences the `cascade_policy` default: more conservative
    tiers default to `PAUSE`, solo-developer defaults to `PROCEED`."""
    if persona_tier is PersonaTier.SOLO_DEVELOPER:
        cascade_policy = CascadePolicy.PROCEED
    elif persona_tier is PersonaTier.TEAM_BINDING:
        cascade_policy = CascadePolicy.PAUSE
    else:
        cascade_policy = CascadePolicy.CASCADE_CANCEL
    return D4MultiplicativeTunable(
        topology_fault_handling=cell.t_perm_3_reading,
        workload_class=cell.workload_class,
        topology_pattern=cell.topology_pattern,
        cascade_policy=cascade_policy,
    )
