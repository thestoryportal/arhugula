"""Lifecycle event emitter binding — stage 5 LOOP_INIT (U-RT-41, PARTIAL-LAND).

Per `Spec_Harness_Runtime_v1.md` v1.1 §C-RT-04 `HarnessContext.lifecycle_emitter`
field (`LifecycleEventEmitter` (runtime-defined) — stage 5; emits
`workflow_event_class` events). Concretizes the
`LifecycleEventEmitter` Protocol (narrowed at `harness_runtime.types`
at this landing) over the canonical `WorkflowEventClass` enum from
`harness_core.workflow_event_class` (C-CP-05 §5.1 verbatim — 8 values).

**PARTIAL-LAND posture (8 of 9 spec-cited events).** C-RT-11 §11 step 2
commits the runtime to emit `WorkflowEventClass.DRAINED` on drain
detection. The canonical `WorkflowEventClass` enum is closed at
cardinality 8 (no `DRAINED` value); spec §16 open question #9
explicitly authorizes split: "If landed
`harness_core.workflow_event_class` enum doesn't carry `DRAINED`,
U-RT-41 lands an aligned name. Surface as Class 1 fork at U-RT-41
landing if alignment fails." No semantic-aligned name is available
among the 8 (all are workflow-lifecycle / breaker / lease /
resumption events; none signals drain-complete). Class 1 fork filed
at `.harness/class_1_tension_u_rt_41_drained_event_class_alignment.md`.

**Drain observability preserved.** C-RT-11 §11 commits three drain
surfaces; the DRAINED emit (struck here) is the only unmaterialized
one. Drain remains visible to operators via:
- `ctx.drained_flag` (asyncio.Event) — primary drain signal
- `RunResult.status == 'drained'` — terminal-return signal (U-RT-42)

**Test-introspectable emit ring.** Each `emit(event_class)` call
appends a record to an in-memory ring (the emitter's
`emitted_events` tuple). This gives the L9 verification suite a
deterministic surface for event-ordering assertions without requiring
OTel-pipeline scaffolding. The ring is bounded by the emitter's
lifetime (no eviction policy at HEAD; consumers that need long-run
emission discipline wrap a span exporter externally).

**Module convention.** One module per unit.
`materialize_lifecycle_emitter_stage` composer returns a frozen
`LifecycleEmitterStage` dataclass with `slots=True`. Typed
`LifecycleEmitterBindError` for bootstrap-time failures. Mirrors the
L5..L7 + U-RT-39 + U-RT-40 stage shape established at U-RT-21..40.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness_core.workflow_event_class import WorkflowEventClass

from harness_runtime.types import BootstrapStage, RuntimeConfig


class LifecycleEmitterBindError(Exception):
    """Raised when lifecycle-emitter stage materialization fails."""


@dataclass(slots=True)
class RuntimeLifecycleEventEmitter:
    """Runtime lifecycle-event emitter (C-RT-04 `lifecycle_emitter` binding).

    Concretizes `harness_runtime.types.LifecycleEventEmitter` for the 8
    canonical `WorkflowEventClass` values. Emit records are appended to
    `_emitted_events` (test-introspectable via `emitted_events`); the
    emitter is mutable-by-design (event recording) but its API surface
    is fixed at construction.

    NOT frozen — the emit ring is mutable. The stage wrapper is frozen.
    """

    _emitted_events: list[WorkflowEventClass] = field(
        default_factory=lambda: [],
    )
    _emitted_bootstrap_stages: list[BootstrapStage] = field(
        default_factory=lambda: [],
    )

    def emit(self, event_class: WorkflowEventClass) -> None:
        """Emit a lifecycle event of the given canonical class.

        Appends to the emit ring. No external side-effects at HEAD
        (OTel-span emission lands at L9 wiring; this surface is the
        runtime-internal call-site).
        """
        self._emitted_events.append(event_class)

    def emit_bootstrap_stage_complete(self, stage: BootstrapStage) -> None:
        """Emit one `BootstrapStageCompleteEvent`-grade record per C-RT-01.

        Per `Spec_Harness_Runtime_v1.md` v1.1 §1 invariant + U-RT-43 AC #3
        (each of the 9 bootstrap substages emits exactly one lifecycle
        event). `WorkflowEventClass` is closed at cardinality 8 and
        addresses workflow lifecycle, not bootstrap; this surface is
        bounded to the bootstrap orchestrator (U-RT-43) and tests.
        Records append to a separate ring for deterministic introspection.
        """
        self._emitted_bootstrap_stages.append(stage)

    @property
    def emitted_events(self) -> tuple[WorkflowEventClass, ...]:
        """Snapshot of every emit since construction (deterministic order)."""
        return tuple(self._emitted_events)

    @property
    def emitted_bootstrap_stages(self) -> tuple[BootstrapStage, ...]:
        """Snapshot of every bootstrap-stage emit since construction (deterministic order)."""
        return tuple(self._emitted_bootstrap_stages)

    def clear(self) -> None:
        """Reset the emit ring (test convenience; no production caller)."""
        self._emitted_events.clear()
        self._emitted_bootstrap_stages.clear()


@dataclass(frozen=True, slots=True)
class LifecycleEmitterStage:
    """Frozen result of stage 5 LOOP_INIT lifecycle-emitter binding.

    The bootstrap orchestrator (U-RT-43) binds `emitter` to
    `HarnessContext.lifecycle_emitter` (C-RT-04 stage 5 invariant).
    Mirrors the L5..L7 + U-RT-39 + U-RT-40 stage shape.
    """

    emitter: RuntimeLifecycleEventEmitter


def materialize_lifecycle_emitter_stage(
    config: RuntimeConfig,
) -> LifecycleEmitterStage:
    """Build the stage 5 LOOP_INIT lifecycle emitter stage.

    The emitter starts with an empty event ring. `config` is read for
    API consistency with the L5..L7 + U-RT-39 + U-RT-40 composers; no
    field is consumed at HEAD.
    """
    _ = config
    return LifecycleEmitterStage(emitter=RuntimeLifecycleEventEmitter())
