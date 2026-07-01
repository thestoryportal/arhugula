"""U-OD-39 production wiring — cost-attribution helper for tool dispatch.

Per OD spec v1.8 §C-OD-26.1 invocation signature + §C-OD-26.2 billable-span
enumeration row "tool.dispatch" (with `mcp.tool.call` piggybacking parent
per §C-OD-26.2 table): every tool dispatch must invoke the cost-attribution
chain post-tool-invocation and write one audit-ledger entry. This helper
packs the §C-OD-26.1 v1.10 canonical 5-substep convention into a single
callable invoked from `RuntimeToolDispatcher.dispatch`.

Per U-OD-39 AC #2 (Phase D iteration-1 F2-04 absorption), the tool-rate
resolution uses `ToolRate.cost_kind` formulas:

  - `flat_per_invocation` — cost = rate (constant per invocation;
    input/output bytes ignored)
  - `per_input_byte` — cost = rate × len(canonical_json(tool_args))
  - `per_output_byte` — cost = rate × len(canonical_json(response))

All arithmetic in `Decimal` per §C-OD-28.4 invariant 2 (no float coercion
at the rate compute layer; conversion to float happens only at
`SpanCostRecord.total_cost` boundary per the carrier's float-typed shape).

Per U-OD-39 AC #3 (mcp.tool.call piggyback per §C-OD-26.2 table): the helper
is invoked ONCE per `RuntimeToolDispatcher.dispatch` call, attributing the
entire tool-dispatch surface (including the nested `mcp.tool.call` span)
to a single SpanCostRecord. No separate cost-attribution invocation at the
mcp.tool.call span site; that span piggybacks per spec.

Per U-OD-39 AC #1: cost-attribution invoked on every tool dispatch (success
+ failure). On failure paths (timeout / protocol_error / schema_violation /
transport), the response is the exception-derived placeholder and
`per_output_byte` cost_kind degrades to `len(canonical_json("")) = 2`
(empty-string JSON `""`). The LLM-dispatch precedent at
`cost_attribution_llm_dispatch.py` shows best-effort invocation; this
helper is structured identically — caller-side wrapping with
exception-swallowing per `llm_dispatch.py:934` precedent is the production
binding-arc concern; the helper itself raises `RateTableMissingError` on
unknown tool_id per §C-OD-28.2 default fail-closed.

Home rationale: identical to LLM-dispatch precedent. This helper composes
OD types (`ToolRate`, `RateTable`, `SpanCostRecord`) + CXA converter +
runtime Protocols (`AuditLedgerWriter`, `CostAttributionChain`). OD's
downstream-consumer invariant (`harness-od/CLAUDE.md` §1.1) prohibits OD
importing runtime; the helper lives at runtime instead.

Per AC #4 (cost-record attached + audit-ledger entry written): same 5-substep
chain as LLM dispatch — resolve rate → compute cost → attach idempotency-key
→ project to `CostRecordAuditPayload` → convert via `cp_audit_to_od_audit`
→ append to audit ledger.

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-26.1 + §C-OD-26.2 + §C-OD-28.1
- `Spec_Operational_Discipline_v1_10.md` §C-OD-26.6.1 (CostRecordAuditPayload)
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-39 (5 ACs)
- `Cross_Axis_Composition_Document_v2_9.md` §2.3.7 row 8 (cost-attribution
  audit-write seam — shared with LLM dispatch)
- `harness-od/src/harness_od/rate_table_v1.py` (live `RATE_TABLE_V1` with
  `tool_rates` substrate)
- Companion: `cost_attribution_llm_dispatch.py` (structural precedent)
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast

from harness_cp.engine_namespace import ReplayDisposition
from harness_cxa.cp_audit_conversion import cp_audit_to_od_audit
from harness_od.audit_ledger_types import AuditLedgerEntry
from harness_od.cost_record_audit_writer import (
    _project_cost_record_to_audit_payload,
)
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
from harness_od.rate_table_types import RateTable, ToolRate

from harness_runtime.types import AuditLedgerWriter, CostAttributionChain

#: Canonical signing key id for tool-dispatch cost-attribution audit entries.
#: Same convention as the LLM-dispatch precedent at
#: `cost_attribution_llm_dispatch.py:_DEFAULT_SIGNING_KEY_ID`. Operator
#: overrides via bootstrap config per ADR-D5 v1.4 §1.4.1 + C-OD-21 §21.2.
_DEFAULT_SIGNING_KEY_ID = "harness-cost-attribution-v1"

#: Default ReplayDisposition for live tool dispatches outside a replay
#: context. Per ADR-D1 v1.2 §1.1.1, NO_REPLAY is the PURE_PATTERN_NO_ENGINE
#: disposition — the dominant case for at-the-edge tool invocations.
_DEFAULT_REPLAY_DISPOSITION = ReplayDisposition.NO_REPLAY

#: Dispatch kind for tool-dispatch cost records (C-OD-15 §15.1.1) — the typed
#: key for `RollupAxis.PER_DISPATCH_KIND`. The cross-family `provider_discriminator`
#: tag (a §15.3 chain-composition concept) is `None` at this per-dispatch site.
_TOOL_DISPATCH_KIND = DispatchKind.TOOL


class ToolRateMissingError(LookupError):
    """Raised when tool_id is not registered in `RateTable.tool_rates`.

    Per §C-OD-28.2 default fail-closed: rate-table resolution failure is a
    contract violation surfaced as `CP-FAIL-RATE-TABLE-MISSING` at the
    invocation site. Caller-side production binding wraps this in
    best-effort exception-swallowing per `llm_dispatch.py:934` precedent.
    """


def _resolve_tool_rate(rate_table: RateTable, tool_id: str) -> ToolRate:
    """Resolve `ToolRate` for `tool_id` per §C-OD-28.4 invariant 4.

    Raises `ToolRateMissingError` on miss per §C-OD-28.2 default fail-closed.
    """
    rate = rate_table.tool_rates.get(tool_id)
    if rate is None:
        raise ToolRateMissingError(
            f"CP-FAIL-RATE-TABLE-MISSING: tool_id={tool_id!r} not in "
            f"RateTable.tool_rates (known tool_ids: "
            f"{sorted(rate_table.tool_rates.keys())})"
        )
    return rate


def _canonical_json_byte_length(payload: Any) -> int:
    """Canonical-JSON serialization byte length per AC #2 cost_kind formulas.

    Per `per_input_byte` / `per_output_byte` AC #2 prose:
    "canonical JSON serialization of the tool's input args / output".
    Canonical here means UTF-8 byte length of `json.dumps(payload,
    sort_keys=True, separators=(",", ":"))` — sort_keys ensures Pydantic-v2
    style canonical ordering; separators produce minimal-whitespace form.
    Both choices match the existing canonical-json discipline at
    `harness-od/src/harness_od/idempotency_join_dedup.py` per F2 join
    composition (consistency with the substrate's canonical-JSON convention
    is the operative tiebreaker).
    """
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _compute_tool_cost(rate: ToolRate, *, tool_args: Any, response: Any) -> Decimal:
    """Compute tool-dispatch cost per `ToolRate.cost_kind` formula.

    Per U-OD-39 AC #2 + §C-OD-28.4 invariant 2 (Decimal-precision-preserving
    at the rate compute layer):

      - `flat_per_invocation` → `cost = rate.rate`
      - `per_input_byte` → `cost = rate.rate × len(canonical_json(tool_args))`
      - `per_output_byte` → `cost = rate.rate × len(canonical_json(response))`

    All arithmetic in `Decimal`. The float coercion at SpanCostRecord
    boundary happens at the caller; this function returns Decimal.
    """
    if rate.cost_kind == "flat_per_invocation":
        return rate.rate
    if rate.cost_kind == "per_input_byte":
        return rate.rate * Decimal(_canonical_json_byte_length(tool_args))
    if rate.cost_kind == "per_output_byte":
        return rate.rate * Decimal(_canonical_json_byte_length(response))
    # Pydantic's Literal validation forecloses unknown values; this branch
    # is unreachable under normal ToolRate construction. Defensive guard.
    raise ValueError(
        f"ToolRate.cost_kind unknown: {rate.cost_kind!r} "
        f"(expected one of flat_per_invocation / per_input_byte / per_output_byte)"
    )


def attribute_tool_dispatch_cost(
    *,
    rate_table: RateTable,
    cost_chain: CostAttributionChain,
    audit_writer: AuditLedgerWriter,
    tool_id: str,
    tool_args: Any,
    response: Any,
    span_id: str,
    idempotency_key: str,
    parent_idempotency_key: str,
    workflow_id: str,
    parent_action_id: str,
    tenant_id: str | None = None,
) -> SpanCostRecord:
    """Run the §C-OD-26.1 canonical cost-attribution chain for one tool dispatch.

    Resolves tool-rate → computes cost per `cost_kind` formula → attaches
    idempotency-key → projects to CostRecordAuditPayload → converts via
    cp_audit_to_od_audit → appends to audit ledger. Returns the
    idempotency-key-bearing SpanCostRecord so the caller can emit the
    cost.attributed_decimal OTel span attribute per U-OD-49.

    Mirrors `attribute_llm_dispatch_cost` exactly except:
      - Rate resolution: `tool_rates[tool_id]` instead of
        `providers[provider].models[model]`.
      - Cost compute: `_compute_tool_cost` (3-branch cost_kind formula)
        instead of `cost_chain.compute_per_attempt_cost(SpanCostInputs,
        PriceRateEntry)` (LLM-specific token-weighted path).
      - dispatch_kind: `DispatchKind.TOOL` instead of `DispatchKind.LLM`;
        provider_discriminator: `None` (no chain-level family tag per-dispatch).
      - gen_ai_provider_name: `f"tool:{tool_id}"` (repurposed carrier field;
        SpanCostRecord schema's `gen_ai_provider_name` is `str`-typed and
        permits non-LLM tags by C-OD-05 §5.1 row-15 family taxonomy).
      - gen_ai_request_model: empty string `""` (no model concept at tool
        dispatch; the field is `str`-typed and non-nullable).

    Parameters
    ----------
    rate_table
        The resolved PRICE_TABLE_REF for this workflow's execution.
    cost_chain
        Concrete cost-attribution chain (consumed for `attach_idempotency_key`
        only; the LLM-specific `compute_per_attempt_cost` path is bypassed
        per `_compute_tool_cost` direct-Decimal compute).
    audit_writer
        Audit-ledger writer.
    tool_id
        Tool identifier from `step.step_payload["tool_id"]` per
        `runtime_tool_dispatcher.py:305-312` extraction.
    tool_args
        Tool input args (any JSON-serializable shape) used for
        `per_input_byte` cost_kind canonical-JSON byte count.
    response
        Tool output response (any JSON-serializable shape) used for
        `per_output_byte` cost_kind canonical-JSON byte count.
    span_id
        Current tool.dispatch span's OTel span_id (hex form).
    idempotency_key
        Tool-dispatch idempotency key composed at
        `runtime_tool_dispatcher.py:401` step 6 via `_compose_idempotency_key`.
    parent_idempotency_key
        Parent step's idempotency_key from `step_context`.
    workflow_id
        Parent workflow identifier from `step_context.workflow_id` for
        canonical action_id pattern.
    parent_action_id
        Parent action_id from `step_context.parent_action_id`.
    tenant_id
        Tenant scope for audit-ledger append (None → single-tenant).

    Returns
    -------
    SpanCostRecord
        The idempotency-key-bearing cost record for caller-side OTel
        attribute emission.

    Raises
    ------
    ToolRateMissingError
        tool_id not in rate_table.tool_rates per §C-OD-28.2 default
        fail-closed.
    """
    # Substep 1 — resolve tool rate per §C-OD-28.4 invariant 4 (per-tool
    # registration is the canonical lookup; no fallback per F2-04 ratification
    # — tool dispatch with unknown tool_id is a contract violation, not a
    # silent zero-cost case).
    tool_rate = _resolve_tool_rate(rate_table, tool_id)

    # Substep 2 + 3 — compute per-invocation cost via the 3-branch cost_kind
    # formula. Substep 2 (rate bridging) is collapsed into substep 3 here
    # because tool rates don't need the Decimal→float ProviderRates→
    # PriceRateEntry bridge of the LLM path; the ToolRate.rate is already
    # Decimal at the carrier per C-OD-28.1.
    cost_decimal = _compute_tool_cost(tool_rate, tool_args=tool_args, response=response)

    # Substep 4 — build SpanCostRecord; attach idempotency key joining to the
    # IS state-ledger parent entry per C-IS-05 / C-OD-14 §14.4.
    # SpanCostRecord schema repurposing for tool dispatch:
    #   - dispatch_kind = DispatchKind.TOOL (the PER_DISPATCH_KIND key)
    #   - provider_discriminator = None (no chain-level family tag per-dispatch)
    #   - gen_ai_provider_name = "tool:<tool_id>" (carrier-namespace repurpose)
    #   - gen_ai_request_model = "" (no model concept; str-typed required field)
    cost_record = SpanCostRecord(
        span_id=span_id,
        idempotency_key="",  # populated at attach_idempotency_key
        total_cost=float(cost_decimal),
        total_latency_ms=0,  # latency observability deferred to follow-on arc
        derived_keys=(),
        engine_replay_disposition=_DEFAULT_REPLAY_DISPOSITION,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=None,
        dispatch_kind=_TOOL_DISPATCH_KIND,
        gen_ai_provider_name=f"tool:{tool_id}",
        gen_ai_request_model="",
    )
    # The chain Protocol returns `object` for OD-typed values per its
    # documented "consumers narrow at concrete call sites" contract
    # (types.py CostAttributionChain). The concrete return is a SpanCostRecord.
    attached = cast(
        SpanCostRecord,
        cost_chain.attach_idempotency_key(span_id, parent_idempotency_key, cost_record),
    )

    # Substep 5 — project to typed CostRecordAuditPayload via the canonical
    # helper; convert via cp_audit_to_od_audit `cost:` action_id prefix
    # branch; append to audit ledger. Same path as LLM-dispatch precedent;
    # the CXA converter routes by action_id prefix not by surface kind.
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
    # emission via U-OD-49 string-form serialization.
    return attached  # type: ignore[no-any-return]


__all__ = [
    "ToolRateMissingError",
    "attribute_tool_dispatch_cost",
]
