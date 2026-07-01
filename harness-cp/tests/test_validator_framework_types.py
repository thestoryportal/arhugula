"""Tests for U-CP-58 — C-CP-25 ValidatorFramework type carriers.

ACs from CP plan v2.16 §1 (= v2.15 §1 U-CP-58 preserved):
  AC #1 ValidatorOutcome has exactly 5 members matching spec §25.2 verbatim values
  AC #2 ValidatorFailClass has exactly 5 members matching spec §25.2
  AC #3 ValidatorNextAction has exactly 4 members (PROCEED / RETRY / ESCALATE_HITL / ABORT)
  AC #4 All enums frozen + hashable
  AC #5 pyright strict mode passes (verified at workspace `uv run pyright` invocation)
"""

from __future__ import annotations

from harness_cp.validator_framework_types import (
    ValidatorFailClass,
    ValidatorNextAction,
    ValidatorOutcome,
)

# --- AC #1 ----------------------------------------------------------------


def test_validator_outcome_has_exactly_five_members() -> None:
    """AC #1 — ValidatorOutcome 5 members."""
    assert len(ValidatorOutcome) == 5


def test_validator_outcome_member_values_verbatim() -> None:
    """AC #1 — member string values match spec §25.2 verbatim."""
    assert {c.value for c in ValidatorOutcome} == {
        "pass",
        "revalidate",
        "escalate",
        "permanent_fail",
        "operator_burden_exceeded",
    }


def test_validator_outcome_member_names() -> None:
    """AC #1 — member names PASS / REVALIDATE / ESCALATE / PERMANENT_FAIL / OPERATOR_BURDEN_EXCEEDED."""
    assert {c.name for c in ValidatorOutcome} == {
        "PASS",
        "REVALIDATE",
        "ESCALATE",
        "PERMANENT_FAIL",
        "OPERATOR_BURDEN_EXCEEDED",
    }


# --- AC #2 ----------------------------------------------------------------


def test_validator_fail_class_has_exactly_five_members() -> None:
    """AC #2 — ValidatorFailClass 5 members."""
    assert len(ValidatorFailClass) == 5


def test_validator_fail_class_member_values_verbatim() -> None:
    """AC #2 — member string values match spec §25.2 verbatim."""
    assert {c.value for c in ValidatorFailClass} == {
        "schema_violation",
        "semantic_inconsistency",
        "safety_policy",
        "resource_constraint",
        "external_rejection",
    }


def test_validator_fail_class_distinct_from_retry_exit_class() -> None:
    """Path β disambiguation: the NEW C-CP-25 ValidatorFailClass is distinct
    from the OLD C-CP-21 ValidatorRetryExitClass at harness_cp.validator_fail_taxonomy.
    """
    from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass

    assert ValidatorFailClass is not ValidatorRetryExitClass
    new_values = {c.value for c in ValidatorFailClass}
    old_values = {c.value for c in ValidatorRetryExitClass}
    assert new_values.isdisjoint(old_values)


# --- AC #3 ----------------------------------------------------------------


def test_validator_next_action_has_exactly_four_members() -> None:
    """AC #3 — ValidatorNextAction 4 members."""
    assert len(ValidatorNextAction) == 4


def test_validator_next_action_member_names() -> None:
    """AC #3 — member names PROCEED / RETRY / ESCALATE_HITL / ABORT."""
    assert {c.name for c in ValidatorNextAction} == {
        "PROCEED",
        "RETRY",
        "ESCALATE_HITL",
        "ABORT",
    }


# --- AC #4 ----------------------------------------------------------------


def test_validator_outcome_hashable() -> None:
    """AC #4 — Enum members are hashable; can populate a set."""
    members = {c for c in ValidatorOutcome}
    assert len(members) == 5


def test_validator_fail_class_hashable() -> None:
    """AC #4 — Enum members are hashable."""
    members = {c for c in ValidatorFailClass}
    assert len(members) == 5


def test_validator_next_action_hashable() -> None:
    """AC #4 — Enum members are hashable."""
    members = {c for c in ValidatorNextAction}
    assert len(members) == 4


def test_enums_frozen_at_attribute_level() -> None:
    """AC #4 — Enum members reject mutation (StrEnum is immutable-by-design)."""
    import pytest

    with pytest.raises(AttributeError):
        ValidatorOutcome.PASS.value = "mutated"  # type: ignore[misc]


# ============================================================================
# U-CP-59 — Validator Protocol + 3 dataclasses
# ============================================================================


from collections.abc import Mapping
from typing import Any

import pytest
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_framework_types import (
    HITLEscalationBrief,
    Validator,
    ValidatorEvaluation,
    ValidatorFramework,
    ValidatorResult,
)
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep
from pydantic import ValidationError

# --- AC #1 — Validator Protocol signature matches §25.1 -------------------


def test_validator_protocol_signature_matches_spec() -> None:
    """AC #1 — Validator Protocol has the documented validate() signature."""

    class _ConcreteValidator:
        async def validate(
            self,
            step: WorkflowStep,
            step_result: Mapping[str, Any],
            *,
            step_context: StepExecutionContext,
        ) -> ValidatorResult:
            return ValidatorResult(outcome=ValidatorOutcome.PASS)

    instance = _ConcreteValidator()
    assert isinstance(instance, Validator)


def test_validator_framework_protocol_signature_matches_spec() -> None:
    """AC #1 — ValidatorFramework Protocol has the documented evaluate() signature."""

    class _ConcreteFramework:
        async def evaluate(
            self,
            step: WorkflowStep,
            step_result: Mapping[str, Any],
            *,
            step_context: StepExecutionContext,
        ) -> ValidatorEvaluation:
            raise NotImplementedError

    assert isinstance(_ConcreteFramework(), ValidatorFramework)


# --- AC #2 — ValidatorResult 5-field shape --------------------------------


def test_validator_result_pass_minimal_construction() -> None:
    """AC #2 — outcome required; others optional."""
    result = ValidatorResult(outcome=ValidatorOutcome.PASS)
    assert result.outcome == ValidatorOutcome.PASS
    assert result.fail_class is None
    assert result.revalidation_payload is None
    assert result.escalation_brief is None
    assert result.fail_detail_hash is None


def test_validator_result_full_construction() -> None:
    """AC #2 — all 5 fields populatable; frozen."""
    brief = HITLEscalationBrief(
        parent_step_id="step-1",
        parent_action_id="action-1",
        fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        fail_detail_hash="a" * 64,
        escalation_reason="reason",
    )
    result = ValidatorResult(
        outcome=ValidatorOutcome.ESCALATE,
        fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        revalidation_payload={"key": "value"},
        escalation_brief=brief,
        fail_detail_hash="a" * 64,
    )
    assert result.outcome == ValidatorOutcome.ESCALATE
    assert result.fail_class == ValidatorFailClass.SCHEMA_VIOLATION
    assert result.escalation_brief is brief


def test_validator_result_frozen() -> None:
    """AC #5 — Pydantic v2 frozen=True rejects mutation."""
    result = ValidatorResult(outcome=ValidatorOutcome.PASS)
    with pytest.raises(ValidationError):
        result.outcome = ValidatorOutcome.PERMANENT_FAIL  # type: ignore[misc]


# --- AC #3 — ValidatorEvaluation burden_count -----------------------------


def test_validator_evaluation_includes_burden_count() -> None:
    """AC #3 — burden_count cumulative integer present + populates."""
    inner = ValidatorResult(outcome=ValidatorOutcome.PASS)
    evaluation = ValidatorEvaluation(
        result=inner,
        span_attributes={"validator.outcome": "pass"},
        next_action=ValidatorNextAction.PROCEED,
        burden_count=3,
    )
    assert evaluation.burden_count == 3
    assert evaluation.result is inner
    assert evaluation.next_action == ValidatorNextAction.PROCEED


# --- AC #4 — HITLEscalationBrief default palette --------------------------


def test_hitl_escalation_brief_default_palette_full() -> None:
    """AC #4 — proposed_response_palette defaults to C-CP-16 §16.1 full palette."""
    brief = HITLEscalationBrief(
        parent_step_id="step-x",
        parent_action_id="action-x",
        fail_class=ValidatorFailClass.SAFETY_POLICY,
        fail_detail_hash="b" * 64,
        escalation_reason="r",
    )
    assert brief.proposed_response_palette == frozenset(HITLResponse)
    assert len(brief.proposed_response_palette) == 4


def test_hitl_escalation_brief_explicit_palette_narrowed() -> None:
    """AC #4 — narrowed palette honored at construction."""
    narrowed: frozenset[HITLResponse] = frozenset(
        {HITLResponse.APPROVE, HITLResponse.REJECT, HITLResponse.RESPOND}
    )
    brief = HITLEscalationBrief(
        parent_step_id="step-y",
        parent_action_id="action-y",
        fail_class=ValidatorFailClass.EXTERNAL_REJECTION,
        fail_detail_hash="c" * 64,
        escalation_reason="r",
        proposed_response_palette=narrowed,
    )
    assert brief.proposed_response_palette == narrowed
    assert HITLResponse.EDIT not in brief.proposed_response_palette


def test_hitl_escalation_brief_frozen() -> None:
    """AC #5 — Pydantic v2 frozen=True on HITLEscalationBrief."""
    brief = HITLEscalationBrief(
        parent_step_id="s",
        parent_action_id="a",
        fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        fail_detail_hash="d" * 64,
        escalation_reason="r",
    )
    with pytest.raises(ValidationError):
        brief.escalation_reason = "mutated"  # type: ignore[misc]
