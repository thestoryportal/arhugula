"""Deterministic fall-through procedure — U-CP-08.

Implements C-CP-03 §3.5 (the `fallback.cause` 4-value attribute domain) plus
§3.2/§3.3 (the layer-advancement procedure). Declares the closed 4-value
`FallThroughCause` enum, the `FallThroughResult` record, and the deterministic
`fall_through` procedure.

`FallThroughCause` is a byte-exact factor-out of the C-CP-03 §3.5 `retry.*` /
`fallback.*` namespace `fallback.cause` attribute domain
(`∈ {time_budget_exceeded, capability_shortfall, breaker_open,
rate_limit_storm}`) — a closed 4-value set. The v2.1 invented enum
(`LAYER_NO_DECISION` etc.) is struck per the v2.8 conformance delta. The
"layer produced no decision" case is NOT an enum member — it is `cause = None`
at the `fall_through` signature (a silent advance per §3.2 step 2).

Layer ordering is fixed `DECLARATIVE → EMBEDDING → LLM_AS_ROUTER` per the
U-CP-05 ordering invariant; no upward skip permitted.

Authority: Implementation_Plan_Control_Plane_v2_8.md §2.1 U-CP-08 (revised body
— `FallThroughCause` conformed to C-CP-03 §3.5; cited section corrected
§3.2 → §3.5); Spec_Control_Plane_v1_3.md §3 C-CP-03 §3.2, §3.3, §3.5; CLAUDE.md
§3.2 (hand-rolled retry — NO tenacity/pybreaker).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import ProviderAgnosticPayload
from harness_cp.routing_layer import RoutingLayer

# `InferenceRequest` is unified to the U-CP-00c `ProviderAgnosticPayload` per
# Implementation_Plan_Control_Plane_v2_9.md §0.1 item 3 — `InferenceRequest` is
# a plan-spelling variant of `ProviderAgnosticPayload`, not a new type.
type InferenceRequest = ProviderAgnosticPayload

# The fixed layer-ordering invariant per C-CP-03 §3.2 + U-CP-05 §2.2: the
# layered-routing fall-through advances DECLARATIVE → EMBEDDING → LLM_AS_ROUTER;
# no upward skip permitted.
_LAYER_ORDER: tuple[RoutingLayer, ...] = (
    RoutingLayer.DECLARATIVE,
    RoutingLayer.EMBEDDING,
    RoutingLayer.LLM_AS_ROUTER,
)


class FallThroughCause(StrEnum):
    """The cause of a layer fall-through (C-CP-03 §3.5 `fallback.cause`).

    Closed at cardinality 4. Byte-exact factor-out of the C-CP-03 §3.5
    `fallback.cause` attribute domain
    (`∈ {time_budget_exceeded, capability_shortfall, breaker_open,
    rate_limit_storm}`). SCREAMING_SNAKE_CASE member names are the Python-stack
    rendering of the spec lowercase tokens; the string values match the spec
    byte-exact. No value invented — the v2.1 set (`LAYER_NO_DECISION` etc.) is
    struck. Extension is a Workflow §4.1.2 Class-2 D-revision.
    """

    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    """Per-layer time budget exceeded (C-CP-03 §3.2)."""

    CAPABILITY_SHORTFALL = "capability_shortfall"
    """Provider/model capability shortfall (C-CP-03 §3.3)."""

    BREAKER_OPEN = "breaker_open"
    """Circuit breaker open for `{provider, model}` (C-CP-03 §3.4)."""

    RATE_LIMIT_STORM = "rate_limit_storm"
    """Rate-limit-storm preemptive advancement (C-CP-04 §4.2)."""


class FallThroughResult(BaseModel):
    """The result of a single layer fall-through evaluation.

    C-CP-03 §3.2 — the procedure advances to the next layer in the fixed
    `DECLARATIVE → EMBEDDING → LLM_AS_ROUTER` order. `next_layer = None` at the
    final layer (LLM_AS_ROUTER); `emit_fallback_event` is `False` for the
    silent "no decision" advance and `True` for a `FallThroughCause`-driven
    advance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    triggered_at_layer: RoutingLayer
    """The layer the fall-through was triggered from."""

    cause: FallThroughCause | None
    """The fall-through cause; `None` for the silent "no decision" advance."""

    next_layer: RoutingLayer | None
    """The next layer in fixed order; `None` at the final layer."""

    emit_fallback_event: bool
    """`True` iff a `fallback.triggered` event must be emitted (non-`None`
    cause); `False` for the silent "no decision" advance."""


def fall_through(
    current_layer: RoutingLayer,
    cause: FallThroughCause | None,
    request: InferenceRequest,
) -> FallThroughResult:
    """Advance the layered-routing strategy by one layer — deterministic.

    Per C-CP-03 §3.2:
      - ``cause is None`` — the "layer produced no decision" case: advance to
        the next layer per §3.2 step 2 WITHOUT emitting a `fallback.triggered`
        event (no spec cause applies; a silent advance).
      - ``cause`` is a `FallThroughCause` — emit `fallback.triggered` per
        §3.2/§3.3 with `fallback.cause = cause`.

    Returns the next layer in `DECLARATIVE → EMBEDDING → LLM_AS_ROUTER` order;
    `next_layer = None` at the final layer (LLM_AS_ROUTER fall-through emits
    `fallback.exhausted` per §3.2 step 3). The procedure is deterministic given
    its inputs; `request` is carried for span-attribute association only.
    """
    # `request` participates only in span-attribute association at the caller;
    # the advancement is purely a function of (current_layer, cause).
    _ = request
    idx = _LAYER_ORDER.index(current_layer)
    next_layer = _LAYER_ORDER[idx + 1] if idx + 1 < len(_LAYER_ORDER) else None
    return FallThroughResult(
        triggered_at_layer=current_layer,
        cause=cause,
        next_layer=next_layer,
        emit_fallback_event=cause is not None,
    )
