"""R-FS-1 arc CA — `RunResult.cost_attribution` run-result rollup (runtime spec v1.53 §9 C-RT-09).

Covers:
- `_rollup_cost_attribution` — the PER_PROVIDER_AND_MODEL rollup helper: empty/None → ();
  per-(provider, model) grouped sums; the single-axis sum-invariant.
- `_build_run_result` — reads the run-scoped `cost_records` and populates
  `cost_attribution` (empty records → ()).
- `_attribute_cost_best_effort` (the LLM per-dispatch cost wrapper) appends the
  returned `SpanCostRecord` into the run-scoped sink (wiring-by-execution).

The validator + webhook wrapper appends are proven by-execution in
`test_u_od_40_validator_webhook_integration.py`; the tool wrapper in
`test_lifecycle_runtime_tool_dispatcher_cost_attribution.py`.
"""

from __future__ import annotations

import pytest
from harness_cp.engine_namespace import ReplayDisposition
from harness_cp.workflow_driver_types import RunResult as _CpRunResult
from harness_cp.workflow_driver_types import RunStatus as _CpRunStatus
from harness_od.cross_family_rollup import CrossFamilyCostRollup, RollupAxis
from harness_od.idempotency_join_dedup import DispatchKind, SpanCostRecord
from harness_od.rate_table_v1 import RATE_TABLE_V1
from harness_runtime.api import (
    RunResult,
    _build_run_result,
    _rollup_cost_attribution,
    _rollup_cost_attribution_by_dispatch_kind,
)
from harness_runtime.lifecycle.cost_attribution import RuntimeCostAttributionChain
from harness_runtime.lifecycle.llm_dispatch import _attribute_cost_best_effort


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


def _record(
    *,
    provider: str,
    model: str,
    cost: float,
    span_id: str = "s",
    dispatch_kind: DispatchKind = DispatchKind.LLM,
) -> SpanCostRecord:
    """A synthetic `SpanCostRecord` carrying production-shaped discriminators
    (v1.30: `provider_discriminator=None` + a typed `dispatch_kind`) — the rollup
    math is independent of how the record was produced."""
    return SpanCostRecord(
        span_id=span_id,
        idempotency_key="idem",
        total_cost=cost,
        total_latency_ms=1,
        derived_keys=(),
        engine_replay_disposition=ReplayDisposition.NO_REPLAY,
        retry_attempt_number=None,
        retry_cause_attribution=None,
        is_replay_derived=False,
        provider_discriminator=None,
        dispatch_kind=dispatch_kind,
        gen_ai_provider_name=provider,
        gen_ai_request_model=model,
    )


def _success_cp_result() -> _CpRunResult:
    return _CpRunResult(
        workflow_id="wf-ca",
        run_id="run-ca-unit",
        status=_CpRunStatus.SUCCESS,
        terminal_step_index=None,
        partial_state=None,
        final_state={"k": "v"},
        fail_class=None,
    )


class _ShutdownReport:
    audit_ledger_head_hash = "deadbeef"


# ---------------------------------------------------------------------------
# _rollup_cost_attribution — the PER_PROVIDER_AND_MODEL run-result rollup
# ---------------------------------------------------------------------------


def test_rollup_empty_and_none_yield_empty_tuple() -> None:
    assert _rollup_cost_attribution([]) == ()
    assert _rollup_cost_attribution(None) == ()


def test_rollup_groups_by_provider_and_model_with_sum_invariant() -> None:
    records = [
        _record(provider="anthropic", model="claude-opus", cost=1.0),
        _record(provider="anthropic", model="claude-opus", cost=2.0),
        _record(provider="anthropic", model="claude-haiku", cost=0.5),
        _record(provider="openai", model="gpt-5", cost=4.0),
    ]
    rollups = _rollup_cost_attribution(records)

    # One CrossFamilyCostRollup per distinct (provider, model) group.
    assert len(rollups) == 3
    by_key = {r.group_key: r for r in rollups}
    assert by_key["anthropic::claude-opus"].total_cost == pytest.approx(3.0)
    assert by_key["anthropic::claude-opus"].span_count == 2
    assert by_key["anthropic::claude-haiku"].total_cost == pytest.approx(0.5)
    assert by_key["openai::gpt-5"].total_cost == pytest.approx(4.0)

    # Every entry self-identifies the single chosen axis.
    assert all(r.rollup_axis is RollupAxis.PER_PROVIDER_AND_MODEL for r in rollups)
    assert all(isinstance(r, CrossFamilyCostRollup) for r in rollups)

    # Single-axis sum-invariant: total over the rollup tuple == total over records
    # (a multi-axis flat tuple would double-count — runtime spec v1.53 §9 C-RT-09).
    assert sum(r.total_cost for r in rollups) == pytest.approx(sum(r.total_cost for r in records))


# ---------------------------------------------------------------------------
# _build_run_result — reads cost_records and populates cost_attribution
# ---------------------------------------------------------------------------


def test_build_run_result_populates_cost_attribution_from_records() -> None:
    records = [
        _record(provider="anthropic", model="claude-opus", cost=1.5),
        _record(provider="anthropic", model="claude-opus", cost=2.5),
    ]
    result = _build_run_result(_success_cp_result(), _ShutdownReport(), cost_records=records)

    assert isinstance(result, RunResult)
    assert result.cost_attribution == _rollup_cost_attribution(records)
    assert len(result.cost_attribution) == 1
    assert result.cost_attribution[0].group_key == "anthropic::claude-opus"
    assert result.cost_attribution[0].total_cost == pytest.approx(4.0)


def test_build_run_result_no_records_yields_empty_cost_attribution() -> None:
    # Backward-compatible default: a run with no cost-bearing dispatch (or a
    # caller that does not pass cost_records) yields the v1.4 empty-tuple shape.
    assert _build_run_result(_success_cp_result(), _ShutdownReport()).cost_attribution == ()
    assert (
        _build_run_result(_success_cp_result(), _ShutdownReport(), cost_records=[]).cost_attribution
        == ()
    )


# ---------------------------------------------------------------------------
# _rollup_cost_attribution_by_dispatch_kind — the PER_DISPATCH_KIND rollup
# (v1.57 / B-COST-DISCRIMINATOR-TAXONOMY)
# ---------------------------------------------------------------------------


def test_rollup_by_dispatch_kind_empty_and_none_yield_empty_tuple() -> None:
    assert _rollup_cost_attribution_by_dispatch_kind([]) == ()
    assert _rollup_cost_attribution_by_dispatch_kind(None) == ()


def test_rollup_by_dispatch_kind_groups_with_sum_invariant() -> None:
    records = [
        _record(
            provider="anthropic", model="claude-opus", cost=1.0, dispatch_kind=DispatchKind.LLM
        ),
        _record(
            provider="anthropic", model="claude-opus", cost=2.0, dispatch_kind=DispatchKind.LLM
        ),
        _record(provider="tool:echo", model="", cost=0.5, dispatch_kind=DispatchKind.TOOL),
        _record(provider="webhook:x", model="", cost=0.25, dispatch_kind=DispatchKind.WEBHOOK),
    ]
    rollups = _rollup_cost_attribution_by_dispatch_kind(records)
    by_key = {r.group_key: r for r in rollups}
    assert by_key["llm"].total_cost == pytest.approx(3.0)
    assert by_key["llm"].span_count == 2
    assert by_key["tool"].total_cost == pytest.approx(0.5)
    assert by_key["webhook"].total_cost == pytest.approx(0.25)
    assert all(r.rollup_axis is RollupAxis.PER_DISPATCH_KIND for r in rollups)
    # Single-axis sum-invariant: dispatch-kind partition recovers the run total.
    assert sum(r.total_cost for r in rollups) == pytest.approx(sum(r.total_cost for r in records))


def test_build_run_result_populates_both_cost_fields_orthogonally() -> None:
    # Same records, two orthogonal partitions: each field independently sums to
    # the run total (runtime spec v1.57 §9 C-RT-09).
    records = [
        _record(
            provider="anthropic", model="claude-opus", cost=3.0, dispatch_kind=DispatchKind.LLM
        ),
        _record(provider="tool:echo", model="", cost=1.0, dispatch_kind=DispatchKind.TOOL),
    ]
    result = _build_run_result(_success_cp_result(), _ShutdownReport(), cost_records=records)

    assert result.cost_attribution_by_dispatch_kind == _rollup_cost_attribution_by_dispatch_kind(
        records
    )
    by_kind = {r.group_key: r.total_cost for r in result.cost_attribution_by_dispatch_kind}
    assert by_kind == {"llm": pytest.approx(3.0), "tool": pytest.approx(1.0)}
    # Both fields partition the same $4.0 total, independently.
    assert sum(r.total_cost for r in result.cost_attribution) == pytest.approx(4.0)
    assert sum(r.total_cost for r in result.cost_attribution_by_dispatch_kind) == pytest.approx(4.0)


def test_build_run_result_no_records_yields_empty_dispatch_kind_field() -> None:
    # The new field defaults to () (minor bump — the v1.45 pause_snapshot precedent).
    assert (
        _build_run_result(_success_cp_result(), _ShutdownReport()).cost_attribution_by_dispatch_kind
        == ()
    )


# ---------------------------------------------------------------------------
# Wiring-by-execution — the LLM per-dispatch wrapper appends to the sink
# ---------------------------------------------------------------------------


def test_attribute_cost_best_effort_appends_returned_record_to_sink() -> None:
    """The real LLM cost wrapper, given a run-scoped sink, appends the
    SpanCostRecord it produced (the same record it emits as the OTel attribute)."""
    from opentelemetry.sdk.trace import TracerProvider

    sink: list[SpanCostRecord] = []
    tracer = TracerProvider().get_tracer("ca-test")
    with tracer.start_as_current_span("dispatch") as span:
        _attribute_cost_best_effort(
            span=span,
            cost_chain=RuntimeCostAttributionChain(),
            audit_writer=_RecordingAuditWriter(),
            rate_table=RATE_TABLE_V1,
            cost_record_sink=sink,
            provider_name="anthropic",
            model="claude-haiku-4-5",
            parent_idempotency_key="parent-idem-1",
            workflow_id="test-wf",
            parent_action_id="workflow:test-wf:step:0",
            input_tokens=1000,
            output_tokens=500,
            cache_creation=None,
            cache_read=None,
            tenant_id=None,
        )

    assert len(sink) == 1
    assert sink[0].gen_ai_provider_name == "anthropic"
    assert sink[0].gen_ai_request_model == "claude-haiku-4-5"
    # The rolled-up shape a run with this single dispatch would surface.
    rollups = _rollup_cost_attribution(sink)
    assert len(rollups) == 1
    assert rollups[0].group_key == "anthropic::claude-haiku-4-5"


def test_attribute_cost_best_effort_without_sink_is_noop() -> None:
    """Backward-compatible: no sink (unit-test / pre-CA construction) → no append,
    no error (the OTel attribute path still runs)."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("ca-test")
    with tracer.start_as_current_span("dispatch") as span:
        _attribute_cost_best_effort(
            span=span,
            cost_chain=RuntimeCostAttributionChain(),
            audit_writer=_RecordingAuditWriter(),
            rate_table=RATE_TABLE_V1,
            cost_record_sink=None,
            provider_name="anthropic",
            model="claude-haiku-4-5",
            parent_idempotency_key="parent-idem-1",
            workflow_id="test-wf",
            parent_action_id="workflow:test-wf:step:0",
            input_tokens=1000,
            output_tokens=500,
            cache_creation=None,
            cache_read=None,
            tenant_id=None,
        )
    # No exception ⟹ the None-sink guard holds.
