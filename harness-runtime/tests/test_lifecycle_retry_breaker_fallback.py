"""Tests for U-RT-58 — retry / breaker / fallback composer wrapping C-RT-15
(`Spec_Harness_Runtime_v1.md` v1.4 §14.6 C-RT-16).

Acceptance-criterion coverage:

  AC #1  — Protocol satisfaction (``StepDispatcher`` from CP workflow driver).
  AC #2  — Per-candidate iteration verified across a 3-candidate chain.
  AC #3  — Per-candidate retry-then-success under ``RetryPolicy(max_attempts=3)``.
  AC #4  — ``retry.*`` 6-attribute namespace emission per C-CP-03 §3.5.
  AC #5  — ``fallback.exhausted`` emission + ``RetryBreakerFallbackExhaustedError``.
  AC #6  — Breaker pre-check (OPEN → skip) + ``harness.breaker.*`` emission.
  AC #7  — Nested-span hierarchy (outer → per-attempt; verified via
           InMemorySpanExporter parent-span-id linkage).
  AC #8  — Reserved registry key extension + ``ReservedToolNameError`` at
           manifest validation.
  AC #9  — Bootstrap stage 5 wrap (``ctx.llm_dispatcher`` post-condition is
           the wrapper; ``.inner`` is the bare ``RuntimeLLMDispatcher``).
  AC #10 — Phase 7d retirement-event prerequisite (post-landing; tracked at
           the retirement event file, not this test).

Test conventions follow ``tests/test_lifecycle_llm_dispatch.py``:
in-memory OTel span exporter + SimpleSpanProcessor for synchronous flushing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier
from harness_core.identity import StepID
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding, RoutingDecisionTrace
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.provider_capabilities import ProviderCapability
from harness_cp.routing_manifest_residence import (
    ReservedToolNameError,
    RetryPolicy,
    RoleRoutingBinding,
    RoutingManifest,
    WorkloadRoutingOverride,
    validate_routing_manifest,
)
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_od.harness_breaker_schema import BreakerScope
from harness_runtime.lifecycle.llm_dispatch import (
    LLMDispatchPayloadShapeError,
    LLMDispatchProviderUnreachableError,
    RoutedPrimaryResolution,
    RuntimeLLMDispatcher,
)
from harness_runtime.lifecycle.retry_breaker import (
    DEFAULT_RETRY_POLICY,
    BreakerStateMachine,
    BreakerTransition,
    RuntimeRetryBreaker,
    materialize_retry_breaker_stage,
)
from harness_runtime.lifecycle.retry_breaker_fallback import (
    DEFAULT_LLM_DISPATCH_RETRY_POLICY,
    RESERVED_LLM_DISPATCH_KEY,
    RetryBreakerFallbackDispatcher,
    RetryBreakerFallbackExhaustedError,
    _required_capabilities,
    materialize_retry_breaker_fallback_dispatcher_stage,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _MockInnerDispatcher:
    """Records each `dispatch` call; returns canned outcomes per attempt.

    ``outcomes`` is a list of either ``Mapping[str, Any]`` (success) or
    ``BaseException`` instances (raise this exception). The mock advances
    through the list one entry per ``dispatch`` call. If the list exhausts,
    further calls raise ``IndexError`` (test failure signal — the test set
    up wrong outcome count)."""

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
        # `step_context` accepted at v1.6 Path A per amended StepDispatcher
        # Protocol (C-RT-17 resolution); mock inner does not consume.
        self.calls.append((binding, step))
        if self._cursor >= len(self.outcomes):
            raise IndexError(
                f"_MockInnerDispatcher exhausted after {self._cursor} calls; "
                f"test outcome-list under-supplied"
            )
        outcome = self.outcomes[self._cursor]
        self._cursor += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _candidate(provider: str, model: str) -> ProviderCandidate:
    # ``ProviderFamily`` enum is 4-valued per C-CP-04 §4.1: ANTHROPIC / OPENAI /
    # GOOGLE / LOCAL_OPEN_WEIGHT. "ollama" maps to LOCAL_OPEN_WEIGHT for tests.
    family_map = {
        "anthropic": ProviderFamily.ANTHROPIC,
        "openai": ProviderFamily.OPENAI,
        "ollama": ProviderFamily.LOCAL_OPEN_WEIGHT,
    }
    return ProviderCandidate(provider=provider, model=model, family=family_map[provider])


def _chain(
    primary: ProviderCandidate,
    *,
    same_family: tuple[ProviderCandidate, ...] = (),
    cross_family: tuple[ProviderCandidate, ...] = (),
    terminal: ProviderCandidate | None = None,
) -> FallbackChain:
    return FallbackChain(
        primary=primary,
        same_family=same_family,
        cross_family=cross_family,
        terminal=terminal,
    )


def _binding(provider: str = "anthropic", model: str = "claude-test-1") -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-001",
        model_binding=ModelBinding(provider=provider, model=model),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": None,
            "params": {"max_tokens": 100},
        },
    )


def _tracer_provider_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


def _step_context(step_index: int = 0) -> StepExecutionContext:
    """Default step_context for v1.6 Path A test fixtures.

    C-RT-16 wrapper accepts step_context but does not consume it at v1.6;
    pass-through to the inner C-RT-15 dispatcher per the Protocol
    conformance discipline.
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


def _retry_breaker_with_llm_policy(*, max_attempts: int = 3) -> RuntimeRetryBreaker:
    """Construct a registry with the reserved LLM-dispatch policy pre-bound.

    Mirrors what ``materialize_retry_breaker_stage`` does at bootstrap; used
    where the bootstrap path isn't exercised end-to-end."""
    return RuntimeRetryBreaker(
        retry_policies={
            RESERVED_LLM_DISPATCH_KEY: RetryPolicy(
                max_attempts=max_attempts,
                backoff="full_jitter",
                jitter="full_jitter",
            )
        },
        default_policy=DEFAULT_RETRY_POLICY,
        base_delay_seconds=0.0,  # makes computed delays small for tests
        delay_cap_seconds=0.01,
    )


async def _noop_sleep(_seconds: float) -> None:
    """Sleep mock — keeps async tests fast and deterministic."""
    return None


# ---------------------------------------------------------------------------
# AC #1 — Protocol satisfaction.
# ---------------------------------------------------------------------------


def test_wrapper_satisfies_step_dispatcher_protocol() -> None:
    """The wrapper structurally satisfies the CP-side ``StepDispatcher``
    Protocol — the driver's call site at `workflow_driver.py:379` accepts
    the wrapper unchanged."""
    tp, _ = _tracer_provider_with_exporter()
    inner = _MockInnerDispatcher(outcomes=[])
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(),
        fallback_chain=_chain(_candidate("anthropic", "claude-test-1")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )
    assert isinstance(wrapper, StepDispatcher)


# ---------------------------------------------------------------------------
# AC #2 — Per-candidate iteration across a 3-candidate chain.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iterates_three_candidates_until_success() -> None:
    """Mock inner that fails transient on candidates 0+1 and succeeds on
    candidate 2; assert the wrapper iterates all three and returns the
    candidate-2 result."""
    primary = _candidate("anthropic", "claude-test-1")
    same_family = (_candidate("anthropic", "claude-test-2"),)
    cross_family = (_candidate("openai", "gpt-test-1"),)
    chain = _chain(primary, same_family=same_family, cross_family=cross_family)

    # max_attempts=1 → each candidate gets one attempt; fails advance to next.
    breaker = _retry_breaker_with_llm_policy(max_attempts=1)
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("transient on candidate 0"),
            RuntimeError("transient on candidate 1"),
            {"result": "candidate-2-success"},
        ]
    )
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert result == {"result": "candidate-2-success"}

    # All three candidates exercised; rebound binding observable.
    assert len(inner.calls) == 3
    seen_providers = [call[0].model_binding.provider for call in inner.calls]
    seen_models = [call[0].model_binding.model for call in inner.calls]
    assert seen_providers == ["anthropic", "anthropic", "openai"]
    assert seen_models == ["claude-test-1", "claude-test-2", "gpt-test-1"]


# ---------------------------------------------------------------------------
# AC #3 — Per-candidate retry-then-success under max_attempts=3.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_twice_then_succeeds_on_attempt_3() -> None:
    """Mock inner that fails (transient) twice then succeeds on attempt 3
    under ``RetryPolicy(max_attempts=3)``; assert success without iterating
    to the next candidate; verify three per-attempt spans."""
    primary = _candidate("anthropic", "claude-test-1")
    chain = _chain(primary)
    breaker = _retry_breaker_with_llm_policy(max_attempts=3)
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("transient attempt 0"),
            RuntimeError("transient attempt 1"),
            {"result": "success-on-attempt-3"},
        ]
    )
    sleep_calls: list[float] = []

    async def _recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_recording_sleep,
    )

    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert result == {"result": "success-on-attempt-3"}
    assert len(inner.calls) == 3
    # Two sleeps between three attempts.
    assert len(sleep_calls) == 2

    # Verify three per-attempt spans + one outer span.
    spans = exporter.get_finished_spans()
    attempt_spans = [s for s in spans if s.name == "harness.runtime.retry_attempt"]
    assert len(attempt_spans) == 3
    outer_spans = [s for s in spans if s.name == "harness.runtime.retry_breaker_fallback"]
    assert len(outer_spans) == 1

    # Final attempt is the success path — canonical CP §3.5 sampling discipline
    # omits `retry.fail_class` on success (presence is the tail-keep fail signal).
    last_attempt = attempt_spans[-1]
    assert last_attempt.attributes is not None
    assert "retry.fail_class" not in last_attempt.attributes


# ---------------------------------------------------------------------------
# AC #4 — retry.* 6-attribute namespace emission per C-CP-03 §3.5.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_six_attribute_namespace_emitted_per_attempt() -> None:
    """Each per-attempt span carries the canonical CP §3.5 6-attribute namespace
    (per Spec_Control_Plane_v1_3.md §3.5 + ADR-D1 v1.2 §1.1.1): ``retry.attempt_number``
    (1-indexed), ``retry.original_span_id`` (16-hex outer-span-id),
    ``retry.delay_ms``, ``retry.cause_attribution``, ``retry.fail_class``
    (`ValidatorRetryExitClass` enum), ``engine.replay_disposition`` (via
    `REPLAY_DISPOSITION_MAPPING[binding.engine_class]`).

    Path A resolution of `.harness/class_1_tension_c_rt_16_retry_attribute_drift.md`
    landed at runtime spec v1.5 + plan v2.4 (2026-05-20); previously named
    `retry.attempt` / `retry.attempt_count` / `retry.policy_id` / `retry.backoff_ms`
    / `retry.cause_class` / `retry.terminal` — drifted names per the runtime
    spec v1.4 step 4 phrasing, NOT canonical."""
    from harness_cp.engine_namespace import REPLAY_DISPOSITION_MAPPING

    primary = _candidate("anthropic", "claude-test-1")
    chain = _chain(primary)
    breaker = _retry_breaker_with_llm_policy(max_attempts=3)
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("transient attempt 0"),
            {"result": "ok"},
        ]
    )
    tp, exporter = _tracer_provider_with_exporter()
    binding = _binding()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    await wrapper.dispatch(binding, _step(), step_context=_step_context())
    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    attempts = [s for s in spans if s.name == "harness.runtime.retry_attempt"]
    assert len(attempts) == 2

    expected_replay = REPLAY_DISPOSITION_MAPPING[binding.engine_class].value
    expected_original_span_id = format(outer.context.span_id, "016x")

    # First attempt (transient → retry).
    first = attempts[0].attributes
    assert first is not None
    assert first["retry.attempt_number"] == 1  # 1-indexed per CP §3.5
    assert first["retry.original_span_id"] == expected_original_span_id
    assert first["engine.replay_disposition"] == expected_replay
    assert first["retry.cause_attribution"] == "transient-retry"
    assert first["retry.fail_class"] == "transient-retry"
    assert "retry.delay_ms" in first

    # Second attempt (success — fail_class omitted by canonical sampling
    # discipline; presence of `retry.fail_class` is tail-keep signal).
    second = attempts[1].attributes
    assert second is not None
    assert second["retry.attempt_number"] == 2
    assert second["retry.original_span_id"] == expected_original_span_id
    assert second["engine.replay_disposition"] == expected_replay
    assert second["retry.delay_ms"] == 0
    # On success path canonical sampling discipline says fail_class omitted.
    assert "retry.fail_class" not in second


# ---------------------------------------------------------------------------
# AC #5 — fallback.exhausted emission + typed terminal error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_exhausted_emits_and_raises_typed() -> None:
    """All candidates fail-fast (provider-unreachable); the wrapper iterates,
    emits ``fallback.exhausted`` on the outer span, and raises
    ``RetryBreakerFallbackExhaustedError`` mapping to
    ``RT-FAIL-FALLBACK-EXHAUSTED``."""
    primary = _candidate("anthropic", "claude-test-1")
    same_family = (_candidate("anthropic", "claude-test-2"),)
    chain = _chain(primary, same_family=same_family)
    breaker = _retry_breaker_with_llm_policy(max_attempts=2)
    inner = _MockInnerDispatcher(
        outcomes=[
            LLMDispatchProviderUnreachableError("anthropic"),
            LLMDispatchProviderUnreachableError("anthropic"),
        ]
    )
    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    with pytest.raises(RetryBreakerFallbackExhaustedError) as exc_info:
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    # Carries the last-failed candidate for attribution.
    assert exc_info.value.failed.provider == "anthropic"
    assert exc_info.value.failed.model == "claude-test-2"
    # Maps to the RT-FAIL-FALLBACK-EXHAUSTED token.
    assert "RT-FAIL-FALLBACK-EXHAUSTED" in str(exc_info.value)

    # Outer span carries the fallback.exhausted event.
    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    event_names = [e.name for e in outer.events]
    assert "fallback.exhausted" in event_names
    exhausted_event = next(e for e in outer.events if e.name == "fallback.exhausted")
    assert exhausted_event.attributes is not None
    assert exhausted_event.attributes["fallback.chain_length"] == 2


@pytest.mark.asyncio
async def test_fallback_exhausted_surfaces_last_provider_failure_detail() -> None:
    """The terminal wrapper error preserves the last provider exception detail
    so an operator can distinguish auth, argv, timeout, and output failures."""
    primary = _candidate("anthropic", "claude-test-1")
    chain = _chain(primary)
    breaker = _retry_breaker_with_llm_policy(max_attempts=1)
    inner = _MockInnerDispatcher(
        outcomes=[RuntimeError("gemini CLI exited 1: model not found")]
    )
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    with pytest.raises(RetryBreakerFallbackExhaustedError) as exc_info:
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())

    assert exc_info.value.last_failure_detail == (
        "RuntimeError: gemini CLI exited 1: model not found"
    )
    assert (
        "last failure: RuntimeError: gemini CLI exited 1: model not found"
        in str(exc_info.value)
    )


@pytest.mark.asyncio
async def test_payload_shape_error_treated_as_fail_fast() -> None:
    """``LLMDispatchPayloadShapeError`` is fail-fast per D2 — the candidate
    is abandoned without consuming the retry budget."""
    primary = _candidate("anthropic", "claude-test-1")
    same_family = (_candidate("anthropic", "claude-test-2"),)
    chain = _chain(primary, same_family=same_family)
    breaker = _retry_breaker_with_llm_policy(max_attempts=5)
    inner = _MockInnerDispatcher(
        outcomes=[
            LLMDispatchPayloadShapeError("missing messages key"),
            {"result": "candidate-1-ok"},
        ]
    )
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert result == {"result": "candidate-1-ok"}
    # Exactly 2 inner calls: one fail-fast on candidate 0; one success on candidate 1.
    # Even though max_attempts=5, fail-fast doesn't burn the budget.
    assert len(inner.calls) == 2


# ---------------------------------------------------------------------------
# AC #6 — Breaker pre-check + harness.breaker.* emission.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_open_skips_candidate_emits_retry_skipped() -> None:
    """An OPEN breaker on candidate 0 causes the wrapper to emit
    ``retry.skipped`` and advance to candidate 1 without invoking the inner
    dispatcher for candidate 0."""
    primary = _candidate("anthropic", "claude-test-1")
    same_family = (_candidate("anthropic", "claude-test-2"),)
    chain = _chain(primary, same_family=same_family)
    breaker = _retry_breaker_with_llm_policy(max_attempts=3)

    # Pre-trip candidate-0's breaker to OPEN.
    pre_breaker = breaker.get_breaker(BreakerScope.PER_MODEL, "anthropic:claude-test-1")
    pre_breaker.state = pre_breaker.state.__class__("open")  # set to OPEN
    assert pre_breaker.should_attempt() is False

    inner = _MockInnerDispatcher(outcomes=[{"result": "candidate-1-ok"}])
    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert result == {"result": "candidate-1-ok"}
    # Inner was called only for candidate 1 (candidate 0 was skipped).
    assert len(inner.calls) == 1
    assert inner.calls[0][0].model_binding.model == "claude-test-2"

    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    event_names = [e.name for e in outer.events]
    assert "retry.skipped" in event_names
    skipped = next(e for e in outer.events if e.name == "retry.skipped")
    assert skipped.attributes is not None
    assert skipped.attributes["retry.skipped.reason"] == "breaker-open"
    assert skipped.attributes["retry.skipped.candidate"] == "anthropic:claude-test-1"


@pytest.mark.asyncio
async def test_breaker_transition_emitted_via_registry() -> None:
    """When the breaker trips CLOSED → OPEN after the fail-threshold, the
    composer invokes ``RuntimeRetryBreaker.emit_breaker_transition_event``."""
    # Use a per-test registry with fail_threshold=1 → first failure trips.
    breaker = RuntimeRetryBreaker(
        retry_policies={
            RESERVED_LLM_DISPATCH_KEY: RetryPolicy(
                max_attempts=1, backoff="full_jitter", jitter="full_jitter"
            )
        },
        default_policy=DEFAULT_RETRY_POLICY,
        fail_threshold=1,
        base_delay_seconds=0.0,
        delay_cap_seconds=0.01,
    )

    # Spy on emit_breaker_transition_event via a wrapper (slots dataclass
    # disallows monkey-patching; we wrap the registry instead).
    emissions: list[BreakerTransition] = []

    @dataclass
    class _SpyingRegistry:
        inner: RuntimeRetryBreaker

        def get_policy(self, tool_name: str) -> RetryPolicy:
            return self.inner.get_policy(tool_name)

        def get_breaker(self, scope: BreakerScope, identifier: str) -> BreakerStateMachine:
            return self.inner.get_breaker(scope, identifier)

        def compute_delay_seconds(self, attempt: int, rng: Any | None = None) -> float:
            return self.inner.compute_delay_seconds(attempt, rng)

        def advance_staircase(self, current: Any, cause: Any, attempt: int) -> Any:
            return self.inner.advance_staircase(current, cause, attempt)

        def emit_breaker_transition_event(
            self, transition: Any, parent_span_ref: Any, **kwargs: Any
        ) -> Any:
            emissions.append(transition)
            return self.inner.emit_breaker_transition_event(transition, parent_span_ref, **kwargs)

    spying = _SpyingRegistry(inner=breaker)

    primary = _candidate("anthropic", "claude-test-1")
    same_family = (_candidate("anthropic", "claude-test-2"),)
    chain = _chain(primary, same_family=same_family)
    inner = _MockInnerDispatcher(
        outcomes=[
            LLMDispatchProviderUnreachableError("anthropic"),  # fail-fast → record_failure
            {"result": "candidate-1-ok"},
        ]
    )
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=spying,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    # One emission (candidate 0's breaker CLOSED → OPEN on first failure
    # because fail_threshold=1).
    assert len(emissions) == 1
    assert emissions[0].to_state.value == "open"


# ---------------------------------------------------------------------------
# AC #7 — Nested-span hierarchy verified via parent-span-id linkage.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_span_hierarchy_outer_parent_of_attempts() -> None:
    """Per-attempt ``harness.runtime.retry_attempt`` spans nest inside the
    outer ``harness.runtime.retry_breaker_fallback`` span."""
    primary = _candidate("anthropic", "claude-test-1")
    chain = _chain(primary)
    breaker = _retry_breaker_with_llm_policy(max_attempts=2)
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("transient attempt 0"),
            {"result": "ok"},
        ]
    )
    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    attempts = [s for s in spans if s.name == "harness.runtime.retry_attempt"]
    assert len(attempts) == 2
    for attempt in attempts:
        assert attempt.parent is not None
        assert attempt.parent.span_id == outer.context.span_id


# ---------------------------------------------------------------------------
# AC #8 — Reserved registry key + ReservedToolNameError at validation.
# ---------------------------------------------------------------------------


def test_reserved_registry_key_populated_after_bootstrap() -> None:
    """``materialize_retry_breaker_stage`` injects the reserved
    ``"llm_dispatch"`` key into the registry's policy map."""
    manifest = RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(_chain(_candidate("anthropic", "claude-test-1")),),
        retry_policies={},  # no operator override
    )
    config = _runtime_config(manifest)
    stage = materialize_retry_breaker_stage(config)
    policy = stage.registry.get_policy(RESERVED_LLM_DISPATCH_KEY)
    assert policy.max_attempts == DEFAULT_LLM_DISPATCH_RETRY_POLICY.max_attempts
    assert policy.backoff == DEFAULT_LLM_DISPATCH_RETRY_POLICY.backoff


def test_reserved_tool_name_error_on_operator_supplied_key() -> None:
    """Operator-supplied ``"llm_dispatch"`` in ``retry_policies`` raises
    ``ReservedToolNameError`` at manifest validation time."""
    manifest = RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(_chain(_candidate("anthropic", "claude-test-1")),),
        retry_policies={
            "llm_dispatch": RetryPolicy(
                max_attempts=99, backoff="full_jitter", jitter="full_jitter"
            )
        },
    )
    with pytest.raises(ReservedToolNameError) as exc_info:
        validate_routing_manifest(manifest)
    assert exc_info.value.reserved_name == "llm_dispatch"


def test_valid_manifest_passes_with_non_reserved_tool_names() -> None:
    """Non-reserved tool names in ``retry_policies`` do not raise."""
    manifest = RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(_chain(_candidate("anthropic", "claude-test-1")),),
        retry_policies={
            "my_tool": RetryPolicy(max_attempts=2, backoff="full_jitter", jitter="full_jitter")
        },
    )
    assert validate_routing_manifest(manifest) is None


# ---------------------------------------------------------------------------
# AC #9 — Bootstrap stage 5 wrap.
# ---------------------------------------------------------------------------


def test_materialize_factory_wraps_inner_dispatcher() -> None:
    """The factory returns a wrapper whose ``.inner`` is the bare
    ``RuntimeLLMDispatcher`` and which carries the supplied ``routing_manifest``
    (the U-RT-114 §14.5.3 per-role MODEL read substrate — stage 5 passes
    ``ctx.routing_manifest``; absent ⟹ ``None`` ⟹ no per-role routing)."""
    tp, _ = _tracer_provider_with_exporter()
    providers: dict[str, Any] = {"anthropic": object()}
    bare = RuntimeLLMDispatcher(providers=providers, tracer_provider=tp)

    breaker = _retry_breaker_with_llm_policy()
    chain = _chain(_candidate("anthropic", "claude-test-1"))
    manifest = _routing_manifest_with_roles({"worker-a": "role-model-a"})
    wrapper = materialize_retry_breaker_fallback_dispatcher_stage(
        inner=bare,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        routing_manifest=manifest,
    )
    assert isinstance(wrapper, RetryBreakerFallbackDispatcher)
    assert isinstance(wrapper, StepDispatcher)
    assert wrapper.inner is bare
    # The factory threads routing_manifest to the wrapper (the U-RT-114 read
    # substrate); default omitted ⟹ None (the §14.5.3 non-breaking default).
    assert wrapper.routing_manifest is manifest
    assert (
        materialize_retry_breaker_fallback_dispatcher_stage(
            inner=bare, retry_breaker=breaker, fallback_chain=chain, tracer_provider=tp
        ).routing_manifest
        is None
    )


# ---------------------------------------------------------------------------
# Defensive — degenerate fallback chain (single primary) fails closed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_candidate_chain_fail_fast_exhausts() -> None:
    """A 1-candidate chain whose only candidate fails-fast exhausts
    immediately."""
    primary = _candidate("anthropic", "claude-test-1")
    chain = _chain(primary)
    breaker = _retry_breaker_with_llm_policy(max_attempts=1)
    inner = _MockInnerDispatcher(outcomes=[LLMDispatchProviderUnreachableError("anthropic")])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    with pytest.raises(RetryBreakerFallbackExhaustedError):
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _runtime_config(manifest: RoutingManifest) -> Any:
    """Construct a minimal ``RuntimeConfig`` with the given manifest.

    Tests that only need ``materialize_retry_breaker_stage`` don't require
    the full config — they need ``config.routing_manifest`` reachable."""

    @dataclass(frozen=True)
    class _MinimalConfig:
        routing_manifest: RoutingManifest

    return _MinimalConfig(routing_manifest=manifest)


# ---------------------------------------------------------------------------
# R-CL-P1 — C-CP-03 §3.3 capability-shortfall fallback (capability-preservation).
# ---------------------------------------------------------------------------


def _thinking_step() -> WorkflowStep:
    """An INFERENCE_STEP whose payload requests extended thinking.

    ``params["thinking"]`` set -> the call requires the ``THINKING`` provider
    capability per ``_required_capabilities`` (C-CP-03 §3.3 derivation)."""
    return WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "think hard"}],
            "tools": None,
            "params": {"thinking": {"type": "enabled", "budget_tokens": 4096}},
        },
    )


def test_required_capabilities_derivation() -> None:
    """``_required_capabilities`` maps the payload to the C-CP-01 §1.2 capability
    discriminators: ``tools`` -> TOOLS, ``params['thinking']`` -> THINKING."""
    # Neither tools nor thinking -> empty (the common path; pre-check no-op).
    assert _required_capabilities(_step()) == frozenset()
    # thinking param -> THINKING.
    assert _required_capabilities(_thinking_step()) == frozenset({ProviderCapability.THINKING})
    # tools present -> TOOLS.
    tools_step = WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "use a tool"}],
            "tools": [{"name": "calc"}],
            "params": {},
        },
    )
    assert _required_capabilities(tools_step) == frozenset({ProviderCapability.TOOLS})
    # both -> {TOOLS, THINKING}.
    both_step = WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "calc"}],
            "params": {"thinking": {"type": "enabled"}},
        },
    )
    assert _required_capabilities(both_step) == frozenset(
        {ProviderCapability.TOOLS, ProviderCapability.THINKING}
    )


@pytest.mark.asyncio
async def test_capability_shortfall_skips_incapable_primary_before_provider_call() -> None:
    """A thinking step at a non-thinking primary advances to a thinking-capable
    cross-family candidate WITHOUT calling the incapable provider (C-CP-03 §3.3
    advance-before-error). Uses the *real* runtime model-ID shape
    ``claude-opus-4-7`` (the Anthropic extended-thinking tier per
    ``reflect_provider_capabilities.supports_thinking``) — not the short §13.4
    token — so the test exercises the format runtime bindings actually carry."""
    primary = _candidate("openai", "gpt-test-1")  # supports_thinking == False
    cross_family = (_candidate("anthropic", "claude-opus-4-7"),)  # supports_thinking == True
    chain = _chain(primary, cross_family=cross_family)
    breaker = _retry_breaker_with_llm_policy(max_attempts=1)
    # One inner call expected — the capable anthropic candidate. The incapable
    # openai primary must be skipped before any provider dispatch.
    inner = _MockInnerDispatcher(outcomes=[{"result": "thinking-ok"}])
    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    result = await wrapper.dispatch(_binding(), _thinking_step(), step_context=_step_context())
    assert result == {"result": "thinking-ok"}

    # The incapable primary was NEVER dispatched; only the capable candidate.
    assert len(inner.calls) == 1
    assert inner.calls[0][0].model_binding.provider == "anthropic"
    assert inner.calls[0][0].model_binding.model == "claude-opus-4-7"

    # Outer span carries the §3.3 fallback.triggered (capability_shortfall) event.
    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    triggered = [e for e in outer.events if e.name == "fallback.triggered"]
    assert len(triggered) == 1
    attrs = triggered[0].attributes
    assert attrs is not None
    assert attrs["fallback.cause"] == "capability_shortfall"
    assert attrs["fallback.from_provider"] == "openai"
    assert attrs["fallback.from_model"] == "gpt-test-1"
    assert attrs["fallback.required_capability"] == "thinking"


@pytest.mark.asyncio
async def test_capability_shortfall_exhausts_when_no_capable_candidate() -> None:
    """A thinking step with no thinking-capable candidate fails-closed
    (``RetryBreakerFallbackExhaustedError``) WITHOUT any provider call — the
    capability-preservation guarantee: better to fail than silently serve a
    thinking step on a non-thinking model (§3.2 step 3 / §3.3)."""
    primary = _candidate("openai", "gpt-test-1")  # no thinking
    cross_family = (_candidate("ollama", "llama-test-1"),)  # no thinking
    chain = _chain(primary, cross_family=cross_family)
    breaker = _retry_breaker_with_llm_policy(max_attempts=1)
    inner = _MockInnerDispatcher(outcomes=[])  # must never be called
    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    with pytest.raises(RetryBreakerFallbackExhaustedError):
        await wrapper.dispatch(_binding(), _thinking_step(), step_context=_step_context())

    # No provider was ever dispatched (both candidates skipped pre-call).
    assert len(inner.calls) == 0
    # Two capability_shortfall triggers + a terminal fallback.exhausted.
    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    triggered = [e for e in outer.events if e.name == "fallback.triggered"]
    assert len(triggered) == 2
    assert all(e.attributes["fallback.cause"] == "capability_shortfall" for e in triggered)
    # The terminal fallback.exhausted attributes the shortfall cause, NOT
    # retry-exhaustion (no provider attempt ran) — accurate failure-mode telemetry.
    exhausted = next(e for e in outer.events if e.name == "fallback.exhausted")
    assert exhausted.attributes is not None
    assert exhausted.attributes["fallback.exhaustion_cause"] == "capability-shortfall"
    assert exhausted.attributes["fallback.last_failure_class"] == "capability-shortfall"


@pytest.mark.asyncio
async def test_no_capability_requirement_is_behavior_neutral() -> None:
    """A step with no tools + no thinking param derives an empty capability set;
    the §3.3 pre-check is a no-op and the primary dispatches normally — the
    no-regression guard for the common path (existing fixtures use
    ``params:{max_tokens:…}``)."""
    primary = _candidate("openai", "gpt-test-1")  # would shortfall IF thinking required
    chain = _chain(primary)
    breaker = _retry_breaker_with_llm_policy(max_attempts=1)
    inner = _MockInnerDispatcher(outcomes=[{"result": "ok"}])
    tp, exporter = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=breaker,
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert result == {"result": "ok"}
    assert len(inner.calls) == 1
    assert inner.calls[0][0].model_binding.provider == "openai"
    # No capability-shortfall event emitted.
    spans = exporter.get_finished_spans()
    outer = next(s for s in spans if s.name == "harness.runtime.retry_breaker_fallback")
    assert "fallback.triggered" not in [e.name for e in outer.events]


# ---------------------------------------------------------------------------
# U-RT-114 — per-role MODEL dispatch-read at the C-RT-16 wrapper (§14.5.3)
# ---------------------------------------------------------------------------
#
# §14.5.3 places the per-role MODEL binding read at the dispatch-composition
# surface (the [P2-a] placement) so it COMPOSES with C-RT-16 fallback: a
# non-default branch `agent_role` with a `per_role_bindings` entry promotes its
# `preferred_model_binding` to the PRIMARY fallback candidate, and the
# operator-configured chain becomes the fallback tail. Default / absent role /
# `None`-manifest / role-absent-from-catalog ⟹ the stage-bound chain UNCHANGED
# (the §14.5.3 non-breaking-default + linear-path-untouched invariants). The
# inner C-RT-15 dispatcher is role-AGNOSTIC for MODEL selection — see
# `test_lifecycle_llm_dispatch.py`. `_MockInnerDispatcher` records each rebound
# `binding.model_binding`, so the dispatched candidate per call is asserted
# directly.


def _routing_manifest_with_roles(
    role_models: dict[str, str], *, provider: str = "anthropic"
) -> RoutingManifest:
    """A `RoutingManifest` whose `per_role_bindings` map each role to a
    ModelBinding at the named `(provider, model)` — the only field U-RT-114
    reads (the role's own `fallback_chain_ref` is out of scope at B1)."""
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole(role): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider=provider, model=model),
                layer_budget_overrides={},
            )
            for role, model in role_models.items()
        },
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


def _step_context_with_role(role: str | None) -> StepExecutionContext:
    """A step_context with the branch `agent_role` set (or `None` = linear /
    default path)."""
    return _step_context().model_copy(
        update={"agent_role": AgentRole(role) if role is not None else None}
    )


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_default_none_role_uses_stage_chain() -> None:
    """`agent_role is None` ⟹ the stage-bound chain's primary is dispatched
    UNCHANGED, even with a populated catalog (the §14.5.3 non-breaking-default /
    linear-path-untouched invariant)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role(None))
    assert inner.calls[-1][0].model_binding.model == "stage-primary"


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_non_default_role_promotes_per_role_model() -> None:
    """A NON-default branch role with a catalog entry ⟹ the per-role model is the
    PRIMARY candidate dispatched (the §14.5.3 per-role MODEL specialization,
    placed at the wrapper so it composes with fallback)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-a"))
    assert inner.calls[-1][0].model_binding.model == "role-model-a"


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_role_absent_from_catalog_uses_stage_chain() -> None:
    """A non-default role NOT in the catalog ⟹ the stage chain unchanged
    (non-breaking default)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-z"))
    assert inner.calls[-1][0].model_binding.model == "stage-primary"


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_none_manifest_uses_stage_chain() -> None:
    """No `routing_manifest` (the default) ⟹ the stage chain unchanged for any
    role (empty-catalog ⟹ zero behaviour change)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        # routing_manifest omitted -> None
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-a"))
    assert inner.calls[-1][0].model_binding.model == "stage-primary"


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_per_role_primary_fails_then_fallback_candidate_dispatched() -> None:
    """[P2-a] regression — the KEY proof that per-role routing COMPOSES with
    fallback. A non-default role promotes its model to PRIMARY; the original
    chain becomes the fallback tail. When the per-role model fails, the NEXT
    chain candidate (the original stage primary) is actually dispatched — NOT the
    per-role model retried. Pre-fix (per-role override at the inner) EVERY
    candidate dispatched the same per-role model and fallback was silently
    defeated; this test distinguishes the fix from the bug."""
    # Original chain: stage-primary (anthropic) -> gpt-fallback (openai, cross).
    chain = _chain(
        _candidate("anthropic", "stage-primary"),
        cross_family=(_candidate("openai", "gpt-fallback"),),
    )
    # Augmented chain for "worker-a": [role-model-a, stage-primary, gpt-fallback].
    # max_attempts=1; per-role primary FAILS transient -> advance.
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("per-role model unreachable"),  # candidate 0 = role-model-a
            {"ok": "fell-back-to-stage-primary"},  # candidate 1 = stage-primary
        ]
    )
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )
    result = await wrapper.dispatch(
        _binding(), _step(), step_context=_step_context_with_role("worker-a")
    )
    dispatched = [c[0].model_binding.model for c in inner.calls]
    # Per-role model first, then the REAL next chain candidate — fallback preserved.
    assert dispatched == ["role-model-a", "stage-primary"]
    assert result == {"ok": "fell-back-to-stage-primary"}


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_distinct_workers_route_to_distinct_models() -> None:
    """Two workers under DISTINCT roles dispatch DISTINCT per-role models through
    the SAME wrapper (the non-hollow ORCHESTRATOR_WORKERS specialization the
    U-RT-114 read makes effective)."""
    inner = _MockInnerDispatcher(outcomes=[{"a": 1}, {"b": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles(
            {"worker-a": "role-model-a", "worker-b": "role-model-b"}
        ),
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-a"))
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-b"))
    assert inner.calls[0][0].model_binding.model == "role-model-a"
    assert inner.calls[1][0].model_binding.model == "role-model-b"
    assert inner.calls[0][0].model_binding.model != inner.calls[1][0].model_binding.model


@pytest.mark.asyncio
async def test_u_rt_114_wrapper_per_role_equals_primary_dedups_no_double_dispatch() -> None:
    """Dedup edge: per-role model == the chain's only candidate ⟹ the augmented
    chain is NOT built with a same-model duplicate; the single candidate is
    attempted exactly once (no same-model double-dispatch on advance)."""
    inner = _MockInnerDispatcher(outcomes=[RuntimeError("fail")])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "stage-primary"}),
    )
    with pytest.raises(RetryBreakerFallbackExhaustedError):
        await wrapper.dispatch(
            _binding(), _step(), step_context=_step_context_with_role("worker-a")
        )
    assert len(inner.calls) == 1  # NOT 2 — dedup avoided the duplicate.
    assert inner.calls[0][0].model_binding.model == "stage-primary"


# ---------------------------------------------------------------------------
# B-L2-FALLBACK-COMPOSITION (§14.6) — the route-once layered-routing PRIMARY
# composes with the C-RT-16 fallback chain. The `routing_resolver` (the bare
# C-RT-15 dispatcher's `resolve_routed_binding`) is resolved ONCE per step and
# seeds the wrapper's PRIMARY candidate; the inner faithfully dispatches each
# rebound candidate. The two load-bearing witnesses: (a) the chain ADVANCES under
# routing (a routed primary that fails falls back through the chain — the exact
# silent-fallback-defeat the retired interim guard prevented), and (b) routing is
# resolved ONCE, not re-run per fallback attempt (the §14.5.3 invariant).
# ---------------------------------------------------------------------------


@dataclass
class _RecordingResolver:
    """A `routing_resolver` stub (``RoutedBindingResolver``): wraps a fixed
    routed ModelBinding into a ``RoutedPrimaryResolution`` (or returns ``None``)
    and counts invocations — the route-once witness. The synthesized trace's
    layer is ``embedding`` so the routed-primary span attribution
    (B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION) has a non-DECLARATIVE layer to report."""

    routed: ModelBinding | None
    calls: int = 0

    async def __call__(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> RoutedPrimaryResolution | None:
        self.calls += 1
        if self.routed is None:
            return None
        return RoutedPrimaryResolution(
            model_binding=self.routed,
            routing_trace=RoutingDecisionTrace(
                layer="embedding",
                candidate=f"{self.routed.provider}:{self.routed.model}",
                decision_ms=0,
                budget_exhausted=False,
            ),
            binding_rationale=None,
        )


@pytest.mark.asyncio
async def test_b_l2_routed_primary_seeds_chain_primary() -> None:
    """A routed ModelBinding from the resolver ⟹ it is the chain PRIMARY dispatched
    first (the layered-routing decision seeded as the wrapper's primary candidate,
    §14.6). The stage chain becomes the deduped fallback tail."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=_RecordingResolver(
            routed=ModelBinding(provider="anthropic", model="routed-model")
        ),
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "routed-model"


@pytest.mark.asyncio
async def test_b_l2_routed_primary_fails_then_fallback_advances() -> None:
    """THE KEY non-vacuity witness — the chain ADVANCES under routing_activation
    (the exact silent-fallback-defeat the interim guard prevented). The routed
    PRIMARY fails transient → the wrapper advances to the NEXT chain candidate (the
    stage primary), which is actually dispatched — NOT the routed model re-routed.
    Pre-fix (routing re-run per attempt at the inner) EVERY attempt re-picked the
    routed model and the chain never advanced; this distinguishes the fix from the
    bug."""
    # Original chain: stage-primary -> gpt-fallback (cross-family).
    chain = _chain(
        _candidate("anthropic", "stage-primary"),
        cross_family=(_candidate("openai", "gpt-fallback"),),
    )
    # Augmented chain: [routed-model, stage-primary, gpt-fallback]. Routed primary
    # FAILS transient (max_attempts=1) -> advance to the next chain candidate.
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("routed model unreachable"),  # candidate 0 = routed-model
            {"ok": "fell-back-to-stage-primary"},  # candidate 1 = stage-primary
        ]
    )
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=_RecordingResolver(
            routed=ModelBinding(provider="anthropic", model="routed-model")
        ),
    )
    result = await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    dispatched = [c[0].model_binding.model for c in inner.calls]
    # Routed model first, then the REAL next chain candidate — fallback preserved.
    assert dispatched == ["routed-model", "stage-primary"]
    assert result == {"ok": "fell-back-to-stage-primary"}


@pytest.mark.asyncio
async def test_b_l2_routing_resolved_once_not_per_attempt() -> None:
    """The route-ONCE witness — routing is resolved exactly ONCE per step, NOT
    re-run per fallback attempt (the §14.5.3 "no two-authority-at-dispatch"
    invariant the inner-routing re-evaluation broke). Even though the routed primary
    fails and the wrapper advances through TWO more candidates (3 inner dispatches),
    the resolver is called exactly once."""
    chain = _chain(
        _candidate("anthropic", "stage-primary"),
        cross_family=(_candidate("openai", "gpt-fallback"),),
    )
    inner = _MockInnerDispatcher(
        outcomes=[
            RuntimeError("routed fail"),  # routed-model
            RuntimeError("stage fail"),  # stage-primary
            {"ok": "third"},  # gpt-fallback
        ]
    )
    tp, _ = _tracer_provider_with_exporter()
    resolver = _RecordingResolver(routed=ModelBinding(provider="anthropic", model="routed-model"))
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=resolver,
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert len(inner.calls) == 3  # routed-model, stage-primary, gpt-fallback
    assert resolver.calls == 1  # resolved ONCE, not per attempt — the route-once fix.


@pytest.mark.asyncio
async def test_b_l2_resolver_none_falls_back_to_per_role_augmentation() -> None:
    """Precedence / mutual-exclusivity: when the resolver returns ``None`` (its
    contract when a DETERMINISTIC binding governs — e.g. a per-role binding is
    present), the wrapper's existing U-RT-114 per-role augmentation governs; the
    routed path does NOT override the operator's per-role model. Routing fills ONLY
    the no-deterministic-binding gap."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
        routing_resolver=_RecordingResolver(routed=None),  # routing declines (deterministic)
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-a"))
    # Resolver declined (None) → per-role augmentation governs → role-model-a primary.
    assert inner.calls[-1][0].model_binding.model == "role-model-a"


@pytest.mark.asyncio
async def test_b_l2_no_resolver_byte_identical_stage_chain() -> None:
    """Zero blast radius: no `routing_resolver` (the default / non-inference path)
    ⟹ the stage chain primary is dispatched unchanged — byte-identical to the
    pre-B-L2 wrapper."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        # routing_resolver omitted -> None
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "stage-primary"


# ---------------------------------------------------------------------------
# B-MODEL-RESOLUTION-CONSOLIDATION (§14.5.3/§14.6) — full-chain witnesses through
# the REAL wrapper `_effective_chain` (no proxy): the model-resolution precedence
# per-step > per-workload > per-role > routed > default. `_MockInnerDispatcher`
# records each rebound `binding.model_binding`, so the dispatched candidate is
# asserted DIRECTLY (the inner-decline tests in test_lifecycle_llm_dispatch.py are
# half-proofs; these prove the wrapper actually CONSUMES each source).
# ---------------------------------------------------------------------------


def _manifest_with_workload_override(
    model: str,
    *,
    provider: str = "anthropic",
    workload: WorkloadClass = WorkloadClass.SOFTWARE_ENGINEERING,
    role_models: dict[str, str] | None = None,
) -> RoutingManifest:
    """A `RoutingManifest` with a per-workload `model_binding_override` (W-2) at
    `workload`, optionally plus `per_role_bindings`."""
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole(r): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider=provider, model=m),
                layer_budget_overrides={},
            )
            for r, m in (role_models or {}).items()
        },
        per_workload_overrides={
            workload: WorkloadRoutingOverride(
                model_binding_override=ModelBinding(provider=provider, model=model)
            )
        },
        fallback_chains=(),
        retry_policies={},
    )


def _binding_with_model_override(
    model: str, *, provider: str = "anthropic"
) -> StepEffectiveBinding:
    """A `StepEffectiveBinding` carrying a per-step MODEL override SIGNAL (the C-CP-06
    §6.2 `model_binding_override`), as `resolve_step_binding` produces for a per-step
    `StepOverride.model_binding`."""
    return _binding().model_copy(
        update={
            "override_applied": True,
            "model_binding_override": ModelBinding(provider=provider, model=model),
        }
    )


@pytest.mark.asyncio
async def test_consolidation_per_step_model_override_dispatched() -> None:
    """THE currently-broken case the consolidation fixes: a per-step MODEL override
    (no role / workload / routed) ⟹ the wrapper dispatches the per-step model — NOT
    the stage chain primary (pre-fix the wrapper never read the per-step model)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )
    await wrapper.dispatch(
        _binding_with_model_override("per-step-model"), _step(), step_context=_step_context()
    )
    assert inner.calls[-1][0].model_binding.model == "per-step-model"


@pytest.mark.asyncio
async def test_consolidation_per_workload_model_override_dispatched_routing_off() -> None:
    """The present-tense gap closed: a per-workload MODEL override ⟹ the wrapper
    dispatches it even ROUTING-OFF (no resolver) — joining per-workload ENGINE/PROMPT
    which were already consumed routing-off. Pre-fix this model was dropped to the
    stage chain."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_manifest_with_workload_override("per-workload-model"),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        # routing_resolver omitted -> None (routing OFF)
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "per-workload-model"


@pytest.mark.asyncio
async def test_consolidation_per_workload_override_applies_with_default_workload_class() -> None:
    """The §14.6.2 workload-None MIRROR (advisor catch): when the wrapper's
    `workload_class` is None but the manifest binds the MVP-default workload
    (SOFTWARE_ENGINEERING), `_effective_chain` MUST apply it (`self.workload_class or
    _MVP_DEFAULT_WORKLOAD_CLASS`) — mirroring the decline expression so the model is
    not silently dropped to default. Bites hardest in minimal-construction wrappers."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_manifest_with_workload_override("default-workload-model"),
        # workload_class omitted -> None ⟹ resolves to _MVP_DEFAULT_WORKLOAD_CLASS (SE)
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "default-workload-model"


@pytest.mark.asyncio
async def test_consolidation_per_step_beats_per_role() -> None:
    """Precedence pair: a per-step MODEL override + a bound branch role ⟹ the per-step
    model wins (per-step > per-role)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_routing_manifest_with_roles({"worker-a": "role-model-a"}),
    )
    await wrapper.dispatch(
        _binding_with_model_override("per-step-model"),
        _step(),
        step_context=_step_context_with_role("worker-a"),
    )
    assert inner.calls[-1][0].model_binding.model == "per-step-model"


@pytest.mark.asyncio
async def test_consolidation_per_workload_beats_per_role() -> None:
    """Precedence pair: a per-workload override + a bound branch role (no per-step) ⟹
    the per-WORKLOAD model wins (per-workload > per-role). MATCHES the cleared
    cross-subsystem convention — the PROMPT subsystem resolves (role, workload) as
    per_workload > per_role ("mirrors RoutingManifest workload-override-on-top-of-
    role"); MODEL follows the same workload-over-role order (operator-confirmed at
    build-open 2026-06-22)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_manifest_with_workload_override(
            "per-workload-model", role_models={"worker-a": "role-model-a"}
        ),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context_with_role("worker-a"))
    assert inner.calls[-1][0].model_binding.model == "per-workload-model"


@pytest.mark.asyncio
async def test_consolidation_per_workload_beats_default() -> None:
    """Precedence pair: a per-workload override + no per-step/per-role/routed ⟹ the
    per-workload model wins over the stage chain default (which becomes the tail)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_manifest_with_workload_override("per-workload-model"),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "per-workload-model"


@pytest.mark.asyncio
async def test_consolidation_per_step_beats_routed() -> None:
    """Precedence pair: a per-step MODEL override wins over a routed candidate (the
    defense-in-depth property — even if a lenient resolver returns a routed binding,
    `_effective_chain` prefers per-step; per-step > routed)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_resolver=_RecordingResolver(
            routed=ModelBinding(provider="anthropic", model="routed-model")
        ),
    )
    await wrapper.dispatch(
        _binding_with_model_override("per-step-model"), _step(), step_context=_step_context()
    )
    assert inner.calls[-1][0].model_binding.model == "per-step-model"


@pytest.mark.asyncio
async def test_consolidation_per_workload_beats_routed() -> None:
    """Precedence pair: a per-workload override wins over a routed candidate
    (per-workload > routed) — the routed candidate fills only the no-override gap."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=_manifest_with_workload_override("per-workload-model"),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        routing_resolver=_RecordingResolver(
            routed=ModelBinding(provider="anthropic", model="routed-model")
        ),
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "per-workload-model"


@pytest.mark.asyncio
async def test_consolidation_no_override_negative_control() -> None:
    """Negative control: a manifest with NO matching per-step / per-role / per-workload
    model AND no routed ⟹ the stage chain primary is dispatched UNCHANGED (the
    §14.5.3 non-breaking default holds; zero behaviour change when nothing governs)."""
    inner = _MockInnerDispatcher(outcomes=[{"ok": 1}])
    tp, _ = _tracer_provider_with_exporter()
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("anthropic", "stage-primary")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        # per-workload override at a DIFFERENT workload than the wrapper's class.
        routing_manifest=_manifest_with_workload_override(
            "other-workload-model", workload=WorkloadClass.RESEARCH
        ),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
    )
    await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner.calls[-1][0].model_binding.model == "stage-primary"


# ---------------------------------------------------------------------------
# U-RT-114 — live e2e: per-role models to a REAL local Ollama daemon (wrapper)
# ---------------------------------------------------------------------------
#
# The integration AC (runtime plan v2.43 U-RT-114): under per-role model
# bindings, distinct workers dispatch against distinct models — proven here
# against a REAL Ollama daemon (free, local, credential-less; CI without a daemon
# skips cleanly) through the C-RT-16 wrapper (the [P2-a] placement) end-to-end.
# Ollama echoes the served `model` in its ChatResponse, so the per-role routing
# is asserted on the real provider's own response, not a fake.


def _ollama_reachable() -> bool:
    """True iff a local ollama daemon answers on 127.0.0.1:11434 (free, no creds)."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.e2e
@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="U-RT-114 per-role live e2e requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.asyncio
async def test_u_rt_114_per_role_models_live_ollama_e2e_through_wrapper() -> None:
    """Two workers under DISTINCT branch roles dispatch DISTINCT Ollama models
    through the REAL wrapper → `RuntimeLLMDispatcher` → real ollama daemon. The
    served `model` confirms the per-role binding took effect on the live provider
    (the U-RT-114 integration AC, non-hollow), exercised at the [P2-a] placement
    (the C-RT-16 wrapper) end-to-end. The CP-resolved binding is `llama3.2:latest`
    (NOT the per-role model), so a passing assertion proves the wrapper's per-role
    read OVERRODE the binding via the augmented-chain primary."""
    from harness_runtime.lifecycle.providers import OllamaAdapter
    from ollama import AsyncClient as AsyncOllamaClient

    model_fast, model_slow = "llama3.2:1b", "llama3.2:3b"

    async def _noop_ping() -> None:
        return None

    adapter = OllamaAdapter(
        client=AsyncOllamaClient(host="http://127.0.0.1:11434"), ping=_noop_ping
    )
    tp, _ = _tracer_provider_with_exporter()
    ollama_role_manifest = _routing_manifest_with_roles(
        {"worker-fast": model_fast, "worker-slow": model_slow}, provider="ollama"
    )
    inner = RuntimeLLMDispatcher(
        providers={"ollama": adapter},
        tracer_provider=tp,
        routing_manifest=ollama_role_manifest,
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=1),
        fallback_chain=_chain(_candidate("ollama", "llama3.2:latest")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
        routing_manifest=ollama_role_manifest,
    )

    def _tiny_step() -> WorkflowStep:
        return WorkflowStep(
            step_id=StepID("worker"),
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "tools": None,
                "params": {"options": {"num_predict": 1}},
            },
        )

    try:
        result_fast = await wrapper.dispatch(
            _binding("ollama", "llama3.2:latest"),
            _tiny_step(),
            step_context=_step_context_with_role("worker-fast"),
        )
        result_slow = await wrapper.dispatch(
            _binding("ollama", "llama3.2:latest"),
            _tiny_step(),
            step_context=_step_context_with_role("worker-slow"),
        )
    finally:
        await adapter.aclose()

    assert result_fast["model"] == model_fast
    assert result_slow["model"] == model_slow
    assert result_fast["model"] != result_slow["model"]


# ---------------------------------------------------------------------------
# B-EDIT-CARRIER-DURABLE-ASYNC-RESUME / Codex out-of-family [P1] — HITL terminal
# control-flow exceptions must PROPAGATE through the retry/fallback wrapper, never
# be retried or candidate-advanced.
#
# The production stage-5 stack is retry(HITL(bare_dispatcher)) — the wrapper's
# `inner` IS the HITL gate composer. A HITL gate REJECT (and an EDIT-decode
# failure) surfaces from `inner.dispatch` as an ordinary Exception; before the
# fix `_classify_provider_exception` mapped ALL Exceptions to TRANSIENT_RETRY, so
# the wrapper retried — and for a durable-async RESUME the retry re-entered an
# already-emptied resume holder and RE-PAUSED instead of surfacing the operator's
# terminal decision. These full-chain witnesses drive the REAL retry(HITL(...))
# stack (`[[full-chain-witness-not-half-proofs]]`): the terminal HITL exception
# propagates, and the composer's inner LLM dispatcher is invoked ZERO times (no
# retry, no candidate-advance, no re-pause).
# ---------------------------------------------------------------------------


def _real_hitl_composer_with_resume(inner_llm: Any, resume_response: Any) -> Any:
    """A REAL RuntimeHITLGateComposer with `resume_context_holder` primed so its
    Step-0 (§14.8.8.5) consume routes the resumed response. The ask/ledger/audit
    surfaces are never invoked on the resume short-circuit, so they are stubs."""
    from harness_cp.hitl_placement import HITLPlacementKind
    from harness_cp.pause_resume_protocol_types import ResumeContext
    from harness_od.audit_ledger_types import SignatureAlgorithm
    from harness_runtime.lifecycle.hitl_gate_composer import RuntimeHITLGateComposer

    tp, _ = _tracer_provider_with_exporter()
    composer = RuntimeHITLGateComposer(
        inner=inner_llm,
        applicable_placements=frozenset({HITLPlacementKind.PRE_ACTION}),
        ask_user_question_surface=cast(Any, object()),
        ledger_writer=cast(Any, object()),
        audit_writer=cast(Any, object()),
        tracer_provider=tp,
        audit_signing_key_id="test",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=lambda: cast(Any, "b" * 64),
    )
    composer.resume_context_holder.set(ResumeContext(hitl_response=resume_response))
    return composer


def _resume_hitl_result(response: Any, *, edited_proposal: Any = None) -> Any:
    from harness_core.identity import EntryID
    from harness_cp.hitl_placement import HITLResult

    return HITLResult(
        response=response,
        edited_proposal=edited_proposal,
        timestamp="2026-06-21T00:00:00Z",
        audit_ledger_entry_id=EntryID("e-resume-fullchain"),
        response_summary_hash="0" * 64,
    )


@pytest.mark.asyncio
async def test_retry_wrapper_propagates_resume_reject_terminally_no_retry_no_advance() -> None:
    """Full chain: a durable-async resume REJECT through retry(HITL(...)) raises
    HITLGateRejectedError terminally; the composer's inner LLM is dispatched ZERO
    times. The buggy TRANSIENT_RETRY path would retry → empty holder → fall
    through → dispatch the inner ≥1 time AND return success (no raise)."""
    from harness_cp.hitl_response_palette import HITLResponse
    from harness_runtime.lifecycle.hitl_gate_composer import HITLGateRejectedError

    tp, _ = _tracer_provider_with_exporter()
    inner_llm = _MockInnerDispatcher(outcomes=[{"never": "dispatched"}])
    composer = _real_hitl_composer_with_resume(inner_llm, _resume_hitl_result(HITLResponse.REJECT))
    # Multi-candidate chain + max_attempts=3 → proves NEITHER retry NOR advance.
    chain = _chain(
        _candidate("anthropic", "claude-test-1"),
        cross_family=(_candidate("openai", "gpt-test-1"),),
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=composer,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=3),
        fallback_chain=chain,
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    with pytest.raises(HITLGateRejectedError):
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner_llm.calls == []  # zero LLM dispatches — no retry, no advance, no re-pause


@pytest.mark.asyncio
async def test_retry_wrapper_propagates_resume_edit_decode_terminally() -> None:
    """Full chain: a durable-async resume EDIT with a None proposal through
    retry(HITL(...)) raises HITLGateEditDecodeError terminally; the composer's
    inner LLM is dispatched ZERO times (not retried as a transient failure)."""
    from harness_cp.hitl_response_palette import HITLResponse
    from harness_runtime.lifecycle.hitl_gate_composer import HITLGateEditDecodeError

    tp, _ = _tracer_provider_with_exporter()
    inner_llm = _MockInnerDispatcher(outcomes=[{"never": "dispatched"}])
    composer = _real_hitl_composer_with_resume(
        inner_llm, _resume_hitl_result(HITLResponse.EDIT, edited_proposal=None)
    )
    wrapper = RetryBreakerFallbackDispatcher(
        inner=composer,
        retry_breaker=_retry_breaker_with_llm_policy(max_attempts=3),
        fallback_chain=_chain(_candidate("anthropic", "claude-test-1")),
        tracer_provider=tp,
        sleep_fn=_noop_sleep,
    )

    with pytest.raises(HITLGateEditDecodeError):
        await wrapper.dispatch(_binding(), _step(), step_context=_step_context())
    assert inner_llm.calls == []
