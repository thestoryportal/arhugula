"""U-OD-40 production wiring — cost-attribution helper for validator dispatch.

Per OD spec v1.8 §C-OD-26.2 billable-span enumeration row "validator.evaluate"
+ Decision 2.D5 RATIFIED (CPU-meter default): every ConcreteValidatorFramework
.evaluate() invocation must invoke the cost-attribution chain post-evaluate
and write one audit-ledger entry. This helper packs the §C-OD-26.1 v1.10
canonical chain into a single callable invoked from the
`ValidatorPostEvaluateHook` Protocol implementation (CP spec v1.24 §28.10).

Cost formula (Decision 2.D5):

    cost = cpu_rate_per_ms × execution_time_ms

All arithmetic in `Decimal` per §C-OD-28.4 invariant 2; coercion to float
happens only at the `SpanCostRecord.total_cost` boundary.

Per U-OD-40 AC #1: cost-attribution invoked on every validator.evaluate
(every outcome — PASS / REVALIDATE / ESCALATE / PERMANENT_FAIL /
OPERATOR_BURDEN_EXCEEDED). The CP spec v1.24 §28.10.4 invariant 1 ensures
the hook fires EXACTLY ONCE per evaluate() invocation (after
ValidatorEvaluation construction; before return). The §28.10.4 invariant 2
best-effort discipline at the hook firing site swallows any exception
this helper might raise (e.g., ZERO `cpu_rate_per_ms` rate; rate-table
substrate misconfiguration); production binding-arc concern.

Home rationale: identical to U-OD-39 tool-dispatch precedent. This helper
composes OD types (`RateTable`, `SpanCostRecord`) + CXA converter + runtime
Protocols (`AuditLedgerWriter`, `CostAttributionChain`). OD's
downstream-consumer invariant (`harness-od/CLAUDE.md` §1.1) prohibits OD
importing runtime; the helper lives at runtime instead. The harness-cp
hook surface (Protocol declaration only) is independent of OD-axis
vocabulary per X-AL-3 spec extension at
`.harness/class_1_fork_u_od_40_validator_post_evaluate_hook.md`.

Per AC #3 + AC #4 (cost-record attached + audit-ledger entry written):
same 5-substep chain as tool dispatch precedent — resolve rate → compute
cost → attach idempotency-key → project to CostRecordAuditPayload →
convert via cp_audit_to_od_audit → append to audit ledger.

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-26.1 + §C-OD-26.2 + §C-OD-28.1
- `Spec_Operational_Discipline_v1_10.md` §C-OD-26.6.1 (CostRecordAuditPayload)
- `Spec_Control_Plane_v1_24.md` §28.10 (ValidatorPostEvaluateHook Protocol)
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-40 (5 ACs)
- `Implementation_Plan_Control_Plane_v2_27.md` U-CP-73 (Protocol authoring)
- `Cross_Axis_Composition_Document_v2_9.md` §2.3.7 row 8 (cost-attribution
  audit-write seam — shared with LLM dispatch + tool dispatch)
- `harness-od/src/harness_od/rate_table_v1.py` (live `RATE_TABLE_V1` with
  `cpu_rate_per_ms` substrate)
- Companion: `cost_attribution_tool_dispatch.py` (structural precedent)
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, cast

from harness_cp.engine_namespace import ReplayDisposition
from harness_cxa.cp_audit_conversion import cp_audit_to_od_audit
from harness_od.audit_ledger_types import AuditLedgerEntry
from harness_od.cost_record_audit_writer import (
    _project_cost_record_to_audit_payload,
)
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
from harness_od.rate_table_types import RateTable

from harness_runtime.lifecycle.cost_record_sink import SupportsCostRecordAppend
from harness_runtime.types import AuditLedgerWriter, CostAttributionChain

if TYPE_CHECKING:
    from harness_cp.validator_framework_types import ValidatorEvaluation
    from harness_cp.workflow_driver_types import (
        StepExecutionContext,
        WorkflowStep,
    )

#: Canonical signing key id for validator-dispatch cost-attribution audit
#: entries. Same convention as the LLM-dispatch + tool-dispatch precedents.
_DEFAULT_SIGNING_KEY_ID = "harness-cost-attribution-v1"

#: Default ReplayDisposition for live validator dispatches outside a replay
#: context (PURE_PATTERN_NO_ENGINE engine class default per ADR-D1 v1.2 §1.1.1).
_DEFAULT_REPLAY_DISPOSITION = ReplayDisposition.NO_REPLAY

#: Dispatch kind for validator-dispatch cost records (C-OD-15 §15.1.1) — the
#: typed key for `RollupAxis.PER_DISPATCH_KIND`. The cross-family
#: `provider_discriminator` tag (§15.3) is `None` at this per-dispatch site.
_VALIDATOR_DISPATCH_KIND = DispatchKind.VALIDATOR


def _compute_validator_cost(rate_table: RateTable, execution_time_ms: float) -> Decimal:
    """Compute validator-dispatch cost per Decision 2.D5 CPU-meter formula.

    Per OD spec v1.8 §C-OD-28.1 RATE_TABLE_V1 substrate + U-OD-40 AC #1:

        cost = rate_table.cpu_rate_per_ms × Decimal(execution_time_ms)

    Returns Decimal-precision cost; float coercion at SpanCostRecord
    boundary at the caller per §C-OD-28.4 invariant 2.

    Note: `execution_time_ms` may be a float with fractional milliseconds
    (sub-millisecond `time.monotonic_ns()` precision per CP spec v1.24
    §28.10.4 invariant 5). Decimal coercion preserves the fractional
    component via `Decimal(str(execution_time_ms))` to avoid float
    binary-representation rounding artifacts.
    """
    elapsed_decimal = Decimal(str(execution_time_ms))
    return rate_table.cpu_rate_per_ms * elapsed_decimal


def attribute_validator_dispatch_cost(
    *,
    rate_table: RateTable,
    cost_chain: CostAttributionChain,
    audit_writer: AuditLedgerWriter,
    validator_id: str,
    execution_time_ms: float,
    span_id: str,
    idempotency_key: str,
    parent_idempotency_key: str,
    workflow_id: str,
    parent_action_id: str,
    tenant_id: str | None = None,
) -> SpanCostRecord:
    """Run the §C-OD-26.1 canonical cost-attribution chain for one validator dispatch.

    Computes CPU-meter cost (Decision 2.D5) from elapsed `execution_time_ms`
    → attaches idempotency-key → projects to CostRecordAuditPayload →
    converts via cp_audit_to_od_audit → appends to audit ledger. Returns the
    idempotency-key-bearing SpanCostRecord so the caller can emit the
    cost.attributed_decimal OTel span attribute per U-OD-49.

    Mirrors `attribute_tool_dispatch_cost` exactly except:
      - Cost compute: `_compute_validator_cost` (CPU-meter formula
        `cpu_rate_per_ms × execution_time_ms`) instead of the 3-branch
        tool `cost_kind` formula.
      - dispatch_kind: `DispatchKind.VALIDATOR` instead of `DispatchKind.TOOL`;
        provider_discriminator: `None` (no chain-level family tag per-dispatch).
      - gen_ai_provider_name: `f"validator:{validator_id}"` (repurposed
        carrier field; SpanCostRecord schema's `gen_ai_provider_name` is
        `str`-typed and permits non-LLM tags by C-OD-05 §5.1 row-15 family
        taxonomy).
      - gen_ai_request_model: empty string `""` (no model concept at
        validator dispatch; field is `str`-typed and non-nullable).

    Parameters
    ----------
    rate_table
        The resolved PRICE_TABLE_REF for this workflow's execution.
    cost_chain
        Concrete cost-attribution chain (consumed for
        `attach_idempotency_key` only; CPU-meter compute is direct-Decimal).
    audit_writer
        Audit-ledger writer.
    validator_id
        Validator identifier — typically `step.step_id` or a validator-class
        name. Repurposed into the SpanCostRecord.gen_ai_provider_name
        carrier-namespace as `f"validator:{validator_id}"`.
    execution_time_ms
        Elapsed wall-clock validator execution time per
        ConcreteValidatorFramework.evaluate() instrumentation
        (CP spec v1.24 §28.10.4 invariant 5).
    span_id
        Current validator.evaluate span's OTel span_id (hex form).
    idempotency_key
        Validator-dispatch idempotency key — composed at the caller
        (typically `f"validator:{workflow_id}:{step_id}"`).
    parent_idempotency_key
        Parent step's idempotency_key from `step_context`.
    workflow_id
        Parent workflow identifier from `step_context.workflow_id`.
    parent_action_id
        Parent action_id from `step_context.parent_action_id`.
    tenant_id
        Tenant scope for audit-ledger append (None → single-tenant).

    Returns
    -------
    SpanCostRecord
        The idempotency-key-bearing cost record for caller-side OTel
        attribute emission.

    Notes
    -----
    Per CP spec v1.24 §28.10.4 invariant 2, the
    `ValidatorPostEvaluateHook.on_post_evaluate` firing site at
    ConcreteValidatorFramework.evaluate() swallows exceptions; this helper
    MAY raise (e.g., rate-table misconfiguration) without crashing the
    validator dispatch.
    """
    # Substep 1 + 2 — compute CPU-meter cost. RATE_TABLE_V1's
    # `cpu_rate_per_ms` is the canonical CPU-rate substrate per §C-OD-28.1.
    # Decision 2.D5 RATIFIED chose CPU-meter as the canonical validator cost
    # discipline (rationale: validator execution dominated by CPU work; LLM
    # validators route their cost through the LLM-dispatch chain at the
    # nested llm_dispatch span, not at the outer validator.evaluate span;
    # double-count avoidance is structural, not policy).
    cost_decimal = _compute_validator_cost(rate_table, execution_time_ms)

    # Substep 3 — build SpanCostRecord; attach idempotency key joining to
    # the IS state-ledger parent entry per C-IS-05 / C-OD-14 §14.4.
    cost_record = SpanCostRecord(
        span_id=span_id,
        idempotency_key="",  # populated at attach_idempotency_key
        total_cost=float(cost_decimal),
        total_latency_ms=int(execution_time_ms),  # validator-specific:
        # we have elapsed time on hand. C-OD-14 §14.5 latency is informational
        # observability — non-canonical at the cost record. Coerced to int
        # per SpanCostRecord schema (latency in whole ms is the carrier shape).
        derived_keys=(),
        engine_replay_disposition=_DEFAULT_REPLAY_DISPOSITION,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=None,
        dispatch_kind=_VALIDATOR_DISPATCH_KIND,
        gen_ai_provider_name=f"validator:{validator_id}",
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
    # prefix branch; append to audit ledger. Same path as LLM-dispatch +
    # tool-dispatch precedents; the CXA converter routes by action_id
    # prefix not by surface kind.
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


class CostAttributingValidatorHook:
    """ValidatorPostEvaluateHook impl wiring validator.evaluate → cost-attribution.

    Implements `harness_cp.validator_framework_types.ValidatorPostEvaluateHook`
    Protocol (declared at CP spec v1.24 §28.10.1; Protocol Pythonic shape
    via @runtime_checkable + async on_post_evaluate). Constructed at
    `materialize_validator_framework_stage` factory binding time when
    `ctx.cost_chain` + `ctx.audit_writer` are bound (stage 4 OD ordering
    per stage_4_od.py:79-82 places cost_chain + audit_writer BEFORE
    validator_framework at line 89).

    Per CP spec v1.24 §28.10.4 invariant 2 + 3 (best-effort observability):
    exceptions from `attribute_validator_dispatch_cost` (e.g., rate-table
    misconfiguration) propagate to the hook firing site at
    `ConcreteValidatorFramework.evaluate()` which swallows them per
    §28.10.4 invariant 2. The hook itself does NOT catch exceptions —
    the catch happens at the framework firing site (preserves the
    `__cause__` chain through to the framework's exception-swallow boundary
    for debug visibility).
    """

    def __init__(
        self,
        *,
        rate_table: RateTable,
        cost_chain: CostAttributionChain,
        audit_writer: AuditLedgerWriter,
        cost_record_sink: SupportsCostRecordAppend | None = None,
    ) -> None:
        self._rate_table = rate_table
        self._cost_chain = cost_chain
        self._audit_writer = audit_writer
        # R-FS-1 arc CA — run-scoped cost-record sink (same list as
        # `ctx.cost_record_accumulator`, threaded by the stage-4 validator
        # factory). The returned SpanCostRecord is appended for the
        # `RunResult.cost_attribution` rollup (runtime spec v1.53 §9 C-RT-09).
        self._cost_record_sink = cost_record_sink

    async def on_post_evaluate(
        self,
        *,
        step: WorkflowStep,
        step_context: StepExecutionContext,
        evaluation: ValidatorEvaluation,
        execution_time_ms: float,
    ) -> None:
        """Invoke cost-attribution chain for the completed validator evaluation.

        validator_id derives from step.step_id (the per-step validator
        identifier; matches the validator registry key). span_id is
        synthesized from step_id + workflow_id since CP spec §25.5
        validator.evaluate span emission is the workflow-driver
        responsibility, not the framework's — the framework receives the
        evaluation envelope but not the OTel span. Span correlation at the
        audit-ledger row happens via the workflow.parent_action_id which IS
        available at step_context.

        Cost attribution proceeds even on non-PASS outcomes per U-OD-40
        AC #1 (every evaluation is billable per Decision 2.D5; failure
        outcomes still consumed CPU time).
        """
        validator_id = str(step.step_id)
        span_id = f"validator-evaluate-{step_context.workflow_id}-{step.step_id}"
        idempotency_key = f"validator:{step_context.workflow_id}:{step.step_id}"

        attached = attribute_validator_dispatch_cost(
            rate_table=self._rate_table,
            cost_chain=self._cost_chain,
            audit_writer=self._audit_writer,
            validator_id=validator_id,
            execution_time_ms=execution_time_ms,
            span_id=span_id,
            idempotency_key=idempotency_key,
            parent_idempotency_key=step_context.parent_idempotency_key,
            workflow_id=step_context.workflow_id,
            parent_action_id=step_context.parent_action_id,
            tenant_id=step_context.tenant_id,
        )

        # R-FS-1 arc CA — record into the run-scoped accumulator for the
        # `RunResult.cost_attribution` rollup (runtime spec v1.53 §9 C-RT-09).
        # Reached only on success — a helper exception propagates past this to
        # the framework firing site's swallow boundary (§28.10.4 invariant 2).
        if self._cost_record_sink is not None:
            self._cost_record_sink.append(attached)


__all__ = [
    "CostAttributingValidatorHook",
    "attribute_validator_dispatch_cost",
]
