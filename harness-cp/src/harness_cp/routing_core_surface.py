"""Thin routing core surface — `infer` entry-point — U-CP-03.

Implements C-CP-01 §1.1 (the thin routing core API surface). Declares the
`InferenceRequest` API envelope, the `InferenceResponse` record, and `infer` —
the single LLM-inference entry-point at the CP layer.

`infer` is the **probabilistic core** of the deterministic outer harness per
ADD §5.3.3: everything around it (chain-advancement, cascade-enforcement,
retry, breaker, HITL) is deterministic; `infer` is the one probabilistic
boundary. It orchestrates layered routing -> provider dispatch -> response
materialization. Routing-strategy resolution delegates to U-CP-05; provider
dispatch delegates to provider SDK adapters (out of scope at the CP plan; the
AS plan declares the MCP server SDK boundaries).
Layer-3 LLM_AS_ROUTER fall-through materialization lands here as U-CP-100:
`infer` accepts an injected `router` callable and routes the L3 sentinel through
that callable before response materialization.

Note on `InferenceRequest`: this is the C-CP-01 §1.1 *API envelope* — a
6-field record carrying the routing-discriminator fields plus the
provider-agnostic payload. It is distinct from the module-local
`type InferenceRequest = ProviderAgnosticPayload` aliases at
`fall_through_procedure.py` / `layered_routing_strategy.py`, which name the
*inner* routing-call payload surface per Implementation Plan v2.9 §0.1 item 3
(the §0.1 unification is scoped to "U-CP-05/08 routing-call signature
positions" — it does NOT revise the U-CP-03 body).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-03 (preserved
verbatim through v2.9 — v2.9 is a multi-body delta that does not touch U-CP-03);
Spec_Control_Plane_v1_2.md §1 C-CP-01 §1.1; Architectural_Design_Document_v1_3.md
§5.3.3.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from harness_core import PersonaTier, WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import (
    AgentRole,
    ProviderAgnosticPayload,
    RouterResolution,
    RoutingDecisionTrace,
    TraceContext,
)
from harness_cp.layer_budget import (
    DEFAULT_LAYER_BUDGETS,
    LayerBudget,
    effective_layer_budget_ms,
)
from harness_cp.layered_routing_strategy import LayerDecisionFn, route
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoutingManifest


class InferenceRequest(BaseModel):
    """The C-CP-01 §1.1 inference-request API envelope.

    Carries the routing-discriminator fields (`agent_role`, `workload_class`,
    `persona_tier`) consumed by the layered-routing-strategy per C-CP-02 §2.1,
    the context-token count, the provider-agnostic request payload, and the
    trace context for `routing.*` span attribution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_role: AgentRole
    workload_class: WorkloadClass
    persona_tier: PersonaTier
    context_tokens: int
    request_payload: ProviderAgnosticPayload
    trace_context: TraceContext
    """For `routing.*` span attribution (U-CP-01)."""


class InferenceResponse(BaseModel):
    """The C-CP-01 §1.1 inference-response record.

    `routing_decision` is populated by U-CP-05 at routing-time and carries the
    routing layer + candidate + decision latency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_used: str
    model_used: str
    routing_decision: RoutingDecisionTrace
    """layer + candidate + decision_ms — populated by U-CP-05 at routing-time."""

    response_payload: ProviderAgnosticPayload
    tokens_in: int
    tokens_out: int
    cached_tokens_in: int


class ProviderDispatchResult(BaseModel):
    """The materialization inputs returned by the injected provider-dispatch
    callable (R-300 activation).

    `infer` composes the layered routing decision (`route` per U-CP-05) with a
    provider-dispatch callable injected by the caller. Provider dispatch is
    "out of scope at the CP plan" (C-CP-01 §1.1 "deferred to implementation
    discretion: ... specific provider-adapter binding library"); the runtime
    composes its provider-SDK closure and hands the post-call materialization
    inputs back through this record so `infer` can assemble the
    `InferenceResponse`. CP-pure: no harness-runtime import — the callable is a
    closure, not a dependency.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    response_payload: ProviderAgnosticPayload
    tokens_in: int
    tokens_out: int
    cached_tokens_in: int


# The injected provider-dispatch callable. Given the routing-selected
# `provider` + `model`, the request payload, and the routing trace (for
# `routing.*` span attribution on the caller's `llm.inference` span), it
# performs the provider SDK call and returns the materialization inputs.
# Async per the asyncio concurrency commitment (Target Stack §5.1) — the
# runtime provider-SDK boundary is async; `infer` awaits it.
#
# A `Protocol` (not a plain `Callable` alias) so the additive optional
# keyword-only `binding_rationale` parameter (C-CP-02 §2.5.4) is expressible:
# the Layer-3 router rationale rides this channel to the span owner ONLY when
# `infer` resolves via a router; it defaults to `None`, so the non-router path
# calls with the four positional args (no kwarg) and the span emitter keeps
# deriving `f"{layer}:{candidate}"` — legacy four-arg callables stay unbroken.
class ProviderDispatchFn(Protocol):
    def __call__(
        self,
        provider: str,
        model: str,
        payload: ProviderAgnosticPayload,
        trace: RoutingDecisionTrace,
        /,
        *,
        binding_rationale: str | None = None,
    ) -> Awaitable[ProviderDispatchResult]: ...


# The injected Layer-3 router-resolution callable (C-CP-02 §2.5.1). Async — a
# faithful Layer 3 makes one full async LLM call to a router model (§2.2 "One
# full LLM call; 50-200 ms"; "Probabilistic"), resolvable only at the
# already-async `infer` (§5.3.3 — the probabilistic core), never inside sync
# `route`. Injected: the concrete router model + prompt are the §2.4
# impl-discretion / vendor deferral. The router is a terminal leaf — it
# dispatches DIRECTLY against its pre-bound model and MUST NOT re-enter
# `infer`/`route` (the §2.5.1 infinite-regress guard). It takes the inference
# request (`call_site_context`) + a `candidate_set_summary` string (a summary
# of the eligible `"provider:model"` universe; derivation is impl-discretion).
type RouterResolutionFn = Callable[
    [InferenceRequest, str],
    Awaitable[RouterResolution],
]


class RoutingCandidateUnresolvedError(RuntimeError):
    """No routing layer produced a candidate (final fall-through with empty
    candidate), or the candidate is not a well-formed ``"provider:model"``
    string. Surfaced by `infer` after `route` per U-CP-05."""


def _candidate_set_summary(manifest: RoutingManifest) -> str:
    """A summary of the eligible ``"provider:model"`` candidate universe for
    the Layer-3 router (C-CP-02 §2.5.1's `candidate_set_summary`). Derived from
    the manifest's per-role preferred model bindings — the candidate set the
    deterministic layers drew from. Derivation is impl-discretion (§2.5.1); a
    deterministic sorted-unique join keeps it stable for the router prompt +
    tests."""
    return ", ".join(
        sorted(
            {
                f"{b.preferred_model_binding.provider}:{b.preferred_model_binding.model}"
                for b in manifest.per_role_bindings.values()
            }
        )
    )


async def resolve_routing_trace(
    request: InferenceRequest,
    *,
    manifest: RoutingManifest,
    layer_decisions: Mapping[RoutingLayer, LayerDecisionFn],
    budgets: tuple[LayerBudget, ...] = DEFAULT_LAYER_BUDGETS,
    budget_exhausted: frozenset[RoutingLayer] = frozenset(),
    router: RouterResolutionFn | None = None,
) -> tuple[RoutingDecisionTrace, str | None]:
    """The route()+L3-resolution SELECTION half of `infer` — NO provider dispatch.

    Runs the layered routing strategy (`route` per U-CP-05) and, when the
    deterministic layers fall through to the LLM_AS_ROUTER empty-candidate
    sentinel AND a `router` is injected AND the L3 budget is not exhausted,
    resolves Layer 3 via one budget-bounded async router call (§2.5.2/§2.5.3).
    Returns the resolved ``(RoutingDecisionTrace, binding_rationale)`` — the
    rationale is the optional Layer-3 router rationale (``None`` on every
    non-router path).

    Factored from `infer` at B-L2-FALLBACK-COMPOSITION (`Spec_Harness_Runtime_v1.md`
    §14.6) so the C-RT-16 retry/breaker/fallback wrapper can resolve the
    route-once PRIMARY candidate WITHOUT dispatching — the layered routing
    decision composes with the fallback chain as the wrapper's primary
    candidate, instead of re-running per fallback attempt (the silent
    fallback-defeat the §14.5.3 "wrapper owns model-candidate selection"
    invariant forecloses). `infer` calls this then dispatches; the runtime's
    `RuntimeLLMDispatcher.resolve_routed_binding` calls it then seeds the
    wrapper's primary candidate.

    Raises
    ------
    RoutingCandidateUnresolvedError
        No layer produced a candidate; the candidate is malformed; or the
        LLM_AS_ROUTER layer fell through with no injected router / an exhausted
        or over-budget L3 (§2.5.2/§2.5.3 — L3 terminal).
    """
    trace = route(
        request.request_payload,
        manifest,
        dict(layer_decisions),
        budgets,
        budget_exhausted=budget_exhausted,
    )
    # C-CP-02 §2.5.2 — Layer-3 LLM_AS_ROUTER resolution at the async call
    # surface (Reading B). `route` returns the empty-candidate sentinel when the
    # deterministic layers fall through; resolve it via the injected async router
    # ONLY when a router is injected AND the L3 budget is not exhausted. Otherwise
    # the preserved sentinel raise governs (LLM_AS_ROUTER is terminal — no further
    # layer to fall through to).
    binding_rationale: str | None = None
    if trace.candidate == "" and trace.layer == RoutingLayer.LLM_AS_ROUTER.value:
        if router is None or RoutingLayer.LLM_AS_ROUTER in budget_exhausted:
            raise RoutingCandidateUnresolvedError(
                f"routing produced no well-formed 'provider:model' candidate "
                f"(layer={trace.layer!r}, candidate={trace.candidate!r})"
            )
        # ENFORCE the effective L3 LayerBudget (C-CP-03 §3.1 / §2.5.3): a
        # slow/hanging router is interrupted at the budget and converted to L3
        # exhaustion -> the SAME preserved raise (timeout = exhaustion, L3
        # terminal). The budget is the §3.1 OVERRIDE-RESOLVED value keyed on the
        # request's `workload_class` + `persona_tier` (B-LAYER-BUDGET-OVERRIDE).
        l3_budget_seconds = (
            effective_layer_budget_ms(
                budgets,
                RoutingLayer.LLM_AS_ROUTER,
                request.workload_class,
                request.persona_tier,
            )
            / 1000
        )
        start = time.monotonic()
        try:
            resolution = await asyncio.wait_for(
                router(request, _candidate_set_summary(manifest)),
                timeout=l3_budget_seconds,
            )
        except TimeoutError as exc:
            raise RoutingCandidateUnresolvedError(
                f"Layer-3 router exceeded the "
                f"{l3_budget_seconds * 1000:.0f} ms LLM_AS_ROUTER budget "
                f"(layer={trace.layer!r})"
            ) from exc
        # Rebuild the trace with the router-supplied candidate — the four frozen
        # fields only (§2.5.4: the rationale is NOT carried on the trace; it
        # rides the dispatch-seam `binding_rationale` channel returned below).
        trace = RoutingDecisionTrace(
            layer=RoutingLayer.LLM_AS_ROUTER.value,
            candidate=resolution.candidate,
            decision_ms=int((time.monotonic() - start) * 1000),
            budget_exhausted=trace.budget_exhausted,
        )
        binding_rationale = resolution.rationale
    provider, sep, model = trace.candidate.partition(":")
    if not sep or not provider or not model:
        # Reused well-formedness guard — also catches a malformed router return
        # (§2.5.2): a router candidate that is not "provider:model" re-raises.
        raise RoutingCandidateUnresolvedError(
            f"routing produced no well-formed 'provider:model' candidate "
            f"(layer={trace.layer!r}, candidate={trace.candidate!r})"
        )
    return trace, binding_rationale


async def infer(
    request: InferenceRequest,
    *,
    dispatch: ProviderDispatchFn,
    manifest: RoutingManifest,
    layer_decisions: Mapping[RoutingLayer, LayerDecisionFn],
    budgets: tuple[LayerBudget, ...] = DEFAULT_LAYER_BUDGETS,
    budget_exhausted: frozenset[RoutingLayer] = frozenset(),
    router: RouterResolutionFn | None = None,
) -> InferenceResponse:
    """The thin routing core surface — the single LLM-inference entry-point.

    All downstream LLM calls at the CP layer flow through this surface
    (acceptance #1). `infer` orchestrates layered routing -> provider dispatch
    -> response materialization: the routing strategy delegates to U-CP-05
    (`route`) and provider dispatch delegates to the injected `dispatch`
    callable (the provider SDK adapters are out of scope at the CP plan per
    C-CP-01 §1.1; the runtime composes its provider-SDK closure here).

    `infer` is the probabilistic core of the deterministic outer harness per
    ADD §5.3.3 (acceptance #4): everything around it (chain-advancement,
    cascade-enforcement, retry, breaker, HITL) is deterministic.

    R-300 activation (2026-06-01): the v1.6-era `NotImplementedError` stub is
    lifted. `infer`:
      1. routes the inner payload through the layered strategy (`route` per
         U-CP-05) -> `RoutingDecisionTrace` (`routing_decision`, acceptance
         #3); the DECLARATIVE manifest layer carries `layer == "manifest"`
         (`RoutingLayer.DECLARATIVE.value`);
      1b. C-CP-02 §2.5 (Reading B): when `route` falls through to the
         LLM_AS_ROUTER empty-candidate sentinel AND an optional `router`
         (`RouterResolutionFn`) is injected AND the L3 budget is not
         exhausted, resolves Layer 3 here via one budget-bounded async router
         call, rebuilding the trace with the router-supplied candidate; absent
         a router / L3-exhausted / over-budget -> the preserved sentinel raise;
      2. parses the selected ``"provider:model"`` candidate;
      3. invokes the injected `dispatch` callable with the routed
         provider/model + the routing trace (threading the router rationale via
         the optional `binding_rationale` carrier ONLY on the router path);
      4. materializes the `InferenceResponse`.

    Per-layer time budgets are bound per C-CP-03 (`budgets`, default
    `DEFAULT_LAYER_BUDGETS`). The optional `router` is absent by default — the
    Layer-3 resolution surface is inert until a router is injected (§2.5.5).

    Raises
    ------
    RoutingCandidateUnresolvedError
        No layer produced a candidate; the candidate is malformed; or the
        LLM_AS_ROUTER layer fell through with no injected router / an exhausted
        or over-budget L3 (§2.5.2/§2.5.3 — L3 terminal).
    """
    # The route()+L3 SELECTION is factored to `resolve_routing_trace`
    # (B-L2-FALLBACK-COMPOSITION) so the C-RT-16 wrapper can route-once without
    # dispatching; `infer` = resolve + dispatch. The non-router path is
    # byte-identical to the pre-§2.5 behavior (the carrier is None on it).
    trace, binding_rationale = await resolve_routing_trace(
        request,
        manifest=manifest,
        layer_decisions=layer_decisions,
        budgets=budgets,
        budget_exhausted=budget_exhausted,
        router=router,
    )
    provider, _sep, model = trace.candidate.partition(":")
    # Dispatch-call split (§2.5.2/§2.5.4): the carrier is passed ONLY on the
    # router path. The non-router path calls `dispatch` with exactly the four
    # positional args (no kwarg) -> byte-identical to HEAD, so the span
    # emitter's `f"{layer}:{candidate}"` derivation runs unchanged and legacy
    # four-arg callables keep working.
    if binding_rationale is None:
        result = await dispatch(provider, model, request.request_payload, trace)
    else:
        result = await dispatch(
            provider,
            model,
            request.request_payload,
            trace,
            binding_rationale=binding_rationale,
        )
    return InferenceResponse(
        provider_used=provider,
        model_used=model,
        routing_decision=trace,
        response_payload=result.response_payload,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cached_tokens_in=result.cached_tokens_in,
    )
