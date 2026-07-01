"""Tests for U-OD-50 — C-OD-29 `validator.*` canonical namespace schema +
ValidatorEscalationAuditPayload.

ACs from OD plan v2.14 §1 U-OD-50:
  AC #1 Schema declares all 11 attributes across 4 span sites per §C-OD-29.1
  AC #2 ValidatorEscalationAuditPayload extends AuditPayload (via §24.6 sub-namespace
        discipline) with 4 validator-specific fields
  AC #3 Schema attribute names byte-exact match CP spec v1.10 §25.5 producer-side
        (Pattern-P1 alignment)
  AC #4 Cardinality + type annotations match §C-OD-29.1
  AC #5 Unit test: schema declaration matches §C-OD-29.1 row-by-row
"""

from __future__ import annotations

import pytest
from harness_core import AttributeValueType, Cardinality
from harness_od.validator_namespace import (
    SPAN_SITE_VALIDATOR_ESCALATION,
    SPAN_SITE_VALIDATOR_EVALUATE,
    SPAN_SITE_VALIDATOR_FAIL,
    SPAN_SITE_VALIDATOR_REVALIDATION,
    VALIDATOR_SPAN_NAMESPACE_SCHEMA,
    ValidatorEscalationAuditPayload,
)
from pydantic import ValidationError

# ----------------------------------------------------------------------------
# AC #1 + AC #5 — schema declares all 11 attributes; row-by-row match
# ----------------------------------------------------------------------------


def test_schema_has_exactly_eleven_attributes() -> None:
    """AC #1 — exactly 11 attributes across 4 span sites."""
    assert len(VALIDATOR_SPAN_NAMESPACE_SCHEMA) == 11


def test_schema_has_four_distinct_span_sites() -> None:
    """AC #1 — 4 distinct span sites per §C-OD-29.1."""
    sites = {attr.span_site for attr in VALIDATOR_SPAN_NAMESPACE_SCHEMA.values()}
    assert sites == {
        SPAN_SITE_VALIDATOR_EVALUATE,
        SPAN_SITE_VALIDATOR_FAIL,
        SPAN_SITE_VALIDATOR_REVALIDATION,
        SPAN_SITE_VALIDATOR_ESCALATION,
    }


@pytest.mark.parametrize(
    "site,expected_count",
    [
        (SPAN_SITE_VALIDATOR_EVALUATE, 3),
        (SPAN_SITE_VALIDATOR_FAIL, 4),
        (SPAN_SITE_VALIDATOR_REVALIDATION, 2),
        (SPAN_SITE_VALIDATOR_ESCALATION, 2),
    ],
)
def test_per_site_attribute_count(site: str, expected_count: int) -> None:
    """AC #1 — site-decomposition: 3 / 4 / 2 / 2 (= 11)."""
    site_attrs = [a for a in VALIDATOR_SPAN_NAMESPACE_SCHEMA.values() if a.span_site == site]
    assert len(site_attrs) == expected_count


# ----------------------------------------------------------------------------
# AC #3 — Pattern-P1 alignment with CP spec v1.10 §25.5 producer-side
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute_name",
    [
        # validator.evaluate outer (3)
        "step.id",
        "validator.outcome",
        "validator.burden_count_cumulative",
        # validator.fail event (4)
        "validator.fail.class",
        "validator.fail.detail_hash",
        "validator.fail.next_action",
        "validator.fail.escalation_owed",
        # validator.revalidation event (2)
        "validator.revalidation.payload_size_bytes",
        "validator.revalidation.attempt_number",
        # validator.escalation event (2)
        "validator.escalation.parent_hitl_span_id",
        "validator.escalation.fail_class",
    ],
)
def test_attribute_name_present_byte_exact(attribute_name: str) -> None:
    """AC #3 — every §C-OD-29.1 attribute name resolves byte-exact."""
    assert attribute_name in VALIDATOR_SPAN_NAMESPACE_SCHEMA
    assert VALIDATOR_SPAN_NAMESPACE_SCHEMA[attribute_name].attribute_name == attribute_name


def test_schema_pattern_p1_alignment_with_cp_validator_framework() -> None:
    """AC #3 — the CP-side ConcreteValidatorFramework span_attributes use
    byte-exact attribute names. Test invokes the framework + verifies emitted
    keys are a subset of the OD canonical schema."""
    import asyncio
    from collections.abc import Mapping
    from typing import Any

    from harness_as.sandbox_tier import SandboxTier
    from harness_core.identity import StepID

    # Replicates the U-CP-60 test fixture inline:
    from harness_cp.sub_agent_gate_level_descent import GateLevel
    from harness_cp.validator_framework import ConcreteValidatorFramework
    from harness_cp.validator_framework_types import (
        ValidatorFailClass,
        ValidatorOutcome,
        ValidatorResult,
    )
    from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
    from harness_is.state_ledger_entry_schema import Actor, ActorClass

    step = WorkflowStep(
        step_id=StepID("step-pattern-p1"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )
    ctx = StepExecutionContext(
        workflow_id="wf-pp1",
        parent_action_id="workflow:wf-pp1:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="pp1"),
        parent_entry_hash="",
        parent_idempotency_key="pp1-idem",
        tenant_id=None,
        step_index=0,
    )

    class _FailingValidator:
        async def validate(
            self,
            step: WorkflowStep,
            step_result: Mapping[str, Any],
            *,
            step_context: StepExecutionContext,
        ) -> ValidatorResult:
            return ValidatorResult(
                outcome=ValidatorOutcome.PERMANENT_FAIL,
                fail_class=ValidatorFailClass.SAFETY_POLICY,
                fail_detail_hash="a" * 64,
            )

    framework = ConcreteValidatorFramework(
        validator_registry={step.step_id: _FailingValidator()},
    )
    evaluation = asyncio.run(framework.evaluate(step, {}, step_context=ctx))

    # Every CP-emitted key must be a recognized validator.* OD canonical key
    # OR a step.id (also in the schema). non-validator.* keys are not constrained.
    validator_keys_emitted = {
        k for k in evaluation.span_attributes.keys() if k.startswith("validator.") or k == "step.id"
    }
    canonical_keys = set(VALIDATOR_SPAN_NAMESPACE_SCHEMA.keys())
    unrecognized = validator_keys_emitted - canonical_keys
    assert not unrecognized, f"CP framework emitted non-canonical keys: {unrecognized}"


# ----------------------------------------------------------------------------
# AC #4 — Cardinality + type annotations match §C-OD-29.1
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute_name,expected_value_type,expected_cardinality",
    [
        ("step.id", AttributeValueType.STRING, Cardinality.HIGH),
        ("validator.outcome", AttributeValueType.ENUM_REF, Cardinality.LOW),
        ("validator.burden_count_cumulative", AttributeValueType.INT, Cardinality.HIGH),
        ("validator.fail.class", AttributeValueType.ENUM_REF, Cardinality.LOW),
        ("validator.fail.detail_hash", AttributeValueType.STRING, Cardinality.HIGH),
        ("validator.fail.next_action", AttributeValueType.ENUM_REF, Cardinality.LOW),
        ("validator.fail.escalation_owed", AttributeValueType.BOOL, Cardinality.LOW),
        ("validator.revalidation.payload_size_bytes", AttributeValueType.INT, Cardinality.HIGH),
        ("validator.revalidation.attempt_number", AttributeValueType.INT, Cardinality.LOW),
        ("validator.escalation.parent_hitl_span_id", AttributeValueType.STRING, Cardinality.HIGH),
        ("validator.escalation.fail_class", AttributeValueType.ENUM_REF, Cardinality.LOW),
    ],
)
def test_attribute_value_type_and_cardinality_match_spec(
    attribute_name: str,
    expected_value_type: AttributeValueType,
    expected_cardinality: Cardinality,
) -> None:
    """AC #4 + AC #5 — each attribute's value_type + cardinality matches §C-OD-29.1."""
    attr = VALIDATOR_SPAN_NAMESPACE_SCHEMA[attribute_name]
    assert attr.value_type == expected_value_type
    assert attr.cardinality == expected_cardinality


# ----------------------------------------------------------------------------
# AC #2 — ValidatorEscalationAuditPayload 8-field shape
# ----------------------------------------------------------------------------


def test_validator_escalation_audit_payload_full_construction() -> None:
    """AC #2 — instantiable with all 8 fields (4 CP-sourced + 4 validator-specific)."""
    payload = ValidatorEscalationAuditPayload(
        audit_cp_action_id="validator:workflow:wf:step:0:safety_policy",
        audit_cp_response="permanent_fail",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        validator_fail_class="safety_policy",
        validator_fail_detail_hash="a" * 64,
        validator_next_action="abort",
        validator_escalation_owed=False,
    )
    assert payload.audit_cp_action_id.startswith("validator:")
    assert payload.validator_fail_class == "safety_policy"
    assert payload.validator_next_action == "abort"
    assert payload.validator_escalation_owed is False


def test_validator_escalation_audit_payload_frozen() -> None:
    """AC #2 — Pydantic v2 frozen=True rejects mutation."""
    payload = ValidatorEscalationAuditPayload(
        audit_cp_action_id="validator:x:y:z",
        audit_cp_response="escalate",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        validator_fail_class="external_rejection",
        validator_fail_detail_hash="b" * 64,
        validator_next_action="escalate_hitl",
        validator_escalation_owed=True,
    )
    with pytest.raises(ValidationError):
        payload.validator_next_action = "abort"  # type: ignore[misc]


def test_validator_escalation_audit_payload_rejects_extra_fields() -> None:
    """AC #2 — extra='forbid' rejects unknown fields."""
    with pytest.raises(ValidationError):
        ValidatorEscalationAuditPayload(
            audit_cp_action_id="validator:x:y:z",
            audit_cp_response="escalate",
            audit_cp_timestamp="",
            audit_cp_prior_event_hash="0" * 64,
            validator_fail_class="external_rejection",
            validator_fail_detail_hash="b" * 64,
            validator_next_action="escalate_hitl",
            validator_escalation_owed=True,
            unknown_field="extra",  # type: ignore[call-arg]
        )


# ----------------------------------------------------------------------------
# AttributeSpec carrier hygiene
# ----------------------------------------------------------------------------


def test_attribute_spec_frozen() -> None:
    """AttributeSpec rejects mutation."""
    spec = VALIDATOR_SPAN_NAMESPACE_SCHEMA["step.id"]
    with pytest.raises(ValidationError):
        spec.attribute_name = "step.modified"  # type: ignore[misc]
