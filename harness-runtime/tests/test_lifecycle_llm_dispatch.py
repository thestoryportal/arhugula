"""Tests for the LLM-dispatch composer (U-RT-52, C-RT-15).

Covers per the U-RT-52 plan body acceptance criteria:

  AC #1  — Protocol satisfaction (`StepDispatcher` from CP workflow driver).
  AC #2  — Per-provider dispatch (anthropic / openai / ollama).
  AC #3  — GenAI semconv 1.41.0 span attribute emission.
  AC #4  — `anthropic.*` cache attributes — conditional on provider==anthropic.
  AC #5  — `RT-FAIL-PROVIDER-UNREACHABLE` wiring for absent provider.
  AC #6  — Async-only invariant (composer is async).
  AC #7  — Bootstrap stage 5 binding via `materialize_llm_dispatcher_stage`.

Plus a Class 3 fork residual: `RT-FAIL-PAYLOAD-SHAPE` typed error when
`step.step_payload` cannot be coerced to `ProviderAgnosticPayload`.

Test conventions follow `tests/test_lifecycle_span_processor.py` —
in-memory OTel span exporter + simple SimpleSpanProcessor for
synchronous flushing under test.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_core.identity import StepID
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding, RouterResolution
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.embedding_routing import EmbeddingRoutingCorpus, make_embedding_classifier
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_placement import HITLPlacement, HITLPlacementKind
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.layer_budget import DEFAULT_LAYER_BUDGETS, LayerBudget
from harness_cp.layered_routing_strategy import LayerDecisionFn
from harness_cp.per_role_catalog import (
    derive_agent_role,
    derive_fanout_roles,
    validate_per_role_catalog,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.routing_core_surface import RoutingCandidateUnresolvedError
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import (
    RetryPolicy,
    RoleRoutingBinding,
    RoutingManifest,
    WorkloadRoutingOverride,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    StepDispatcher,
    _rehydrate_inter_step_channel_on_replay,
    execute_workflow,
)
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_od.audit_ledger_types import SignatureAlgorithm
from harness_runtime.lifecycle import llm_dispatch as llm_dispatch_module
from harness_runtime.lifecycle.ask_user_question_surface import (
    AskUserQuestionResult,
    AskUserQuestionSurface,
)
from harness_runtime.lifecycle.engine_output_store import EngineOutputStore
from harness_runtime.lifecycle.hitl_gate_composer import RuntimeHITLGateComposer
from harness_runtime.lifecycle.hitl_tool_loop import HITLToolLoopContext, ModelToolCall
from harness_runtime.lifecycle.inter_step_output_channel import InterStepOutputChannel
from harness_runtime.lifecycle.llm_dispatch import (
    LLMDispatchBindError,
    LLMDispatchPayloadShapeError,
    LLMDispatchProviderUnreachableError,
    PromptInjectionConflictError,
    RuntimeLLMDispatcher,
    materialize_llm_dispatcher_stage,
)
from harness_runtime.lifecycle.post_join_synthesis_dispatch import (
    POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX,
    PostJoinSynthesisStepDispatcher,
    _compose_synthesis_payload,
)
from harness_runtime.lifecycle.prompt_selection import (
    PromptSelectionUnauthoredError,
    PromptVersionUnapprovedError,
)
from harness_runtime.lifecycle.retry_breaker import (
    DEFAULT_RETRY_POLICY,
    RuntimeRetryBreaker,
)
from harness_runtime.lifecycle.retry_breaker_fallback import (
    RESERVED_LLM_DISPATCH_KEY,
    RetryBreakerFallbackDispatcher,
)
from harness_runtime.lifecycle.sync_dispatcher_facade import SyncDispatcherFacade
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    """Anthropic / OpenAI usage carrier — duck-typed."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass
class _ProviderResponse:
    """Fake provider response — exposes `.usage`, `.id`, and `model_dump()`."""

    id: str
    usage: _Usage
    _dump: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return self._dump


@dataclass
class _OllamaResponse:
    """Ollama returns prompt_eval_count / eval_count, no `.usage` object."""

    prompt_eval_count: int
    eval_count: int
    _dump: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return self._dump


class _AnthropicMessages:
    """Records kwargs of the last `create` call; returns a canned response."""

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self.canned_response: Any = _ProviderResponse(
            id="msg_test_001",
            usage=_Usage(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=2,
                cache_read_input_tokens=3,
            ),
            _dump={"id": "msg_test_001", "content": [{"text": "ok"}]},
        )

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return self.canned_response


class _AnthropicClient:
    def __init__(self) -> None:
        self.messages = _AnthropicMessages()


@dataclass
class _AnthropicFakeAdapter:
    client: _AnthropicClient


@dataclass
class _AnthropicToolTurnResponse:
    id: str
    content: list[dict[str, Any]]
    stop_reason: str
    usage: _Usage

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class _FakeHITLLoopResult:
    tool_call_id: str
    dispatch_result: Mapping[str, Any] | None


class _FakeHITLToolLoop:
    def __init__(self, dispatch_results: Mapping[str, Mapping[str, Any]]) -> None:
        self.dispatch_results = dispatch_results
        self.calls: list[tuple[tuple[ModelToolCall, ...], HITLToolLoopContext]] = []

    async def run_tool_calls(
        self,
        calls: Sequence[ModelToolCall],
        context: HITLToolLoopContext,
    ) -> tuple[_FakeHITLLoopResult, ...]:
        call_tuple = tuple(calls)
        self.calls.append((call_tuple, context))
        return tuple(
            _FakeHITLLoopResult(
                tool_call_id=call.tool_call_id,
                dispatch_result=self.dispatch_results.get(call.tool_call_id),
            )
            for call in call_tuple
        )


class _OpenAICompletions:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self.canned_response = _ProviderResponse(
            id="cmpl_test_001",
            usage=_Usage(prompt_tokens=15, completion_tokens=7),
            _dump={"id": "cmpl_test_001", "choices": [{"message": {"content": "ok"}}]},
        )

    async def create(self, **kwargs: Any) -> _ProviderResponse:
        self.last_kwargs = kwargs
        return self.canned_response


class _OpenAIChat:
    def __init__(self) -> None:
        self.completions = _OpenAICompletions()


class _OpenAIClient:
    def __init__(self) -> None:
        self.chat = _OpenAIChat()


@dataclass
class _OpenAIFakeAdapter:
    client: _OpenAIClient


class _OllamaClient:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self.canned_response = _OllamaResponse(
            prompt_eval_count=20,
            eval_count=8,
            _dump={"model": "llama3", "message": {"content": "ok"}},
        )

    async def chat(self, **kwargs: Any) -> _OllamaResponse:
        self.last_kwargs = kwargs
        return self.canned_response


@dataclass
class _OllamaFakeAdapter:
    client: _OllamaClient


@dataclass(frozen=True)
class _ExternalCLIResult:
    text: str
    exit_code: int = 0


@dataclass
class _ExternalCLIFakeAdapter:
    calls: list[tuple[str, str]]

    async def dispatch_text(self, *, model: str, prompt: str) -> _ExternalCLIResult:
        self.calls.append((model, prompt))
        return _ExternalCLIResult(text="OK", exit_code=0)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _binding(provider: str, model: str = "test-model-1") -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-001",
        model_binding=ModelBinding(provider=provider, model=model),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step(payload: dict[str, Any] | None = None) -> WorkflowStep:
    if payload is None:
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "params": {"max_tokens": 100},
        }
    return WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload=payload,
    )


def _tracer_provider_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


def _step_context(step_index: int = 0) -> StepExecutionContext:
    """Default step_context for v1.6 Path A test fixtures.

    Composes the 8-field StepExecutionContext with MVP defaults per the
    type's docstring. Tests that exercise step_context semantics override
    individual fields; the C-RT-15 inner LLM dispatcher does not consume
    step_context at v1.6, so this default is sufficient for dispatch tests.
    """
    return StepExecutionContext(
        workflow_id="test-wf",
        parent_action_id=f"workflow:test-wf:step:{step_index}",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="test-step-key",
        tenant_id=None,
        step_index=step_index,
    )


# ---------------------------------------------------------------------------
# AC #1 — Protocol satisfaction.
# ---------------------------------------------------------------------------


def test_runtime_dispatcher_satisfies_step_dispatcher_protocol() -> None:
    """`RuntimeLLMDispatcher` is structurally a `StepDispatcher` per
    `harness_cp.workflow_driver.StepDispatcher` (runtime-checkable).
    """
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": _AnthropicFakeAdapter(_AnthropicClient())},
        tracer_provider=tp,
    )
    assert isinstance(dispatcher, StepDispatcher)


# ---------------------------------------------------------------------------
# AC #2 — Per-provider dispatch round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_anthropic_round_trip() -> None:
    """Anthropic branch calls `client.messages.create(model=..., **kwargs)`."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    result = await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "test-model-1"
    assert adapter.client.messages.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert adapter.client.messages.last_kwargs["max_tokens"] == 100
    assert result["id"] == "msg_test_001"


@pytest.mark.asyncio
async def test_edit_decoded_payload_reaches_real_llm_dispatcher() -> None:
    """B-EDIT-CARRIER real-consumer witness: an operator EDIT (flat `str`)
    decoded by the wrap-time HITL composer becomes the payload the REAL
    `RuntimeLLMDispatcher` dispatches to the provider.

    Closes the half-proof gap (advisor pre-merge): the composer-unit witness in
    `test_lifecycle_hitl_gate_composer.py` asserts the mutated step reaches a
    MOCK inner. This drives the composer's inner = the REAL `RuntimeLLMDispatcher`
    over a recording provider adapter, proving the decoded Mapping is what
    `_coerce_payload(step.step_payload)` consumes and the provider actually
    receives — NOT the original step_payload, and NOT a value sourced elsewhere
    (the inter-step channel is opt-in/None here). `[[full-chain-witness-not-half-proofs]]`.
    """
    edited_messages = [{"role": "user", "content": "EDITED BY OPERATOR"}]
    edited_str = json.dumps(
        {"messages": edited_messages, "tools": None, "params": {"max_tokens": 100}}
    )

    class _AskEdit:
        async def ask(
            self, prompt: str, options: Sequence[HITLResponse], timeout: float | None
        ) -> AskUserQuestionResult:
            _ = prompt, options, timeout
            return AskUserQuestionResult(
                response=HITLResponse.EDIT, latency_ms=4.0, edited_proposal=edited_str
            )

    class _Ledger:
        def append(self, payload: Any, key: Any) -> Any:
            return ("h", payload, key)

    class _Audit:
        def append(self, *, tenant_id: Any, audit_entry: Any) -> Any:
            _ = tenant_id
            return ("w", audit_entry)

    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)
    composer = RuntimeHITLGateComposer(
        inner=dispatcher,
        applicable_placements=frozenset({HITLPlacementKind.PRE_ACTION}),
        ask_user_question_surface=cast(AskUserQuestionSurface, _AskEdit()),
        ledger_writer=cast(Any, _Ledger()),
        audit_writer=cast(Any, _Audit()),
        tracer_provider=tp,
        audit_signing_key_id="harness-runtime-test",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=lambda: Identifier("b" * 64),
    )
    # Original payload is DISTINCT from the operator's edit (proves replacement).
    original = {
        "messages": [{"role": "user", "content": "ORIGINAL"}],
        "tools": None,
        "params": {"max_tokens": 100},
    }
    placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION)
    ctx = _step_context().model_copy(update={"hitl_placements": (placement,)})

    await composer.dispatch(_binding("anthropic"), _step(original), step_context=ctx)

    # The REAL dispatcher consumed the DECODED payload (not the original) — the
    # str→Mapping→step_payload→_coerce_payload→provider chain end-to-end.
    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["messages"] == edited_messages


# ---------------------------------------------------------------------------
# B-POSTJOIN-LLM-SYNTHESIS — real-dispatcher witnesses (out-of-family Codex round 8).
# The arc was built + tested against STUBS (_RecordingInner / _OWDispatcher / a
# hand-rolled _OllamaInner), so the PRODUCTION payload path (`_coerce_payload` →
# `ProviderAgnosticPayload`) + the HITL-EDIT interaction were never exercised — the root
# cause of the round-5..8 finding streak. These drive a `POST_JOIN_SYNTHESIS` step
# through the REAL `RuntimeLLMDispatcher` over a recording provider adapter (the missing
# full-chain witness; `[[full-chain-witness-not-half-proofs]]`).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_join_synthesis_realistic_payload_coerces_through_real_dispatcher() -> None:
    """[P2] — a realistic synthesis payload (`{"messages": [...], "params": {"max_tokens":
    N}}`, like EVERY inference step), composed by `_compose_synthesis_payload`, coerces
    through the PRODUCTION `_coerce_payload` → `ProviderAgnosticPayload` (harness forces
    `tools=None`; the author supplies `params`) and the branch-index-ordered siblings reach
    the real provider call. The harness does NOT manufacture a `params`/`max_tokens` default
    (round 9 [P2]: a fabricated `params={}` coerces locally but the real Anthropic call
    needs `max_tokens`) — the synthesis payload is a normal inference payload."""
    composed = _compose_synthesis_payload(
        {
            "messages": [{"role": "system", "content": "synthesize the siblings"}],
            "params": {"max_tokens": 128},
        },
        ((0, {"finding": "alpha"}), (1, {"finding": "beta"})),
    )
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload=composed,
    )
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    result = await dispatcher.dispatch(_binding("anthropic"), step, step_context=_step_context())

    # Coerced + dispatched (no LLMDispatchPayloadShapeError) — the [P2] fix end-to-end.
    assert result["id"] == "msg_test_001"
    assert adapter.client.messages.last_kwargs is not None
    # The author's params reached the REAL Anthropic call (max_tokens present — the round-9
    # gap: a fabricated params={} would have omitted it).
    assert adapter.client.messages.last_kwargs["max_tokens"] == 128
    # The branch-index-ordered siblings reached the REAL provider call (the last user
    # message; the `system` instruction is extracted by the anthropic translator).
    sibling_msg = adapter.client.messages.last_kwargs["messages"][-1]
    assert sibling_msg["content"].startswith(POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX)
    assert "alpha" in sibling_msg["content"]
    assert "beta" in sibling_msg["content"]


@pytest.mark.asyncio
async def test_post_join_synthesis_tool_bearing_payload_rejected_at_dispatch_boundary() -> None:
    """[P1] — the LOAD-BEARING effect-free boundary guard. A POST_JOIN_SYNTHESIS step whose
    payload binds tools (the shape a post-HITL-EDIT replacement can produce) is rejected at
    the LLM dispatch boundary (post-`_coerce_payload`), BEFORE the provider call. The
    compose-time guard cannot see such a payload; this is the real floor."""
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "write_file"}],
            "params": {},
        },
    )
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    with pytest.raises(LLMDispatchPayloadShapeError, match="effect-free"):
        await dispatcher.dispatch(_binding("anthropic"), step, step_context=_step_context())
    # Rejected BEFORE the provider call — no LLM dispatch fired.
    assert adapter.client.messages.last_kwargs is None


@pytest.mark.asyncio
async def test_post_join_synthesis_hitl_edit_readding_tools_rejected_at_boundary() -> None:
    """[P1] — the HITL-EDIT bypass, closed END-TO-END. A PRE_ACTION HITL gate on a
    POST_JOIN_SYNTHESIS step with operator EDIT replaces the step_payload INSIDE the inner
    dispatcher (AFTER the compose-time guard ran); the edited payload re-adds tools. The
    boundary guard (downstream of the edit, at the real dispatcher) rejects it before the
    provider call — the effect-free invariant holds against EVERY payload source. Mirrors
    `test_edit_decoded_payload_reaches_real_llm_dispatcher` with a synthesis step."""
    edited_str = json.dumps(
        {
            "messages": [{"role": "user", "content": "edited"}],
            "tools": [{"name": "rm_rf"}],
            "params": {},
        }
    )

    class _AskEdit:
        async def ask(
            self, prompt: str, options: Sequence[HITLResponse], timeout: float | None
        ) -> AskUserQuestionResult:
            _ = prompt, options, timeout
            return AskUserQuestionResult(
                response=HITLResponse.EDIT, latency_ms=4.0, edited_proposal=edited_str
            )

    class _Ledger:
        def append(self, payload: Any, key: Any) -> Any:
            return ("h", payload, key)

    class _Audit:
        def append(self, *, tenant_id: Any, audit_entry: Any) -> Any:
            _ = tenant_id
            return ("w", audit_entry)

    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)
    composer = RuntimeHITLGateComposer(
        inner=dispatcher,
        applicable_placements=frozenset({HITLPlacementKind.PRE_ACTION}),
        ask_user_question_surface=cast(AskUserQuestionSurface, _AskEdit()),
        ledger_writer=cast(Any, _Ledger()),
        audit_writer=cast(Any, _Audit()),
        tracer_provider=tp,
        audit_signing_key_id="harness-runtime-test",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=lambda: Identifier("b" * 64),
    )
    synthesis_step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={
            "messages": [{"role": "user", "content": "original"}],
            "tools": None,
            "params": {},
        },
    )
    placement = HITLPlacement(position=HITLPlacementKind.PRE_ACTION)
    ctx = _step_context().model_copy(update={"hitl_placements": (placement,)})

    with pytest.raises(LLMDispatchPayloadShapeError, match="effect-free"):
        await composer.dispatch(_binding("anthropic"), synthesis_step, step_context=ctx)
    # The operator's tool-re-adding EDIT never reached the provider.
    assert adapter.client.messages.last_kwargs is None


def test_post_join_synthesis_single_chain_through_real_dispatcher() -> None:
    """Capstone single-chain witness (advisor pre-ship): the REAL
    `PostJoinSynthesisStepDispatcher` → the REAL `RuntimeLLMDispatcher` as ONE chain — the
    exact seam every other witness still stubs (Test A composes then dispatches separately;
    the full-chain test stubs the inner with `_RecordingInner`; the Ollama e2e stubs with
    `_OllamaInner`). Here the synthesis dispatcher reads the siblings off the context,
    composes the minimal payload, and the real dispatcher coerces + dispatches — no stubbed
    inner (`[[full-chain-witness-not-half-proofs]]`). Sync (the synthesis dispatcher is
    sync); the thin wrapper bridges to the async dispatcher via `asyncio.run` — no running
    loop, so no `@pytest.mark.asyncio`."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    real = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    class _SyncWrap:
        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: StepExecutionContext,
        ) -> Mapping[str, Any]:
            return asyncio.run(real.dispatch(binding, step, step_context=step_context))

    synth = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, _SyncWrap()))
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={
            "messages": [{"role": "system", "content": "synthesize the siblings"}],
            "params": {"max_tokens": 128},
        },
    )
    ctx = _step_context().model_copy(
        update={"sibling_outputs": ((0, {"finding": "alpha"}), (1, {"finding": "beta"}))}
    )

    out = synth.dispatch(_binding("anthropic"), step, step_context=ctx)

    # The realistic payload coerced + dispatched through the real chain (no stub).
    assert out["id"] == "msg_test_001"
    assert adapter.client.messages.last_kwargs is not None
    sibling_msg = adapter.client.messages.last_kwargs["messages"][-1]
    assert sibling_msg["content"].startswith(POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX)
    assert "alpha" in sibling_msg["content"]
    assert "beta" in sibling_msg["content"]


@pytest.mark.asyncio
async def test_dispatch_anthropic_tool_use_runs_hitl_loop_and_continues() -> None:
    """R-CXA-2 provider-turn tool_use blocks flow through the bound HITL loop."""
    client = _AnthropicClient()
    client.messages.responses = [
        _AnthropicToolTurnResponse(
            id="msg_tool",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "search_docs",
                    "input": {"query": "runtime HITL"},
                }
            ],
            stop_reason="tool_use",
            usage=_Usage(input_tokens=11, output_tokens=6),
        ),
        _AnthropicToolTurnResponse(
            id="msg_final",
            content=[{"type": "text", "text": "done"}],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=12, output_tokens=4),
        ),
    ]
    adapter = _AnthropicFakeAdapter(client)
    hitl_loop = _FakeHITLToolLoop({"toolu_001": {"ok": True}})
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        hitl_tool_loop=cast(Any, hitl_loop),
    )

    result = await dispatcher.dispatch(
        _binding("anthropic", model="claude-test"),
        _step(
            {
                "messages": [{"role": "user", "content": "search"}],
                "tools": [
                    {
                        "name": "search_docs",
                        "server": "docs-mcp",
                        "input_schema": {"type": "object"},
                    }
                ],
                "params": {"max_tokens": 100},
            }
        ),
        step_context=_step_context(),
    )

    assert result["id"] == "msg_final"
    assert len(client.messages.calls) == 2
    call, context = hitl_loop.calls[0]
    assert call[0].tool_call_id == "toolu_001"
    assert call[0].tool == "search_docs"
    assert call[0].server == "docs-mcp"
    assert call[0].arguments == {"query": "runtime HITL"}
    assert context.workflow_id == "test-wf"
    assert context.step_id == "step-001"
    assert str(context.actor) == "test-runtime"

    continuation_messages = client.messages.calls[1]["messages"]
    assert continuation_messages[-2] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_001",
                "name": "search_docs",
                "input": {"query": "runtime HITL"},
            }
        ],
    }
    assert continuation_messages[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_001",
                "content": '{"ok": true}',
            }
        ],
    }


@pytest.mark.asyncio
async def test_dispatch_openai_round_trip() -> None:
    """OpenAI branch calls `client.chat.completions.create(...)`."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"openai": adapter}, tracer_provider=tp)

    result = await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())

    assert adapter.client.chat.completions.last_kwargs is not None
    assert adapter.client.chat.completions.last_kwargs["model"] == "test-model-1"
    assert result["id"] == "cmpl_test_001"


@pytest.mark.asyncio
async def test_dispatch_ollama_round_trip() -> None:
    """Ollama branch calls `client.chat(...)`."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"ollama": adapter}, tracer_provider=tp)

    result = await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    assert adapter.client.last_kwargs is not None
    assert adapter.client.last_kwargs["model"] == "test-model-1"
    assert result["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_dispatch_external_cli_round_trip_text_only() -> None:
    """External CLI providers dispatch one text prompt through `dispatch_text`."""
    adapter = _ExternalCLIFakeAdapter(calls=[])
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"claude_code": adapter}, tracer_provider=tp)

    result = await dispatcher.dispatch(
        _binding("claude_code", model="sonnet"),
        _step(),
        step_context=_step_context(),
    )

    assert result["provider"] == "claude_code"
    assert result["model"] == "sonnet"
    assert result["content"] == [{"type": "text", "text": "OK"}]
    assert len(adapter.calls) == 1
    model, prompt = adapter.calls[0]
    assert model == "sonnet"
    assert "user:\nhi" in prompt
    attrs = exporter.get_finished_spans()[0].attributes or {}
    assert attrs["gen_ai.provider.name"] == "claude_code"
    assert attrs["external_cli.exit_code"] == 0


@pytest.mark.asyncio
async def test_dispatch_external_cli_rejects_tools_before_subprocess() -> None:
    """R-CLI-1 v1 is text-only; tool payloads fail before invoking the CLI."""
    adapter = _ExternalCLIFakeAdapter(calls=[])
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"claude_code": adapter}, tracer_provider=tp)
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "not-supported"}],
        "params": {},
    }

    with pytest.raises(LLMDispatchPayloadShapeError, match="text-only"):
        await dispatcher.dispatch(
            _binding("claude_code", model="sonnet"),
            _step(payload),
            step_context=_step_context(),
        )

    assert adapter.calls == []


# ---------------------------------------------------------------------------
# R-PM-1 cascade PR #1 — active-prompt system injection (proof (a):
# dispatch-composition reachability through the real `dispatch(...)` path,
# asserting the recorded provider-client kwargs, NOT the translate fn directly).
# ---------------------------------------------------------------------------


_SYS = "You are the active harness prompt."


@pytest.mark.asyncio
async def test_active_system_prompt_injects_anthropic_system_kwarg() -> None:
    """Anthropic dispatch injects the active prompt as the top-level ``system=``
    kwarg (the base-system-prompt route), reached through the real dispatch path."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["system"] == _SYS
    # The neutral payload's user message passes through untouched.
    assert adapter.client.messages.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_active_system_prompt_injects_through_anthropic_hitl_variant() -> None:
    """The HITL-tool-loop anthropic variant (`_dispatch_anthropic_with_hitl_tool_loop`)
    also injects ``system=`` — symmetric coverage so the variant is not silently
    missed (the active prompt persists across continuation turns)."""
    client = _AnthropicClient()
    client.messages.responses = [
        _AnthropicToolTurnResponse(
            id="msg_tool",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "search_docs",
                    "input": {"query": "x"},
                }
            ],
            stop_reason="tool_use",
            usage=_Usage(input_tokens=11, output_tokens=6),
        ),
        _AnthropicToolTurnResponse(
            id="msg_final",
            content=[{"type": "text", "text": "done"}],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=12, output_tokens=4),
        ),
    ]
    adapter = _AnthropicFakeAdapter(client)
    hitl_loop = _FakeHITLToolLoop({"toolu_001": {"ok": True}})
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        hitl_tool_loop=cast(Any, hitl_loop),
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(
        _binding("anthropic", model="claude-test"),
        _step(
            {
                "messages": [{"role": "user", "content": "search"}],
                "tools": [
                    {
                        "name": "search_docs",
                        "server": "docs-mcp",
                        "input_schema": {"type": "object"},
                    }
                ],
                "params": {"max_tokens": 100},
            }
        ),
        step_context=_step_context(),
    )

    # `system=` is present on both the initial turn and the continuation turn.
    assert len(client.messages.calls) == 2
    assert all(call["system"] == _SYS for call in client.messages.calls)


@pytest.mark.asyncio
async def test_active_system_prompt_injects_through_anthropic_memory_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory-tool anthropic variant (`_dispatch_anthropic_with_memory`) also
    injects ``system=`` — closes the "all 5 helpers" invariant at test level
    (adversarial review F3-01; the 5th helper, previously asserted-not-tested)."""
    recorded: dict[str, Any] = {}

    async def _fake_execute_with_memory_callbacks(
        *, messages_create_kwargs: dict[str, Any], **_kw: Any
    ) -> Any:
        recorded.update(messages_create_kwargs)
        return _ProviderResponse(
            id="msg_mem_001",
            usage=_Usage(input_tokens=1, output_tokens=1),
            _dump={"id": "msg_mem_001", "content": [{"text": "ok"}]},
        )

    monkeypatch.setattr(
        "harness_runtime.lifecycle.llm_dispatch.execute_with_memory_callbacks",
        _fake_execute_with_memory_callbacks,
    )

    class _FakeMemRegistry:
        configured_backend = "sqlite"

        def resolve_backend(self, _surface: Any) -> Any:
            return object()

    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        memory_tool_registry=_FakeMemRegistry(),
        deployment_surface="local-development",
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(
        _binding("anthropic"),
        _step(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "memory_20250818", "name": "memory"}],
                "params": {"max_tokens": 100},
            }
        ),
        step_context=_step_context(),
    )

    assert recorded.get("system") == _SYS


@pytest.mark.asyncio
async def test_active_system_prompt_injects_openai_leading_system_message() -> None:
    """OpenAI dispatch injects the active prompt as a leading ``role:"system"``
    message (the OpenAI base-prompt route)."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())

    msgs = adapter.client.chat.completions.last_kwargs["messages"]  # type: ignore[index]
    assert msgs[0] == {"role": "system", "content": _SYS}
    assert msgs[1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_active_system_prompt_injects_ollama_leading_system_message() -> None:
    """Ollama dispatch injects the active prompt as a leading ``role:"system"``
    message (the Ollama base-prompt route)."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"ollama": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    msgs = adapter.client.last_kwargs["messages"]  # type: ignore[index]
    assert msgs[0] == {"role": "system", "content": _SYS}
    assert msgs[1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_no_active_system_prompt_is_byte_identical() -> None:
    """With no active prompt (the local-first default), dispatch is byte-identical
    to pre-R-PM-1: no ``system`` kwarg for anthropic; no leading system message
    for openai."""
    anthropic = _AnthropicFakeAdapter(_AnthropicClient())
    openai = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": anthropic, "openai": openai},
        tracer_provider=tp,
        active_system_prompt=None,
    )

    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())
    await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())

    assert "system" not in anthropic.client.messages.last_kwargs  # type: ignore[operator]
    assert openai.client.chat.completions.last_kwargs["messages"] == [  # type: ignore[index]
        {"role": "user", "content": "hi"}
    ]


@pytest.mark.asyncio
async def test_active_prompt_conflict_anthropic_params_system_fails_loud() -> None:
    """An active prompt + a payload-carried Anthropic ``params["system"]`` (the
    opaque escape hatch) is fail-loud, not silently merged
    (`RT-FAIL-PROMPT-INJECTION-CONFLICT`)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    with pytest.raises(PromptInjectionConflictError):
        await dispatcher.dispatch(
            _binding("anthropic"),
            _step(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": None,
                    "params": {"max_tokens": 100, "system": "competing system"},
                }
            ),
            step_context=_step_context(),
        )


@pytest.mark.asyncio
async def test_active_prompt_conflict_openai_leading_system_message_fails_loud() -> None:
    """An active prompt + a payload-carried leading ``role:"system"`` message (the
    idiomatic per-step system prompt) is fail-loud — the known operational
    consequence (configuring an active prompt hard-errors workflows that carry
    their own system message)."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    with pytest.raises(PromptInjectionConflictError):
        await dispatcher.dispatch(
            _binding("openai"),
            _step(
                {
                    "messages": [
                        {"role": "system", "content": "step-owned system"},
                        {"role": "user", "content": "hi"},
                    ],
                    "tools": None,
                    "params": {"max_tokens": 100},
                }
            ),
            step_context=_step_context(),
        )


@pytest.mark.asyncio
async def test_active_prompt_injects_after_params_messages_override() -> None:
    """Codex regression — `params["messages"]` (the opaque escape hatch) must NOT
    silently clobber the injected system message. Injection happens after the
    params merge, so the system is prepended to the EFFECTIVE messages."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(
        _binding("openai"),
        _step(
            {
                "messages": [{"role": "user", "content": "ignored-top-level"}],
                "tools": None,
                "params": {
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "from-params"}],
                },
            }
        ),
        step_context=_step_context(),
    )

    msgs = adapter.client.chat.completions.last_kwargs["messages"]  # type: ignore[index]
    assert msgs[0] == {"role": "system", "content": _SYS}
    assert msgs[1] == {"role": "user", "content": "from-params"}


@pytest.mark.asyncio
async def test_active_prompt_conflict_hidden_in_params_messages_fails_loud() -> None:
    """Codex regression — a competing system source hidden in `params["messages"]`
    is detected (the conflict check runs on the post-merge effective messages)."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": adapter},
        tracer_provider=tp,
        active_system_prompt=_SYS,
    )

    with pytest.raises(PromptInjectionConflictError):
        await dispatcher.dispatch(
            _binding("openai"),
            _step(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": None,
                    "params": {
                        "max_tokens": 100,
                        "messages": [
                            {"role": "system", "content": "hidden-in-params"},
                            {"role": "user", "content": "hi"},
                        ],
                    },
                }
            ),
            step_context=_step_context(),
        )


# ---------------------------------------------------------------------------
# AC #3 — GenAI semconv span attributes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genai_span_emits_required_attributes_for_openai() -> None:
    """Span carries §4.3 Required (Stable) tier + gen_ai.usage.* + response.id."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"openai": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(
        _binding("openai", model="gpt-4o-mini"),
        _step(),
        step_context=_step_context(),
    )

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    span = finished[0]
    attrs = span.attributes or {}
    # Span name per OD spec v1.12 §C-OD-04 §4.1 2-token form:
    # `{gen_ai.operation.name} {gen_ai.request.model}` byte-exact to OTel
    # GenAI semconv 1.41.0 per fork doc R1 apply. Operation-token sources
    # from `GenAiOperation.CHAT.value` per §4.2 enum (finding (g) RESOLVED
    # 2026-05-26 per fork doc §9).
    assert span.name == "chat gpt-4o-mini"
    # §4.3 Required (Stable) tier — all 3 attributes always emitted
    # (finding (f) RESOLVED 2026-05-26 per fork doc §8).
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
    assert attrs["gen_ai.usage.input_tokens"] == 15
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.response.id"] == "cmpl_test_001"


@pytest.mark.asyncio
async def test_genai_span_handles_ollama_usage_shape() -> None:
    """Ollama's `prompt_eval_count` / `eval_count` populate input/output tokens."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"ollama": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    attrs = finished[0].attributes or {}
    assert attrs["gen_ai.usage.input_tokens"] == 20
    assert attrs["gen_ai.usage.output_tokens"] == 8
    # Ollama has no response.id — attribute should be absent.
    assert "gen_ai.response.id" not in attrs


# ---------------------------------------------------------------------------
# `[[fork-od-spec-declared-but-not-emitted-attributes]]` Path A — 3 attrs:
# `gen_ai.conversation.id` + `server.address` + `server.port`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_id_emitted_from_step_context_workflow_id() -> None:
    """`gen_ai.conversation.id` sources from `step_context.workflow_id`."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"openai": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["gen_ai.conversation.id"] == "test-wf"


@pytest.mark.asyncio
async def test_server_address_and_port_emitted_for_anthropic() -> None:
    """Anthropic emits static `api.anthropic.com:443` per Path A."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["server.address"] == "api.anthropic.com"
    assert attrs["server.port"] == 443


@pytest.mark.asyncio
async def test_server_address_and_port_emitted_for_openai() -> None:
    """OpenAI emits static `api.openai.com:443` per Path A."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"openai": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["server.address"] == "api.openai.com"
    assert attrs["server.port"] == 443


@pytest.mark.asyncio
async def test_server_address_and_port_absent_for_ollama_when_host_unset() -> None:
    """Ollama with `ollama_host=None` emits NEITHER `server.address` NOR
    `server.port` — OTel Conditionally Required gating prevents the
    static-map-lies-about-remote-daemon failure mode."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"ollama": adapter}, tracer_provider=tp, ollama_host=None
    )

    await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert "server.address" not in attrs
    assert "server.port" not in attrs


@pytest.mark.asyncio
async def test_server_address_and_port_emitted_for_ollama_with_localhost_host() -> None:
    """Ollama with `ollama_host=http://localhost:11434` parses to
    `localhost:11434` and emits both attributes."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"ollama": adapter},
        tracer_provider=tp,
        ollama_host="http://localhost:11434",
    )

    await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["server.address"] == "localhost"
    assert attrs["server.port"] == 11434


@pytest.mark.asyncio
async def test_server_address_and_port_emitted_for_ollama_with_remote_host() -> None:
    """Ollama with a remote `ollama_host` emits the operator-configured
    address+port faithfully — the static-map approach would have lied here."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"ollama": adapter},
        tracer_provider=tp,
        ollama_host="http://ollama.internal:8080",
    )

    await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["server.address"] == "ollama.internal"
    assert attrs["server.port"] == 8080


@pytest.mark.asyncio
async def test_server_address_and_port_emitted_for_ollama_with_host_only() -> None:
    """Ollama with host-only (no port) defaults to the SDK port 11434."""
    adapter = _OllamaFakeAdapter(_OllamaClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"ollama": adapter},
        tracer_provider=tp,
        ollama_host="ollama.internal",
    )

    await dispatcher.dispatch(_binding("ollama"), _step(), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["server.address"] == "ollama.internal"
    assert attrs["server.port"] == 11434


# ---------------------------------------------------------------------------
# AC #4 — anthropic.* conditional emission.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_cache_attributes_emitted_only_for_anthropic_provider() -> None:
    """anthropic.cache_* present for anthropic; absent for openai/ollama."""
    # Anthropic — attributes present.
    anth_tp, anth_exporter = _tracer_provider_with_exporter()
    anth = RuntimeLLMDispatcher(
        providers={"anthropic": _AnthropicFakeAdapter(_AnthropicClient())},
        tracer_provider=anth_tp,
    )
    await anth.dispatch(_binding("anthropic"), _step(), step_context=_step_context())
    anth_attrs = (anth_exporter.get_finished_spans()[0].attributes) or {}
    assert anth_attrs["anthropic.cache_creation_input_tokens"] == 2
    assert anth_attrs["anthropic.cache_read_input_tokens"] == 3

    # OpenAI — attributes absent.
    oa_tp, oa_exporter = _tracer_provider_with_exporter()
    oa = RuntimeLLMDispatcher(
        providers={"openai": _OpenAIFakeAdapter(_OpenAIClient())},
        tracer_provider=oa_tp,
    )
    await oa.dispatch(_binding("openai"), _step(), step_context=_step_context())
    oa_attrs = (oa_exporter.get_finished_spans()[0].attributes) or {}
    assert not any(k.startswith("anthropic.") for k in oa_attrs)


@pytest.mark.asyncio
async def test_anthropic_cache_breakpoint_id_and_ttl_extracted_from_request() -> None:
    """When the payload carries cache_control directives, breakpoint_id +
    ttl_seconds attributes are set per C-AS-14 §14.2.
    """
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "cached prefix",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                ],
            },
        ],
        "tools": None,
        "params": {"max_tokens": 100},
    }
    await dispatcher.dispatch(_binding("anthropic"), _step(payload), step_context=_step_context())

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["anthropic.cache_breakpoint_id"] == "msg-0"
    assert attrs["anthropic.cache_ttl_seconds"] == 3600


# ---------------------------------------------------------------------------
# AC #5 — RT-FAIL-PROVIDER-UNREACHABLE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_provider_raises_unreachable_error() -> None:
    """Provider not in `ctx.providers` → `LLMDispatchProviderUnreachableError`."""
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": _AnthropicFakeAdapter(_AnthropicClient())},
        tracer_provider=tp,
    )
    with pytest.raises(LLMDispatchProviderUnreachableError) as excinfo:
        await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())
    assert excinfo.value.provider_name == "openai"
    assert "RT-FAIL-PROVIDER-UNREACHABLE" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC #6 — Async-only invariant.
# ---------------------------------------------------------------------------


def test_dispatch_method_is_coroutine_function() -> None:
    """`RuntimeLLMDispatcher.dispatch` is async per C-RT-15 invariant."""
    assert inspect.iscoroutinefunction(RuntimeLLMDispatcher.dispatch)


# ---------------------------------------------------------------------------
# AC #7 — Factory binding.
# ---------------------------------------------------------------------------


def test_materialize_factory_builds_dispatcher() -> None:
    """`materialize_llm_dispatcher_stage` returns a `RuntimeLLMDispatcher`."""
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = materialize_llm_dispatcher_stage(
        providers={"anthropic": _AnthropicFakeAdapter(_AnthropicClient())},
        tracer_provider=tp,
    )
    assert isinstance(dispatcher, RuntimeLLMDispatcher)


def test_materialize_factory_raises_on_empty_providers() -> None:
    """Empty providers map → `LLMDispatchBindError` per X-AL-2 bounded contract."""
    tp, _ = _tracer_provider_with_exporter()
    with pytest.raises(LLMDispatchBindError):
        materialize_llm_dispatcher_stage(providers={}, tracer_provider=tp)


# ---------------------------------------------------------------------------
# Class 3 residual — payload shape error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mis_shaped_payload_raises_payload_shape_error() -> None:
    """Payload missing `messages` → `LLMDispatchPayloadShapeError`."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    bad_step = WorkflowStep(
        step_id=StepID("step-bad"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"not_messages": "oops"},
    )
    with pytest.raises(LLMDispatchPayloadShapeError):
        await dispatcher.dispatch(_binding("anthropic"), bad_step, step_context=_step_context())


# ---------------------------------------------------------------------------
# AS-8 anthropic.* 6-attr extension per C-AS-14 §14.2 rows 5-10.
# Closes the request-side + model-derived attr emission gap. Anthropic SDK
# parameter sources resolved via context7 at AS-8 discriminator audit.
# ---------------------------------------------------------------------------


async def _dispatch_with_payload(
    payload: dict[str, Any],
    *,
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    """Dispatch + return the gen_ai span's attribute dict."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)
    await dispatcher.dispatch(
        _binding("anthropic", model=model),
        _step(payload),
        step_context=_step_context(),
    )
    return dict((exporter.get_finished_spans()[0].attributes) or {})


def _default_payload(params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": None,
        "params": params or {"max_tokens": 100},
    }


@pytest.mark.asyncio
async def test_anthropic_tokenizer_version_always_emits_v1_for_non_opus_47() -> None:
    """§14.2 row 9 — strict reading: v2 only for Opus 4.7; else v1.

    `tokenizer_version` is always-emitted (the only non-optional of the 6
    new attrs); model-derived from the binding's `model` string.
    """
    attrs = await _dispatch_with_payload(_default_payload(), model="claude-sonnet-4-6")
    assert attrs["anthropic.tokenizer_version"] == "v1"


@pytest.mark.asyncio
async def test_anthropic_tokenizer_version_emits_v2_for_opus_47() -> None:
    """§14.2 row 9 — Opus 4.7 model strings get tokenizer_version=v2."""
    attrs = await _dispatch_with_payload(_default_payload(), model="claude-opus-4-7-20250101")
    assert attrs["anthropic.tokenizer_version"] == "v2"


@pytest.mark.asyncio
async def test_anthropic_thinking_mode_emits_when_thinking_config_present() -> None:
    """§14.2 row 5 — `thinking.type` from payload.params emits as enum string."""
    attrs = await _dispatch_with_payload(
        _default_payload(
            {"max_tokens": 100, "thinking": {"type": "enabled", "budget_tokens": 2048}}
        )
    )
    assert attrs["anthropic.thinking_mode"] == "enabled"


@pytest.mark.asyncio
async def test_anthropic_thinking_mode_omitted_when_thinking_absent() -> None:
    """§14.2 row 5 — optional field; omitted when payload lacks `thinking`."""
    attrs = await _dispatch_with_payload(_default_payload())
    assert "anthropic.thinking_mode" not in attrs


@pytest.mark.asyncio
async def test_anthropic_thinking_budget_tokens_emits_when_present() -> None:
    """§14.2 row 6 — `thinking.budget_tokens` from payload.params."""
    attrs = await _dispatch_with_payload(
        _default_payload(
            {"max_tokens": 100, "thinking": {"type": "enabled", "budget_tokens": 4096}}
        )
    )
    assert attrs["anthropic.thinking_budget_tokens"] == 4096


@pytest.mark.asyncio
async def test_anthropic_thinking_budget_tokens_omitted_when_thinking_absent() -> None:
    attrs = await _dispatch_with_payload(_default_payload())
    assert "anthropic.thinking_budget_tokens" not in attrs


@pytest.mark.asyncio
async def test_anthropic_thinking_effort_emits_when_output_config_effort_present() -> None:
    """§14.2 row 7 — `output_config.effort` is a beta SDK field (nested)."""
    attrs = await _dispatch_with_payload(
        _default_payload({"max_tokens": 100, "output_config": {"effort": "high"}})
    )
    assert attrs["anthropic.thinking_effort"] == "high"


@pytest.mark.asyncio
async def test_anthropic_thinking_effort_omitted_when_output_config_absent() -> None:
    attrs = await _dispatch_with_payload(_default_payload())
    assert "anthropic.thinking_effort" not in attrs


@pytest.mark.asyncio
async def test_anthropic_batch_id_emits_when_operator_supplies_marker() -> None:
    """§14.2 row 8 — batch_id is operator-supplied out-of-band marker (Batch
    API submission). Not in the synchronous messages.create SDK params.
    """
    attrs = await _dispatch_with_payload(
        _default_payload({"max_tokens": 100, "batch_id": "batch_test_001"})
    )
    assert attrs["anthropic.batch_id"] == "batch_test_001"


@pytest.mark.asyncio
async def test_anthropic_batch_id_omitted_when_not_supplied() -> None:
    attrs = await _dispatch_with_payload(_default_payload())
    assert "anthropic.batch_id" not in attrs


@pytest.mark.asyncio
async def test_anthropic_inference_geo_emits_when_supplied() -> None:
    """§14.2 row 10 — `inference_geo` from payload.params (data-residency)."""
    attrs = await _dispatch_with_payload(
        _default_payload({"max_tokens": 100, "inference_geo": "us"})
    )
    assert attrs["anthropic.inference_geo"] == "us"


@pytest.mark.asyncio
async def test_anthropic_inference_geo_omitted_when_absent() -> None:
    attrs = await _dispatch_with_payload(_default_payload())
    assert "anthropic.inference_geo" not in attrs


@pytest.mark.asyncio
async def test_anthropic_six_attrs_all_emit_together_when_payload_complete() -> None:
    """Integration: when payload supplies all optional sources, all 6 attrs land."""
    attrs = await _dispatch_with_payload(
        _default_payload(
            {
                "max_tokens": 100,
                "thinking": {"type": "enabled", "budget_tokens": 2048},
                "output_config": {"effort": "medium"},
                "batch_id": "batch_test_002",
                "inference_geo": "us",
            }
        ),
        model="claude-opus-4-7-20260101",
    )
    assert attrs["anthropic.thinking_mode"] == "enabled"
    assert attrs["anthropic.thinking_budget_tokens"] == 2048
    assert attrs["anthropic.thinking_effort"] == "medium"
    assert attrs["anthropic.batch_id"] == "batch_test_002"
    assert attrs["anthropic.tokenizer_version"] == "v2"
    assert attrs["anthropic.inference_geo"] == "us"


@pytest.mark.asyncio
async def test_anthropic_six_attrs_absent_for_non_anthropic_providers() -> None:
    """Per AS-AL-3 — anthropic.* emitted ONLY when provider=='anthropic'."""
    oa_tp, oa_exporter = _tracer_provider_with_exporter()
    oa = RuntimeLLMDispatcher(
        providers={"openai": _OpenAIFakeAdapter(_OpenAIClient())},
        tracer_provider=oa_tp,
    )
    await oa.dispatch(_binding("openai"), _step(), step_context=_step_context())
    oa_attrs = (oa_exporter.get_finished_spans()[0].attributes) or {}
    for key in (
        "anthropic.thinking_mode",
        "anthropic.thinking_budget_tokens",
        "anthropic.thinking_effort",
        "anthropic.batch_id",
        "anthropic.tokenizer_version",
        "anthropic.inference_geo",
    ):
        assert key not in oa_attrs


# ---------------------------------------------------------------------------
# R-300 — layered routing-selection activation (live INFERENCE_STEP dispatch).
#
# `dispatch()` is the registered INFERENCE_STEP dispatcher (stage_5_loop_init
# `StepKindDispatcherRegistry`). These tests exercise the live dispatch path
# (dispatch -> infer -> route -> _invoke_provider -> span) with an in-process
# fake provider (mech-α; NO paid call). The e2e form of R-300 must_pass #2 is
# asserted here against the production-observable `routing.layer` span attr;
# the `InferenceResponse.routing_decision.layer` object-shape form is asserted
# at the unit level in `test_routing_core_surface.py`
# (`test_infer_declarative_layer_is_manifest`) because the live path discards
# the InferenceResponse (the span is the canonical routing-visibility surface).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_attributes_emitted_with_manifest_layer() -> None:
    """R-300 must_pass #2 (e2e split) — the live INFERENCE_STEP dispatch routes
    through `infer()`/`route()` and emits the full C-CP-01 §1.4 `routing.*` set
    on the `llm.inference` span, with `routing.layer == "manifest"` on the
    DECLARATIVE-echo hit."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    # must_pass #2 — DECLARATIVE manifest layer hit.
    assert attrs["routing.layer"] == "manifest"
    # Full §1.4 set present (routing visibility survives the discarded
    # InferenceResponse).
    assert attrs["routing.provider"] == "anthropic"
    assert attrs["routing.model"] == "claude-opus-4-8"
    assert str(attrs["routing.binding_rationale"]).startswith("manifest:")


# ---------------------------------------------------------------------------
# R-FS-1 R-impl-1 — U-RT-133 + U-RT-132 (Layer-3 LLM_AS_ROUTER runtime e2e).
#
# The spec-§2.5.5 runtime mock-router proof. The binding site hardcodes
# `_declarative_echo` (always resolves), so the test-only `layer_decisions`
# seam (default = production) is needed to force the route() L3 sentinel and
# reach the injected `router`. NO paid call (in-process fake provider + mock
# router). Non-test production reachability additionally needs DECLARATIVE made
# conditional (R-300-second-provider) — out of R scope (runtime v2.48 §6 O-RT-8).
# ---------------------------------------------------------------------------


def _force_fallthrough(_payload: object, _manifest: object) -> str | None:
    """A DECLARATIVE LayerDecisionFn that returns no candidate -> route()
    falls through to the LLM_AS_ROUTER sentinel (the test-only seam)."""
    return None


@pytest.mark.asyncio
async def test_layer3_router_e2e_emits_router_supplied_rationale() -> None:
    """U-RT-133 + U-RT-132 / CP spec v1.36 §2.5.5 (a)/(b) — via the test-only
    `layer_decisions` seam, a sentinel-forcing DECLARATIVE + an injected mock
    router resolve Layer 3 through `RuntimeLLMDispatcher`; the `llm.inference`
    span carries routing.layer=='llm_as_router' + the router-supplied
    routing.binding_rationale (NOT the f'{layer}:{candidate}' fallback). Proves
    the runtime binding CAN carry a router. NO paid call."""

    async def _router(_request: object, _summary: str) -> RouterResolution:
        return RouterResolution(candidate="anthropic:claude-haiku-4-5", rationale="cost-tier match")

    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        router=_router,
        layer_decisions_override={RoutingLayer.DECLARATIVE: _force_fallthrough},
    )

    await dispatcher.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    # `[0]` is the sole finished span ONLY because the mock `_router` emits no
    # span of its own. R-impl-2's vendor-gated real router will open a child
    # `llm.inference` span for the router-model call, after which `[0]` becomes
    # ambiguous — that arc MUST select the workload span explicitly (e.g. by
    # span name) rather than relying on ordinal `[0]`.
    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["routing.layer"] == RoutingLayer.LLM_AS_ROUTER.value
    assert attrs["routing.provider"] == "anthropic"
    assert attrs["routing.model"] == "claude-haiku-4-5"
    # §2.5.4 — the router-supplied rationale, NOT the layer:candidate fallback.
    assert attrs["routing.binding_rationale"] == "cost-tier match"


@pytest.mark.asyncio
async def test_layer3_no_router_through_dispatch_preserves_raise() -> None:
    """U-RT-133 no-regress — the seam forces the L3 sentinel but `router`
    defaults None (production) -> the preserved RoutingCandidateUnresolvedError
    propagates through `dispatch` unmodified (no silent swallow)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        layer_decisions_override={RoutingLayer.DECLARATIVE: _force_fallthrough},
        # `router` defaults None -> the Layer-3 surface is inert (production).
    )

    with pytest.raises(RoutingCandidateUnresolvedError):
        await dispatcher.dispatch(_binding("anthropic", "x"), _step(), step_context=_step_context())


def test_dispatcher_budgets_field_defaults_to_module_default() -> None:
    """B-LAYER-BUDGET-OVERRIDE — the `budgets` construction seam defaults to the
    module-global `DEFAULT_LAYER_BUDGETS` (byte-identical to the prior hardcoded
    `infer(budgets=DEFAULT_LAYER_BUDGETS)`). Production stage-5 leaves it default
    (no override surface wired); the seam is dormant."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)
    assert dispatcher.budgets is DEFAULT_LAYER_BUDGETS


@pytest.mark.asyncio
async def test_layer3_budget_override_threads_through_dispatcher_seam() -> None:
    """B-LAYER-BUDGET-OVERRIDE — the DORMANT `budgets`-threading seam:
    `RuntimeLLMDispatcher(budgets=...)` reaches the `infer()` L3 timeout, where
    the §3.1 per-persona override (keyed on the envelope's persona_tier, which
    defaults to SOLO_DEVELOPER) governs. Through the test-only `layer_decisions`
    seam + an injected slow router this proves `self.budgets` threads end-to-end
    and the override resolves through the real dispatcher path. NOT a production
    claim: production binds `router=None` + the DEFAULT budgets, so L3 is inert
    and no override surface is wired (the routing-activation gate is UNOWNED)."""

    async def _slow_router(_request: object, _summary: str) -> RouterResolution:
        await asyncio.sleep(0.03)
        return RouterResolution(candidate="anthropic:claude-haiku-4-5", rationale="r")

    # Flat L3 default HUGE (5 s) so ONLY the 1 ms persona override can time the
    # 30 ms router out -> proves the threaded override (not the flat) governs.
    override_budgets = (
        LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
        LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
        LayerBudget(
            layer=RoutingLayer.LLM_AS_ROUTER,
            time_budget_ms=5000,
            per_persona_override={PersonaTier.SOLO_DEVELOPER: 1},
        ),
    )
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        router=_slow_router,
        layer_decisions_override={RoutingLayer.DECLARATIVE: _force_fallthrough},
        budgets=override_budgets,
    )

    with pytest.raises(RoutingCandidateUnresolvedError):
        await dispatcher.dispatch(_binding("anthropic", "x"), _step(), step_context=_step_context())


@pytest.mark.asyncio
async def test_routing_selection_is_behavior_preserving_at_mvp_echo() -> None:
    """R-300 — the DECLARATIVE echo selects the resolved `binding.model_binding`,
    so the routed provider/model reaching the provider SDK is unchanged from the
    pre-activation static path (behavior-preserving at MVP)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    assert adapter.client.messages.last_kwargs is not None
    # The routed model == the binding model (echo), not perturbed by routing.
    assert adapter.client.messages.last_kwargs["model"] == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# B-L2-EMBEDDING-ACTIVATION (C-CP-02 §2.2 — the routing-activation gate)
# ---------------------------------------------------------------------------
def _stub_embedding_classifier(_payload: object, _manifest: object) -> str | None:
    """A deterministic stub Layer-2 classifier (NO fastembed) that always selects a
    non-default candidate — the EMBEDDING route the §2.2-conditional DECLARATIVE
    decline must reach. Substitutes for the real k-NN classifier in the witness."""
    return "anthropic:claude-haiku-4-5"


@pytest.mark.asyncio
async def test_routing_activation_declines_declarative_on_manifest_miss_routes_embedding() -> None:
    """B-L2-FALLBACK-COMPOSITION (§14.6) — the route-once SELECTION
    `resolve_routed_binding`: routing_activation on + a manifest that does NOT bind
    the request's tuple + a bound EMBEDDING classifier → DECLARATIVE declines →
    `route()` falls through to EMBEDDING → the routed ModelBinding is the EMBEDDING
    pick (haiku), NOT the default binding (opus). The C-RT-16 wrapper seeds this as
    its PRIMARY candidate (the end-to-end chain composition lives in
    `test_lifecycle_retry_breaker_fallback.py`)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        routing_manifest=_routing_manifest_with_roles({}),  # binds nothing → miss
        embedding_classifier=_stub_embedding_classifier,
    )

    routed = await dispatcher.resolve_routed_binding(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    # DECLARATIVE declined (manifest-miss) → EMBEDDING picked haiku, NOT the opus echo.
    # B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION: the resolver now returns a
    # RoutedPrimaryResolution carrying the routed model + the resolving trace.
    assert routed is not None
    assert routed.model_binding == ModelBinding(provider="anthropic", model="claude-haiku-4-5")
    assert routed.routing_trace.layer == "embedding"


@pytest.mark.asyncio
async def test_routing_activation_off_declarative_echoes_default_zero_blast_radius() -> None:
    """The NEGATIVE CONTROL: routing_activation OFF (default) → `resolve_routed_binding`
    returns None (no routing augmentation) even with a manifest-miss + a bound
    classifier → the wrapper uses the existing per-role / stage chain and the inner
    `dispatch` echoes the default binding (opus). Proves default-off is byte-identical
    / zero blast radius."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        # routing_activation defaults False
        routing_manifest=_routing_manifest_with_roles({}),
        embedding_classifier=_stub_embedding_classifier,  # bound but unreached
    )

    routed = await dispatcher.resolve_routed_binding(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )
    # routing_activation off → no augmentation; the inner dispatch echoes the default.
    assert routed is None
    await dispatcher.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )
    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_routing_activation_manifest_binds_keeps_declarative() -> None:
    """The §2.2 "manifest binds → DECLARATIVE" half: routing_activation on + a
    manifest that DOES bind the request's NON-default role → DECLARATIVE resolves
    → `resolve_routed_binding` returns None (the per-role binding IS the wrapper's
    chain primary, no augmentation; per-role U-RT-114 augmentation takes
    precedence). Decline is driven by manifest-membership of a NON-default role,
    and mirrors `_effective_chain`'s per-role branch EXACTLY (B-MODEL-RESOLUTION-
    CONSOLIDATION §14.6.2 — the default role is dead config per §14.5.3; see the
    default-role witness below)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        # binds a NON-default role ("reviewer") → DECLARATIVE resolves for it.
        routing_manifest=_routing_manifest_with_roles({"reviewer": "claude-opus-4-8"}),
        embedding_classifier=_stub_embedding_classifier,
    )

    routed = await dispatcher.resolve_routed_binding(
        _binding("anthropic", "claude-opus-4-8"),
        _step(),
        step_context=_step_context_with_role("reviewer"),
    )
    assert routed is None


@pytest.mark.asyncio
async def test_routing_activation_default_role_binding_does_not_block_embedding() -> None:
    """B-MODEL-RESOLUTION-CONSOLIDATION §14.6.2 — a `per_role_bindings` entry for the
    DEFAULT role is dead config (the wrapper's `_effective_chain` early-skips the
    default role per the §14.5.3 non-breaking-default invariant). So a default-role
    binding must NOT count as a deterministic model binding at decline either (else
    the decline would be STRICTER than the authority — routing declines while the
    authority skips → silent drop to default). DECLARATIVE declines → EMBEDDING
    routes to the classifier pick (haiku)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        # binds the DEFAULT role → dead config; must NOT decline.
        routing_manifest=_routing_manifest_with_roles({"default": "claude-opus-4-8"}),
        embedding_classifier=_stub_embedding_classifier,
    )
    routed = await dispatcher.resolve_routed_binding(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )
    assert routed is not None
    assert routed.model_binding == ModelBinding(provider="anthropic", model="claude-haiku-4-5")


def test_factory_threads_routing_activation_and_injected_classifier() -> None:
    """The factory threads routing_activation + an injected classifier to the
    dispatcher (the injected seam lets tests + an operator corpus override bypass the
    default fastembed build, so no `[embedding]` extra is touched). Default-off leaves
    both at their inert defaults."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    on = materialize_llm_dispatcher_stage(
        {"anthropic": adapter},
        cast(Any, tp),
        routing_activation=True,
        embedding_classifier=_stub_embedding_classifier,
    )
    assert on.routing_activation is True
    assert on.embedding_classifier is _stub_embedding_classifier
    # Default-off: flag off + no classifier (byte-identical, no fastembed touch).
    off = materialize_llm_dispatcher_stage({"anthropic": adapter}, cast(Any, tp))
    assert off.routing_activation is False
    assert off.embedding_classifier is None


@pytest.mark.asyncio
async def test_routing_activation_per_step_model_override_pins_declarative_not_embedding() -> None:
    """B-MODEL-RESOLUTION-CONSOLIDATION §14.6.2 (was the Codex [P2] `override_applied`
    regression): routing_activation on + a per-step MODEL override
    (`binding.model_binding_override` set) + a manifest-MISS → `resolve_routed_binding`
    returns None (DECLARATIVE PINNED — the operator's per-step MODEL choice is honored
    at the HEAD of the precedence, NOT routed to EMBEDDING). The decline now keys on
    the MODEL-specific signal, mirroring `_effective_chain`'s per-step branch (the
    coarse `override_applied` proxy over-declined on a non-model override — see the
    non-model companion below)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({}),  # manifest-miss
        routing_activation=True,
        embedding_classifier=_stub_embedding_classifier,
    )
    overridden = _binding("anthropic", "claude-opus-4-8").model_copy(
        update={
            "override_applied": True,
            "model_binding_override": ModelBinding(provider="anthropic", model="claude-opus-4-8"),
        }
    )
    routed = await dispatcher.resolve_routed_binding(
        overridden, _step(), step_context=_step_context()
    )
    # per-step MODEL override → DECLARATIVE pinned → no routing augmentation (None);
    # the wrapper's `_effective_chain` dispatches the per-step opus (full-chain
    # witness in test_lifecycle_retry_breaker_fallback.py) — never hijacked to haiku.
    assert routed is None


@pytest.mark.asyncio
async def test_routing_activation_non_model_per_step_override_does_not_block_embedding() -> None:
    """B-MODEL-RESOLUTION-CONSOLIDATION §14.6.2 — routing_activation on + a per-step
    override with NO model dimension (`override_applied=True` but
    `model_binding_override` None — e.g. an hitl/engine-only override) + a role-miss →
    DECLARATIVE STILL declines (a non-model per-step override must NOT count as a
    deterministic model binding; the prior coarse `override_applied` proxy
    over-declined here, spuriously suppressing routing) → EMBEDDING routes to the
    classifier pick (haiku). The per-step analog of the existing non-model-WORKLOAD
    witness."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({}),  # role-miss
        routing_activation=True,
        embedding_classifier=_stub_embedding_classifier,
    )
    overridden = _binding("anthropic", "claude-opus-4-8").model_copy(
        update={"override_applied": True}  # NO model_binding_override
    )
    routed = await dispatcher.resolve_routed_binding(
        overridden, _step(), step_context=_step_context()
    )
    # non-model per-step override does NOT count → DECLARATIVE declines → EMBEDDING
    # picks haiku → the wrapper's routed PRIMARY candidate.
    assert routed is not None
    assert routed.model_binding == ModelBinding(provider="anthropic", model="claude-haiku-4-5")


@pytest.mark.asyncio
async def test_routing_activation_non_model_workload_override_does_not_block_embedding() -> None:
    """Codex [P2] regression: routing_activation on + a per_workload_overrides entry
    that binds NO model (engine/sandbox only) + a role-miss → DECLARATIVE still
    declines (a non-model workload override must NOT count as a deterministic model
    binding) → EMBEDDING routes to the classifier pick (haiku)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    manifest = RoutingManifest(
        manifest_version=1,
        per_role_bindings={},  # role-miss
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: WorkloadRoutingOverride()  # no model
        },
        fallback_chains=(),
        retry_policies={},
    )
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,  # matches the override key
        routing_manifest=manifest,
        embedding_classifier=_stub_embedding_classifier,
    )
    routed = await dispatcher.resolve_routed_binding(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    # The non-model workload override does NOT count → DECLARATIVE declines → EMBEDDING
    # picks haiku → the wrapper's routed PRIMARY candidate.
    assert routed is not None
    assert routed.model_binding == ModelBinding(provider="anthropic", model="claude-haiku-4-5")


@pytest.mark.asyncio
async def test_routing_activation_model_bearing_workload_override_hits_declarative() -> None:
    """A model-BEARING per_workload_overrides entry counts as a DECLARATIVE hit (so
    EMBEDDING cannot hijack a pinned workload) → `resolve_routed_binding` declines
    (None) because the per-workload model governs at the wrapper. Under
    B-MODEL-RESOLUTION-CONSOLIDATION the wrapper's `_effective_chain` NOW CONSUMES the
    override (dispatches gpt-5.5; full-chain witness in
    test_lifecycle_retry_breaker_fallback.py) — closing the pre-consolidation
    unconsumed `B-ROUTING-MANIFEST-MODEL-FOLD` status-quo."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    manifest = RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: WorkloadRoutingOverride(
                model_binding_override=ModelBinding(provider="openai", model="gpt-5.5")
            )
        },
        fallback_chains=(),
        retry_policies={},
    )
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        routing_manifest=manifest,
        embedding_classifier=_stub_embedding_classifier,
    )
    routed = await dispatcher.resolve_routed_binding(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    # Model-bearing workload override → DECLARATIVE hit (NOT EMBEDDING) → routing
    # declines (None) because the per-workload model governs; the wrapper's
    # `_effective_chain` now dispatches the override (gpt-5.5) per the consolidation
    # (full-chain witness in test_lifecycle_retry_breaker_fallback.py).
    assert routed is None


def test_factory_admits_routing_activation_with_fallback_chains() -> None:
    """B-L2-FALLBACK-COMPOSITION (§14.6): routing_activation NOW composes with the
    C-RT-16 fallback chain — the routing decision is made ONCE at the wrapper (via
    `resolve_routed_binding`) and seeds the PRIMARY candidate, so the prior
    B-L2-EMBEDDING-ACTIVATION detect-then-refuse guard (which raised
    `LLMDispatchBindError` on routing_activation + non-empty fallback_chains) is
    RETIRED. The factory ADMITS both; the inner carries routing_activation. The
    end-to-end chain-advancement + route-once witnesses live in
    `test_lifecycle_retry_breaker_fallback.py`."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    manifest = RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(_chain(_candidate("anthropic", "claude-haiku-4-5")),),
        retry_policies={},
    )
    # No longer raises — the composition arc landed (an injected classifier avoids
    # touching the optional fastembed `[embedding]` extra in this unit test).
    on = materialize_llm_dispatcher_stage(
        {"anthropic": adapter},
        cast(Any, tp),
        routing_activation=True,
        embedding_classifier=_stub_embedding_classifier,
        routing_manifest=manifest,
    )
    assert on.routing_activation is True
    # The same fallback manifest is likewise fine when routing_activation is off
    # (byte-identical to pre-B-L2 — the inner dispatch echoes, the wrapper fallbacks).
    off = materialize_llm_dispatcher_stage(
        {"anthropic": adapter}, cast(Any, tp), routing_manifest=manifest
    )
    assert off.routing_activation is False


# ---------------------------------------------------------------------------
# U-RT-114 — branch AgentRole carry; MODEL selection is ROLE-AGNOSTIC at the
# inner C-RT-15 dispatcher — C-RT-15 §14.5.3 ([P2-a] placement)
# ---------------------------------------------------------------------------
#
# The inner dispatcher ALWAYS dispatches `binding.model_binding` (the candidate
# it is handed) and carries `step_context.agent_role` into the InferenceRequest
# envelope for attribution only. The per-role MODEL dispatch-read (§14.5.3)
# lives ONE LAYER OUT, at the C-RT-16 `RetryBreakerFallbackDispatcher`, where the
# per-role model is promoted to the PRIMARY fallback candidate so it composes
# with fallback (ONE source of truth for model selection — the wrapper's chain).
# See `test_lifecycle_retry_breaker_fallback.py` for the per-role routing +
# [P2-a] fallback-preservation tests. The tests below prove the inner is
# role-AGNOSTIC: even a populated `per_role_bindings` catalog does NOT perturb
# the inner's dispatched model. The fake adapter records the dispatched `model`
# (`last_kwargs["model"]`).


def _routing_manifest_with_roles(role_models: dict[str, str]) -> RoutingManifest:
    """A `RoutingManifest` whose `per_role_bindings` maps each role to an
    anthropic ModelBinding at the named model (the only field U-RT-114 reads)."""
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole(role): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider="anthropic", model=model),
                layer_budget_overrides={},
            )
            for role, model in role_models.items()
        },
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


def _step_context_with_role(role: str | None) -> StepExecutionContext:
    """A step_context with the branch `agent_role` set (or `None` = the linear /
    default path). `branch_index` is irrelevant to the U-RT-114 dispatch-read."""
    return _step_context().model_copy(
        update={"agent_role": AgentRole(role) if role is not None else None}
    )


@pytest.mark.asyncio
async def test_u_rt_114_absent_role_falls_through_to_binding() -> None:
    """`agent_role is None` (the SINGLE_THREADED_LINEAR / no-branch path) → the
    CP-resolved `binding.model_binding` is dispatched (byte-identical to v1.47),
    even when the catalog has entries for OTHER roles."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )

    await dispatcher.dispatch(
        _binding("anthropic", "binding-default-model"),
        _step(),
        step_context=_step_context_with_role(None),
    )

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "binding-default-model"


@pytest.mark.asyncio
async def test_u_rt_114_explicit_default_role_falls_through() -> None:
    """The literal `"default"` role NEVER consults the catalog (even if it holds a
    `"default"` entry) → the CP-resolved binding is dispatched (the AC's
    `"default"` role byte-identical-to-v1.47 case)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({"default": "catalog-default-model"}),
    )

    await dispatcher.dispatch(
        _binding("anthropic", "binding-default-model"),
        _step(),
        step_context=_step_context_with_role("default"),
    )

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "binding-default-model"


@pytest.mark.asyncio
async def test_u_rt_114_inner_model_selection_is_role_agnostic() -> None:
    """A NON-default branch role PRESENT in the catalog → the inner STILL
    dispatches `binding.model_binding` (NOT the per-role model). This is the
    [P2-a] placement guarantee: per-role MODEL selection moved OUT of the inner
    to the C-RT-16 wrapper, so the inner faithfully dispatches the rebound
    candidate and never overrides it — overriding here would silently defeat
    fallback for role-routed branches (two authorities for one decision).
    Per-role routing itself is asserted at the wrapper test module."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )

    await dispatcher.dispatch(
        _binding("anthropic", "binding-default-model"),
        _step(),
        step_context=_step_context_with_role("worker-a"),
    )

    assert adapter.client.messages.last_kwargs is not None
    # The per-role catalog is IGNORED by the inner — binding.model_binding wins.
    assert adapter.client.messages.last_kwargs["model"] == "binding-default-model"


@pytest.mark.asyncio
async def test_u_rt_114_role_absent_from_catalog_falls_through() -> None:
    """A non-default role NOT present in the catalog → fall through to the
    CP-resolved binding (non-breaking default)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )

    await dispatcher.dispatch(
        _binding("anthropic", "binding-default-model"),
        _step(),
        step_context=_step_context_with_role("worker-z"),  # not in catalog
    )

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "binding-default-model"


@pytest.mark.asyncio
async def test_u_rt_114_empty_catalog_falls_through() -> None:
    """An empty `per_role_bindings` catalog → every dispatch falls through to the
    CP-resolved binding (byte-identical to v1.47), regardless of the role."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles({}),
    )

    await dispatcher.dispatch(
        _binding("anthropic", "binding-default-model"),
        _step(),
        step_context=_step_context_with_role("worker-a"),
    )

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "binding-default-model"


@pytest.mark.asyncio
async def test_u_rt_114_inner_linear_none_role_dispatches_binding_model() -> None:
    """`agent_role is None` (the SINGLE_THREADED_LINEAR / no-branch path) → the
    inner dispatches `binding.model_binding` (byte-identical to v1.47). The
    per-role distinct-worker routing + live-ollama + [P2-a] fallback regression
    moved to `test_lifecycle_retry_breaker_fallback.py` (the wrapper layer)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_manifest=_routing_manifest_with_roles(
            {"worker-a": "role-model-a", "worker-b": "role-model-b"}
        ),
    )

    await dispatcher.dispatch(
        _binding("anthropic", "binding-default-model"),
        _step(),
        step_context=_step_context_with_role(None),
    )

    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["model"] == "binding-default-model"


# ---------------------------------------------------------------------------
# R-FS-1 L2 — EMBEDDING layer (Option B: in-process sync embedding classifier).
#
# The classifier is a sync LayerDecisionFn bound at the EMBEDDING layer. Two
# complementary proofs: (1) an embedding decision drives the dispatched
# provider:model end-to-end through `dispatch` (in-process, NO embedding lib —
# a stub embed); (2) the production `embedding_classifier` field, when set,
# actually lands RoutingLayer.EMBEDDING in the `layer_decisions` map `infer()`
# consumes (the #496-class seam-reachability guard — assert membership, not just
# behavior). Production reachability additionally needs DECLARATIVE made
# conditional (R-300) — same inertness as the L3 router (runtime v2.48 §6 O-RT-8).
# ---------------------------------------------------------------------------

_L2_VOCAB = ("code", "python", "function", "creative", "poem", "story")


def _l2_stub_embed(text: str) -> tuple[float, ...]:
    low = text.lower()
    return tuple(1.0 if w in low else 0.0 for w in _L2_VOCAB)


def _l2_classifier(k: int = 1) -> LayerDecisionFn:
    corpus = EmbeddingRoutingCorpus.from_pairs(
        [
            ("python code function", "anthropic:claude-opus-4-8", "software-engineering"),
            ("creative poem story", "anthropic:claude-haiku-4-5", "content-creation"),
        ]
    )
    return make_embedding_classifier(embed=_l2_stub_embed, corpus=corpus, k=k)


@pytest.mark.asyncio
async def test_l2_embedding_decision_drives_dispatch() -> None:
    """The EMBEDDING classifier (bound via the layer_decisions seam, DECLARATIVE
    omitted so EMBEDDING is reached) selects the candidate the call-site text is
    semantically nearest — the dispatched provider:model is the classifier's
    pick, NOT the step binding's model. routing.layer=='embedding'. NO dep."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        layer_decisions_override={RoutingLayer.EMBEDDING: _l2_classifier()},
    )

    # The call-site text matches the content-creation exemplar -> the classifier
    # selects claude-haiku-4-5 (NOT the binding's claude-opus-4-8).
    await dispatcher.dispatch(
        _binding("anthropic", "claude-opus-4-8"),
        _step(
            {
                "messages": [{"role": "user", "content": "write a creative poem"}],
                "tools": None,
                "params": {"max_tokens": 10},
            }
        ),
        step_context=_step_context(),
    )
    attrs = (exporter.get_finished_spans()[0].attributes) or {}
    assert attrs["routing.layer"] == RoutingLayer.EMBEDDING.value  # "embedding"
    assert attrs["routing.provider"] == "anthropic"
    assert attrs["routing.model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_l2_embedding_classifier_field_binds_embedding_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seam-reachability guard (#496 class), relocated to the route-once SELECTION:
    the production `embedding_classifier` field, when set, binds RoutingLayer.EMBEDDING
    into the `layer_decisions` map the route-once `resolve_routed_binding` consumes
    (B-L2-FALLBACK-COMPOSITION — routing selection moved OFF the now-faithful inner
    `dispatch`). Spy-WRAP `resolve_routing_trace` (don't replace) so the routed
    selection is preserved while the map is captured. The classifier here always
    resolves (the assertion is the MAP binding, not the k-NN classification)."""
    classifier: LayerDecisionFn = _stub_embedding_classifier
    captured: dict[str, Any] = {}
    real_resolve = llm_dispatch_module.resolve_routing_trace

    async def _spy_resolve(*args: Any, **kwargs: Any) -> Any:
        captured["layer_decisions"] = kwargs["layer_decisions"]
        return await real_resolve(*args, **kwargs)

    monkeypatch.setattr(llm_dispatch_module, "resolve_routing_trace", _spy_resolve)

    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        routing_manifest=_routing_manifest_with_roles({}),  # miss → reaches EMBEDDING
        embedding_classifier=classifier,  # the production field
    )
    await dispatcher.resolve_routed_binding(
        _binding("anthropic"), _step(), step_context=_step_context()
    )

    layer_decisions = captured["layer_decisions"]
    assert RoutingLayer.DECLARATIVE in layer_decisions
    assert RoutingLayer.EMBEDDING in layer_decisions
    assert layer_decisions[RoutingLayer.EMBEDDING] is classifier


@pytest.mark.asyncio
async def test_l2_absent_classifier_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No regression: with embedding_classifier=None (the production default),
    the production layer_decisions map is exactly {DECLARATIVE: echo} — no
    EMBEDDING entry (byte-identical to pre-L2)."""
    captured: dict[str, Any] = {}
    real_infer = llm_dispatch_module.infer

    async def _spy_infer(*args: Any, **kwargs: Any) -> Any:
        captured["layer_decisions"] = kwargs["layer_decisions"]
        return await real_infer(*args, **kwargs)

    monkeypatch.setattr(llm_dispatch_module, "infer", _spy_infer)

    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)
    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    assert set(captured["layer_decisions"].keys()) == {RoutingLayer.DECLARATIVE}


# ---------------------------------------------------------------------------
# B4 (C-RT-15 §14.5.3) — per-role PROMPT dispatch-read. The dispatcher indexes
# `step_context.agent_role` into `per_role_system_prompts`; a bound role injects
# its own system prompt, an unbound role (incl. default/linear) falls through to
# `active_system_prompt`. Verified by the recorded provider-client `system=`
# kwarg through the real `dispatch(...)` path (the §14.5.2 acceptance shape).
# ---------------------------------------------------------------------------

_DEFAULT_SYS = "DEFAULT-default-role-prompt"
_RESEARCHER_SYS = "RESEARCHER-per-role-prompt"


@pytest.mark.asyncio
async def test_b4_per_role_prompt_bound_vs_unbound_differ_in_same_run() -> None:
    """THE non-vacuous B4 e2e: in ONE dispatcher, a fan-out branch bound to a
    per-role prompt (`"researcher"`) injects its OWN `system=`, while an UNBOUND
    role (`"writer"`) falls through to the default-role `active_system_prompt`.
    The bound key matches the role the driver composes on the branch context."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_DEFAULT_SYS,
        per_role_system_prompts={AgentRole("researcher"): _RESEARCHER_SYS},
    )

    # Bound role → its per-role prompt.
    await dispatcher.dispatch(
        _binding("anthropic"), _step(), step_context=_step_context_with_role("researcher")
    )
    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["system"] == _RESEARCHER_SYS

    # Unbound role, SAME dispatcher → fall-through to the default-role prompt.
    await dispatcher.dispatch(
        _binding("anthropic"), _step(), step_context=_step_context_with_role("writer")
    )
    assert adapter.client.messages.last_kwargs["system"] == _DEFAULT_SYS


@pytest.mark.asyncio
async def test_b4_per_role_prompt_linear_path_untouched() -> None:
    """§14.5.3 invariant: the linear / no-branch path (`agent_role is None`) — and
    the literal `"default"` role — never consult the per-role map, so they inject
    the default-role `active_system_prompt` verbatim, even with a populated map."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_DEFAULT_SYS,
        per_role_system_prompts={AgentRole("researcher"): _RESEARCHER_SYS},
    )

    await dispatcher.dispatch(
        _binding("anthropic"), _step(), step_context=_step_context_with_role(None)
    )
    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["system"] == _DEFAULT_SYS


@pytest.mark.asyncio
async def test_b4_empty_per_role_map_is_byte_identical_to_default() -> None:
    """Empty `per_role_system_prompts` (no per-role bindings — the default) → every
    role falls through to `active_system_prompt` (byte-identical to pre-B4)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_DEFAULT_SYS,
    )

    await dispatcher.dispatch(
        _binding("anthropic"), _step(), step_context=_step_context_with_role("researcher")
    )
    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["system"] == _DEFAULT_SYS


# ---------------------------------------------------------------------------
# B4 Slice 2 — the catalog CONJUNCTION e2e. Slice 1 + B1 proved per-role PROMPT
# (inner) and per-role MODEL (wrapper) each in isolation. This proves the
# *catalog* claim the dossier states verbatim: a populated catalog routes
# DISTINCT workers to distinct models AND prompts, through the SAME real
# dispatch stack the bootstrap composes (RetryBreakerFallbackDispatcher[model
# layer] over RuntimeLLMDispatcher[prompt layer]). The two worker roles are
# derived via the single-source-of-truth `derive_agent_role` contract — the same
# function the fan-out driver composes on — so the operator catalog and the
# driver agree by construction.
# ---------------------------------------------------------------------------


def _candidate(provider: str, model: str) -> ProviderCandidate:
    family_map = {
        "anthropic": ProviderFamily.ANTHROPIC,
        "openai": ProviderFamily.OPENAI,
        "ollama": ProviderFamily.LOCAL_OPEN_WEIGHT,
    }
    return ProviderCandidate(provider=provider, model=model, family=family_map[provider])


def _chain(primary: ProviderCandidate) -> FallbackChain:
    return FallbackChain(primary=primary, same_family=(), cross_family=(), terminal=None)


def _retry_breaker_with_llm_policy() -> RuntimeRetryBreaker:
    """Mirror of `materialize_retry_breaker_stage` — the reserved LLM-dispatch
    policy pre-bound, fast/deterministic delays for tests."""
    return RuntimeRetryBreaker(
        retry_policies={
            RESERVED_LLM_DISPATCH_KEY: RetryPolicy(
                max_attempts=1, backoff="full_jitter", jitter="full_jitter"
            )
        },
        default_policy=DEFAULT_RETRY_POLICY,
        base_delay_seconds=0.0,
        delay_cap_seconds=0.01,
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_b4_slice2_distinct_workers_distinct_models_and_prompts_e2e() -> None:
    """THE B4-Slice-2 conjunction proof. Two fan-out workers whose roles are
    DERIVED from their `step_id`s via the B1↔B4 contract get, through one real
    dispatch stack, BOTH their own model (per-role routing, wrapper layer) AND
    their own system prompt (per-role prompt, inner layer) — from operator
    catalogs keyed on the same derivation. Verified at the provider boundary
    (`messages.create(model=..., system=...)`), the load-bearing surface."""
    # The two worker roles the fan-out driver would compose (AgentRole(str(step_id))).
    researcher = derive_agent_role(StepID("researcher"))
    writer = derive_agent_role(StepID("writer"))
    assert researcher != writer

    # Operator authors the per-role MODEL catalog (RoutingManifest.per_role_bindings)…
    routing = _routing_manifest_with_roles(
        {str(researcher): "model-for-researcher", str(writer): "model-for-writer"}
    )
    # …and the per-role PROMPT catalog (PromptSelectionManifest → the stage-0/5
    # resolved per-role system-prompt map the inner indexes).
    per_role_prompts = {
        researcher: "PROMPT-researcher",
        writer: "PROMPT-writer",
    }

    # The validator confirms the catalog binds EXACTLY the derivable worker roles:
    # both live, no dead bindings (the operator authored coherent keys).
    derivable = derive_fanout_roles([StepID("researcher"), StepID("writer")])
    coherence = validate_per_role_catalog(
        derivable_roles=derivable, bound_roles=routing.per_role_bindings.keys()
    )
    assert coherence.live_roles == frozenset({researcher, writer})
    assert coherence.unbound_roles == frozenset()
    assert coherence.has_dead_bindings is False

    # The REAL composed stack: model layer (wrapper) over prompt layer (inner) over
    # a recording provider — exactly the stage-5 composition shape.
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    inner = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt="PROMPT-default-role",
        per_role_system_prompts=per_role_prompts,
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=routing,
    )

    # Worker 1 — researcher.
    await wrapper.dispatch(
        _binding("anthropic", "stage-primary"),
        _step(),
        step_context=_step_context_with_role(str(researcher)),
    )
    researcher_call = adapter.client.messages.last_kwargs
    assert researcher_call is not None
    assert researcher_call["model"] == "model-for-researcher"
    assert researcher_call["system"] == "PROMPT-researcher"

    # Worker 2 — writer, through the SAME stack, distinct role.
    await wrapper.dispatch(
        _binding("anthropic", "stage-primary"),
        _step(),
        step_context=_step_context_with_role(str(writer)),
    )
    writer_call = adapter.client.messages.last_kwargs
    assert writer_call is not None
    assert writer_call["model"] == "model-for-writer"
    assert writer_call["system"] == "PROMPT-writer"

    # THE CONJUNCTION: distinct workers got BOTH distinct models AND distinct
    # prompts — the catalog is non-vacuous across both dimensions at once.
    assert researcher_call["model"] != writer_call["model"]
    assert researcher_call["system"] != writer_call["system"]


@pytest.mark.asyncio
async def test_b_l2_full_chain_routed_primary_reaches_provider_and_chain_advances() -> None:
    """B-L2-FALLBACK-COMPOSITION (§14.6) FULL-CHAIN witness — the REAL
    `resolve_routed_binding` (producer) + the REAL C-RT-16 wrapper (consumer) + the
    REAL inner `RuntimeLLMDispatcher` + a recording provider compose in ONE path (no
    mock resolver — `[[full-chain-witness-not-half-proofs]]`).

    routing_activation on + a manifest-MISS + a stub EMBEDDING classifier (→ haiku) +
    a stage chain whose primary is opus. The wrapper resolves ONCE via the bare
    dispatcher's `resolve_routed_binding` → seeds haiku as PRIMARY → the inner
    faithfully dispatches haiku to the provider. The routed primary FAILS once
    (the recording adapter raises on the first call), so the wrapper ADVANCES to the
    stage primary (opus) and dispatches it — the chain composes with routing
    end-to-end. Asserts on the provider boundary (`messages.create(model=...)`)."""
    # A recording adapter that fails the FIRST dispatch (routed haiku), succeeds the
    # second (stage-primary opus) — proving the chain advances under real routing.
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    seen: list[str] = []
    real_create = adapter.client.messages.create

    async def _failing_first_create(**kwargs: Any) -> Any:
        seen.append(kwargs["model"])
        if len(seen) == 1:
            raise RuntimeError("routed model (haiku) transient-unreachable")
        return await real_create(**kwargs)

    adapter.client.messages.create = _failing_first_create  # type: ignore[method-assign]

    tp, _ = _tracer_provider_with_exporter()
    inner = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        routing_manifest=_routing_manifest_with_roles({}),  # MISS → DECLARATIVE declines
        embedding_classifier=_stub_embedding_classifier,  # → anthropic:claude-haiku-4-5
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(),
        fallback_chain=_chain(_candidate("anthropic", "claude-opus-4-8")),  # stage primary
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=inner.resolve_routed_binding,  # the REAL route-once handle
    )

    result = await wrapper.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    # Routed haiku tried FIRST (the route-once primary), then advanced to the stage
    # primary opus on its failure — routing composed with the fallback chain through
    # the REAL provider boundary.
    assert seen == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert result is not None


# ---------------------------------------------------------------------------
# B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION (§14.6.1 scope-boundary closure) — the
# inner `gen_ai` span's `routing.layer` reports the REAL EMBEDDING/L3 layer the
# wrapper used for the routed PRIMARY (not the inner's faithful DECLARATIVE echo,
# RoutingLayer.DECLARATIVE == "manifest"); fallback candidates keep faithful echo
# reporting; routing-off is byte-identical to pre-arc. ContextVar-threaded
# (concurrency-safe across fan-out branches; the B-INTERSTEP-PERRUN-ISOLATION
# precedent). Witnessed through the REAL wrapper + REAL resolver (no mock).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_l2_routed_primary_span_reports_real_embedding_layer() -> None:
    """THE FIX: a routed-primary dispatch (EMBEDDING picked haiku) → the
    `chat claude-haiku-4-5` gen_ai span carries `routing.layer == "embedding"`,
    NOT the DECLARATIVE echo `"manifest"` the inner would otherwise report."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    inner = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        routing_manifest=_routing_manifest_with_roles({}),  # MISS → DECLARATIVE declines
        embedding_classifier=_stub_embedding_classifier,  # → anthropic:claude-haiku-4-5
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(),
        fallback_chain=_chain(_candidate("anthropic", "claude-opus-4-8")),  # stage primary
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=inner.resolve_routed_binding,  # the REAL route-once handle
    )

    await wrapper.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    haiku_span = next(s for s in exporter.get_finished_spans() if s.name == "chat claude-haiku-4-5")
    attrs = haiku_span.attributes or {}
    # The real EMBEDDING layer the wrapper resolved — NOT the inner's echo.
    assert attrs["routing.layer"] == "embedding"
    assert attrs["routing.layer"] != "manifest"
    # The dispatched candidate is still faithfully the routed model.
    assert attrs["routing.model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_b_l2_routing_off_span_reports_declarative_echo_byte_identical() -> None:
    """NEGATIVE CONTROL: routing OFF (default) → no routed primary published →
    the span carries the DECLARATIVE-echo `routing.layer == "manifest"`
    (byte-identical to pre-arc; the ContextVar is never set)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, exporter = _tracer_provider_with_exporter()
    # Bare dispatcher, no wrapper, no routing_activation → pure echo path.
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    span = next(s for s in exporter.get_finished_spans() if s.name == "chat claude-opus-4-8")
    attrs = span.attributes or {}
    assert attrs["routing.layer"] == "manifest"  # RoutingLayer.DECLARATIVE echo


@pytest.mark.asyncio
async def test_b_l2_fallback_candidate_span_keeps_faithful_echo() -> None:
    """THE BOUNDARY: the routed primary (haiku, EMBEDDING) FAILS → fallback
    advances to the stage primary (opus). The opus span — a CHAIN-selected
    fallback candidate, not routing-selected — keeps the faithful DECLARATIVE
    echo (`"manifest"`), while the haiku (routed primary) span reports the real
    `"embedding"` layer. Proves the ContextVar is scoped to the routed primary
    ONLY (cleared for fallback candidates)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    seen: list[str] = []
    real_create = adapter.client.messages.create

    async def _failing_first_create(**kwargs: Any) -> Any:
        seen.append(kwargs["model"])
        if len(seen) == 1:
            raise RuntimeError("routed model (haiku) transient-unreachable")
        return await real_create(**kwargs)

    adapter.client.messages.create = _failing_first_create  # type: ignore[method-assign]

    tp, exporter = _tracer_provider_with_exporter()
    inner = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        routing_activation=True,
        routing_manifest=_routing_manifest_with_roles({}),  # MISS → DECLARATIVE declines
        embedding_classifier=_stub_embedding_classifier,  # → anthropic:claude-haiku-4-5
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(),
        fallback_chain=_chain(_candidate("anthropic", "claude-opus-4-8")),  # stage primary
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=inner.resolve_routed_binding,
    )

    await wrapper.dispatch(
        _binding("anthropic", "claude-opus-4-8"), _step(), step_context=_step_context()
    )

    spans = {s.name: (s.attributes or {}) for s in exporter.get_finished_spans()}
    # Routed primary (haiku): the real EMBEDDING layer.
    assert spans["chat claude-haiku-4-5"]["routing.layer"] == "embedding"
    # Fallback candidate (opus): faithful DECLARATIVE echo — NOT overridden.
    assert spans["chat claude-opus-4-8"]["routing.layer"] == "manifest"


# ---------------------------------------------------------------------------
# B4 Slice 3 (CP spec v1.37 §6.1) — per-step PROMPT override at dispatch. A
# resolved binding's `prompt_version_sha` overrides BOTH per-role and the
# run-level default for THAT step (precedence per-step > per-role > default),
# resolved from the stage-5 `prompt_versions_by_sha` store map; fail-loud on an
# unauthored sha + binding-tier governance parity. Verified by the recorded
# provider `system=` kwarg through the real `dispatch(...)` path.
# ---------------------------------------------------------------------------

_STEP_SHA = "c" * 64
_STEP_SYS = "STEP-per-step-prompt"


def _binding_with_prompt(
    prompt_version_sha: str | None,
    *,
    persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER,
) -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-001",
        model_binding=ModelBinding(provider="anthropic", model="test-model-1"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=True,
        persona_tier=persona_tier,
        prompt_version_sha=prompt_version_sha,
    )


@pytest.mark.asyncio
async def test_b4_slice3_per_step_prompt_beats_per_role_and_default() -> None:
    """THE precedence e2e: ONE dispatcher carrying a default prompt, a per-role
    prompt, AND a per-step store. A binding with `prompt_version_sha` injects the
    per-step content over BOTH per-role (researcher) and default (writer); a
    binding WITHOUT a per-step sha falls through to per-role / then default."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_DEFAULT_SYS,
        per_role_system_prompts={AgentRole("researcher"): _RESEARCHER_SYS},
        prompt_versions_by_sha={_STEP_SHA: _STEP_SYS},
    )

    # per-step beats per-role (researcher would otherwise inject _RESEARCHER_SYS).
    await dispatcher.dispatch(
        _binding_with_prompt(_STEP_SHA), _step(), step_context=_step_context_with_role("researcher")
    )
    assert adapter.client.messages.last_kwargs is not None
    assert adapter.client.messages.last_kwargs["system"] == _STEP_SYS

    # per-step beats default (writer is unbound per-role).
    await dispatcher.dispatch(
        _binding_with_prompt(_STEP_SHA), _step(), step_context=_step_context_with_role("writer")
    )
    assert adapter.client.messages.last_kwargs["system"] == _STEP_SYS

    # no per-step sha → falls through to per-role.
    await dispatcher.dispatch(
        _binding_with_prompt(None), _step(), step_context=_step_context_with_role("researcher")
    )
    assert adapter.client.messages.last_kwargs["system"] == _RESEARCHER_SYS

    # no per-step sha + unbound role → falls through to default.
    await dispatcher.dispatch(
        _binding_with_prompt(None), _step(), step_context=_step_context_with_role("writer")
    )
    assert adapter.client.messages.last_kwargs["system"] == _DEFAULT_SYS


@pytest.mark.asyncio
async def test_b4_slice3_per_step_prompt_unauthored_sha_fails_loud() -> None:
    """A per-step `prompt_version_sha` that is not an authored store member
    (`prompt_versions_by_sha`) fails loud (`RT-FAIL-PROMPT-SELECTION-UNAUTHORED`),
    mirroring the per-role bootstrap guard — never silently falls through."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt=_DEFAULT_SYS,
        prompt_versions_by_sha={_STEP_SHA: _STEP_SYS},
    )
    with pytest.raises(PromptSelectionUnauthoredError):
        await dispatcher.dispatch(
            _binding_with_prompt("d" * 64), _step(), step_context=_step_context_with_role("writer")
        )


@pytest.mark.asyncio
async def test_b4_slice3_per_step_prompt_governance_parity_at_binding_tier() -> None:
    """Binding-tier governance parity (OD C-OD-34): a per-step prompt at a binding
    DEPLOYMENT tier (team-binding) must be operator-approved, else fail loud
    (`RT-FAIL-PROMPT-VERSION-UNAPPROVED`) — closing the gap where a per-step
    override could bypass the approval the per-role/default paths enforce.
    Governance keys on the DEPLOYMENT tier (the dispatcher's `persona_tier`,
    threaded from `config.persona_tier`), not the per-workflow binding tier."""
    tp, _ = _tracer_provider_with_exporter()

    # team-binding DEPLOYMENT + sha NOT approved → fail loud.
    unapproved = RuntimeLLMDispatcher(
        providers={"anthropic": _AnthropicFakeAdapter(_AnthropicClient())},
        tracer_provider=tp,
        persona_tier=PersonaTier.TEAM_BINDING,
        active_system_prompt=_DEFAULT_SYS,
        prompt_versions_by_sha={_STEP_SHA: _STEP_SYS},
        approved_prompt_version_shas=frozenset(),
    )
    with pytest.raises(PromptVersionUnapprovedError):
        await unapproved.dispatch(
            _binding_with_prompt(_STEP_SHA),
            _step(),
            step_context=_step_context_with_role("writer"),
        )


@pytest.mark.asyncio
async def test_b4_slice3_per_step_governance_keys_on_deployment_not_workflow_tier() -> None:
    """Codex P1 regression: a workflow whose binding declares SOLO_DEVELOPER must
    NOT bypass approval on a binding-tier DEPLOYMENT. Governance keys on the
    dispatcher's deployment persona tier, never the per-workflow binding tier —
    else a SOLO-manifest workflow downgrades the deployment's governance posture."""
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": _AnthropicFakeAdapter(_AnthropicClient())},
        tracer_provider=tp,
        persona_tier=PersonaTier.TEAM_BINDING,  # binding-tier DEPLOYMENT
        active_system_prompt=_DEFAULT_SYS,
        prompt_versions_by_sha={_STEP_SHA: _STEP_SYS},
        approved_prompt_version_shas=frozenset(),  # unapproved
    )
    # The binding declares SOLO — but the DEPLOYMENT is team-binding, so the gate fires.
    with pytest.raises(PromptVersionUnapprovedError):
        await dispatcher.dispatch(
            _binding_with_prompt(_STEP_SHA, persona_tier=PersonaTier.SOLO_DEVELOPER),
            _step(),
            step_context=_step_context_with_role("writer"),
        )


@pytest.mark.asyncio
async def test_b4_slice3_per_step_prompt_governance_inert_at_solo_and_when_approved() -> None:
    """The governance gate is inert at the solo-developer DEPLOYMENT tier (no
    approval required) and passes at a binding DEPLOYMENT once the sha is approved
    — in both cases the per-step content injects."""
    tp, _ = _tracer_provider_with_exporter()

    # solo DEPLOYMENT tier: unapproved sha is INERT → injects.
    solo_adapter = _AnthropicFakeAdapter(_AnthropicClient())
    solo = RuntimeLLMDispatcher(
        providers={"anthropic": solo_adapter},
        tracer_provider=tp,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        active_system_prompt=_DEFAULT_SYS,
        prompt_versions_by_sha={_STEP_SHA: _STEP_SYS},
        approved_prompt_version_shas=frozenset(),
    )
    await solo.dispatch(
        _binding_with_prompt(_STEP_SHA),
        _step(),
        step_context=_step_context_with_role("writer"),
    )
    assert solo_adapter.client.messages.last_kwargs is not None
    assert solo_adapter.client.messages.last_kwargs["system"] == _STEP_SYS

    # team-binding DEPLOYMENT + sha APPROVED → injects.
    approved_adapter = _AnthropicFakeAdapter(_AnthropicClient())
    approved = RuntimeLLMDispatcher(
        providers={"anthropic": approved_adapter},
        tracer_provider=tp,
        persona_tier=PersonaTier.TEAM_BINDING,
        active_system_prompt=_DEFAULT_SYS,
        prompt_versions_by_sha={_STEP_SHA: _STEP_SYS},
        approved_prompt_version_shas=frozenset({_STEP_SHA}),
    )
    await approved.dispatch(
        _binding_with_prompt(_STEP_SHA),
        _step(),
        step_context=_step_context_with_role("writer"),
    )
    assert approved_adapter.client.messages.last_kwargs is not None
    assert approved_adapter.client.messages.last_kwargs["system"] == _STEP_SYS


# ---------------------------------------------------------------------------
# B4 Slice 4 — FULL-STACK consumer e2e: a per-step ROLE override drives, through
# the REAL driver-fold → wrapper(inner(recording provider)) stack, BOTH the
# override role's model (wrapper `_effective_chain`) AND its prompt (inner
# per-role map). Closes the transitivity seam the CP driver-fold tests (fake
# dispatcher) + the Slice-2 dispatch e2e (hand-built step_context) leave
# unexercised together — the genuinely-new composition for Slice 4.
# ---------------------------------------------------------------------------

_FS_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="b4-slice4-fullstack")


class _FSLedger:
    """Minimal `LedgerWriterLike` for execute_workflow (mirror of harness-cp _FakeLedger)."""

    def __init__(self) -> None:
        self.actor = _FS_ACTOR
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


class _FSEmitter:
    def __init__(self) -> None:
        self.emits: list[Any] = []

    def emit(self, event_class: Any) -> None:
        self.emits.append(event_class)


class _FSLedgerReader:
    def read_by_idempotency_key(self, idempotency_key: Any, bounded_window: Any) -> Any:
        _ = (idempotency_key, bounded_window)

        class _R:
            entries: tuple[object, ...] = ()
            truncated = False
            next_position = None

        return _R()


class _FSCtx:
    """Minimal `DriverContext` for a single linear INFERENCE_STEP execute_workflow run."""

    def __init__(self) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = _FSLedger()
        self.lifecycle_emitter = _FSEmitter()
        self.drained_flag = asyncio.Event()
        self.pause_resume_protocol = None
        self.pause_requested_flag = asyncio.Event()
        self.ledger_reader = _FSLedgerReader()
        self.tracer_provider = NoOpTracerProvider()
        self.validator_framework = None
        self.tenant_id = None


class _SingleKindFacadeRegistry:
    def __init__(self, dispatcher: Any) -> None:
        self._d = dispatcher

    def lookup(self, step_kind: Any) -> Any:
        _ = step_kind
        return self._d


@pytest.mark.asyncio
async def test_b4_slice4_per_step_role_override_full_stack_e2e() -> None:
    """THE B4-Slice-4 full-stack proof. A per-step `StepOverride.agent_role` on a
    SINGLE_THREADED_LINEAR step flows through the REAL CP `execute_workflow`
    driver-fold → `SyncDispatcherFacade` → `RetryBreakerFallbackDispatcher`
    (model, via `_effective_chain(step_context.agent_role)`) → `RuntimeLLMDispatcher`
    (prompt, via the per-role map) → recording provider. The discriminating
    assertion is at the provider boundary: `messages.create` receives BOTH the
    override role's `model=` (proves the wrapper picked up the driver fold) AND
    its `system=` (proves the inner did). This closes the seam the CP driver-fold
    tests (fake dispatcher) + the Slice-2 dispatch e2e (hand-built step_context)
    cover only by transitivity (`[[test-bypass-as-runtime-truth-pattern]]`)."""
    override_role = "audit-specialist"
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    inner = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        active_system_prompt="PROMPT-default-role",
        per_role_system_prompts={AgentRole(override_role): "PROMPT-audit-specialist"},
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles(
            {override_role: "model-for-audit-specialist"}
        ),
    )
    facade = SyncDispatcherFacade(
        inner=wrapper,
        loop=asyncio.get_running_loop(),
        result_timeout_seconds=10.0,
    )
    registry = cast(Any, _SingleKindFacadeRegistry(facade))

    manifest = WorkflowManifestEntry(
        workflow_id="wf-b4-slice4-fullstack",
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        hitl_placements=(),
        per_step_overrides={
            StepID("step-0"): StepOverride(
                step_id=StepID("step-0"), agent_role=AgentRole(override_role)
            )
        },
    )
    step = WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "params": {"max_tokens": 8},
        },
    )
    ctx = _FSCtx()

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=[step],
            run_id="run-b4-slice4-fullstack",
            ctx=cast(Any, ctx),
            default_model_binding=ModelBinding(provider="anthropic", model="default-model"),
            step_dispatchers=registry,
        )
    )

    assert result.status is RunStatus.SUCCESS, result.fail_class
    call = adapter.client.messages.last_kwargs
    assert call is not None
    # Wrapper picked up the per-step role fold for MODEL selection.
    assert call["model"] == "model-for-audit-specialist"
    # Inner picked up the per-step role fold for PROMPT injection.
    assert call["system"] == "PROMPT-audit-specialist"


# ---------------------------------------------------------------------------
# B-INTERSTEP (runtime spec §14.21 C-RT-34) — inter-step output injection.
#
# Genuine non-vacuity: the REAL `RuntimeLLMDispatcher.dispatch` path injects the
# immediately-prior step's output into the ACTUAL provider call's `messages` when
# the run-scoped channel is bound. NOT a stub consumer — the assertion reads what
# the fake provider boundary received, proving the prior output reaches the model.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inter_step_channel_injects_prior_output_into_provider_call() -> None:
    """A bound, non-empty channel → the dispatched provider call SEES the prior
    step's output as a prepended upstream-context message (the EVALUATOR_OPTIMIZER
    evaluate-sees-the-draft data flow, exercised through the real dispatch path)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "the-models-first-draft"})
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        inter_step_channel=channel,
    )

    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    call = adapter.client.messages.last_kwargs
    assert call is not None
    messages = call["messages"]
    # The injected upstream-context message is prepended, labeled, and carries the
    # prior step's serialized output — the model genuinely receives it.
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("Upstream step output:")
    assert "the-models-first-draft" in messages[0]["content"]
    # The step's own message is preserved AFTER the injected upstream context.
    assert {"role": "user", "content": "hi"} in messages
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_b_engine_output_replay_rehydrated_output_reaches_real_provider_call(
    tmp_path: Path,
) -> None:
    """B-ENGINE-OUTPUT-REPLAY full-chain witness — a prior run's step output, durably
    stored ON DISK, is REHYDRATED into a FRESH inter-step channel (the post-restart
    resume) and the REAL `RuntimeLLMDispatcher` then INJECTS it into the actual
    provider call: the model GENUINELY receives the recovered upstream output on
    resume (the C-CP-08 §8.1 "outputs cached and replayed" clause, end-to-end through
    the real dispatcher → real provider — NOT a channel-unit proxy). Composes the
    durable store + the CP rehydrate + the real dispatcher + the recording provider
    in one path; the store's disk-survival across a restart is the store unit tests."""
    # A prior (now-crashed) run durably stored step-0's output (RESERVE-before-COMMIT).
    store = EngineOutputStore(journal_dir=tmp_path / "eo")
    store.record("run-key", 0, "step-0", {"draft": "recovered-prior-draft"})

    # A FRESH channel (the restart) + the CP rehydrate over `resume_at=1` (step-0
    # materialized) → the fresh channel is repopulated from the durable store.
    channel = InterStepOutputChannel()

    class _ReplayCtx:
        engine_output_store = store
        inter_step_output_channel = channel

    fail = _rehydrate_inter_step_channel_on_replay(
        cast(Any, _ReplayCtx()),
        run_idempotency_key="run-key",
        resume_at=1,
        steps=[
            WorkflowStep(
                step_id=StepID("step-0"), step_kind=StepKind.INFERENCE_STEP, step_payload={}
            ),
            WorkflowStep(
                step_id=StepID("step-1"), step_kind=StepKind.INFERENCE_STEP, step_payload={}
            ),
        ],
        workflow_id="wf",
        run_id="r",
    )
    assert fail is None  # rehydrate succeeded (no skew / identity mismatch)
    assert channel.most_recent_output() == {"draft": "recovered-prior-draft"}

    # The REAL dispatcher dispatches the resumed step → injects the recovered step-0
    # output into the ACTUAL provider call.
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter}, tracer_provider=tp, inter_step_channel=channel
    )
    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    call = adapter.client.messages.last_kwargs
    assert call is not None
    messages = call["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("Upstream step output:")
    # The provider GENUINELY receives the prior run's RECOVERED output on resume.
    assert "recovered-prior-draft" in messages[0]["content"]


class _FullChainHandoffLedger:
    """In-memory ledger writer double for the handoff full-chain witness (the
    drained branch entries land here; the witness asserts on provider kwargs, not
    the ledger, so a recording double is sufficient)."""

    def __init__(self) -> None:
        self.actor = Actor(actor_class=ActorClass.AGENT, actor_id="fullchain-handoff")
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


class _FullChainHandoffEmitter:
    def __init__(self) -> None:
        self.emits: list[Any] = []

    def emit(self, event_class: Any) -> None:
        self.emits.append(event_class)


class _InferenceFacadeRegistry:
    """Maps INFERENCE_STEP → the sync facade over the real `RuntimeLLMDispatcher`."""

    def __init__(self, facade: SyncDispatcherFacade) -> None:
        self._facade = facade

    def lookup(self, step_kind: StepKind) -> Any:
        if step_kind is StepKind.INFERENCE_STEP:
            return self._facade
        raise AssertionError(f"unexpected step kind {step_kind!r}")


@pytest.mark.asyncio
async def test_decentralized_handoff_inter_step_output_reaches_real_provider_call() -> None:
    """B-INTERSTEP-NONLINEAR handoff slice — FULL-CHAIN witness (no proxy). A genuine
    2-stage DECENTRALIZED_HANDOFF runs through the REAL CP driver (`execute_workflow`
    → `_execute_decentralized_handoff`) where each stage dispatches through the REAL
    `RuntimeLLMDispatcher` (via the production `SyncDispatcherFacade` async/sync
    bridge, the `api.py` `asyncio.to_thread` shape). Stage A's output is recorded by
    the driver's new inter-step wiring; stage B's ACTUAL provider call then carries
    stage A's output as the injected upstream-context message — the model GENUINELY
    receives the prior stage-expert's output (composes the NEW handoff producer + the
    existing dispatcher consumer through the real path; `[[full-chain-witness-not-half-proofs]]`)."""
    loop = asyncio.get_running_loop()
    channel = InterStepOutputChannel()
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    # Seed DISTINCT per-stage responses so the witness is discriminating (not the
    # shared canned "ok"). The dispatcher returns the response's model_dump → that
    # is the output recorded to the channel + injected into the next stage's call.
    adapter.client.messages.responses = [
        _ProviderResponse(
            id="msg-a",
            usage=_Usage(input_tokens=1, output_tokens=1),
            _dump={"id": "msg-a", "content": [{"text": "STAGE_A_OUTPUT_TOKEN"}]},
        ),
        _ProviderResponse(
            id="msg-b",
            usage=_Usage(input_tokens=1, output_tokens=1),
            _dump={"id": "msg-b", "content": [{"text": "STAGE_B_OUTPUT_TOKEN"}]},
        ),
    ]
    tp, _ = _tracer_provider_with_exporter()
    inner = RuntimeLLMDispatcher(
        providers={"anthropic": adapter}, tracer_provider=tp, inter_step_channel=channel
    )
    facade = SyncDispatcherFacade(inner=inner, loop=loop, result_timeout_seconds=30.0)

    ctx = type("_FullChainHandoffCtx", (), {})()
    ctx.ledger_writer = _FullChainHandoffLedger()
    ctx.lifecycle_emitter = _FullChainHandoffEmitter()
    ctx.drained_flag = asyncio.Event()
    ctx.pause_requested_flag = asyncio.Event()
    ctx.pause_resume_protocol = None
    ctx.ledger_reader = None
    ctx.tracer_provider = tp
    ctx.validator_framework = None
    ctx.tenant_id = None
    # B-INTERSTEP-NONLINEAR — the SAME channel the dispatcher reads (bootstrap wires
    # one instance to both; opt-in `RuntimeConfig.inter_step_data_flow`).
    ctx.inter_step_output_channel = channel

    chain = FallbackChain(
        primary=ProviderCandidate(
            provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
        ),
        same_family=(),
        cross_family=(),
        terminal=None,
    )
    manifest = WorkflowManifestEntry(
        workflow_id="wf-fullchain-handoff",
        # DECENTRALIZED_HANDOFF is §10.3-admissible only for PIPELINE_AUTOMATION.
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.DECENTRALIZED_HANDOFF,
        layer_budgets=(),
        fallback_chain=chain,
        hitl_placements=(),
        per_step_overrides={},
    )

    def _inference(step_id: str, content: str) -> WorkflowStep:
        return WorkflowStep(
            step_id=StepID(step_id),
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={
                "messages": [{"role": "user", "content": content}],
                "tools": None,
                "params": {"max_tokens": 50},
            },
        )

    result = await asyncio.to_thread(
        execute_workflow,
        manifest,
        [_inference("stage-a", "Plan the work"), _inference("stage-b", "Do the work")],
        run_id="run-fullchain",
        ctx=cast(Any, ctx),
        default_model_binding=ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        step_dispatchers=cast(Any, _InferenceFacadeRegistry(facade)),
    )

    assert result.status is RunStatus.SUCCESS
    calls = adapter.client.messages.calls
    assert len(calls) == 2  # two stage-expert provider calls, in handoff order
    # Stage A is the first owner → NO upstream injection (the channel was empty).
    assert not calls[0]["messages"][0]["content"].startswith("Upstream step output:")
    # Stage B's ACTUAL provider call carries stage A's output as upstream context —
    # the model genuinely receives the prior stage-expert's output through the real path.
    stage_b_msg0 = calls[1]["messages"][0]
    assert stage_b_msg0["role"] == "user"
    assert stage_b_msg0["content"].startswith("Upstream step output:")
    assert "STAGE_A_OUTPUT_TOKEN" in stage_b_msg0["content"]
    # Stage B's own message is preserved AFTER the injected upstream context.
    assert {"role": "user", "content": "Do the work"} in calls[1]["messages"]


@pytest.mark.asyncio
async def test_inter_step_channel_none_no_injection_byte_identical() -> None:
    """No channel (opt-out default) → the provider call's messages are byte-identical
    to pre-v1.59 (no upstream injection)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(providers={"anthropic": adapter}, tracer_provider=tp)

    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    call = adapter.client.messages.last_kwargs
    assert call is not None
    assert call["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_inter_step_channel_empty_no_injection() -> None:
    """A bound but EMPTY channel (first step — no prior output) → no injection."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        inter_step_channel=InterStepOutputChannel(),
    )

    await dispatcher.dispatch(_binding("anthropic"), _step(), step_context=_step_context())

    call = adapter.client.messages.last_kwargs
    assert call is not None
    assert call["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_inter_step_channel_survives_params_messages_override() -> None:
    """Codex regression — a payload using the `params["messages"]` escape hatch
    must STILL receive the upstream context. The injection runs on the FINAL
    post-`params`-merge kwargs, so `kwargs.update(payload.params)` cannot drop it
    (early `payload.messages` injection silently would have)."""
    adapter = _AnthropicFakeAdapter(_AnthropicClient())
    tp, _ = _tracer_provider_with_exporter()
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "DRAFT-via-params"})
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter}, tracer_provider=tp, inter_step_channel=channel
    )
    payload = {
        "messages": [{"role": "user", "content": "top-level"}],
        "tools": None,
        "params": {"max_tokens": 100, "messages": [{"role": "user", "content": "from-params"}]},
    }

    await dispatcher.dispatch(_binding("anthropic"), _step(payload), step_context=_step_context())

    messages = adapter.client.messages.last_kwargs["messages"]
    # params["messages"] replaced the top-level messages, yet the upstream injection
    # (post-`params`) still leads — the data flow reaches the provider.
    assert messages[0]["content"].startswith("Upstream step output:")
    assert "DRAFT-via-params" in messages[0]["content"]
    assert {"role": "user", "content": "from-params"} in messages


@pytest.mark.asyncio
async def test_inter_step_channel_openai_injects_after_active_system_message() -> None:
    """Codex regression — with an active system prompt, the upstream-context `user`
    message lands AFTER the leading `system` message (does not displace it)."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "DRAFT-openai"})
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": adapter},
        tracer_provider=tp,
        inter_step_channel=channel,
        active_system_prompt=_SYS,
    )

    await dispatcher.dispatch(_binding("openai"), _step(), step_context=_step_context())

    messages = adapter.client.chat.completions.last_kwargs["messages"]
    assert messages[0] == {"role": "system", "content": _SYS}
    assert messages[1]["content"].startswith("Upstream step output:")
    assert "DRAFT-openai" in messages[1]["content"]
    assert {"role": "user", "content": "hi"} in messages


@pytest.mark.asyncio
async def test_inter_step_channel_does_not_mask_system_conflict() -> None:
    """Codex regression — prepending the upstream `user` message must NOT hide a
    payload-leading `role:"system"` message from the competing-system-source
    conflict check. With an active prompt + a step-owned system message + the
    channel bound, the dispatch STILL fails loud (the check runs before the
    upstream injection)."""
    adapter = _OpenAIFakeAdapter(_OpenAIClient())
    tp, _ = _tracer_provider_with_exporter()
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "DRAFT"})
    dispatcher = RuntimeLLMDispatcher(
        providers={"openai": adapter},
        tracer_provider=tp,
        inter_step_channel=channel,
        active_system_prompt=_SYS,
    )

    with pytest.raises(PromptInjectionConflictError):
        await dispatcher.dispatch(
            _binding("openai"),
            _step(
                {
                    "messages": [
                        {"role": "system", "content": "step-owned system"},
                        {"role": "user", "content": "hi"},
                    ],
                    "tools": None,
                    "params": {"max_tokens": 100},
                }
            ),
            step_context=_step_context(),
        )


@pytest.mark.asyncio
async def test_inter_step_channel_reaches_anthropic_hitl_tool_loop() -> None:
    """Codex review round 2 — the Anthropic HITL tool-loop path must ALSO carry the
    upstream context. The loop seeds its per-turn mutable messages from the
    TRANSLATED `kwargs["messages"]` (params merge + upstream injection), not raw
    `payload.messages`, so tool-using model calls keep the inter-step context."""
    client = _AnthropicClient()
    client.messages.responses = [
        _AnthropicToolTurnResponse(
            id="msg_tool",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "search_docs",
                    "input": {"query": "q"},
                }
            ],
            stop_reason="tool_use",
            usage=_Usage(input_tokens=11, output_tokens=6),
        ),
        _AnthropicToolTurnResponse(
            id="msg_final",
            content=[{"type": "text", "text": "done"}],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=12, output_tokens=4),
        ),
    ]
    adapter = _AnthropicFakeAdapter(client)
    hitl_loop = _FakeHITLToolLoop({"toolu_001": {"ok": True}})
    tp, _ = _tracer_provider_with_exporter()
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "DRAFT-hitl"})
    dispatcher = RuntimeLLMDispatcher(
        providers={"anthropic": adapter},
        tracer_provider=tp,
        hitl_tool_loop=cast(Any, hitl_loop),
        inter_step_channel=channel,
    )

    await dispatcher.dispatch(
        _binding("anthropic", model="claude-test"),
        _step(
            {
                "messages": [{"role": "user", "content": "search"}],
                "tools": [
                    {
                        "name": "search_docs",
                        "server": "docs-mcp",
                        "input_schema": {"type": "object"},
                    }
                ],
                "params": {"max_tokens": 100},
            }
        ),
        step_context=_step_context(),
    )

    # The FIRST model call in the tool loop carries the upstream context.
    first_messages = client.messages.calls[0]["messages"]
    assert first_messages[0]["content"].startswith("Upstream step output:")
    assert "DRAFT-hitl" in first_messages[0]["content"]
