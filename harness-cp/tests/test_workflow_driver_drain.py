"""Tests for U-CP-57 — workflow driver drain composition.

Acceptance-criterion coverage (per Implementation_Plan_Control_Plane_v2_11.md):
  #1 driver-entry drain check
      → test_drain_at_entry_returns_drained_no_workflow_start_emit
      → test_drain_at_entry_no_ledger_append
      → test_drain_at_entry_no_validation_error
  #2 per-step pre-entry drain check (Path B — no step.boundary emit)
      → test_drain_pre_step_no_step_boundary_emit_path_b
      → test_drain_pre_step_returns_drained_with_prior_step_index
      → test_drain_pre_step_at_first_step_terminal_index_none
  #3 per-step post-exit drain check (after ledger append persists)
      → test_drain_post_step_after_ledger_append_persists
      → test_drain_post_step_terminal_index_is_completed_step
  #4 no mid-step drain interruption
      → test_no_mid_step_drain_interruption_via_drained_flag
  #5 bounded-wait composition (driver does NOT own timeout)
      → test_driver_does_not_own_bounded_wait_timeout
  #6 drained_flag not auto-set by driver
      → test_drained_flag_not_set_by_driver
      → test_drained_flag_not_set_on_step_failure
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepDispatcherRegistry,
    StepKindDispatcherNotBoundError,
    execute_workflow,
)
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass

# U-RT-59 (C-RT-17 §14.7.7): drain tests use a single-kind registry per the
# routing-layer refactor. Sibling pattern of test_workflow_driver.py's
# `_SingleKindRegistry` + `_registry` helper — duplicated here to keep
# drain tests self-contained (no cross-test-file dependency).


class _SingleKindRegistry:
    def __init__(self, kind: StepKind, dispatcher: StepDispatcher) -> None:
        self._kind = kind
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind != self._kind:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _SingleKindRegistry(StepKind.INFERENCE_STEP, dispatcher))


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-driver-runtime")


def _manifest() -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id="wf-drain",
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _step(idx: int) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(f"step-{idx}"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"index": idx},
    )


class _FakeLedger:
    actor: Actor

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> Any:
        self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appends)


class _FakeEmitter:
    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emits.append(event_class)


class _FakeCtx:
    def __init__(
        self,
        *,
        ledger: _FakeLedger,
        emitter: _FakeEmitter,
        drained_flag: asyncio.Event,
    ) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = drained_flag
        # U-OD-35 — DriverContext requires tracer_provider per C-OD-25 §25.2.
        # Drain tests don't observe spans; NoOpTracerProvider keeps the surface
        # quiescent. Envelope behavior on DRAINED close is exercised in
        # test_workflow_driver_envelope.py.
        self.tracer_provider = NoOpTracerProvider()
        # U-CP-61 — optional ValidatorFramework binding; drain tests don't
        # exercise validators (validator_framework=None skips the hook).
        self.validator_framework: object | None = None
        # U-RT-87 (v2.20) — pause_resume_protocol + pause_requested_flag per
        # runtime spec v1.21 §4 + §14.14.3 DriverContext Protocol extension.
        # Drain tests don't exercise pause-trigger (protocol=None skips check).
        self.pause_resume_protocol: object | None = None
        self.pause_requested_flag = asyncio.Event()
        # tenant_id binding lift — drain tests run single-tenant.
        self.tenant_id: str | None = None


class _EchoDispatcher:
    """Echo dispatcher (no drain awareness — driver owns drain logic)."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[StepEffectiveBinding, WorkflowStep]] = []

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        # `step_context` accepted at v1.6 Path A per amended StepDispatcher
        # Protocol (C-RT-17 resolution); drain echo dispatcher does not consume.
        self.dispatched.append((binding, step))
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


class _DrainAfterStepsDispatcher:
    """Dispatcher that sets the drained_flag after N steps complete.

    Models the post-step drain site: drain becomes true mid-iteration (between
    step N and step N+1). Used to test the per-step pre-entry + post-exit
    drain checks.
    """

    def __init__(self, drained_flag: asyncio.Event, *, set_after: int) -> None:
        self.dispatched: list[tuple[StepEffectiveBinding, WorkflowStep]] = []
        self._flag = drained_flag
        self._set_after = set_after

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        # `step_context` accepted at v1.6 Path A per amended StepDispatcher
        # Protocol (C-RT-17 resolution); drain dispatcher does not consume.
        self.dispatched.append((binding, step))
        result = {"step_id": str(step.step_id)}
        if len(self.dispatched) == self._set_after:
            self._flag.set()
        return result


def _ctx_drained() -> tuple[_FakeCtx, _FakeLedger, _FakeEmitter, asyncio.Event]:
    """Build a context whose drained_flag is ALREADY set at construction."""
    ledger = _FakeLedger()
    emitter = _FakeEmitter()
    flag = asyncio.Event()
    flag.set()
    return _FakeCtx(ledger=ledger, emitter=emitter, drained_flag=flag), ledger, emitter, flag


def _ctx_clean() -> tuple[_FakeCtx, _FakeLedger, _FakeEmitter, asyncio.Event]:
    """Build a context whose drained_flag is not yet set."""
    ledger = _FakeLedger()
    emitter = _FakeEmitter()
    flag = asyncio.Event()
    return _FakeCtx(ledger=ledger, emitter=emitter, drained_flag=flag), ledger, emitter, flag


# ---------------------------------------------------------------------------
# AC #1 — Driver-entry drain check
# ---------------------------------------------------------------------------


def test_drain_at_entry_returns_drained_no_workflow_start_emit() -> None:
    """When drained_flag is set BEFORE execute_workflow() is called, the driver
    returns DRAINED without emitting workflow.start (§25.4 row "Driver entry").
    """
    ctx, _, emitter, _ = _ctx_drained()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.DRAINED
    assert result.terminal_step_index is None
    assert result.partial_state is None
    assert result.final_state is None
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


def test_drain_at_entry_no_ledger_append() -> None:
    """Entry-drain emits no lifecycle event and appends nothing to the ledger."""
    ctx, ledger, emitter, _ = _ctx_drained()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert ledger.appends == []
    assert emitter.emits == []


def test_drain_at_entry_no_validation_error() -> None:
    """Entry-drain returns DRAINED even when manifest would otherwise fail
    topology / engine-class validation. Drain check precedes validation per
    plan v2.11 U-CP-57 AC #1 ("drain check precedes topology + engine-class
    validation").
    """
    ctx, _, _, _ = _ctx_drained()
    invalid_manifest = WorkflowManifestEntry(
        workflow_id="wf-drain",
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.RECONCILER_LOOP,  # sole out-of-scope class (WAL_SEGMENT went in-scope at U-CP-94/E-impl-2)
        topology_pattern=TopologyPattern.PARALLELIZATION,  # out-of-scope at v1.4
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )
    # Would normally raise; drain at entry returns DRAINED instead.
    result = execute_workflow(
        manifest_entry=invalid_manifest,
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.DRAINED


# ---------------------------------------------------------------------------
# AC #2 — Per-step pre-entry drain check (Path B — no step.boundary emit)
# ---------------------------------------------------------------------------


def test_drain_pre_step_no_step_boundary_emit_path_b() -> None:
    """When drain is detected at the per-step pre-entry site, no step.boundary
    is emitted for the would-be next step (Path B operator-ratified — §5.2
    step.kind 5-value enum preserved verbatim).
    """
    ctx, _, emitter, _ = _ctx_clean()
    # Drain after step 0 completes (post-step). Then before step 1 enters,
    # the per-step pre-entry check fires and returns DRAINED without emitting
    # a step.boundary for step 1.
    dispatcher = _DrainAfterStepsDispatcher(ctx.drained_flag, set_after=1)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # Exactly 1 step.boundary emitted (for completed step 0). Path B: no
    # step.boundary at the pre-entry drain site for step 1.
    step_boundaries = [e for e in emitter.emits if e is WorkflowEventClass.STEP_BOUNDARY]
    assert len(step_boundaries) == 1


def test_drain_pre_step_returns_drained_with_prior_step_index() -> None:
    """Pre-entry drain at step N+1 returns DRAINED with terminal_step_index=N
    (the last fully-completed step).
    """
    ctx, _, _, _ = _ctx_clean()
    dispatcher = _DrainAfterStepsDispatcher(ctx.drained_flag, set_after=2)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2), _step(3)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert result.status is RunStatus.DRAINED
    # Steps 0 and 1 completed (set_after=2 → flag set after step 1 dispatches).
    # Post-step check after step 1 finds flag set → returns with index=1.
    # Note: the post-step check fires first when the flag is set during step
    # dispatch; the pre-entry check would fire at the NEXT iteration.
    assert result.terminal_step_index == 1
    assert result.partial_state is not None
    assert "step-0" in result.partial_state
    assert "step-1" in result.partial_state
    assert "step-2" not in result.partial_state


def test_drain_pre_step_at_first_step_terminal_index_none() -> None:
    """When drain is detected before step 0 enters (i.e. flag set between
    workflow.start emission and step 0 pre-entry), terminal_step_index is None
    since no step has yet completed.

    Test mechanism: sets the flag inside the emitter's `emit(WORKFLOW_START)`
    callback to fire the pre-entry check at step 0. This relies on the driver's
    emission sequencing (workflow.start emit → step iteration loop). A future
    driver refactor that batches lifecycle emits differently would invalidate
    the mechanism; rewrite this test against the new sequencing.
    """
    _, _, _, flag = _ctx_clean()

    # Use a dispatcher that sets the flag BEFORE any step is dispatched —
    # actually we can do this by setting the flag in a fake emitter callback
    # right after workflow.start emit. Simpler: pre-set flag after manifest
    # validation but before step iteration by wrapping ctx.

    class _SetFlagOnEmit:
        def __init__(self, flag: asyncio.Event) -> None:
            self.emits: list[WorkflowEventClass] = []
            self._flag = flag

        def emit(self, event_class: WorkflowEventClass) -> None:
            self.emits.append(event_class)
            if event_class is WorkflowEventClass.WORKFLOW_START:
                self._flag.set()

    emitter = _SetFlagOnEmit(flag)
    ctx2 = _FakeCtx(ledger=_FakeLedger(), emitter=cast(_FakeEmitter, emitter), drained_flag=flag)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx2),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.DRAINED
    # Flag set right after workflow.start; pre-entry check before step 0 → fire.
    assert result.terminal_step_index is None
    assert result.partial_state == {}


# ---------------------------------------------------------------------------
# AC #3 — Per-step post-exit drain check
# ---------------------------------------------------------------------------


def test_drain_post_step_after_ledger_append_persists() -> None:
    """When drain is detected at the per-step post-exit site, the
    just-completed step's ledger entry HAS persisted (per U-IS-11 append
    discipline) and step.boundary HAS been emitted.
    """
    ctx, ledger, emitter, _ = _ctx_clean()
    dispatcher = _DrainAfterStepsDispatcher(ctx.drained_flag, set_after=1)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # Step 0 ledger entry persisted.
    assert len(ledger.appends) == 1
    # Step 0 step.boundary emitted.
    assert WorkflowEventClass.STEP_BOUNDARY in emitter.emits


def test_drain_post_step_terminal_index_is_completed_step() -> None:
    """Post-step drain at step N returns DRAINED with terminal_step_index=N
    (this step counted; its ledger entry persisted).
    """
    ctx, _, _, _ = _ctx_clean()
    dispatcher = _DrainAfterStepsDispatcher(ctx.drained_flag, set_after=1)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert result.status is RunStatus.DRAINED
    assert result.terminal_step_index == 0  # step 0 counted


# ---------------------------------------------------------------------------
# AC #4 — No mid-step drain interruption
# ---------------------------------------------------------------------------


def test_no_mid_step_drain_interruption_via_drained_flag() -> None:
    """Step bodies run to completion even when drained_flag is set mid-dispatch.
    The driver does not interrupt step bodies; it checks the flag only at the
    3 site boundaries (per `Spec_Harness_Runtime_v1.md` §11 v1.2 settlement
    "Completes the current in-flight step (no mid-step interruption)").
    """
    ctx, _, _, _ = _ctx_clean()

    # Dispatcher that sets the flag DURING step 0's dispatch, then returns
    # normally. The driver should still let step 0 complete normally.
    class _SelfSettingDispatcher:
        def __init__(self, flag: asyncio.Event) -> None:
            self.dispatched: list[tuple[StepEffectiveBinding, WorkflowStep]] = []
            self._flag = flag

        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            # `step_context` accepted at v1.6 Path A; mock does not consume.
            self._flag.set()  # mid-dispatch drain
            # Step body continues to completion (returns its output normally).
            self.dispatched.append((binding, step))
            return {"step_id": str(step.step_id), "completed_after_drain": True}

    dispatcher = _SelfSettingDispatcher(ctx.drained_flag)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # Step 0 dispatched (not interrupted); step 1 NOT dispatched (drain caught
    # at post-step or pre-step boundary).
    assert len(dispatcher.dispatched) == 1
    # Result is DRAINED with step 0 counted.
    assert result.status is RunStatus.DRAINED
    # Step 0's output was accumulated (it completed after setting the flag).
    assert result.partial_state is not None
    assert "step-0" in result.partial_state


# ---------------------------------------------------------------------------
# AC #5 — Bounded-wait composition (driver does NOT own timeout)
# ---------------------------------------------------------------------------


def test_driver_does_not_own_bounded_wait_timeout() -> None:
    """The driver does not implement a timeout primitive at the driver layer.
    Per spec §25.4 row "Bounded wait" + §25.7 mode 5: timeout ownership at
    `shutdown(ctx, timeout=...)` at `Spec_Harness_Runtime_v1.md` C-RT-10.
    Driver contract is composition-only at the RT-FAIL-DRAIN-TIMEOUT fail
    class.

    Verification: `execute_workflow()` signature carries no `timeout`
    parameter; module exposes no public timeout symbol.
    """
    import inspect

    from harness_cp import workflow_driver

    sig = inspect.signature(execute_workflow)
    assert "timeout" not in sig.parameters
    # No public timeout PRIMITIVE in the module. `FanoutTimeoutDisposition` (a re-exported
    # enum imported from `workflow_manifest_entry` — the B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY
    # crash-resume disposition policy domain, CP spec v1.63 §1) is NOT a driver-owned
    # bounded-wait timeout primitive, so it is exempt from this guard (it carries "timeout"
    # only as part of the deadline-cut disposition concept).
    _timeout_symbol_allowlist = {"FanoutTimeoutDisposition"}
    public_names = [n for n in dir(workflow_driver) if not n.startswith("_")]
    for name in public_names:
        if name in _timeout_symbol_allowlist:
            continue
        assert "timeout" not in name.lower()


# ---------------------------------------------------------------------------
# AC #6 — drained_flag not auto-set by driver
# ---------------------------------------------------------------------------


def test_drained_flag_not_set_by_driver() -> None:
    """Driver never calls `ctx.drained_flag.set()` itself; flag ownership is
    at U-RT-44 signal handler.
    """
    ctx, _, _, flag = _ctx_clean()
    assert not flag.is_set()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    # Flag still not set after happy-path completion.
    assert not flag.is_set()


def test_drained_flag_not_set_on_step_failure() -> None:
    """Failure ≠ drain — driver does not set drained_flag on step failure."""
    ctx, _, _, flag = _ctx_clean()

    class _FailingDispatcher:
        def __init__(self) -> None:
            self.dispatched: list[tuple[StepEffectiveBinding, WorkflowStep]] = []

        def dispatch(self, binding: StepEffectiveBinding, step: WorkflowStep) -> dict[str, Any]:
            raise RuntimeError("step failure")

    assert not flag.is_set()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _FailingDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    # drained_flag NOT auto-set on failure.
    assert not flag.is_set()
