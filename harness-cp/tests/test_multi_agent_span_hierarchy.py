"""Tests for U-CP-32 — multi-agent span hierarchy + sampling (C-CP-14).

Acceptance-criterion coverage:
  #1 hierarchy per §14.1        -> test_span_hierarchy_parent_child_relationships
  #2 sampling per §14.3         -> test_span_sampling_per_spec
                                   test_subagent_tail_keep_on_failed
  #3 hitl.gate.evaluated inside -> test_hitl_gate_evaluated_inside_subagent_span
  #5 deterministic construction -> test_hierarchy_deterministic
  #6 fanout always-sampled      -> test_fanout_spans_always_sampled
"""

from __future__ import annotations

from harness_cp.multi_agent_span_hierarchy import (
    MULTI_AGENT_SPAN_HIERARCHY,
    MULTI_AGENT_SPAN_SAMPLING,
    ParentRelationship,
    SpanHierarchyNode,
)
from harness_cp.per_class_attribute_composition import SamplingRate


def _node(name: str) -> SpanHierarchyNode:
    return next(n for n in MULTI_AGENT_SPAN_HIERARCHY if n.span_name == name)


def _sampling(name: str):
    return next(s for s in MULTI_AGENT_SPAN_SAMPLING if s.span_name == name)


def test_span_hierarchy_parent_child_relationships() -> None:
    """#1 — MULTI_AGENT_SPAN_HIERARCHY declares the §14.1 parent-child tree."""
    root = _node("parent_session")
    assert root.parent_relationship is ParentRelationship.ROOT
    assert root.parent_span_name is None
    assert _node("topology.fanout.opened").parent_span_name == "parent_session"
    assert _node("subagent.span[0]").parent_span_name == "topology.fanout.opened"
    siblings = _node("subagent.span[1..N-1]")
    assert siblings.parent_relationship is ParentRelationship.SIBLING_OF


def test_span_sampling_per_spec() -> None:
    """#2 — sampling per §14.3: fanout always-sampled; subagent.span base-rate."""
    assert _sampling("topology.fanout.opened").head_sampling_rate is SamplingRate.ALWAYS_SAMPLED
    assert _sampling("subagent.span").head_sampling_rate is SamplingRate.BASE_RATE
    assert _sampling("subagent.span.closed").head_sampling_rate is SamplingRate.ALWAYS_SAMPLED


def test_subagent_tail_keep_on_failed() -> None:
    """#2 — subagent.span tail-keeps on result_status == FAILED."""
    decision = _sampling("subagent.span")
    assert decision.tail_keep_predicate is not None

    class _Span:
        result_status = "FAILED"

    class _OkSpan:
        result_status = "SUCCEEDED"

    assert decision.tail_keep_predicate(_Span()) is True
    assert decision.tail_keep_predicate(_OkSpan()) is False


def test_hitl_gate_evaluated_inside_subagent_span() -> None:
    """#3 — hitl.gate.evaluated appears inside subagent.span[0]'s children."""
    assert "hitl.gate.evaluated" in _node("subagent.span[0]").ordered_children


def test_hierarchy_deterministic() -> None:
    """#5 — hierarchy construction is deterministic (a fixed constant)."""
    names = [n.span_name for n in MULTI_AGENT_SPAN_HIERARCHY]
    assert names == [
        "parent_session",
        "topology.fanout.opened",
        "subagent.span[0]",
        "subagent.span[1..N-1]",
        "topology.fanout.closed",
    ]


def test_fanout_spans_always_sampled() -> None:
    """#6 — topology.fanout.opened + .closed are always-sampled anchors."""
    for name in ("topology.fanout.opened", "topology.fanout.closed"):
        assert _sampling(name).head_sampling_rate is SamplingRate.ALWAYS_SAMPLED
        assert _sampling(name).tail_keep_predicate is None
