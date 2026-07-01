"""`LayerBudget` data type — U-CP-06.

Implements C-CP-03 §3.1 (per-layer time-budget bound). Declares the
`LayerBudget` record, the `DEFAULT_LAYER_BUDGETS` constant (one entry per
`RoutingLayer`), and `effective_budget` — the override-resolution function.

**Field set.** The plan U-CP-06 Signatures block commits a 4-field
`LayerBudget` — `layer`, `time_budget_ms`, `per_workload_override`,
`per_persona_override`. The CP spec §3.1 record block displays a 3-field shape
(`layer`, `timeout_ms`, `soft_warn_ms`), but the §3.1 narrative immediately
following commits the override surface verbatim — "Per-layer time-budget is
**per-workload-class operator-tunable** ... Tuning is per layer × per workload
class × per persona tier". The plan's `per_workload_override` /
`per_persona_override` maps materialize that spec-committed tuning surface; the
plan body is the execution authority and is followed here. `soft_warn_ms` (the
§3.1 optional soft-warning threshold) is not in the plan's 4-field commitment
and is not added — adding a spec-record field the plan unit does not commit
would be a design extension.

**Default values.** `DEFAULT_LAYER_BUDGETS` exposes one entry per
`RoutingLayer`; concrete millisecond values are operator-binding-time
discretion per the §3.1 deferred list. The values below are conservative
placeholders that honor cheapest-deterministic-first (manifest tightest,
llm_as_router loosest) — they are characterization defaults, operator-tunable.

**Carrier note.** `RoutingLayer` is declared at `harness_cp.routing_layer`
(carrier-home relocation from U-CP-05 per the mutual U-CP-05 ↔ U-CP-06 cycle —
see that module's docstring).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-06 (preserved
verbatim through v2.6 — only the `[U-CP-00]` Pattern-E edge recorded at v2.5
§0.5, no body rewrite); Spec_Control_Plane_v1_2.md §3 C-CP-03 §3.1 (preserved
verbatim into v1.3); ADR-F1 v1.2 §Decision.
"""

from __future__ import annotations

from harness_core import PersonaTier, WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_cp.routing_layer import RoutingLayer


class LayerBudget(BaseModel):
    """One routing layer's per-layer time-budget bound (C-CP-03 §3.1).

    Declares exactly four fields per the U-CP-06 plan Signatures block.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer: RoutingLayer
    """The routing layer this budget governs."""

    time_budget_ms: int
    """Wall-clock budget per layer (ms). Layer exceedance of this hard bound
    triggers deterministic fall-through per C-CP-03 §3.2 — not error."""

    per_workload_override: dict[WorkloadClass, int] | None = None
    """Optional per-workload-class budget override (ms). Materializes the
    §3.1 per-workload-class operator-tunable surface."""

    per_persona_override: dict[PersonaTier, int] | None = None
    """Optional per-persona-tier budget override (ms). Materializes the §3.1
    per-persona-tier tuning surface (higher-tier persona caps tighter per
    Persona §6 per-class cost ceiling)."""


DEFAULT_LAYER_BUDGETS: tuple[LayerBudget, ...] = (
    LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
    LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
    LayerBudget(layer=RoutingLayer.LLM_AS_ROUTER, time_budget_ms=200),
)
"""One `LayerBudget` per `RoutingLayer` value — exactly 3 entries. Concrete
millisecond values are operator-binding-time discretion per §3.1's deferred
list; the values here are cheapest-deterministic-first-ordered placeholders."""


def effective_budget(
    layer: RoutingLayer,
    workload_class: WorkloadClass,
    persona_tier: PersonaTier,
) -> int:
    """Return the effective `time_budget_ms` for a layer after overrides.

    Override-resolution precedence per U-CP-06 acceptance #3: per-workload
    override is resolved first, then per-persona override, then the default.
    Both unset returns the layer default `time_budget_ms`.
    """
    budget = _budget_for_layer(layer)
    if budget.per_workload_override is not None and workload_class in budget.per_workload_override:
        return budget.per_workload_override[workload_class]
    if budget.per_persona_override is not None and persona_tier in budget.per_persona_override:
        return budget.per_persona_override[persona_tier]
    return budget.time_budget_ms


def _budget_for_layer(layer: RoutingLayer) -> LayerBudget:
    """Resolve the `DEFAULT_LAYER_BUDGETS` entry for a layer."""
    for budget in DEFAULT_LAYER_BUDGETS:
        if budget.layer is layer:
            return budget
    msg = f"no LayerBudget declared for routing layer {layer!r}"
    raise KeyError(msg)


def _resolve_layer_budget(
    budgets: tuple[LayerBudget, ...], layer: RoutingLayer
) -> LayerBudget | None:
    """Resolve a layer's `LayerBudget` from the PASSED `budgets` tuple first,
    then `DEFAULT_LAYER_BUDGETS` — the passed-tuple-then-DEFAULT fall-through
    shape `routing_core_surface._layer_time_budget_ms` used (now superseded by
    `effective_layer_budget_ms`). `None` when neither declares the layer."""
    for budget in budgets:
        if budget.layer is layer:
            return budget
    for budget in DEFAULT_LAYER_BUDGETS:
        if budget.layer is layer:
            return budget
    return None


def effective_layer_budget_ms(
    budgets: tuple[LayerBudget, ...],
    layer: RoutingLayer,
    workload_class: WorkloadClass,
    persona_tier: PersonaTier,
) -> int:
    """Return the effective per-layer `time_budget_ms` after §3.1 overrides,
    resolved against the PASSED `budgets` tuple (the operator-bound budgets the
    routing core threads), not the module-global `DEFAULT_LAYER_BUDGETS`.

    This is the `budgets`-tuple-aware sibling of `effective_budget`: the L3
    timeout site (`routing_core_surface.infer`) receives the operator-bound
    `budgets` tuple, so the per-workload-class / per-persona-tier override maps
    that materialize the C-CP-03 §3.1 tuning surface — which §3.1 commits
    explicitly **on `llm_as_router`** ("the higher-tier persona caps budget
    tighter on `llm_as_router`") — must be resolved against THAT tuple.
    `effective_budget` (above) reads `DEFAULT_LAYER_BUDGETS` and so physically
    cannot serve that site.

    Override-resolution precedence per U-CP-06 acceptance #3: per-workload
    override first, then per-persona override, then the layer's flat
    `time_budget_ms` (the §2.5.3 default — 200 ms for `LLM_AS_ROUTER` — when no
    override applies). The layer entry falls back passed-tuple → DEFAULT → the
    200 ms reservation when the passed tuple omits it (mirroring the former
    `_layer_time_budget_ms`).
    """
    budget = _resolve_layer_budget(budgets, layer)
    if budget is None:
        return 200
    if budget.per_workload_override is not None and workload_class in budget.per_workload_override:
        return budget.per_workload_override[workload_class]
    if budget.per_persona_override is not None and persona_tier in budget.per_persona_override:
        return budget.per_persona_override[persona_tier]
    return budget.time_budget_ms
