"""Tests for U-CP-05 — layered routing strategy (C-CP-02 §2.1/§2.2).

Acceptance-criterion coverage:
  #1 RoutingLayer cardinality 3   -> test_routing_layer_cardinality_three
  #2 layer ordering fixed         -> test_layer_ordering_fixed
  #3 budget exhaustion fall-thru  -> test_budget_exhaustion_triggers_fall_through
  #4 emits routing.* per layer    -> test_emits_routing_attributes_per_layer
  #5 declarative short-circuits   -> test_declarative_short_circuits
"""

from __future__ import annotations

from harness_cp.cp_shared_types import ProviderAgnosticPayload
from harness_cp.layered_routing_strategy import (
    LAYER_ORDER,
    InferenceRequest,
    route,
)
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoutingManifest


def _request() -> InferenceRequest:
    return ProviderAgnosticPayload(messages=(), tools=None, params={})


def _manifest() -> RoutingManifest:
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


def test_routing_layer_cardinality_three() -> None:
    assert len(RoutingLayer) == 3


def test_layer_ordering_fixed() -> None:
    assert LAYER_ORDER == (
        RoutingLayer.DECLARATIVE,
        RoutingLayer.EMBEDDING,
        RoutingLayer.LLM_AS_ROUTER,
    )


def test_declarative_short_circuits() -> None:
    calls: list[RoutingLayer] = []

    def decl(_req: InferenceRequest, _m: RoutingManifest) -> str | None:
        calls.append(RoutingLayer.DECLARATIVE)
        return "anthropic:claude-opus"

    def emb(_req: InferenceRequest, _m: RoutingManifest) -> str | None:
        calls.append(RoutingLayer.EMBEDDING)
        return "should-not-run"

    trace = route(
        _request(),
        _manifest(),
        {RoutingLayer.DECLARATIVE: decl, RoutingLayer.EMBEDDING: emb},
    )
    assert trace.candidate == "anthropic:claude-opus"
    assert trace.layer == RoutingLayer.DECLARATIVE.value
    # No embedding computation on a declarative hit.
    assert calls == [RoutingLayer.DECLARATIVE]


def test_budget_exhaustion_triggers_fall_through() -> None:
    def emb(_req: InferenceRequest, _m: RoutingManifest) -> str | None:
        return "openai:gpt-4o"

    # DECLARATIVE budget exhausted -> fall through to EMBEDDING.
    trace = route(
        _request(),
        _manifest(),
        {RoutingLayer.EMBEDDING: emb},
        budget_exhausted=frozenset({RoutingLayer.DECLARATIVE}),
    )
    assert trace.layer == RoutingLayer.EMBEDDING.value
    assert trace.candidate == "openai:gpt-4o"
    assert trace.budget_exhausted is True


def test_emits_routing_attributes_per_layer() -> None:
    # The trace carries the deciding layer + candidate (routing.* attributes).
    def llm(_req: InferenceRequest, _m: RoutingManifest) -> str | None:
        return "ollama:llama3"

    trace = route(
        _request(),
        _manifest(),
        {RoutingLayer.LLM_AS_ROUTER: llm},
    )
    assert trace.layer == RoutingLayer.LLM_AS_ROUTER.value
    assert trace.candidate == "ollama:llama3"
