"""U-OD-38 + U-OD-41 sub-arc B production wiring — cost-attribution helper for LLM dispatch.

Per OD spec v1.10 §C-OD-26.1 (amended) + §26.2 row "llm_dispatch": every
LLM dispatch must invoke the cost-attribution chain post-provider-call and
write one audit-ledger entry. This helper packs the §C-OD-26.1 v1.10
canonical 5-substep convention into a single callable invoked from
`RuntimeLLMDispatcher.dispatch`:

  1. Resolve per-(provider, model) rates from the RateTable
     (`harness_od.rate_table_resolver.resolve_for`).
  2. Bridge Decimal ProviderRates → float PriceRateEntry
     (`harness_od.rate_table_bridge.provider_rates_to_price_rate_entry`).
  3. Compute per-attempt cost via the cost chain
     (`CostAttributionChain.compute_per_attempt_cost`).
  4. Build a SpanCostRecord; attach idempotency_key joining to the IS state
     ledger parent entry (`CostAttributionChain.attach_idempotency_key`).
  5. Project the cost-record to a `CostRecordAuditPayload` typed carrier via
     the canonical helper `_project_cost_record_to_audit_payload` at
     `harness-od/src/harness_od/cost_record_audit_writer.py` per OD spec
     v1.10 §C-OD-26.6.1; pass through the `cp_audit_to_od_audit` converter
     via the `cost:` action_id prefix branch to obtain a signed
     AuditLedgerEntry; append via `audit_writer.append`.

Home rationale: this helper composes OD types (SpanCostInputs / PriceRateEntry /
SpanCostRecord) + CXA converter + runtime Protocols. OD's downstream-consumer
invariant (`harness-od/CLAUDE.md` §1.1 — 0 outbound cross-axis edges)
prohibits OD importing runtime; the helper lives at runtime instead.

Per AC #5 (U-OD-38): "1 LLM call → 1 cost-record + 1 audit-ledger entry".
The helper returns the cost-record for caller-side telemetry attribute
emission via U-OD-49 string-form serialization at the OTel boundary.

Per AC #1 + AC #4 (U-OD-38): cost-attribution invoked on every LLM dispatch
(success + failure paths). PRICE_TABLE_REF resolution failure raises
`RateTableMissingError` (CP-FAIL-RATE-TABLE-MISSING) per §C-OD-28.2 default
fail-closed.

Per U-OD-41 AC #8 (Sub-arc B production wiring, 2026-05-24 follow-on arc):
the production callsite was migrated from the legacy CPAuditLedgerEntry path
(`cost:{span_id}` action_id pattern preserved at OD spec v1.8 §C-OD-26.3) to
the v1.10 typed CostRecordAuditPayload path with the canonical
`cost:<workflow_id>:<step_action_id>` action_id pattern per OD spec v1.10
§C-OD-26.6.1 step 2. The migration consumes `step_context.workflow_id`
(NEW field at CP spec v1.12 §25.2.1 per
`.harness/class_1_fork_step_execution_context_workflow_id_field_absence.md`
Path A ratification) + `step_context.parent_action_id` from the runtime
context plumbed by the workflow driver.

Authority:
- `Spec_Operational_Discipline_v1_10.md` §C-OD-26.1 (amended) + §C-OD-26.6
- `Spec_Control_Plane_v1_12.md` §25.2.1 (NEW workflow_id field)
- `Implementation_Plan_Operational_Discipline_v2_17.md` U-OD-41 (Sub-arc B)
- `Implementation_Plan_Control_Plane_v2_18.md` U-CP-56 (StepExecutionContext)
- `Cross_Axis_Composition_Document_v2_9.md` §2.3.7 row 8 (cost-attribution
  audit-write seam)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from harness_cp.engine_namespace import ReplayDisposition
from harness_cxa.cp_audit_conversion import cp_audit_to_od_audit
from harness_od.audit_ledger_types import AuditLedgerEntry
from harness_od.cost_formula import PriceRateKey, SpanCostInputs
from harness_od.cost_record_audit_writer import (
    _project_cost_record_to_audit_payload,
)
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
from harness_od.rate_table_bridge import provider_rates_to_price_rate_entry
from harness_od.rate_table_resolver import resolve_for
from harness_od.rate_table_types import RateTable

from harness_runtime.types import AuditLedgerWriter, CostAttributionChain

#: Canonical signing key id used for cost-attribution audit entries at v1.
#: Operator overrides via bootstrap config in production per ADR-D5 v1.4
#: §1.4.1 + C-OD-21 §21.2. Same convention as the U-RT-59 Fork 2 sub-agent
#: dispatch audit-write path at `sub_agent_dispatch.py`.
_DEFAULT_SIGNING_KEY_ID = "harness-cost-attribution-v1"

#: Default ReplayDisposition for live LLM dispatches outside a replay
#: context. Per ADR-D1 v1.2 §1.1.1, NO_REPLAY is the PURE_PATTERN_NO_ENGINE
#: disposition — the dominant case for at-the-edge LLM calls.
_DEFAULT_REPLAY_DISPOSITION = ReplayDisposition.NO_REPLAY

#: Dispatch kind for LLM-dispatch cost records (C-OD-15 §15.1.1) — the typed
#: key for `RollupAxis.PER_DISPATCH_KIND`. The cross-family `provider_discriminator`
#: tag (a §15.3 chain-composition concept) defaults to `None` at the bare-edge
#: call; the runtime fallback-chain cost path supplies it per the dispatched
#: provider's family (R-FS-1 `B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION`).
_LLM_DISPATCH_KIND = DispatchKind.LLM


def attribute_llm_dispatch_cost(
    *,
    rate_table: RateTable,
    cost_chain: CostAttributionChain,
    audit_writer: AuditLedgerWriter,
    provider_name: str,
    model: str,
    span_id: str,
    parent_idempotency_key: str,
    workflow_id: str,
    parent_action_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    tokenizer_version: str | None = None,
    tenant_id: str | None = None,
    provider_discriminator: str | None = None,
) -> SpanCostRecord:
    """Run the §C-OD-26.1 v1.10 canonical cost-attribution chain for one LLM dispatch.

    Resolves rates → computes cost → attaches idempotency key → projects to
    CostRecordAuditPayload via the canonical helper → converts via
    cp_audit_to_od_audit → appends to audit ledger. Returns the
    idempotency-key-bearing SpanCostRecord so the caller can emit the
    cost.attributed_decimal OTel span attribute per U-OD-49.

    Parameters
    ----------
    rate_table
        The resolved PRICE_TABLE_REF for this workflow's execution (immutable
        post-resolution per §C-OD-28.4 invariant 1).
    cost_chain
        Concrete cost-attribution chain (`RuntimeCostAttributionChain` at v1).
    audit_writer
        Audit-ledger writer (`RuntimeAuditLedgerWriter` at v1).
    provider_name
        LLM provider name from `binding.model_binding.provider` per C-CP-01.
    model
        LLM model id from `binding.model_binding.model`.
    span_id
        The current dispatch span's OTel span_id (hex form) — preserved at
        the SpanCostRecord and OTel boundary.
    parent_idempotency_key
        Parent's `idempotency_key` per C-IS-05 (the F2 state-ledger join key).
        Sourced from `step_context.parent_idempotency_key`.
    workflow_id
        Parent workflow identifier sourced from `step_context.workflow_id`
        (NEW at CP spec v1.12 §25.2.1). Composes the canonical
        `cost:<workflow_id>:<step_action_id>` audit action_id pattern per
        OD spec v1.10 §C-OD-26.6.1 step 2.
    parent_action_id
        Parent step action_id sourced from `step_context.parent_action_id`.
        Used as `<step_action_id>` in the canonical action_id pattern.
    input_tokens
        Per-attempt input token count from `gen_ai.usage.input_tokens`.
    output_tokens
        Per-attempt output token count from `gen_ai.usage.output_tokens`.
    cache_creation
        Anthropic-specific cache-creation input tokens (defaults to 0).
    cache_read
        Anthropic-specific cache-read input tokens (defaults to 0).
    tokenizer_version
        anthropic.tokenizer_version attribute; defaults to `"v0"` when absent
        (openai / ollama don't carry this attribute at v1).
    tenant_id
        Tenant scope for audit-ledger append (None → single-tenant).
    provider_discriminator
        The cross-family fallback-chain family tag (C-OD-15 §15.1 / §15.3 —
        a `CrossFamilyTag` value string, e.g. `"frontier_managed_alt"`) for the
        dispatched provider's family. `None` at the bare-edge call (a record
        with no chain-level family context per §15.1.2); the runtime
        fallback-chain cost path (`_attribute_cost_best_effort`) supplies it via
        `cross_family_tag_for_provider`, making `RollupAxis.PER_PROVIDER_DISCRIMINATOR`
        non-vacuous in production (R-FS-1 `B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION`).
        Carried `str`-typed (not `CrossFamilyTag`) to preserve the U-OD-20 no-cycle
        property; validated against `CrossFamilyTag` at the rollup.

    Returns
    -------
    SpanCostRecord
        The idempotency-key-bearing cost record — caller emits the
        cost.attributed_decimal OTel attribute via
        `cost_record_otel_serializer.serialize_decimal_for_otel`.

    Raises
    ------
    RateTableMissingError
        Provider not in rate table — CP-FAIL-RATE-TABLE-MISSING per
        §C-OD-28.2 default fail-closed.
    """
    # Substep 1 — resolve rates per (provider, model). Per §C-OD-28.4 inv 4,
    # per-model overrides resolve before falling back to provider-level.
    rates = resolve_for(rate_table, provider=provider_name, model=model)

    # Substep 2 — bridge Decimal ProviderRates → float PriceRateEntry.
    # Class 3 drift: cost_formula.py uses float arithmetic; full-Decimal
    # chain migration deferred (precision loss bounded at ~15 sig digits).
    rate_key = PriceRateKey(
        provider_name=provider_name,
        model=model,
        tokenizer_version=tokenizer_version or "v0",
    )
    rate_entry = provider_rates_to_price_rate_entry(rates, rate_key)

    # Substep 3 — compute per-attempt cost via the chain.
    cost_inputs = SpanCostInputs(
        input_tokens=input_tokens,
        cache_creation=cache_creation,
        cache_read=cache_read,
        output_tokens=output_tokens,
        rate_key=rate_key,
    )
    span_cost = cost_chain.compute_per_attempt_cost(cost_inputs, rate_entry)

    # Substep 4 — build SpanCostRecord; attach idempotency key joining to
    # the IS state-ledger parent entry per C-IS-05 / C-OD-14 §14.4.
    # `SpanRef` (`harness_od.otel_genai_base.SpanRef`) is a TypeAlias for
    # the live OTel-SDK span handle but is unused in attach_idempotency_key
    # body beyond correlation threading (per its docstring). We pass the
    # span_id string itself as the correlation marker; the actual join key
    # is parent_idempotency_key.
    cost_record = SpanCostRecord(
        span_id=span_id,
        idempotency_key="",  # populated at attach_idempotency_key
        total_cost=float(span_cost),
        total_latency_ms=0,  # latency observability deferred to follow-on arc
        derived_keys=(),
        engine_replay_disposition=_DEFAULT_REPLAY_DISPOSITION,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=provider_discriminator,
        dispatch_kind=_LLM_DISPATCH_KIND,
        gen_ai_provider_name=provider_name,
        gen_ai_request_model=model,
    )
    # The chain Protocol returns `object` for OD-typed values per its
    # documented "consumers narrow at concrete call sites" contract
    # (types.py CostAttributionChain). The concrete return is a SpanCostRecord.
    attached = cast(
        SpanCostRecord,
        cost_chain.attach_idempotency_key(span_id, parent_idempotency_key, cost_record),
    )

    # Substep 5 — project to typed CostRecordAuditPayload via the canonical
    # helper; convert via the cp_audit_to_od_audit `cost:` action_id prefix
    # branch; append to audit ledger. Per OD spec v1.10 §C-OD-26.6.1:
    # action_id pattern is `cost:<workflow_id>:<step_action_id>` (canonical
    # at v1.10; the v1.8 §C-OD-26.3 `cost:{span_id}` prose preserved verbatim
    # at v1.10 but the more-specific pattern is operationalized at the
    # helper construction). audit_cp_response="cost_attributed" hard-coded
    # at the helper per §C-OD-26.6.1 step 4. prior_event_hash defaults to
    # the helper's sentinel `"0"*64` per sibling-subclass convention.
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

    # Return the attached SpanCostRecord for caller-side OTel attribute
    # emission via U-OD-49 string-form serialization at the OTel boundary.
    return attached  # type: ignore[no-any-return]


__all__ = [
    "attribute_llm_dispatch_cost",
]


# Pleaser for unused import warnings — Mapping is used at type-annotation
# layer for future extension to per-tenant rate-overrides.
_ = Mapping
