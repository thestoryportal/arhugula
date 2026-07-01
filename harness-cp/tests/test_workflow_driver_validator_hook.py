"""Tests for U-CP-61 — validator.* span emission at workflow_driver post-dispatch hook.

ACs from CP plan v2.16 §1 U-CP-61:
  AC #1 Hook fires on every step (per Decision 2.D3 — opt-out via no-op validator;
        driver-level opt-out via validator_framework=None)
  AC #2 `validator.evaluate` outer span emits with 3 canonical attributes per OD spec
        v1.8 §C-OD-29.1 row 1-3
  AC #3 `validator.fail` event attributes emit when outcome != PASS (4-attr set)
  AC #4 `validator.escalation.parent_hitl_span_id` populated when outcome=ESCALATE
  AC #5 PASS proceeds; PERMANENT_FAIL returns FAILED with CP-FAIL-VALIDATOR-PERMANENT
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
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
from harness_cp.validator_framework import (
    ConcreteValidatorFramework,
    SyncValidatorFrameworkFacade,
    materialize_sync_validator_framework_facade,
)
from harness_cp.validator_framework_types import (
    ValidatorFailClass,
    ValidatorOutcome,
    ValidatorResult,
)
from harness_cp.workflow_driver import (
    StepDispatcher,
    StepDispatcherRegistry,
    execute_workflow,
)
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-validator-hook")


# ----------------------------------------------------------------------------
# Inline fixtures (mirrors test_workflow_driver_envelope.py minimal substrate)
# ----------------------------------------------------------------------------


def _manifest(workflow_id: str = "wf-validator-hook") -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


class _FakeLedger:
    actor: Actor

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> Any:
        self.appends.append((payload, write_key))

        class _Result:
            entry_hash = "0" * 64
            next_position = None

        return _Result()


class _FakeLedgerReader:
    def find_by_workflow_step(self, workflow_id: str, step_index: int) -> object | None:
        return None


class _FakeEmitter:
    def __init__(self) -> None:
        self.emitted: list[WorkflowEventClass] = []

    def emit(self, event_class: WorkflowEventClass) -> None:
        self.emitted.append(event_class)


class _FakeCtx:
    def __init__(
        self,
        *,
        tracer_provider: object,
        validator_framework: object | None = None,
    ) -> None:
        self.ledger_writer = _FakeLedger()
        self.ledger_reader = _FakeLedgerReader()
        self.lifecycle_emitter = _FakeEmitter()
        self.drained_flag = asyncio.Event()
        self.tracer_provider = tracer_provider
        self.validator_framework = validator_framework
        # U-RT-87 (v2.20) — pause_resume_protocol + pause_requested_flag per
        # runtime spec v1.21 §4 + §14.14.3 DriverContext Protocol extension.
        self.pause_resume_protocol: object | None = None
        self.pause_requested_flag = asyncio.Event()
        # tenant_id binding lift — validator-hook tests run single-tenant.
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


class _SingleKindRegistry:
    def __init__(self, dispatcher: StepDispatcher) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        return self._dispatcher


def _registry(dispatcher: StepDispatcher) -> StepDispatcherRegistry:
    return cast(StepDispatcherRegistry, _SingleKindRegistry(dispatcher))


# ----------------------------------------------------------------------------
# Validator test doubles + facade factory
# ----------------------------------------------------------------------------


class _FixedValidator:
    def __init__(self, result: ValidatorResult) -> None:
        self._result = result

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: Any,
    ) -> ValidatorResult:
        return self._result


def _make_facade(
    result: ValidatorResult,
    step_ids: list[StepID],
) -> SyncValidatorFrameworkFacade:
    """Construct a SyncValidatorFrameworkFacade backed by a background loop.

    The facade requires construction on a running event loop. We spin up a
    daemon-thread loop, materialize the facade on it, and return the facade.
    The background loop persists for the lifetime of the test process (daemon
    thread; reaped at exit).
    """
    loop_ready = threading.Event()
    container: dict[str, Any] = {}

    def _run_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _on_loop() -> None:
            inner = ConcreteValidatorFramework(
                validator_registry={sid: _FixedValidator(result) for sid in step_ids},
            )
            facade = materialize_sync_validator_framework_facade(
                inner=inner,
                result_timeout_seconds=10.0,
            )
            container["facade"] = facade
            loop_ready.set()
            # Block the loop alive for the test process lifetime.
            await asyncio.Event().wait()

        loop.run_until_complete(_on_loop())

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    loop_ready.wait(timeout=5.0)
    return cast(SyncValidatorFrameworkFacade, container["facade"])


# ----------------------------------------------------------------------------
# AC #1 — Hook fires only when validator_framework is bound
# ----------------------------------------------------------------------------


def test_hook_skipped_when_validator_framework_is_none() -> None:
    """AC #1 — validator_framework=None: hook is no-op; PASS-path workflow."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    ctx = _FakeCtx(tracer_provider=provider)
    assert ctx.validator_framework is None

    step = WorkflowStep(
        step_id=StepID("step-no-validator"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"echo": "ok"},
    )
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[step],
        run_id="run-no-validator",
        ctx=cast(Any, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )
    assert result.status.value == "success"

    span_names = [s.name for s in exporter.get_finished_spans()]
    assert "validator.evaluate" not in span_names


def test_hook_fires_when_validator_framework_bound() -> None:
    """AC #1 + AC #2 — validator.evaluate span emitted with envelope attrs."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    step = WorkflowStep(
        step_id=StepID("step-pass"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"echo": "ok"},
    )
    facade = _make_facade(
        ValidatorResult(outcome=ValidatorOutcome.PASS),
        [step.step_id],
    )

    ctx = _FakeCtx(tracer_provider=provider, validator_framework=facade)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[step],
        run_id="run-pass",
        ctx=cast(Any, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )

    assert result.status.value == "success"
    span_names = [s.name for s in exporter.get_finished_spans()]
    assert "validator.evaluate" in span_names

    eval_span = next(s for s in exporter.get_finished_spans() if s.name == "validator.evaluate")
    attrs = dict(eval_span.attributes or {})
    # AC #2 — 3 canonical envelope attributes
    assert attrs["step.id"] == "step-pass"
    assert attrs["validator.outcome"] == "pass"
    assert attrs["validator.burden_count_cumulative"] == 0


# ----------------------------------------------------------------------------
# AC #3 — validator.fail attributes on non-PASS outcome
# ----------------------------------------------------------------------------


def test_hook_emits_fail_attributes_on_non_pass() -> None:
    """AC #3 — non-PASS outcome emits validator.fail.* attributes (4-attr set)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    step = WorkflowStep(
        step_id=StepID("step-escalate"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"echo": "ok"},
    )
    facade = _make_facade(
        ValidatorResult(
            outcome=ValidatorOutcome.ESCALATE,
            fail_class=ValidatorFailClass.SAFETY_POLICY,
            fail_detail_hash="a" * 64,
        ),
        [step.step_id],
    )

    ctx = _FakeCtx(tracer_provider=provider, validator_framework=facade)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[step],
        run_id="run-escalate",
        ctx=cast(Any, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )

    eval_span = next(s for s in exporter.get_finished_spans() if s.name == "validator.evaluate")
    attrs = dict(eval_span.attributes or {})

    assert attrs["validator.fail.next_action"] == "escalate_hitl"
    assert attrs["validator.fail.escalation_owed"] is True
    assert attrs["validator.fail.class"] == "safety_policy"
    assert attrs["validator.fail.detail_hash"] == "a" * 64


# ----------------------------------------------------------------------------
# AC #4 — ESCALATE populates validator.escalation.parent_hitl_span_id
# ----------------------------------------------------------------------------


def test_hook_populates_parent_hitl_span_id_on_escalate() -> None:
    """AC #4 — outcome=ESCALATE adds validator.escalation.parent_hitl_span_id."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    step = WorkflowStep(
        step_id=StepID("step-escalate-link"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"echo": "ok"},
    )
    facade = _make_facade(
        ValidatorResult(
            outcome=ValidatorOutcome.ESCALATE,
            fail_class=ValidatorFailClass.EXTERNAL_REJECTION,
            fail_detail_hash="b" * 64,
        ),
        [step.step_id],
    )

    ctx = _FakeCtx(tracer_provider=provider, validator_framework=facade)
    execute_workflow(
        manifest_entry=_manifest(),
        steps=[step],
        run_id="run-escalate-link",
        ctx=cast(Any, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )

    eval_span = next(s for s in exporter.get_finished_spans() if s.name == "validator.evaluate")
    attrs = dict(eval_span.attributes or {})
    parent_span_id = attrs.get("validator.escalation.parent_hitl_span_id")
    assert parent_span_id is not None
    assert isinstance(parent_span_id, str)
    assert len(parent_span_id) == 16  # 16-hex span_id format
    assert attrs["validator.escalation.fail_class"] == "external_rejection"


# ----------------------------------------------------------------------------
# AC #5 — PASS proceeds; PERMANENT_FAIL returns FAILED
# ----------------------------------------------------------------------------


def test_pass_outcome_proceeds_to_ledger_append() -> None:
    """AC #5 — PASS outcome: workflow completes SUCCESS; ledger has the entry."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    step = WorkflowStep(
        step_id=StepID("step-pass-proceeds"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"echo": "ok"},
    )
    facade = _make_facade(
        ValidatorResult(outcome=ValidatorOutcome.PASS),
        [step.step_id],
    )

    ctx = _FakeCtx(tracer_provider=provider, validator_framework=facade)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[step],
        run_id="run-pass-proceeds",
        ctx=cast(Any, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )

    assert result.status.value == "success"
    assert len(ctx.ledger_writer.appends) >= 1  # type: ignore[attr-defined]


def test_permanent_fail_returns_failed_with_typed_fail_class() -> None:
    """AC #5 — PERMANENT_FAIL returns RunResult(FAILED) with CP-FAIL-VALIDATOR-PERMANENT."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    step = WorkflowStep(
        step_id=StepID("step-permanent-fail"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"echo": "ok"},
    )
    facade = _make_facade(
        ValidatorResult(
            outcome=ValidatorOutcome.PERMANENT_FAIL,
            fail_class=ValidatorFailClass.SAFETY_POLICY,
            fail_detail_hash="c" * 64,
        ),
        [step.step_id],
    )

    ctx = _FakeCtx(tracer_provider=provider, validator_framework=facade)
    result = execute_workflow(
        manifest_entry=_manifest(),
        steps=[step],
        run_id="run-permanent-fail",
        ctx=cast(Any, ctx),
        default_model_binding=_DEFAULT_BINDING,
        step_dispatchers=_registry(cast(StepDispatcher, _EchoDispatcher())),
    )

    assert result.status.value == "failed"
    assert result.fail_class is not None
    assert "CP-FAIL-VALIDATOR-PERMANENT" in result.fail_class
