"""Engine-class taxonomy + capability-floor preservation invariant — U-CP-15.

Implements C-CP-07 §7.1 (five-element engine-class taxonomy) and §7.4
(capability-floor preservation per class). Declares the closed 5-value
`EngineClass` enum, the `CapabilityFloor` record, and the populated
`CAPABILITY_FLOORS`.

The taxonomy is **closed** at cardinality 5 per C-CP-07 §7.1 / ADR-D1 §1.1;
extension is a Workflow §4.1.2 Class-2 D1 revision.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-15 (preserved
verbatim at v2.2/v2.3); Spec_Control_Plane_v1_2.md §7 C-CP-07 §7.1 + §7.4
(preserved verbatim into v1.3); ADR-D1 v1.1 §1.1 + §1.4.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EngineClass(StrEnum):
    """The 5 durable-execution engine classes (C-CP-07 §7.1).

    Member string values are the §7.1 "Class" column verbatim — they match the
    `engine.class` span-attribute enumeration at C-CP-09 §9.1. Each member's
    durable-execution-substrate citation (acceptance #2) is in its docstring.
    """

    EVENT_SOURCED_REPLAY = "event-sourced-replay"
    """Lifecycle owned by the engine; replay from Event History with cached
    activity outputs. Substrate: Temporal / Restate / DBOS (C-CP-07 §7.1 row 1;
    candidate enumeration deferred per §7.4)."""

    SAVE_POINT_CHECKPOINT = "save-point-checkpoint"
    """Lifecycle owned by the application atop engine save points; harness
    composes lease + dedup + resumption. Substrate: DBOS / LangGraph
    checkpointer (C-CP-07 §7.1 row 2; candidates deferred per §7.4)."""

    PURE_PATTERN_NO_ENGINE = "pure-pattern-no-engine"
    """Lifecycle owned by the harness over the F2 substrate (filesystem-journal
    + state-ledger + idempotency-key). Substrate: F2 / 12-factor-agents pattern
    (C-CP-07 §7.1 row 3)."""

    RECONCILER_LOOP = "reconciler-loop"
    """Lifecycle owned by a K8s controller; CRDs persist agent state across
    restarts. Substrate: K8s CRD reconciler over etcd (C-CP-07 §7.1 row 4)."""

    WAL_SEGMENT = "WAL-segment"
    """Lifecycle owned by the harness; append-only segment log with per-segment
    resume. Substrate: Kafka-style WAL (C-CP-07 §7.1 row 5; implementation
    deferred per §7.4)."""


class CapabilityFloor(BaseModel):
    """One F3 capability-floor required across engine classes (C-CP-07 §7.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_name: str
    required_at_class: frozenset[EngineClass]
    """Engine classes at which the floor must be preserved. Per the C-CP-07
    §7.4 table every floor is populated for every class — all 5."""

    rationale: str
    """C-CP-07 §7.4 floor description."""


# --- Registry population (spec C-CP-07 §7.4 capability-floor table) ---------

_ALL_CLASSES: frozenset[EngineClass] = frozenset(EngineClass)

CAPABILITY_FLOORS: tuple[CapabilityFloor, ...] = (
    CapabilityFloor(
        capability_name="durable_replay_across_restart",
        required_at_class=_ALL_CLASSES,
        rationale=(
            "F3 capability-floor (i) — durable replay across restart. "
            "Preserved across all five engine classes per ADR-D1 v1.1 §1.4 "
            "(C-CP-07 §7.4)."
        ),
    ),
    CapabilityFloor(
        capability_name="idempotency_keyed_exactly_once",
        required_at_class=_ALL_CLASSES,
        rationale=(
            "F3 capability-floor (ii) — idempotency-keyed exactly-once via the "
            "F2 state ledger. Every engine class joins the F2 ledger on "
            "`idempotency_key` (C-CP-07 §7.4)."
        ),
    ),
    CapabilityFloor(
        capability_name="lease_coordination",
        required_at_class=_ALL_CLASSES,
        rationale=(
            "F3 capability-floor (iii) — lease coordination. Every engine class "
            "provides a concurrent-resume mitigation mechanism (C-CP-07 §7.4)."
        ),
    ),
    CapabilityFloor(
        capability_name="observable_lifecycle",
        required_at_class=_ALL_CLASSES,
        rationale=(
            "F3 capability-floor (iv) — observable lifecycle. Every engine "
            "class emits the eight lifecycle events per C-CP-05 §5.1 "
            "(C-CP-07 §7.4)."
        ),
    ),
)
"""The 4 F3 capability-floors, each required at every engine class (§7.4)."""
