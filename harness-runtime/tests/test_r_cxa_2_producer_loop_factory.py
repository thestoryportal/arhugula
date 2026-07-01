"""R-CXA-2 stage-5 producer-loop composition tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harness_core import PersonaTier
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.pause_resume_protocol import PauseReason, ResumeOutcomeKind
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.workflow_driver_types import StepKind
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import WriteResult, read_ledger
from harness_runtime.bootstrap.factories.r_cxa_2_producer_loop_factory import (
    materialize_r_cxa_2_producer_loop_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.ask_user_question_surface import AskUserQuestionResult
from harness_runtime.lifecycle.cp_is_wiring import materialize_cp_is_wiring_stage
from harness_runtime.lifecycle.hitl_placement import RuntimeHITLPlacementRegistry
from harness_runtime.lifecycle.hitl_tool_loop import (
    HITLToolLoopContext,
    ModelToolCall,
)
from harness_runtime.lifecycle.state_ledger import LedgerWriter, materialize_state_ledger
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

_ACTOR = ActorIdentity("r-cxa-2-stage-5")
_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)


def _pt_resolver() -> Identifier:
    return _PROCEDURAL_TIER_SNAPSHOT_FIXTURE


def _resolver_for(tmp_path: Path) -> PathResolver:
    config = PathBindingConfig(
        raw_entries=(
            {
                "path_class": PathClass.STATE_LEDGER,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(tmp_path / "state.jsonl"),
            },
        ),
    )
    return PathResolver(build_path_binding(config))


def _ledger_writer(tmp_path: Path) -> LedgerWriter:
    return materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime"),
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


class _AskSurface:
    def __init__(
        self,
        order: list[str],
        response: HITLResponse = HITLResponse.APPROVE,
    ) -> None:
        self.order = order
        self.response = response
        self.calls: list[tuple[str, tuple[HITLResponse, ...], float | None]] = []

    async def ask(
        self,
        prompt: str,
        options: tuple[HITLResponse, ...],
        timeout: float | None,
    ) -> AskUserQuestionResult:
        self.order.append("gate")
        self.calls.append((prompt, options, timeout))
        return AskUserQuestionResult(response=self.response, latency_ms=1.0)


class _ToolDispatcher:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[tuple[Any, Any, Any]] = []

    async def dispatch(self, binding: Any, step: Any, *, step_context: Any) -> dict[str, Any]:
        self.order.append("dispatch")
        self.calls.append((binding, step, step_context))
        return {"ok": True, "tool_args": dict(step.step_payload["tool_args"])}


def _context() -> HITLToolLoopContext:
    return HITLToolLoopContext(
        workflow_id="wf-1",
        step_id="step-1",
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        actor=_ACTOR,
    )


def _call(tool_call_id: str = "provider-call-1") -> ModelToolCall:
    return ModelToolCall(
        tool_call_id=tool_call_id,
        tool="search",
        server="mcp-main",
        arguments={"query": tool_call_id},
        provider="fixture-provider",
        model="fixture-model",
    )


def _post_tool_dispatcher_context(
    tmp_path: Path,
    order: list[str],
) -> tuple[_MutableHarnessContext, RuntimeConfig, _AskSurface, _ToolDispatcher]:
    config = _config(tmp_path)
    ledger_writer = _ledger_writer(tmp_path)
    ctx = _MutableHarnessContext()
    ctx.ledger_writer = ledger_writer
    ctx.cxa_stages["cp_is_wiring"] = materialize_cp_is_wiring_stage(
        config,
        ledger_writer,
        _pt_resolver,
    )
    ctx.hitl_registry = RuntimeHITLPlacementRegistry()
    ask_surface = _AskSurface(order)
    tool_dispatcher = _ToolDispatcher(order)
    ctx.ask_user_question_surface = ask_surface
    ctx.tool_dispatcher = tool_dispatcher
    return ctx, config, ask_surface, tool_dispatcher


def test_factory_binds_r_cxa_2_loops_to_context(tmp_path: Path) -> None:
    ctx, config, _ask_surface, _tool_dispatcher = _post_tool_dispatcher_context(tmp_path, [])

    stage = materialize_r_cxa_2_producer_loop_stage(ctx, config)

    assert ctx.hitl_tool_loop is stage.hitl_tool_loop
    assert ctx.engine_recovery_loop is stage.engine_recovery_loop


def test_bound_hitl_loop_emits_rewrite_before_tool_dispatch(tmp_path: Path) -> None:
    order: list[str] = []
    ctx, config, ask_surface, tool_dispatcher = _post_tool_dispatcher_context(tmp_path, order)
    stage = materialize_r_cxa_2_producer_loop_stage(ctx, config)

    results = asyncio.run(stage.hitl_tool_loop.run_tool_calls([_call()], _context()))

    assert results[0].rewrite_write_result is WriteResult.APPENDED
    assert results[0].dispatched is True
    assert order == ["gate", "dispatch"]
    assert len(ask_surface.calls) == 1
    binding, step, step_context = tool_dispatcher.calls[0]
    assert binding.step_id == "step-1:tool:provider-call-1"
    assert binding.model_binding.provider == "fixture-provider"
    assert step.step_kind is StepKind.TOOL_STEP
    assert step.step_payload == {
        "tool_id": "search",
        "tool_args": {"query": "provider-call-1"},
    }
    assert step_context.workflow_id == "wf-1"
    assert step_context.parent_idempotency_key.endswith(":provider-call-1")
    entries = read_ledger(stage.hitl_tool_loop.wiring.ledger_writer.handle)
    assert [entry.action_id for entry in entries] == ["cp.hitl-tool-call-rewriting"]


def test_bound_engine_loop_emits_pause_and_resume_entries(tmp_path: Path) -> None:
    ctx, config, _ask_surface, _tool_dispatcher = _post_tool_dispatcher_context(tmp_path, [])
    stage = materialize_r_cxa_2_producer_loop_stage(ctx, config)

    pause = asyncio.run(
        stage.engine_recovery_loop.capture_pause(
            engine_class=EngineClass.WAL_SEGMENT,
            workflow_id="wf-1",
            run_id="run-1",
            step_id="step-1",
            pause_reason=PauseReason.OPERATOR_INITIATED_PAUSE,
        )
    )
    resume = asyncio.run(
        stage.engine_recovery_loop.attempt_resume(
            engine_class=EngineClass.WAL_SEGMENT,
            workflow_id="missing-workflow",
            run_id="run-1",
            step_id="step-1",
            resume_event_id="resume-evt-1",
            resume_attempt_count=1,
            resume_at="2026-06-08T12:00:00Z",
        )
    )

    assert pause.write_result is WriteResult.APPENDED
    assert resume.write_result is WriteResult.APPENDED
    assert resume.resume_outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED
    entries = read_ledger(stage.engine_recovery_loop.wiring.ledger_writer.handle)
    assert [entry.action_id for entry in entries] == [
        "cp.pause-captured",
        "cp.resume-attempted",
    ]
