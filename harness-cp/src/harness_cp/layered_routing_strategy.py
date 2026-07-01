"""Layered routing strategy — U-CP-05.

Implements C-CP-02 §2.1 + §2.2 (the declarative -> embedding -> LLM-as-router
layered routing strategy with the cheapest-deterministic-first ordering
invariant).

The `RoutingLayer` enum is carrier-homed at U-CP-06 (`routing_layer.py` — the
U-CP-05 <-> U-CP-06 cycle resolution); `RoutingDecisionTrace` is carrier-homed
at U-CP-00c (`cp_shared_types.py` — operator decision D7). This unit imports
both and contributes the `route` strategy function.

`InferenceRequest` is unified to the v2.8 U-CP-00c `ProviderAgnosticPayload`
per Implementation Plan v2.9 §0.1 item 3 (`InferenceRequest` is a plan-spelling
variant of the `(messages, tools, params)` routing/inference call surface).

Layer ordering is fixed at compile time: DECLARATIVE -> EMBEDDING ->
LLM_AS_ROUTER per the §2.2 invariant. The DECLARATIVE layer short-circuits the
strategy on a manifest hit; budget exhaustion at a layer triggers fall-through
to the next layer (the fall-through procedure proper is U-CP-08).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.1 U-CP-05 (preserved
verbatim at v2.2-v2.9); Spec_Control_Plane_v1_3.md §2 C-CP-02 §2.1 + §2.2;
ADR-F1 v1.2.
"""

from __future__ import annotations

from collections.abc import Callable

from harness_cp.cp_shared_types import ProviderAgnosticPayload, RoutingDecisionTrace
from harness_cp.layer_budget import LayerBudget
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoutingManifest

# `InferenceRequest` is unified to the U-CP-00c `ProviderAgnosticPayload`
# (v2.9 §0.1 item 3) — the `(messages, tools, params)` routing/inference call
# surface. No new type.
type InferenceRequest = ProviderAgnosticPayload

# The fixed layer ordering per C-CP-02 §2.2 — cheapest-deterministic-first.
# Reordering is a Workflow §4.1.2 Class-2 F1 revision.
LAYER_ORDER: tuple[RoutingLayer, ...] = (
    RoutingLayer.DECLARATIVE,
    RoutingLayer.EMBEDDING,
    RoutingLayer.LLM_AS_ROUTER,
)

# A per-layer decision function: given the request + manifest, returns the
# selected `"provider:model"` candidate, or `None` if the layer produces no
# decision. Layer budgets cap the per-layer wall-clock cost.
LayerDecisionFn = Callable[[InferenceRequest, RoutingManifest], "str | None"]


def layer_budget_for(layer: RoutingLayer, budgets: tuple[LayerBudget, ...]) -> LayerBudget | None:
    """Return the `LayerBudget` for `layer`, or `None` if unbudgeted (U-CP-06)."""
    for b in budgets:
        if b.layer == layer:
            return b
    return None


def route(
    request: InferenceRequest,
    manifest: RoutingManifest,
    layer_decisions: dict[RoutingLayer, LayerDecisionFn],
    budgets: tuple[LayerBudget, ...] = (),
    *,
    budget_exhausted: frozenset[RoutingLayer] = frozenset(),
) -> RoutingDecisionTrace:
    """Route an inference request through the layered strategy.

    Tries DECLARATIVE, then EMBEDDING, then LLM_AS_ROUTER in fixed order
    (C-CP-02 §2.2). A layer that returns a decision short-circuits the
    strategy (acceptance #5 — a DECLARATIVE manifest hit performs no
    embedding/LLM-router computation). A layer whose budget is exhausted
    (`layer in budget_exhausted`) is skipped — fall-through to the next layer
    per U-CP-08. The returned trace records the deciding layer, candidate, and
    whether a budget was exhausted en route. Deterministic given inputs."""
    any_budget_exhausted = False
    for layer in LAYER_ORDER:
        if layer in budget_exhausted:
            any_budget_exhausted = True
            continue
        decision_fn = layer_decisions.get(layer)
        if decision_fn is None:
            continue
        candidate = decision_fn(request, manifest)
        if candidate is not None:
            return RoutingDecisionTrace(
                layer=layer.value,
                candidate=candidate,
                decision_ms=0,
                budget_exhausted=any_budget_exhausted,
            )
    # No layer produced a decision — final layer fall-through.
    return RoutingDecisionTrace(
        layer=RoutingLayer.LLM_AS_ROUTER.value,
        candidate="",
        decision_ms=0,
        budget_exhausted=any_budget_exhausted,
    )
