"""C-CP-25 ValidatorFramework body — U-CP-60 (CP plan v2.16 §1).

Concrete `ConcreteValidatorFramework` materializes the §25.1 Protocol surface
declared at U-CP-59 (`harness_cp.validator_framework_types.ValidatorFramework`).
Owns the per-step Validator dispatch + the ValidatorOutcome → ValidatorNextAction
bijective mapping table per §25.2 (F2-03 RATIFIED at Phase A.2):

| ValidatorOutcome           | ValidatorNextAction |
|----------------------------|---------------------|
| PASS                       | PROCEED             |
| REVALIDATE                 | RETRY               |
| ESCALATE                   | ESCALATE_HITL       |
| PERMANENT_FAIL             | ABORT               |
| OPERATOR_BURDEN_EXCEEDED   | ESCALATE_HITL       |

The mapping is bijective on outcomes (each outcome maps to exactly one
next_action) but NOT on next_actions (ESCALATE_HITL ← {ESCALATE,
OPERATOR_BURDEN_EXCEEDED}); consumers disambiguate via `validator.outcome`
span attribute per OD §C-OD-29.

**Burden counter ownership (impl discretion per §25.4 invariant 5).** The
framework owns a private monotonic `_BurdenCounter` shared across all
`.evaluate()` invocations within a workflow lifetime. Increments on every
non-PASS outcome. The §25.4 invariant 5 "reset only at workflow boundary"
is preserved by framework lifetime ≡ workflow lifetime (framework
instantiated at stage 5 LOOP_INIT per §25.3 + discarded at workflow close).

**REVALIDATE budget-exhaustion conversion (AC #6 per F2-03).** Per §25.7
invariant 3 ("REVALIDATE bounded by C-RT-16 retry policy"), when the retry
wrapper exhausts the policy budget the framework converts the surfaced
REVALIDATE outcome to PERMANENT_FAIL + emits CP-FAIL-VALIDATOR-PERMANENT.
Implemented as `convert_revalidate_to_permanent_fail()` invoked by the
U-CP-61 workflow-driver hook on retry exhaustion (NOT inside `.evaluate()`,
which has no visibility into retry state).

**Single Validator per step invariant (§25.7 invariant 1 + AC #3).** The
operator-supplied `validator_registry` is a Mapping keyed by step_id. The
framework raises `MultipleValidatorsError` at registry-conflict detection
(extracted from the registry's own duplicate-key handling).

Authority: CP spec v1.10 §25 (C-CP-25 ValidatorFramework); plan unit U-CP-60
(CP plan v2.16 §1 = v2.15 §1 preserved).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from harness_core.identity import StepID

from harness_cp.validator_framework_types import (
    Validator,
    ValidatorEvaluation,
    ValidatorFailClass,
    ValidatorFramework,
    ValidatorNextAction,
    ValidatorOutcome,
    ValidatorPostEvaluateHook,
    ValidatorResult,
)
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep

# ----------------------------------------------------------------------------
# Typed errors (CP fail classes per §25.6 + AC #3 + AC #4)
# ----------------------------------------------------------------------------


class MultipleValidatorsError(Exception):
    """Raised when the validator_registry contains multiple validators for the same step_id.

    Materializes the §25.7 invariant 1 ("every step has at most one Validator")
    + AC #3 of U-CP-60. Multi-validator per step is deferred to a future arc
    per §25.7 invariant 1 closing sentence.
    """

    def __init__(self, step_id: StepID, count: int) -> None:
        super().__init__(
            f"Multiple validators registered for step_id={step_id!r} (count={count}); "
            "§25.7 invariant 1 requires at most one Validator per step."
        )
        self.step_id = step_id
        self.count = count


# N818 suppressed below: the name mirrors the canonical CP fail-class identifier
# `CP-FAIL-VALIDATOR-PERMANENT` (per §25.6); the `Fail` stem is spec vocabulary,
# not an accidental missing `Error` suffix. Renaming would diverge from the
# contract identifier and ripple through 7 call sites.
class CPFailValidatorPermanent(Exception):  # noqa: N818
    """Raised when a Validator returns PERMANENT_FAIL (CP fail class
    `CP-FAIL-VALIDATOR-PERMANENT` per §25.6 + AC #4).

    Also raised as the terminal escalation of a REVALIDATE-budget-exhaustion
    conversion per §25.7 invariant 3 + AC #6.
    """

    fail_class: str = "CP-FAIL-VALIDATOR-PERMANENT"

    def __init__(self, step_id: StepID, validator_fail_class: ValidatorFailClass | None) -> None:
        msg = f"Validator returned PERMANENT_FAIL for step_id={step_id!r}"
        if validator_fail_class is not None:
            msg += f" (fail_class={validator_fail_class.value})"
        super().__init__(msg)
        self.step_id = step_id
        self.validator_fail_class = validator_fail_class


# ----------------------------------------------------------------------------
# Outcome → next_action bijective mapping (§25.2 + AC #1)
# ----------------------------------------------------------------------------


_OUTCOME_TO_NEXT_ACTION: Mapping[ValidatorOutcome, ValidatorNextAction] = {
    ValidatorOutcome.PASS: ValidatorNextAction.PROCEED,
    ValidatorOutcome.REVALIDATE: ValidatorNextAction.RETRY,
    ValidatorOutcome.ESCALATE: ValidatorNextAction.ESCALATE_HITL,
    ValidatorOutcome.PERMANENT_FAIL: ValidatorNextAction.ABORT,
    ValidatorOutcome.OPERATOR_BURDEN_EXCEEDED: ValidatorNextAction.ESCALATE_HITL,
}
"""§25.2 mapping table; bijective on outcomes; NOT bijective on next_actions."""


def _map_outcome_to_next_action(outcome: ValidatorOutcome) -> ValidatorNextAction:
    """Pure helper: bijective lookup from §25.2 mapping table. AC #1."""
    return _OUTCOME_TO_NEXT_ACTION[outcome]


# ----------------------------------------------------------------------------
# Framework body (§25.1 Protocol concretization + AC #2-6)
# ----------------------------------------------------------------------------


class ConcreteValidatorFramework:
    """Concrete ValidatorFramework per CP spec v1.10 §25.1.

    Materializes the Protocol declared at U-CP-59
    (`harness_cp.validator_framework_types.ValidatorFramework`) — verified by
    `isinstance(..., ValidatorFramework)` at runtime per Protocol's
    `runtime_checkable` decoration.

    The class name disambiguates from the Protocol of the same name in the
    types module — operator code imports the Protocol for type-checking;
    bootstrap stage 5 instantiates `ConcreteValidatorFramework`.
    """

    def __init__(
        self,
        validator_registry: Mapping[StepID, Validator],
        *,
        post_evaluate_hook: ValidatorPostEvaluateHook | None = None,
    ) -> None:
        """Construct with the operator-populated per-step Validator registry.

        Per §25.3 stage 5 instantiation. Single-Validator-per-step invariant
        enforced at construction (Mapping type prohibits duplicate keys, but
        the AC #3 test verifies the typed error raises if construction is
        passed e.g. a list-of-pairs with duplicates).

        Per CP spec v1.24 §28.10.2, optional `post_evaluate_hook` accepts an
        operator-supplied `ValidatorPostEvaluateHook` Protocol implementation.
        `None` default preserves all pre-v1.24 construction sites byte-
        identical. Non-`None` opts in to post-evaluate observability hook
        firing per §28.10.3 (best-effort swallow per §28.10.4 invariant 2).
        """
        self._validator_registry = validator_registry
        self._burden_count: int = 0
        self._post_evaluate_hook = post_evaluate_hook

    @property
    def burden_count(self) -> int:
        """Current cumulative burden count for the workflow (§25.4 invariant 5).

        Read-only public surface; incremented internally by `.evaluate()` on
        every non-PASS outcome. Monotonic per workflow lifetime; resets only
        at workflow boundary (≡ framework instance lifetime).
        """
        return self._burden_count

    async def evaluate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorEvaluation:
        """Run the per-step Validator + wrap into ValidatorEvaluation.

        Per §25.4 invocation discipline:
        1. Look up Validator from `validator_registry` by `step.step_id`
        2. Run `.validate()` to get ValidatorResult
        3. Increment burden_count on non-PASS outcomes (§25.4 invariant 5)
        4. Map outcome → next_action (§25.2 mapping table; AC #1)
        5. Build span_attributes for §25.5 emission (validator.* namespace)
        6. Return ValidatorEvaluation

        Per CP spec v1.24 §28.10.3, post-evaluate hook fires AFTER
        ValidatorEvaluation construction + BEFORE return when
        `self._post_evaluate_hook is not None`. Best-effort discipline
        per §28.10.4 invariant 2: hook exceptions swallowed at the
        firing site (cost-attribution is observability; MUST NOT fail
        dispatch). Elapsed-time scope per §28.10.4 invariant 5: covers
        validator-registry lookup through ValidatorEvaluation
        construction; excludes the hook firing itself.
        """
        start_monotonic_ns = time.monotonic_ns()

        validator = self._validator_registry[step.step_id]

        result: ValidatorResult = await validator.validate(
            step,
            step_result,
            step_context=step_context,
        )

        if result.outcome != ValidatorOutcome.PASS:
            self._burden_count += 1

        next_action = _map_outcome_to_next_action(result.outcome)
        span_attributes = self._build_span_attributes(
            step=step,
            result=result,
            next_action=next_action,
            burden_count=self._burden_count,
        )

        evaluation = ValidatorEvaluation(
            result=result,
            span_attributes=span_attributes,
            next_action=next_action,
            burden_count=self._burden_count,
        )

        # CP spec v1.24 §28.10.3 post-evaluate hook firing (best-effort).
        if self._post_evaluate_hook is not None:
            execution_time_ms = (time.monotonic_ns() - start_monotonic_ns) / 1_000_000.0
            try:
                await self._post_evaluate_hook.on_post_evaluate(
                    step=step,
                    step_context=step_context,
                    evaluation=evaluation,
                    execution_time_ms=execution_time_ms,
                )
            except Exception:
                pass  # §28.10.4 invariant 2 — observability MUST NOT fail dispatch

        return evaluation

    def convert_revalidate_to_permanent_fail(
        self,
        evaluation: ValidatorEvaluation,
        step_id: StepID,
    ) -> ValidatorEvaluation:
        """Convert a REVALIDATE evaluation to PERMANENT_FAIL on retry-budget exhaustion.

        Per §25.7 invariant 3 + AC #6 (Phase D iteration-1 F2-03 absorption).
        Invoked by the U-CP-61 workflow-driver hook on C-RT-16 retry exhaustion
        — the framework itself has no visibility into retry state. Re-wraps
        the inner ValidatorResult with outcome=PERMANENT_FAIL +
        fail_class=RESOURCE_CONSTRAINT (retry budget is a resource budget).

        Does NOT raise; returns a new ValidatorEvaluation. The hook MAY raise
        `CPFailValidatorPermanent` per §25.6 + AC #4 if the next_action=ABORT
        path requires workflow termination.
        """
        if evaluation.result.outcome != ValidatorOutcome.REVALIDATE:
            raise ValueError(
                f"convert_revalidate_to_permanent_fail invoked on outcome="
                f"{evaluation.result.outcome.value!r}; only REVALIDATE may be converted."
            )

        converted_result = evaluation.result.model_copy(
            update={
                "outcome": ValidatorOutcome.PERMANENT_FAIL,
                "fail_class": ValidatorFailClass.RESOURCE_CONSTRAINT,
            }
        )
        next_action = _map_outcome_to_next_action(ValidatorOutcome.PERMANENT_FAIL)
        span_attributes = self._build_span_attributes(
            step=None,
            step_id=step_id,
            result=converted_result,
            next_action=next_action,
            burden_count=evaluation.burden_count,
            converted_from_revalidate=True,
        )
        return ValidatorEvaluation(
            result=converted_result,
            span_attributes=span_attributes,
            next_action=next_action,
            burden_count=evaluation.burden_count,
        )

    def _build_span_attributes(
        self,
        *,
        result: ValidatorResult,
        next_action: ValidatorNextAction,
        burden_count: int,
        step: WorkflowStep | None = None,
        step_id: StepID | None = None,
        converted_from_revalidate: bool = False,
    ) -> Mapping[str, Any]:
        """Build the `validator.*` span attributes per §25.5.

        outer `validator.evaluate` envelope: step.id + validator.outcome +
        validator.burden_count_cumulative. Non-PASS adds `validator.fail` fields.
        """
        effective_step_id = step.step_id if step is not None else step_id
        attrs: dict[str, Any] = {
            "step.id": str(effective_step_id) if effective_step_id is not None else "",
            "validator.outcome": result.outcome.value,
            "validator.burden_count_cumulative": burden_count,
        }
        if result.outcome != ValidatorOutcome.PASS:
            attrs["validator.fail.next_action"] = next_action.value
            attrs["validator.fail.escalation_owed"] = (
                next_action == ValidatorNextAction.ESCALATE_HITL
            )
            if result.fail_class is not None:
                attrs["validator.fail.class"] = result.fail_class.value
            if result.fail_detail_hash is not None:
                attrs["validator.fail.detail_hash"] = result.fail_detail_hash
        if converted_from_revalidate:
            attrs["validator.revalidation.terminal_conversion"] = "permanent_fail"
        return attrs


# ============================================================================
# U-CP-61 — SyncValidatorFrameworkFacade (sync bridge over async framework)
# ============================================================================
#
# Mirrors `harness_runtime.lifecycle.sync_dispatcher_facade.SyncDispatcherFacade`
# (U-RT-59 precedent). The async/sync mismatch between spec §25.1
# `async def evaluate` and the sync `execute_workflow` driver is bridged via
# `asyncio.run_coroutine_threadsafe(coro, loop)` from the driver's
# worker thread (per `asyncio.to_thread(execute_workflow, ...)` at
# `harness_runtime/api.py`).


@runtime_checkable
class SyncValidatorFrameworkLike(Protocol):
    """Sync-facing ValidatorFramework surface consumed by the workflow_driver hook.

    The CP driver's `execute_workflow` is sync (per spec §25.3 + workflow_driver
    surface); ValidatorFramework.evaluate is async (per spec §25.1). The driver
    consumes the sync facade; the facade bridges to the async impl via the
    captured async event loop. Pattern mirrors `SyncDispatcherFacade` at
    `harness_runtime.lifecycle.sync_dispatcher_facade` (U-RT-59 precedent).
    """

    def evaluate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorEvaluation: ...


@dataclass(frozen=True)
class SyncValidatorFrameworkFacade:
    """Sync ValidatorFramework facade over an async ConcreteValidatorFramework.

    Construction MUST occur on the event loop that will host the eventual
    `asyncio.to_thread(execute_workflow, ...)` invocation. Use
    `materialize_sync_validator_framework_facade` rather than constructing
    directly so the loop is captured uniformly.

    Mirrors `SyncDispatcherFacade` from `harness_runtime.lifecycle.sync_dispatcher_facade`
    per U-RT-59 precedent for the same async/sync bridge pattern.
    """

    inner: ValidatorFramework
    loop: asyncio.AbstractEventLoop
    result_timeout_seconds: float

    def evaluate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorEvaluation:
        """Sync `evaluate`; bridges to captured async loop.

        Invoked from the CP driver's worker thread. Schedules
        `self.inner.evaluate(...)` onto `self.loop` via
        `asyncio.run_coroutine_threadsafe` and blocks on
        `future.result(timeout=self.result_timeout_seconds)`.

        Exception propagation: `future.result()` re-raises any exception
        raised by the inner coroutine verbatim (no wrapping).
        """
        coro = self.inner.evaluate(step, step_result, step_context=step_context)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=self.result_timeout_seconds)


def materialize_sync_validator_framework_facade(
    inner: ValidatorFramework,
    *,
    result_timeout_seconds: float,
) -> SyncValidatorFrameworkFacade:
    """Construct `SyncValidatorFrameworkFacade` capturing the running event loop.

    MUST be invoked from a coroutine running on the event loop that hosts
    the subsequent worker-thread `evaluate(...)` invocations. Bootstrap
    stage 5 (when wired) satisfies this — until then, operator-populated
    integration uses this factory directly.

    Raises
    ------
    RuntimeError
        If called from sync code or a non-loop-owning thread —
        `asyncio.get_running_loop()` propagates verbatim.
    """
    loop = asyncio.get_running_loop()
    return SyncValidatorFrameworkFacade(
        inner=inner,
        loop=loop,
        result_timeout_seconds=result_timeout_seconds,
    )
