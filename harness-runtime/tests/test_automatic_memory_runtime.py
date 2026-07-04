"""Tests for automatic local memory runtime wiring."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import DeploymentSurface, WorkloadClass
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderCandidate, ProviderFamily
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.memory_operation_ledger import MemoryOperationKind
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_record_envelope import (
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    MemoryVisibility,
    RedactionState,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_retrieval_index import DerivedRetrievalIndexStore
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.automatic_memory import materialize_automatic_memory_runtime
from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher
from harness_runtime.types import OTelConfig, RuntimeConfig
from opentelemetry.sdk.trace import TracerProvider

_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class _ProviderResponse:
    id: str
    usage: _Usage
    _dump: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return self._dump


class _OpenAICompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[_ProviderResponse] = []

    async def create(self, **kwargs: Any) -> _ProviderResponse:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        _fill_memory_tool_call_refs(response._dump, kwargs.get("tools"))
        return response


class _OpenAIChat:
    def __init__(self) -> None:
        self.completions = _OpenAICompletions()


class _OpenAIClient:
    def __init__(self) -> None:
        self.chat = _OpenAIChat()


@dataclass
class _OpenAIFakeAdapter:
    client: _OpenAIClient


def _fill_memory_tool_call_refs(response: dict[str, Any], tools: object) -> None:
    scope_ref = _memory_tool_schema_const(tools, "memory.search", "scope_ref")
    policy_ref = _memory_tool_schema_const(tools, "memory.search", "policy_ref")
    if scope_ref is None or policy_ref is None:
        return
    for choice in response.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        for tool_call in message.get("tool_calls", []):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict) or function.get("name") != "memory.search":
                continue
            raw_arguments = function.get("arguments")
            if not isinstance(raw_arguments, str):
                continue
            arguments = json.loads(raw_arguments)
            if arguments.get("scope_ref") == "scope-filled-by-schema":
                arguments["scope_ref"] = scope_ref
            if arguments.get("policy_ref") == "policy-filled-by-schema":
                arguments["policy_ref"] = policy_ref
            function["arguments"] = json.dumps(arguments, sort_keys=True)


def _memory_tool_schema_const(
    tools: object,
    tool_name: str,
    property_name: str,
) -> str | None:
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict) or function.get("name") != tool_name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            continue
        prop_schema = properties.get(property_name)
        if not isinstance(prop_schema, dict):
            continue
        value = prop_schema.get("const")
        if isinstance(value, str):
            return value
        enum_values = prop_schema.get("enum")
        if (
            isinstance(enum_values, list)
            and len(enum_values) == 1
            and isinstance(enum_values[0], str)
        ):
            return enum_values[0]
    return None


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _load_portable_init():
    root = Path(__file__).resolve().parents[2]
    path = root / "tools" / "portable_init.py"
    spec = importlib.util.spec_from_file_location("portable_init", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _memory_store(root: Path) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=root / ".harness" / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _seed_preference(root: Path) -> MemoryStoreRecord:
    content: dict[str, object] = {
        "semantic_kind": "preference",
        "statement": "Use concise operator-facing memory summaries.",
        "confidence": "high",
        "source_authority": "operator_direct",
        "status": "active",
        "injection_policy": "tool_allowed",
        "preference_subject": "operator_workflow",
        "preference_strength": "strong",
        "confirmation_required": False,
        "tags": ["memory", "summary"],
    }
    content_hash = compute_memory_content_hash(content)
    record = MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(
                MemoryTier.SEMANTIC,
                MemoryRecordKind.PREFERENCE,
                content_hash,
            ),
            schema_version="memory-store-record/v1",
            tier=MemoryTier.SEMANTIC,
            kind=MemoryRecordKind.PREFERENCE,
            created_at=_NOW,
            scope=MemoryScope(
                project=root.name,
                workflow="workflow-memory",
                workload_class=WorkloadClass.PIPELINE_AUTOMATION.value,
                provider_family=ProviderFamily.OPENAI.value,
                cli_profile="profile:generic",
                visibility=MemoryVisibility.PROJECT,
            ),
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:test"),),
            content_hash=content_hash,
            redaction_state=RedactionState.ACTIVE,
        ),
        content=content,
    )
    store = _memory_store(root)
    store.write_record(record)
    DerivedRetrievalIndexStore(
        root_binding=MemoryRootBinding(default_root=root / ".harness" / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    ).rebuild(indexed_at=_NOW)
    return record


def _binding(provider: str = "openai", model: str = "gpt-5") -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="memory-step",
        model_binding=ModelBinding(provider=provider, model=model),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _fallback_chain(provider: str = "openai", model: str = "gpt-5") -> FallbackChain:
    return FallbackChain(
        primary=ProviderCandidate(provider=provider, model=model, family=ProviderFamily.OPENAI),
        same_family=(),
        cross_family=(),
    )


def _step(payload: dict[str, object] | None = None) -> WorkflowStep:
    return WorkflowStep(
        step_id="memory-step",
        step_kind=StepKind.INFERENCE_STEP,
        step_payload=payload
        or {
            "messages": [{"role": "user", "content": "What memory applies here?"}],
            "tools": None,
            "params": {"max_tokens": 100},
        },
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
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


@pytest.mark.asyncio
async def test_default_local_init_normal_inference_exposes_and_persists_memory(
    tmp_path: Path,
) -> None:
    portable_init = _load_portable_init()
    (tmp_path / "harness.toml.example").write_text(
        '[runtime]\nrepository_root = "/absolute/path/to/your/workspace"\n'
        'deployment_surface = "local_development"\n'
        'default_topology = "single_threaded_linear"\n'
        '[runtime.otel]\notlp_endpoint = "http://localhost:4318"\n'
        "[runtime.memory]\nenabled = true\nnative_provider_enabled = false\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("", encoding="utf-8")
    portable_init.initialize(tmp_path)
    seed = _seed_preference(tmp_path)
    runtime = materialize_automatic_memory_runtime(
        _config(tmp_path),
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
    )
    assert runtime is not None

    client = _OpenAIClient()
    client.chat.completions.responses = [
        _ProviderResponse(
            id="cmpl-memory-tool",
            usage=_Usage(prompt_tokens=10, completion_tokens=3),
            _dump={
                "id": "cmpl-memory-tool",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_memory_search",
                                    "type": "function",
                                    "function": {
                                        "name": "memory.search",
                                        "arguments": json.dumps(
                                            {
                                                "query": "operator memory summary preference",
                                                "scope_ref": "scope-filled-by-schema",
                                                "policy_ref": "policy-filled-by-schema",
                                            },
                                            sort_keys=True,
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        ),
        _ProviderResponse(
            id="cmpl-memory-final",
            usage=_Usage(prompt_tokens=16, completion_tokens=5),
            _dump={
                "id": "cmpl-memory-final",
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
            },
        ),
    ]
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": _OpenAIFakeAdapter(client)},
        tracer_provider=TracerProvider(),
        memory_runtime=runtime,
        fallback_chain=_fallback_chain(),
    )

    await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())

    first_call = client.chat.completions.calls[0]
    assert "tools" in first_call
    assert {tool["function"]["name"] for tool in first_call["tools"]} >= {
        "memory.search",
        "memory.read",
    }
    tool_content = json.loads(client.chat.completions.calls[1]["messages"][-1]["content"])
    assert tool_content["results"][0]["memory_ref"] == str(seed.envelope.memory_id)
    assert tool_content["results"][0]["text"] == "Use concise operator-facing memory summaries."
    store = _memory_store(tmp_path)
    operations = store.read_memory_operations()
    assert MemoryOperationKind.CAPTURE in {entry.operation_kind for entry in operations}
    captured_refs = [
        ref
        for entry in operations
        if entry.operation_kind is MemoryOperationKind.CAPTURE
        for ref in entry.memory_refs
    ]
    assert captured_refs
    captured = store.read_record(
        captured_refs[-1],
        MemoryRecordKind.EPISODIC_TURN,
        run_id="run-memory-step-0",
    )
    assert captured.content["capture_mode"] == "summarized"
