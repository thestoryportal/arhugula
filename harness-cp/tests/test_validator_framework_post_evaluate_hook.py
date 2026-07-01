"""Tests for U-CP-73 — ValidatorPostEvaluateHook Protocol surface + firing site.

ACs from CP plan v2.27 §2 (U-CP-73) per CP spec v1.24 §28.10:
  AC #1  Protocol declared @runtime_checkable with correct async signature
  AC #2  ConcreteValidatorFramework.__init__ accepts optional kw-only post_evaluate_hook
  AC #3  None default preserves all 6 existing construction sites byte-identical
  AC #4  evaluate() fires hook EXACTLY ONCE per invocation when hook is not None
  AC #5  evaluate() measures execution_time_ms via time.monotonic_ns()
  AC #6  evaluate() swallows ALL hook exceptions; returns evaluation unchanged
  AC #7  Hook does NOT fire if evaluate() raises before ValidatorEvaluation construction
  AC #8  SyncValidatorFrameworkFacade transparent passthrough
  AC #9  convert_revalidate_to_permanent_fail does NOT fire hook

Reading-only fork doc anchor:
`.harness/class_1_fork_u_od_40_validator_post_evaluate_hook.md`
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core.identity import StepID
from harness_cp.sub_agent_gate_level_descent import GateLevel
from harness_cp.validator_framework import (
    ConcreteValidatorFramework,
    materialize_sync_validator_framework_facade,
)
from harness_cp.validator_framework_types import (
    ValidatorEvaluation,
    ValidatorFailClass,
    ValidatorOutcome,
    ValidatorPostEvaluateHook,
    ValidatorResult,
)
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _make_step(step_id_str: str = "step-1") -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(step_id_str),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )


def _make_step_context(step_id_str: str = "step-1") -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-test",
        parent_action_id="workflow:wf-test:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-hook"),
        parent_entry_hash="",
        parent_idempotency_key="idem-key-test",
        tenant_id=None,
        step_index=0,
    )


class _FixedValidator:
    """Returns a pre-configured ValidatorResult per .validate() call."""

    def __init__(self, result: ValidatorResult) -> None:
        self._result = result
        self.call_count = 0

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorResult:
        self.call_count += 1
        return self._result


class _RaisingValidator:
    """Raises on .validate() — used to verify hook does NOT fire on validator exception."""

    class Boom(Exception):
        pass

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorResult:
        raise self.Boom("validator failed")


class _RecordingHook:
    """Records every on_post_evaluate invocation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def on_post_evaluate(
        self,
        *,
        step: WorkflowStep,
        step_context: StepExecutionContext,
        evaluation: ValidatorEvaluation,
        execution_time_ms: float,
    ) -> None:
        self.calls.append(
            {
                "step": step,
                "step_context": step_context,
                "evaluation": evaluation,
                "execution_time_ms": execution_time_ms,
            }
        )


class _RaisingHook:
    """Raises on every on_post_evaluate call."""

    class Boom(Exception):
        pass

    async def on_post_evaluate(
        self,
        *,
        step: WorkflowStep,
        step_context: StepExecutionContext,
        evaluation: ValidatorEvaluation,
        execution_time_ms: float,
    ) -> None:
        raise self.Boom("hook failed")


def _pass_result() -> ValidatorResult:
    return ValidatorResult(
        outcome=ValidatorOutcome.PASS,
        fail_class=None,
        fail_detail_hash=None,
    )


# ----------------------------------------------------------------------------
# AC #1 — Protocol declaration
# ----------------------------------------------------------------------------


def test_validator_post_evaluate_hook_protocol_runtime_checkable() -> None:
    """AC #1 — Protocol decorated @runtime_checkable per CP spec v1.24 §28.10.1."""
    hook = _RecordingHook()
    assert isinstance(hook, ValidatorPostEvaluateHook)


def test_validator_post_evaluate_hook_signature_matches_spec() -> None:
    """AC #1 — Protocol's on_post_evaluate signature matches CP spec v1.24 §28.10.1.

    Required: async; kw-only; step + step_context + evaluation + execution_time_ms;
    returns None.
    """
    sig = inspect.signature(ValidatorPostEvaluateHook.on_post_evaluate)
    params = sig.parameters
    # Self + 4 declared params
    assert "self" in params
    assert "step" in params
    assert "step_context" in params
    assert "evaluation" in params
    assert "execution_time_ms" in params
    # All four post-self params kw-only
    for name in ("step", "step_context", "evaluation", "execution_time_ms"):
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must be kw-only per §28.10.1"
        )


# ----------------------------------------------------------------------------
# AC #2 + AC #3 — ConcreteValidatorFramework ctor extension
# ----------------------------------------------------------------------------


def test_concrete_validator_framework_ctor_optional_hook_default_none() -> None:
    """AC #3 — `post_evaluate_hook` defaults to None preserving pre-v1.24 sites."""
    fw = ConcreteValidatorFramework(validator_registry={})
    assert fw._post_evaluate_hook is None


def test_concrete_validator_framework_ctor_optional_hook_explicit_value() -> None:
    """AC #2 — `post_evaluate_hook` accepts a Protocol implementation."""
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(validator_registry={}, post_evaluate_hook=hook)
    assert fw._post_evaluate_hook is hook


def test_concrete_validator_framework_ctor_hook_kw_only() -> None:
    """AC #2 — hook param is keyword-only (cannot be passed positionally)."""
    hook = _RecordingHook()
    with pytest.raises(TypeError):
        ConcreteValidatorFramework({}, hook)  # type: ignore[misc]


# ----------------------------------------------------------------------------
# AC #4 + AC #5 — Hook fires once with correct kwargs + elapsed-time measurement
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_fires_hook_once_after_evaluation_construction() -> None:
    """AC #4 — hook fires EXACTLY ONCE per evaluate() invocation."""
    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(_pass_result())
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )

    evaluation = await fw.evaluate(step, {}, step_context=ctx)

    assert len(hook.calls) == 1
    assert hook.calls[0]["evaluation"] is evaluation


@pytest.mark.asyncio
async def test_evaluate_fires_hook_with_correct_kwargs() -> None:
    """AC #4 — hook receives step + step_context + evaluation + execution_time_ms."""
    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(_pass_result())
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )

    await fw.evaluate(step, {}, step_context=ctx)

    call = hook.calls[0]
    assert call["step"] is step
    assert call["step_context"] is ctx
    assert isinstance(call["evaluation"], ValidatorEvaluation)
    assert isinstance(call["execution_time_ms"], float)
    assert call["execution_time_ms"] >= 0.0


@pytest.mark.asyncio
async def test_evaluate_measures_execution_time_ms_via_monotonic_ns() -> None:
    """AC #5 — execution_time_ms is a non-negative float (monotonic measurement)."""
    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(_pass_result())
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )

    await fw.evaluate(step, {}, step_context=ctx)

    elapsed_ms = hook.calls[0]["execution_time_ms"]
    # Minimum monotonic resolution ≥ 0; should be a small positive number for an
    # async noop, well under 1 second.
    assert 0.0 <= elapsed_ms < 1000.0


# ----------------------------------------------------------------------------
# AC #6 — Best-effort swallow
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_swallows_hook_exception_returns_evaluation_unchanged() -> None:
    """AC #6 — hook exceptions swallowed; evaluation returned unchanged."""
    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(_pass_result())
    hook = _RaisingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )

    evaluation = await fw.evaluate(step, {}, step_context=ctx)

    # No exception propagated; evaluation returned per §28.10.4 invariant 2
    assert evaluation.result.outcome == ValidatorOutcome.PASS


# ----------------------------------------------------------------------------
# AC #7 — Hook does NOT fire on pre-construction exceptions
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_does_not_fire_hook_on_registry_miss() -> None:
    """AC #7 — registry-lookup miss raises KeyError; hook NOT fired."""
    step = _make_step("unknown-step")
    ctx = _make_step_context()
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={},
        post_evaluate_hook=hook,
    )

    with pytest.raises(KeyError):
        await fw.evaluate(step, {}, step_context=ctx)

    assert hook.calls == []


@pytest.mark.asyncio
async def test_evaluate_does_not_fire_hook_on_validator_exception() -> None:
    """AC #7 — validator.validate() raise propagates; hook NOT fired."""
    step = _make_step()
    ctx = _make_step_context()
    validator = _RaisingValidator()
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )

    with pytest.raises(_RaisingValidator.Boom):
        await fw.evaluate(step, {}, step_context=ctx)

    assert hook.calls == []


# ----------------------------------------------------------------------------
# AC #8 — SyncValidatorFrameworkFacade transparent passthrough
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_facade_transparent_hook_passthrough() -> None:
    """AC #8 — SyncValidatorFrameworkFacade fires hook via wrapped async fw.

    Sync facade bridges to the captured event loop; calling sync `evaluate`
    from sync code requires the loop to be running on another thread. Here
    we exercise the inner-fw path the facade delegates to, confirming the
    hook fires regardless of facade wrapping.
    """
    import asyncio as _asyncio

    step = _make_step()
    ctx = _make_step_context()
    validator = _FixedValidator(_pass_result())
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={step.step_id: validator},
        post_evaluate_hook=hook,
    )
    facade = materialize_sync_validator_framework_facade(fw, result_timeout_seconds=5.0)

    # Sync facade.evaluate() runs from a worker thread + bridges to the
    # captured loop. Exercise the facade via to_thread.
    evaluation = await _asyncio.to_thread(facade.evaluate, step, {}, step_context=ctx)

    assert evaluation.result.outcome == ValidatorOutcome.PASS
    assert len(hook.calls) == 1


# ----------------------------------------------------------------------------
# AC #9 — convert_revalidate_to_permanent_fail does NOT fire hook
# ----------------------------------------------------------------------------


def test_convert_revalidate_does_not_fire_hook() -> None:
    """AC #9 — conversion path is distinct surface; per §28.10.4 invariant 6."""
    step = _make_step()
    revalidate_result = ValidatorResult(
        outcome=ValidatorOutcome.REVALIDATE,
        fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        fail_detail_hash=None,
    )
    revalidate_evaluation = ValidatorEvaluation(
        result=revalidate_result,
        span_attributes={"step.id": "step-1"},
        next_action=__import__(
            "harness_cp.validator_framework_types",
            fromlist=["ValidatorNextAction"],
        ).ValidatorNextAction.RETRY,
        burden_count=1,
    )
    hook = _RecordingHook()
    fw = ConcreteValidatorFramework(
        validator_registry={},
        post_evaluate_hook=hook,
    )

    converted = fw.convert_revalidate_to_permanent_fail(
        revalidate_evaluation,
        step_id=step.step_id,
    )

    assert converted.result.outcome == ValidatorOutcome.PERMANENT_FAIL
    assert hook.calls == []  # conversion path does NOT fire post-evaluate hook
