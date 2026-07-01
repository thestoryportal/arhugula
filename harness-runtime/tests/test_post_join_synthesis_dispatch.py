"""Unit witnesses for the runtime `PostJoinSynthesisStepDispatcher`
(R-FS-1 arc B-POSTJOIN-LLM-SYNTHESIS; CP spec v1.54 §3).

These prove the dispatcher composes the branch-index-ordered sibling outputs
into the synthesis step's LLM input and dispatches through the inner LLM
dispatcher — WITHOUT a real provider (the inner is a recording stub). The
full-chain real-provider witness is the gated e2e."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from harness_core import StepID
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_runtime.lifecycle.post_join_synthesis_dispatch import (
    POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX,
    PostJoinSynthesisStepDispatcher,
)


class _RecordingInner:
    """A recording `StepDispatcher` stand-in for the inner LLM facade — captures
    the (composed) step it is handed + returns a fixed synthesized output."""

    def __init__(self) -> None:
        self.received_step: Any = None
        self.received_binding: Any = None
        self.received_context: Any = None

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> Mapping[str, Any]:
        self.received_binding = binding
        self.received_step = step
        self.received_context = step_context
        return {"synthesized": "ok"}


class _StubContext:
    """Minimal `step_context` carrying only the field the dispatcher reads."""

    def __init__(self, sibling_outputs: Any) -> None:
        self.sibling_outputs = sibling_outputs


def _synthesis_step(messages: list[dict[str, Any]]) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"messages": messages},
    )


def test_post_join_synthesis_composes_siblings_and_dispatches_inner() -> None:
    """The dispatcher appends the branch-index-ORDERED siblings as a context
    `user` message AFTER the step's declared synthesis instruction, preserves the
    step_kind + binding + step_context, and returns the inner's output."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = _synthesis_step([{"role": "system", "content": "synthesize the siblings"}])
    binding = cast(StepEffectiveBinding, object())
    ctx = _StubContext(sibling_outputs=((0, {"a": 1}), (1, {"b": 2})))

    out = disp.dispatch(binding, step, step_context=ctx)

    assert out == {"synthesized": "ok"}
    msgs = inner.received_step.step_payload["messages"]
    # Declared synthesis instruction preserved at index 0.
    assert msgs[0] == {"role": "system", "content": "synthesize the siblings"}
    # Siblings appended as one trailing context `user` message.
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].startswith(POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX)
    # Branch-index-ORDERED siblings serialized in the context message.
    payload = json.loads(msgs[-1]["content"][len(POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX) :])
    assert payload == [
        {"branch_index": 0, "output": {"a": 1}},
        {"branch_index": 1, "output": {"b": 2}},
    ]
    # step_kind unchanged (model_copy only replaced step_payload); binding + ctx passed through.
    assert inner.received_step.step_kind is StepKind.POST_JOIN_SYNTHESIS
    assert inner.received_binding is binding
    assert inner.received_context is ctx


def test_post_join_synthesis_no_siblings_appends_empty_set() -> None:
    """No siblings (empty) → an empty siblings context message (degenerate but
    valid — never silently dropped); the inner is still dispatched."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = _synthesis_step([])
    out = disp.dispatch(cast(StepEffectiveBinding, object()), step, step_context=_StubContext(()))

    assert out == {"synthesized": "ok"}
    msgs = inner.received_step.step_payload["messages"]
    assert msgs[-1]["content"] == POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX + "[]"


def test_post_join_synthesis_none_context_is_safe() -> None:
    """A `None` step_context (no sibling carrier) → empty siblings, no crash."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = _synthesis_step([])
    out = disp.dispatch(cast(StepEffectiveBinding, object()), step, step_context=None)

    assert out == {"synthesized": "ok"}
    assert inner.received_step.step_payload["messages"][-1]["content"] == (
        POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX + "[]"
    )


def test_post_join_synthesis_rejects_params_messages_escape_hatch() -> None:
    """Codex [P2] — a synthesis payload using the `params['messages']` escape hatch
    would have its appended siblings CLOBBERED by the LLM translator's
    `kwargs.update(payload.params)`; reject it fail-closed (raises → the driver maps
    it to a FAILED RunResult), never silently dropping the branch data."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={
            "messages": [],
            "params": {"messages": [{"role": "user", "content": "clobber"}]},
        },
    )
    with pytest.raises(ValueError, match=r"params\['messages'\]"):
        disp.dispatch(
            cast(StepEffectiveBinding, object()),
            step,
            step_context=_StubContext(((0, {"a": 1}),)),
        )
    # The inner was NEVER reached (fail-closed before dispatch).
    assert inner.received_step is None


def test_post_join_synthesis_rejects_tool_capable_payload() -> None:
    """Codex round 5 [P1] — a synthesis payload declaring TOP-LEVEL provider `tools`
    is rejected fail-closed: the synthesis is read-only / effect-free (a pure compose of
    the siblings); tools would enter the model tool loop + dispatch real effects,
    violating the load-bearing §25.12 effect-free property + READ_ONLY blast-radius."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"messages": [], "tools": [{"name": "write_file"}]},
    )
    with pytest.raises(ValueError, match="may not declare provider tool-binding"):
        disp.dispatch(
            cast(StepEffectiveBinding, object()),
            step,
            step_context=_StubContext(((0, {"a": 1}),)),
        )
    # Fail-closed BEFORE dispatch — the inner (a real LLM in prod) is never reached.
    assert inner.received_step is None


@pytest.mark.parametrize(
    ("param_key", "param_value"),
    [
        ("tools", [{"name": "write_file"}]),
        ("tool_choice", "required"),
        ("functions", [{"name": "legacy_fn"}]),
        ("function_call", "auto"),
        ("mcp_servers", [{"url": "https://x"}]),
    ],
)
def test_post_join_synthesis_rejects_params_tool_binding(param_key: str, param_value: Any) -> None:
    """Codex round 6 [P1] + adversarial-reviewer F1 (both reviewers converged) — the
    round-5 guard rejected only TOP-LEVEL `tools`, but the LLM translators merge
    `kwargs.update(payload.params)` AFTER setting tools, so a tool-binding key smuggled
    through `params` (`tools` / `tool_choice` / `functions` / `function_call` /
    `mcp_servers`) reaches the provider unchecked. Reject every effect-bearing route
    fail-closed (the `[[enforce-floor-no-bypass-seam]]` discipline)."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={"messages": [], "params": {param_key: param_value}},
    )
    with pytest.raises(ValueError, match="may not declare provider tool-binding"):
        disp.dispatch(
            cast(StepEffectiveBinding, object()),
            step,
            step_context=_StubContext(((0, {"a": 1}),)),
        )
    # Fail-closed BEFORE dispatch — the provider call (and its tool binding) never fires.
    assert inner.received_step is None


def test_post_join_synthesis_allows_benign_params() -> None:
    """A synthesis payload with NON-tool-binding `params` (sampling / thinking / geo —
    the large open operator-tunable surface) is NOT rejected: only the bounded
    effect-bearing key set is forbidden (denylist, not a fragile allowlist)."""
    inner = _RecordingInner()
    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, inner))
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={
            "messages": [],
            "params": {"temperature": 0.2, "thinking": {"type": "enabled"}},
        },
    )
    out = disp.dispatch(
        cast(StepEffectiveBinding, object()),
        step,
        step_context=_StubContext(((0, {"a": 1}),)),
    )
    assert out == {"synthesized": "ok"}
    # The benign params survive onto the composed payload (untouched), siblings appended.
    assert inner.received_step.step_payload["params"] == {
        "temperature": 0.2,
        "thinking": {"type": "enabled"},
    }
    assert inner.received_step.step_payload["messages"][-1]["role"] == "user"
