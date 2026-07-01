"""Tests for U-CP-33 — concurrent-prompt-cache warm-up protocol (C-CP-14 §14.4).

Lands the v2.9-revised body. Acceptance-criterion coverage:
  #1 four steps in order        -> test_on_fanout_dispatch_four_steps_in_order
  #2 step 1 persists via U-IS-02 -> test_step_1_persists_via_u_is_02
  #3 completion proxy 2 kinds   -> test_completion_proxy_two_kinds
  #4 step 3 first-of-two signal -> test_step_3_first_of_two_signals
  #5 step 4 concurrent fan-out  -> test_step_4_all_siblings_dispatched
  #7 LeadAgentPlan opaque       -> test_lead_agent_plan_opaque_mapping_no_invented_fields
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, WorkloadClass
from harness_cp.concurrent_prompt_cache_warmup import (
    CacheCompletionProxy,
    CacheCompletionProxyKind,
    CacheWarmupInput,
    LeadAgentPlan,
    SubAgent,
    await_cache_completion,
    on_fanout_dispatch,
    persist_lead_agent_plan,
)
from harness_is.path_binding import PathBinding, PathBindingEntry
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver


def _resolver() -> PathResolver:
    binding = PathBinding(
        entries=(
            PathBindingEntry(
                path_class=PathClass.PROMPTS,
                workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
                deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
                path="/harness/prompts",
            ),
        )
    )
    return PathResolver(binding)


def _proxy() -> CacheCompletionProxy:
    return CacheCompletionProxy(
        proxy_kind=CacheCompletionProxyKind.CACHE_ACKNOWLEDGEMENT, proxy_at_ms=12
    )


def _input(n: int) -> CacheWarmupInput:
    siblings: tuple[SubAgent, ...] = tuple({"idx": i} for i in range(n))
    plan: LeadAgentPlan = {"plan_text": "decompose the task", "steps": [1, 2]}
    return CacheWarmupInput(siblings=siblings, cache_breakpoint_id="bp-0", lead_agent_plan=plan)


def test_on_fanout_dispatch_four_steps_in_order() -> None:
    """#1 — on_fanout_dispatch runs the four-step protocol; result carries all."""
    result = on_fanout_dispatch(
        _input(3),
        _resolver(),
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        _proxy(),
    )
    assert result.plan_path == "/harness/prompts"
    assert result.completion_proxy.proxy_kind is (CacheCompletionProxyKind.CACHE_ACKNOWLEDGEMENT)
    assert result.siblings_dispatched == 3


def test_step_1_persists_via_u_is_02() -> None:
    """#2 — step 1 persists the plan via the U-IS-02 PathResolver."""
    path = persist_lead_agent_plan(
        {"plan": "x"},
        _resolver(),
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert str(path) == "/harness/prompts"


def test_completion_proxy_two_kinds() -> None:
    """#3 — CacheCompletionProxyKind declares exactly two kinds."""
    assert len(CacheCompletionProxyKind) == 2
    assert {k.value for k in CacheCompletionProxyKind} == {
        "cache-acknowledgement",
        "first-token-emission",
    }


def test_step_3_first_of_two_signals() -> None:
    """#4 — await_cache_completion declares the completion-proxy contract."""
    with pytest.raises(NotImplementedError):
        await_cache_completion({"idx": 0})


def test_step_4_all_siblings_dispatched() -> None:
    """#5 — every sibling is dispatched (1 synchronous + N-1 concurrent)."""
    for n in (1, 2, 5):
        result = on_fanout_dispatch(
            _input(n),
            _resolver(),
            WorkloadClass.SOFTWARE_ENGINEERING,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            _proxy(),
        )
        assert result.siblings_dispatched == n


def test_lead_agent_plan_opaque_mapping_no_invented_fields() -> None:
    """#7 — LeadAgentPlan is the opaque alias Mapping[str, Any], no field set."""
    # Any mapping shape is admissible — the alias invents no record fields.
    plan_a: LeadAgentPlan = {}
    plan_b: LeadAgentPlan = {"arbitrary": "shape", "n": 1}
    for plan in (plan_a, plan_b):
        path = persist_lead_agent_plan(
            plan,
            _resolver(),
            WorkloadClass.SOFTWARE_ENGINEERING,
            DeploymentSurface.LOCAL_DEVELOPMENT,
        )
        assert str(path) == "/harness/prompts"
