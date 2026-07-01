"""C-OD-26 cost-attribution audit-payload carrier — Sub-arc B landing.

U-OD-41 — Sub-arc B landing of `[[fork-u-cp-72-cost-and-pause-resume-prefix-gap]]`
3-arc cascade per fork §2.2 routing target. Declares the
`CostRecordAuditPayload` field-set consumed by the `cp_audit_to_od_audit`
converter at `harness-cxa/src/harness_cxa/cp_audit_conversion.py` when the
converter encounters a `cost:`-prefixed CP action_id (per CXA v2.9 §2.3.7
row 8 + U-CP-72 cost: branch restoration at the same impl arc).

**Pattern alignment** with PauseResumeAuditPayload (§C-OD-30.2 at
`pause_resume_namespace.py`) + ValidatorEscalationAuditPayload
(§C-OD-29.2) + WebhookDeliveryAuditPayload (§C-OD-32.2) +
OperatorBurdenAuditPayload (§C-OD-33.2) + TrustEvaluationAuditPayload
(§C-OD-31.2). The 4 `audit_cp_*` common fields are the shared CP-sourced
field-set per C-OD-24.6 `audit.cp.*` sub-namespace discipline.

**Sub-namespace tagging.** `audit.cost.*` for cost-specific fields per
CXA v2.9 §0.4 attribution convention (cost-attribution is OD-axis-owned
namespace per harness-od/CLAUDE.md cost-attribution chain ownership
C-OD-12 + C-OD-13). The `cp_audit_to_od_audit` converter projects the 5
cost-specific fields into `AuditPayload.audit_namespace_attrs` as
`audit.cost.*` sub-namespace keys via `_project_producer_namespace_attrs`
with `COST_AUDIT_NAMESPACE_PREFIX = "audit.cost"` constant.

**Helper-composed only** per OD spec v1.10 §C-OD-26.6.1 step 1. Direct
construction at non-helper call sites is forbidden — the helper at
`harness-od/src/harness_od/cost_record_audit_writer.py`
(`_project_cost_record_to_audit_payload`) is the canonical construction
site.

Authority: OD spec v1.10 §C-OD-26.6 (NEW at v1.10 — Sub-arc B sequel
landing); CXA v2.9 §2.3.7 row 8 (cost-attribution audit-write seam — CP→OD
bucket 8th typed seam); plan unit U-OD-41 (OD plan v2.17, formerly v2.16
cross-axis-blocked on U-CP-72 cost: branch un-STRIKE per
`[[fork-u-cp-72-cost-and-pause-resume-prefix-gap]]` §2.2).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ----------------------------------------------------------------------------
# CostRecordAuditPayload (§C-OD-26.6 audit-ledger projection)
# ----------------------------------------------------------------------------


class CostRecordAuditPayload(BaseModel):
    """Cost-attribution per-span audit payload — Sub-arc B carrier.

    Projected from SpanCostRecord (existing C-OD-14 §14.4 12+3-field carrier
    at `harness-od/src/harness_od/idempotency_join_dedup.py:134`) at every
    billable-span exit per OD spec v1.10 §C-OD-26.1 canonical invocation.
    Consumed by the `cp_audit_to_od_audit` converter via the `cost:`
    action_id prefix branch per CXA v2.9 §2.3.7 row 8. Output is the
    OD-canonical AuditLedgerEntry signed and ready for
    `audit_writer.append`.

    Pattern: parallel to PauseResumeAuditPayload (§C-OD-30.2) +
    ValidatorEscalationAuditPayload (§C-OD-29.2) + the 3 other CP-sourced
    AuditPayload subclasses. The 4 audit_cp_* common fields are the shared
    field-set per C-OD-24.6 `audit.cp.*` sub-namespace discipline.

    Sub-namespace tagging: `audit.cost.*` per CXA v2.9 §0.4 attribution
    convention (cost-attribution is OD-axis-owned namespace per harness-od/
    CLAUDE.md cost-attribution chain ownership; §C-OD-24.6 sub-namespace
    extends OD-canonical audit.* per C-OD-05 §5.1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # CP-sourced inherited per §C-OD-24.6 sub-namespace discipline:
    audit_cp_action_id: str
    """f"cost:{workflow_id}:{step_action_id}" per OD spec v1.10 §C-OD-26.6.1
    step 2 + CXA v2.9 §0.3 row 8 discriminator-table pattern. Discriminator
    at OD audit-trace consumers."""

    audit_cp_response: str
    """`"cost_attributed"` per OD spec v1.8 §C-OD-26.3 prose (preserved
    verbatim at v1.9 + v1.10). Helper-set constant per §C-OD-26.6.1 step 4
    (mirrors sibling PauseResumeAuditPayload `"paused"`/`"resumed"`
    convention)."""

    audit_cp_timestamp: str
    """ISO-8601 UTC timestamp at cost-attribution moment (per-span exit) OR
    `""` MVP sentinel per v1.7 §24.4 NOTE 8a-iii. String-typed per existing
    convention at sibling subclasses (Pydantic v2 serialization at
    audit-ledger row write)."""

    audit_cp_prior_event_hash: str
    """SHA-256 hex (64 chars) prior CP-event hash per the SHA-256 chain per
    C-IS-06 + C-IS-13 §13.5. `"0"*64` sentinel at MVP when prior is absent
    (first cost-record in workflow). String-typed (not `str | None`) per the
    empirical sibling-subclass convention — sentinel zero-hash rather than
    None per pause_resume_namespace.py:226-227 precedent."""

    # Cost-attribution-specific fields per CXA v2.9 §0.3 row 8 prose
    # enumeration (projectable subset of SpanCostRecord):
    span_id: str
    """The span_id from SpanCostRecord (idempotency_join_dedup.py:135).
    Maps to CXA v2.9 row 8 audit-trail join key per C-OD-24.4."""

    idempotency_key: str
    """The parent span's idempotency_key from SpanCostRecord
    (idempotency_join_dedup.py:136 + §14.4 join key). Maps to CXA v2.9 row 8
    idempotency-anchor per C-OD-24.4 invariant."""

    provider: str
    """The gen_ai_provider_name from SpanCostRecord (D-5 v2.8 amendment).
    Maps to CXA v2.9 row 8 `provider` field (C-OD-04 §4.3 base-layer
    attribute)."""

    model_id: str
    """The gen_ai_request_model from SpanCostRecord. Maps to CXA v2.9 row 8
    `model_id` field (C-OD-04 §4.3 base-layer attribute)."""

    usage_total_cost_usd: float
    """The total_cost from SpanCostRecord (U-OD-19 SpanTotalCost.total_cost
    USD). Maps to CXA v2.9 row 8 `usage_total_cost_usd` field. Always
    populated at billable-span exit per OD spec v1.10 §C-OD-26.1 canonical
    invocation."""

    # --- deferred to implementation discretion per §C-OD-26.6.5 ---
    #
    # usage_input_tokens / usage_output_tokens — upstream from SpanCostRecord
    # at SpanTotalCost layer per OD spec v1.7 §14.4; projectable but require
    # upstream attribution wiring at U-OD-41 helper construction. May be
    # added as Optional fields at U-OD-41 impl OR carried via gen_ai.usage.*
    # span attribute pass-through. Implementer-discretion per FM-2.
    #
    # cumulative_cost_usd — downstream rollup field per U-OD-21
    # rollup_costs_by_axis; NOT available at per-span audit-write moment.
    # Deferred indefinitely; carried at separate rollup-event audit shape
    # if needed at future arc.


__all__ = [
    "CostRecordAuditPayload",
]
