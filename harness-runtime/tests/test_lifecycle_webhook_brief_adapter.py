"""Unit tests for `webhook_brief_adapter.project_brief_to_payload`.

Per `.harness/class_1_fork_webhook_composer_per_workflow_context_threading.md`
Reading (H) operator-ratified 2026-05-28 + runtime spec v1.34 §14.10.1
brief-surface absorption.
"""

from __future__ import annotations

from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_framework_types import (
    HITLEscalationBrief,
    ValidatorFailClass,
)
from harness_runtime.lifecycle.webhook_brief_adapter import project_brief_to_payload


def _make_brief(
    *,
    parent_step_id: str = "step-1",
    parent_action_id: str = "workflow:wf-1:step:0",
    fail_class: ValidatorFailClass | None = None,
    fail_detail_hash: str | None = None,
    escalation_reason: str = "durable_async_cell_synchrony",
    palette: frozenset[HITLResponse] = frozenset({HITLResponse.APPROVE, HITLResponse.REJECT}),
) -> HITLEscalationBrief:
    return HITLEscalationBrief(
        parent_step_id=parent_step_id,
        parent_action_id=parent_action_id,
        fail_class=fail_class,
        fail_detail_hash=fail_detail_hash,
        escalation_reason=escalation_reason,
        proposed_response_palette=palette,
    )


def test_projection_maps_parent_action_id_to_approval_id() -> None:
    brief = _make_brief(parent_action_id="workflow:abc:step:42")
    payload = project_brief_to_payload(brief, "idem-1")
    assert payload.approval_id == "workflow:abc:step:42"


def test_projection_pass_through_idempotency_key() -> None:
    brief = _make_brief()
    payload = project_brief_to_payload(brief, "idem-canonical-42")
    assert payload.idempotency_key == "idem-canonical-42"


def test_projection_maps_parent_action_id_to_gate_evaluation_ref() -> None:
    brief = _make_brief(parent_action_id="workflow:xyz:step:7")
    payload = project_brief_to_payload(brief, "idem-1")
    assert payload.gate_evaluation_ref == "workflow:xyz:step:7"


def test_projection_serializes_brief_fields_into_payload_body() -> None:
    brief = _make_brief(
        parent_step_id="step-99",
        escalation_reason="validator_escalated",
        fail_class=ValidatorFailClass.SCHEMA_VIOLATION,
        fail_detail_hash="0" * 64,
        palette=frozenset({HITLResponse.APPROVE, HITLResponse.REJECT}),
    )
    payload = project_brief_to_payload(brief, "idem-1")
    assert payload.payload_body["escalation_reason"] == "validator_escalated"
    assert payload.payload_body["parent_step_id"] == "step-99"
    assert payload.payload_body["fail_class"] == "schema_violation"
    assert payload.payload_body["fail_detail_hash"] == "0" * 64
    # Palette is sorted by enum value for deterministic serialization
    assert payload.payload_body["proposed_response_palette"] == sorted(["approve", "reject"])


def test_projection_handles_none_fail_class_and_hash() -> None:
    brief = _make_brief(fail_class=None, fail_detail_hash=None)
    payload = project_brief_to_payload(brief, "idem-1")
    assert payload.payload_body["fail_class"] is None
    assert payload.payload_body["fail_detail_hash"] is None
