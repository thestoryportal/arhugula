"""Unit tests for the R-impl-2 real Layer-3 router (`router_resolution.py`).

Pure (no live model call) — proves the prompt composition, the small-model-robust
response parsing, the RouterResolution shape, and the router's OWN child
`llm.inference` span (§2.5.4). The live-Ollama exercise is the gated
`@pytest.mark.e2e` test in `test_lifecycle_llm_dispatch.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from harness_core.persona_tier import PersonaTier
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import (
    AgentRole,
    ProviderAgnosticPayload,
    RouterResolution,
    TraceContext,
)
from harness_cp.routing_core_surface import InferenceRequest
from harness_runtime.lifecycle.router_resolution import (
    _ollama_assistant_text,
    build_router_messages,
    make_llm_router,
    make_ollama_router,
    parse_router_response,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_SUMMARY = "anthropic:claude-haiku-4-5, ollama:llama3.2:1b, openai:gpt-5.5"


def _request() -> InferenceRequest:
    return InferenceRequest(
        agent_role=AgentRole("worker-a"),
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        context_tokens=3,
        request_payload=ProviderAgnosticPayload(messages=(), tools=None, params={}),
        trace_context=TraceContext(trace_id="t", span_id="s", trace_flags=0, trace_state=None),
    )


def _tracer_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp, exporter


# --- build_router_messages --------------------------------------------------


def test_build_router_messages_shape() -> None:
    messages = build_router_messages(_request(), _SUMMARY)
    assert [m["role"] for m in messages] == ["system", "user"]
    # The candidate set + the call-site discriminators ride the user turn.
    user = messages[1]["content"]
    assert _SUMMARY in user
    assert "agent_role=worker-a" in user
    assert "workload_class=" in user and "persona_tier=" in user


# --- parse_router_response --------------------------------------------------


def test_parse_clean_json_in_set() -> None:
    text = '{"candidate": "ollama:llama3.2:1b", "rationale": "cheap local"}'
    candidate, rationale = parse_router_response(text, _SUMMARY)
    assert candidate == "ollama:llama3.2:1b"
    assert rationale == "cheap local"


def test_parse_json_wrapped_in_prose_still_decodes() -> None:
    # Small models often wrap the JSON in chatter; the first {...} block decodes.
    text = 'Sure! Here is my pick:\n{"candidate": "openai:gpt-5.5", "rationale": "best"}\nThanks'
    candidate, rationale = parse_router_response(text, _SUMMARY)
    assert candidate == "openai:gpt-5.5"
    assert rationale == "best"


def test_parse_json_candidate_not_in_set_falls_back_to_scan() -> None:
    # JSON candidate is not a set member, but a real member appears in the prose
    # → the candidate-set scan wins over the parsed (non-member) candidate.
    text = (
        '{"candidate": "ollama:not-a-real-model", "rationale": "r"} use anthropic:claude-haiku-4-5'
    )
    candidate, rationale = parse_router_response(text, _SUMMARY)
    assert candidate == "anthropic:claude-haiku-4-5"
    assert rationale == "r"


def test_parse_prose_only_scans_for_set_member() -> None:
    text = "I would route this to ollama:llama3.2:1b for cost reasons."
    candidate, rationale = parse_router_response(text, _SUMMARY)
    assert candidate == "ollama:llama3.2:1b"
    # No JSON rationale → a trimmed snippet of the raw output.
    assert rationale.startswith("I would route")


def test_parse_prose_with_trailing_period_still_matches() -> None:
    # A sentence-ending period must not be captured into the token (Codex [P2]).
    candidate, _ = parse_router_response("I pick ollama:llama3.2:1b.", _SUMMARY)
    assert candidate == "ollama:llama3.2:1b"


def test_parse_garbage_yields_empty_candidate_for_infer_to_reject() -> None:
    # No JSON, no set member, no provider:model token → "" so infer() raises
    # RoutingCandidateUnresolvedError (§2.5.2). Rationale never empty.
    candidate, rationale = parse_router_response("I cannot help with that.", _SUMMARY)
    assert candidate == ""
    assert rationale != ""


def test_parse_out_of_set_candidate_is_rejected() -> None:
    # The candidate set is the eligible-universe authorization boundary: a
    # well-formed token NOT in the set (even cleanly JSON-returned) is rejected →
    # "" so infer() raises, never dispatched (Codex R-impl-2 [P2]).
    candidate, _ = parse_router_response(
        '{"candidate": "cohere:command-r", "rationale": "r"}', _SUMMARY
    )
    assert candidate == ""


def test_parse_empty_candidate_set_is_unresolved() -> None:
    # No eligible universe → no valid pick → "" (fail-closed; never routes to an
    # unconstrained off-manifest model).
    candidate, _ = parse_router_response("route to cohere:command-r please", "")
    assert candidate == ""


def test_parse_scan_rejects_in_set_prefix_of_off_list_token() -> None:
    # `openai:gpt-4` is in-set; the model named the OFF-LIST `openai:gpt-4o`.
    # Whole-token exact matching must NOT accept the in-set prefix (Codex [P2]).
    candidate, _ = parse_router_response("I pick openai:gpt-4o", "openai:gpt-4, ollama:llama3.2:1b")
    assert candidate == ""
    # The genuine whole token IS matched.
    candidate2, _ = parse_router_response("I pick openai:gpt-4", "openai:gpt-4, ollama:llama3.2:1b")
    assert candidate2 == "openai:gpt-4"


# --- _ollama_assistant_text -------------------------------------------------


def test_ollama_assistant_text_from_object() -> None:
    response = SimpleNamespace(message=SimpleNamespace(content="hello"))
    assert _ollama_assistant_text(response) == "hello"


def test_ollama_assistant_text_from_mapping() -> None:
    response = {"message": {"content": "hi"}}
    assert _ollama_assistant_text(response) == "hi"


def test_ollama_assistant_text_missing_is_empty() -> None:
    assert _ollama_assistant_text(SimpleNamespace()) == ""


# --- make_llm_router (fake chat; proves the span + the resolution) -----------


@pytest.mark.asyncio
async def test_make_llm_router_returns_resolution_and_emits_own_span() -> None:
    tp, exporter = _tracer_with_exporter()

    async def _chat(messages: list[dict[str, str]]) -> str:
        # The router prompt reached the chat fn (system + user).
        assert messages[0]["role"] == "system"
        return '{"candidate": "ollama:llama3.2:1b", "rationale": "cost-tier match"}'

    router = make_llm_router(
        chat=_chat, provider_name="ollama", model="llama3.2:1b", tracer_provider=tp
    )
    resolution = await router(_request(), _SUMMARY)

    assert isinstance(resolution, RouterResolution)
    assert resolution.candidate == "ollama:llama3.2:1b"
    assert resolution.rationale == "cost-tier match"

    # §2.5.4 — the router emits its OWN child `llm.inference` span (the router
    # model's call), distinct from any workload span, marked routing.role.
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert spans[0].name == "chat llama3.2:1b"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "ollama"
    assert attrs["gen_ai.request.model"] == "llama3.2:1b"
    assert attrs["routing.role"] == "llm_as_router"


@pytest.mark.asyncio
async def test_make_ollama_router_calls_adapter_client_chat() -> None:
    # `make_ollama_router` wires `adapter.client.chat(model=, messages=)` as the
    # chat fn (the same surface `_dispatch_ollama` uses) — proven with a fake
    # client (no live call), so the factory wiring is CI-covered.
    tp, _ = _tracer_with_exporter()
    captured: dict[str, object] = {}

    class _FakeOllamaClient:
        async def chat(self, *, model: str, messages: list[dict[str, str]], **kwargs: object):
            captured["model"] = model
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                message=SimpleNamespace(
                    content='{"candidate": "ollama:llama3.2:1b", "rationale": "r"}'
                )
            )

    adapter = SimpleNamespace(client=_FakeOllamaClient())
    router = make_ollama_router(adapter=adapter, model="llama3.2:1b", tracer_provider=tp)
    resolution = await router(_request(), _SUMMARY)

    assert captured["model"] == "llama3.2:1b"
    assert resolution.candidate == "ollama:llama3.2:1b"
