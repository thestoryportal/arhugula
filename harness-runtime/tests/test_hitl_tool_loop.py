"""R-CXA-2 model-driven HITL tool-loop producer tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from harness_core import PersonaTier
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import WriteResult, read_ledger
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.cp_is_wiring import (
    RuntimeCpIsWiring,
    materialize_cp_is_wiring_stage,
)
from harness_runtime.lifecycle.hitl_placement import RuntimeHITLPlacementRegistry
from harness_runtime.lifecycle.hitl_tool_loop import (
    HITLGateDecision,
    HITLToolLoopContext,
    ModelToolCall,
    RuntimeHITLToolLoop,
)
from harness_runtime.lifecycle.state_ledger import LedgerWriter, materialize_state_ledger
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

_ACTOR = ActorIdentity("hitl-loop")
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


def _wiring(tmp_path: Path) -> RuntimeCpIsWiring:
    stage = materialize_cp_is_wiring_stage(
        _config(tmp_path),
        _ledger_writer(tmp_path),
        _pt_resolver,
    )
    return stage.wiring


def _context() -> HITLToolLoopContext:
    return HITLToolLoopContext(
        workflow_id="wf-1",
        step_id="step-1",
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        actor=_ACTOR,
    )


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: list[ModelToolCall] = []

    async def dispatch(
        self,
        call: ModelToolCall,
        context: HITLToolLoopContext,
    ) -> dict[str, Any]:
        _ = context
        self.calls.append(call)
        return {"tool_call_id": call.tool_call_id, "ok": True}


class _Gate:
    def __init__(self, response: HITLResponse = HITLResponse.APPROVE) -> None:
        self.response = response
        self.calls: list[ModelToolCall] = []

    async def decide(
        self,
        *,
        call: ModelToolCall,
        context: HITLToolLoopContext,
    ) -> HITLGateDecision:
        _ = context
        self.calls.append(call)
        return HITLGateDecision(response=self.response)


def _loop(
    tmp_path: Path,
    *,
    hitl_required_ids: frozenset[str],
    gate: _Gate | None = None,
    dispatcher: _Dispatcher | None = None,
) -> tuple[RuntimeHITLToolLoop, _Gate, _Dispatcher]:
    gate = gate or _Gate()
    dispatcher = dispatcher or _Dispatcher()
    loop = RuntimeHITLToolLoop(
        wiring=_wiring(tmp_path),
        placement_registry=RuntimeHITLPlacementRegistry(),
        hitl_required=lambda call, _context: call.tool_call_id in hitl_required_ids,
        gate=gate,
        dispatcher=dispatcher,
    )
    return loop, gate, dispatcher


def _call(tool_call_id: str, *, tool: str = "search") -> ModelToolCall:
    return ModelToolCall(
        tool_call_id=tool_call_id,
        tool=tool,
        server="mcp-main",
        arguments={"query": tool_call_id},
        provider="fixture-provider",
        model="fixture-model",
    )


def test_hitl_tool_loop_emits_only_when_hitl_required(tmp_path: Path) -> None:
    loop, gate, dispatcher = _loop(tmp_path, hitl_required_ids=frozenset({"call-hitl"}))

    results = asyncio.run(
        loop.run_tool_calls([_call("call-hitl"), _call("call-direct")], _context())
    )

    assert [result.tool_call_id for result in results] == ["call-hitl", "call-direct"]
    assert results[0].rewrite_write_result is WriteResult.APPENDED
    assert results[1].rewrite_write_result is None
    assert [call.tool_call_id for call in gate.calls] == ["call-hitl"]
    assert [call.tool_call_id for call in dispatcher.calls] == ["call-hitl", "call-direct"]
    entries = read_ledger(loop.wiring.ledger_writer.handle)
    assert [entry.action_id for entry in entries] == ["cp.hitl-tool-call-rewriting"]


def test_hitl_tool_loop_preserves_model_tool_call_id_for_replay(tmp_path: Path) -> None:
    loop, _gate, _dispatcher = _loop(tmp_path, hitl_required_ids=frozenset({"provider-call-7"}))

    first = asyncio.run(loop.run_tool_calls([_call("provider-call-7")], _context()))
    second = asyncio.run(loop.run_tool_calls([_call("provider-call-7")], _context()))

    assert first[0].rewrite_write_result is WriteResult.APPENDED
    assert second[0].rewrite_write_result is WriteResult.IDEMPOTENT_NOOP
    assert first[0].tool_call_id == "provider-call-7"
    assert second[0].tool_call_id == "provider-call-7"
    assert len(read_ledger(loop.wiring.ledger_writer.handle)) == 1


def test_hitl_tool_loop_reject_skips_dispatch(tmp_path: Path) -> None:
    loop, gate, dispatcher = _loop(
        tmp_path,
        hitl_required_ids=frozenset({"call-hitl"}),
        gate=_Gate(response=HITLResponse.REJECT),
    )

    results = asyncio.run(loop.run_tool_calls([_call("call-hitl")], _context()))

    assert results[0].gate_response is HITLResponse.REJECT
    assert results[0].dispatched is False
    assert [call.tool_call_id for call in gate.calls] == ["call-hitl"]
    assert dispatcher.calls == []
    entries = read_ledger(loop.wiring.ledger_writer.handle)
    assert [entry.action_id for entry in entries] == ["cp.hitl-tool-call-rewriting"]
