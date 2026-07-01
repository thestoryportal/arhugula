"""Tests for U-RT-74 — retry-only tool-dispatch composer wrapping C-RT-19
(`Spec_Harness_Runtime_v1.md` v1.16 §14.11 C-RT-21).

Acceptance-criterion coverage (per plan v2.13 §1B U-RT-74):

  AC #1 — Instantiable with inner + retry_breaker + tracer_provider.
  AC #2 — Success on first attempt: outer + inner spans both emit; result
          returned verbatim from inner.
  AC #3 — Transient fail then success on attempt 2: 2 inner spans
          (attempt_number=1 terminal=retry; attempt_number=2 terminal=success);
          jittered backoff sleep occurs between attempts.
  AC #4 — max_attempts exhaustion on RT-FAIL-TOOL-INVOCATION-TIMEOUT:
          tool_retry.exhausted event on outer span; raises
          RetryToolExhaustedError mapped to RT-FAIL-TOOL-RETRY-EXHAUSTED.
  AC #5 — Fail-fast on permanent (RT-FAIL-TOOL-INVOCATION-PROTOCOL-ERROR):
          single inner span terminal=fail-fast; wrapper re-raises verbatim
          (no retry consumption).
  AC #6 — NO breaker interaction: wrapper does NOT call get_breaker /
          record_failure / record_success; no harness.breaker.* emission.
  AC #7 — NO fallback-chain interaction: wrapper does NOT consume
          ctx.fallback_chain / ProviderCandidate / fallback.exhausted.
  AC #8 — isinstance(wrapper, StepDispatcher) via @runtime_checkable.

Test conventions follow ``tests/test_lifecycle_retry_breaker_fallback.py``:
in-memory OTel span exporter + SimpleSpanProcessor for synchronous flushing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_core.identity import StepID
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.routing_manifest_residence import RetryPolicy
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.effect_fence import EffectFenceAmbiguousUncommittedError
from harness_runtime.lifecycle.retry_breaker import (
    DEFAULT_RETRY_POLICY,
    RuntimeRetryBreaker,
)
from harness_runtime.lifecycle.retry_breaker_tool import (
    DEFAULT_TOOL_DISPATCH_RETRY_POLICY,
    RESERVED_TOOL_DISPATCH_KEY,
    RetryBreakerToolDispatcher,
    RetryToolExhaustedError,
)
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    MCPHostUnreachableError,
    ToolInvocationProtocolError,
    ToolInvocationTimeoutError,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _MockInnerToolDispatcher:
    """Records each `dispatch` call; returns canned outcomes per attempt."""

    outcomes: list[Mapping[str, Any] | BaseException]
    calls: list[tuple[StepEffectiveBinding, WorkflowStep]] = field(default_factory=list)
    _cursor: int = 0

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> Mapping[str, Any]:
        self.calls.append((binding, step))
        if self._cursor >= len(self.outcomes):
            raise IndexError(f"_MockInnerToolDispatcher exhausted after {self._cursor} calls")
        outcome = self.outcomes[self._cursor]
        self._cursor += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class _RecordingSleep:
    """Records the durations passed to it without actually sleeping."""

    delays: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-001",
        model_binding=ModelBinding(provider="anthropic", model="claude-test-1"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.TOOL_STEP,
        step_payload={
            "tool_id": "tool-test-1",
            "tool_args": {"x": 1},
        },
    )


def _step_context(step_index: int = 0) -> StepExecutionContext:
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


def _tracer_provider_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


def _retry_breaker_with_tool_policy(*, max_attempts: int = 3) -> RuntimeRetryBreaker:
    """Construct a registry with the reserved tool-dispatch policy pre-bound."""
    return RuntimeRetryBreaker(
        retry_policies={
            RESERVED_TOOL_DISPATCH_KEY: RetryPolicy(
                max_attempts=max_attempts,
                backoff="full_jitter",
                jitter="full_jitter",
            )
        },
        default_policy=DEFAULT_RETRY_POLICY,
        base_delay_seconds=0.0,
        delay_cap_seconds=0.01,
    )


# ---------------------------------------------------------------------------
# AC #1 — Instantiable.
# ---------------------------------------------------------------------------


def test_wrapper_instantiable_with_required_args() -> None:
    tp, _ = _tracer_provider_with_exporter()
    inner = _MockInnerToolDispatcher(outcomes=[])
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(),
        tracer_provider=tp,
    )
    assert wrapper.inner is inner
    assert wrapper.retry_breaker is not None
    assert wrapper.tracer_provider is tp


# ---------------------------------------------------------------------------
# AC #8 — Protocol satisfaction.
# ---------------------------------------------------------------------------


def test_wrapper_satisfies_step_dispatcher_protocol() -> None:
    tp, _ = _tracer_provider_with_exporter()
    inner = _MockInnerToolDispatcher(outcomes=[])
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(),
        tracer_provider=tp,
    )
    assert isinstance(wrapper, StepDispatcher)


# ---------------------------------------------------------------------------
# AC #2 — Success on first attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_first_attempt_emits_outer_and_inner_span() -> None:
    tp, exporter = _tracer_provider_with_exporter()
    result_payload: Mapping[str, Any] = {"tool_output": "ok"}
    inner = _MockInnerToolDispatcher(outcomes=[result_payload])
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(),
        tracer_provider=tp,
        sleep_fn=_RecordingSleep(),
    )

    out = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    assert out == result_payload
    assert len(inner.calls) == 1
    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "harness.runtime.retry_tool_dispatch" in span_names
    assert "harness.runtime.tool_retry_attempt" in span_names
    inner_span = next(s for s in spans if s.name == "harness.runtime.tool_retry_attempt")
    assert inner_span.attributes["retry.attempt_number"] == 1
    assert inner_span.attributes["retry.terminal"] == "success"


# ---------------------------------------------------------------------------
# AC #3 — Transient fail then success on attempt 2.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_fail_then_success_emits_two_inner_spans() -> None:
    tp, exporter = _tracer_provider_with_exporter()
    transient = ToolInvocationTimeoutError("timeout")
    success_payload: Mapping[str, Any] = {"tool_output": "ok"}
    inner = _MockInnerToolDispatcher(outcomes=[transient, success_payload])
    recording_sleep = _RecordingSleep()
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(max_attempts=3),
        tracer_provider=tp,
        sleep_fn=recording_sleep,
    )

    out = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    assert out == success_payload
    assert len(inner.calls) == 2
    inner_spans = [
        s for s in exporter.get_finished_spans() if s.name == "harness.runtime.tool_retry_attempt"
    ]
    assert len(inner_spans) == 2
    by_attempt = {s.attributes["retry.attempt_number"]: s for s in inner_spans}
    assert by_attempt[1].attributes["retry.terminal"] == "retry"
    assert by_attempt[2].attributes["retry.terminal"] == "success"
    # Jittered backoff sleep recorded between attempts.
    assert len(recording_sleep.delays) == 1


# ---------------------------------------------------------------------------
# B-EFFECT-FENCE (§14.22 C-RT-31) — the fence fail-close is NOT retried.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effect_fence_ambiguous_error_is_not_retried() -> None:
    """`EffectFenceAmbiguousUncommittedError` fail-fasts the breaker (by-execution).

    The at-most-once guarantee depends on a re-dispatch NOT being retried into a
    second `call_tool`: the fence error is not in `_TRANSIENT_TOOL_DISPATCH_ERRORS`,
    so the breaker propagates it verbatim after exactly ONE inner attempt (proving
    the §14.22.5 invariant-3 "across BOTH resume and retry" claim by execution,
    not just by reading the transient allow-list — advisor Check B). The driver then
    routes the verbatim error to a §26.2 PAUSE/FAILED (B-EFFECT-FENCE-HITL-ROUTE).
    """
    tp, _ = _tracer_provider_with_exporter()
    fence_error = EffectFenceAmbiguousUncommittedError(idempotency_key="run-1:step-0:tool")
    inner = _MockInnerToolDispatcher(outcomes=[fence_error])
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(max_attempts=3),
        tracer_provider=tp,
        sleep_fn=_RecordingSleep(),
    )

    with pytest.raises(EffectFenceAmbiguousUncommittedError):
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    # Exactly one inner attempt — the fence error was NOT retried (no re-fire path).
    assert len(inner.calls) == 1


# ---------------------------------------------------------------------------
# AC #4 — max_attempts exhaustion on transient timeout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_exhaustion_raises_typed_terminal_error() -> None:
    tp, exporter = _tracer_provider_with_exporter()
    inner = _MockInnerToolDispatcher(
        outcomes=[
            ToolInvocationTimeoutError("t1"),
            ToolInvocationTimeoutError("t2"),
            ToolInvocationTimeoutError("t3"),
        ]
    )
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(max_attempts=3),
        tracer_provider=tp,
        sleep_fn=_RecordingSleep(),
    )

    with pytest.raises(RetryToolExhaustedError) as excinfo:
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    assert excinfo.value.max_attempts == 3
    assert excinfo.value.last_failure_class == "ToolInvocationTimeoutError"
    assert "RT-FAIL-TOOL-RETRY-EXHAUSTED" in str(excinfo.value)
    outer_spans = [
        s for s in exporter.get_finished_spans() if s.name == "harness.runtime.retry_tool_dispatch"
    ]
    assert len(outer_spans) == 1
    events = list(outer_spans[0].events)
    exhausted_events = [e for e in events if e.name == "tool_retry.exhausted"]
    assert len(exhausted_events) == 1
    attrs = dict(exhausted_events[0].attributes or {})
    assert attrs["tool_retry.max_attempts"] == 3
    assert attrs["tool_retry.last_failure_class"] == "ToolInvocationTimeoutError"


@pytest.mark.asyncio
async def test_max_attempts_exhaustion_on_mcp_host_unreachable() -> None:
    """Second transient class — MCPHostUnreachableError — also triggers retry."""
    tp, _ = _tracer_provider_with_exporter()
    inner = _MockInnerToolDispatcher(
        outcomes=[
            MCPHostUnreachableError("u1"),
            MCPHostUnreachableError("u2"),
        ]
    )
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(max_attempts=2),
        tracer_provider=tp,
        sleep_fn=_RecordingSleep(),
    )

    with pytest.raises(RetryToolExhaustedError) as excinfo:
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    assert excinfo.value.last_failure_class == "MCPHostUnreachableError"


# ---------------------------------------------------------------------------
# AC #5 — Fail-fast on permanent error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_fast_on_permanent_error_reraises_verbatim() -> None:
    tp, exporter = _tracer_provider_with_exporter()
    perm = ToolInvocationProtocolError("mcp protocol error")
    inner = _MockInnerToolDispatcher(outcomes=[perm])
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_tool_policy(max_attempts=3),
        tracer_provider=tp,
        sleep_fn=_RecordingSleep(),
    )

    with pytest.raises(ToolInvocationProtocolError) as excinfo:
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    # Same instance — re-raised verbatim, not wrapped.
    assert excinfo.value is perm
    # Only one inner call (no retry consumption on fail-fast).
    assert len(inner.calls) == 1
    inner_spans = [
        s for s in exporter.get_finished_spans() if s.name == "harness.runtime.tool_retry_attempt"
    ]
    assert len(inner_spans) == 1
    assert inner_spans[0].attributes["retry.terminal"] == "fail-fast"


# ---------------------------------------------------------------------------
# AC #6 — NO breaker interaction.
# ---------------------------------------------------------------------------


class _BreakerInteractionRecorder(RuntimeRetryBreaker):
    """Wraps RuntimeRetryBreaker to record breaker-API calls (any call to
    these methods is a v1.15 invariant violation)."""

    def __init__(self, inner: RuntimeRetryBreaker) -> None:
        # Copy state from inner via dict; preserve dataclass shape.
        super().__init__(
            retry_policies=inner.retry_policies,
            default_policy=inner.default_policy,
            base_delay_seconds=inner.base_delay_seconds,
            delay_cap_seconds=inner.delay_cap_seconds,
        )
        self.breaker_calls: list[str] = []

    def get_breaker(self, scope: Any, identifier: str) -> object:  # type: ignore[override]
        self.breaker_calls.append(f"get_breaker({scope},{identifier})")
        return super().get_breaker(scope, identifier)

    def emit_breaker_transition_event(  # type: ignore[override]
        self, transition: object, parent_span_ref: Any
    ) -> Any:
        self.breaker_calls.append("emit_breaker_transition_event")
        return super().emit_breaker_transition_event(transition, parent_span_ref)


@pytest.mark.asyncio
async def test_no_breaker_interaction_during_dispatch() -> None:
    tp, exporter = _tracer_provider_with_exporter()
    recorder = _BreakerInteractionRecorder(_retry_breaker_with_tool_policy())
    inner = _MockInnerToolDispatcher(outcomes=[{"tool_output": "ok"}])
    wrapper = RetryBreakerToolDispatcher(
        inner=inner,
        retry_breaker=recorder,
        tracer_provider=tp,
        sleep_fn=_RecordingSleep(),
    )

    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    assert recorder.breaker_calls == [], (
        f"v1.15 invariant: wrapper must NOT call breaker API; observed: {recorder.breaker_calls}"
    )
    # No harness.breaker.* spans emitted either.
    breaker_spans = [
        s for s in exporter.get_finished_spans() if s.name.startswith("harness.breaker.")
    ]
    assert breaker_spans == []


# ---------------------------------------------------------------------------
# AC #7 — NO fallback-chain interaction (verified by absence-of-import).
# ---------------------------------------------------------------------------


def test_module_does_not_import_fallback_chain_symbols() -> None:
    """v1.15 invariant: wrapper module does NOT import FallbackChain /
    ProviderCandidate / fallback.exhausted machinery. Verified via module
    namespace introspection (the symbols are not in the module dict)."""
    import harness_runtime.lifecycle.retry_breaker_tool as mod

    forbidden = {
        "FallbackChain",
        "ProviderCandidate",
        "FallbackChainExhaustedError",
        "advance_or_raise",
    }
    leaked = forbidden & set(vars(mod))
    assert not leaked, (
        f"v1.15 invariant: wrapper module must not surface fallback-chain symbols; leaked: {leaked}"
    )


# ---------------------------------------------------------------------------
# Defaults audit (spec §14.11 + plan AC for reserved key).
# ---------------------------------------------------------------------------


def test_reserved_key_and_default_policy_match_spec() -> None:
    assert RESERVED_TOOL_DISPATCH_KEY == "tool_dispatch"
    assert DEFAULT_TOOL_DISPATCH_RETRY_POLICY.max_attempts == 3
    assert DEFAULT_TOOL_DISPATCH_RETRY_POLICY.backoff == "full_jitter"
    assert DEFAULT_TOOL_DISPATCH_RETRY_POLICY.jitter == "full_jitter"
