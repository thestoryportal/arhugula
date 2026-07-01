"""Workflow execution driver typed errors — U-CP-56.

Implements C-CP-25 §25.7 verbatim for failure modes 1 and 2:
- `TopologyPatternNotYetMaterializedError` — raised when manifest declares a
  topology pattern outside the v1.4 in-scope set ({SINGLE_THREADED_LINEAR}).
- `EngineClassNotYetMaterializedError` — raised when manifest declares an
  engine class outside the v1.4 in-scope set ({pure-pattern-no-engine,
  save-point-checkpoint}). Introduced for symmetry per CP spec v1.4 Adjacent
  finding §C.

Authority:
- `Spec_Control_Plane_v1_4.md` §25.7 (failure-mode taxonomy)
- `Implementation_Plan_Control_Plane_v2_11.md` U-CP-56 acceptance criterion #2
"""

from __future__ import annotations

from harness_cp.engine_class import EngineClass
from harness_cp.topology_pattern import TopologyPattern


class WorkflowDriverError(Exception):
    """Base class for U-CP-56 workflow-driver typed errors."""


class TopologyPatternNotYetMaterializedError(WorkflowDriverError):
    """The manifest's topology pattern is outside the v1.4 in-scope set.

    At C-CP-25 v1.4: only `SINGLE_THREADED_LINEAR` is materialized. Other 5
    patterns deferred per X-AL-3 (no silent design extension).
    """

    def __init__(self, pattern: TopologyPattern) -> None:
        super().__init__(
            f"topology pattern {pattern!r} is not yet materialized at "
            f"C-CP-25 v1.4 (only SINGLE_THREADED_LINEAR is in scope)"
        )
        self.pattern = pattern


class EngineClassNotYetMaterializedError(WorkflowDriverError):
    """The manifest's engine class is outside the v1.4 in-scope set.

    At C-CP-25 v1.4: only `pure-pattern-no-engine` and `save-point-checkpoint`
    are materialized. Other 3 engine classes (`event-sourced-replay`,
    `reconciler-loop`, `WAL-segment`) deferred per X-AL-3.

    Introduced for symmetry with `TopologyPatternNotYetMaterializedError`
    (CP spec v1.4 Adjacent finding §C); operator may rename at next revision-
    pass (mechanical token replacement).
    """

    def __init__(self, engine_class: EngineClass) -> None:
        super().__init__(
            f"engine class {engine_class!r} is not yet materialized at "
            f"C-CP-25 v1.4 (only pure-pattern-no-engine and "
            f"save-point-checkpoint are in scope)"
        )
        self.engine_class = engine_class


class BranchBarrierDeadlineExceededError(WorkflowDriverError):
    """A non-linear-topology branch barrier exceeded its wall-clock deadline.

    C-CP-25 §25.11 (bounded barriers — U-CP-82): every barrier (`TaskGroup` /
    `gather` join over branches) is wrapped in a wall-clock deadline so a stuck
    branch cannot strand its parent indefinitely. `bounded_barrier` raises this
    on deadline-exceeded. The deadline value is §25.18 impl-discretion supplied
    by the calling strategy (U-CP-86..U-CP-90), not minted as a driver config
    field here.

    Raising this enforces only the *bound*; the cascade-policy *reaction* to a
    deadline (cancel not-yet-dispatched siblings / proceed-degraded / pause)
    composes at U-CP-85 (§25.15) — out of U-CP-82's scope.
    """

    def __init__(self, deadline_seconds: float) -> None:
        super().__init__(
            f"branch barrier exceeded its {deadline_seconds}s wall-clock "
            f"deadline (C-CP-25 §25.11 bounded barriers)"
        )
        self.deadline_seconds = deadline_seconds


__all__ = [
    "BranchBarrierDeadlineExceededError",
    "EngineClassNotYetMaterializedError",
    "TopologyPatternNotYetMaterializedError",
    "WorkflowDriverError",
]
