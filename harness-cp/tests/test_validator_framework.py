"""Tests for U-CP-60 — C-CP-25 ValidatorFramework body + outcome→next_action mapping.

ACs from CP plan v2.16 §1 (U-CP-60):
  AC #1 Bijective-on-outcomes mapping (5 outcomes → 4 next_actions)
  AC #2 Burden count monotonic per workflow; tracked on `ctx.operator_burden_counter`
  AC #3 Single Validator per step invariant; raises MultipleValidatorsError
  AC #4 CP fail class CP-FAIL-VALIDATOR-PERMANENT raised on PERMANENT_FAIL
  AC #5 Unit test: each of 5 outcomes maps to documented next_action
  AC #6 REVALIDATE-budget-exhaustion-escalates-to-PERMANENT_FAIL test per §25.7 invariant 3
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from harness_core.identity import StepID
from harness_cp.validator_framework import (
    ConcreteValidatorFramework,
    CPFailValidatorPermanent,
    MultipleValidatorsError,
    _map_outcome_to_next_action,
)
from harness_cp.validator_framework_types import (
    ValidatorEvaluation,
    ValidatorFailClass,
    ValidatorFramework,
    ValidatorNextAction,
    ValidatorOutcome,
    ValidatorResult,
)
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep

# ----------------------------------------------------------------------------
# Test fixtures
# ----------------------------------------------------------------------------


def _make_step(step_id_str: str = "step-1") -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(step_id_str),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )


def _make_step_context(step_id_str: str = "step-1") -> StepExecutionContext:
    """Minimal StepExecutionContext for framework tests. Validator framework
    doesn't introspect most fields, only passes through to validator.validate()."""
    from harness_as.sandbox_tier import SandboxTier
    from harness_cp.sub_agent_gate_level_descent import GateLevel
    from harness_is.state_ledger_entry_schema import Actor, ActorClass

    return StepExecutionContext(
        workflow_id="wf-test",
        parent_action_id="workflow:wf-test:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-validator-fw"),
        parent_entry_hash="",
        parent_idempotency_key="idem-key-test",
        tenant_id=None,
        step_index=0,
    )


class _FixedOutcomeValidator:
    """Test double: returns a pre-configured ValidatorResult on every .validate()."""

    def __init__(self, result: ValidatorResult) -> None:
        self._result = result

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorResult:
        return self._result


# ----------------------------------------------------------------------------
# AC #1 + AC #5 — Bijective mapping (5 outcomes → 4 next_actions)
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected_next_action",
    [
        (ValidatorOutcome.PASS, ValidatorNextAction.PROCEED),
        (ValidatorOutcome.REVALIDATE, ValidatorNextAction.RETRY),
        (ValidatorOutcome.ESCALATE, ValidatorNextAction.ESCALATE_HITL),
        (ValidatorOutcome.PERMANENT_FAIL, ValidatorNextAction.ABORT),
        (ValidatorOutcome.OPERATOR_BURDEN_EXCEEDED, ValidatorNextAction.ESCALATE_HITL),
    ],
)
def test_outcome_maps_to_next_action(
    outcome: ValidatorOutcome,
    expected_next_action: ValidatorNextAction,
) -> None:
    """AC #1 + AC #5 — bijective mapping per §25.2."""
    assert _map_outcome_to_next_action(outcome) == expected_next_action


def test_mapping_covers_all_outcomes() -> None:
    """AC #1 — every ValidatorOutcome member has a mapping."""
    for outcome in ValidatorOutcome:
        result = _map_outcome_to_next_action(outcome)
        assert isinstance(result, ValidatorNextAction)


def test_mapping_not_bijective_on_next_actions() -> None:
    """AC #1 — ESCALATE_HITL is reached from both ESCALATE and OPERATOR_BURDEN_EXCEEDED."""
    assert (
        _map_outcome_to_next_action(ValidatorOutcome.ESCALATE) == ValidatorNextAction.ESCALATE_HITL
    )
    assert (
        _map_outcome_to_next_action(ValidatorOutcome.OPERATOR_BURDEN_EXCEEDED)
        == ValidatorNextAction.ESCALATE_HITL
    )


# ----------------------------------------------------------------------------
# Protocol satisfaction
# ----------------------------------------------------------------------------


def test_concrete_validator_framework_satisfies_protocol() -> None:
    """ConcreteValidatorFramework is structurally a ValidatorFramework."""
    framework = ConcreteValidatorFramework(validator_registry={})
    assert isinstance(framework, ValidatorFramework)


# ----------------------------------------------------------------------------
# AC #2 — Burden count monotonic per workflow
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burden_count_zero_on_construction() -> None:
    """AC #2 — burden_count starts at 0."""
    framework = ConcreteValidatorFramework(validator_registry={})
    assert framework.burden_count == 0


@pytest.mark.asyncio
async def test_burden_count_unchanged_on_pass() -> None:
    """AC #2 — PASS outcome does NOT increment burden_count."""
    step = _make_step()
    validator = _FixedOutcomeValidator(ValidatorResult(outcome=ValidatorOutcome.PASS))
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    await framework.evaluate(step, {}, step_context=_make_step_context())
    assert framework.burden_count == 0

    await framework.evaluate(step, {}, step_context=_make_step_context())
    assert framework.burden_count == 0


@pytest.mark.asyncio
async def test_burden_count_increments_on_non_pass() -> None:
    """AC #2 — every non-PASS outcome increments burden_count monotonically."""
    step = _make_step()
    validator = _FixedOutcomeValidator(
        ValidatorResult(
            outcome=ValidatorOutcome.REVALIDATE,
            fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        )
    )
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    await framework.evaluate(step, {}, step_context=_make_step_context())
    assert framework.burden_count == 1

    await framework.evaluate(step, {}, step_context=_make_step_context())
    assert framework.burden_count == 2

    await framework.evaluate(step, {}, step_context=_make_step_context())
    assert framework.burden_count == 3


# ----------------------------------------------------------------------------
# AC #3 — MultipleValidatorsError typed error exists
# ----------------------------------------------------------------------------


def test_multiple_validators_error_is_exception() -> None:
    """AC #3 — MultipleValidatorsError carries step_id + count."""
    err = MultipleValidatorsError(StepID("step-x"), count=3)
    assert isinstance(err, Exception)
    assert err.step_id == StepID("step-x")
    assert err.count == 3
    assert "step-x" in str(err)


# ----------------------------------------------------------------------------
# AC #4 — CP-FAIL-VALIDATOR-PERMANENT typed error
# ----------------------------------------------------------------------------


def test_cp_fail_validator_permanent_carries_fail_class_attribute() -> None:
    """AC #4 — typed error has fail_class = 'CP-FAIL-VALIDATOR-PERMANENT'."""
    err = CPFailValidatorPermanent(StepID("step-z"), ValidatorFailClass.SAFETY_POLICY)
    assert err.fail_class == "CP-FAIL-VALIDATOR-PERMANENT"
    assert err.step_id == StepID("step-z")
    assert err.validator_fail_class == ValidatorFailClass.SAFETY_POLICY


def test_cp_fail_validator_permanent_nullable_fail_class() -> None:
    """AC #4 — typed error tolerates missing validator_fail_class."""
    err = CPFailValidatorPermanent(StepID("step-q"), None)
    assert err.validator_fail_class is None


# ----------------------------------------------------------------------------
# AC #5 — evaluate() integrates mapping + span attrs
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_returns_validator_evaluation_with_correct_next_action() -> None:
    """AC #5 — evaluate produces ValidatorEvaluation with §25.2 next_action."""
    step = _make_step()
    validator = _FixedOutcomeValidator(
        ValidatorResult(
            outcome=ValidatorOutcome.ESCALATE,
            fail_class=ValidatorFailClass.SAFETY_POLICY,
            fail_detail_hash="d" * 64,
        )
    )
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    evaluation = await framework.evaluate(step, {}, step_context=_make_step_context())

    assert isinstance(evaluation, ValidatorEvaluation)
    assert evaluation.result.outcome == ValidatorOutcome.ESCALATE
    assert evaluation.next_action == ValidatorNextAction.ESCALATE_HITL
    assert evaluation.burden_count == 1


@pytest.mark.asyncio
async def test_evaluate_span_attributes_include_required_envelope_keys() -> None:
    """AC #5 — span attrs include §25.5 envelope keys."""
    step = _make_step()
    validator = _FixedOutcomeValidator(ValidatorResult(outcome=ValidatorOutcome.PASS))
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    evaluation = await framework.evaluate(step, {}, step_context=_make_step_context())

    assert evaluation.span_attributes["step.id"] == str(step.step_id)
    assert evaluation.span_attributes["validator.outcome"] == "pass"
    assert evaluation.span_attributes["validator.burden_count_cumulative"] == 0


@pytest.mark.asyncio
async def test_evaluate_span_attributes_include_fail_keys_on_non_pass() -> None:
    """AC #5 — span attrs include validator.fail.* on non-PASS outcomes."""
    step = _make_step()
    validator = _FixedOutcomeValidator(
        ValidatorResult(
            outcome=ValidatorOutcome.PERMANENT_FAIL,
            fail_class=ValidatorFailClass.SAFETY_POLICY,
            fail_detail_hash="e" * 64,
        )
    )
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    evaluation = await framework.evaluate(step, {}, step_context=_make_step_context())

    assert evaluation.span_attributes["validator.fail.next_action"] == "abort"
    assert evaluation.span_attributes["validator.fail.escalation_owed"] is False
    assert evaluation.span_attributes["validator.fail.class"] == "safety_policy"
    assert evaluation.span_attributes["validator.fail.detail_hash"] == "e" * 64


# ----------------------------------------------------------------------------
# AC #6 — REVALIDATE-budget-exhaustion conversion
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_revalidate_to_permanent_fail_basic_conversion() -> None:
    """AC #6 — convert_revalidate_to_permanent_fail returns PERMANENT_FAIL evaluation.

    Simulates the workflow-driver hook (U-CP-61) invoking the conversion
    after the C-RT-16 retry wrapper exhausts the policy budget per §25.7
    invariant 3.
    """
    step = _make_step()
    validator = _FixedOutcomeValidator(
        ValidatorResult(
            outcome=ValidatorOutcome.REVALIDATE,
            fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        )
    )
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    revalidate_evaluation = await framework.evaluate(step, {}, step_context=_make_step_context())
    assert revalidate_evaluation.result.outcome == ValidatorOutcome.REVALIDATE
    assert revalidate_evaluation.next_action == ValidatorNextAction.RETRY

    converted = framework.convert_revalidate_to_permanent_fail(
        revalidate_evaluation, step_id=step.step_id
    )

    assert converted.result.outcome == ValidatorOutcome.PERMANENT_FAIL
    assert converted.result.fail_class == ValidatorFailClass.RESOURCE_CONSTRAINT
    assert converted.next_action == ValidatorNextAction.ABORT
    assert (
        converted.span_attributes["validator.revalidation.terminal_conversion"] == "permanent_fail"
    )


@pytest.mark.asyncio
async def test_convert_revalidate_rejects_non_revalidate_outcome() -> None:
    """AC #6 — conversion is only legal on REVALIDATE outcomes."""
    step = _make_step()
    validator = _FixedOutcomeValidator(ValidatorResult(outcome=ValidatorOutcome.PASS))
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    pass_evaluation = await framework.evaluate(step, {}, step_context=_make_step_context())

    with pytest.raises(ValueError, match="only REVALIDATE may be converted"):
        framework.convert_revalidate_to_permanent_fail(pass_evaluation, step_id=step.step_id)


@pytest.mark.asyncio
async def test_convert_revalidate_preserves_burden_count() -> None:
    """AC #6 — conversion preserves the cumulative burden count (no double-charge)."""
    step = _make_step()
    validator = _FixedOutcomeValidator(ValidatorResult(outcome=ValidatorOutcome.REVALIDATE))
    framework = ConcreteValidatorFramework(validator_registry={step.step_id: validator})

    # Three REVALIDATE evaluations before exhaustion-conversion:
    for _ in range(3):
        eval_n = await framework.evaluate(step, {}, step_context=_make_step_context())
    assert framework.burden_count == 3

    converted = framework.convert_revalidate_to_permanent_fail(eval_n, step_id=step.step_id)
    assert converted.burden_count == 3  # not incremented by conversion
