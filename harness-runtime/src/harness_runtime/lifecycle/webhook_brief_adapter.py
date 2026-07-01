"""Adapter projecting `HITLEscalationBrief` → `WebhookPayload`.

Resolves the runtime spec v1.34 §14.10.1 + §14.8.8.1 step 3 carrier-consumer
signature divergence per `.harness/class_1_fork_webhook_composer_per_workflow_context_threading.md`
Reading (H) operator-ratified 2026-05-28.

Background: the spec contract at §14.10.1 + §14.8.8.1 step 3 declares the
canonical 2-arg `deliver_webhook(brief, idempotency_key)` surface against
`HITLEscalationBrief` (CP-axis); production carrier at
`webhook_delivery_composer.py:166-171` declares a 3-arg surface against
`WebhookConfig` + `WebhookPayload`. The two abstractions are not signature-
compatible — Reading (H) ratifies BOTH surfaces side-by-side and introduces
this projector as the boundary adapter.

Public API:
    `project_brief_to_payload(brief, idempotency_key) -> WebhookPayload`

The projection is a faithful field-mapping (no field invented):
- `approval_id` ← `brief.parent_action_id` (semantic match per
  C-CP-21 §21.8 "approval-id" surface; the parent action ID names the
  workflow step seeking approval).
- `idempotency_key` ← per-call `idempotency_key` param (canonical pass-through;
  the brief itself does not carry an idempotency_key field, by design — it
  is composed by the caller per `compose_hitl_action_id(parent_action_id,
  placement_position)` shape at `hitl_gate_composer.py:994-996`).
- `gate_evaluation_ref` ← `brief.parent_action_id` (the ledger join key per
  C-CP-21 §21.8; the gate's parent action ID is also its evaluation reference
  — the action ID composes from the same ledger-entry hash chain).
- `payload_body` ← serialized brief fields (`escalation_reason`, `fail_class`,
  `fail_detail_hash`, `proposed_response_palette`, `parent_step_id`) — opaque
  per §21.8 deferred clause; provides the operator-facing context for the
  HITL approval decision.

Ratified 2026-05-28 per fork doc §0.1 Reading (H) + §0.2 recommendation.
"""

from __future__ import annotations

from typing import Any

from harness_core import EntryID
from harness_cp.hitl_timeout_degradation import WebhookPayload
from harness_cp.validator_framework_types import HITLEscalationBrief
from harness_is.state_ledger_entry_schema import Identifier


def project_brief_to_payload(
    brief: HITLEscalationBrief,
    idempotency_key: str,
) -> WebhookPayload:
    """Project a `HITLEscalationBrief` to a `WebhookPayload` for outbound HTTP
    delivery via `WebhookDeliveryComposer.deliver_webhook(...)` (the raw 3-arg
    surface).

    Per fork doc §0.1 Reading (H): this adapter mediates between the
    spec-canonical brief surface (CP-axis validator-escalation context) and
    the production-canonical webhook payload surface (CP-axis HTTP-wire
    format).

    Parameters
    ----------
    brief
        The HITL escalation brief from validator framework escalation
        (CP spec v1.18 §25.2 `HITLEscalationBrief`).
    idempotency_key
        The per-call idempotency key composed by the caller per
        `compose_hitl_action_id(parent_action_id, placement_position)` shape.

    Returns
    -------
    WebhookPayload
        The projected payload with `approval_id` / `idempotency_key` /
        `gate_evaluation_ref` / `payload_body` fields populated per the
        field-mapping enumerated at the module docstring.
    """
    payload_body: dict[str, Any] = {
        "escalation_reason": brief.escalation_reason,
        "parent_step_id": brief.parent_step_id,
        "fail_class": brief.fail_class.value if brief.fail_class is not None else None,
        "fail_detail_hash": brief.fail_detail_hash,
        "proposed_response_palette": sorted(
            response.value for response in brief.proposed_response_palette
        ),
    }
    return WebhookPayload(
        approval_id=brief.parent_action_id,
        idempotency_key=Identifier(idempotency_key),
        gate_evaluation_ref=EntryID(brief.parent_action_id),
        payload_body=payload_body,
    )
