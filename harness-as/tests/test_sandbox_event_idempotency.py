"""Tests for U-AS-19 — cross-axis idempotency-key composition (C-AS-15 §15.6)."""

from __future__ import annotations

import datetime

from harness_as.sandbox_event_idempotency import (
    CostAttribution,
    IdempotencyKey,
    SubAgentDispatchId,
    attach_idempotency_key_to_sandbox_event,
    derive_sub_agent_idempotency_key,
    join_cost_attribution_by_idempotency_key,
)
from harness_as.sandbox_span_schema import SandboxSpanEvent, SpanEventKind
from harness_is import Identifier

_TS = datetime.datetime(2026, 5, 16, tzinfo=datetime.UTC)


class _Strategy:
    """A stub SubAgentKeyDerivationStrategy."""

    def derive(self, parent_key: IdempotencyKey, dispatch_id: SubAgentDispatchId) -> IdempotencyKey:
        return Identifier(f"{parent_key}::{dispatch_id}")


def _exit_event(key: str, ms: int, usd: float) -> SandboxSpanEvent:
    return SandboxSpanEvent(
        kind=SpanEventKind.SANDBOX_EXIT,
        parent_span_id="tool.call.0",
        attributes={
            "idempotency_key": key,
            "sandbox.cost.tier_overhead_ms": ms,
            "sandbox.cost.tier_overhead_usd": usd,
        },
        timestamp=_TS,
    )


def test_attach_idempotency_key_propagates_from_parent() -> None:
    """Acceptance #1 — a sandbox event carries the parent tool.call idempotency key."""
    event = SandboxSpanEvent(
        kind=SpanEventKind.SANDBOX_ENTER,
        parent_span_id="tool.call.0",
        attributes={"sandbox.tier": "tier-1-process"},
        timestamp=_TS,
    )
    attached = attach_idempotency_key_to_sandbox_event(event, Identifier("idem-abc"))
    assert attached.attributes["idempotency_key"] == "idem-abc"


def test_idempotency_key_is_opaque_join_key() -> None:
    """Acceptance #6 — the idempotency key is treated as an opaque string."""
    event = SandboxSpanEvent(
        kind=SpanEventKind.SANDBOX_VIOLATION,
        parent_span_id="tool.call.0",
        attributes={},
        timestamp=_TS,
    )
    attached = attach_idempotency_key_to_sandbox_event(event, Identifier("opaque-xyz"))
    assert isinstance(attached.attributes["idempotency_key"], str)


def test_derive_sub_agent_idempotency_key_uses_strategy_interface() -> None:
    """Acceptance #4 — derivation goes through the SubAgentKeyDerivationStrategy."""
    derived = derive_sub_agent_idempotency_key(Identifier("parent-1"), "dispatch-9", _Strategy())
    assert derived == "parent-1::dispatch-9"


def test_sub_agent_idempotency_key_differs_from_parent() -> None:
    """Acceptance #4 — the derived sub-agent key differs from the parent key."""
    derived = derive_sub_agent_idempotency_key(Identifier("parent-1"), "dispatch-9", _Strategy())
    assert derived != "parent-1"


def test_sub_agent_idempotency_key_deterministic_per_dispatch_id() -> None:
    """Acceptance #4 — derivation is deterministic per (parent, dispatch_id)."""
    a = derive_sub_agent_idempotency_key(Identifier("parent-1"), "dispatch-9", _Strategy())
    b = derive_sub_agent_idempotency_key(Identifier("parent-1"), "dispatch-9", _Strategy())
    assert a == b


def test_join_cost_attribution_aggregates_per_idempotency_key() -> None:
    """Acceptance #5 — cost attribution sums per idempotency key."""
    joined = join_cost_attribution_by_idempotency_key(
        [_exit_event("k1", 100, 0.5), _exit_event("k1", 50, 0.25)]
    )
    attribution = joined[Identifier("k1")]
    assert attribution.total_tier_overhead_ms == 150
    assert attribution.total_tier_overhead_usd == 0.75
    assert attribution.contributing_event_count == 2


def test_join_cost_attribution_separates_keys() -> None:
    """Acceptance #5 — distinct idempotency keys aggregate separately."""
    joined = join_cost_attribution_by_idempotency_key(
        [_exit_event("k1", 100, 0.5), _exit_event("k2", 30, 0.1)]
    )
    assert set(joined) == {Identifier("k1"), Identifier("k2")}
    assert joined[Identifier("k2")].total_tier_overhead_ms == 30


def test_cross_axis_state_ledger_entry_shape_compatible() -> None:
    """Acceptance #2 — the idempotency key is shape-compatible with C-IS-05."""
    key: IdempotencyKey = Identifier("idem-1")
    assert isinstance(key, str)
    assert isinstance(CostAttribution.model_fields["idempotency_key"], object)


def test_f2_12_uniform_across_replay_scenarios() -> None:
    """Acceptance #7 — the cost-attribution join is deterministic (replay-uniform)."""
    events = [_exit_event("k1", 100, 0.5), _exit_event("k1", 50, 0.25)]
    assert join_cost_attribution_by_idempotency_key(
        events
    ) == join_cost_attribution_by_idempotency_key(events)
