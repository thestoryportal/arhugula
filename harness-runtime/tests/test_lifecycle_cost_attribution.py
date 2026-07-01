"""U-RT-31 — cost-attribution 5-step chain wiring tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L6 U-RT-31:
  #1 end-to-end attribution observable on a single fake span.
     -> test_end_to_end_chain_on_single_fake_span
     -> test_step_1_per_attempt_cost_applies_formula_verbatim
     -> test_step_2_compose_total_cost_adds_sandbox_overhead
     -> test_step_2_compose_total_cost_no_sandbox_passthrough
     -> test_step_3_attach_idempotency_key_overrides_record_key
     -> test_step_5_dedupe_on_replay_first_ingestion
     -> test_step_5_dedupe_on_replay_drop_on_deterministic_match
  #2 sandbox fanout splits attribution correctly.
     -> test_step_4_rollup_fanout_parallel_max_latency
     -> test_step_4_rollup_fanout_sequential_sum_latency
     -> test_step_4_rollup_fanout_total_cost_propagates_sandbox_overhead
     -> test_step_4_rollup_fanout_empty_siblings

Plus canonical-table + composer + dashboard surface tests:
  -> test_dashboard_bindings_cover_8_active_cells
  -> test_dashboard_binding_for_cell_returns_canonical_form
  -> test_eval_primitives_table_has_5_entries
  -> test_eval_emission_contract_requires_child_span
  -> test_compute_alerting_signal_above_threshold
  -> test_materialize_returns_stage_with_chain
  -> test_cost_attribution_stage_is_frozen
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from harness_core import DeploymentSurface, PersonaTier, WorkloadClass
from harness_cp.engine_namespace import ReplayDisposition
from harness_cp.topology_pattern import TopologyPattern
from harness_od.cost_attribution_dashboard_binding import (
    AlertingSignal,
    AlertingThresholdComposition,
    DashboardBindingForm,
)
from harness_od.cost_attribution_sandbox_fanout import (
    FanOutPattern,
    SandboxOverhead,
    SpanTotalCost,
)
from harness_od.cost_formula import (
    PriceRateEntry,
    PriceRateKey,
    SpanCostInputs,
)
from harness_od.idempotency_join_dedup import (
    DedupOutcome,
    DispatchKind,
    F2StateLedgerEntry,
    SpanCostRecord,
    SpanIngestionView,
)
from harness_od.observability_matrix import CellID
from harness_od.operator_burden_eval_primitives import (
    EVAL_PRIMITIVE_DECLARATIONS,
)
from harness_od.otel_genai_base import SpanRef
from harness_runtime.lifecycle.cost_attribution import (
    CostAttributionStage,
    RuntimeCostAttributionChain,
    materialize_cost_attribution_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from opentelemetry.sdk.trace import TracerProvider

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _chain(tmp_path: Path) -> RuntimeCostAttributionChain:
    return materialize_cost_attribution_stage(_config(tmp_path)).chain


def _span() -> SpanRef:
    """A live OTel-SDK span handle for `SpanRef`-shaped tests."""
    return TracerProvider().get_tracer("u-rt-31-test").start_span("parent")


def _rate_key() -> PriceRateKey:
    return PriceRateKey(
        provider_name="anthropic",
        model="claude-opus-4-7",
        tokenizer_version="2024-01",
    )


def _rates() -> PriceRateEntry:
    return PriceRateEntry(
        key=_rate_key(),
        base_input=0.000015,
        base_output=0.000075,
    )


def _inputs() -> SpanCostInputs:
    return SpanCostInputs(
        input_tokens=1000,
        cache_creation=100,
        cache_read=400,
        output_tokens=500,
        rate_key=_rate_key(),
    )


def _cost_record(
    *,
    span_id: str = "span-0",
    idempotency_key: str = "k-0",
    total_cost: float = 0.0,
) -> SpanCostRecord:
    return SpanCostRecord(
        span_id=span_id,
        idempotency_key=idempotency_key,
        total_cost=total_cost,
        total_latency_ms=100,
        derived_keys=(),
        engine_replay_disposition=ReplayDisposition.NO_REPLAY,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator="anthropic-family",
        dispatch_kind=DispatchKind.LLM,
        gen_ai_provider_name="anthropic",
        gen_ai_request_model="claude-opus-4-7",
    )


def _sibling(
    *,
    span_cost: float = 0.10,
    duration_ms: int = 100,
    sandbox: SandboxOverhead | None = None,
) -> SpanTotalCost:
    if sandbox is None:
        return SpanTotalCost(
            span_cost=span_cost,
            sandbox_overhead=None,
            total_cost=span_cost,
            total_latency_ms=duration_ms,
        )
    return SpanTotalCost(
        span_cost=span_cost,
        sandbox_overhead=sandbox,
        total_cost=span_cost + sandbox.tier_overhead_usd,
        total_latency_ms=duration_ms + sandbox.tier_overhead_ms,
    )


# ---------------------------------------------------------------------------
# AC #1 — end-to-end attribution observable on a single fake span.
# ---------------------------------------------------------------------------


def test_end_to_end_chain_on_single_fake_span(tmp_path: Path) -> None:
    """Drive all 5 steps end-to-end against a single fake span.

    Step 1: compute per-attempt cost from token inputs.
    Step 2: compose with sandbox overhead → total cost.
    Step 3: attach idempotency key to cost record.
    Step 4: roll up at fan-out close (single-sibling rollup is a degenerate case).
    Step 5: dedupe on replay → first ingestion records.
    """
    chain = _chain(tmp_path)
    # Step 1.
    span_cost = chain.compute_per_attempt_cost(_inputs(), _rates())
    assert span_cost > 0.0
    # Step 2.
    overhead = SandboxOverhead(tier_overhead_usd=0.001, tier_overhead_ms=50)
    total = chain.compose_total_cost(
        span_cost=span_cost, span_duration_ms=200, sandbox_overhead=overhead
    )
    assert math.isclose(total.total_cost, span_cost + 0.001)
    assert total.total_latency_ms == 250
    # Step 3.
    record = _cost_record(idempotency_key="placeholder")
    joined = chain.attach_idempotency_key(
        span=_span(),
        parent_idempotency_key="parent-key-1",
        cost_record=record,
    )
    assert joined.idempotency_key == "parent-key-1"
    # Step 4 (degenerate single-sibling rollup).
    rollup = chain.rollup_fanout(
        parent_span_ref=_span(),
        sibling_costs=[total],
        pattern=FanOutPattern.PARALLEL,
    )
    assert rollup.sibling_count == 1
    assert math.isclose(rollup.parent_fanout_total_cost, total.total_cost)
    # Step 5.
    span_view = SpanIngestionView(
        trace_id="trace-1",
        span_id="span-1",
        idempotency_key="parent-key-1",
        engine_replay_disposition=ReplayDisposition.NO_REPLAY,
        retry_attempt_number=None,
        retry_cause_attribution=None,
    )
    outcome = chain.dedupe_on_replay(span_view, None)
    assert outcome is DedupOutcome.RECORD_FIRST_INGESTION


def test_step_1_per_attempt_cost_applies_formula_verbatim(tmp_path: Path) -> None:
    """C-OD-14 §14.1: `uncached*base_input + cache_creation*1.25*base_input +
    cache_read*0.10*base_input + output*base_output`."""
    chain = _chain(tmp_path)
    # uncached = 1000 - 400 - 100 = 500.
    # cost = 500 * 0.000015 + 100 * 1.25 * 0.000015 + 400 * 0.10 * 0.000015 + 500 * 0.000075
    #      = 0.0075 + 0.001875 + 0.0006 + 0.0375
    #      = 0.047475
    cost = chain.compute_per_attempt_cost(_inputs(), _rates())
    assert math.isclose(cost, 0.047475)


def test_step_2_compose_total_cost_adds_sandbox_overhead(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    overhead = SandboxOverhead(tier_overhead_usd=0.002, tier_overhead_ms=75)
    total = chain.compose_total_cost(
        span_cost=0.05, span_duration_ms=300, sandbox_overhead=overhead
    )
    assert math.isclose(total.total_cost, 0.052)
    assert total.total_latency_ms == 375


def test_step_2_compose_total_cost_no_sandbox_passthrough(tmp_path: Path) -> None:
    """Non-sandbox spans pass `sandbox_overhead=None` → totals equal inputs."""
    chain = _chain(tmp_path)
    total = chain.compose_total_cost(span_cost=0.05, span_duration_ms=300, sandbox_overhead=None)
    assert math.isclose(total.total_cost, 0.05)
    assert total.total_latency_ms == 300
    assert total.sandbox_overhead is None


def test_step_3_attach_idempotency_key_overrides_record_key(tmp_path: Path) -> None:
    """The join replaces the record's `idempotency_key` with the parent's."""
    chain = _chain(tmp_path)
    record = _cost_record(idempotency_key="placeholder")
    joined = chain.attach_idempotency_key(
        span=_span(),
        parent_idempotency_key="parent-canonical",
        cost_record=record,
    )
    assert joined.idempotency_key == "parent-canonical"
    # Original record unchanged (frozen).
    assert record.idempotency_key == "placeholder"


def test_step_5_dedupe_on_replay_first_ingestion(tmp_path: Path) -> None:
    """First ingestion (`ledger_entry=None`) → `RECORD_FIRST_INGESTION`."""
    chain = _chain(tmp_path)
    span = SpanIngestionView(
        trace_id="t",
        span_id="s",
        idempotency_key="k",
        engine_replay_disposition=ReplayDisposition.NO_REPLAY,
        retry_attempt_number=None,
        retry_cause_attribution=None,
    )
    assert chain.dedupe_on_replay(span, None) is DedupOutcome.RECORD_FIRST_INGESTION


def test_step_5_dedupe_on_replay_drop_on_deterministic_match(tmp_path: Path) -> None:
    """`DETERMINISTIC_REPLAY` with matching trace/span IDs + cause →
    `DROP_DETERMINISTIC_REPLAY_RE_READ` (no new cost accrual)."""
    chain = _chain(tmp_path)
    span = SpanIngestionView(
        trace_id="t",
        span_id="s",
        idempotency_key="k",
        engine_replay_disposition=ReplayDisposition.DETERMINISTIC_REPLAY,
        retry_attempt_number=1,
        retry_cause_attribution="transient",
    )
    entry = F2StateLedgerEntry(
        idempotency_key="k",
        original_trace_id="t",
        original_span_id="s",
        cause_attribution="transient",
    )
    assert chain.dedupe_on_replay(span, entry) is DedupOutcome.DROP_DETERMINISTIC_REPLAY_RE_READ


# ---------------------------------------------------------------------------
# AC #2 — sandbox fanout splits attribution correctly.
# ---------------------------------------------------------------------------


def test_step_4_rollup_fanout_parallel_max_latency(tmp_path: Path) -> None:
    """PARALLEL fan-out: `total_latency = max(sibling.total_latency_ms)`."""
    chain = _chain(tmp_path)
    siblings = [
        _sibling(span_cost=0.10, duration_ms=100),
        _sibling(span_cost=0.20, duration_ms=300),  # slowest
        _sibling(span_cost=0.05, duration_ms=200),
    ]
    rollup = chain.rollup_fanout(
        parent_span_ref=_span(),
        sibling_costs=siblings,
        pattern=FanOutPattern.PARALLEL,
    )
    assert math.isclose(rollup.parent_fanout_total_cost, 0.35)  # Σ
    assert rollup.parent_fanout_total_latency == 300  # max
    assert rollup.sibling_count == 3


def test_step_4_rollup_fanout_sequential_sum_latency(tmp_path: Path) -> None:
    """SEQUENTIAL fan-out: `total_latency = Σ sibling.total_latency_ms`."""
    chain = _chain(tmp_path)
    siblings = [
        _sibling(span_cost=0.10, duration_ms=100),
        _sibling(span_cost=0.20, duration_ms=300),
        _sibling(span_cost=0.05, duration_ms=200),
    ]
    rollup = chain.rollup_fanout(
        parent_span_ref=_span(),
        sibling_costs=siblings,
        pattern=FanOutPattern.SEQUENTIAL,
    )
    assert math.isclose(rollup.parent_fanout_total_cost, 0.35)
    assert rollup.parent_fanout_total_latency == 600  # 100+300+200


def test_step_4_rollup_fanout_total_cost_propagates_sandbox_overhead(
    tmp_path: Path,
) -> None:
    """Per-sibling cost is the POST-sandbox-overhead `total_cost`, not the
    pre-sandbox `span_cost` — sandbox overhead propagates through the rollup."""
    chain = _chain(tmp_path)
    overhead = SandboxOverhead(tier_overhead_usd=0.005, tier_overhead_ms=20)
    siblings = [
        _sibling(span_cost=0.10, duration_ms=100, sandbox=overhead),
        _sibling(span_cost=0.20, duration_ms=200, sandbox=overhead),
    ]
    rollup = chain.rollup_fanout(
        parent_span_ref=_span(),
        sibling_costs=siblings,
        pattern=FanOutPattern.PARALLEL,
    )
    # Each sibling's total_cost = span_cost + 0.005; sum = 0.10+0.005 + 0.20+0.005.
    assert math.isclose(rollup.parent_fanout_total_cost, 0.31)


def test_step_4_rollup_fanout_empty_siblings(tmp_path: Path) -> None:
    """Degenerate empty-siblings rollup: cost = 0, latency = 0, count = 0."""
    chain = _chain(tmp_path)
    rollup = chain.rollup_fanout(
        parent_span_ref=_span(),
        sibling_costs=[],
        pattern=FanOutPattern.PARALLEL,
    )
    assert rollup.parent_fanout_total_cost == 0.0
    assert rollup.parent_fanout_total_latency == 0
    assert rollup.sibling_count == 0


# ---------------------------------------------------------------------------
# Canonical-table + composer surface.
# ---------------------------------------------------------------------------


def test_dashboard_bindings_cover_8_active_cells(tmp_path: Path) -> None:
    """`dashboard_bindings` exposes the C-OD-16 §16.1 8-cell binding table."""
    chain = _chain(tmp_path)
    assert len(chain.dashboard_bindings) == 8


def test_dashboard_binding_for_cell_returns_canonical_form(tmp_path: Path) -> None:
    """solo-developer x local-development cell -> TUI ring-buffer query form."""
    chain = _chain(tmp_path)
    cell = CellID(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    binding = chain.dashboard_binding_for(cell)
    assert binding.binding_form is DashboardBindingForm.TUI_TRACE_BROWSER_RING_BUFFER_QUERY


def test_eval_primitives_table_has_5_entries(tmp_path: Path) -> None:
    """C-OD-17 §17.1 commits exactly 5 operator-burden eval primitives."""
    chain = _chain(tmp_path)
    assert chain.eval_primitives is EVAL_PRIMITIVE_DECLARATIONS
    assert len(chain.eval_primitives) == 5


def test_eval_emission_contract_requires_child_span(tmp_path: Path) -> None:
    """C-OD-17 §17.2 commits separate-child-span emission for eval scores."""
    chain = _chain(tmp_path)
    contract = chain.eval_emission_contract
    assert contract.child_span_emission_required is True
    assert contract.applies_at_all_cells is True


def test_compute_alerting_signal_above_threshold(tmp_path: Path) -> None:
    """`compute_alerting_signal` flags ABOVE_THRESHOLD when scaled rollup
    exceeds the per-class ceiling (C-OD-16 §16.2)."""
    chain = _chain(tmp_path)
    threshold = AlertingThresholdComposition(
        cell_id=CellID(
            persona_tier=PersonaTier.SOLO_DEVELOPER,
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        ),
        per_class_cost_ceiling={wc: 1.0 for wc in WorkloadClass},
        base_rate=1.0,
        scaled_estimate_factor=1.0,
    )
    signal = chain.compute_alerting_signal(
        observed_cost_rollup=2.0,
        threshold=threshold,
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
    )
    assert signal is AlertingSignal.ABOVE_THRESHOLD


def test_materialize_returns_stage_with_chain(tmp_path: Path) -> None:
    stage = materialize_cost_attribution_stage(_config(tmp_path))
    assert isinstance(stage, CostAttributionStage)
    assert isinstance(stage.chain, RuntimeCostAttributionChain)


def test_cost_attribution_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_cost_attribution_stage(_config(tmp_path))
    with pytest.raises(FrozenInstanceError):
        stage.chain = RuntimeCostAttributionChain()  # type: ignore[misc]
