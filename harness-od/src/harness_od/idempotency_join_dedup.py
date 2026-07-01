"""Idempotency-key join + replay-aware dedup + per-attempt cost-attribution — U-OD-20.

Implements C-OD-14 §14.4 (idempotency-key join — every per-span cost record
carries the parent's `idempotency_key` per C-IS-05; per-sub-agent inheritance
propagates a derived key per C-AS-15 §15.6) and §14.5 (F2-12 ✅ CLOSED) —
§14.5.1 trace-ingestion dedup algorithm, §14.5.2 replay-aware dedup with retry
orthogonality, §14.5.3 cause_attribution invariance check at
`deterministic_replay`, §14.5.4 per-attempt cost-attribution discipline.

The dedup algorithm is replay-aware: cost-per-span accrues exactly once per
attempt for re-emitting `engine.replay_disposition` values and zero additional
accrual for `deterministic_replay`. `dedupe_on_replay` discriminates per the CP
`ReplayDisposition` 5-value enum; `cause_attribution_invariance_check`
ESCALATEs to a `terminal-fail-exit` validator-fail on a replay
cause_attribution mismatch; `per_attempt_cost_attribution_roll_up` sums
per-attempt costs with `deterministic_replay` re-reads excluded.

F2-12 ✅ CLOSED affected-contract notation: this unit is the sole
contract-bearing F2-12 carry-forward site in the OD plan, CLOSED at OD plan
v2.2 cascade Step 6b. The closure-bearing notation preserves the historical
3-deferred-surface structure as record (`F2_12_DeferredSurface`) and carries
the 9-entry cascade execution path (`F2_12_CLOSURE_PATH`).

Authority: Implementation_Plan_Operational_Discipline_v2_2.md §3.5.3 U-OD-20
(v2.2 amendment absorbing OD spec v1.3 §14.5; v2.4 Form A citation-precision
amendment to acc #11 / Depends on; v2.6 §3.5.3 M-1 delta — `SpanRef` at
`attach_idempotency_key_to_cost_record` re-pointed to the U-OD-04 carrier,
`[U-OD-04]` edge added); Depends on: [U-OD-18, U-OD-19, U-OD-04, U-IS-12
(cross-axis: IS — C-IS-10 §10.2)]; Spec_Operational_Discipline_v1_3.md §14
C-OD-14 §14.4 + §14.5 + §14.5.1 + §14.5.2 + §14.5.3 + §14.5.4; ADR-D6 v1.2
§1.5-§1.5.3; ADR-D1 v1.2 §1.1.1 (`engine.replay_disposition` as per-class
dedup discriminator) + §1.1.2.2 (F2 ledger entry shape extension with
`original_trace_id` + `original_span_id`).
"""

from __future__ import annotations

from enum import StrEnum

from harness_cp.engine_namespace import ReplayDisposition
from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import SpanRef

__all__ = [
    "F2_12_CLOSURE_PATH",
    "F2_12_NOTATION",
    "ClosureStatus",
    "DedupOutcome",
    "DispatchKind",
    "F2StateLedgerEntry",
    "F2_12_AffectedContractNotation",
    "F2_12_DeferredSurface",
    "FilingStatus",
    "InvarianceCheckResult",
    "ParentOperationTotalCost",
    "ReplaySemanticDivergenceError",
    "RevisionStep",
    "SpanCostRecord",
    "SpanIngestionView",
    "attach_idempotency_key_to_cost_record",
    "cause_attribution_invariance_check",
    "dedupe_on_replay",
    "per_attempt_cost_attribution_roll_up",
    "propagate_to_subagent",
]


# --- cross-axis read-only view types ----------------------------------------
# `ReplayDisposition` is consumed READ-ONLY from the CP axis
# (`harness_cp.engine_namespace`; CP C-CP-09 §9.1 4-attribute `engine.*`
# namespace). `F2StateLedgerEntry` is the OD-axis read-only view of the F2
# state-ledger entry consumed at trace-ingestion dedup (C-OD-14 §14.5.1). The
# landed IS `StateLedgerEntry` (C-IS-05 §5) carries the six immutable F-layer
# fields; ADR-D1 v1.2 §1.1.2.2 extends the ledger entry shape with
# `original_trace_id` + `original_span_id`, and §14.5.1 dedup additionally
# reads `cause_attribution`. The precise cross-axis seam (which IS-axis carrier
# materializes the extended shape) resolves at sub-phase 7c against C-IS-10
# §10.2; here the dedup-relevant projection is materialized OD-local read-only,
# carrying exactly the fields the §14.5.1 algorithm consumes.


class F2StateLedgerEntry(BaseModel):
    """The dedup-relevant projection of an F2 state-ledger entry (C-OD-14 §14.5.1).

    Read-only at OD: the dedup algorithm at §14.5.1 looks up by
    `idempotency_key` and reads `original_trace_id` / `original_span_id`
    (ADR-D1 v1.2 §1.1.2.2 ledger-shape extension) + `cause_attribution`. The
    canonical F2 ledger entry is owned by the IS axis (C-IS-05 §5); the 7c
    cross-axis seam resolves the full carrier. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the harness-canonical join key per C-IS-05 / C-IS-10 §10.2.
    idempotency_key: str
    #: `original_trace_id` per ADR-D1 v1.2 §1.1.2.2 ledger-shape extension.
    original_trace_id: str
    #: `original_span_id` per ADR-D1 v1.2 §1.1.2.2 ledger-shape extension.
    original_span_id: str
    #: the stored cause_attribution — compared at the §14.5.3 invariance check.
    cause_attribution: str | None


class SpanIngestionView(BaseModel):
    """The trace-ingestion-time projection of a span (C-OD-14 §14.5.1).

    The dedup algorithm at §14.5.1 consumes `trace_id`, `span_id`,
    `idempotency_key` (from the F2 state-ledger join), `engine.replay_disposition`,
    and the optional `retry.attempt_number` / `retry.cause_attribution`. This
    is the OD-local read-only carrier for those fields at ingestion time —
    distinct from `SpanRef` (the OTel-SDK span handle), which is the live-span
    handle threaded through emission. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the span's OTel trace id.
    trace_id: str
    #: the span's OTel span id.
    span_id: str
    #: the parent's `idempotency_key` (F2 state-ledger join key).
    idempotency_key: str
    #: `engine.replay_disposition` (CP C-CP-09 §9.1).
    engine_replay_disposition: ReplayDisposition
    #: `retry.attempt_number` — `None` outside a retry context (§14.5.2).
    retry_attempt_number: int | None
    #: `retry.cause_attribution` — compared at the §14.5.3 invariance check.
    retry_cause_attribution: str | None


# --- dispatch-type cost taxonomy (C-OD-15 §15.1.1, v1.30 — B-COST-DISCRIMINATOR-TAXONOMY) ---


class DispatchKind(StrEnum):
    """The bounded dispatch-type vocabulary for per-dispatch cost rollup (C-OD-15 §15.1.1).

    The `RollupAxis.PER_DISPATCH_KIND` cost breakdown keys on this — the
    operator-meaningful llm-vs-tool-vs-validator-vs-webhook split of run cost.

    Homed in this U-OD-20 carrier module (NOT in the U-OD-21 consumer like
    `CrossFamilyTag`) because it is the cost record's **own** attribute — the
    kind of dispatch that produced it. So `SpanCostRecord.dispatch_kind` types
    it *directly* as a typed enum (illegal states unrepresentable, CLAUDE.md §4),
    rather than the `str`+validate discipline `provider_discriminator` uses to
    avoid a U-OD-20 → U-OD-21 cycle. The four members map 1:1 to the four
    production `cost_attribution_*_dispatch.py` cost helpers.
    """

    LLM = "llm"
    TOOL = "tool"
    VALIDATOR = "validator"
    WEBHOOK = "webhook"


# --- per-span cost record (C-OD-14 §14.4 + §14.5.2 + §14.5.3) ---------------


class SpanCostRecord(BaseModel):
    """The per-span cost record — 13 fields at v1.30 (C-OD-14 §14.4 + §14.5.2/.3).

    Carries the parent's `idempotency_key` per C-IS-05 (the §14.4 join key),
    `total_cost` / `total_latency_ms` from the U-OD-19 `SpanTotalCost`, the
    `derived_keys` for sub-agent inheritance per C-AS-15 §15.6, and the four
    v2.2 replay-orthogonality fields per OD spec v1.3 §14.5.2 / §14.5.3:
    `engine_replay_disposition`, `retry_attempt_number`,
    `retry_cause_attribution`, `is_replay_derived`.

    v2.8 (D-5): three rollup-key fields appended so the U-OD-21 cross-family
    rollup (`rollup_costs_by_axis`, C-OD-15 §15.1) is materializable —
    `provider_discriminator`, `gen_ai_provider_name` and `gen_ai_request_model`
    (C-OD-04 §4.3 base-layer attributes). `gen_ai_*` are `str`-typed; the cost
    record carries the provider identity of the span whose cost it records.

    v1.30 (B-COST-DISCRIMINATOR-TAXONOMY): a fourth rollup-key field
    `dispatch_kind` (the typed `DispatchKind` enum) carries the dispatch-type
    dimension for the new `RollupAxis.PER_DISPATCH_KIND`. The pre-existing
    `provider_discriminator` is the cross-family fallback-chain family tag per
    C-OD-15 §15.1 — a *chain-composition* concept (§15.3) a per-dispatch helper
    has no context for, so it is now `str | None` (`None` at the per-dispatch
    site; populated by the §15.3 fallback-chain composition). It stays
    `str`-typed (not `CrossFamilyTag`) to avoid a U-OD-20 → U-OD-21 cycle; the
    production helpers no longer write dispatch-type strings into it (that was
    the latent contract-vs-production defect this arc fixes — the dispatch type
    now lives in `dispatch_kind`).

    Frozen → `Eq`; this is the carrier U-OD-21 `rollup_costs_by_axis` consumes
    (acceptance #1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the span's id.
    span_id: str
    #: the parent span's `idempotency_key` per C-IS-05 (§14.4 join key).
    idempotency_key: str
    #: per-span total cost (USD) — from U-OD-19 `SpanTotalCost.total_cost`.
    total_cost: float
    #: per-span total latency (ms) — from U-OD-19 `SpanTotalCost.total_latency_ms`.
    total_latency_ms: int
    #: derived `idempotency_key`s for sub-agent inheritance (C-AS-15 §15.6).
    derived_keys: tuple[str, ...]
    #: `engine.replay_disposition` — CP plan U-CP-21 4-attribute schema (§14.5.2).
    engine_replay_disposition: ReplayDisposition
    #: `retry.attempt_number` — `None` outside a retry context (§14.5.2).
    retry_attempt_number: int | None
    #: `retry.cause_attribution` — for the §14.5.3 invariance check.
    retry_cause_attribution: str | None
    #: set by the dedup algorithm — `True` for replay-derived spans (§14.5.1).
    is_replay_derived: bool
    #: v2.8 — cross-family fallback-chain family tag (C-OD-15 §15.1); `str`-typed
    #: to avoid a U-OD-20 → U-OD-21 cycle (`CrossFamilyTag` is U-OD-21's bounded
    #: vocabulary `rollup_costs_by_axis` validates non-`None` values against).
    #: v1.30 — `None` at the per-dispatch site (the family tag is a §15.3
    #: chain-composition concept a per-dispatch helper has no context for);
    #: populated by the §15.3 fallback-chain composition. The dispatch type now
    #: lives in `dispatch_kind`, NOT here (the fixed contract-vs-production defect).
    provider_discriminator: str | None = None
    #: v1.30 — the dispatch type (B-COST-DISCRIMINATOR-TAXONOMY); the typed key
    #: for `RollupAxis.PER_DISPATCH_KIND`. Enum-typed directly (carrier-homed,
    #: no cycle — unlike `provider_discriminator`).
    dispatch_kind: DispatchKind
    #: v2.8 — the span's `gen_ai.provider.name` (C-OD-04 §4.3).
    gen_ai_provider_name: str
    #: v2.8 — the span's `gen_ai.request.model` (C-OD-04 §4.3).
    gen_ai_request_model: str


def attach_idempotency_key_to_cost_record(
    span: SpanRef,
    parent_idempotency: str,
    cost_record: SpanCostRecord,
) -> SpanCostRecord:
    """Attach the parent's `idempotency_key` to a per-span cost record (C-OD-14 §14.4).

    Returns a `SpanCostRecord` with `idempotency_key` set to the parent's value
    (acceptance #2). `span` is the live OTel-SDK span handle (`SpanRef`,
    carried at U-OD-04); replay-safe composition with the F2 state-ledger is
    enforced downstream by `dedupe_on_replay` (the §14.5.1 algorithm — no
    longer promissory at v2.2). The parameter is consumed for the span-handle
    correlation; the join key itself is `parent_idempotency`.
    """
    _ = span  # span handle threaded for correlation; join key is parent_idempotency
    return cost_record.model_copy(update={"idempotency_key": parent_idempotency})


def propagate_to_subagent(parent_idempotency: str) -> str:
    """Derive a sub-agent `idempotency_key` from the parent's (C-AS-15 §15.6).

    Per the §14.4 per-sub-agent inheritance row: sub-agent dispatch propagates
    a derived `idempotency_key`. The derived key namespaces the parent key
    under a `subagent` discriminator so per-sibling rollup at §14.3 composes
    against the derived keys without colliding with the parent (acceptance #4).
    """
    return f"{parent_idempotency}::subagent"


# --- §14.5.1 trace-ingestion dedup algorithm --------------------------------


class DedupOutcome(StrEnum):
    """The trace-ingestion dedup outcome (C-OD-14 §14.5.1 / §14.5.2).

    `DROP_DETERMINISTIC_REPLAY_RE_READ` — `deterministic_replay` re-read of a
    matching ledger entry; zero additional cost accrual, replay invisible at
    D6. `RECORD_REPLAY_DERIVED` — `checkpoint_resume` / `reconciler_iteration`
    / `wal_consume` re-emission; recorded as a new replay-derived ingestion.
    `RECORD_FIRST_INGESTION` — no matching ledger entry; first ingestion.
    `ERROR_UNEXPECTED_RE_INGESTION_FOR_NO_REPLAY` — `no_replay` disposition
    with a matching ledger entry (unexpected). `ESCALATE_REPLAY_SEMANTIC_DIVERGENCE`
    — `deterministic_replay` with a cause_attribution mismatch (§14.5.3).
    """

    DROP_DETERMINISTIC_REPLAY_RE_READ = "DROP_DETERMINISTIC_REPLAY_RE_READ"
    RECORD_REPLAY_DERIVED = "RECORD_REPLAY_DERIVED"
    RECORD_FIRST_INGESTION = "RECORD_FIRST_INGESTION"
    ERROR_UNEXPECTED_RE_INGESTION_FOR_NO_REPLAY = "ERROR_UNEXPECTED_RE_INGESTION_FOR_NO_REPLAY"
    ESCALATE_REPLAY_SEMANTIC_DIVERGENCE = "ESCALATE_REPLAY_SEMANTIC_DIVERGENCE"


#: the `engine.replay_disposition` values that RECORD a new replay-derived span
#: at trace-ingestion (C-OD-14 §14.5.1 — re-emission expected).
_REPLAY_DERIVED_DISPOSITIONS: frozenset[ReplayDisposition] = frozenset(
    {
        ReplayDisposition.CHECKPOINT_RESUME,
        ReplayDisposition.RECONCILER_ITERATION,
        ReplayDisposition.WAL_CONSUME,
    }
)


def dedupe_on_replay(
    span: SpanIngestionView,
    ledger_entry: F2StateLedgerEntry | None,
) -> DedupOutcome:
    """Resolve the trace-ingestion dedup outcome for `span` (C-OD-14 §14.5.1).

    Materializes the §14.5.1 algorithm verbatim. When `ledger_entry` is `None`
    (no prior entry for the join key), the span is a first ingestion →
    `RECORD_FIRST_INGESTION`. When a `ledger_entry` exists, the outcome
    discriminates on `span.engine_replay_disposition`:

    - `deterministic_replay` — verify `trace_id` + `span_id` match the ledger
      entry; check cause_attribution invariance (§14.5.3). On match → DROP
      (zero new cost accrual). On a `trace_id`/`span_id` mismatch OR a
      cause_attribution mismatch → `ESCALATE_REPLAY_SEMANTIC_DIVERGENCE`.
    - `checkpoint_resume` / `reconciler_iteration` / `wal_consume` —
      re-emission expected → `RECORD_REPLAY_DERIVED`.
    - `no_replay` — re-ingestion unexpected → ERROR (acceptance #3 / #12).
    """
    if ledger_entry is None:
        return DedupOutcome.RECORD_FIRST_INGESTION

    disposition = span.engine_replay_disposition
    if disposition is ReplayDisposition.DETERMINISTIC_REPLAY:
        topology_matches = (
            span.trace_id == ledger_entry.original_trace_id
            and span.span_id == ledger_entry.original_span_id
        )
        cause_matches = span.retry_cause_attribution == ledger_entry.cause_attribution
        if topology_matches and cause_matches:
            return DedupOutcome.DROP_DETERMINISTIC_REPLAY_RE_READ
        return DedupOutcome.ESCALATE_REPLAY_SEMANTIC_DIVERGENCE
    if disposition in _REPLAY_DERIVED_DISPOSITIONS:
        return DedupOutcome.RECORD_REPLAY_DERIVED
    # disposition is ReplayDisposition.NO_REPLAY
    return DedupOutcome.ERROR_UNEXPECTED_RE_INGESTION_FOR_NO_REPLAY


# --- §14.5.3 cause_attribution invariance check -----------------------------


class InvarianceCheckResult(StrEnum):
    """The §14.5.3 cause_attribution invariance check result (C-OD-14 §14.5.3).

    `PASS` — `deterministic_replay` span's cause_attribution matches the
    ledger entry. `ESCALATE_REPLAY_SEMANTIC_DIVERGENCE` — mismatch; an
    engine-replay-contract violation. `NOT_APPLICABLE` — the span is not a
    `deterministic_replay` disposition, so the invariance check does not apply.
    """

    PASS = "PASS"
    ESCALATE_REPLAY_SEMANTIC_DIVERGENCE = "ESCALATE_REPLAY_SEMANTIC_DIVERGENCE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReplaySemanticDivergenceError(BaseModel):
    """The ESCALATE event emitted on a §14.5.3 cause_attribution mismatch.

    Per §14.5.3 the escalation carries fixed validator-fail attributes:
    `validator.fail.class = terminal-fail-exit`, `validator.fail.cause_attribution
    = replay_semantic_divergence` (the new C5 catalog value added at OD spec
    v1.3), `validator.fail.permanence = permanent`. The event is always-sampled
    per C-OD-09 §9.2 (`validator.fail.permanence=permanent` always-sampled).
    Frozen → `Eq`; a structured record, NOT a raised exception — the escalation
    is an emitted observability event, not control flow.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `validator.fail.class` — fixed `terminal-fail-exit` per §14.5.3.
    validator_fail_class: str = "terminal-fail-exit"
    #: `validator.fail.cause_attribution` — fixed per §14.5.3.
    validator_fail_cause_attribution: str = "replay_semantic_divergence"
    #: `validator.fail.permanence` — fixed `permanent` per §14.5.3.
    validator_fail_permanence: str = "permanent"
    #: always-sampled per C-OD-09 §9.2 — fixed `True`.
    always_sampled: bool = True


def cause_attribution_invariance_check(
    span: SpanIngestionView,
    ledger_entry: F2StateLedgerEntry,
) -> InvarianceCheckResult:
    """Check cause_attribution invariance under `deterministic_replay` (C-OD-14 §14.5.3).

    Applies the §14.5.3 invariance assertion: iff
    `span.engine_replay_disposition == deterministic_replay`, the span's
    `retry.cause_attribution` MUST equal the ledger entry's stored
    cause_attribution. A mismatch signals replay-introduced semantic
    divergence — a deterministic-replay-contract violation — and the result is
    `ESCALATE_REPLAY_SEMANTIC_DIVERGENCE` (the caller emits the
    `ReplaySemanticDivergenceError` event). For non-`deterministic_replay`
    dispositions the check is `NOT_APPLICABLE` (acceptance #13).
    """
    if span.engine_replay_disposition is not ReplayDisposition.DETERMINISTIC_REPLAY:
        return InvarianceCheckResult.NOT_APPLICABLE
    if span.retry_cause_attribution == ledger_entry.cause_attribution:
        return InvarianceCheckResult.PASS
    return InvarianceCheckResult.ESCALATE_REPLAY_SEMANTIC_DIVERGENCE


# --- §14.5.4 per-attempt cost-attribution discipline ------------------------


class ParentOperationTotalCost(BaseModel):
    """The parent operation's total cost rolled up per-attempt (C-OD-14 §14.5.4).

    `total_cost` is `Σ cost(retry-attempt child span_i) for i in 1..N`;
    `per_attempt_costs` maps `retry.attempt_number → cost`;
    `replay_re_reads_excluded` counts the `deterministic_replay` re-reads
    filtered out before aggregation (they contribute zero — cost was accrued
    at first execution). Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the parent operation's id.
    parent_operation_id: str
    #: `Σ` per-attempt costs (`deterministic_replay` re-reads excluded).
    total_cost: float
    #: `retry.attempt_number → cost` for each counted attempt.
    per_attempt_costs: dict[int, float]
    #: count of `deterministic_replay` re-reads excluded from the sum.
    replay_re_reads_excluded: int


def per_attempt_cost_attribution_roll_up(
    parent_operation_id: str,
    retry_attempt_costs: list[SpanCostRecord],
) -> ParentOperationTotalCost:
    """Roll up per-attempt costs into the parent operation total (C-OD-14 §14.5.4).

    Computes `total_cost = Σ cost(retry-attempt child span_i) for i in 1..N`
    per §14.5.4. A `SpanCostRecord` flagged `is_replay_derived` with a
    `deterministic_replay` disposition is a replay re-read — its cost was
    accrued at first execution, so it contributes ZERO to the sum and is
    counted in `replay_re_reads_excluded` (acceptance #14). Records are keyed
    in `per_attempt_costs` by `retry_attempt_number`; a record with no
    attempt number is keyed under attempt `1` (the implicit single-attempt
    case). The roll-up composes with the C-OD-23 operator-burden eval
    primitive at the per-operation aggregation level without re-aggregation.
    """
    total_cost = 0.0
    per_attempt_costs: dict[int, float] = {}
    replay_re_reads_excluded = 0
    for record in retry_attempt_costs:
        is_deterministic_re_read = (
            record.engine_replay_disposition is ReplayDisposition.DETERMINISTIC_REPLAY
        )
        if is_deterministic_re_read:
            replay_re_reads_excluded += 1
            continue
        attempt = record.retry_attempt_number if record.retry_attempt_number else 1
        total_cost += record.total_cost
        per_attempt_costs[attempt] = per_attempt_costs.get(attempt, 0.0) + record.total_cost
    return ParentOperationTotalCost(
        parent_operation_id=parent_operation_id,
        total_cost=total_cost,
        per_attempt_costs=per_attempt_costs,
        replay_re_reads_excluded=replay_re_reads_excluded,
    )


# --- F2-12 ✅ CLOSED affected-contract notation ------------------------------


class F2_12_DeferredSurface(StrEnum):  # noqa: N801 — name is the U-OD-20 plan signature verbatim
    """The 3 F2-12 deferred surfaces — preserved as historical record (C-OD-14 §14.5).

    Exactly 3 surfaces per §14.5 v1.2 verbatim. All 3 are CLOSED at the F2-12
    cascade: surface 1 at D1 v1.2 §1.1.1/§1.1.2; surface 2 at D6 v1.2 §1.2.2
    (corrected to child-per-attempt); surface 3 at D6 v1.2 §1.5 + OD spec v1.3
    §14.5.1 (acceptance #5).
    """

    SPAN_REEMISSION_SEMANTICS_UNDER_ENGINE_REPLAY = "SPAN_REEMISSION_SEMANTICS_UNDER_ENGINE_REPLAY"
    RETRY_ATTEMPT_SIBLING_SPAN_DISCIPLINE_AT_D6_INGESTION = (
        "RETRY_ATTEMPT_SIBLING_SPAN_DISCIPLINE_AT_D6_INGESTION"
    )
    TRACE_INGESTION_DEDUP_COMPOSITION_ALGORITHM = "TRACE_INGESTION_DEDUP_COMPOSITION_ALGORITHM"


class ClosureStatus(StrEnum):
    """The F2-12 affected-contract closure status (C-OD-14 §14.5; v2.2 amendment).

    `ACTIVE` — historical (v1 / v2.1). `CLOSED_AT_CASCADE_STEP_6B` — closed at
    OD plan v2.2 cascade Step 6b (this unit's status). `CLOSED_AT_DECLARATION`
    — formal `closure_pending false` at `F2-12_Closure_Declaration.md`.
    """

    ACTIVE = "ACTIVE"
    CLOSED_AT_CASCADE_STEP_6B = "CLOSED_AT_CASCADE_STEP_6B"
    CLOSED_AT_DECLARATION = "CLOSED_AT_DECLARATION"


class FilingStatus(StrEnum):
    """The filing status of a cascade revision step (C-OD-14 §14.5)."""

    FILED = "FILED"
    PENDING = "PENDING"


class RevisionStep(BaseModel):
    """One step of the F2-12 cascade execution path (C-OD-14 §14.5).

    Carries the `step_number`, the `step_label` ("Step 1" / "Step 2a" / ...),
    the `artifact` revised, the `scope` of the revision, the `filing_status`,
    and the `filing_date`. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_number: int
    step_label: str
    artifact: str
    scope: str
    filing_status: FilingStatus
    filing_date: str | None


class F2_12_AffectedContractNotation(BaseModel):  # noqa: N801 — name is the U-OD-20 plan signature verbatim
    """The F2-12 ✅ CLOSED affected-contract notation (C-OD-14 §14.5; v2.2 amendment).

    This unit is the sole contract-bearing F2-12 carry-forward site in the OD
    plan, CLOSED at OD plan v2.2 cascade Step 6b. The notation carries the
    contract id, the historical active engagement site, the v1.3 closed
    engagement site, the 3 deferred surfaces (historical record) with their
    per-surface closing cascade step, the v1 / v2.2 commitment levels, the
    9-entry closure path, the closure-pending flags, and the closure status.
    Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: fixed `C-OD-14` (acceptance #6).
    contract_id: str = "C-OD-14"
    #: historical active engagement site (acceptance #6).
    active_engagement_site: str = "C-OD-14 §14.5"
    #: the v1.3 closed engagement site (acceptance #6).
    closed_engagement_site: str = "C-OD-14 §14.5 (closed) + §14.5.1 + §14.5.2 + §14.5.3 + §14.5.4"
    #: the 3 deferred surfaces — historical record (acceptance #5).
    deferred_surfaces_at_v1: frozenset[F2_12_DeferredSurface]
    #: each deferred surface mapped to its closing cascade step label (acc #5).
    closure_status_per_surface: dict[F2_12_DeferredSurface, str]
    #: the v1 commitment level — §14.5 v1.2 verbatim (acceptance #7).
    v1_commitment_level: str = (
        "cost-attribution-per-span formula + sandbox-tier overhead + "
        "per-sibling rollup + idempotency-key join"
    )
    #: the v2.2 commitment level — §14.5 v1.3 verbatim closure (acceptance #7).
    v2_2_commitment_level: str = (
        "v1 + dedup algorithm + replay-aware orthogonality + "
        "cause_attribution invariance check + per-attempt cost-attribution discipline"
    )
    #: the 9-entry cascade closure path (acceptance #8).
    closure_path: tuple[RevisionStep, ...]
    #: historical — `closure_pending` was `True` at v1.
    closure_pending_at_v1: bool = True
    #: v2.2 — `closure_pending` is `False` (acceptance #9).
    closure_pending_at_v2_2: bool = False
    #: the closure status — `CLOSED_AT_CASCADE_STEP_6B` (acceptance #6).
    closure_status: ClosureStatus = ClosureStatus.CLOSED_AT_CASCADE_STEP_6B


#: The F2-12 cascade execution path — 9 entries at v2.2 (was 6 at v2.1),
#: reflecting the cascade-discovered sub-step decomposition (Step 2 → 2a + 2b;
#: Step 5 → 5a + 5b; Step 6 → 6a + 6b). All 9 steps FILED; Close pending
#: (acceptance #8 / #10).
F2_12_CLOSURE_PATH: tuple[RevisionStep, ...] = (
    RevisionStep(
        step_number=1,
        step_label="Step 1",
        artifact="Council deliberation",
        scope="Substantive resolution substrate for all three sub-scopes",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=2,
        step_label="Step 2a",
        artifact="ADR-D1 v1.1 -> v1.2",
        scope="Sub-scope (i) span re-emission semantics",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=3,
        step_label="Step 2b",
        artifact="ADR-D6 v1.1 -> v1.2",
        scope="Sub-scopes (ii) + (iii) retry + dedup",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=4,
        step_label="Step 3",
        artifact="ADD v1.2 -> v1.3",
        scope="Cross-axis consolidation absorbing D1 + D6 v1.2",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=5,
        step_label="Step 4",
        artifact="PRD v1.0.1 -> v1.1",
        scope="R-CP-04 + R-CP-07 + R-OD-05 observable-behavior absorption",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=6,
        step_label="Step 5a",
        artifact="CP spec v1.2 -> v1.3",
        scope="C-CP-08 + C-CP-09 + §3.5 + §5.4 contract-surface absorption",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=7,
        step_label="Step 5b",
        artifact="OD spec v1.2 -> v1.3",
        scope="C-OD-14 §14.5.1 dedup algorithm + §14.5.1-§14.5.4 absorption",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=8,
        step_label="Step 6a",
        artifact="CP plan v2.1 -> v2.2",
        scope="U-CP-20 + U-CP-21 + U-CP-55 plan-level absorption",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
    RevisionStep(
        step_number=9,
        step_label="Step 6b",
        artifact="OD plan v2.1 -> v2.2",
        scope="U-OD-20 dedup + orthogonality + invariance + per-attempt",
        filing_status=FilingStatus.FILED,
        filing_date="2026-05-14",
    ),
)


#: The F2-12 ✅ CLOSED affected-contract notation — `closure_status` is
#: `CLOSED_AT_CASCADE_STEP_6B` (acceptance #6 / #9 / #10).
F2_12_NOTATION: F2_12_AffectedContractNotation = F2_12_AffectedContractNotation(
    deferred_surfaces_at_v1=frozenset(F2_12_DeferredSurface),
    closure_status_per_surface={
        F2_12_DeferredSurface.SPAN_REEMISSION_SEMANTICS_UNDER_ENGINE_REPLAY: "Step 2a",
        F2_12_DeferredSurface.RETRY_ATTEMPT_SIBLING_SPAN_DISCIPLINE_AT_D6_INGESTION: ("Step 2b"),
        F2_12_DeferredSurface.TRACE_INGESTION_DEDUP_COMPOSITION_ALGORITHM: "Step 2b",
    },
    closure_path=F2_12_CLOSURE_PATH,
)
