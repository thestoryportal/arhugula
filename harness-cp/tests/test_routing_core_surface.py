"""Tests for U-CP-03 — thin routing core surface (C-CP-01 §1.1).

Acceptance-criterion coverage:
  #1 `infer` single entry-point   -> test_infer_single_entry_point
  #2 routing-discriminator fields -> test_inference_request_routing_discriminators
  #3 routing.* span attribution   -> test_infer_emits_routing_attributes
  #4 probabilistic core per ADD   -> test_infer_probabilistic_core_per_add_53

R-300 activation (2026-06-01) — `infer` lifts the v1.6 `NotImplementedError`
stub and composes `route` (U-CP-05) with an injected provider-dispatch
callable. Activation must_pass coverage:
  must_pass #1 infer() invokes route()        -> test_infer_invokes_route
  must_pass #2 layer == 'manifest' on decl    -> test_infer_declarative_layer_is_manifest
  must_pass #3 per-layer LayerBudget bound     -> test_infer_binds_layer_budgets
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from harness_core import PersonaTier, WorkloadClass
from harness_cp.cp_shared_types import (
    AgentRole,
    ProviderAgnosticPayload,
    RouterResolution,
    RoutingDecisionTrace,
    TraceContext,
)
from harness_cp.layer_budget import DEFAULT_LAYER_BUDGETS, LayerBudget
from harness_cp.routing_core_surface import (
    InferenceRequest,
    InferenceResponse,
    ProviderDispatchFn,
    ProviderDispatchResult,
    RouterResolutionFn,
    RoutingCandidateUnresolvedError,
    infer,
)
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoutingManifest
from pydantic import ValidationError


def _request() -> InferenceRequest:
    return InferenceRequest(
        agent_role=AgentRole("lead"),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        context_tokens=1024,
        request_payload=ProviderAgnosticPayload(messages=(), tools=None, params={}),
        trace_context=TraceContext(trace_id="t0", span_id="s0", trace_flags=0, trace_state=None),
    )


def _manifest() -> RoutingManifest:
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


def _result() -> ProviderDispatchResult:
    return ProviderDispatchResult(
        response_payload=ProviderAgnosticPayload(
            messages=({"role": "assistant", "content": "ok"},), tools=None, params={}
        ),
        tokens_in=7,
        tokens_out=3,
        cached_tokens_in=0,
    )


def _recording_dispatch() -> tuple[
    ProviderDispatchFn, list[tuple[str, str, RoutingDecisionTrace, str | None]]
]:
    calls: list[tuple[str, str, RoutingDecisionTrace, str | None]] = []

    async def _dispatch(
        provider: str,
        model: str,
        _payload: ProviderAgnosticPayload,
        trace: RoutingDecisionTrace,
        *,
        binding_rationale: str | None = None,
    ) -> ProviderDispatchResult:
        # C-CP-02 §2.5.4 — the dispatch seam additively accepts the optional
        # `binding_rationale` carrier (the Protocol-ization type-ripple fix).
        calls.append((provider, model, trace, binding_rationale))
        return _result()

    return _dispatch, calls


def test_infer_single_entry_point() -> None:
    """#1 — `infer` is the LLM-inference entry-point: positional request +
    kw-only routing/dispatch injection (R-300)."""
    sig = inspect.signature(infer)
    params = list(sig.parameters)
    assert params[0] == "request"
    assert sig.parameters["request"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    # Injected deps are keyword-only (workspace kw-only-Callable idiom).
    for name in ("dispatch", "manifest", "layer_decisions"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    # Activation lifts the stub — `infer` is a coroutine function now.
    assert inspect.iscoroutinefunction(infer)


def test_inference_request_routing_discriminators() -> None:
    """#2 — InferenceRequest carries agent_role, workload_class, persona_tier."""
    req = _request()
    assert req.agent_role == AgentRole("lead")
    assert req.workload_class is WorkloadClass.SOFTWARE_ENGINEERING
    assert req.persona_tier is PersonaTier.SOLO_DEVELOPER
    # 6-field API envelope.
    assert set(InferenceRequest.model_fields) == {
        "agent_role",
        "workload_class",
        "persona_tier",
        "context_tokens",
        "request_payload",
        "trace_context",
    }


def test_infer_emits_routing_attributes() -> None:
    """#3 — InferenceResponse.routing_decision carries the routing trace."""
    assert InferenceResponse.model_fields["routing_decision"].annotation is RoutingDecisionTrace
    assert set(InferenceResponse.model_fields) == {
        "provider_used",
        "model_used",
        "routing_decision",
        "response_payload",
        "tokens_in",
        "tokens_out",
        "cached_tokens_in",
    }


def test_infer_probabilistic_core_per_add_53() -> None:
    """#4 — `infer` is the probabilistic core per ADD §5.3.3."""
    assert "probabilistic core" in (infer.__doc__ or "")
    assert InferenceRequest.model_config.get("frozen") is True
    assert InferenceResponse.model_config.get("frozen") is True


# --- R-300 activation must_pass --------------------------------------------


async def test_infer_invokes_route() -> None:
    """must_pass #1 — `infer` invokes `route`; the DECLARATIVE layer decision
    fires and the routed provider/model + trace reach the dispatch callable;
    the response is materialized from the dispatch result."""
    decl_calls: list[RoutingLayer] = []

    def decl(_payload: ProviderAgnosticPayload, _m: RoutingManifest) -> str | None:
        decl_calls.append(RoutingLayer.DECLARATIVE)
        return "anthropic:claude-opus-4-8"

    dispatch, dispatch_calls = _recording_dispatch()

    response = await infer(
        _request(),
        dispatch=dispatch,
        manifest=_manifest(),
        layer_decisions={RoutingLayer.DECLARATIVE: decl},
    )

    # route() ran the DECLARATIVE layer.
    assert decl_calls == [RoutingLayer.DECLARATIVE]
    # The routed candidate was parsed and handed to the dispatch callable.
    assert dispatch_calls and dispatch_calls[0][0] == "anthropic"
    assert dispatch_calls[0][1] == "claude-opus-4-8"
    assert dispatch_calls[0][2].candidate == "anthropic:claude-opus-4-8"
    # The response is materialized from route() + dispatch result.
    assert response.provider_used == "anthropic"
    assert response.model_used == "claude-opus-4-8"
    assert response.tokens_in == 7
    assert response.tokens_out == 3
    assert isinstance(response.routing_decision, RoutingDecisionTrace)


async def test_infer_declarative_layer_is_manifest() -> None:
    """must_pass #2 — on a DECLARATIVE hit, routing_decision.layer == 'manifest'
    (`RoutingLayer.DECLARATIVE.value`)."""

    def decl(_payload: ProviderAgnosticPayload, _m: RoutingManifest) -> str | None:
        return "anthropic:claude-opus-4-8"

    dispatch, _ = _recording_dispatch()

    response = await infer(
        _request(),
        dispatch=dispatch,
        manifest=_manifest(),
        layer_decisions={RoutingLayer.DECLARATIVE: decl},
    )

    assert response.routing_decision.layer == "manifest"
    assert response.routing_decision.layer == RoutingLayer.DECLARATIVE.value


async def test_infer_binds_layer_budgets() -> None:
    """must_pass #3 — per-layer LayerBudget is bound per C-CP-03. The default
    is `DEFAULT_LAYER_BUDGETS`; an explicit tuple is honored and budget
    exhaustion at a layer falls through to the next."""
    # Default budgets are bound when not supplied.
    assert inspect.signature(infer).parameters["budgets"].default is DEFAULT_LAYER_BUDGETS

    embedding_calls: list[RoutingLayer] = []

    def decl(_payload: ProviderAgnosticPayload, _m: RoutingManifest) -> str | None:
        return "anthropic:declarative-model"

    def emb(_payload: ProviderAgnosticPayload, _m: RoutingManifest) -> str | None:
        embedding_calls.append(RoutingLayer.EMBEDDING)
        return "openai:embedding-model"

    dispatch, _ = _recording_dispatch()
    budgets: tuple[LayerBudget, ...] = DEFAULT_LAYER_BUDGETS

    # DECLARATIVE budget exhausted -> fall through to EMBEDDING.
    response = await infer(
        _request(),
        dispatch=dispatch,
        manifest=_manifest(),
        layer_decisions={RoutingLayer.DECLARATIVE: decl, RoutingLayer.EMBEDDING: emb},
        budgets=budgets,
        budget_exhausted=frozenset({RoutingLayer.DECLARATIVE}),
    )

    assert embedding_calls == [RoutingLayer.EMBEDDING]
    assert response.routing_decision.layer == RoutingLayer.EMBEDDING.value
    assert response.routing_decision.budget_exhausted is True
    assert response.provider_used == "openai"


async def test_infer_raises_on_unresolved_candidate() -> None:
    """No layer produces a candidate -> RoutingCandidateUnresolvedError (the
    dispatch callable is never invoked)."""
    dispatch, dispatch_calls = _recording_dispatch()

    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},  # no layer decides -> empty candidate
        )
    assert dispatch_calls == []


async def test_infer_raises_on_malformed_candidate() -> None:
    """A candidate without a 'provider:model' separator is rejected."""

    def decl(_payload: ProviderAgnosticPayload, _m: RoutingManifest) -> str | None:
        return "no-separator"

    dispatch, dispatch_calls = _recording_dispatch()

    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={RoutingLayer.DECLARATIVE: decl},
        )
    assert dispatch_calls == []


# --- R-FS-1 R-impl-1 — U-CP-99 + U-CP-100 (Layer-3 LLM_AS_ROUTER) ------------


def _router(
    candidate: str, rationale: str
) -> tuple[RouterResolutionFn, list[tuple[InferenceRequest, str]]]:
    """A mock RouterResolutionFn (NO paid call) recording its args."""
    seen: list[tuple[InferenceRequest, str]] = []

    async def _resolve(request: InferenceRequest, candidate_set_summary: str) -> RouterResolution:
        seen.append((request, candidate_set_summary))
        return RouterResolution(candidate=candidate, rationale=rationale)

    return _resolve, seen


def test_router_resolution_two_frozen_fields() -> None:
    """U-CP-99 / §2.5.1 — RouterResolution is a frozen two-field model
    (candidate, rationale); extra rejected; the trace is NOT widened (§2.5.4)."""
    r = RouterResolution(candidate="anthropic:claude-haiku-4-5", rationale="cost match")
    assert (r.candidate, r.rationale) == ("anthropic:claude-haiku-4-5", "cost match")
    assert set(RouterResolution.model_fields) == {"candidate", "rationale"}
    with pytest.raises(ValidationError):
        RouterResolution(candidate="a:b", rationale="x", extra="nope")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        r.candidate = "x:y"  # type: ignore[misc]
    # §2.5.4 anti-widening invariant — RoutingDecisionTrace stays four fields.
    assert set(RoutingDecisionTrace.model_fields) == {
        "layer",
        "candidate",
        "decision_ms",
        "budget_exhausted",
    }


def test_provider_dispatch_fn_accepts_optional_binding_rationale() -> None:
    """U-CP-99 / §2.5.4 — the Protocol-ized ProviderDispatchFn exposes the
    additive optional kw-only `binding_rationale` (the type-ripple shape: a
    closure WITH the defaulted param is the assignable seam)."""
    dispatch, _ = _recording_dispatch()
    # `_recording_dispatch` is annotated `-> ProviderDispatchFn` (pyright-checked
    # assignability is the type-ripple proof); the runtime shape is the optional
    # kw-only `binding_rationale` param. (ProviderDispatchFn is a structural
    # Protocol, not @runtime_checkable — no isinstance check.)
    sig = inspect.signature(dispatch)
    assert sig.parameters["binding_rationale"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["binding_rationale"].default is None
    # `infer` exposes the optional `router` kw-only param (U-CP-100).
    rp = inspect.signature(infer).parameters["router"]
    assert rp.kind is inspect.Parameter.KEYWORD_ONLY and rp.default is None


async def test_infer_layer3_router_resolves_sentinel() -> None:
    """U-CP-100 / §2.5.2 — empty layer_decisions -> the route() L3 sentinel; an
    injected router resolves it; the rebuilt trace carries layer=='llm_as_router'
    + the router candidate; the rationale is threaded to dispatch (§2.5.4)."""
    router, seen = _router("openai:gpt-x", "latency-floor")
    dispatch, dispatch_calls = _recording_dispatch()

    response = await infer(
        _request(),
        dispatch=dispatch,
        manifest=_manifest(),
        layer_decisions={},  # deterministic layers fall through -> L3 sentinel
        router=router,
    )

    assert response.routing_decision.layer == RoutingLayer.LLM_AS_ROUTER.value
    assert response.routing_decision.candidate == "openai:gpt-x"
    assert (response.provider_used, response.model_used) == ("openai", "gpt-x")
    # The router was consulted with the request + a (str) candidate_set_summary.
    assert len(seen) == 1 and isinstance(seen[0][1], str)
    # §2.5.4 — the router rationale was threaded to dispatch on the router path.
    assert dispatch_calls[0][3] == "latency-floor"


async def test_infer_non_router_path_threads_no_rationale() -> None:
    """U-CP-100 / §2.5.4 — a DECLARATIVE hit calls dispatch with
    binding_rationale=None even when a router is present-but-not-reached
    (the carrier is passed ONLY on the router path; byte-identical to HEAD)."""

    def decl(_p: ProviderAgnosticPayload, _m: RoutingManifest) -> str | None:
        return "anthropic:claude-opus-4-8"

    router, seen = _router("x:y", "z")
    dispatch, dispatch_calls = _recording_dispatch()
    await infer(
        _request(),
        dispatch=dispatch,
        manifest=_manifest(),
        layer_decisions={RoutingLayer.DECLARATIVE: decl},
        router=router,  # present, but DECLARATIVE resolves -> router NOT reached
    )
    assert seen == []  # router not consulted
    assert dispatch_calls[0][3] is None  # no rationale on the non-router path


async def test_infer_no_router_preserves_raise() -> None:
    """U-CP-100 / §2.5.2 no-regress (a) — L3 sentinel + no router -> the
    preserved RoutingCandidateUnresolvedError; dispatch never invoked."""
    dispatch, dispatch_calls = _recording_dispatch()
    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},
            router=None,
        )
    assert dispatch_calls == []


async def test_infer_l3_budget_exhausted_preserves_raise() -> None:
    """U-CP-100 / §2.5.3 no-regress (b) — L3 sentinel + router injected but
    LLM_AS_ROUTER pre-exhausted -> preserved raise; router NOT invoked (L3
    terminal)."""
    router, seen = _router("openai:gpt-x", "r")
    dispatch, dispatch_calls = _recording_dispatch()
    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},
            router=router,
            budget_exhausted=frozenset({RoutingLayer.LLM_AS_ROUTER}),
        )
    assert seen == []
    assert dispatch_calls == []


async def test_infer_router_timeout_preserves_raise() -> None:
    """U-CP-100 / §2.5.3 no-regress (c) — a router exceeding the L3 budget is
    interrupted at the budget and converted to L3 exhaustion -> preserved raise
    (timeout ENFORCEMENT, not assertion)."""

    async def _slow(_request: InferenceRequest, _summary: str) -> RouterResolution:
        await asyncio.sleep(0.05)
        return RouterResolution(candidate="openai:gpt-x", rationale="r")

    budgets = (
        LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
        LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
        LayerBudget(layer=RoutingLayer.LLM_AS_ROUTER, time_budget_ms=1),
    )
    dispatch, dispatch_calls = _recording_dispatch()
    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},
            router=_slow,
            budgets=budgets,
        )
    assert dispatch_calls == []


async def test_infer_router_malformed_candidate_raises() -> None:
    """U-CP-100 / §2.5.2 no-regress (d) — a router returning a malformed
    candidate re-raises via the reused well-formedness guard."""
    router, _ = _router("no-separator", "r")
    dispatch, dispatch_calls = _recording_dispatch()
    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},
            router=router,
        )
    assert dispatch_calls == []


async def _slow_router_30ms(_request: InferenceRequest, _summary: str) -> RouterResolution:
    """A router slow enough (30 ms) to exceed a 1 ms override budget but fast
    enough to resolve within a 5 s flat default — the discriminating delay."""
    await asyncio.sleep(0.03)
    return RouterResolution(candidate="openai:gpt-x", rationale="r")


async def test_infer_l3_honors_per_persona_budget_override() -> None:
    """B-LAYER-BUDGET-OVERRIDE (CP spec v1.43 §2.5.3 / §3.1) — the L3 router
    timeout honors the §3.1 per-persona-tier override (keyed on the request's
    `persona_tier`), NOT the flat `time_budget_ms`. §3.1 commits the per-persona
    tuning surface explicitly ON `llm_as_router`, so this materializes the
    cleared (built-but-vacuous) surface.

    Discriminating witness: with the SAME 30 ms router, a tiny 1 ms per-persona
    override governs -> raises; the flat default is set HUGE (5 s) so it alone
    could NEVER time the router out (a non-honoring impl would NOT raise here ->
    the test fails, not hangs). Negative control: no override -> the flat 5 s
    default governs -> the router resolves within budget -> success."""
    # `_request()` carries persona_tier=SOLO_DEVELOPER; the override targets it.
    override_budgets = (
        LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
        LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
        LayerBudget(
            layer=RoutingLayer.LLM_AS_ROUTER,
            time_budget_ms=5000,
            per_persona_override={PersonaTier.SOLO_DEVELOPER: 1},
        ),
    )
    dispatch, dispatch_calls = _recording_dispatch()
    # Override present -> the 1 ms persona budget governs -> the 30 ms router
    # times out -> L3 exhaustion -> the preserved raise. (If the override were
    # NOT honored, the 5 s flat default would let the router resolve -> no raise.)
    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},
            router=_slow_router_30ms,
            budgets=override_budgets,
        )
    assert dispatch_calls == []

    # Negative control: SAME router, NO override -> the flat 5 s default governs
    # -> the 30 ms router resolves within budget -> success (no raise, dispatched).
    flat_budgets = (
        LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
        LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
        LayerBudget(layer=RoutingLayer.LLM_AS_ROUTER, time_budget_ms=5000),
    )
    response = await infer(
        _request(),
        dispatch=dispatch,
        manifest=_manifest(),
        layer_decisions={},
        router=_slow_router_30ms,
        budgets=flat_budgets,
    )
    assert response.routing_decision.layer == RoutingLayer.LLM_AS_ROUTER.value
    assert response.routing_decision.candidate == "openai:gpt-x"
    assert len(dispatch_calls) == 1  # the negative-control path dispatched


async def test_infer_l3_honors_per_workload_budget_override() -> None:
    """B-LAYER-BUDGET-OVERRIDE — the L3 timeout also honors the §3.1
    per-workload-class override (keyed on the request's `workload_class`), which
    takes precedence over per-persona. `_request()` carries
    workload_class=SOFTWARE_ENGINEERING; a 1 ms override on it governs over the
    huge flat default -> the 30 ms router times out -> raise."""
    override_budgets = (
        LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
        LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
        LayerBudget(
            layer=RoutingLayer.LLM_AS_ROUTER,
            time_budget_ms=5000,
            per_workload_override={WorkloadClass.SOFTWARE_ENGINEERING: 1},
        ),
    )
    dispatch, dispatch_calls = _recording_dispatch()
    with pytest.raises(RoutingCandidateUnresolvedError):
        await infer(
            _request(),
            dispatch=dispatch,
            manifest=_manifest(),
            layer_decisions={},
            router=_slow_router_30ms,
            budgets=override_budgets,
        )
    assert dispatch_calls == []
