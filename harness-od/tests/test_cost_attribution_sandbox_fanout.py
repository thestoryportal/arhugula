"""Tests for U-OD-19 — sandbox-tier overhead + fan-out rollup (C-OD-14 §14.2/§14.3).

Test set per the U-OD-19 §3.5.2 `Tests:` field — covers acceptance #1-#8.
"""

from __future__ import annotations

from harness_od.cost_attribution_sandbox_fanout import (
    FanOutPattern,
    FanOutRollupResult,
    SandboxOverhead,
    SpanTotalCost,
    compose_span_total_cost,
    rollup_fanout_at_close,
)
from harness_od.otel_genai_base import SpanRef
from opentelemetry.sdk.trace import TracerProvider


def _span() -> SpanRef:
    """An OTel-SDK span handle — the U-OD-04 `SpanRef` carrier substrate."""
    return TracerProvider().get_tracer("u-od-19-test").start_span("parent")


# --- acceptance #1 / #2 — compose_span_total_cost --------------------------


def test_compose_no_sandbox() -> None:
    """Acceptance #2 — non-sandbox-bounded spans carry `sandbox_overhead=None`;
    `total_cost == span_cost`, `total_latency_ms == span_duration_ms`."""
    result = compose_span_total_cost(span_cost=2.50, span_duration_ms=400, sandbox_overhead=None)
    assert result.sandbox_overhead is None
    assert result.total_cost == 2.50
    assert result.total_latency_ms == 400


def test_compose_with_sandbox_overhead_additive() -> None:
    """Acceptance #1 — §14.2 verbatim: `total_cost = span_cost +
    sandbox_overhead.tier_overhead_usd`."""
    overhead = SandboxOverhead(tier_overhead_usd=0.75, tier_overhead_ms=120)
    result = compose_span_total_cost(
        span_cost=2.00, span_duration_ms=300, sandbox_overhead=overhead
    )
    assert result.total_cost == 2.75


def test_compose_latency_additive() -> None:
    """Acceptance #1 — §14.2 verbatim: `total_latency_ms = span_duration_ms +
    sandbox_overhead.tier_overhead_ms`."""
    overhead = SandboxOverhead(tier_overhead_usd=0.10, tier_overhead_ms=250)
    result = compose_span_total_cost(
        span_cost=1.00, span_duration_ms=300, sandbox_overhead=overhead
    )
    assert result.total_latency_ms == 550


def test_compose_zero_overhead_explicit() -> None:
    """Acceptance #1 — an explicit zero-overhead `SandboxOverhead` (distinct
    from `None`) leaves `total_*` equal to the inputs."""
    overhead = SandboxOverhead(tier_overhead_usd=0.0, tier_overhead_ms=0)
    result = compose_span_total_cost(
        span_cost=3.00, span_duration_ms=500, sandbox_overhead=overhead
    )
    assert result.total_cost == 3.00
    assert result.total_latency_ms == 500
    assert result.sandbox_overhead is not None


# --- acceptance #4 / #5 — rollup_fanout_at_close ----------------------------


def _sibling(total_cost: float, total_latency_ms: int) -> SpanTotalCost:
    return SpanTotalCost(
        span_cost=total_cost,
        sandbox_overhead=None,
        total_cost=total_cost,
        total_latency_ms=total_latency_ms,
    )


def test_rollup_total_cost_sum_of_siblings() -> None:
    """Acceptance #4 — §14.3 verbatim: `parent.fanout.total_cost =
    Σ sibling.total_cost`."""
    siblings = [_sibling(1.0, 100), _sibling(2.0, 200), _sibling(3.5, 150)]
    result = rollup_fanout_at_close(_span(), siblings, FanOutPattern.PARALLEL)
    assert result.parent_fanout_total_cost == 6.5


def test_rollup_total_latency_parallel_max() -> None:
    """Acceptance #4 — PARALLEL fan-out: total latency is `max(sibling)`."""
    siblings = [_sibling(1.0, 100), _sibling(1.0, 350), _sibling(1.0, 200)]
    result = rollup_fanout_at_close(_span(), siblings, FanOutPattern.PARALLEL)
    assert result.parent_fanout_total_latency == 350


def test_rollup_total_latency_sequential_sum() -> None:
    """Acceptance #4 — SEQUENTIAL fan-out: total latency is `Σ sibling`."""
    siblings = [_sibling(1.0, 100), _sibling(1.0, 350), _sibling(1.0, 200)]
    result = rollup_fanout_at_close(_span(), siblings, FanOutPattern.SEQUENTIAL)
    assert result.parent_fanout_total_latency == 650


def test_rollup_sibling_count_matches() -> None:
    """Acceptance #4 — `sibling_count` reflects the rolled-up sibling set."""
    siblings = [_sibling(1.0, 100) for _ in range(4)]
    result = rollup_fanout_at_close(_span(), siblings, FanOutPattern.PARALLEL)
    assert result.sibling_count == 4
    assert isinstance(result, FanOutRollupResult)


def test_rollup_uses_post_overhead_total_cost() -> None:
    """Acceptance #5 — per-sibling cost is the post-sandbox-overhead
    `SpanTotalCost.total_cost`, not the pre-sandbox `span_cost`."""
    overhead = SandboxOverhead(tier_overhead_usd=0.50, tier_overhead_ms=100)
    sib = compose_span_total_cost(span_cost=1.00, span_duration_ms=200, sandbox_overhead=overhead)
    assert sib.span_cost == 1.00
    assert sib.total_cost == 1.50
    result = rollup_fanout_at_close(_span(), [sib, sib], FanOutPattern.PARALLEL)
    # rollup sums total_cost (1.50) not span_cost (1.00) — 2 x 1.50.
    assert result.parent_fanout_total_cost == 3.0


# --- acceptance #3 / #7 — cross-axis edges (Pattern P1 byte-exact) ----------


def test_cross_axis_edge_to_u_as_nn_c_as_15_section_15_6() -> None:
    """Acceptance #3 / #7 — `SandboxOverhead` field names are byte-exact with
    the AS C-AS-15 §15.6 `sandbox.cost.tier_overhead_*` attribute surface
    (Pattern P1 mechanical-alignment discipline)."""
    overhead = SandboxOverhead(tier_overhead_usd=0.25, tier_overhead_ms=80)
    # the field names map byte-exact to `sandbox.cost.tier_overhead_{usd,ms}`.
    assert set(SandboxOverhead.model_fields) == {"tier_overhead_usd", "tier_overhead_ms"}
    assert overhead.tier_overhead_usd == 0.25
    assert overhead.tier_overhead_ms == 80


def test_cross_axis_edge_to_u_cp_nn_c_cp_14_section_14_1() -> None:
    """Acceptance #6 / #7 — `FanOutPattern` value set is sourced from the CP
    C-CP-14 §14.1 fan-out pattern attribute (cross-axis edge annotated)."""
    assert {p.value for p in FanOutPattern} == {"PARALLEL", "SEQUENTIAL"}
    assert len(FanOutPattern) == 2
