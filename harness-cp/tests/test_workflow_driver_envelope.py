"""Tests for U-OD-35 — workflow.envelope OTel tracer integration at driver entry.

Acceptance-criterion coverage (per Implementation_Plan_Operational_Discipline_v2_14.md):
  #1 workflow.envelope span opens at workflow_driver entry (post-drain-check)
      → test_envelope_opens_on_normal_execution
      → test_envelope_does_not_open_when_drained_at_entry
  #2 Single envelope per workflow (per §C-OD-25.4 invariant 1)
      → test_single_envelope_per_workflow
  #3 Closes on normal SUCCESS / FAILED / DRAINED + on exception via OTel exception-status
      → test_envelope_status_ok_on_success
      → test_envelope_status_error_on_failed
      → test_envelope_closes_on_drained_mid_execution
  #4 Head=1.0 always-sampled (per §C-OD-25.3) — verified at OD-axis sampler config;
     at driver level: envelope span is always created when entered.
      → covered by test_envelope_opens_on_normal_execution
  #5 Integration test: root span observable at OTel collector with
     parent_span_id=null + status=OK on SUCCESS / status=ERROR on FAILED;
     subsequent child spans nest under the envelope.
      → test_envelope_is_root_span
      → test_envelope_nests_child_spans

Spec authority: OD spec v1.8 §C-OD-25.1 + §25.2 + §25.3 + §25.4 + §25.5.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
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
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

# ---------------------------------------------------------------------------
# Fixtures — production TracerProvider + in-memory exporter for span assertions.
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
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-envelope-driver")


@pytest.fixture
def exporter_and_provider() -> tuple[InMemorySpanExporter, TracerProvider]:
    """Per-test isolated TracerProvider + in-memory exporter.

    Per-test isolation: a fresh TracerProvider is created per test so spans
    from prior tests do not leak into this test's collector. The provider is
    NOT registered globally (register_globally=False semantics) — we pass it
    via the DriverContext directly.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def _manifest(
    *,
    engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
    workflow_id: str = "wf-envelope",
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=engine_class,
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


class _FakeLedgerReader:
    def read_by_idempotency_key(self, idempotency_key: Any, bounded_window: Any) -> Any:
        class _Result:
            entries: tuple[object, ...] = ()
            truncated = False
            next_position = None

        return _Result()


class _FakeCtx:
    def __init__(
        self,
        *,
        tracer_provider: TracerProvider,
        drained: bool = False,
        validator_framework: object | None = None,
    ) -> None:
        self.ledger_writer = _FakeLedger()
        self.ledger_reader = _FakeLedgerReader()
        self.lifecycle_emitter = _FakeEmitter()
        self.drained_flag = asyncio.Event()
        if drained:
            self.drained_flag.set()
        self.tracer_provider = tracer_provider
        self.validator_framework = validator_framework
        # U-RT-87 (v2.20) — pause_resume_protocol + pause_requested_flag per
        # runtime spec v1.21 §4 + §14.14.3 DriverContext Protocol extension.
        self.pause_resume_protocol: object | None = None
        self.pause_requested_flag = asyncio.Event()
        # tenant_id binding lift — envelope tests run single-tenant.
        self.tenant_id: str | None = None


class _EchoDispatcher:
    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        return {"step_id": str(step.step_id), "echoed_payload": dict(step.step_payload)}


class _RaisingDispatcher:
    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        raise RuntimeError("dispatcher failure for envelope FAILED test")


class _SingleKindRegistry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        if step_kind != StepKind.INFERENCE_STEP:
            raise StepKindDispatcherNotBoundError(step_kind)
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _SingleKindRegistry(dispatcher))


# ---------------------------------------------------------------------------
# AC #1 — Envelope opens at driver entry (post-drain-check)
# ---------------------------------------------------------------------------


def test_envelope_opens_on_normal_execution(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    spans = exporter.get_finished_spans()
    envelopes = [s for s in spans if s.name == "workflow.envelope"]
    assert len(envelopes) == 1, f"expected 1 envelope, got {len(envelopes)}"


def test_envelope_does_not_open_when_drained_at_entry(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """C-OD-25 AC #1 — envelope opens POST-drain-check. Drain-at-entry returns
    DRAINED without ever opening the envelope (no observable workflow ran)."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider, drained=True)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status is RunStatus.DRAINED
    spans = exporter.get_finished_spans()
    envelopes = [s for s in spans if s.name == "workflow.envelope"]
    assert envelopes == [], "envelope must not open on drain-at-entry"


# ---------------------------------------------------------------------------
# AC #2 — Single envelope per workflow
# ---------------------------------------------------------------------------


def test_single_envelope_per_workflow(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    spans = exporter.get_finished_spans()
    envelopes = [s for s in spans if s.name == "workflow.envelope"]
    assert len(envelopes) == 1


# ---------------------------------------------------------------------------
# AC #3 — Status on normal close (SUCCESS / FAILED / DRAINED)
# ---------------------------------------------------------------------------


def test_envelope_status_ok_on_success(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """SUCCESS close leaves status UNSET per OTel default (UNSET is OK-equivalent
    for downstream tooling; explicit OK marking is not required by §C-OD-25.4)."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    envelope = _single_envelope(exporter)
    assert envelope.status.status_code in {StatusCode.UNSET, StatusCode.OK}


def test_envelope_status_error_on_failed(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _RaisingDispatcher())),
    )
    assert result.status is RunStatus.FAILED
    envelope = _single_envelope(exporter)
    assert envelope.status.status_code is StatusCode.ERROR
    # fail_class propagates into status description (load-bearing for triage).
    assert envelope.status.description is not None
    assert "step-failure" in envelope.status.description


def test_envelope_closes_on_drained_mid_execution(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """Drain set BEFORE entry returns DRAINED with no envelope (covered above).
    Drain set MID-execution: envelope opened, then per-step drain check returns
    DRAINED — envelope closes deterministically."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)

    class _DrainMidDispatcher:
        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            ctx.drained_flag.set()
            return {"step_id": str(step.step_id), "echoed_payload": {}}

    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _DrainMidDispatcher())),
    )
    assert result.status is RunStatus.DRAINED
    envelope = _single_envelope(exporter)
    # DRAINED close leaves status UNSET (DRAINED is not a fail per §C-OD-25.5
    # default — workflow.fail_class null on DRAINED outcome).
    assert envelope.status.status_code in {StatusCode.UNSET, StatusCode.OK}


# ---------------------------------------------------------------------------
# AC #5 — Root span discipline + child-span nesting
# ---------------------------------------------------------------------------


def test_envelope_is_root_span(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    envelope = _single_envelope(exporter)
    assert envelope.parent is None, "workflow.envelope must be a root span"
    assert envelope.context.span_id != 0


def test_envelope_nests_child_spans(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """Verifies OTel parent-context propagation per §C-OD-25.4 invariant 3 —
    child spans opened inside execute_workflow must carry the envelope as
    their parent (no manual parent-id management; trust OTel context)."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test.envelope.child")

    class _ChildSpanDispatcher:
        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            with tracer.start_as_current_span("child.span"):
                pass
            return {"step_id": str(step.step_id), "echoed_payload": {}}

    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-1",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _ChildSpanDispatcher())),
    )
    spans = exporter.get_finished_spans()
    envelope = _single_envelope(exporter)
    children = [s for s in spans if s.name == "child.span"]
    assert len(children) == 1
    assert children[0].parent is not None
    assert children[0].parent.span_id == envelope.context.span_id


# ---------------------------------------------------------------------------
# U-OD-36 — 12-attribute set per §C-OD-25.1
# ---------------------------------------------------------------------------


_EXPECTED_OPEN_TIME_ATTRS = {
    "workflow.id",
    "workflow.run_id",
    "workflow.idempotency_key",
    "workflow.entry_version",
    "workflow.topology_pattern",
    "workflow.engine_class",
    "workflow.workload_class",
    "workflow.persona_tier",
}


def test_envelope_open_time_attributes_populated(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #1 — All 8 open-time attributes present + AC #4 — enums serialize via .value."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    manifest = _manifest(workflow_id="wf-attr")
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0)],
        run_id="run-attr",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    envelope = _single_envelope(exporter)
    attrs = dict(envelope.attributes or {})

    # All open-time attribute keys present
    missing = _EXPECTED_OPEN_TIME_ATTRS - attrs.keys()
    assert not missing, f"missing open-time attributes: {missing}"

    # Identity attributes carry expected values
    assert attrs["workflow.id"] == "wf-attr"
    assert attrs["workflow.run_id"] == "run-attr"
    assert isinstance(attrs["workflow.idempotency_key"], str)
    assert len(attrs["workflow.idempotency_key"]) == 64  # sha256 hex
    assert isinstance(attrs["workflow.entry_version"], int)

    # AC #4 — enums serialize via .value (string form)
    assert attrs["workflow.topology_pattern"] == TopologyPattern.SINGLE_THREADED_LINEAR.value
    assert attrs["workflow.engine_class"] == EngineClass.PURE_PATTERN_NO_ENGINE.value
    assert attrs["workflow.workload_class"] == WorkloadClass.PIPELINE_AUTOMATION.value
    assert attrs["workflow.persona_tier"] == PersonaTier.TEAM_BINDING.value


def test_envelope_close_attributes_on_success(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #1 — outcome+step_count populated on SUCCESS; terminal_step_index omitted;
    fail_class omitted."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-success",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    envelope = _single_envelope(exporter)
    attrs = dict(envelope.attributes or {})

    assert attrs["workflow.outcome"] == RunStatus.SUCCESS.value
    assert attrs["workflow.step_count"] == 3
    assert "workflow.fail_class" not in attrs
    assert "workflow.terminal_step_index" not in attrs


def test_envelope_close_attributes_on_failed(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #1 — outcome+fail_class+terminal_step_index+step_count on FAILED;
    step_count reflects fully-completed steps (failing step does NOT count)."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)

    class _RaiseAtIndexDispatcher:
        def __init__(self, fail_at: int) -> None:
            self.dispatched = 0
            self._fail_at = fail_at

        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            if self.dispatched == self._fail_at:
                raise RuntimeError("simulated failure at index 2")
            self.dispatched += 1
            return {"step_id": str(step.step_id), "echoed_payload": {}}

    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2), _step(3)],
        run_id="run-fail",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _RaiseAtIndexDispatcher(fail_at=2))),
    )
    envelope = _single_envelope(exporter)
    attrs = dict(envelope.attributes or {})

    assert attrs["workflow.outcome"] == RunStatus.FAILED.value
    assert "workflow.fail_class" in attrs
    assert "step-failure" in str(attrs["workflow.fail_class"])
    assert attrs["workflow.terminal_step_index"] == 2  # failed step index
    assert attrs["workflow.step_count"] == 2  # steps 0 + 1 completed; 2 raised


def test_envelope_close_attributes_on_drained_mid_execution(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #2 — fail_class null on DRAINED (omitted attribute); outcome=DRAINED;
    step_count reflects completed steps."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)

    class _DrainAfterFirstDispatcher:
        def __init__(self) -> None:
            self.calls = 0

        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls >= 1:
                ctx.drained_flag.set()
            return {"step_id": str(step.step_id), "echoed_payload": {}}

    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-drain",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _DrainAfterFirstDispatcher())),
    )
    envelope = _single_envelope(exporter)
    attrs = dict(envelope.attributes or {})

    assert attrs["workflow.outcome"] == RunStatus.DRAINED.value
    assert "workflow.fail_class" not in attrs  # null-on-DRAINED per §25.5
    # post-step drain at step 0: terminal_step_index = 0; step_count = 1
    assert attrs["workflow.terminal_step_index"] == 0
    assert attrs["workflow.step_count"] == 1


def test_envelope_step_count_zero_when_no_steps_complete_before_drain(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """Pre-step drain at first step (mid-envelope, not at-entry): step_count=0."""
    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)

    class _SetDrainBeforeDispatchDispatcher:
        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            # Should never be called — drain is set before the dispatch.
            raise AssertionError("should not dispatch")

    # Set drain INSIDE the workflow (not at entry) via a custom emitter that
    # trips drain on workflow.start emission.
    class _DrainOnStartEmitter:
        def __init__(self) -> None:
            self.emits: list[WorkflowEventClass] = []

        def emit(self, event_class: WorkflowEventClass) -> None:
            self.emits.append(event_class)
            if event_class is WorkflowEventClass.WORKFLOW_START:
                ctx.drained_flag.set()

    ctx.lifecycle_emitter = _DrainOnStartEmitter()
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-drain-pre",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _SetDrainBeforeDispatchDispatcher())),
    )
    envelope = _single_envelope(exporter)
    attrs = dict(envelope.attributes or {})

    assert attrs["workflow.outcome"] == RunStatus.DRAINED.value
    assert attrs["workflow.step_count"] == 0
    # terminal_step_index is None at pre-step-0 drain → omitted attribute
    assert "workflow.terminal_step_index" not in attrs


# ---------------------------------------------------------------------------
# U-OD-37 — Exception handling + resumption fresh-envelope
# ---------------------------------------------------------------------------


def test_envelope_records_exception_on_validation_failure(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #1 — Exception inside envelope (here: a not-yet-materialized ENGINE class
    raises EngineClassNotYetMaterializedError) records the exception event and sets
    span status to ERROR. OTel auto-discipline + the C-RT-17 validation error class
    combined. All 5 engine classes are materialized at HEAD (RECONCILER_LOOP was the
    last, U-CP-96/E-impl-3a), so the gate is preserved-but-unreachable for valid
    input; this test patches _IN_SCOPE to exclude RECONCILER_LOOP to exercise the
    preserved gate (was DECENTRALIZED_HANDOFF via the topology gate, then the engine
    gate; both materialization gates are now closed for valid input)."""
    exporter, provider = exporter_and_provider
    monkeypatch.setattr(
        "harness_cp.workflow_driver._IN_SCOPE_ENGINE_CLASSES",
        frozenset(EngineClass) - {EngineClass.RECONCILER_LOOP},
    )
    ctx = _FakeCtx(tracer_provider=provider)
    manifest = WorkflowManifestEntry(
        workflow_id="wf-exc",
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.RECONCILER_LOOP,  # patched out of _IN_SCOPE → raises the preserved gate
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )
    with pytest.raises(Exception):
        execute_workflow(
            manifest_entry=manifest,
            steps=[_step(0)],
            run_id="run-exc",
            ctx=cast(DriverContext, ctx),
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
        )
    envelope = _single_envelope(exporter)
    # OTel default discipline closes span with ERROR + exception event.
    assert envelope.status.status_code is StatusCode.ERROR
    # Exception event recorded with the typed error class name.
    exc_events = [e for e in envelope.events if e.name == "exception"]
    assert len(exc_events) == 1
    attrs = dict(exc_events[0].attributes or {})
    assert attrs.get("exception.type") in {
        "EngineClassNotYetMaterializedError",
        "harness_cp.workflow_driver_errors.EngineClassNotYetMaterializedError",
    }


def test_envelope_resumption_opens_fresh_envelope(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #2 — Resumption (per U-CP-56) opens a FRESH envelope per §C-OD-25.4
    invariant 1. Two sequential execute_workflow calls with the same workflow
    identity produce two distinct envelope spans (distinct span_ids)."""
    exporter, provider = exporter_and_provider
    ctx1 = _FakeCtx(tracer_provider=provider)
    ctx2 = _FakeCtx(tracer_provider=provider)
    manifest = _manifest(workflow_id="wf-resume")
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0)],
        run_id="run-resume",
        ctx=cast(DriverContext, ctx1),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    execute_workflow(
        manifest_entry=manifest,
        steps=[_step(0)],
        run_id="run-resume",
        ctx=cast(DriverContext, ctx2),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    envelopes = [s for s in exporter.get_finished_spans() if s.name == "workflow.envelope"]
    assert len(envelopes) == 2
    assert envelopes[0].context.span_id != envelopes[1].context.span_id
    # Both envelopes carry the same workflow.id but distinct identity per-run.
    attrs1 = dict(envelopes[0].attributes or {})
    attrs2 = dict(envelopes[1].attributes or {})
    assert attrs1["workflow.id"] == attrs2["workflow.id"] == "wf-resume"


def test_envelope_end_time_reflects_workflow_termination(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #4 — Span.end_time_ns reflects actual workflow termination time
    (envelope closes after body returns, not at start)."""
    import time

    exporter, provider = exporter_and_provider
    ctx = _FakeCtx(tracer_provider=provider)

    class _SleepDispatcher:
        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            time.sleep(0.01)  # 10ms; observable above timing noise
            return {"step_id": str(step.step_id), "echoed_payload": {}}

    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0)],
        run_id="run-time",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _SleepDispatcher())),
    )
    envelope = _single_envelope(exporter)
    # Envelope duration must exceed the dispatcher sleep (>= 10ms = 10_000_000ns).
    duration_ns = envelope.end_time - envelope.start_time
    assert duration_ns >= 10_000_000, (
        f"envelope duration {duration_ns}ns should reflect dispatcher work"
    )


def test_envelope_child_span_nests_under_envelope_for_each_step(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #3 — Span context propagation: child spans opened by dispatchers
    across multiple steps all nest under the SAME envelope (single envelope
    per workflow per §25.4 invariant 1)."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test.envelope.multi-child")

    class _MultiChildDispatcher:
        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            with tracer.start_as_current_span(f"step.{step.step_id}.body"):
                pass
            return {"step_id": str(step.step_id), "echoed_payload": {}}

    ctx = _FakeCtx(tracer_provider=provider)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        run_id="run-multi",
        ctx=cast(DriverContext, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _MultiChildDispatcher())),
    )
    envelope = _single_envelope(exporter)
    children = [
        s
        for s in exporter.get_finished_spans()
        if s.name.startswith("step.") and s.name.endswith(".body")
    ]
    assert len(children) == 3
    for child in children:
        assert child.parent is not None
        assert child.parent.span_id == envelope.context.span_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _single_envelope(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.envelope"]
    assert len(spans) == 1, (
        f"expected 1 envelope, got {len(spans)}: {[s.name for s in exporter.get_finished_spans()]}"
    )
    return spans[0]
