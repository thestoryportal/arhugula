"""Dispatcher-level REAL-MODEL witness for `PostJoinSynthesisStepDispatcher`
(R-FS-1 arc B-POSTJOIN-LLM-SYNTHESIS; CP spec v1.54 §3 / runtime §14.24 C-RT-33).

The "a real LLM actually composes the siblings — no proxy on the model call"
proof: the REAL `PostJoinSynthesisStepDispatcher` composes branch-index-ordered
sibling outputs into the synthesis LLM input and a **real local Ollama model**
composes them. Free-local Ollama, no paid call, zero secret
(`[[feedback-run-credential-gated-live-e2e-authorized]]`); the paid path is the
same realization with a different adapter/model, surfaced never auto-fired
(`[[feedback-background-agent-no-unilateral-paid-calls-or-secret-relocation]]`).

**Scope (precise — this is NOT the full-chain seam).** This calls
`disp.dispatch(...)` DIRECTLY — it proves the real dispatcher's compose + a real
LLM over it, NOT the driver→registry→dispatcher→final_state seam. That seam is the
deterministic `test_post_join_synthesis_full_chain.py` (`execute_workflow` through
the CP driver + a registry + the real dispatcher with a recording inner). The
stage-5 BINDING is separately proven by the bootstrap suite
(`test_lifecycle_step_dispatchers` + `test_bootstrap`). The inner here is a thin
real-Ollama adapter (mirrors `router_resolution.make_ollama_router`'s
`adapter.client.chat` surface), not the full stage-5 inference facade.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
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

pytestmark = pytest.mark.e2e

_OLLAMA_HOST = "http://127.0.0.1:11434"
_MODEL = "llama3.2:3b"


def _model_available() -> bool:
    """Gate on the Ollama daemon being reachable AND the model being pulled."""
    try:
        with urllib.request.urlopen(f"{_OLLAMA_HOST}/api/tags", timeout=3) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    return _MODEL in {m.get("name") for m in payload.get("models", [])}


def _ollama_assistant_text(response: Any) -> str:
    """Extract the assistant text from an Ollama ChatResponse (pydantic v2 or
    mapping shape) — mirrors `router_resolution._ollama_assistant_text`."""
    message = getattr(response, "message", None)
    if message is not None:
        return str(getattr(message, "content", "") or "")
    return str(response["message"]["content"])  # mapping fallback


def test_post_join_synthesis_real_ollama_composes_siblings() -> None:
    """The REAL `PostJoinSynthesisStepDispatcher` composes the branch-index-ordered
    siblings into the LLM input (DETERMINISTIC assert: the prompt the model received
    carried both siblings) and a REAL Ollama model composes them (a non-empty real
    completion). No proxy on the dispatcher or the model call."""
    if not _model_available():
        pytest.skip(f"local Ollama or model {_MODEL!r} not available")

    import ollama

    client = ollama.AsyncClient(host=_OLLAMA_HOST)
    captured: dict[str, Any] = {}

    class _OllamaInner:
        """A real-Ollama-backed inner inference dispatcher (sync — runs the async
        Ollama chat on a fresh loop, as the synthesis dispatcher calls it sync)."""

        def dispatch(
            self,
            binding: StepEffectiveBinding,
            step: WorkflowStep,
            *,
            step_context: Any = None,
        ) -> dict[str, Any]:
            messages = list(step.step_payload["messages"])
            captured["messages"] = messages
            response = asyncio.run(
                client.chat(
                    model=_MODEL,
                    messages=messages,
                    options={"temperature": 0, "num_predict": 128},
                )
            )
            return {"content": _ollama_assistant_text(response)}

    disp = PostJoinSynthesisStepDispatcher(inner=cast(StepDispatcher, _OllamaInner()))
    step = WorkflowStep(
        step_id=StepID("synthesis"),
        step_kind=StepKind.POST_JOIN_SYNTHESIS,
        step_payload={
            "messages": [
                {
                    "role": "system",
                    "content": "Synthesize the worker findings below into ONE sentence.",
                }
            ]
        },
    )
    ctx = cast(
        Any,
        type(
            "_Ctx",
            (),
            {
                "sibling_outputs": (
                    (0, {"finding": "the sky is blue"}),
                    (1, {"finding": "the grass is green"}),
                )
            },
        )(),
    )

    out = disp.dispatch(cast(StepEffectiveBinding, object()), step, step_context=ctx)

    # DETERMINISTIC — the composed prompt the model received carried BOTH
    # branch-index-ordered siblings (the real dispatcher's compose, no proxy).
    sibling_msg = captured["messages"][-1]["content"]
    assert sibling_msg.startswith(POST_JOIN_SYNTHESIS_SIBLINGS_PREFIX)
    assert "the sky is blue" in sibling_msg
    assert "the grass is green" in sibling_msg
    # REAL LLM call — a non-empty completion (the model actually composed them).
    assert out["content"].strip()
