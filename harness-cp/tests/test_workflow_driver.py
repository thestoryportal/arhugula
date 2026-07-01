"""Tests for U-CP-56 — workflow execution driver core.

Acceptance-criterion coverage (per Implementation_Plan_Control_Plane_v2_11.md):
  #1 type surface materialized
      → test_run_result_seven_fields
      → test_run_status_four_members
      → test_step_kind_five_members
      → test_workflow_step_three_fields
      → test_typed_errors_subclass_workflow_driver_error
  #2 topology + engine-class validation at entry
      → test_topology_pattern_not_yet_materialized_raised_at_non_single_threaded_linear
      → test_engine_class_not_yet_materialized_raised_at_out_of_scope_engine_class
      → test_validation_failure_emits_no_workflow_start
  #3 workflow.start emission
      → test_workflow_start_emitted_after_validation
  #4 step iteration loop
      → test_step_iteration_declaration_order
      → test_per_step_step_boundary_emitted
      → test_state_ledger_append_per_step
      → test_step_idempotency_key_deterministic
  #5 lifecycle event filter (single-threaded-linear)
      → test_lifecycle_events_in_happy_path
      → test_no_terminal_lifecycle_event_at_success
  #6 replay-resumption read at re-entry
      → test_workflow_resumption_emitted_on_save_point_checkpoint_reentry
      → test_no_resumption_emission_under_pure_pattern_no_engine
  #7 terminal SUCCESS return
      → test_terminal_success_return_shape
  #8 failure-mode taxonomy
      → test_step_failure_returns_failed_status
      → test_ledger_append_failure_returns_failed_status
  #9 determinism
      → test_driver_iteration_deterministic_given_inputs
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_placement import HITLPlacement, HITLPlacementKind
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    DriverContext,
    StepDispatcher,
    StepDispatcherRegistry,
    StepKindDispatcherNotBoundError,
    _append_step_ledger_entry,
    execute_workflow,
)
from harness_cp.workflow_driver_errors import (
    EngineClassNotYetMaterializedError,
    TopologyPatternNotYetMaterializedError,
    WorkflowDriverError,
)
from harness_cp.workflow_driver_types import (
    RunResult,
    RunStatus,
    StepExecutionContext,
    StepKind,
    WorkflowStep,
    fold_step_hitl_placements,
)
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier

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


def _manifest(
    *,
    engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
    topology_pattern: TopologyPattern = TopologyPattern.SINGLE_THREADED_LINEAR,
    workflow_id: str = "wf-1",
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=engine_class,
        topology_pattern=topology_pattern,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _step(idx: int, kind: StepKind = StepKind.INFERENCE_STEP) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(f"step-{idx}"),
        step_kind=kind,
        step_payload={"index": idx},
    )


class _FakeLedger:
    """In-memory `LedgerWriterLike` substrate for tests."""

    actor: Actor

    def __init__(self, *, fail: bool = False, prior_entries: int = 0) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []
        self._fail = fail
        self._prior = prior_entries

    def append(self, payload: Any, write_key: Any) -> Any:
        if self._fail:
            raise RuntimeError("simulated ledger append failure")
        self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return self._prior == 0 and len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return self._prior + len(self.appends)


class _FakeEmitter:
    """In-memory `LifecycleEventEmitterLike` substrate for tests."""

    def __init__(self) -> None:
        self.emits: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emits.append(event_class)


class _FakeLedgerReader:
    """In-memory `LedgerReaderLike` substrate for tests (v2.12).

    Holds a mapping `idempotency_key → entries_count` simulating ledger
    contents. `read_by_idempotency_key` returns a stub `ReadResult`-shaped
    object whose `entries` is a tuple of dummies sized to the count.
    """

    def __init__(self, materialized_keys: dict[str, int] | None = None) -> None:
        self._keys = materialized_keys or {}

    def read_by_idempotency_key(self, idempotency_key: Any, bounded_window: Any) -> Any:
        _ = bounded_window
        # The driver passes Identifier(hex_string); compare on str form.
        count = self._keys.get(str(idempotency_key), 0)

        class _Result:
            def __init__(self, n: int) -> None:
                self.entries = tuple(object() for _ in range(n))
                self.truncated = False
                self.next_position = None

        return _Result(count)


class _FakeCtx:
    """Combined fake `DriverContext`.

    `drained_flag` defaults to a fresh never-set `asyncio.Event` so U-CP-56
    happy-path tests don't trigger drain. U-CP-57 drain tests explicitly set
    the flag at the relevant boundary site.
    """

    def __init__(
        self,
        *,
        ledger: _FakeLedger,
        emitter: _FakeEmitter,
        drained_flag: asyncio.Event | None = None,
        ledger_reader: _FakeLedgerReader | None = None,
        tracer_provider: object | None = None,
        validator_framework: object | None = None,
        pause_resume_protocol: object | None = None,
        pause_requested_flag: asyncio.Event | None = None,
        tenant_id: str | None = None,
    ) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = emitter
        self.drained_flag = drained_flag if drained_flag is not None else asyncio.Event()
        # U-RT-87 (v2.20) — pause_resume_protocol + pause_requested_flag fields
        # per runtime spec v1.21 §4 + §14.14.3 DriverContext Protocol extension.
        self.pause_resume_protocol = pause_resume_protocol
        self.pause_requested_flag = (
            pause_requested_flag if pause_requested_flag is not None else asyncio.Event()
        )
        self.ledger_reader = ledger_reader if ledger_reader is not None else _FakeLedgerReader()
        # U-OD-35 — DriverContext requires tracer_provider per C-OD-25 §25.2.
        # Default to NoOpTracerProvider so happy-path tests don't assert span
        # observables; envelope-specific tests live in test_workflow_driver_envelope.py.
        self.tracer_provider = (
            tracer_provider if tracer_provider is not None else NoOpTracerProvider()
        )
        # U-CP-61 — optional ValidatorFramework binding; default None (skip hook).
        self.validator_framework = validator_framework
        # tenant_id binding lift — DriverContext.tenant_id surfaced from
        # HarnessContext.tenant_id (which reads RuntimeConfig.tenant_id).
        # Default None preserves single-tenant behavior for happy-path tests.
        self.tenant_id = tenant_id


class _SingleKindRegistry:
    """Minimal test `StepDispatcherRegistry` impl — binds one (kind, dispatcher).

    Concrete impl for the v1.6 routing-layer refactor per C-RT-17 §14.7.7.
    The production `StepKindDispatcherRegistry` lives in `harness_runtime`;
    CP tests use this inline impl to avoid the CP→runtime dependency
    direction. Lookup of an unbound kind raises
    `StepKindDispatcherNotBoundError` (same shape as the production impl).
    """

    def __init__(self, kind: StepKind, dispatcher: StepDispatcher) -> None:
        self._kind = kind
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind != self._kind:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    """Compose a single-kind INFERENCE_STEP registry for legacy tests.

    Pre-U-RT-59 tests passed a single dispatcher; post-refactor the driver
    requires a `StepDispatcherRegistry`. This helper wraps a dispatcher
    in a minimal one-entry registry bound to INFERENCE_STEP (the default
    `_step(...)` fixture's step_kind).
    """
    return cast(StepDispatcherRegistry, _SingleKindRegistry(StepKind.INFERENCE_STEP, dispatcher))


class _EchoDispatcher:
    """Step dispatcher that echoes the step payload back."""

    def __init__(self, *, fail_at_step: int | None = None) -> None:
        self.dispatched: list[tuple[StepEffectiveBinding, WorkflowStep]] = []
        self._fail_at = fail_at_step

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        # `step_context` accepted at v1.6 Path A per amended StepDispatcher
        # Protocol (C-RT-17 resolution); echo dispatcher does not consume.
        if self._fail_at is not None and len(self.dispatched) == self._fail_at:
            raise RuntimeError(f"simulated step failure at index {self._fail_at}")
        self.dispatched.append((binding, step))
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


def _ctx(*, prior_entries: int = 0) -> tuple[_FakeCtx, _FakeLedger, _FakeEmitter]:
    ledger = _FakeLedger(prior_entries=prior_entries)
    emitter = _FakeEmitter()
    return _FakeCtx(ledger=ledger, emitter=emitter), ledger, emitter


# ---------------------------------------------------------------------------
# AC #1 — Type surface materialized
# ---------------------------------------------------------------------------


def test_run_result_eight_fields() -> None:
    """8-field RunResult per C-CP-25 §25.2 + v1.21 §14.14.5 invariant 4
    additive `pause_snapshot` field (U-RT-89)."""
    result = RunResult(
        workflow_id="wf",
        run_id="run-1",
        status=RunStatus.SUCCESS,
    )
    field_names = set(type(result).model_fields.keys())
    assert field_names == {
        "workflow_id",
        "run_id",
        "status",
        "terminal_step_index",
        "partial_state",
        "final_state",
        "fail_class",
        "pause_snapshot",
    }


def test_run_status_five_members() -> None:
    """5-member RunStatus per C-CP-25 §25.2 + v1.21 §14.14.5 invariant 4
    additive `PAUSED` value (U-RT-89)."""
    members = {m.name for m in RunStatus}
    assert members == {"SUCCESS", "DRAINED", "FAILED", "PARTIAL", "PAUSED"}


def test_step_kind_seven_members() -> None:
    """7-member StepKind per CP spec §5.2 + v1.39 additive `managed-agents`
    (R-FS-1 arc M; operator-ratified 2026-06-17, Option B) + v1.54 additive
    `post-join-synthesis` (R-FS-1 arc B-POSTJOIN-LLM-SYNTHESIS; operator-ratified
    2026-06-23, arc-a A)."""
    members = {m.value for m in StepKind}
    assert members == {
        "declarative-step",
        "inference-step",
        "tool-step",
        "HITL-step",
        "sub-agent-dispatch",
        "managed-agents",
        "post-join-synthesis",
    }


def test_workflow_step_three_fields() -> None:
    step = _step(0)
    field_names = set(type(step).model_fields.keys())
    assert field_names == {"step_id", "step_kind", "step_payload"}


def test_typed_errors_subclass_workflow_driver_error() -> None:
    assert issubclass(TopologyPatternNotYetMaterializedError, WorkflowDriverError)
    assert issubclass(EngineClassNotYetMaterializedError, WorkflowDriverError)


# ---------------------------------------------------------------------------
# AC #2 — Topology + engine-class validation at entry
# ---------------------------------------------------------------------------


def test_decentralized_handoff_materialized_runs_through_execute_workflow() -> None:
    # U-CP-90 landed DECENTRALIZED_HANDOFF (single-owner sequential handoff) — the
    # LAST non-linear pattern. ALL SIX TopologyPattern values are now materialized,
    # so the pattern runs through execute_workflow (NO NOT_YET_MATERIALIZED raise),
    # emits WORKFLOW_START, and persists its stage entry. (The retained
    # NOT_YET_MATERIALIZED sentinel mechanism is exercised via monkeypatch at
    # test_workflow_driver_branch_substrate.py::test_not_yet_materialized_sentinel_still_raises.)
    manifest = _manifest(topology_pattern=TopologyPattern.DECENTRALIZED_HANDOFF)
    ctx, ledger, emitter = _ctx()
    result = execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.SUCCESS
    assert WorkflowEventClass.WORKFLOW_START in emitter.emits
    assert ledger.appends  # the stage persisted (no silent no-op)


def test_engine_class_not_yet_materialized_raised_at_out_of_scope_engine_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # U-CP-96 (E-impl-3a) materialized RECONCILER_LOOP — the LAST engine class —
    # so at HEAD _IN_SCOPE_ENGINE_CLASSES == the full closed EngineClass set and NO
    # valid engine class triggers the gate; it is preserved-but-unreachable for
    # forward-safety (exactly as the topology gate after all 6 patterns materialized
    # at U-CP-90). This test exercises the PRESERVED defensive gate by patching
    # _IN_SCOPE to a subset that excludes RECONCILER_LOOP (a hypothetical
    # not-yet-materialized class) and asserts it still raises before any
    # emit/append. The closing milestone (all 5 materialized) is asserted in
    # test_all_engine_classes_materialized_at_head.
    monkeypatch.setattr(
        "harness_cp.workflow_driver._IN_SCOPE_ENGINE_CLASSES",
        frozenset(EngineClass) - {EngineClass.RECONCILER_LOOP},
    )
    manifest = _manifest(engine_class=EngineClass.RECONCILER_LOOP)
    ctx, ledger, emitter = _ctx()
    with pytest.raises(EngineClassNotYetMaterializedError):
        execute_workflow(
            manifest_entry=manifest,
            steps=[_step(0)],
            run_id="run-1",
            ctx=cast(DriverContext, ctx),
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        )
    assert emitter.emits == []
    assert ledger.appends == []


def test_all_engine_classes_materialized_at_head() -> None:
    # U-CP-96 (E-impl-3a) — RECONCILER_LOOP is the LAST engine class; with it in
    # _IN_SCOPE, every member of the closed 5-class EngineClass enum is materialized
    # and the EngineClassNotYetMaterializedError gate is preserved-but-unreachable
    # for any valid manifest (the E sub-program's gate-level closing).
    from harness_cp.workflow_driver import _IN_SCOPE_ENGINE_CLASSES

    assert _IN_SCOPE_ENGINE_CLASSES == frozenset(EngineClass)


def test_validation_failure_emits_no_workflow_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pre-dispatch materialization-gate raise must fire BEFORE WORKFLOW_START, so
    # no lifecycle event is emitted. Both materialization gates are now closed for
    # valid input (all 6 topology patterns at U-CP-90; all 5 engine classes at
    # U-CP-96/E-impl-3a — RECONCILER_LOOP was the last). The behavioral invariant
    # (gate raise ⟹ no WORKFLOW_START) is exercised against the PRESERVED engine
    # gate by patching _IN_SCOPE to exclude RECONCILER_LOOP (a hypothetical
    # not-yet-materialized class). Was DECENTRALIZED_HANDOFF via the topology gate,
    # then RECONCILER_LOOP via the engine gate; now both are preserved-unreachable.
    monkeypatch.setattr(
        "harness_cp.workflow_driver._IN_SCOPE_ENGINE_CLASSES",
        frozenset(EngineClass) - {EngineClass.RECONCILER_LOOP},
    )
    manifest = _manifest(engine_class=EngineClass.RECONCILER_LOOP)
    ctx, _, emitter = _ctx()
    with pytest.raises(EngineClassNotYetMaterializedError):
        execute_workflow(
            manifest_entry=manifest,
            steps=[_step(0)],
            run_id="run-1",
            ctx=cast(DriverContext, ctx),
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        )
    assert WorkflowEventClass.WORKFLOW_START not in emitter.emits


# ---------------------------------------------------------------------------
# AC #3 — workflow.start emission post-validation
# ---------------------------------------------------------------------------


def test_workflow_start_emitted_after_validation() -> None:
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert emitter.emits[0] is WorkflowEventClass.WORKFLOW_START


# ---------------------------------------------------------------------------
# AC #4 — Step iteration loop
# ---------------------------------------------------------------------------


def test_step_iteration_declaration_order() -> None:
    steps = [_step(i) for i in range(3)]
    ctx, _, _ = _ctx()
    dispatcher = _EchoDispatcher()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=steps,
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert result.status is RunStatus.SUCCESS
    dispatched_step_ids = [str(s.step_id) for _, s in dispatcher.dispatched]
    assert dispatched_step_ids == ["step-0", "step-1", "step-2"]


# ---------------------------------------------------------------------------
# R-FS-1 arc M (C-RT-28 §14.20) — MANAGED_AGENTS step routing (CP spec v1.39)
# ---------------------------------------------------------------------------


class _ManagedAgentsEchoDispatcher:
    """Stand-in managed-agents dispatcher returning a session-outcome mapping.

    Exercises the CP driver's registry dispatch of the NEW
    `StepKind.MANAGED_AGENTS` (not the runtime `ManagedAgentsStepDispatcher`,
    which harness-cp cannot import). Mirrors that dispatcher's return shape.
    """

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        _ = (binding, step_context)
        return {
            "session_id": "session_test",
            "agent_id": str(step.step_payload.get("agent_id", "")),
            "status": "idle",
            "runtime_ms": 1250,
            "billable_seconds": 1.25,
        }


class ManagedAgentsSessionError(Exception):
    """Test-local class named to match the driver's `type(exc).__name__` check.

    The runtime `ManagedAgentsSessionError` lives in harness-runtime (which
    harness-cp cannot import per the workspace dependency graph); the driver
    name-matches by class name, so a same-named local class exercises the
    `RT-FAIL-MANAGED-AGENTS-SESSION` mapping (the `StepDispatchTimeoutError`
    name-match precedent).
    """


class _RaisingManagedAgentsDispatcher:
    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        _ = (binding, step, step_context)
        raise ManagedAgentsSessionError("simulated managed-agents session failure")


def _managed_agents_step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.MANAGED_AGENTS,
        step_payload={"agent_id": "agent_test", "environment_id": "env_test"},
    )


def test_managed_agents_step_routes_through_registry() -> None:
    """The NEW StepKind.MANAGED_AGENTS routes through the driver registry to its
    bound dispatcher; the outcome accumulates into final_state (CP spec v1.39 +
    runtime C-RT-28 §14.20)."""
    ctx, _, _ = _ctx()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_managed_agents_step()],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _SingleKindRegistry(
                StepKind.MANAGED_AGENTS,
                cast(StepDispatcher, _ManagedAgentsEchoDispatcher()),
            ),
        ),
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert result.final_state["step-0"]["session_id"] == "session_test"
    assert result.final_state["step-0"]["status"] == "idle"


def test_managed_agents_session_error_maps_to_fail_class() -> None:
    """A ManagedAgentsSessionError from the managed-agents dispatcher maps to
    `step-failure: RT-FAIL-MANAGED-AGENTS-SESSION` (driver name-match per the
    StepDispatchTimeoutError precedent; runtime C-RT-28 §14.20.4)."""
    ctx, _, _ = _ctx()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_managed_agents_step()],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _SingleKindRegistry(
                StepKind.MANAGED_AGENTS,
                cast(StepDispatcher, _RaisingManagedAgentsDispatcher()),
            ),
        ),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "RT-FAIL-MANAGED-AGENTS-SESSION" in result.fail_class


def test_per_step_step_boundary_emitted() -> None:
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    step_boundaries = [e for e in emitter.emits if e is WorkflowEventClass.STEP_BOUNDARY]
    assert len(step_boundaries) == 3


def test_state_ledger_append_per_step() -> None:
    ctx, ledger, _ = _ctx()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert len(ledger.appends) == 3


def test_step_idempotency_key_deterministic() -> None:
    # Two runs with identical inputs produce identical per-step idempotency keys.
    keys_run_a: list[str] = []
    keys_run_b: list[str] = []
    for accumulator in (keys_run_a, keys_run_b):
        ctx, ledger, _ = _ctx()
        execute_workflow(
            manifest_entry=_manifest(),
            steps=[_step(0), _step(1)],
            run_id="run-deterministic",
            ctx=cast(DriverContext, ctx),
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        )
        for payload, _ in ledger.appends:
            accumulator.append(str(payload.idempotency_key))
    assert keys_run_a == keys_run_b
    assert len(set(keys_run_a)) == 2  # each step has a distinct key


# ---------------------------------------------------------------------------
# AC #5 — Lifecycle event filter (single-threaded-linear)
# ---------------------------------------------------------------------------


def test_lifecycle_events_in_happy_path() -> None:
    """Happy path emits exactly: workflow.start + N step.boundary."""
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    expected = [
        WorkflowEventClass.WORKFLOW_START,
        WorkflowEventClass.STEP_BOUNDARY,
        WorkflowEventClass.STEP_BOUNDARY,
    ]
    assert emitter.emits == expected


def test_no_terminal_lifecycle_event_at_success() -> None:
    """No new event class at terminal exit (per §25.3.4 + §25.5 strict
    composition against §5.1 closed-at-8 taxonomy).
    """
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    # Last emit is the per-step step.boundary; no terminal sentinel.
    assert emitter.emits[-1] is WorkflowEventClass.STEP_BOUNDARY


# ---------------------------------------------------------------------------
# AC #6 — Replay-resumption read at re-entry
# ---------------------------------------------------------------------------


def _expected_step_key(run_id: str, workflow_id: str, entry_version: int, step_index: int) -> str:
    """Compute the expected step idempotency_key per §25.6, for test setup."""
    import hashlib

    run_h = hashlib.sha256()
    run_h.update(run_id.encode("utf-8"))
    run_h.update(b"\x00")
    run_h.update(workflow_id.encode("utf-8"))
    run_h.update(b"\x00")
    run_h.update(str(entry_version).encode("utf-8"))
    run_key = run_h.hexdigest()

    step_h = hashlib.sha256()
    step_h.update(run_key.encode("utf-8"))
    step_h.update(b"\x00")
    step_h.update(str(step_index).encode("utf-8"))
    return step_h.hexdigest()


def test_workflow_resumption_emitted_on_save_point_checkpoint_reentry() -> None:
    """v2.12 (un-strike of AC #6) — RESUMPTION emit is *selective* per run.

    Materializes prior step entries matching `run-1`'s expected keys; driver
    detects them, advances resume_at over the contiguous prefix, and emits
    RESUMPTION.
    """
    manifest = _manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT)
    # Materialize ledger entries for steps 0 and 1 of this run.
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
        _expected_step_key("run-1", "wf-1", 1, 1): 1,
    }
    ledger = _FakeLedger(prior_entries=2)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # RESUMPTION emitted before WORKFLOW_START.
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    resumption_idx = emitter.emits.index(WorkflowEventClass.RESUMPTION)
    start_idx = emitter.emits.index(WorkflowEventClass.WORKFLOW_START)
    assert resumption_idx < start_idx
    # Only step 2 dispatched (steps 0 + 1 already in ledger).
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_resumption_not_emitted_for_unrelated_prior_run() -> None:
    """v2.12 — prior ledger entries from a different run produce no RESUMPTION.

    Even with non-genesis ledger, if the expected step keys for THIS run
    return zero matches, the driver treats this as a genesis run for the
    purpose of resumption.
    """
    manifest = _manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT)
    # Materialize ledger entries for an unrelated run (run-OTHER).
    materialized = {
        _expected_step_key("run-OTHER", "wf-1", 1, 0): 1,
        _expected_step_key("run-OTHER", "wf-1", 1, 1): 1,
    }
    ledger = _FakeLedger(prior_entries=2)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert WorkflowEventClass.RESUMPTION not in emitter.emits
    # All steps dispatched; resume_at == 0.
    assert len(dispatcher.dispatched) == 2


def test_resumption_skips_already_replayed_steps() -> None:
    """v2.12 — driver resumes at first unmaterialized step; prior dispatched skip."""
    manifest = _manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT)
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
        _expected_step_key("run-1", "wf-1", 1, 1): 1,
        _expected_step_key("run-1", "wf-1", 1, 2): 1,
    }
    ledger = _FakeLedger(prior_entries=3)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1), _step(2), _step(3), _step(4)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # Only steps 3 + 4 dispatched.
    assert len(dispatcher.dispatched) == 2
    dispatched_ids = {str(d[1].step_id) for d in dispatcher.dispatched}
    assert dispatched_ids == {"step-3", "step-4"}


def test_resume_at_advances_over_contiguous_prefix_only() -> None:
    """v2.12 — gap behavior: contiguous prefix only, gap-fill out of scope."""
    manifest = _manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT)
    # Materialize step 0 + step 2, gap at step 1.
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
        # step 1 intentionally missing
        _expected_step_key("run-1", "wf-1", 1, 2): 1,
    }
    ledger = _FakeLedger(prior_entries=2)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # resume_at advances to 1 (only step 0 is contiguous-prefix-materialized);
    # step 1 + step 2 dispatch.
    assert len(dispatcher.dispatched) == 2
    dispatched_ids = [str(d[1].step_id) for d in dispatcher.dispatched]
    assert dispatched_ids == ["step-1", "step-2"]


class _KeyRecordingDispatcher:
    """Records the per-step idempotency key each dispatch receives (B-EFFECT-FENCE).

    OBSERVES `step_context.parent_idempotency_key` only — it does NOT fake the
    fence suppression (the fence is proven at the real tool sink in
    `harness-runtime/tests/test_effect_fence.py`). This test proves the one
    remaining chain link: the driver hands the sink a byte-identical key on a
    resume re-dispatch, so the fence keys on the SAME value across the crash.
    """

    def __init__(self) -> None:
        self.keys_by_step: dict[str, str] = {}

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.keys_by_step[str(step.step_id)] = step_context.parent_idempotency_key
        return {"step_id": str(step.step_id)}


def test_resume_redispatch_hands_byte_identical_idempotency_key_to_tool_sink() -> None:
    """B-EFFECT-FENCE chain link — the driver hands the TOOL sink a byte-identical
    per-step idempotency key on a resume re-dispatch of an uncommitted step.

    The effect at `workflow_driver.py:2031` (dispatch) precedes the per-step
    ledger commit at `:2336`, so a crash in between leaves the step uncommitted →
    `_determine_resume_at` re-dispatches it. This test proves the re-dispatch
    carries the SAME `step_context.parent_idempotency_key` the genesis run used;
    composed with the sink-side at-most-once proof in
    `harness-runtime/tests/test_effect_fence.py`, the effect fence resolves the
    re-dispatched effect to the SAME claim → fail-closed, never re-fired.
    """
    manifest = _manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT)
    steps = [_step(0, kind=StepKind.TOOL_STEP), _step(1, kind=StepKind.TOOL_STEP)]

    # Genesis run — both tool steps dispatch.
    fresh = _KeyRecordingDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=steps,
        run_id="run-1",
        ctx=cast(
            DriverContext,
            _FakeCtx(
                ledger=_FakeLedger(prior_entries=0),
                emitter=_FakeEmitter(),
                ledger_reader=_FakeLedgerReader({}),
            ),
        ),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _SingleKindRegistry(StepKind.TOOL_STEP, cast(StepDispatcher, fresh)),
        ),
    )

    # Resume — step 0 materialized (committed) → resume_at=1 → only step 1 re-runs.
    resumed = _KeyRecordingDispatcher()
    materialized = {_expected_step_key("run-1", "wf-1", 1, 0): 1}
    execute_workflow(
        manifest_entry=manifest,
        steps=steps,
        run_id="run-1",
        ctx=cast(
            DriverContext,
            _FakeCtx(
                ledger=_FakeLedger(prior_entries=1),
                emitter=_FakeEmitter(),
                ledger_reader=_FakeLedgerReader(materialized),
            ),
        ),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=cast(
            StepDispatcherRegistry,
            _SingleKindRegistry(StepKind.TOOL_STEP, cast(StepDispatcher, resumed)),
        ),
    )

    # Only step 1 re-dispatched, with the byte-identical key the genesis run used.
    assert list(resumed.keys_by_step) == ["step-1"]
    assert resumed.keys_by_step["step-1"] == fresh.keys_by_step["step-1"]


def test_entry_version_changes_idempotency_key_basis() -> None:
    """v2.12 — bumping entry_version invalidates prior-run resumption substrate.

    Prior run was at entry_version=1; this run is at entry_version=2 — the
    computed expected step keys differ → zero matches → no RESUMPTION.
    """
    manifest_v2 = _manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT)
    # Construct a manifest with entry_version=2 by re-building from defaults.
    manifest_v2 = WorkflowManifestEntry(
        workflow_id=manifest_v2.workflow_id,
        workload_class=manifest_v2.workload_class,
        persona_tier=manifest_v2.persona_tier,
        engine_class=manifest_v2.engine_class,
        topology_pattern=manifest_v2.topology_pattern,
        layer_budgets=manifest_v2.layer_budgets,
        fallback_chain=manifest_v2.fallback_chain,
        hitl_placements=manifest_v2.hitl_placements,
        per_step_overrides=manifest_v2.per_step_overrides,
        entry_version=2,
    )
    # Materialize prior-version (1) step keys.
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
    }
    ledger = _FakeLedger(prior_entries=1)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest_v2,
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # v2-keyed expected key differs from v1-stored key → no match → no RESUMPTION.
    assert WorkflowEventClass.RESUMPTION not in emitter.emits
    assert len(dispatcher.dispatched) == 1


def test_no_resumption_emission_under_pure_pattern_no_engine() -> None:
    """Under pure-pattern-no-engine, no RESUMPTION is emitted at entry
    regardless of ledger state (state-ledger native dedup per §8.2 row 3
    handles dedup at per-step idempotency_key).
    """
    ctx, _, emitter = _ctx(prior_entries=5)  # non-genesis ledger
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.PURE_PATTERN_NO_ENGINE),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert WorkflowEventClass.RESUMPTION not in emitter.emits


# ---------------------------------------------------------------------------
# AC #6 (E-impl-1 / U-CP-93) — EVENT_SOURCED_REPLAY resumption-routing
#
# EVENT_SOURCED_REPLAY is materialized as resumption-routing impl against the
# cleared C-CP-07/08 contracts, following the U-CP-56 SAVE_POINT_CHECKPOINT
# precedent (added to _IN_SCOPE as impl). The §8.1 *cached-output replay*
# refinement (replaying prior activity outputs into downstream-visible state)
# is degenerate at HEAD — the F2 EntryPayload carries no activity output and
# the driver threads no inter-step data flow (B-INTERSTEP) — and is a
# registered build arc, not exercised here. These tests assert what is
# observable: RESUMPTION emission + the materialized prefix not being
# re-dispatched ("no re-execution of activities", §8.1).
# See `.harness/r-fs-1-e-impl-1-finding.md`.
# ---------------------------------------------------------------------------


def test_event_sourced_replay_resumes_across_restart_without_refire() -> None:
    """U-CP-93 keystone — a fresh driver instance over the same persisted event
    history (simulated process restart, F3 floor (i) durable-replay-across-
    restart) advances resume_at over the contiguous materialized prefix, emits
    RESUMPTION before WORKFLOW_START, and does NOT re-dispatch the prefix.

    The dispatcher's `.dispatched` list is the side-effect counter: the
    materialized prefix steps are absent from it (§8.1 "no re-execution of
    activities" + F3 floor (ii) idempotency-keyed exactly-once — no double-
    apply). This proves no-re-execution; it does NOT prove cached-output
    replay (the deferred refinement — see the module finding above).
    """
    manifest = _manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY)
    # Materialize ledger entries for steps 0 and 1 of run-1 (the persisted
    # event-history prefix from a prior, now-crashed run instance).
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
        _expected_step_key("run-1", "wf-1", 1, 1): 1,
    }
    # Fresh ctx/emitter/dispatcher = a fresh driver instance (process restart).
    ledger = _FakeLedger(prior_entries=2)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # RESUMPTION emitted before WORKFLOW_START.
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert emitter.emits.index(WorkflowEventClass.RESUMPTION) < emitter.emits.index(
        WorkflowEventClass.WORKFLOW_START
    )
    # Only step 2 dispatched — the materialized prefix (steps 0 + 1) is NOT
    # re-fired (no re-execution of activities).
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_event_sourced_replay_no_resumption_for_genesis_run() -> None:
    """U-CP-93 — a genesis EVENT_SOURCED_REPLAY run (no prior event history)
    emits no RESUMPTION and dispatches every step, byte-identically to the
    fresh-linear happy path (the new branch perturbs nothing for resume_at==0).
    """
    manifest = _manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY)
    ledger = _FakeLedger(prior_entries=0)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader({}))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert WorkflowEventClass.RESUMPTION not in emitter.emits
    assert len(dispatcher.dispatched) == 2


def test_event_sourced_replay_observable_lifecycle_events_emit() -> None:
    """U-CP-93 F3 floor (iv) observable-lifecycle — an EVENT_SOURCED_REPLAY run
    emits the workflow lifecycle events (WORKFLOW_START + per-step STEP_BOUNDARY)
    per C-CP-05 §5.1, identical to the other in-scope engine classes.
    """
    manifest = _manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY)
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert WorkflowEventClass.WORKFLOW_START in emitter.emits
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 2


# ---------------------------------------------------------------------------
# B-ENGINE-OUTPUT-REPLAY — the §8.1 "activity outputs cached and replayed" clause:
# on an EVENT_SOURCED_REPLAY resume, the durably-stored prefix outputs are replayed
# into the inter-step channel so the first re-dispatched step reads its recovered
# predecessor (degenerate without the store: the fresh channel reads empty). The CP
# driver reads the store + channel via `getattr` (the cp_is_wiring idiom — harness-cp
# does not import the runtime holders), so these tests bind DUCK-TYPED fakes; the
# real store/channel + the real LLM dispatcher consuming the rehydrated channel are
# the runtime e2e (`test_b_engine_output_replay_*`).
# ---------------------------------------------------------------------------


class _FakeOutputStore:
    """Duck-typed `EngineOutputStore` for CP-level rehydrate tests. `journal_present`
    models whether a journal FILE exists (the discriminator the rehydrate uses when
    `read_outputs` is empty: absent → config-flip degrade; present → unreadable
    fail-close)."""

    def __init__(
        self,
        outputs: dict[int, tuple[str, dict[str, Any]]],
        *,
        journal_present: bool = True,
    ) -> None:
        self._outputs = outputs
        self._journal_present = journal_present

    def read_outputs(self, run_key: str) -> dict[int, tuple[str, dict[str, Any]]]:
        _ = run_key
        return dict(self._outputs)

    def journal_exists(self, run_key: str) -> bool:
        _ = run_key
        return self._journal_present

    def record(self, run_key: str, step_index: int, step_id: str, output: Any) -> None:
        # The producer fires on each re-dispatched step; accept it (the witness reads
        # the rehydrated prefix, not the post-run store).
        _ = run_key
        self._outputs[step_index] = (step_id, dict(output))
        self._journal_present = True


class _FakeOutputChannel:
    """Duck-typed `InterStepOutputChannel` (record + most_recent_output)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, step_id: str, output: Any) -> None:
        self.records.append((step_id, dict(output)))

    def most_recent_output(self) -> Any:
        return self.records[-1][1] if self.records else None


def _esr_resume_ctx(materialized_count: int) -> _FakeCtx:
    """An EVENT_SOURCED_REPLAY resume ctx with `materialized_count` ledger-
    materialized steps (so `resume_at == materialized_count`)."""
    materialized = {_expected_step_key("run-1", "wf-1", 1, i): 1 for i in range(materialized_count)}
    return _FakeCtx(
        ledger=_FakeLedger(prior_entries=materialized_count),
        emitter=_FakeEmitter(),
        ledger_reader=_FakeLedgerReader(materialized),
    )


class _ChannelReadingDispatcher:
    """Records, at EACH dispatch, what the inter-step channel's `most_recent_output()`
    is — a CP-level stand-in for the runtime LLM dispatcher's consumer read, so the
    witness observes what the FIRST re-dispatched step actually sees as upstream
    context (the load-bearing replay effect), not the post-run channel state (which
    the producer also appends this step's own output to)."""

    def __init__(self, channel: _FakeOutputChannel) -> None:
        self._channel = channel
        self.seen_upstream: list[Any] = []
        self.dispatched: list[tuple[Any, WorkflowStep]] = []

    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = step_context
        self.seen_upstream.append(self._channel.most_recent_output())
        self.dispatched.append((binding, step))
        return {"echo": str(step.step_id)}


def test_event_sourced_replay_rehydrates_channel_from_store_on_resume() -> None:
    """B-ENGINE-OUTPUT-REPLAY witness — a resume with a bound output store + channel
    REHYDRATES the channel from the stored prefix outputs (steps 0,1) BEFORE the loop,
    so the first re-dispatched step (step-2) SEES the recovered step-1 output as its
    upstream context (the §8.1 'outputs cached and replayed' clause; the empty-channel
    degeneracy without the store). The prefix is not re-dispatched."""
    ctx = _esr_resume_ctx(2)
    channel = _FakeOutputChannel()
    ctx.inter_step_output_channel = channel
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    dispatcher = _ChannelReadingDispatcher(channel)
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # Only step-2 re-dispatched (prefix not re-fired) AND it saw step-1's RECOVERED
    # output as upstream context (vs None without the store — the negative control).
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"
    assert dispatcher.seen_upstream == [{"feedback": "v1"}]
    # The channel was rehydrated with the prefix in order before the loop.
    assert [r[0] for r in channel.records[:2]] == ["step-0", "step-1"]


def test_event_sourced_replay_no_store_leaves_channel_empty_on_resume() -> None:
    """NEGATIVE CONTROL — the same resume WITHOUT a bound output store: the first
    re-dispatched step (step-2) sees `None` upstream (the fresh channel was never
    rehydrated — the documented degeneracy: a downstream consumer reads None where a
    fresh run would read the upstream output). Proves the store rehydration is the
    ONLY source of a recovered prefix output."""
    ctx = _esr_resume_ctx(2)
    channel = _FakeOutputChannel()
    ctx.inter_step_output_channel = channel  # but NO engine_output_store bound
    dispatcher = _ChannelReadingDispatcher(channel)
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert dispatcher.seen_upstream == [None]


def test_event_sourced_replay_missing_stored_output_fails_closed() -> None:
    """FAIL-CLOSED (store↔ledger skew) — the ledger says 2 steps materialized but the
    store is missing step 1 → the resume fails closed (FAILED + missing-output)
    rather than rehydrating a partial prefix (the symmetric of B-FANOUT-PAUSE's
    fail-close). The store-write-before-ledger-append discipline makes this state
    unreachable in practice; the guard is the conservative corruption gate."""
    ctx = _esr_resume_ctx(2)
    ctx.inter_step_output_channel = _FakeOutputChannel()
    ctx.engine_output_store = _FakeOutputStore({0: ("step-0", {"draft": "v0"})})  # step 1 absent
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "engine-output-replay-missing-output" in result.fail_class


def test_event_sourced_replay_stored_identity_mismatch_fails_closed() -> None:
    """FAIL-CLOSED (body change) — a stored prefix output whose step_id does NOT match
    the re-supplied body fails closed (FAILED + identity-mismatch) rather than applying
    stale output under the wrong step."""
    ctx = _esr_resume_ctx(2)
    ctx.inter_step_output_channel = _FakeOutputChannel()
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"v": 0}), 1: ("renamed-step", {"v": 1})}  # body has step-1 at index 1
    )
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "engine-output-replay-identity-mismatch" in result.fail_class


def test_event_sourced_replay_no_journal_degrades_not_fails() -> None:
    """DEGRADE-ON-ABSENT (advisor [P2]) — a resume where the output store is bound but
    has NO journal file (a config flip: the original run had `engine_output_replay=
    False`, so nothing was recorded) DEGRADES to the empty-channel path (the resumed
    step reads None upstream) rather than fail-closing a previously-working resume.
    NOT corruption — distinct from the partial-prefix skew (fail-closed) below."""
    ctx = _esr_resume_ctx(2)
    channel = _FakeOutputChannel()
    ctx.inter_step_output_channel = channel
    # Store bound, but EMPTY and NO journal file (config flip / fresh).
    ctx.engine_output_store = _FakeOutputStore({}, journal_present=False)
    dispatcher = _ChannelReadingDispatcher(channel)
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # NOT FAILED — the resume completes degraded; the re-dispatched step sees None.
    assert result.status is not RunStatus.FAILED
    assert dispatcher.seen_upstream == [None]


def test_event_sourced_replay_unreadable_store_fails_closed() -> None:
    """FAIL-CLOSED on unreadable store (Codex [P2]) — a journal FILE exists but yields
    no readable records (an unreadable / corrupt store; `read_outputs` returns empty
    on a read error) → fail closed rather than silently dropping cached outputs and
    resuming with wrong upstream context. Distinguished from the config-flip degrade
    above by FILE EXISTENCE."""
    ctx = _esr_resume_ctx(2)
    ctx.inter_step_output_channel = _FakeOutputChannel()
    # A journal file EXISTS but read yields nothing (unreadable / corrupt).
    ctx.engine_output_store = _FakeOutputStore({}, journal_present=True)
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "engine-output-replay-unreadable-store" in result.fail_class


# ---------------------------------------------------------------------------
# B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT (#766) + B-TOP-LEVEL-CRASH-RESUME-
# FINAL-STATE-RECONSTRUCT (this arc) — the OUTPUT-side analogue of the channel rehydrate
# above. On a durable-engine-class resume, `_execute_workflow_body` would return a
# SUFFIX-ONLY `final_state` (the loop starts at `resume_at` with `accumulated` empty; the
# committed prefix is skipped + never seeded). `reconstruct_final_state` (now DEFAULT
# True — reconstruction is the correct behavior; suffix-only was the silent-truncation
# bug) seeds the committed prefix `[0, resume_at)` from the durable store so the resumed
# `final_state` reconstructs the COMPLETE terminal state — for BOTH the top-level run
# (the `run_workflow` handler / `api.run`+`api.resume` use the default) and the child
# fold the parent fan-out / hierarchical-pause consumes (`sub_agent_dispatch.py` folds
# `child_result.final_state` verbatim). Scoped to the durable-output-store classes
# ESR / WAL / SAVE_POINT_CHECKPOINT / RECONCILER_LOOP (`_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES`;
# SAVE_POINT joined at v1.79 and RECONCILER at v1.80 — the store is mechanically class-agnostic,
# witnessed by a real forward-run round-trip below). RECONCILER joining resolved the registered
# two-authorities probe: its U-RT-123 substrate persists a CONVERGENCE DIGEST (StateSummary) for
# the CAS-lease, NOT the per-step output map → no competing output authority. An aborting CAS
# reconverge returns FAILED upstream of the seed (no reconstruction on a lost claim). The lone
# non-member is PURE_PATTERN_NO_ENGINE (non-durable, not resumable). Explicit
# `reconstruct_final_state=False` is the opt-out escape hatch (no production caller takes it).
# ---------------------------------------------------------------------------


def test_reconstruct_final_state_seeds_committed_prefix_on_resume() -> None:
    """RECONSTRUCT witness — an EVENT_SOURCED_REPLAY resume (steps 0,1 committed) with the
    child-scoped opt-in seeds `accumulated` with the durably-stored prefix so the SUCCESS
    `final_state` reconstructs the COMPLETE terminal state {step-0, step-1, step-2}, not
    the suffix-only {step-2}. This is the result-fidelity the parent fold needs (a
    suffix-only child final_state silently corrupts the parent aggregate)."""
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    dispatcher = _EchoDispatcher()
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    # FULL terminal state — the committed prefix is seeded from the store + the suffix
    # step-2 dispatched this envelope (the prefix is NOT re-dispatched).
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}
    assert result.final_state["step-0"] == {"draft": "v0"}
    assert result.final_state["step-1"] == {"feedback": "v1"}
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_top_level_default_reconstructs_final_state() -> None:
    """TOP-LEVEL default-on witness (B-TOP-LEVEL-CRASH-RESUME-FINAL-STATE-RECONSTRUCT) —
    the SAME resume WITHOUT passing the flag (the path the top-level `run_workflow`
    handler at `mcp_server.py:427` / `harness_runtime.api.run`+`api.resume` take) now
    reconstructs the COMPLETE final_state {step-0, step-1, step-2}, because the
    `reconstruct_final_state` default is True (reconstruction is the correct behavior;
    suffix-only was the silent-truncation bug — a SUCCESS run that lied about its output).
    RED before the default flip: this returned the suffix-only {step-2}. Closes the
    v1.75 §2 registered top-level follow-on (impl-not-fork: the spec was silent on the
    resume final_state shape — CP v1.76 §25.2/§25.6 now DEFINES the resume-transparency
    invariant — and no real consumer relied on suffix-only)."""
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        # reconstruct_final_state omitted → default True (the top-level path).
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}


def test_explicit_opt_out_leaves_suffix_only_final_state() -> None:
    """GATED-NOT-AUTOMATIC control (RED-without-the-seed) — the SAME resume with the seed
    EXPLICITLY opted out (`reconstruct_final_state=False`) returns the suffix-only
    final_state {step-2}. Proves the seeding is GATED on the flag — it does not happen by
    accident — and documents the opt-out escape hatch that preserves the pre-arc degenerate
    behavior (no production caller takes it; the default is now reconstruct)."""
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=False,  # explicit opt-out → the degenerate suffix-only path.
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-2"}


def test_reconstruct_final_state_fails_closed_on_store_skew() -> None:
    """FAIL-CLOSED — with the opt-in on, a store missing a committed prefix step (the
    ledger says 2 materialized but the store holds only step 0) fails the CHILD run
    closed (FAILED + missing-output) rather than folding a partial state into the parent.
    Reuses the shared `_read_durable_replay_prefix` skew gate."""
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore({0: ("step-0", {"draft": "v0"})})  # step 1 absent
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "engine-output-replay-missing-output" in result.fail_class


def test_reconstruct_final_state_round_trips_through_store_reconciler() -> None:
    """FULL-CHAIN round-trip (producer→crash→consumer) for RECONCILER_LOOP — the BLOCKING
    producer-half witness for B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT-RECONCILER (the
    RECONCILER slice, completing the final_state-reconstruction family across ALL FOUR durable
    classes). A fresh RECONCILER_LOOP run RECORDS its prefix to the store (phase 1, via the
    REAL `_record_durable_step_output` producer gate now extended to RECONCILER — NOT a hand-
    seeded store), then a resume READS IT BACK + RECONSTRUCTS the full final_state (phase 2,
    via the seed gate extended in the SAME PR). This proves the EngineOutputStore is mechanically
    class-agnostic and a real RECONCILER forward run flows through the same LINEAR dispatch loop
    as ESR/WAL/SAVE_POINT — the grounding that resolved the registered two-authorities probe (the
    U-RT-123 reconciler substrate carries a CONVERGENCE DIGEST for its CAS-lease, NOT the per-step
    output map, so store-reuse adds no competing authority). RED before the gate extension: phase
    1 records NOTHING (producer excludes RECONCILER) → phase 2's final_state would be {step-2}."""
    store = _FakeOutputStore({}, journal_present=False)

    # Phase 1 — fresh RECONCILER run (resume_at == 0): all 3 steps dispatch + RECORD.
    ctx1 = _esr_resume_ctx(0)
    ctx1.engine_output_store = store
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx1),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    # The producer recorded the committed prefix on the fresh run (proves the producer half
    # is non-vacuous for RECONCILER — not papered over by a hand-seeded store).
    assert set(store.read_outputs("run-1").keys()) == {0, 1, 2}

    # Phase 2 — resume (steps 0,1 committed) over the SAME store: reconstruct the full state.
    ctx2 = _esr_resume_ctx(2)
    ctx2.engine_output_store = store
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx2),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}


def test_reconstruct_final_state_engine_class_gate_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STANDING GATE GUARD — proves the engine-class gate is load-bearing without depending on
    reverting the source. With RECONCILER monkeypatched OUT of
    `_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES`, a RECONCILER resume over a bound store holding the
    prefix degrades to the suffix-only final_state {step-2} (no reconstruction) — so the gate, not
    a stray bound store, is what selects reconstruction. Mirrors the file's `_IN_SCOPE`-patch idiom
    (test_workload_engine_class_selection_*). After RECONCILER joined, every durable resumable
    class reconstructs, so this patch-out is the boundary proof (PURE_PATTERN_NO_ENGINE is
    non-resumable → resume_at is always 0 → it can't exercise the gate)."""
    import harness_cp.workflow_driver as _wd

    monkeypatch.setattr(
        _wd,
        "_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES",
        frozenset(_wd._FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES) - {EngineClass.RECONCILER_LOOP},
    )
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-2"}


class _FakeReconcilerRecoveryLoop:
    """Duck-typed `RuntimeEngineRecoveryLoop` for the RECONCILER CAS-firing witnesses. The CP
    driver's RECONCILER branch calls `has_pause_record(...)` (sync) then, if a record is present,
    `attempt_resume(...)` (async, run via `asyncio.run`) and reads
    `result.resume_outcome.outcome_kind`."""

    def __init__(self, outcome_kind: Any, *, has_record: bool = True) -> None:
        self._kind = outcome_kind
        self._has_record = has_record

    def has_pause_record(self, *, engine_class: Any, workflow_id: Any, run_id: Any) -> bool:
        _ = (engine_class, workflow_id, run_id)
        return self._has_record

    async def attempt_resume(self, **_kwargs: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(resume_outcome=SimpleNamespace(outcome_kind=self._kind))


def test_reconstruct_final_state_reconciler_cas_abort_short_circuits_before_seed() -> None:
    """CAS-ORDERING (advisor check) — a RECONCILER resume whose engine recovery loop fires a CAS
    reconverge that ABORTs (a LOST claim) returns FAILED with `final_state=None` BEFORE the seed
    site, even though a store holding the full prefix is bound. So reconstruction NEVER papers over
    a lost claim — the abort short-circuits (workflow_driver.py CAS block) upstream of the seed.
    Pre-existing fail-closed behavior, but RECONCILER-specific and unexercised by the ESR/WAL/
    SAVE_POINT slices; this pins the ordering."""
    from harness_cp.pause_resume_protocol import ResumeOutcomeKind

    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    ctx.engine_recovery_loop = _FakeReconcilerRecoveryLoop(
        ResumeOutcomeKind.ABORT_REVALIDATION_FAILED
    )
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.FAILED
    assert result.final_state is None


def test_reconstruct_final_state_reconciler_cas_clean_falls_through_and_reconstructs() -> None:
    """CAS-ORDERING (advisor check) — a RECONCILER resume whose CAS reconverge fires and SUCCEEDS
    (RESUME_CLEAN, a WON claim) falls THROUGH the CAS block to the seed site and reconstructs the
    full final_state {step-0,1,2} from the bound store. Complements the abort witness: a succeeding
    claim does not bypass reconstruction."""
    from harness_cp.pause_resume_protocol import ResumeOutcomeKind

    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    ctx.engine_recovery_loop = _FakeReconcilerRecoveryLoop(ResumeOutcomeKind.RESUME_CLEAN)
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}


def test_reconstruct_final_state_no_journal_degrades_not_fails() -> None:
    """DEGRADE-ON-ABSENT — the opt-in is on for an ESR resume but the store has NO journal
    file (a config flip: the original run had `engine_output_replay=False`). The
    reconstruct degrades to the suffix-only final_state {step-2} (the pre-arc behavior),
    NOT a fail-close — never failing a previously-working resume. Distinct from the
    partial-prefix skew (fail-closed) above."""
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore({}, journal_present=False)
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-2"}


def test_reconstruct_final_state_round_trips_through_store_wal_segment() -> None:
    """FULL-CHAIN round-trip (producer→crash→consumer) — a fresh WAL_SEGMENT run RECORDS
    its prefix to the store (phase 1, via the real `_record_durable_step_output` producer
    gate), then a resume READS IT BACK + RECONSTRUCTS the full final_state (phase 2) —
    proving the producer gate + the reconstruct compose through the real `execute_workflow`
    for WAL_SEGMENT (parity with EVENT_SOURCED_REPLAY), not a pre-seeded prefix. The store
    is the CP-level `_FakeOutputStore` duck (harness-cp does not import the runtime
    `EngineOutputStore`); its `record`/`read_outputs` round-trip is genuine. RED without
    the fix: phase 2's final_state would be {step-2} only."""
    store = _FakeOutputStore({}, journal_present=False)

    # Phase 1 — fresh run (resume_at == 0): all 3 steps dispatch + RECORD to the store.
    ctx1 = _esr_resume_ctx(0)
    ctx1.engine_output_store = store
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.WAL_SEGMENT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx1),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    # Phase 2 — resume (steps 0,1 committed) over the SAME store: reconstruct the full state.
    ctx2 = _esr_resume_ctx(2)
    ctx2.engine_output_store = store
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.WAL_SEGMENT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx2),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}


def test_reconstruct_final_state_round_trips_through_store_save_point() -> None:
    """FULL-CHAIN round-trip (producer→crash→consumer) for SAVE_POINT_CHECKPOINT — the
    BLOCKING producer-half witness for B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT-SAVE-
    POINT (the SAVE_POINT slice of the registered SAVE-POINT-RECONCILER follow-on). A fresh
    SAVE_POINT_CHECKPOINT run RECORDS its prefix to the store (phase 1, via the REAL
    `_record_durable_step_output` producer gate now extended to SAVE_POINT — NOT a hand-
    seeded store), then a resume READS IT BACK + RECONSTRUCTS the full final_state (phase 2,
    via the seed gate extended in the SAME PR). This proves the EngineOutputStore is
    mechanically class-agnostic and a real SAVE_POINT forward run flows through the same
    LINEAR dispatch loop as ESR/WAL — the grounding that overturns the registered "needs an
    entirely new substrate" anticipation. RED before the gate extensions: phase 1 records
    NOTHING (producer excludes SAVE_POINT) → phase 2's final_state would be {step-2} only."""
    store = _FakeOutputStore({}, journal_present=False)

    # Phase 1 — fresh SAVE_POINT run (resume_at == 0): all 3 steps dispatch + RECORD.
    ctx1 = _esr_resume_ctx(0)
    ctx1.engine_output_store = store
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx1),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    # The producer recorded the committed prefix on the fresh run (proves the producer half
    # is non-vacuous for SAVE_POINT — not papered over by a hand-seeded store).
    assert set(store.read_outputs("run-1").keys()) == {0, 1, 2}

    # Phase 2 — resume (steps 0,1 committed) over the SAME store: reconstruct the full state.
    ctx2 = _esr_resume_ctx(2)
    ctx2.engine_output_store = store
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx2),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}


def test_reconstruct_also_seeds_partial_state_on_drained_child() -> None:
    """PARTIAL_STATE reconstruction — the prefix is seeded into `accumulated` BEFORE the
    loop, so a DRAINED child's `partial_state=dict(accumulated)` ALSO reconstructs the
    committed prefix (the parent fold consumes `child_result.partial_state` on a DRAINED
    child at `sub_agent_dispatch.py:668` — the SAME suffix-only corruption class as
    final_state). Here step-2 dispatches (joining the seeded prefix), then the dispatcher
    sets the drain flag → the post-step drain returns DRAINED with the FULL partial_state
    {step-0, step-1, step-2}, not the suffix-only {step-2}."""
    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )

    class _DrainAfterDispatch:
        def __init__(self, flag: asyncio.Event) -> None:
            self._flag = flag
            self.dispatched: list[tuple[Any, WorkflowStep]] = []

        def dispatch(
            self, binding: Any, step: WorkflowStep, *, step_context: Any = None
        ) -> dict[str, Any]:
            _ = step_context
            self.dispatched.append((binding, step))
            self._flag.set()  # drain after this step completes → post-step DRAINED return
            return {"step_id": str(step.step_id), "echoed": True}

    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _DrainAfterDispatch(ctx.drained_flag))),
        reconstruct_final_state=True,
    )
    assert result.status is RunStatus.DRAINED
    assert result.partial_state is not None
    assert set(result.partial_state.keys()) == {"step-0", "step-1", "step-2"}


def test_reconstruct_final_state_on_explicit_pause_override_path() -> None:
    """#680 WITNESS (explicit-pause re-enter path) — the seeding site is reached on BOTH
    resume paths: the crash-recovery engine-class block (the tests above) AND the
    `resume_at_step_index_override` path that the B-HIERARCHICAL-PAUSE child re-enter
    (#680) + every `attempt_resume`-validated resume drive. Driving `_execute_workflow_body`
    with the override directly (resume_at = 2 from the override, NOT the engine-class
    block) still reconstructs the full final_state {step-0, step-1, step-2} — so the #680
    captured-child-resume fold reconstructs for an ESR/WAL child via the SAME shared
    seeding site (the SAVE_POINT/RECONCILER hierarchical child stays suffix-only → the
    registered output-substrate follow-on)."""
    from harness_cp.workflow_driver import _execute_workflow_body
    from opentelemetry import trace as _otel_trace

    ctx = _esr_resume_ctx(2)
    ctx.engine_output_store = _FakeOutputStore(
        {0: ("step-0", {"draft": "v0"}), 1: ("step-1", {"feedback": "v1"})}
    )
    span = _otel_trace.get_tracer("test").start_span("test-envelope")
    result, _steps_executed = _execute_workflow_body(
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        span=span,
        run_idempotency_key="rik-1",  # the fake store ignores the key
        resume_at_step_index_override=2,  # the explicit-pause / #680 resume path
        reconstruct_final_state=True,
    )
    span.end()
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}


# ---------------------------------------------------------------------------
# B-ENGINE-OUTPUT-REPLAY-WAL-SEGMENT — WAL_SEGMENT shares the EngineOutputStore +
# the C-CP-08 §8.1 cached-output-replay refinement (the EVENT_SOURCED_REPLAY shape
# applied to the segment-replay class). The producer gate AND the resume-side
# rehydrate were BOTH extended to WAL_SEGMENT in one arc (never record-only — a
# never-rehydrated journal is the exact defect the producer gate prevents).
# ---------------------------------------------------------------------------


def test_wal_segment_records_then_rehydrates_full_chain() -> None:
    """FULL-CHAIN witness (advisor) — a WAL_SEGMENT run RECORDS each step output to
    the shared store (phase 1, fresh run), then a resume REHYDRATES the stored prefix
    so the first re-dispatched segment-step SEES its recovered predecessor's output
    (phase 2). Proves BOTH halves through the real `execute_workflow` (the producer
    gate now fires for WAL_SEGMENT + the §8.1 cached-output rehydrate) — not
    gate-membership / store-has-records presence."""
    store = _FakeOutputStore({}, journal_present=False)

    # Phase 1 — fresh WAL_SEGMENT run (resume_at == 0): all 3 steps dispatch + record.
    ctx1 = _esr_resume_ctx(0)  # 0 materialized → segment-replay resume_at == 0 (fresh)
    ctx1.inter_step_output_channel = _FakeOutputChannel()
    ctx1.engine_output_store = store
    disp1 = _ChannelReadingDispatcher(ctx1.inter_step_output_channel)
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.WAL_SEGMENT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx1),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, disp1)),
    )
    assert len(disp1.dispatched) == 3  # fresh run, all steps dispatched
    # The producer gate now fires for WAL_SEGMENT — each step output is durably stored.
    assert set(store.read_outputs("run-1").keys()) == {0, 1, 2}

    # Phase 2 — resume with 2 materialized (resume_at == 2): rehydrate from the store.
    ctx2 = _esr_resume_ctx(2)
    ctx2.inter_step_output_channel = _FakeOutputChannel()
    ctx2.engine_output_store = store  # SAME store, now carrying the phase-1 records
    disp2 = _ChannelReadingDispatcher(ctx2.inter_step_output_channel)
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.WAL_SEGMENT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx2),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, disp2)),
    )
    # Only step-2 re-dispatched (prefix not re-fired) AND it saw step-1's RECOVERED
    # output (the phase-1 record), not None.
    assert len(disp2.dispatched) == 1
    assert str(disp2.dispatched[0][1].step_id) == "step-2"
    assert disp2.seen_upstream == [{"echo": "step-1"}]


def test_wal_segment_no_store_leaves_channel_empty_on_resume() -> None:
    """NEGATIVE CONTROL — the same WAL_SEGMENT resume WITHOUT a bound output store:
    step-2 sees `None` upstream (the rehydration is the ONLY source of the recovered
    prefix output; mirrors the EVENT_SOURCED_REPLAY negative control)."""
    ctx = _esr_resume_ctx(2)
    channel = _FakeOutputChannel()
    ctx.inter_step_output_channel = channel  # but NO engine_output_store bound
    dispatcher = _ChannelReadingDispatcher(channel)
    execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.WAL_SEGMENT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert dispatcher.seen_upstream == [None]


def test_wal_segment_missing_stored_output_fails_closed() -> None:
    """FAIL-CLOSED (store↔ledger skew) — WAL_SEGMENT shares the corruption gate via the
    same `_rehydrate_inter_step_channel_on_replay` helper: the ledger says 2 steps
    materialized but the store is missing step 1 → FAILED rather than a partial-prefix
    rehydrate (free with the shared rehydrate half)."""
    ctx = _esr_resume_ctx(2)
    ctx.inter_step_output_channel = _FakeOutputChannel()
    ctx.engine_output_store = _FakeOutputStore({0: ("step-0", {"draft": "v0"})})  # step 1 absent
    result = execute_workflow(
        manifest_entry=_manifest(engine_class=EngineClass.WAL_SEGMENT),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "engine-output-replay-missing-output" in result.fail_class


# ---------------------------------------------------------------------------
# U-CP-94 (R-FS-1 E-impl-2) — WAL_SEGMENT segment-replay resumption (CP/IS-only)
#
# WAL_SEGMENT is materialized as segment-replay resumption impl against the
# cleared C-CP-07/08 contracts, following the U-CP-56 / U-CP-93 precedent. These
# tests assert the CP-clear surface: segment-prefix resume_at (the F2
# idempotency-key join, §8.2 row 5) + RESUMPTION emission + the materialized
# segment prefix not being re-dispatched. The engine-layer recovery-loop firing
# (U-CP-95 capture_pause/attempt_resume → cp.pause-captured/cp.resume-attempted)
# requires a bound `ctx.engine_recovery_loop` (the durable U-RT-121 substrate
# via the U-RT-122 factory bind) and is exercised by execution at the runtime
# go-live e2e (test_u_rt_95...). As with EVENT_SOURCED_REPLAY the CP/IS-level
# resume_at is degenerate vs save-point (same accepted bar); see
# `.harness/r-fs-1-e-impl-2-finding.md`.
# ---------------------------------------------------------------------------


def test_wal_segment_resumes_across_restart_without_refire() -> None:
    """U-CP-94 keystone — a fresh driver instance over the same persisted segment
    prefix (simulated process restart, F3 floor (i)) advances resume_at over the
    contiguous materialized segment prefix, emits RESUMPTION before
    WORKFLOW_START, and does NOT re-dispatch the prefix (§8.1 `segment_replay`
    "per-segment dedup" — the F2 idempotency-key join is the dedup, F3 floor
    (ii)). Mirrors the U-CP-93 EVENT_SOURCED_REPLAY keystone; proves
    no-re-execution, not cached-output replay (the deferred B-ENGINE-OUTPUT-REPLAY
    refinement)."""
    manifest = _manifest(engine_class=EngineClass.WAL_SEGMENT)
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
        _expected_step_key("run-1", "wf-1", 1, 1): 1,
    }
    ledger = _FakeLedger(prior_entries=2)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert emitter.emits.index(WorkflowEventClass.RESUMPTION) < emitter.emits.index(
        WorkflowEventClass.WORKFLOW_START
    )
    # Only step 2 dispatched — the materialized segment prefix (0 + 1) is NOT
    # re-fired (no re-execution).
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_wal_segment_no_resumption_for_genesis_run() -> None:
    """U-CP-94 — a genesis WAL_SEGMENT run (no prior segments) emits no RESUMPTION
    and dispatches every step, byte-identically to the fresh-linear happy path
    (the new branch perturbs nothing for resume_at==0)."""
    manifest = _manifest(engine_class=EngineClass.WAL_SEGMENT)
    ledger = _FakeLedger(prior_entries=0)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader({}))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert WorkflowEventClass.RESUMPTION not in emitter.emits
    assert len(dispatcher.dispatched) == 2


def test_wal_segment_observable_lifecycle_events_emit() -> None:
    """U-CP-94 F3 floor (iv) observable-lifecycle — a WAL_SEGMENT run emits the
    workflow lifecycle events (WORKFLOW_START + per-step STEP_BOUNDARY) per
    C-CP-05 §5.1, identical to the other in-scope engine classes."""
    manifest = _manifest(engine_class=EngineClass.WAL_SEGMENT)
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert WorkflowEventClass.WORKFLOW_START in emitter.emits
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 2


# ---------------------------------------------------------------------------
# U-CP-96 (R-FS-1 E-impl-3a) — RECONCILER_LOOP convergence resumption (CP/IS-only)
#
# RECONCILER_LOOP is materialized as level-triggered read/diff/converge resumption
# impl against the cleared C-CP-07/08 contracts + the v1_33 §7.4 substrate-deferral
# (hand-rolled etcd-style per I-6), following the U-CP-93/94 precedent. These tests
# assert the CP-clear surface: convergence-prefix resume_at (the F2 idempotency-key
# join, §8.2 row 4 "reconciler reads ledger to detect prior actions") + RESUMPTION
# emission + the materialized prefix not being re-dispatched. RECONCILER_LOOP is the
# EVENT_SOURCED_REPLAY shape at this unit — it does NOT fire the engine recovery loop
# (that is U-CP-97, E-impl-3b). The genuine distinguishing capabilities (the durable
# CAS-lease etcd-style substrate U-RT-123 + the recovery-loop firing U-CP-97) are
# E-impl-3b. The §8.1 *cached-output replay* refinement is degenerate at HEAD
# (B-ENGINE-OUTPUT-REPLAY arc), as for ESR/WAL. RECONCILER_LOOP is the LAST engine
# class — these tests close the per-class materialization coverage.
# ---------------------------------------------------------------------------


def test_reconciler_loop_resumes_across_restart_without_refire() -> None:
    """U-CP-96 keystone — a fresh driver instance over the same persisted F2
    convergence prefix (simulated process restart, F3 floor (i) durable-reconverge-
    across-restart) advances resume_at over the contiguous materialized prefix, emits
    RESUMPTION before WORKFLOW_START, and does NOT re-dispatch the prefix.

    The dispatcher's `.dispatched` list is the side-effect counter: the materialized
    prefix steps are absent (the F2 idempotency-key join is the convergence dedup,
    §8.2 row 4 + F3 floor (ii) idempotency-keyed exactly-once). This proves
    no-re-execution of already-converged steps; it does NOT prove the engine-owned
    CRD_RECONCILER_LEDGER / CAS-lease substrate (U-RT-123, E-impl-3b) nor the
    recovery-loop firing (U-CP-97) — this is the (A) resumption-semantics half.
    """
    manifest = _manifest(engine_class=EngineClass.RECONCILER_LOOP)
    # Materialize ledger entries for steps 0 and 1 of run-1 (the persisted
    # convergence prefix from a prior, now-crashed run instance).
    materialized = {
        _expected_step_key("run-1", "wf-1", 1, 0): 1,
        _expected_step_key("run-1", "wf-1", 1, 1): 1,
    }
    # Fresh ctx/emitter/dispatcher = a fresh driver instance (process restart).
    ledger = _FakeLedger(prior_entries=2)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader(materialized))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # RESUMPTION emitted before WORKFLOW_START.
    assert WorkflowEventClass.RESUMPTION in emitter.emits
    assert emitter.emits.index(WorkflowEventClass.RESUMPTION) < emitter.emits.index(
        WorkflowEventClass.WORKFLOW_START
    )
    # Only step 2 dispatched — the converged prefix (steps 0 + 1) is NOT re-fired.
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_reconciler_loop_no_resumption_for_genesis_run() -> None:
    """U-CP-96 — a genesis RECONCILER_LOOP run (no prior convergence prefix) emits
    no RESUMPTION and dispatches every step, byte-identically to the fresh-linear
    happy path (the new branch perturbs nothing for resume_at==0)."""
    manifest = _manifest(engine_class=EngineClass.RECONCILER_LOOP)
    ledger = _FakeLedger(prior_entries=0)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=ledger, emitter=emitter, ledger_reader=_FakeLedgerReader({}))
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert WorkflowEventClass.RESUMPTION not in emitter.emits
    assert len(dispatcher.dispatched) == 2


def test_reconciler_loop_observable_lifecycle_events_emit() -> None:
    """U-CP-96 F3 floor (iv) observable-lifecycle — a RECONCILER_LOOP run emits the
    workflow lifecycle events (WORKFLOW_START + per-step STEP_BOUNDARY) per
    C-CP-05 §5.1, identical to the other in-scope engine classes."""
    manifest = _manifest(engine_class=EngineClass.RECONCILER_LOOP)
    ctx, _, emitter = _ctx()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert WorkflowEventClass.WORKFLOW_START in emitter.emits
    assert emitter.emits.count(WorkflowEventClass.STEP_BOUNDARY) == 2


# ---------------------------------------------------------------------------
# AC #7 — Terminal SUCCESS return
# ---------------------------------------------------------------------------


def test_terminal_success_return_shape() -> None:
    ctx, _, _ = _ctx()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.SUCCESS
    assert result.terminal_step_index is None
    assert result.partial_state is None
    assert result.fail_class is None
    assert result.final_state is not None
    # final_state aggregates step outputs keyed by step_id.
    assert set(result.final_state.keys()) == {"step-0", "step-1"}


# ---------------------------------------------------------------------------
# AC #8 — Failure-mode taxonomy
# ---------------------------------------------------------------------------


def test_step_failure_returns_failed_status() -> None:
    ctx, _, _ = _ctx()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher(fail_at_step=1))),
    )
    assert result.status is RunStatus.FAILED
    assert result.terminal_step_index == 1
    assert result.partial_state is not None
    assert "step-0" in result.partial_state
    assert "step-1" not in result.partial_state  # failed step not in partial
    assert result.fail_class is not None
    assert result.fail_class.startswith("step-failure")


def test_ledger_append_failure_returns_failed_status() -> None:
    failing_ledger = _FakeLedger(fail=True)
    emitter = _FakeEmitter()
    ctx = _FakeCtx(ledger=failing_ledger, emitter=emitter)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert result.fail_class.startswith("ledger-append-failed")


# ---------------------------------------------------------------------------
# AC #9 — Determinism
# ---------------------------------------------------------------------------


def test_driver_iteration_deterministic_given_inputs() -> None:
    """Two runs with identical inputs produce identical observable results:
    same emission sequence, same dispatched step order, same final_state keys.
    """
    runs: list[tuple[tuple[WorkflowEventClass, ...], tuple[str, ...], tuple[str, ...]]] = []
    for _ in range(2):
        ctx, _, emitter = _ctx()
        dispatcher = _EchoDispatcher()
        result = execute_workflow(
            manifest_entry=_manifest(),
            steps=[_step(0), _step(1)],
            run_id="run-deterministic",
            ctx=cast(DriverContext, ctx),
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
        )
        runs.append(
            (
                tuple(emitter.emits),
                tuple(str(s.step_id) for _, s in dispatcher.dispatched),
                tuple(sorted((result.final_state or {}).keys())),
            )
        )
    assert runs[0] == runs[1]


# ---------------------------------------------------------------------------
# Tenant-id binding lift — driver reads ctx.tenant_id at StepExecutionContext
# composition site (replacing the v1.6 MVP hardcoded None). Per workflow_
# driver_types.py deferral comment: this is the v1.7+ extension that lifts
# the hardcode as a binding fix (per-deployment scoping via RuntimeConfig,
# not a per-workflow WorkflowManifestEntry schema extension like CP-19's
# default_gate_level at CP spec v1.20 §6.1.Y).
# ---------------------------------------------------------------------------


class _TenantIdProbeDispatcher:
    """Records `step_context.tenant_id` observed at each dispatch."""

    def __init__(self) -> None:
        self.observed: list[str | None] = []

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.observed.append(getattr(step_context, "tenant_id", "<missing>"))
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


def _run_and_capture_tenant_id(*, tenant_id: str | None) -> str | None:
    ctx, _, _ = _ctx()
    ctx.tenant_id = tenant_id  # override default None set at _FakeCtx.__init__
    probe = _TenantIdProbeDispatcher()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-tenant-test",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, probe)),
    )
    assert len(probe.observed) == 1
    return probe.observed[0]


def test_tenant_id_none_propagates_to_step_context() -> None:
    """Single-tenant (default): ctx.tenant_id=None → step_context.tenant_id=None."""
    assert _run_and_capture_tenant_id(tenant_id=None) is None


def test_tenant_id_non_none_propagates_to_step_context() -> None:
    """Multi-tenant: ctx.tenant_id='acme' → step_context.tenant_id='acme'."""
    assert _run_and_capture_tenant_id(tenant_id="acme") == "acme"


def test_tenant_id_empty_string_propagates_verbatim() -> None:
    """Empty-string tenant is NOT coerced at driver layer.

    Coercion (if any) is audit-writer's concern per `_tenant_tag` (which
    treats falsy as single-tenant sentinel). The driver propagates verbatim.
    """
    assert _run_and_capture_tenant_id(tenant_id="") == ""


class _BranchFieldsProbeDispatcher:
    """Records `step_context.branch_index` + `.agent_role` at each dispatch
    (U-CP-81 regression: the SINGLE_THREADED_LINEAR path composes no branch
    fields)."""

    def __init__(self) -> None:
        self.observed: list[tuple[Any, Any]] = []

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        self.observed.append(
            (
                getattr(step_context, "branch_index", "<missing>"),
                getattr(step_context, "agent_role", "<missing>"),
            )
        )
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


def test_linear_path_composes_no_branch_fields() -> None:
    """U-CP-81 — the SINGLE_THREADED_LINEAR strategy composes the existing
    per-step context verbatim; no branch field is set on the linear path."""
    ctx, _, _ = _ctx()
    probe = _BranchFieldsProbeDispatcher()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-branch-fields",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, probe)),
    )
    assert probe.observed == [(None, None), (None, None)]


def test_driver_context_protocol_declares_tenant_id() -> None:
    """DriverContext Protocol must declare tenant_id (structural typing check).

    HarnessContext satisfies the Protocol via a `@computed_field` property
    reading `self.config.tenant_id`; test fixtures (_FakeCtx) bind it as a
    plain instance attribute. Both shapes match structurally.
    """
    assert "tenant_id" in DriverContext.__annotations__, (
        "DriverContext.tenant_id must be declared so HarnessContext can "
        "structurally satisfy the protocol via the computed property."
    )


def test_driver_context_protocol_declares_procedural_tier_snapshot_resolver() -> None:
    """R-003 — DriverContext must declare the resolver field so HarnessContext
    structurally satisfies it (bound at bootstrap stage 6)."""
    assert "procedural_tier_snapshot_resolver" in DriverContext.__annotations__


class _LedgerOnlyCtx:
    """Minimal DriverContext shape for `_append_step_ledger_entry` (it reads
    only `ledger_writer` + `procedural_tier_snapshot_resolver`)."""

    def __init__(self, ledger: _FakeLedger, resolver: Any) -> None:
        self.ledger_writer = ledger
        self.procedural_tier_snapshot_resolver = resolver


def test_append_step_ledger_entry_populates_procedural_tier_snapshot_ref() -> None:
    """R-003 — the per-step state-ledger write (§25.3.3.7, workflow-context)
    populates the sidecar via the bound resolver per IS spec v1.3 §C-IS-05 §5.1."""
    ledger = _FakeLedger()
    ctx = _LedgerOnlyCtx(ledger, lambda: Identifier("b" * 64))
    _append_step_ledger_entry(
        ctx=cast(DriverContext, ctx),
        workflow_id="wf-1",
        step_index=0,
        step_idempotency_key="idem-0",
        step_output={"ok": True},
    )
    [(payload, _key)] = ledger.appends
    assert payload.procedural_tier_snapshot_ref == Identifier("b" * 64)


def test_append_step_ledger_entry_none_when_resolver_absent() -> None:
    """R-003 — when no resolver is bound (operator opt-out / test ctx), the
    sidecar stays None (the getattr-defensive opt-out path)."""
    ledger = _FakeLedger()
    ctx = _LedgerOnlyCtx(ledger, None)
    _append_step_ledger_entry(
        ctx=cast(DriverContext, ctx),
        workflow_id="wf-1",
        step_index=0,
        step_idempotency_key="idem-0",
        step_output={"ok": True},
    )
    [(payload, _key)] = ledger.appends
    assert payload.procedural_tier_snapshot_ref is None


# ---------------------------------------------------------------------------
# R-FS-1 B4 Slice 3 — per-step PROMPT override binding-flow seam (driver→dispatch)
# ---------------------------------------------------------------------------


def test_per_step_prompt_override_threads_through_driver_to_dispatch() -> None:
    """CP spec v1.37 §6.1/§6.2 — a `StepOverride.prompt_version_sha` annotated on
    a manifest step is resolved by the driver's `resolve_step_binding` call and
    threaded on the SAME `StepEffectiveBinding` object to the dispatcher, exactly
    as `model_binding` already flows. This proves the driver→dispatch binding-flow
    seam end-to-end through the real `execute_workflow` loop (the runtime dispatch
    consumes `binding.prompt_version_sha` — covered at
    test_lifecycle_llm_dispatch.py — but the CP driver must actually deliver it).
    """
    sha = "d" * 64
    manifest = WorkflowManifestEntry(
        workflow_id="wf-1",
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={
            StepID("step-0"): StepOverride(step_id=StepID("step-0"), prompt_version_sha=sha)
        },
    )
    ctx, _, _ = _ctx()
    dispatcher = _EchoDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # step-0 carries the per-step prompt override on its dispatched binding;
    # step-1 (no override) is None — proving the field is per-step-scoped, not
    # leaked run-wide.
    by_step = {str(s.step_id): b for b, s in dispatcher.dispatched}
    assert by_step["step-0"].prompt_version_sha == sha
    assert by_step["step-0"].override_applied is True
    assert by_step["step-1"].prompt_version_sha is None
    assert by_step["step-1"].override_applied is False


# ---------------------------------------------------------------------------
# R-FS-1 B4 Slice 4 — per-step ROLE override folded onto step_context.agent_role
# ---------------------------------------------------------------------------


def test_per_step_role_override_threads_onto_linear_step_context() -> None:
    """CP spec v1.38 §6.1 (B4 Slice 4) — a `StepOverride.agent_role` on a
    SINGLE_THREADED_LINEAR step is folded by the driver onto the SINGLE
    `step_context.agent_role` source (the §14.5.3 invariant-3 "linear path
    untouched" composition-time relaxation). By-execution proof through the real
    `execute_workflow` loop: the dispatch-observed `step_context.agent_role` IS
    the override on step-0; step-1 (no override) stays None (byte-identical to
    v1.37 — invariant-1 non-breaking default holds per-step)."""
    role = AgentRole("specialist-reviewer")
    manifest = _manifest().model_copy(
        update={
            "per_step_overrides": {
                StepID("step-0"): StepOverride(step_id=StepID("step-0"), agent_role=role)
            }
        }
    )
    ctx, _, _ = _ctx()
    probe = _BranchFieldsProbeDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-role",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, probe)),
    )
    # (branch_index, agent_role) per dispatched step: linear → branch_index None
    # throughout; agent_role is the override on step-0, None on step-1.
    assert probe.observed == [(None, role), (None, None)]


# ---------------------------------------------------------------------------
# R-FS-1 B-HITL-PLACEMENT-PER-STEP-PRODUCER — the driver composes
# `StepExecutionContext.hitl_placements` from `manifest_entry.hitl_placements`
# at the per-step dispatch site so the runtime wrap-time HITL gate composer
# (runtime §14.8.2 step 1) fires per-step in production. Tested on both
# construction mechanisms: SINGLE_THREADED_LINEAR (direct construction) and
# DECENTRALIZED_HANDOFF (stage context inherits via compose_branch_child_context's
# model_copy — the mechanism shared by every branch-based topology).
# ---------------------------------------------------------------------------


class _StepContextCapturingDispatcher:
    """Records the `step_context` each dispatched step receives."""

    def __init__(self) -> None:
        self.contexts: list[StepExecutionContext] = []

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> dict[str, Any]:
        self.contexts.append(step_context)
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


class _StepContextByIdDispatcher:
    """Records the `step_context` each dispatched step received, keyed by step_id
    (so a per-step fold can be asserted per step)."""

    def __init__(self) -> None:
        self.by_id: dict[str, StepExecutionContext] = {}

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> dict[str, Any]:
        self.by_id[str(step.step_id)] = step_context
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


def _manifest_with_placements(
    placements: tuple[HITLPlacement, ...],
    *,
    topology_pattern: TopologyPattern = TopologyPattern.SINGLE_THREADED_LINEAR,
) -> WorkflowManifestEntry:
    return _manifest(topology_pattern=topology_pattern).model_copy(
        update={"hitl_placements": placements}
    )


@pytest.mark.parametrize(
    "topology_pattern",
    [TopologyPattern.SINGLE_THREADED_LINEAR, TopologyPattern.DECENTRALIZED_HANDOFF],
)
def test_driver_surfaces_manifest_placements_onto_step_context(
    topology_pattern: TopologyPattern,
) -> None:
    """The dispatched step's `step_context.hitl_placements` carries the manifest's
    declared placements — on the linear path (direct construction) AND the
    decentralized-handoff path (inherited via compose_branch_child_context)."""
    placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION)
    manifest = _manifest_with_placements((placement,), topology_pattern=topology_pattern)
    ctx, _ledger, _emitter = _ctx()
    dispatcher = _StepContextCapturingDispatcher()
    result = execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert result.status is RunStatus.SUCCESS
    assert dispatcher.contexts, "the step was dispatched"
    for sc in dispatcher.contexts:
        assert sc.hitl_placements == (placement,)


@pytest.mark.parametrize(
    "topology_pattern",
    [TopologyPattern.SINGLE_THREADED_LINEAR, TopologyPattern.DECENTRALIZED_HANDOFF],
)
def test_driver_empty_manifest_placements_yields_empty_step_context(
    topology_pattern: TopologyPattern,
) -> None:
    """Negative control: a manifest with NO placements → `step_context` carries
    the `()` default → the composer short-circuits (byte-identical to pre-arc)."""
    manifest = _manifest_with_placements((), topology_pattern=topology_pattern)
    ctx, _ledger, _emitter = _ctx()
    dispatcher = _StepContextCapturingDispatcher()
    result = execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert result.status is RunStatus.SUCCESS
    assert dispatcher.contexts
    for sc in dispatcher.contexts:
        assert sc.hitl_placements == ()


# ---------------------------------------------------------------------------
# R-FS-1 B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — the
# singular per-step `StepOverride.hitl_placement` override folds onto the
# workflow `hitl_placements` tuple by union-by-`position` (tune-not-remove,
# monotone). The previously-dead `StepEffectiveBinding.hitl_placement` becomes
# load-bearing. Helper unit tests + by-execution on the linear path.
# ---------------------------------------------------------------------------


def test_fold_none_override_returns_workflow_tuple_verbatim() -> None:
    """No per-step override (`None`) → the workflow tuple verbatim (byte-identical
    to the v1.41 producer-only arc)."""
    wf = (
        HITLPlacement(position=HITLPlacementKind.PRE_ACTION),
        HITLPlacement(position=HITLPlacementKind.SUB_AGENT_BOUNDARY),
    )
    assert fold_step_hitl_placements(wf, None) == wf
    assert fold_step_hitl_placements((), None) == ()


def test_fold_new_position_is_appended() -> None:
    """An override of a `position` ABSENT from the workflow tuple is APPENDED
    (ADD a gate position at this step)."""
    wf = (HITLPlacement(position=HITLPlacementKind.PRE_ACTION),)
    override = HITLPlacement(position=HITLPlacementKind.SUB_AGENT_BOUNDARY)
    folded = fold_step_hitl_placements(wf, override)
    assert folded == (*wf, override)
    # monotone: every workflow position survives + the new one is present.
    assert {p.position for p in folded} == {
        HITLPlacementKind.PRE_ACTION,
        HITLPlacementKind.SUB_AGENT_BOUNDARY,
    }


def test_fold_same_position_workflow_wins_no_loosening() -> None:
    """ADD-only: an override of a `position` ALREADY PRESENT in the workflow tuple
    is a NO-OP — the workflow placement wins, unchanged. A replace/tune could
    LOOSEN the §17.1 floor at the attribute level (e.g. a `tool_filter` narrowing
    leaves other tools ungated; a weaker `cascade_policy`/`timeout`), so the fold
    must NOT replace — that is the operator-gated B-HITL-PLACEMENT-PER-STEP-LOOSEN
    arc. (advisor decorrelated catch: position-level monotonicity does not imply
    attribute-level monotonicity; replace-on-collision was a silent loosening.)"""
    wf = (
        HITLPlacement(position=HITLPlacementKind.PRE_ACTION, timeout=30_000),
        HITLPlacement(position=HITLPlacementKind.SUB_AGENT_BOUNDARY),
    )
    # An override that would NARROW the gate (tool_filter) — must be ignored.
    override = HITLPlacement(
        position=HITLPlacementKind.PRE_ACTION, timeout=5, tool_filter=("git_push",)
    )
    folded = fold_step_hitl_placements(wf, override)
    # The workflow placement is preserved VERBATIM (override ignored, no loosening).
    assert folded == wf
    assert folded[0].timeout == 30_000
    assert folded[0].tool_filter is None


def test_per_step_placement_override_folds_onto_linear_step_context() -> None:
    """By-execution through the real `execute_workflow`: a per-step
    `StepOverride.hitl_placement` of a NEW position on step-0 folds (union) onto
    the dispatched `step_context.hitl_placements`; step-1 (no override) carries
    only the workflow tuple — proving the fold is per-step-scoped, not leaked."""
    workflow_placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION)
    override_placement = HITLPlacement(position=HITLPlacementKind.SUB_AGENT_BOUNDARY)
    manifest = _manifest_with_placements((workflow_placement,)).model_copy(
        update={
            "per_step_overrides": {
                StepID("step-0"): StepOverride(
                    step_id=StepID("step-0"), hitl_placement=override_placement
                )
            }
        }
    )
    ctx, _ledger, _emitter = _ctx()
    dispatcher = _StepContextByIdDispatcher()
    result = execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-fold-linear",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    assert result.status is RunStatus.SUCCESS
    # step-0: the workflow placement UNION the per-step override (both positions).
    assert dispatcher.by_id["step-0"].hitl_placements == (
        workflow_placement,
        override_placement,
    )
    # step-1: no override → the workflow tuple verbatim (not leaked from step-0).
    assert dispatcher.by_id["step-1"].hitl_placements == (workflow_placement,)


def test_per_step_placement_override_same_position_is_noop_on_linear_path() -> None:
    """By-execution: a per-step override of a position the workflow ALREADY declares
    is a NO-OP (ADD-only — the workflow placement wins, no attribute loosening). A
    tune/replace is the operator-gated B-HITL-PLACEMENT-PER-STEP-LOOSEN arc."""
    workflow_placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION, timeout=30_000)
    # An override that would narrow the gate to one tool — must be ignored.
    override_placement = HITLPlacement(
        position=HITLPlacementKind.PRE_ACTION, timeout=5, tool_filter=("git_push",)
    )
    manifest = _manifest_with_placements((workflow_placement,)).model_copy(
        update={
            "per_step_overrides": {
                StepID("step-0"): StepOverride(
                    step_id=StepID("step-0"), hitl_placement=override_placement
                )
            }
        }
    )
    ctx, _ledger, _emitter = _ctx()
    dispatcher = _StepContextByIdDispatcher()
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0), _step(1)],
        run_id="run-fold-noop",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, dispatcher)),
    )
    # step-0: the workflow placement preserved verbatim (override did not loosen it).
    assert dispatcher.by_id["step-0"].hitl_placements == (workflow_placement,)
    assert dispatcher.by_id["step-0"].hitl_placements[0].timeout == 30_000
    assert dispatcher.by_id["step-0"].hitl_placements[0].tool_filter is None
    assert dispatcher.by_id["step-1"].hitl_placements == (workflow_placement,)


# ---------------------------------------------------------------------------
# B-HITL-WRAP-FAIL-CLASS-SURFACING — the driver surfaces a runtime exception's
# `rt_fail_class` marker as its canonical RT-FAIL-* code in `fail_class` (the
# wrap-time HITL gate's terminal exceptions carry it), without harness-cp
# importing the harness-runtime exception TYPES. A non-marker exception falls
# back to the Python class name (byte-identical to before).
# ---------------------------------------------------------------------------


class _MarkerError(Exception):
    """Stand-in for a runtime wrap-time HITL exception (harness-cp cannot import
    the real `HITLGateRejectedError`); carries the same `rt_fail_class` marker."""

    rt_fail_class = "RT-FAIL-HITL-GATE-REJECTED"


class _RaisingDispatcher:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def dispatch(
        self, binding: StepEffectiveBinding, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = (binding, step, step_context)
        raise self._exc


def test_step_fail_class_helper_reads_marker_else_type_name() -> None:
    """Unit: `_step_fail_class` surfaces the `rt_fail_class` marker code when present
    (the precise RT-FAIL-* code), else the Python class name."""
    from harness_cp.workflow_driver import _step_fail_class

    assert _step_fail_class("step-failure", _MarkerError("rejected")).startswith(
        "step-failure: RT-FAIL-HITL-GATE-REJECTED: "
    )
    assert _step_fail_class("step-failure", ValueError("boom")).startswith(
        "step-failure: ValueError: "
    )


def test_marker_exception_surfaces_rt_fail_code_at_dispatch() -> None:
    """Integration: a dispatcher raising a marker-carrying exception → the
    RunResult.fail_class carries the canonical RT-FAIL-* code, NOT the class name."""
    ctx, _, _ = _ctx()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _RaisingDispatcher(_MarkerError("rej")))),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "step-failure: RT-FAIL-HITL-GATE-REJECTED" in result.fail_class
    assert "_MarkerError" not in result.fail_class  # the code, not the class name


def test_non_marker_exception_falls_back_to_type_name_at_dispatch() -> None:
    """Negative control: a plain (no-marker) exception still surfaces the Python
    class name — byte-identical to pre-arc behavior."""
    ctx, _, _ = _ctx()
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _RaisingDispatcher(ValueError("boom")))),
    )
    assert result.status is RunStatus.FAILED
    assert result.fail_class is not None
    assert "step-failure: ValueError: boom" in result.fail_class
