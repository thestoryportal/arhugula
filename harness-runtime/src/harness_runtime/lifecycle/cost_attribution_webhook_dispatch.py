"""U-OD-40 production wiring — cost-attribution helper for webhook delivery.

Per OD spec v1.8 §C-OD-26.2 billable-span enumeration row "hitl.webhook.deliver"
+ §C-OD-28.1 RATE_TABLE_V1 substrate: every WebhookDeliveryComposer
.deliver_webhook() invocation must invoke the cost-attribution chain
post-deliver and write one audit-ledger entry.

Cost formula (per RATE_TABLE_V1.webhook_rate `WebhookRate`):

    cost = flat_per_attempt + (egress_rate_per_byte × bytes_sent if plus_egress)

All arithmetic in `Decimal` per §C-OD-28.4 invariant 2; coercion to float
happens only at the `SpanCostRecord.total_cost` boundary.

Per U-OD-40 AC #2 + AC #3 (cost-record attached at span exit): the helper
is invoked inline at `WebhookDeliveryComposer.deliver_webhook` post-attempt
(both success + failure paths — every attempt is billable per
flat_per_attempt semantics; failures still incurred network egress).

Home rationale: identical to U-OD-39 tool-dispatch precedent. WebhookDelivery
Composer lives at harness-runtime so inline-wrap is the simpler pattern (vs
hook Protocol for cross-axis validator case which lives at harness-cp). The
inline-wrap mirrors the RuntimeToolDispatcher._attribute_tool_cost_best_effort
pattern at `runtime_tool_dispatcher.py:285` — caller-side wrapping with
exception-swallowing keeps cost-attribution as observability-only (MUST NOT
fail dispatch).

Per AC #4 (audit-ledger entry written): same 5-substep chain as tool +
validator dispatch precedents — compute cost → attach idempotency-key →
project to CostRecordAuditPayload → convert via cp_audit_to_od_audit →
append to audit ledger.

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-26.1 + §C-OD-26.2 + §C-OD-28.1
- `Spec_Operational_Discipline_v1_10.md` §C-OD-26.6.1 (CostRecordAuditPayload)
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-40 (5 ACs)
- `Cross_Axis_Composition_Document_v2_9.md` §2.3.7 row 8 (cost-attribution
  audit-write seam)
- `harness-od/src/harness_od/rate_table_v1.py` (live `RATE_TABLE_V1` with
  `webhook_rate` substrate)
- Companion: `cost_attribution_tool_dispatch.py` +
  `cost_attribution_validator_dispatch.py` (structural precedents)
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from harness_cp.engine_namespace import ReplayDisposition
from harness_cxa.cp_audit_conversion import cp_audit_to_od_audit
from harness_od.audit_ledger_types import AuditLedgerEntry
from harness_od.cost_record_audit_writer import (
    _project_cost_record_to_audit_payload,
)
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
from harness_od.rate_table_types import RateTable

from harness_runtime.types import AuditLedgerWriter, CostAttributionChain

_DEFAULT_SIGNING_KEY_ID = "harness-cost-attribution-v1"
_DEFAULT_REPLAY_DISPOSITION = ReplayDisposition.NO_REPLAY

#: Dispatch kind for webhook-dispatch cost records (C-OD-15 §15.1.1) — the typed
#: key for `RollupAxis.PER_DISPATCH_KIND`. The cross-family `provider_discriminator`
#: tag (§15.3) is `None` at this per-dispatch site.
_WEBHOOK_DISPATCH_KIND = DispatchKind.WEBHOOK


def _compute_webhook_cost(rate_table: RateTable, bytes_sent: int) -> Decimal:
    """Compute webhook-delivery cost per WebhookRate formula.

    Per U-OD-40 AC #2:

        cost = webhook_rate.flat_per_attempt
             + (egress_rate_per_byte × bytes_sent if webhook_rate.plus_egress)

    All arithmetic in `Decimal` per §C-OD-28.4 invariant 2.

    When `webhook_rate.plus_egress is False`, `bytes_sent` is ignored and
    cost reduces to the flat-per-attempt baseline (still billed per attempt).
    """
    cost = rate_table.webhook_rate.flat_per_attempt
    if rate_table.webhook_rate.plus_egress:
        cost = cost + rate_table.egress_rate_per_byte * Decimal(bytes_sent)
    return cost


def attribute_webhook_dispatch_cost(
    *,
    rate_table: RateTable,
    cost_chain: CostAttributionChain,
    audit_writer: AuditLedgerWriter,
    webhook_target: str,
    bytes_sent: int,
    span_id: str,
    idempotency_key: str,
    parent_idempotency_key: str,
    workflow_id: str,
    parent_action_id: str,
    tenant_id: str | None = None,
) -> SpanCostRecord:
    """Run the §C-OD-26.1 canonical cost-attribution chain for one webhook delivery.

    Computes webhook cost per WebhookRate formula (flat_per_attempt +
    optional egress) → attaches idempotency-key → projects to
    CostRecordAuditPayload → converts via cp_audit_to_od_audit → appends
    to audit ledger. Returns the idempotency-key-bearing SpanCostRecord
    for caller-side OTel attribute emission per U-OD-49.

    Mirrors `attribute_tool_dispatch_cost` + `attribute_validator_dispatch_cost`
    exactly except:
      - Cost compute: `_compute_webhook_cost` (flat + optional egress)
        instead of tool cost_kind formulas / validator CPU-meter.
      - dispatch_kind: `DispatchKind.WEBHOOK` (vs `TOOL`/`VALIDATOR`);
        provider_discriminator: `None` (no chain-level family tag per-dispatch).
      - gen_ai_provider_name: `f"webhook:{webhook_target}"` (repurposed
        carrier-namespace; SpanCostRecord schema permits non-LLM tags by
        C-OD-05 §5.1 row-15 family taxonomy).
      - gen_ai_request_model: empty string `""` (no model concept).

    Parameters
    ----------
    rate_table
        The resolved PRICE_TABLE_REF for this workflow's execution.
    cost_chain
        Concrete cost-attribution chain (consumed for `attach_idempotency_key`
        only; webhook compute is direct-Decimal).
    audit_writer
        Audit-ledger writer.
    webhook_target
        Webhook destination identifier — typically the webhook URL or
        operator-named target. Repurposed into
        SpanCostRecord.gen_ai_provider_name as `f"webhook:{webhook_target}"`.
    bytes_sent
        Number of bytes egressed in the webhook payload (request body).
        Ignored if `webhook_rate.plus_egress is False`.
    span_id
        Current hitl.webhook.deliver span's OTel span_id (hex form).
    idempotency_key
        Webhook-dispatch idempotency key — composed at the caller
        (typically `f"webhook:{workflow_id}:{attempt_index}"`).
    parent_idempotency_key
        Parent HITL gate's idempotency_key.
    workflow_id
        Parent workflow identifier.
    parent_action_id
        Parent action_id from the HITL gate envelope.
    tenant_id
        Tenant scope for audit-ledger append (None → single-tenant).

    Returns
    -------
    SpanCostRecord
        The idempotency-key-bearing cost record for caller-side OTel
        attribute emission.
    """
    # Substep 1 + 2 — compute webhook cost per WebhookRate formula. No
    # rate resolution needed; webhook_rate is singleton at RateTable
    # (operator-overridable but not per-target).
    cost_decimal = _compute_webhook_cost(rate_table, bytes_sent)

    # Substep 3 — build SpanCostRecord; attach idempotency key joining to
    # the IS state-ledger parent entry per C-IS-05 / C-OD-14 §14.4.
    cost_record = SpanCostRecord(
        span_id=span_id,
        idempotency_key="",  # populated at attach_idempotency_key
        total_cost=float(cost_decimal),
        total_latency_ms=0,  # webhook latency observability deferred (delivery
        # time depends on remote endpoint; not material at the cost record).
        derived_keys=(),
        engine_replay_disposition=_DEFAULT_REPLAY_DISPOSITION,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=None,
        dispatch_kind=_WEBHOOK_DISPATCH_KIND,
        gen_ai_provider_name=f"webhook:{webhook_target}",
        gen_ai_request_model="",
    )
    # The chain Protocol returns `object` for OD-typed values per its
    # documented "consumers narrow at concrete call sites" contract
    # (types.py CostAttributionChain). The concrete return is a SpanCostRecord.
    attached = cast(
        SpanCostRecord,
        cost_chain.attach_idempotency_key(span_id, parent_idempotency_key, cost_record),
    )

    # Substep 4 + 5 — project to typed CostRecordAuditPayload via the
    # canonical helper; convert via cp_audit_to_od_audit `cost:` action_id
    # prefix branch; append to audit ledger.
    cost_payload = _project_cost_record_to_audit_payload(
        attached,
        workflow_id=workflow_id,
        parent_action_id=parent_action_id,
    )
    audit_entry: AuditLedgerEntry = cp_audit_to_od_audit(
        cost_payload,
        key_id=_DEFAULT_SIGNING_KEY_ID,
    )
    audit_writer.append(tenant_id, audit_entry)

    return attached  # type: ignore[no-any-return]


__all__ = [
    "attribute_webhook_dispatch_cost",
]
