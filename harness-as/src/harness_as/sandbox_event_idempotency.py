"""Cross-axis idempotency-key composition + cost-attribution join — U-AS-19.

Implements C-AS-15 §15.6 (cross-axis idempotency-key composition, sub-agent
boundary inheritance, cost-attribution joining). Declares `IdempotencyKey`,
`SubAgentKeyDerivationStrategy`, `CostAttribution`, and the
`attach_idempotency_key_to_sandbox_event` /
`derive_sub_agent_idempotency_key` / `join_cost_attribution_by_idempotency_key`
functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-19 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §15.6 C-AS-15; ADR-D2 v1.2.

Depends on: U-AS-09, U-AS-17 (`SandboxSpanEvent`, `SpanEventKind`); cross-axis
IS — U-IS-07 (the `StateLedgerEntry.idempotency_key` shape) + U-IS-12 (the
`idempotency_key` cross-axis join contract per C-IS-10 §10.2). `idempotency_key`
is treated as an **opaque** join key (AC6) — construction lives at C-IS-07 §7.1;
`IdempotencyKey` is an opaque `str`-alias.

`derive_sub_agent_idempotency_key` takes the `SubAgentKeyDerivationStrategy` as
an injected argument — the strategy is filled by the CP plan per ADR-D4 v1.1
§1.9 (forward-declared interface, the U-AS-08 floor-injection pattern).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from harness_is import Identifier
from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_span_schema import SandboxSpanEvent, SpanEventKind

#: An opaque cross-axis idempotency join key (C-IS-10 §10.2). Bound to the IS
#: `StateLedgerEntry.idempotency_key` shape (U-IS-07) — the harness-canonical
#: cross-axis join key per the IDEMPOTENCY_KEY_JOIN_EXPORT seam (C-IS-10 §10.2).
type IdempotencyKey = Identifier
#: A sub-agent dispatch identifier.
type SubAgentDispatchId = str

_IDEMPOTENCY_ATTR = "idempotency_key"
_OVERHEAD_MS_ATTR = "sandbox.cost.tier_overhead_ms"
_OVERHEAD_USD_ATTR = "sandbox.cost.tier_overhead_usd"


class SubAgentKeyDerivationStrategy(Protocol):
    """Forward-declared sub-agent idempotency-key derivation strategy (§15.6).

    The strategy is filled by the CP plan per ADR-D4 v1.1 §1.9.
    """

    def derive(
        self, parent_key: IdempotencyKey, dispatch_id: SubAgentDispatchId
    ) -> IdempotencyKey: ...


class CostAttribution(BaseModel):
    """Aggregated per-idempotency-key sandbox-cost attribution (C-AS-15 §15.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: IdempotencyKey
    total_tier_overhead_ms: int
    total_tier_overhead_usd: float
    contributing_event_count: int


def attach_idempotency_key_to_sandbox_event(
    event: SandboxSpanEvent,
    parent_tool_call_idempotency_key: IdempotencyKey,
) -> SandboxSpanEvent:
    """Attach the parent `tool.call` idempotency key to a sandbox event (§15.6).

    Every sandbox event on a `tool.call` parent carries the parent's
    `idempotency_key` (§15.6 row 1) — the cross-axis join key (opaque).
    """
    return event.model_copy(
        update={
            "attributes": {
                **event.attributes,
                _IDEMPOTENCY_ATTR: parent_tool_call_idempotency_key,
            }
        }
    )


def derive_sub_agent_idempotency_key(
    parent_idempotency_key: IdempotencyKey,
    sub_agent_dispatch_id: SubAgentDispatchId,
    strategy: SubAgentKeyDerivationStrategy,
) -> IdempotencyKey:
    """Derive a sub-agent idempotency key at sub-agent dispatch (C-AS-15 §15.6).

    Sub-agent boundary inheritance (§15.6 row 2) — the derived key is produced
    by the injected `SubAgentKeyDerivationStrategy`; it is deterministic per
    `(parent_key, dispatch_id)` and distinct from the parent key.
    """
    return strategy.derive(parent_idempotency_key, sub_agent_dispatch_id)


def join_cost_attribution_by_idempotency_key(
    sandbox_exit_events: Iterable[SandboxSpanEvent],
) -> dict[IdempotencyKey, CostAttribution]:
    """Aggregate sandbox-cost attribution by idempotency key (C-AS-15 §15.6 row 3).

    Sums `sandbox.cost.tier_overhead_ms` / `tier_overhead_usd` across the
    `sandbox.exit` events that share an `idempotency_key`; events without an
    idempotency key or that are not `sandbox.exit` are skipped. Consumed at the
    D6 cost-attribution dashboarding (OD plan Session 4).
    """
    totals: dict[str, tuple[int, float, int]] = {}
    for event in sandbox_exit_events:
        if event.kind is not SpanEventKind.SANDBOX_EXIT:
            continue
        raw_key = event.attributes.get(_IDEMPOTENCY_ATTR)
        if not isinstance(raw_key, str):
            continue
        ms = event.attributes.get(_OVERHEAD_MS_ATTR, 0)
        usd = event.attributes.get(_OVERHEAD_USD_ATTR, 0)
        prev_ms, prev_usd, count = totals.get(raw_key, (0, 0.0, 0))
        totals[raw_key] = (
            prev_ms + int(ms if isinstance(ms, int) else 0),
            prev_usd + float(usd if isinstance(usd, (int, float)) else 0.0),
            count + 1,
        )
    return {
        Identifier(key): CostAttribution(
            idempotency_key=Identifier(key),
            total_tier_overhead_ms=ms,
            total_tier_overhead_usd=usd,
            contributing_event_count=count,
        )
        for key, (ms, usd, count) in totals.items()
    }
