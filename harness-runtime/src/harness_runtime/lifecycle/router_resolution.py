"""R-impl-2 — the real Layer-3 LLM_AS_ROUTER resolution callable (C-CP-02 §2.5).

Impl-discretion realization (CP spec v1.36 §2.4 vendor deferral) of the
``RouterResolutionFn`` the R-impl-1 surface (U-CP-99/100 + U-RT-132/133) injects.
R-impl-1 proved the surface with a MOCK router; this binds a **real** router model
+ prompt. Free-local Ollama is preferred where it suffices (the gated live e2e
runs against local Ollama — no paid call); the same realization works for a paid
Haiku-class model by passing a different ``provider_name`` / ``model`` and the
matching adapter (surface, never auto-fire the paid call —
``[[feedback-background-agent-no-unilateral-paid-calls-or-secret-relocation]]``).

The router is a **terminal leaf** (§2.5.1): it dispatches DIRECTLY against its
pre-bound model and MUST NOT re-enter ``infer()`` / ``route()`` (the
infinite-regress guard). It takes ``(call_site_context, candidate_set_summary)``
and returns a ``RouterResolution`` (``candidate`` + ``rationale``). The
candidate's well-formedness is validated by ``infer()`` — a malformed return
re-raises ``RoutingCandidateUnresolvedError`` per §2.5.2 — so this realization
constrains the model to the candidate set and falls back to a candidate-set scan
of the raw output (small local models are imperfect JSON emitters).

Per §2.5.4 the router call is itself a billable LLM call, distinct from the
*workload* call it selects a binding for, so it **emits its own child
``llm.inference`` span** (the router model's). The router-call **cost-bucket
attribution** (router cost → the routed step vs. a distinct ``routing:`` bucket)
is the registered CA-arc forward item (CP plan v2.37 §6 O-CP-7 item 2), NOT here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from harness_cp.cp_shared_types import RouterResolution
from harness_cp.routing_core_surface import InferenceRequest, RouterResolutionFn

# An async chat callable: a list of provider-neutral ``{"role","content"}``
# messages in, the assistant's text reply out. Per-provider construction is the
# factory's job; the routing logic (prompt + parse + span) is provider-neutral.
_ChatFn = Callable[[list[dict[str, str]]], Awaitable[str]]

_ROUTER_SYSTEM_PROMPT = (
    "You are a routing model. Given a CANDIDATE SET of `provider:model` options "
    "and the call-site context, pick the single best candidate for the request "
    "and give a short (<=8 word) rationale. You MUST pick a candidate verbatim "
    "from the CANDIDATE SET. Respond with ONLY a JSON object, no prose: "
    '{"candidate": "<provider:model>", "rationale": "<short reason>"}'
)

# Matches a whole `provider:model` token (the model part may itself contain `.`
# and `:`, e.g. `ollama:llama3.2:1b`). The token MUST end in an alphanumeric so a
# trailing sentence period (`I pick ollama:llama3.2:1b.`) is excluded rather than
# captured into the token (which would false-negative the membership check; Codex
# [P2]). Used to tokenize the raw reply for EXACT candidate-set membership —
# substring matching would accept an in-set prefix of an off-list token
# (`openai:gpt-4` vs `openai:gpt-4o`; Codex [P2]).
_CANDIDATE_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]+:[A-Za-z0-9._/:-]*[A-Za-z0-9]")


def _parse_candidate_set(candidate_set_summary: str) -> tuple[str, ...]:
    """Split the §2.5.1 ``candidate_set_summary`` (a sorted comma-joined
    ``"provider:model"`` string) back into its members."""
    return tuple(c.strip() for c in candidate_set_summary.split(",") if c.strip())


def build_router_messages(
    request: InferenceRequest, candidate_set_summary: str
) -> list[dict[str, str]]:
    """Compose the router prompt — system instruction + a user turn carrying the
    candidate set and the call-site discriminators (§2.5.1 ``call_site_context``).
    Deterministic given inputs (testable without a model call)."""
    user = (
        f"CANDIDATE SET: {candidate_set_summary}\n"
        f"CALL SITE: agent_role={request.agent_role}, "
        f"workload_class={request.workload_class}, "
        f"persona_tier={request.persona_tier}, "
        f"context_tokens={request.context_tokens}\n"
        "Pick the best candidate from the CANDIDATE SET."
    )
    return [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_router_response(text: str, candidate_set_summary: str) -> tuple[str, str]:
    """Parse the router model's reply into ``(candidate, rationale)``.

    The candidate set is the **eligible-universe authorization boundary** (the
    manifest-derived `provider:model` universe the prompt constrains the model
    to). A pick is valid ONLY if it is a candidate-set member — an out-of-set
    (or no-set) pick is treated as **unresolved** (``candidate == ""`` →
    ``infer()`` raises ``RoutingCandidateUnresolvedError`` per §2.5.2), so a
    hallucinated/off-list model can never be dispatched (Codex R-impl-2 [P2]).

    Robust to small-model JSON-noncompliance: (1) JSON-decode the first ``{...}``
    block; (2) accept a parsed ``candidate`` that is a set member; (3) else scan
    the raw text for the first set member; (4) else ``""``. The rationale comes
    from the JSON when present, else a trimmed snippet of the raw output."""
    members = _parse_candidate_set(candidate_set_summary)
    member_set = set(members)

    parsed_candidate = ""
    parsed_rationale = ""
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace is not None:
        try:
            obj = cast(Mapping[str, Any], json.loads(brace.group(0)))
            c = obj.get("candidate")
            r = obj.get("rationale")
            if isinstance(c, str):
                parsed_candidate = c.strip()
            if isinstance(r, str):
                parsed_rationale = r.strip()
        except (json.JSONDecodeError, AttributeError):
            pass

    # Constrain to the candidate set — only a member is a valid pick.
    if parsed_candidate in member_set:
        candidate = parsed_candidate
    else:
        # Else scan the raw output for a WHOLE `provider:model` token that is
        # EXACTLY a set member (not a substring — an in-set prefix of an off-list
        # token must NOT match). If none appears (out-of-set / no-set / garbage),
        # the pick is unresolved ("").
        candidate = next((t for t in _CANDIDATE_TOKEN_RE.findall(text) if t in member_set), "")

    rationale = parsed_rationale or (text.strip()[:80] if text.strip() else "llm-as-router")
    return candidate, rationale


def make_llm_router(
    *,
    chat: _ChatFn,
    provider_name: str,
    model: str,
    tracer_provider: Any,
) -> RouterResolutionFn:
    """Build a real ``RouterResolutionFn`` from a provider-neutral async chat
    callable. The returned router emits its own child ``llm.inference`` span
    (§2.5.4) around the router-model call, then parses the reply. Terminal leaf
    — no ``infer()`` / ``route()`` re-entry."""

    async def _router(request: InferenceRequest, candidate_set_summary: str) -> RouterResolution:
        messages = build_router_messages(request, candidate_set_summary)
        # §2.5.4 — the router call's OWN child `llm.inference` span (the router
        # model's call), distinct from the workload span it selects a binding
        # for. Span name per OD §C-OD-04 §4.1 (`{operation} {model}`). §2.5.4
        # phrases it as "child of the routing decision", but the routing decision
        # is a `RoutingDecisionTrace` RECORD, not a span — so this span parents to
        # whatever span is current at the call site (the surrounding workflow/step
        # span in production), the best available realization of the intent.
        tracer = tracer_provider.get_tracer("harness.runtime.router_resolution")
        with tracer.start_as_current_span(f"chat {model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.provider.name", provider_name)
            span.set_attribute("gen_ai.request.model", model)
            # Marks this as the Layer-3 router-meta call (vs. a workload call);
            # the router-call cost-bucket discrimination is the CA-arc forward
            # item (CP plan v2.37 §6 O-CP-7 item 2).
            span.set_attribute("routing.role", "llm_as_router")
            text = await chat(messages)
        candidate, rationale = parse_router_response(text, candidate_set_summary)
        return RouterResolution(candidate=candidate, rationale=rationale)

    return _router


def _ollama_assistant_text(response: Any) -> str:
    """Extract the assistant text from an Ollama ``ChatResponse`` (pydantic v2
    model or a Mapping stub) — ``response.message.content``."""
    message = getattr(response, "message", None)
    if message is None and isinstance(response, Mapping):
        message = cast(Mapping[str, Any], response).get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, Mapping):
        content = cast(Mapping[str, Any], message).get("content")
    return content if isinstance(content, str) else ""


def make_ollama_router(
    *,
    adapter: Any,
    model: str,
    tracer_provider: Any,
) -> RouterResolutionFn:
    """The free-local-Ollama realization of the Layer-3 router (the vendor gate's
    preferred path — no paid call). ``adapter`` is the ``ctx.providers["ollama"]``
    client (``adapter.client.chat`` is the Ollama AsyncClient chat method, the
    same surface ``_dispatch_ollama`` uses)."""

    async def _chat(messages: list[dict[str, str]]) -> str:
        # `format="json"` forces valid-JSON output (the parser's happy path);
        # `num_predict` caps the reply (a router pick is tiny) so the call stays
        # inside the L3 budget; `temperature=0` makes the pick deterministic.
        response = await adapter.client.chat(
            model=model,
            messages=messages,
            format="json",
            options={"temperature": 0, "num_predict": 128},
        )
        return _ollama_assistant_text(response)

    return make_llm_router(
        chat=_chat,
        provider_name="ollama",
        model=model,
        tracer_provider=tracer_provider,
    )
