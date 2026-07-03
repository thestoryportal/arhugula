"""Tests for automatic local memory runtime wiring."""

from __future__ import annotations

from pathlib import Path

from harness_as.sandbox_tier import SandboxTier
from harness_core import DeploymentSurface
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderCandidate, ProviderFamily
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.automatic_memory import materialize_automatic_memory_runtime
from harness_runtime.types import OTelConfig, RuntimeConfig


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def test_first_use_initializes_local_memory_root_and_prompt_packet(tmp_path: Path) -> None:
    runtime = materialize_automatic_memory_runtime(_config(tmp_path))

    assert runtime is not None
    assert (tmp_path / ".harness" / "memory" / "semantic" / "index.jsonl").is_file()

    context = runtime.compose_for_dispatch(
        binding=StepEffectiveBinding(
            step_id="memory-step",
            model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            override_applied=False,
            persona_tier=PersonaTier.SOLO_DEVELOPER,
        ),
        fallback_chain=FallbackChain(
            primary=ProviderCandidate(
                provider="anthropic",
                model="claude-opus-4-7",
                family=ProviderFamily.ANTHROPIC,
            ),
            same_family=(),
            cross_family=(),
        ),
        step=WorkflowStep(
            step_id="memory-step",
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={"messages": [{"role": "user", "content": "remember repo rules"}]},
        ),
        step_context=StepExecutionContext(
            workflow_id="workflow-memory",
            parent_action_id="workflow:workflow-memory:step:0",
            parent_gate_level=GateLevel.AUTO,
            parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
            parent_entry_hash="",
            parent_idempotency_key="run-memory-step-0",
            tenant_id=None,
            step_index=0,
            run_engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        ),
    )

    assert context.access_mode.value == "prompt_extension_packet"
    assert context.packet is not None
    assert context.packet.sections == ()
