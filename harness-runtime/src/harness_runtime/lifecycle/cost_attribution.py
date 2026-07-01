"""Cost-attribution 5-step chain wiring — stage 4 OD (U-RT-31).

Per `Spec_Harness_Runtime_v1.md` v1.1 §7 (C-RT-07 stage 4 invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L6 (U-RT-31). The runtime
binds the OD cost-attribution primitives into a single reference-time
registry composing the C-OD-12 + C-OD-13 + C-OD-14 + C-OD-15 + C-OD-16 +
C-OD-17 contracts:

- `harness_od.cost_formula` — `compute_span_cost_with_rates(inputs, rates)`
  (C-OD-14 §14.1 per-span cost formula); `SpanCostInputs` + `PriceRateEntry`
  + `PriceRateKey` schemas.
- `harness_od.cost_attribution_sandbox_fanout` — `compose_span_total_cost`
  (C-OD-14 §14.2 sandbox-tier overhead composition); `rollup_fanout_at_close`
  (C-OD-14 §14.3 fan-out aggregation); `SandboxOverhead` + `SpanTotalCost`
  + `FanOutPattern` + `FanOutRollupResult` schemas.
- `harness_od.idempotency_join_dedup` — `attach_idempotency_key_to_cost_record`
  (C-OD-14 §14.4 join-key composition); `dedupe_on_replay` (C-OD-14 §14.5.1
  replay-aware dedup decision); `SpanCostRecord` 12-field schema.
- `harness_od.cost_attribution_dashboard_binding` — `PER_CELL_DASHBOARD_BINDINGS`
  (C-OD-16 §16.1 8-cell binding table); `compute_alerting_signal` (§16.2);
  `base_rate_for(cell)` (§10.3); `CellDashboardBinding` + `AlertingSignal` +
  `AlertingThresholdComposition` schemas.
- `harness_od.operator_burden_eval_primitives` — `EVAL_PRIMITIVE_DECLARATIONS`
  (C-OD-17 §17.1 5-row table); `EVAL_EMISSION_CONTRACT` (§17.2 separate-child-span
  commitment); `emit_eval_as_child_span` + `reject_span_event_only_emission`
  (§17.2 enforcement).

**The 5-step cost-attribution chain (C-OD-12 + C-OD-13).** Each step is a
pure composition of an OD primitive:

1. **Per-attempt cost** — `compute_span_cost_with_rates(SpanCostInputs,
   PriceRateEntry)` applies C-OD-14 §14.1 verbatim:
   `uncached*base_input + cache_creation*1.25*base_input +
   cache_read*0.10*base_input + output*base_output`.
2. **Sandbox-overhead composition** — `compose_span_total_cost(span_cost,
   duration_ms, sandbox_overhead)` applies C-OD-14 §14.2:
   `total_cost = span_cost + sandbox_overhead.tier_overhead_usd`;
   `total_latency_ms = duration_ms + sandbox_overhead.tier_overhead_ms`.
   Non-sandbox spans pass `sandbox_overhead=None`.
3. **Idempotency-key join** — `attach_idempotency_key_to_cost_record(
   span_ref, parent_idempotency_key, cost_record)` applies C-OD-14 §14.4:
   sets the record's `idempotency_key` to the parent's for cross-span
   roll-up at the F2 ledger join.
4. **Fan-out rollup at close** — `rollup_fanout_at_close(parent_span_ref,
   sibling_costs, pattern)` applies C-OD-14 §14.3:
   `parent_fanout_total_cost = Σ sibling.total_cost` regardless of pattern;
   `parent_fanout_total_latency = max(sibling.total_latency_ms)` for
   PARALLEL or `Σ` for SEQUENTIAL.
5. **Replay-aware dedup + cause_attribution invariance** —
   `dedupe_on_replay(span, ledger_entry)` applies C-OD-14 §14.5.1: a
   deterministic-replay matching span yields
   `DROP_DETERMINISTIC_REPLAY_RE_READ` (no new cost accrual); replay-derived
   dispositions yield `RECORD_REPLAY_DERIVED`; cause_attribution mismatch
   escalates per §14.5.3.

**Cross-axis composition with U-RT-24.** The U-RT-24 `RuntimeRetryBreaker`
also wraps `dedupe_on_replay` (under its `dedupe_decision` method). Both
sites compose the same OD function at different surfaces — retry sees the
dedup decision in the failure-handling loop; cost-attribution sees it as
step 5 of the chain. No conflict; the OD function is pure.

**Rate table is library-only at HEAD.** The OD `compute_span_cost(inputs)`
(without an explicit rate snapshot) raises `RateLookupError` until U-OD-21's
resident rate table lands. Callers compose their own `PriceRateEntry`
snapshot and use `compute_span_cost_with_rates`. The registry's
`compute_per_attempt_cost(inputs, rates)` surface threads through this
snapshot-based discipline; the live rate-table refresh is a future-unit
concern.

Per-component landing posture:

- `RuntimeCostAttributionChain` — frozen dataclass; stateless composition
  of the 5-step chain + dashboard binding + eval primitives. Exposes the
  per-step methods + property surfaces for canonical tables.
- `CostAttributionStage` — frozen materialization stage carrying the
  registry.
- `materialize_cost_attribution_stage(config)` — sync composer (stateless).

Scope discipline (U-RT-31 boundary held): NO audit-ledger writer (U-RT-32),
NO live rate table (U-OD-21 deferred), NO live dashboard backend (the
binding-form table is exposed but the actual dashboard surface is per-cell
deployment-binding-time). This unit lands the per-span chain composition +
canonical-table surfaces only.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_core.workload_class import WorkloadClass
from harness_od.cost_attribution_dashboard_binding import (
    PER_CELL_DASHBOARD_BINDINGS,
    AlertingSignal,
    AlertingThresholdComposition,
    CellDashboardBinding,
    compute_alerting_signal,
)
from harness_od.cost_attribution_sandbox_fanout import (
    FanOutPattern,
    FanOutRollupResult,
    SandboxOverhead,
    SpanTotalCost,
    compose_span_total_cost,
    rollup_fanout_at_close,
)
from harness_od.cost_formula import (
    PriceRateEntry,
    SpanCostInputs,
    compute_span_cost_with_rates,
)
from harness_od.idempotency_join_dedup import (
    DedupOutcome,
    F2StateLedgerEntry,
    SpanCostRecord,
    SpanIngestionView,
    attach_idempotency_key_to_cost_record,
    dedupe_on_replay,
)
from harness_od.observability_matrix import CellID
from harness_od.operator_burden_eval_primitives import (
    EVAL_EMISSION_CONTRACT,
    EVAL_PRIMITIVE_DECLARATIONS,
    EvalEmissionContract,
    EvalPrimitiveDeclaration,
)
from harness_od.otel_genai_base import SpanRef

from harness_runtime.types import RuntimeConfig

__all__ = [
    "CostAttributionStage",
    "RuntimeCostAttributionChain",
    "materialize_cost_attribution_stage",
]


@dataclass(frozen=True, slots=True)
class RuntimeCostAttributionChain:
    """Concrete `CostAttributionChain` Protocol implementation (U-RT-31).

    Composes the 5-step cost-attribution chain + dashboard binding + eval
    primitives into a single reference-time surface. The registry holds no
    mutable state — every method is a pure composition of the underlying OD
    decision function or a property carrying an OD static table. The L8
    LOOP_INIT orchestrator + the U-RT-32 audit-writer compose against this
    registry's outputs at workflow-lifecycle events.

    Per-table properties expose the canonical OD data verbatim:

    - `dashboard_bindings` → `PER_CELL_DASHBOARD_BINDINGS` (8 ACTIVE cells,
      C-OD-16 §16.1).
    - `eval_primitives` → `EVAL_PRIMITIVE_DECLARATIONS` (5 primitives,
      C-OD-17 §17.1).
    - `eval_emission_contract` → `EVAL_EMISSION_CONTRACT` (§17.2
      separate-child-span commitment).
    """

    @property
    def dashboard_bindings(self) -> dict[CellID, CellDashboardBinding]:
        """The 8-cell C-OD-16 §16.1 per-cell dashboard binding table."""
        return PER_CELL_DASHBOARD_BINDINGS

    @property
    def eval_primitives(self) -> tuple[EvalPrimitiveDeclaration, ...]:
        """The 5-row C-OD-17 §17.1 operator-burden eval primitive table."""
        return EVAL_PRIMITIVE_DECLARATIONS

    @property
    def eval_emission_contract(self) -> EvalEmissionContract:
        """The C-OD-17 §17.2 separate-child-span eval emission commitment."""
        return EVAL_EMISSION_CONTRACT

    # --- Step 1: per-attempt cost (C-OD-14 §14.1) -------------------------

    def compute_per_attempt_cost(
        self,
        inputs: SpanCostInputs,
        rates: PriceRateEntry,
    ) -> float:
        """Step 1 — apply the C-OD-14 §14.1 per-span cost formula.

        Returns the per-span cost in USD. `rates` is a `PriceRateEntry`
        snapshot supplied by the caller; the resident rate table at U-OD-21
        is deferred (the rate-table-less `compute_span_cost(inputs)` would
        raise `RateLookupError` at HEAD). Pure composition of
        `harness_od.cost_formula.compute_span_cost_with_rates`."""
        return compute_span_cost_with_rates(inputs, rates)

    # --- Step 2: sandbox-overhead composition (C-OD-14 §14.2) -------------

    def compose_total_cost(
        self,
        span_cost: float,
        span_duration_ms: int,
        sandbox_overhead: SandboxOverhead | None,
    ) -> SpanTotalCost:
        """Step 2 — apply the C-OD-14 §14.2 sandbox-overhead composition.

        `total_cost = span_cost + sandbox_overhead.tier_overhead_usd` (when
        sandboxed); `total_latency_ms = span_duration_ms +
        sandbox_overhead.tier_overhead_ms`. Non-sandbox spans pass
        `sandbox_overhead=None` and the totals equal the inputs. Pure
        composition of
        `harness_od.cost_attribution_sandbox_fanout.compose_span_total_cost`."""
        return compose_span_total_cost(
            span_cost=span_cost,
            span_duration_ms=span_duration_ms,
            sandbox_overhead=sandbox_overhead,
        )

    # --- Step 3: idempotency-key join (C-OD-14 §14.4) ---------------------

    def attach_idempotency_key(
        self,
        span: SpanRef,
        parent_idempotency_key: str,
        cost_record: SpanCostRecord,
    ) -> SpanCostRecord:
        """Step 3 — apply the C-OD-14 §14.4 idempotency-key join composition.

        Returns a new `SpanCostRecord` with `idempotency_key` set to
        `parent_idempotency_key`. The parent key is the F2 state-ledger join
        key per C-IS-05 — cross-span rollup at the audit-ledger join site
        consumes this field. Pure composition of
        `harness_od.idempotency_join_dedup.attach_idempotency_key_to_cost_record`."""
        return attach_idempotency_key_to_cost_record(
            span=span,
            parent_idempotency=parent_idempotency_key,
            cost_record=cost_record,
        )

    # --- Step 4: fan-out rollup at close (C-OD-14 §14.3) ------------------

    def rollup_fanout(
        self,
        parent_span_ref: SpanRef,
        sibling_costs: list[SpanTotalCost],
        pattern: FanOutPattern,
    ) -> FanOutRollupResult:
        """Step 4 — apply the C-OD-14 §14.3 fan-out aggregation at close.

        `parent_fanout_total_cost = Σ sibling.total_cost` regardless of
        pattern; `parent_fanout_total_latency = max` (PARALLEL) or `Σ`
        (SEQUENTIAL) of sibling `total_latency_ms`. Per-sibling cost is the
        post-sandbox-overhead `total_cost`, not the pre-sandbox `span_cost`
        (sandbox overhead propagates through the rollup). Pure composition
        of `harness_od.cost_attribution_sandbox_fanout.rollup_fanout_at_close`."""
        return rollup_fanout_at_close(
            parent_span_ref=parent_span_ref,
            sibling_costs=sibling_costs,
            pattern=pattern,
        )

    # --- Step 5: replay-aware dedup (C-OD-14 §14.5.1) ---------------------

    def dedupe_on_replay(
        self,
        span: SpanIngestionView,
        ledger_entry: F2StateLedgerEntry | None,
    ) -> DedupOutcome:
        """Step 5 — apply the C-OD-14 §14.5.1 replay-aware dedup decision.

        First ingestion (`ledger_entry is None`) → `RECORD_FIRST_INGESTION`.
        A `DETERMINISTIC_REPLAY` matching span against a matching ledger
        entry → `DROP_DETERMINISTIC_REPLAY_RE_READ` (no new cost accrual).
        `CHECKPOINT_RESUME` / `RECONCILER_ITERATION` / `WAL_CONSUME` →
        `RECORD_REPLAY_DERIVED` (re-emission expected; new replay-derived
        cost accrual). A cause_attribution mismatch → `ESCALATE_REPLAY_SEMANTIC_DIVERGENCE`
        per §14.5.3.

        Pure composition of `harness_od.idempotency_join_dedup.dedupe_on_replay`.
        Cross-axis composition note: U-RT-24's `RuntimeRetryBreaker.dedupe_decision`
        wraps the same OD function for the retry surface; both sites compose
        independently at distinct surfaces."""
        return dedupe_on_replay(span, ledger_entry)

    # --- Dashboard + alerting surface (C-OD-16) ---------------------------

    def dashboard_binding_for(self, cell_id: CellID) -> CellDashboardBinding:
        """Return the C-OD-16 §16.1 dashboard binding for a cell."""
        return PER_CELL_DASHBOARD_BINDINGS[cell_id]

    def compute_alerting_signal(
        self,
        observed_cost_rollup: float,
        threshold: AlertingThresholdComposition,
        workload_class: WorkloadClass,
    ) -> AlertingSignal:
        """Apply the C-OD-16 §16.2 unbiased-cost-scaled alerting comparison."""
        return compute_alerting_signal(
            observed_cost_rollup=observed_cost_rollup,
            threshold=threshold,
            workload_class=workload_class,
        )


@dataclass(frozen=True, slots=True)
class CostAttributionStage:
    """Frozen result of stage 4 OD cost-attribution chain materialization.

    The bootstrap orchestrator (U-RT-43) binds `chain` to
    `HarnessContext.cost_chain`. Mirrors the L5 / L6 stage shape.
    """

    chain: RuntimeCostAttributionChain


def materialize_cost_attribution_stage(
    config: RuntimeConfig,
) -> CostAttributionStage:
    """Build the stage 4 OD cost-attribution chain registry.

    Stage 4 composer. The chain is stateless — every step composes a pure
    OD function or static table — so the composer constructs a single
    `RuntimeCostAttributionChain` and wraps it. `config` is read for API
    consistency with the L5 / L6 composers; no field is consumed at HEAD."""
    _ = config
    return CostAttributionStage(chain=RuntimeCostAttributionChain())
