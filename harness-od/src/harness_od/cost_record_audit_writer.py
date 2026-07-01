"""U-OD-41 cost-record audit-ledger write composition helper.

Implements OD spec v1.10 §C-OD-26.1 canonical invocation signature (v1.10
amended) + §C-OD-26.3 audit-ledger write per cost-record + §C-OD-26.6
CostRecordAuditPayload typed carrier. Helper-composed CostRecordAuditPayload
construction per §C-OD-26.6.1 step 1 (direct construction at non-helper
sites is forbidden — this module is the canonical construction site).

Pattern: parallel to the U-OD-50/51/52/53/54 sibling helper-composition
sites for ValidatorEscalationAuditPayload + PauseResumeAuditPayload +
TrustEvaluationAuditPayload + WebhookDeliveryAuditPayload +
OperatorBurdenAuditPayload.

The composed CostRecordAuditPayload is routed through the canonical
`cp_audit_to_od_audit` converter at
`harness-cxa/src/harness_cxa/cp_audit_conversion.py` via the `cost:`
action_id prefix branch (restored at this same impl arc per Sub-arc B
sequel landing). Output is the OD-canonical AuditLedgerEntry consumed by
`audit_writer.append`.

Authority: OD spec v1.10 §C-OD-26 (NEW §C-OD-26.6 + §C-OD-26.1 amendment
at v1.10 Sub-arc B sequel); CXA v2.9 §2.3.7 row 8 (cost-attribution
audit-write seam — CP→OD bucket 8th typed seam at v2.9); plan unit
U-OD-41 (OD plan v2.17, formerly v2.16 cross-axis-blocked on U-CP-72 cost:
branch un-STRIKE per `[[fork-u-cp-72-cost-and-pause-resume-prefix-gap]]`
§2.2).
"""

from __future__ import annotations

from harness_od.cost_namespace import CostRecordAuditPayload
from harness_od.idempotency_join_dedup import SpanCostRecord

#: Sentinel zero-hash per sibling-subclass convention
#: (pause_resume_namespace.py:226-227). Used as `audit_cp_prior_event_hash`
#: value when prior CP event is absent (first cost-record in workflow).
_ZERO_HASH_SENTINEL = "0" * 64

#: Constant `audit_cp_response` value per OD spec v1.8 §C-OD-26.3 prose
#: (preserved verbatim through v1.10). Helper-set per §C-OD-26.6.1 step 4
#: (mirrors sibling-subclass response-value convention).
_RESPONSE_VALUE = "cost_attributed"


def _project_cost_record_to_audit_payload(
    attached: SpanCostRecord,
    *,
    workflow_id: str,
    parent_action_id: str,
    prior_event_hash: str = _ZERO_HASH_SENTINEL,
    timestamp: str = "",
) -> CostRecordAuditPayload:
    """Project a SpanCostRecord to CostRecordAuditPayload per §C-OD-26.6.1.

    Helper-composition site for CostRecordAuditPayload per OD spec v1.10
    §C-OD-26.6.1 step 1 (direct construction at non-helper sites is
    forbidden). Invoked at the billable-span exit per §C-OD-26.1 v1.10
    canonical invocation pattern; output is routed through
    `cp_audit_to_od_audit` via the `cost:` action_id prefix branch.

    Parameters
    ----------
    attached:
        The SpanCostRecord with idempotency_key attached (post-attach_idempotency_key
        chain per §C-OD-26.1 step 2). Provides the 5 cost-specific fields
        (span_id, idempotency_key, provider, model_id, usage_total_cost_usd).
    workflow_id:
        The parent workflow identifier (from step_context.workflow_id). Used
        to construct the `audit_cp_action_id` per §C-OD-26.6.1 step 2
        pattern `cost:<workflow_id>:<step_action_id>`.
    parent_action_id:
        The billable span's parent step action_id (the LLM dispatch / tool
        dispatch / etc. that caused the cost attribution). Used to construct
        the `audit_cp_action_id` per §C-OD-26.6.1 step 2 pattern.
    prior_event_hash:
        SHA-256 hex (64 chars) prior CP-event hash. Defaults to `"0"*64`
        sentinel per §C-OD-26.6.1 step 3 (sibling-subclass convention).
    timestamp:
        ISO-8601 UTC timestamp at cost-attribution moment OR `""` MVP
        sentinel per §24.4 NOTE 8a-iii. Defaults to `""`.

    Returns
    -------
    CostRecordAuditPayload
        The projected payload ready for `cp_audit_to_od_audit` conversion
        via the `cost:` action_id prefix branch.
    """
    return CostRecordAuditPayload(
        # 4 audit_cp_* common fields per §C-OD-26.6:
        audit_cp_action_id=f"cost:{workflow_id}:{parent_action_id}",
        audit_cp_response=_RESPONSE_VALUE,
        audit_cp_timestamp=timestamp,
        audit_cp_prior_event_hash=prior_event_hash,
        # 5 cost-specific fields projected from SpanCostRecord:
        span_id=attached.span_id,
        idempotency_key=attached.idempotency_key,
        provider=attached.gen_ai_provider_name,
        model_id=attached.gen_ai_request_model,
        usage_total_cost_usd=attached.total_cost,
    )


__all__ = [
    "_project_cost_record_to_audit_payload",
]
