"""Sandbox-tier overhead + per-sibling fan-out rollup — U-OD-19.

Implements C-OD-14 §14.2 (sandbox-tier overhead addition — `total_cost` and
`total_latency` compose the per-span cost with the `sandbox.cost.tier_overhead_*`
attributes per AS C-AS-15 §15.6) and §14.3 (per-sibling rollup at the
`topology.fanout.closed` event per CP C-CP-14 §14.1).

`compose_span_total_cost` applies sandbox-tier overhead additively over the
U-OD-18 per-span `span_cost`; `rollup_fanout_at_close` aggregates sibling
`SpanTotalCost` values at fan-out close — sum of cost, and `max` (PARALLEL) or
`Σ` (SEQUENTIAL) of latency per `FanOutPattern`.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.5.2 U-OD-19
(v2.6 M-1 revision — `SpanRef` at `rollup_fanout_at_close` re-pointed to the
U-OD-04 carrier, `[U-OD-04]` edge added; all v2.1 surfaces preserved verbatim
from v2.1 §3.5.2); Spec_Operational_Discipline_v1_2.md §14 C-OD-14 §14.2 +
§14.3 (preserved verbatim into v1.3); ADR-D6 v1.2 (cost-attribution-per-span);
the `sandbox.cost.tier_overhead_*` attribute names are AS C-AS-15 §15.6 / the
fan-out pattern is CP C-CP-14 §14.1 — both consumed cross-axis (Pattern P1
byte-exact alignment).

Depends on: [U-OD-18, U-OD-04, U-AS-NN (cross-axis: AS — C-AS-15 §15.6),
U-CP-NN (cross-axis: CP — C-CP-14 §14.1)]. The cross-axis dependencies are
attribute-name + enum-value surfaces (resolved at U-OD-34); no cross-axis type
is consumed at a signature position — `SpanRef` (U-OD-04) is the only
non-OD-internal type, and it is now carried.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import SpanRef

__all__ = [
    "FanOutPattern",
    "FanOutRollupResult",
    "SandboxOverhead",
    "SpanTotalCost",
    "compose_span_total_cost",
    "rollup_fanout_at_close",
]


class SandboxOverhead(BaseModel):
    """Sandbox-tier cost + latency overhead for a sandbox-bounded span.

    The `sandbox.cost.tier_overhead_*` attributes per AS C-AS-15 §15.6 — the
    per-sandbox-instance cost overhead (`tier_overhead_usd`) and the sandbox
    tier startup/teardown latency (`tier_overhead_ms`). Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `sandbox.cost.tier_overhead_usd` (C-AS-15 §15.6).
    tier_overhead_usd: float
    #: `sandbox.cost.tier_overhead_ms` (C-AS-15 §15.6).
    tier_overhead_ms: int


class SpanTotalCost(BaseModel):
    """A per-span cost with sandbox-tier overhead composed in (C-OD-14 §14.2).

    `total_cost` is the U-OD-18 `span_cost` plus
    `sandbox_overhead.tier_overhead_usd`; `total_latency_ms` is the span
    duration plus `sandbox_overhead.tier_overhead_ms`. Non-sandbox-bounded
    spans carry `sandbox_overhead = None` and `total_* == span/duration`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the per-span cost from U-OD-18 `compute_span_cost`.
    span_cost: float
    #: the sandbox-tier overhead — `None` for non-sandbox-bounded spans.
    sandbox_overhead: SandboxOverhead | None
    #: `span_cost + sandbox_overhead.tier_overhead_usd` (§14.2 verbatim).
    total_cost: float
    #: `span_duration_ms + sandbox_overhead.tier_overhead_ms` (§14.2 verbatim).
    total_latency_ms: int


def compose_span_total_cost(
    span_cost: float,
    span_duration_ms: int,
    sandbox_overhead: SandboxOverhead | None,
) -> SpanTotalCost:
    """Compose the per-span total cost with sandbox-tier overhead (C-OD-14 §14.2).

    Applies the §14.2 formula verbatim:
    `total_cost = span_cost + sandbox_overhead.tier_overhead_usd`;
    `total_latency_ms = span_duration_ms + sandbox_overhead.tier_overhead_ms`.
    Non-sandbox-bounded spans pass `sandbox_overhead = None` — then
    `total_cost == span_cost` and `total_latency_ms == span_duration_ms`
    (acceptance #2).
    """
    if sandbox_overhead is None:
        return SpanTotalCost(
            span_cost=span_cost,
            sandbox_overhead=None,
            total_cost=span_cost,
            total_latency_ms=span_duration_ms,
        )
    return SpanTotalCost(
        span_cost=span_cost,
        sandbox_overhead=sandbox_overhead,
        total_cost=span_cost + sandbox_overhead.tier_overhead_usd,
        total_latency_ms=span_duration_ms + sandbox_overhead.tier_overhead_ms,
    )


class FanOutPattern(StrEnum):
    """The fan-out latency-aggregation pattern (CP C-CP-14 §14.1).

    `PARALLEL` — sibling spans run concurrently; parent fan-out total latency
    is `max(sibling.total_latency)`. `SEQUENTIAL` — sibling spans run in
    series; parent fan-out total latency is `Σ sibling.total_latency`.
    """

    PARALLEL = "PARALLEL"
    SEQUENTIAL = "SEQUENTIAL"


class FanOutRollupResult(BaseModel):
    """The per-sibling cost + latency rollup at fan-out close (C-OD-14 §14.3).

    `parent_fanout_total_cost` is `Σ sibling.total_cost` regardless of
    `FanOutPattern`; `parent_fanout_total_latency` is `max` (PARALLEL) or `Σ`
    (SEQUENTIAL) of sibling `total_latency_ms`. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `Σ sibling.total_cost` (§14.3 verbatim).
    parent_fanout_total_cost: float
    #: `max(sibling.total_latency)` for PARALLEL, `Σ` for SEQUENTIAL.
    parent_fanout_total_latency: int
    #: count of sibling spans rolled up.
    sibling_count: int


def rollup_fanout_at_close(
    parent_span_ref: SpanRef,
    sibling_costs: list[SpanTotalCost],
    pattern: FanOutPattern,
) -> FanOutRollupResult:
    """Roll up sibling span costs onto the parent at `topology.fanout.closed`.

    Fires at the `topology.fanout.closed` event per the U-OD-08 F3 lifecycle
    mapping + CP C-CP-14 §14.1. Aggregation per §14.3 verbatim:
    `parent.fanout.total_cost = Σ sibling.total_cost`;
    `parent.fanout.total_latency = max(sibling.total_latency)` for PARALLEL or
    `Σ sibling.total_latency` for SEQUENTIAL. Per-sibling cost is the
    post-sandbox-overhead `SpanTotalCost.total_cost`, not the pre-sandbox
    `span_cost` (acceptance #5) — sandbox overhead propagates through the
    rollup.

    `parent_span_ref` is the OTel span handle (U-OD-04 carrier) the rolled-up
    fan-out totals are attributed to.
    """
    # `parent_span_ref` identifies the OTel parent span the rollup attributes
    # to; the aggregation itself is over the sibling SpanTotalCost values.
    _ = parent_span_ref
    total_cost = sum(sibling.total_cost for sibling in sibling_costs)
    latencies = [sibling.total_latency_ms for sibling in sibling_costs]
    if pattern is FanOutPattern.PARALLEL:
        total_latency = max(latencies) if latencies else 0
    else:
        total_latency = sum(latencies)
    return FanOutRollupResult(
        parent_fanout_total_cost=total_cost,
        parent_fanout_total_latency=total_latency,
        sibling_count=len(sibling_costs),
    )
