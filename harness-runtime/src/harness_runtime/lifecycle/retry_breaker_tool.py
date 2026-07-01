"""Retry-only tool-dispatch composer wrapping C-RT-19 — stage 5 LOOP_INIT
(U-RT-74, opens L9-septies).

Per `Spec_Harness_Runtime_v1.md` v1.16 §14.11 C-RT-21 — sibling carrier to
§14.6 C-RT-16 `RetryBreakerFallbackDispatcher`. Wraps the bare C-RT-19
`RuntimeToolDispatcher` with per-step retry orchestration only. NO fallback
chain (tool dispatch has no provider/model candidate semantics). NO breaker
at v1.15 MVP (deferred to future OD-axis-coordinated arc per Q1a=(i)
ratification — extending `BreakerScope` to per-tool/per-server would route
to OD spec back-flow). Satisfies the same `harness_cp.workflow_driver.
StepDispatcher` Protocol that the inner dispatcher satisfies — from the CP
driver's perspective the wrapper IS the tool dispatcher.

Per-step invocation discipline (the body of
`RetryBreakerToolDispatcher.dispatch(binding, step, *, step_context)`):

  1. Look up `RetryPolicy` from the registry under the reserved
     `"tool_dispatch"` key — operator may not declare a tool by that name
     (enforced at manifest-validation time at C-RT-16 §14.6 D6).
  2. Start outer span `harness.runtime.retry_tool_dispatch` covering the
     full retry envelope.
  3. Per-attempt loop bounded by `policy.max_attempts`:
     - Start inner span `harness.runtime.tool_retry_attempt` carrying the
       C-CP-03 §3.5 `retry.*` 6-attribute namespace.
     - Dispatch via `self.inner.dispatch(binding, step, step_context=...)`.
     - On success: annotate `retry.terminal=success`; return.
     - On retry-eligible transient (`ToolInvocationTimeoutError` or
       `MCPHostUnreachableError` — the only NO entries in spec §14.9.5
       "Permanent?" column): advance staircase; on RETRY_WITH_BACKOFF and
       remaining attempts, sleep jittered backoff and continue; on
       exhaustion break out.
     - On fail-fast permanent (any other §14.9.5 typed error): annotate
       `retry.terminal=fail-fast`; re-raise verbatim.
  4. On `policy.max_attempts` exhaustion: emit `tool_retry.exhausted`
     event on outer span; raise `RetryToolExhaustedError` (maps to
     `RT-FAIL-TOOL-RETRY-EXHAUSTED` per §14 fail-class taxonomy).

Two nesting levels per composer invocation (canonical OTel retry-wrapper
pattern): outer `harness.runtime.retry_tool_dispatch` → per-attempt
`harness.runtime.tool_retry_attempt` → inner C-RT-19 `tool.dispatch` +
`sandbox.enter` + `mcp.tool.call` + `sandbox.exit`. Head sampler picks
outer; tail sampler picks per-attempt.

**OTel context-manager note.** OTel tracer `start_as_current_span` is sync;
inside this async function we use plain `with` per OTel API contract
(matches §14.6 sibling phrasing).

**Framework-pull discipline.** Hand-rolled per CLAUDE.md §3.2 — NO tenacity
/ pybreaker / circuitbreaker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from harness_cp.engine_namespace import REPLAY_DISPOSITION_MAPPING
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.routing_manifest_residence import RetryPolicy
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass
from harness_cp.validator_fail_transient_staircase import StaircaseStage
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep

from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    MCPHostUnreachableError,
    ToolInvocationTimeoutError,
)
from harness_runtime.types import RetryBreakerRegistry

__all__ = [
    "DEFAULT_TOOL_DISPATCH_RETRY_POLICY",
    "RESERVED_TOOL_DISPATCH_KEY",
    "RetryBreakerToolDispatcher",
    "RetryToolExhaustedError",
]


RESERVED_TOOL_DISPATCH_KEY = "tool_dispatch"
"""Reserved registry key for the tool-dispatch retry policy.

The runtime composer reserves this key for tool-dispatch retry policy
lookup; tools may not declare a tool named ``"tool_dispatch"`` (enforced at
manifest-validation time via the existing reserved-name discipline at
C-RT-16 §14.6 D6). The default policy below mirrors the §14.6 LLM-dispatch
default per spec §14.11."""


DEFAULT_TOOL_DISPATCH_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff="full_jitter",
    jitter="full_jitter",
)
"""Default ``RetryPolicy`` for the reserved ``"tool_dispatch"`` registry key
(per spec §14.11 step 1 default-fallback prose; mirrors §14.6)."""


class RetryToolExhaustedError(Exception):
    """Raised when per-step tool-dispatch retry exhausts `policy.max_attempts`
    under the reserved ``"tool_dispatch"`` policy.

    Maps to ``RT-FAIL-TOOL-RETRY-EXHAUSTED`` per `Spec_Harness_Runtime_v1.md`
    v1.16 §14.11 failure-mode taxonomy (new row at v1.15). The driver
    ``try/except`` at ``workflow_driver.py`` catches and maps to
    ``step-failure: RT-FAIL-TOOL-RETRY-EXHAUSTED: ...`` per C-CP-25 §25.3.3.4.

    Carries the last failure class for operator-facing attribution.
    """

    def __init__(self, last_failure_class: str, max_attempts: int) -> None:
        self.last_failure_class = last_failure_class
        self.max_attempts = max_attempts
        super().__init__(
            f"RT-FAIL-TOOL-RETRY-EXHAUSTED: tool-dispatch exhausted "
            f"after {max_attempts} attempts "
            f"(last_failure_class={last_failure_class!r})"
        )


_TRANSIENT_TOOL_DISPATCH_ERRORS: tuple[type[BaseException], ...] = (
    ToolInvocationTimeoutError,
    MCPHostUnreachableError,
)
"""The only §14.9.5 fail classes whose ``Permanent?`` column reads NO.

All other §14.9.5 typed errors propagate as fail-fast per spec §14.11."""


@dataclass(slots=True)
class RetryBreakerToolDispatcher:
    """Per-step retry-only tool-dispatch composer (C-RT-21).

    Wraps the bare C-RT-19 ``RuntimeToolDispatcher`` (or any
    ``StepDispatcher`` Protocol-satisfying inner) with the per-attempt
    loop + jittered backoff between attempts. Satisfies the
    ``harness_cp.workflow_driver.StepDispatcher`` Protocol via the same
    ``runtime_checkable`` introspection that the inner dispatcher satisfies.

    Attributes
    ----------
    inner :
        The inner ``StepDispatcher`` (typically ``RuntimeToolDispatcher``).
        Invoked exactly once per per-attempt iteration with the binding +
        step + step_context unchanged (no rebind — tool dispatch has no
        candidate parameter to override).
    retry_breaker :
        The U-RT-24 registry. Used for policy lookup (``get_policy``),
        staircase advancement (``advance_staircase``), and jittered backoff
        delay computation (``compute_delay_seconds``). Note: per Q1a=(i)
        ratification, ``get_breaker`` / breaker transition events are NOT
        invoked at v1.15.
    tracer_provider :
        The stage 4 ``TracerProvider`` for outer + inner span emission.
        Typed ``Any`` for the same C-RT-04 reason ``RuntimeToolDispatcher``
        uses (avoids pulling the OTel SDK type into the schema at L0).
    sleep_fn :
        Awaitable sleep function for jittered backoff between attempts.
        Defaults to ``asyncio.sleep``; tests inject a recording no-op for
        deterministic async tests.
    """

    inner: Any  # StepDispatcher Protocol (typed Any to keep schema L0-free)
    retry_breaker: RetryBreakerRegistry
    tracer_provider: Any
    sleep_fn: Callable[[float], Awaitable[None]] = field(default_factory=lambda: asyncio.sleep)

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Invoke the tool-step body under retry orchestration; return step output.

        Raises
        ------
        RetryToolExhaustedError
            Per-step retry exhausted ``policy.max_attempts`` while still
            seeing transient failures. Maps to
            ``RT-FAIL-TOOL-RETRY-EXHAUSTED``.
        asyncio.CancelledError
            Re-raised verbatim (shutdown / cancellation propagates).
        Exception
            Any §14.9.5 permanent-class typed error from the inner
            dispatcher propagates verbatim (fail-fast).
        """
        policy = self.retry_breaker.get_policy(RESERVED_TOOL_DISPATCH_KEY)
        tracer = self.tracer_provider.get_tracer("harness.runtime.retry_tool_dispatch")

        with tracer.start_as_current_span("harness.runtime.retry_tool_dispatch") as outer_span:
            original_span_id_hex = _format_span_id_hex(outer_span)
            replay_disposition = REPLAY_DISPOSITION_MAPPING[binding.engine_class]
            last_failure_class: str = "unknown"

            for attempt in range(policy.max_attempts):
                with tracer.start_as_current_span(
                    "harness.runtime.tool_retry_attempt"
                ) as inner_span:
                    inner_span.set_attribute("retry.attempt_number", attempt + 1)
                    inner_span.set_attribute("retry.original_span_id", original_span_id_hex)
                    inner_span.set_attribute("engine.replay_disposition", replay_disposition.value)

                    try:
                        result = await self.inner.dispatch(binding, step, step_context=step_context)
                    except asyncio.CancelledError:
                        raise
                    except _TRANSIENT_TOOL_DISPATCH_ERRORS as exc:
                        # Transient: classify + advance staircase.
                        cause_class = type(exc).__name__
                        is_last_attempt = attempt == policy.max_attempts - 1
                        transition = self.retry_breaker.advance_staircase(
                            StaircaseStage.STAGE_1_REFLEXION,
                            ValidatorRetryExitClass.TRANSIENT_RETRY,
                            attempt,
                        )
                        next_stage = transition.to_stage
                        if (
                            next_stage is StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF
                            and not is_last_attempt
                        ):
                            backoff_seconds = self.retry_breaker.compute_delay_seconds(attempt)
                            inner_span.set_attribute("retry.delay_ms", int(backoff_seconds * 1000))
                            inner_span.set_attribute(
                                "retry.cause_attribution",
                                ValidatorRetryExitClass.TRANSIENT_RETRY.value,
                            )
                            inner_span.set_attribute(
                                "retry.fail_class",
                                ValidatorRetryExitClass.TRANSIENT_RETRY.value,
                            )
                            inner_span.set_attribute("retry.terminal", "retry")
                            last_failure_class = cause_class
                        else:
                            # Exhaustion: either last attempt or staircase
                            # escalation past STAGE_2.
                            inner_span.set_attribute("retry.delay_ms", 0)
                            inner_span.set_attribute(
                                "retry.cause_attribution",
                                ValidatorRetryExitClass.TRANSIENT_RETRY.value,
                            )
                            inner_span.set_attribute(
                                "retry.fail_class",
                                ValidatorRetryExitClass.TERMINAL_FAIL_EXIT.value,
                            )
                            inner_span.set_attribute("retry.terminal", "max-attempts")
                            last_failure_class = cause_class
                            break
                    except Exception as exc:
                        # Fail-fast: any other §14.9.5 typed error propagates.
                        inner_span.set_attribute("retry.delay_ms", 0)
                        inner_span.set_attribute("retry.cause_attribution", type(exc).__name__)
                        inner_span.set_attribute(
                            "retry.fail_class",
                            ValidatorRetryExitClass.PERMANENT_FAIL_EXIT.value,
                        )
                        inner_span.set_attribute("retry.terminal", "fail-fast")
                        raise
                    else:
                        # Success.
                        inner_span.set_attribute("retry.delay_ms", 0)
                        inner_span.set_attribute("retry.terminal", "success")
                        return result

                # Sleep between retries (outside the inner span CM).
                # Only reached on the retry-with-backoff branch above.
                await self.sleep_fn(self.retry_breaker.compute_delay_seconds(attempt))

            # Exhausted: emit event + raise typed terminal error.
            outer_span.add_event(
                "tool_retry.exhausted",
                attributes={
                    "tool_retry.max_attempts": policy.max_attempts,
                    "tool_retry.last_failure_class": last_failure_class,
                },
            )
            raise RetryToolExhaustedError(
                last_failure_class=last_failure_class,
                max_attempts=policy.max_attempts,
            )


def _format_span_id_hex(span: Any) -> str:
    """Format an OTel span's ``span_id`` as 16-hex W3C trace-context.

    OTel SDK exposes ``span.get_span_context().span_id`` as a 64-bit
    integer; the W3C Trace Context spec formats it as 16 lowercase hex
    characters, zero-padded. Returns the empty string if the span isn't
    recording.
    """
    span_context = span.get_span_context() if hasattr(span, "get_span_context") else None
    if span_context is None or not getattr(span_context, "is_valid", True):
        return ""
    span_id_int = span_context.span_id
    return format(span_id_int, "016x")
