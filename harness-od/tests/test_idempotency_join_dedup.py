"""Tests for U-OD-20 — idempotency-key join + replay-aware dedup + per-attempt cost.

Each test maps to a U-OD-20 acceptance criterion (C-OD-14 §14.4 + §14.5.1-4).
"""

from __future__ import annotations

import pytest
from harness_cp.engine_namespace import ReplayDisposition
from harness_od.idempotency_join_dedup import (
    F2_12_CLOSURE_PATH,
    F2_12_NOTATION,
    ClosureStatus,
    DedupOutcome,
    DispatchKind,
    F2_12_DeferredSurface,
    F2StateLedgerEntry,
    FilingStatus,
    InvarianceCheckResult,
    SpanCostRecord,
    SpanIngestionView,
    attach_idempotency_key_to_cost_record,
    cause_attribution_invariance_check,
    dedupe_on_replay,
    per_attempt_cost_attribution_roll_up,
    propagate_to_subagent,
)
from harness_od.otel_genai_base import SpanRef
from opentelemetry.sdk.trace import TracerProvider


def _span() -> SpanRef:
    """An OTel-SDK span handle — the U-OD-04 `SpanRef` carrier substrate."""
    return TracerProvider().get_tracer("u-od-20-test").start_span("parent")


def _cost_record(
    *,
    disposition: ReplayDisposition = ReplayDisposition.NO_REPLAY,
    attempt: int | None = None,
    total_cost: float = 1.0,
    is_replay_derived: bool = False,
    provider_discriminator: str | None = "frontier_managed",
    dispatch_kind: DispatchKind = DispatchKind.LLM,
    gen_ai_provider_name: str = "anthropic",
    gen_ai_request_model: str = "claude-opus-4-7",
) -> SpanCostRecord:
    return SpanCostRecord(
        span_id="span-1",
        idempotency_key="idem-1",
        total_cost=total_cost,
        total_latency_ms=100,
        derived_keys=(),
        engine_replay_disposition=disposition,
        retry_attempt_number=attempt,
        retry_cause_attribution=None,
        is_replay_derived=is_replay_derived,
        provider_discriminator=provider_discriminator,
        dispatch_kind=dispatch_kind,
        gen_ai_provider_name=gen_ai_provider_name,
        gen_ai_request_model=gen_ai_request_model,
    )


def _ingest_view(
    *,
    disposition: ReplayDisposition,
    trace_id: str = "trace-1",
    span_id: str = "span-1",
    cause: str | None = "rate_limit",
    attempt: int | None = 1,
) -> SpanIngestionView:
    return SpanIngestionView(
        trace_id=trace_id,
        span_id=span_id,
        idempotency_key="idem-1",
        engine_replay_disposition=disposition,
        retry_attempt_number=attempt,
        retry_cause_attribution=cause,
    )


def _ledger(cause: str | None = "rate_limit") -> F2StateLedgerEntry:
    return F2StateLedgerEntry(
        idempotency_key="idem-1",
        original_trace_id="trace-1",
        original_span_id="span-1",
        cause_attribution=cause,
    )


# --- §14.4 idempotency-key join ---------------------------------------------


def test_span_cost_record_thirteen_fields() -> None:
    """Acceptance #1 (v1.30) — `SpanCostRecord` declares exactly 13 fields.

    12 at v2.8 (D-5) + `dispatch_kind` at v1.30 (B-COST-DISCRIMINATOR-TAXONOMY).
    """
    assert len(SpanCostRecord.model_fields) == 13


def test_span_cost_record_provider_discriminator_field() -> None:
    """Acceptance #1 (v2.8 D-5) — `provider_discriminator` rollup-key field present."""
    assert "provider_discriminator" in SpanCostRecord.model_fields


def test_span_cost_record_dispatch_kind_field() -> None:
    """Acceptance #1 (v1.30) — `dispatch_kind` is a typed `DispatchKind` field.

    The PER_DISPATCH_KIND rollup key. Enum-typed directly (carrier-homed, no
    cycle) — unlike the `str`-typed `provider_discriminator`.
    """
    assert "dispatch_kind" in SpanCostRecord.model_fields
    assert SpanCostRecord.model_fields["dispatch_kind"].annotation is DispatchKind


def test_span_cost_record_gen_ai_provider_name_field() -> None:
    """Acceptance #1 (v2.8 D-5) — `gen_ai_provider_name` rollup-key field present."""
    assert "gen_ai_provider_name" in SpanCostRecord.model_fields


def test_span_cost_record_gen_ai_request_model_field() -> None:
    """Acceptance #1 (v2.8 D-5) — `gen_ai_request_model` rollup-key field present."""
    assert "gen_ai_request_model" in SpanCostRecord.model_fields


def test_span_cost_record_new_fields_string_typed_no_cross_unit_dependency() -> None:
    """Acceptance #1 (v2.8 D-5 / §0.3 / v1.30) — the family-tag rollup-key fields are `str`.

    String typing is deliberate: typing `provider_discriminator` as U-OD-21's
    `CrossFamilyTag` enum would create a U-OD-20 → U-OD-21 carrier cycle.
    `gen_ai_*` stay required `str`; `provider_discriminator` is `str | None` at
    v1.30 (per-dispatch-optional — `None` until §15.3 chain composition).
    """
    for field in ("gen_ai_provider_name", "gen_ai_request_model"):
        assert SpanCostRecord.model_fields[field].annotation is str
    assert SpanCostRecord.model_fields["provider_discriminator"].annotation == (str | None)


def test_span_cost_record_replay_orthogonality_fields_present() -> None:
    """Acceptance #1 — the 4 v2.2 replay-orthogonality fields are present."""
    for field in (
        "engine_replay_disposition",
        "retry_attempt_number",
        "retry_cause_attribution",
        "is_replay_derived",
    ):
        assert field in SpanCostRecord.model_fields


def test_attach_idempotency_key_sets_parent_value() -> None:
    """Acceptance #2 — `attach_idempotency_key_to_cost_record` sets the parent key."""
    record = _cost_record()
    out = attach_idempotency_key_to_cost_record(_span(), "parent-idem-9", record)
    assert out.idempotency_key == "parent-idem-9"


def test_span_param_resolves_to_u_od_04_carrier() -> None:
    """Acceptance #12 (v2.6) — `span` resolves to the U-OD-04 `SpanRef` alias."""
    span = _span()
    assert isinstance(span, SpanRef.__value__)
    # the function accepts the U-OD-04-carried handle without re-materializing it
    assert attach_idempotency_key_to_cost_record(span, "k", _cost_record()).idempotency_key == "k"


def test_propagate_to_subagent_derives_namespaced_key() -> None:
    """Acceptance #4 — `propagate_to_subagent` derives a sub-agent key (C-AS-15 §15.6)."""
    derived = propagate_to_subagent("parent-idem")
    assert derived != "parent-idem"
    assert "parent-idem" in derived


# --- §14.5.1 trace-ingestion dedup algorithm --------------------------------


def test_dedupe_first_ingestion_records() -> None:
    """Acceptance #3 — no ledger entry → first ingestion is RECORDed."""
    view = _ingest_view(disposition=ReplayDisposition.NO_REPLAY)
    assert dedupe_on_replay(view, None) is DedupOutcome.RECORD_FIRST_INGESTION


def test_dedupe_on_replay_deterministic_replay_drops() -> None:
    """Acceptance #3/#12 — `deterministic_replay` matching ledger entry → DROP."""
    view = _ingest_view(disposition=ReplayDisposition.DETERMINISTIC_REPLAY)
    out = dedupe_on_replay(view, _ledger())
    assert out is DedupOutcome.DROP_DETERMINISTIC_REPLAY_RE_READ


def test_dedupe_deterministic_replay_topology_mismatch_escalates() -> None:
    """Acceptance #3/#13 — `deterministic_replay` with trace/span mismatch escalates."""
    view = _ingest_view(disposition=ReplayDisposition.DETERMINISTIC_REPLAY, trace_id="other-trace")
    out = dedupe_on_replay(view, _ledger())
    assert out is DedupOutcome.ESCALATE_REPLAY_SEMANTIC_DIVERGENCE


def test_dedupe_deterministic_replay_cause_mismatch_escalates() -> None:
    """Acceptance #13 — `deterministic_replay` cause_attribution mismatch escalates."""
    view = _ingest_view(disposition=ReplayDisposition.DETERMINISTIC_REPLAY, cause="different_cause")
    out = dedupe_on_replay(view, _ledger(cause="rate_limit"))
    assert out is DedupOutcome.ESCALATE_REPLAY_SEMANTIC_DIVERGENCE


@pytest.mark.parametrize(
    "disposition",
    [
        ReplayDisposition.CHECKPOINT_RESUME,
        ReplayDisposition.RECONCILER_ITERATION,
        ReplayDisposition.WAL_CONSUME,
    ],
)
def test_dedupe_replay_derived_dispositions_record(
    disposition: ReplayDisposition,
) -> None:
    """Acceptance #3 — checkpoint/reconciler/wal re-emission → RECORD replay-derived."""
    view = _ingest_view(disposition=disposition)
    assert dedupe_on_replay(view, _ledger()) is DedupOutcome.RECORD_REPLAY_DERIVED


def test_dedupe_on_replay_no_replay_errors_on_re_ingestion() -> None:
    """Acceptance #3/#12 — `no_replay` with an existing ledger entry → ERROR."""
    view = _ingest_view(disposition=ReplayDisposition.NO_REPLAY)
    out = dedupe_on_replay(view, _ledger())
    assert out is DedupOutcome.ERROR_UNEXPECTED_RE_INGESTION_FOR_NO_REPLAY


def test_dedup_outcome_matrix_covers_all_five_dispositions() -> None:
    """Acceptance #12 — every `ReplayDisposition` resolves to a dedup outcome."""
    for disposition in ReplayDisposition:
        view = _ingest_view(disposition=disposition)
        # with a ledger entry (re-ingestion path)
        assert isinstance(dedupe_on_replay(view, _ledger()), DedupOutcome)
        # without a ledger entry (first-ingestion path)
        assert dedupe_on_replay(view, None) is DedupOutcome.RECORD_FIRST_INGESTION


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("disposition", list(ReplayDisposition))
@pytest.mark.parametrize("ledger_present", [True, False])
def test_dedup_outcome_matrix_section_14_5_2_cells(
    attempt: int,
    disposition: ReplayDisposition,
    ledger_present: bool,
) -> None:
    """Acceptance #12 — the §14.5.2 dedup outcome matrix over attempt x disposition x ledger.

    Per §14.5.2, ledger entries are per-attempt: attempt N joins via the
    parent `idempotency_key` but is a DISTINCT entry. `dedupe_on_replay`
    discriminates on the matched-entry presence + disposition; the
    `retry.attempt_number` selects WHICH per-attempt entry is matched, not the
    outcome branch — so the outcome is invariant in `attempt` for a fixed
    (disposition, ledger_present) cell. This test pins all 5 x 2 x 2 cells.
    """
    view = _ingest_view(disposition=disposition, attempt=attempt)
    ledger = _ledger() if ledger_present else None
    outcome = dedupe_on_replay(view, ledger)
    if not ledger_present:
        assert outcome is DedupOutcome.RECORD_FIRST_INGESTION
    elif disposition is ReplayDisposition.DETERMINISTIC_REPLAY:
        assert outcome is DedupOutcome.DROP_DETERMINISTIC_REPLAY_RE_READ
    elif disposition is ReplayDisposition.NO_REPLAY:
        assert outcome is DedupOutcome.ERROR_UNEXPECTED_RE_INGESTION_FOR_NO_REPLAY
    else:
        assert outcome is DedupOutcome.RECORD_REPLAY_DERIVED


# --- §14.5.3 cause_attribution invariance check -----------------------------


def test_invariance_check_pass_on_match() -> None:
    """Acceptance #13 — `deterministic_replay` cause match → PASS."""
    view = _ingest_view(disposition=ReplayDisposition.DETERMINISTIC_REPLAY, cause="rate_limit")
    result = cause_attribution_invariance_check(view, _ledger(cause="rate_limit"))
    assert result is InvarianceCheckResult.PASS


def test_invariance_check_escalate_on_mismatch() -> None:
    """Acceptance #13 — `deterministic_replay` cause mismatch → ESCALATE."""
    view = _ingest_view(disposition=ReplayDisposition.DETERMINISTIC_REPLAY, cause="cause_a")
    result = cause_attribution_invariance_check(view, _ledger(cause="cause_b"))
    assert result is InvarianceCheckResult.ESCALATE_REPLAY_SEMANTIC_DIVERGENCE


@pytest.mark.parametrize(
    "disposition",
    [
        ReplayDisposition.CHECKPOINT_RESUME,
        ReplayDisposition.NO_REPLAY,
        ReplayDisposition.RECONCILER_ITERATION,
        ReplayDisposition.WAL_CONSUME,
    ],
)
def test_invariance_check_not_applicable_for_non_deterministic_replay(
    disposition: ReplayDisposition,
) -> None:
    """Acceptance #13 — the invariance check is NOT_APPLICABLE off `deterministic_replay`."""
    view = _ingest_view(disposition=disposition)
    result = cause_attribution_invariance_check(view, _ledger())
    assert result is InvarianceCheckResult.NOT_APPLICABLE


def test_replay_semantic_divergence_event_attributes() -> None:
    """Acceptance #13 — the ESCALATE event carries the fixed §14.5.3 validator-fail attrs."""
    from harness_od.idempotency_join_dedup import ReplaySemanticDivergenceError

    event = ReplaySemanticDivergenceError()
    assert event.validator_fail_class == "terminal-fail-exit"
    assert event.validator_fail_cause_attribution == "replay_semantic_divergence"
    assert event.validator_fail_permanence == "permanent"
    assert event.always_sampled is True


# --- §14.5.4 per-attempt cost-attribution discipline ------------------------


def test_per_attempt_cost_roll_up_sum_invariant() -> None:
    """Acceptance #14 — total cost = Σ per-attempt costs."""
    records = [
        _cost_record(disposition=ReplayDisposition.NO_REPLAY, attempt=1, total_cost=2.0),
        _cost_record(disposition=ReplayDisposition.CHECKPOINT_RESUME, attempt=2, total_cost=3.0),
        _cost_record(disposition=ReplayDisposition.CHECKPOINT_RESUME, attempt=3, total_cost=5.0),
    ]
    rolled = per_attempt_cost_attribution_roll_up("op-1", records)
    assert rolled.total_cost == 10.0
    assert rolled.per_attempt_costs == {1: 2.0, 2: 3.0, 3: 5.0}
    assert rolled.replay_re_reads_excluded == 0


def test_per_attempt_cost_deterministic_replay_excluded() -> None:
    """Acceptance #14 — `deterministic_replay` re-reads contribute zero to the sum."""
    records = [
        _cost_record(disposition=ReplayDisposition.NO_REPLAY, attempt=1, total_cost=4.0),
        _cost_record(
            disposition=ReplayDisposition.DETERMINISTIC_REPLAY,
            attempt=1,
            total_cost=4.0,
            is_replay_derived=True,
        ),
    ]
    rolled = per_attempt_cost_attribution_roll_up("op-1", records)
    assert rolled.total_cost == 4.0
    assert rolled.replay_re_reads_excluded == 1


def test_per_attempt_cost_roll_up_empty() -> None:
    """Acceptance #14 — an empty attempt list rolls up to zero cost."""
    rolled = per_attempt_cost_attribution_roll_up("op-1", [])
    assert rolled.total_cost == 0.0
    assert rolled.per_attempt_costs == {}


# --- F2-12 ✅ CLOSED affected-contract notation ------------------------------


def test_f2_12_deferred_surface_cardinality_three() -> None:
    """Acceptance #5 — `F2_12_DeferredSurface` enumerates exactly 3 surfaces."""
    assert len(list(F2_12_DeferredSurface)) == 3


def test_f2_12_notation_contract_id_c_od_14() -> None:
    """Acceptance #6 — the notation's `contract_id` is `C-OD-14`."""
    assert F2_12_NOTATION.contract_id == "C-OD-14"
    assert F2_12_NOTATION.active_engagement_site == "C-OD-14 §14.5"


def test_f2_12_notation_closed_engagement_site() -> None:
    """Acceptance #6 — the v1.3 closed engagement site is recorded."""
    assert F2_12_NOTATION.closed_engagement_site == (
        "C-OD-14 §14.5 (closed) + §14.5.1 + §14.5.2 + §14.5.3 + §14.5.4"
    )


def test_f2_12_closure_path_cardinality_nine() -> None:
    """Acceptance #8 — `F2_12_CLOSURE_PATH` declares exactly 9 revision steps."""
    assert len(F2_12_CLOSURE_PATH) == 9
    assert len(F2_12_NOTATION.closure_path) == 9


def test_f2_12_closure_path_all_steps_filed() -> None:
    """Acceptance #8 — all 9 cascade steps are FILED (Close pending)."""
    assert all(step.filing_status is FilingStatus.FILED for step in F2_12_CLOSURE_PATH)


def test_f2_12_closure_path_step_decomposition() -> None:
    """Acceptance #8 — Steps 2/5/6 each decompose into a/b sub-steps."""
    labels = {step.step_label for step in F2_12_CLOSURE_PATH}
    for label in ("Step 2a", "Step 2b", "Step 5a", "Step 5b", "Step 6a", "Step 6b"):
        assert label in labels


def test_f2_12_closure_pending_at_v2_2_false() -> None:
    """Acceptance #9 — `closure_pending_at_v2_2` is `False`."""
    assert F2_12_NOTATION.closure_pending_at_v2_2 is False
    assert F2_12_NOTATION.closure_pending_at_v1 is True


def test_f2_12_notation_closure_status_closed_at_cascade_step_6b() -> None:
    """Acceptance #6/#10 — the closure status is `CLOSED_AT_CASCADE_STEP_6B`."""
    assert F2_12_NOTATION.closure_status is ClosureStatus.CLOSED_AT_CASCADE_STEP_6B


def test_f2_12_closure_status_per_surface_covers_three_surfaces() -> None:
    """Acceptance #5 — every deferred surface maps to a closing cascade step."""
    assert set(F2_12_NOTATION.closure_status_per_surface) == set(F2_12_DeferredSurface)


def test_f2_12_v1_commitment_level_byte_exact() -> None:
    """Acceptance #7 — the v1 / v2.2 commitment levels are recorded."""
    assert "idempotency-key join" in F2_12_NOTATION.v1_commitment_level
    assert "dedup algorithm" in F2_12_NOTATION.v2_2_commitment_level
