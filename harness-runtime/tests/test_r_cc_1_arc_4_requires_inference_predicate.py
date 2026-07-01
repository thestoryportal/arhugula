"""R-CC-1 arc #4 (runtime spec v1.47 §2.1) — `_workflow_requires_inference`.

The bootstrap-provider requirement is conditional on this predicate. It MUST be
exact (no false negatives) so a tool-only workflow runs provider-free while
every inference-bearing workflow still requires ≥1 provider at bootstrap (C9
fail-fast preserved). `INFERENCE_STEP` (→ `ctx.llm_dispatcher`),
`SUB_AGENT_DISPATCH` (→ `ctx.sub_agent_dispatcher`), and `POST_JOIN_SYNTHESIS`
(→ `PostJoinSynthesisStepDispatcher`, runtime §14.24) reach an LLM provider;
`DECLARATIVE_STEP` / `TOOL_STEP` / `HITL_STEP` never do.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from harness_cp.workflow_driver_types import StepKind
from harness_runtime.api import _workflow_requires_inference


def _wf(*kinds: StepKind) -> Any:
    """Minimal structural stand-in for `WorkflowObject` — the predicate reads
    only `.steps[*].step_kind`."""
    return SimpleNamespace(steps=[SimpleNamespace(step_kind=k) for k in kinds])


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        # The three inference-bearing kinds.
        ((StepKind.INFERENCE_STEP,), True),
        ((StepKind.SUB_AGENT_DISPATCH,), True),
        ((StepKind.POST_JOIN_SYNTHESIS,), True),
        # The three provider-free kinds.
        ((StepKind.TOOL_STEP,), False),
        ((StepKind.DECLARATIVE_STEP,), False),
        ((StepKind.HITL_STEP,), False),
        # Mixed — ANY inference-bearing step ⇒ True (exactness, no false negatives).
        ((StepKind.TOOL_STEP, StepKind.INFERENCE_STEP), True),
        ((StepKind.TOOL_STEP, StepKind.SUB_AGENT_DISPATCH), True),
        # The exact B-POSTJOIN scenario the Codex [P1] flagged: DECLARATIVE/TOOL
        # fan-out workers + a terminal POST_JOIN_SYNTHESIS as the ONLY LLM step ⇒
        # still requires a provider (else stage 5 omits the synthesis dispatcher row).
        (
            (StepKind.DECLARATIVE_STEP, StepKind.DECLARATIVE_STEP, StepKind.POST_JOIN_SYNTHESIS),
            True,
        ),
        ((StepKind.DECLARATIVE_STEP, StepKind.TOOL_STEP, StepKind.HITL_STEP), False),
        # Empty workflow ⇒ no inference need.
        ((), False),
    ],
)
def test_requires_inference_predicate(kinds: tuple[StepKind, ...], expected: bool) -> None:
    assert _workflow_requires_inference(_wf(*kinds)) is expected
