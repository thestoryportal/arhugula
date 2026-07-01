"""Runtime `POST_JOIN_SYNTHESIS` step dispatcher (R-FS-1 arc B-POSTJOIN-LLM-SYNTHESIS).

Per CP spec v1.54 §5.2/§25.2/§3 + the paired runtime contract: the opt-in
terminal `StepKind.POST_JOIN_SYNTHESIS` step's body is an **LLM-composed
synthesis** over a concurrent fan-out's branch-index-ordered sibling outputs.
The CP driver carves the terminal synthesis step out of the branch set, drains
the fan-out barrier, then dispatches this step SYNC post-barrier supplying the
siblings on `StepExecutionContext.sibling_outputs` (CP spec v1.54 §3); this
dispatcher composes them into the synthesis step's LLM input and dispatches
through the inner LLM dispatcher (the same C-RT-16 `RetryBreakerFallbackDispatcher`
chain `INFERENCE_STEP` uses, wrapped in the stage-5 `SyncDispatcherFacade`).

**Read-only / effect-free** (CP spec v1.54 change-note): a pure read-of-siblings
+ compose; no effect-fence-carrying tool dispatch. The non-determinism of the LLM
compose is the §25.12 Point-2 (aggregator-purity) sacrifice, disclosed at the CP
driver's synthesis step ledger entry + trace event — NOT here.

**Minimal dispatch** (CP spec v1.54 "out of scope: operator-supplied prompt
templating beyond the minimal synthesis dispatch"): the siblings are appended as
one branch-index-ordered context `user` message AFTER the synthesis step's own
declared `payload.messages` (the operator's synthesis instruction). Reducer DSLs /
configurable templating are the registered follow-on, not this arc.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import WorkflowStep

__all__ = [
    "POST_JOIN_EFFECT_BEARING_PARAM_KEYS",
    "POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX",
    "PostJoinSynthesisStepDispatcher",
    "post_join_tool_binding_violations",
]

#: Prefix labelling the injected branch-index-ordered sibling-output context
#: message, so a model (and a test) can distinguish the synthesis material from
#: the step's own declared synthesis-instruction messages (mirrors the B-INTERSTEP
#: `_UPSTREAM_CONTEXT_PREFIX` convention).
POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX = "[post-join siblings]\n"

#: Provider tool-binding keys (anthropic / openai / ollama) that — if present in
#: `step_payload.params` — the LLM translators merge into the provider call via
#: `kwargs.update(payload.params)` (llm_dispatch.py:1522/1569/1592, AFTER the
#: `payload.tools` assignment). On a read-only / effect-free synthesis step they are
#: forbidden by EVERY route (top-level `tools` AND `params`): they would bind tools to
#: the provider call, letting the model emit/enter tool use — violating the §25.12
#: effect-free property + the unconditional READ_ONLY blast-radius classification. The
#: set is complete for the committed 3-provider stack (anthropic `mcp_servers`/`tools`/
#: `tool_choice`; openai `tools`/`tool_choice`/legacy `functions`/`function_call`;
#: ollama `tools`).
POST_JOIN_EFFECT_BEARING_PARAM_KEYS = (
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "mcp_servers",
)


def post_join_tool_binding_violations(tools: Any, params: Any) -> list[str]:
    """Return the tool-binding keys present on a (would-be) synthesis payload — the
    top-level ``tools`` (when truthy) + any `POST_JOIN_EFFECT_BEARING_PARAM_KEYS` present
    in ``params``. Non-empty ⇒ the payload is NOT effect-free.

    The SINGLE source of truth for "what makes a synthesis non-effect-free," shared by
    two guard sites (out-of-family Codex round 8 [P1]): (1) the compose-time EARLY guard
    here (clear error on the raw step_payload, the common non-HITL case) and (2) the
    LOAD-BEARING boundary guard at the production LLM dispatch (`RuntimeLLMDispatcher`,
    keyed on `step_kind is POST_JOIN_SYNTHESIS`, post-`_coerce_payload`) — the convergence
    point DOWNSTREAM of a HITL PRE_ACTION EDIT, which replaces `step.step_payload` verbatim
    AFTER the compose guard ran and so can re-introduce tools. Compose-time enforcement
    structurally cannot see the edited payload; the boundary is the real floor
    (`[[enforce-floor-no-bypass-seam]]`)."""
    violations: list[str] = []
    if tools:
        violations.append("tools")
    if isinstance(params, Mapping):
        params_map = cast("Mapping[str, Any]", params)
        violations.extend(k for k in POST_JOIN_EFFECT_BEARING_PARAM_KEYS if params_map.get(k))
    return violations


def _compose_synthesis_payload(
    payload: Mapping[str, Any],
    siblings: Sequence[tuple[int, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Append the branch-index-ordered sibling outputs as one context ``user``
    message to the synthesis step's payload ``messages``.

    The siblings are the material the model composes; they are appended AFTER the
    step's declared messages (the operator's synthesis instruction). Deterministic
    serialization (`sort_keys`, `default=str`) keeps the injected content stable;
    the dispatcher does NOT introspect the opaque step body otherwise (the
    `workflow_driver` §25.3.3.4 step-body-opaque discipline)."""
    composed = dict(payload)
    # Compose-time EARLY effect-free guard (clear error for the common non-HITL case;
    # rounds 5/6 [P1] + adversarial F1). The synthesis is read-only / effect-free (the
    # §25.12 safety argument + at-most-once-safe re-dispatch + READ_ONLY blast-radius all
    # rest on it). A tool-bearing payload — top-level `tools` OR `params['tools']` /
    # `tool_choice` / `mcp_servers` / legacy `functions` (the LLM translators merge
    # `kwargs.update(payload.params)` AFTER setting tools) — would enter the model tool
    # loop + dispatch real effects. The LOAD-BEARING enforcement is the boundary guard at
    # the production LLM dispatch (round 8 [P1]) — it re-runs `post_join_tool_binding_
    # violations` POST-HITL-edit, which this compose-time guard cannot see; this is the
    # early/clear half (`[[enforce-floor-no-bypass-seam]]`).
    _tool_violations = post_join_tool_binding_violations(
        composed.get("tools"), composed.get("params")
    )
    if _tool_violations:
        raise ValueError(
            "post-join-synthesis payload may not declare provider tool-binding "
            f"({', '.join(_tool_violations)}): the synthesis step is read-only / effect-free "
            "(a pure compose of the fan-out siblings); tools would enter the model tool loop "
            "and dispatch real effects, violating the §25.12 effect-free property + the "
            "READ_ONLY blast-radius classification. (Re-enforced at the LLM dispatch boundary "
            "post-HITL-edit.)"
        )
    # `params['messages']` clobber guard (a SEPARATE concern — sibling-loss, not effect):
    # the LLM translators do `kwargs.update(payload.params)`, so a `params['messages']`
    # escape-hatch would OVERWRITE the appended sibling context (the model would receive NO
    # branch outputs while the run still reports a synthesized final state). The synthesis
    # OWNS its messages; reject fail-closed.
    _params = composed.get("params")
    if isinstance(_params, Mapping) and "messages" in cast("Mapping[str, Any]", _params):
        raise ValueError(
            "post-join-synthesis payload may not set params['messages']: the provider "
            "escape-hatch overwrites the appended sibling-context message (the model "
            "would receive no branch outputs). Put the synthesis instruction in "
            "payload['messages'] instead."
        )
    messages: list[Any] = list(composed.get("messages", ()))
    sibling_message: dict[str, Any] = {
        "role": "user",
        "content": POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX
        + json.dumps(
            [{"branch_index": bi, "output": dict(out)} for bi, out in siblings],
            sort_keys=True,
            default=str,
        ),
    }
    composed["messages"] = [*messages, sibling_message]
    # Out-of-family Codex round 8 [P2]: the production inner `RuntimeLLMDispatcher`
    # coerces `step_payload` via `ProviderAgnosticPayload.model_validate` (frozen,
    # extra="forbid"), where `tools` and `params` are REQUIRED fields. Force `tools=None`
    # — the synthesis is effect-free, so tools is ALWAYS None here, NOT author-controlled
    # (out-of-family Codex round 8 [P2]). `params` is author-supplied, exactly like EVERY
    # inference step: a synthesis payload is a normal inference payload (the harness only
    # appends the siblings + forces tools-free); the operator supplies `messages` +
    # `params` (e.g. Anthropic `messages.create` REQUIRES `params['max_tokens']`). The
    # realistic minimal shape is `{"messages": [...], "params": {"max_tokens": N}}`, NOT
    # messages-only — "minimal" means no reducer DSL / no configurable templating, not
    # omitting provider-required params (out-of-family Codex round 9 [P2]: a manufactured
    # `params={}` coerces locally but the real Anthropic call fails for want of max_tokens;
    # do NOT fabricate a provider-specific default in this provider-agnostic dispatcher).
    composed["tools"] = None
    return composed


class PostJoinSynthesisStepDispatcher:
    """`StepDispatcher` for `StepKind.POST_JOIN_SYNTHESIS` (CP spec v1.54).

    Wraps the inner LLM dispatcher (the stage-5 `inference_step_dispatcher`
    `SyncDispatcherFacade` — already sync), so this dispatcher is itself sync and
    needs no further facade. On `dispatch`: read the branch-index-ordered siblings
    from `step_context.sibling_outputs`, compose them into the synthesis step's LLM
    input, and dispatch the composed step through the inner LLM dispatcher.
    Satisfies the `@runtime_checkable` `StepDispatcher` Protocol (sync
    `dispatch(binding, step, *, step_context)`)."""

    def __init__(self, *, inner: StepDispatcher) -> None:
        self._inner = inner

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> Mapping[str, Any]:
        siblings: Sequence[tuple[int, Mapping[str, Any]]] = (
            getattr(step_context, "sibling_outputs", None) or ()
        )
        composed_payload = _compose_synthesis_payload(step.step_payload, siblings)
        synthesis_step = step.model_copy(update={"step_payload": composed_payload})
        return self._inner.dispatch(binding, synthesis_step, step_context=step_context)
