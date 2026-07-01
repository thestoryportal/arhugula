"""R-impl-2 — the L3 LLM_AS_ROUTER vendor gate, free-local Ollama live e2e.

R-impl-1 proved the Layer-3 resolution SURFACE with a MOCK router (no live call).
THIS test removes the mock: a **real** Ollama-backed `RouterResolutionFn`
(`make_ollama_router`) resolves Layer 3 end-to-end through the real `infer()`
routing core against a **live local Ollama model** — the vendor gate's preferred
free-local path (no paid call, zero secret; `[[feedback-run-credential-gated-
live-e2e-authorized]]`). The paid Haiku-class path is the same realization with a
different adapter/model and is **surfaced, never auto-fired**
(`[[feedback-background-agent-no-unilateral-paid-calls-or-secret-relocation]]`).

It calls `infer()` DIRECTLY (not through `RuntimeLLMDispatcher`) for one reason:
the dispatcher hardcodes `DEFAULT_LAYER_BUDGETS` (`llm_dispatch.py:584`) whose
LLM_AS_ROUTER budget is **200 ms** (CP spec §2.5.3 / §2.2's "50-200 ms" estimate
for a fast hosted router). A real LOCAL model call exceeds 200 ms, so through the
dispatcher the L3 `asyncio.wait_for` would time out → the no-regress raise, not a
resolution. This test passes a **realistic** L3 budget to `infer()` to exercise
the *resolution* path. That the production default is too tight for a real
(esp. local) router — and that an operator would tune it per-deployment — is
exactly the load-bearing case for the registered forward arc **B-LAYER-BUDGET-
OVERRIDE** (`.harness/beyond-mvp-capability-boundary-ledger.md`); the dispatcher
budget-threading is part of that arc, NOT R-impl-2.

Authority: CP spec v1.36 §2.5 / §2.5.5 (R-impl-2 vendor gate); the R-impl-1 units
U-CP-99/100 + U-RT-132/133 carry the surface this exercises live.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest
from harness_core import PersonaTier, WorkloadClass
from harness_cp.cp_shared_types import (
    AgentRole,
    ModelBinding,
    ProviderAgnosticPayload,
    RoutingDecisionTrace,
    TraceContext,
)
from harness_cp.layer_budget import LayerBudget
from harness_cp.routing_core_surface import (
    InferenceRequest,
    ProviderDispatchResult,
    infer,
)
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoleRoutingBinding, RoutingManifest
from harness_runtime.lifecycle.router_resolution import make_ollama_router
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytestmark = pytest.mark.e2e

_OLLAMA_HOST = "http://127.0.0.1:11434"
_ROUTER_MODEL = "llama3.2:3b"  # the router model (decent instruction-following)
_WORKLOAD_MODEL = "llama3.2:1b"  # the routed candidate (fast)
# Realistic L3 budget for a real local model — the 200 ms production default
# (B-LAYER-BUDGET-OVERRIDE) is too tight for local Ollama.
_BUDGETS = (
    LayerBudget(layer=RoutingLayer.DECLARATIVE, time_budget_ms=5),
    LayerBudget(layer=RoutingLayer.EMBEDDING, time_budget_ms=50),
    LayerBudget(layer=RoutingLayer.LLM_AS_ROUTER, time_budget_ms=60_000),
)


def _router_model_available() -> bool:
    """Gate on BOTH the Ollama daemon being reachable AND the required router
    model being pulled — a daemon-only gate would let the test proceed and fail
    with a model-not-found error when `_ROUTER_MODEL` is absent (Codex [P2])."""
    try:
        with urllib.request.urlopen(f"{_OLLAMA_HOST}/api/tags", timeout=3) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    names = {m.get("name") for m in payload.get("models", [])}
    return _ROUTER_MODEL in names


def _force_fallthrough(_payload: object, _manifest: object) -> str | None:
    """A DECLARATIVE LayerDecisionFn that yields no candidate → `route()` falls
    through to the LLM_AS_ROUTER sentinel so the injected real router is reached."""
    return None


def _envelope() -> InferenceRequest:
    return InferenceRequest(
        agent_role=AgentRole("worker-a"),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        context_tokens=1,
        request_payload=ProviderAgnosticPayload(
            messages=({"role": "user", "content": "ping"},), tools=None, params={}
        ),
        trace_context=TraceContext(trace_id="t", span_id="s", trace_flags=0, trace_state=None),
    )


def _manifest_with_ollama_candidate() -> RoutingManifest:
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("worker-a"): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider="ollama", model=_WORKLOAD_MODEL),
                layer_budget_overrides={},
            )
        },
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


@pytest.mark.asyncio
async def test_real_ollama_router_resolves_layer3_e2e() -> None:
    """A real Ollama router resolves the L3 sentinel end-to-end through `infer()`:
    `route()` falls through → the real router makes a LIVE model call → returns a
    candidate from the set → `infer()` dispatches it with the router-supplied
    `binding_rationale` (§2.5.4), and the router emits its own child span (§2.5.4).
    """
    if not _router_model_available():
        pytest.skip(f"local Ollama or router model {_ROUTER_MODEL!r} not available")

    import ollama

    adapter = SimpleNamespace(client=ollama.AsyncClient(host=_OLLAMA_HOST))
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    router = make_ollama_router(adapter=adapter, model=_ROUTER_MODEL, tracer_provider=tp)

    # Warm up the router model OUTSIDE the timed `infer()` call — Ollama's
    # cold-start (model load from disk) otherwise dominates the L3 budget. The
    # timed call below then measures only warm inference.
    await adapter.client.chat(
        model=_ROUTER_MODEL,
        messages=[{"role": "user", "content": "warmup"}],
        options={"num_predict": 1},
    )

    captured: dict[str, Any] = {}

    async def _recording_dispatch(
        provider: str,
        model: str,
        payload: ProviderAgnosticPayload,
        trace: RoutingDecisionTrace,
        /,
        *,
        binding_rationale: str | None = None,
    ) -> ProviderDispatchResult:
        captured["provider"] = provider
        captured["model"] = model
        captured["trace"] = trace
        captured["binding_rationale"] = binding_rationale
        return ProviderDispatchResult(
            response_payload=ProviderAgnosticPayload(messages=(), tools=None, params={}),
            tokens_in=0,
            tokens_out=0,
            cached_tokens_in=0,
        )

    await infer(
        _envelope(),
        dispatch=_recording_dispatch,
        manifest=_manifest_with_ollama_candidate(),
        layer_decisions={RoutingLayer.DECLARATIVE: _force_fallthrough},
        budgets=_BUDGETS,
        router=router,
    )

    # The real router resolved Layer 3 → the dispatch saw the LLM_AS_ROUTER layer
    # + the router-resolved candidate (the single ollama set member) + a
    # router-supplied rationale (the router path threads `resolution.rationale`,
    # never the f"{layer}:{candidate}" fallback).
    assert captured["trace"].layer == RoutingLayer.LLM_AS_ROUTER.value
    assert captured["provider"] == "ollama"
    assert captured["model"] == _WORKLOAD_MODEL
    assert isinstance(captured["binding_rationale"], str) and captured["binding_rationale"]

    # §2.5.4 — the router emitted its OWN child `llm.inference` span (the live
    # router-model call), distinct from the workload dispatch.
    router_spans = [
        s
        for s in exporter.get_finished_spans()
        if (s.attributes or {}).get("routing.role") == "llm_as_router"
    ]
    assert len(router_spans) == 1
    assert router_spans[0].name == f"chat {_ROUTER_MODEL}"
