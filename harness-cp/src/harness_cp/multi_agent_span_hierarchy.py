"""Multi-agent span hierarchy + per-span sampling discipline — U-CP-32.

Implements C-CP-14 §14.1 (the multi-agent span hierarchy), §14.3 (per-span
sampling discipline), and §14.5 (cross-family-fallback cache-token reset).

Declares the `ParentRelationship` enum, the `SpanHierarchyNode` record + the
`MULTI_AGENT_SPAN_HIERARCHY` constant, the `SpanSamplingDecision` record + the
`MULTI_AGENT_SPAN_SAMPLING` constant. The hierarchy is the §14.1 verbatim
parent→child→sibling span tree; the sampling table is the §14.3 per-span
head-rate + tail-keep discipline.

`ParentRelationship` is U-CP-32's own 3-value enum (`{ROOT, CHILD_OF,
SIBLING_OF}` per the §14.1 signature) — distinct from U-CP-10's
`ParentRelation` (the `{ROOT, CHILD_OF, DELEGATED_TO}` lifecycle-event
ownership relation); the multi-agent span hierarchy carries an explicit
`SIBLING_OF` relation for the concurrent `subagent.span[1..N-1]` siblings.

Hierarchy construction is deterministic — no inference-based reparenting.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.5 U-CP-32 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §14 C-CP-14 §14.1/§14.3/§14.5;
ADR-D4 v1.1 §1.1.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import TailKeepPredicate
from harness_cp.per_class_attribute_composition import SamplingRate


class ParentRelationship(StrEnum):
    """A span's parent relationship in the multi-agent hierarchy (C-CP-14 §14.1)."""

    ROOT = "root"
    """The session-root span — `parent_session`."""

    CHILD_OF = "child-of"
    """The span is a child of its named parent span."""

    SIBLING_OF = "sibling-of"
    """The span is a concurrent sibling — `subagent.span[1..N-1]`."""


class SpanHierarchyNode(BaseModel):
    """One node of the §14.1 multi-agent span hierarchy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_name: str
    parent_relationship: ParentRelationship
    parent_span_name: str | None
    """`None` for the ROOT node."""

    ordered_children: tuple[str, ...]


MULTI_AGENT_SPAN_HIERARCHY: tuple[SpanHierarchyNode, ...] = (
    SpanHierarchyNode(
        span_name="parent_session",
        parent_relationship=ParentRelationship.ROOT,
        parent_span_name=None,
        ordered_children=(
            "topology.fanout.opened",
            "subagent.span[0]",
            "subagent.span[1..N-1]",
            "topology.fanout.closed",
        ),
    ),
    SpanHierarchyNode(
        span_name="topology.fanout.opened",
        parent_relationship=ParentRelationship.CHILD_OF,
        parent_span_name="parent_session",
        ordered_children=(),
    ),
    SpanHierarchyNode(
        span_name="subagent.span[0]",
        parent_relationship=ParentRelationship.CHILD_OF,
        parent_span_name="topology.fanout.opened",
        ordered_children=(
            # alias per AS spec v1.7 §14.1; runtime span-name format owned by
            # OD spec v1.12 §C-OD-04 §4.1.
            "the LLM inference span[]",
            "sandbox.enter",
            "tool.call[]",
            "sandbox.exit",
            "hitl.gate.evaluated",
            "subagent.span.closed",
        ),
    ),
    SpanHierarchyNode(
        span_name="subagent.span[1..N-1]",
        parent_relationship=ParentRelationship.SIBLING_OF,
        parent_span_name="subagent.span[0]",
        ordered_children=(),
    ),
    SpanHierarchyNode(
        span_name="topology.fanout.closed",
        parent_relationship=ParentRelationship.CHILD_OF,
        parent_span_name="parent_session",
        ordered_children=(),
    ),
)
"""The §14.1 multi-agent span hierarchy, verbatim."""


class SpanSamplingDecision(BaseModel):
    """One per-span sampling decision (C-CP-14 §14.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_name: str
    head_sampling_rate: SamplingRate
    tail_keep_predicate: TailKeepPredicate | None
    """`None` when no tail-keep classification applies to the span."""


def _subagent_failed(span: object) -> bool:
    """Tail-keep predicate — keep a `subagent.span` iff its result failed.

    §14.3: `subagent.span` is `BASE_RATE` head-sampled with tail-keep on
    `subagent.result_status == FAILED`. The span argument is opaque
    (`TailKeepPredicate` is `Callable[[Any], bool]`); the predicate reads the
    `result_status` attribute when present.
    """
    status = getattr(span, "result_status", None)
    return status == "FAILED"


MULTI_AGENT_SPAN_SAMPLING: tuple[SpanSamplingDecision, ...] = (
    SpanSamplingDecision(
        span_name="topology.fanout.opened",
        head_sampling_rate=SamplingRate.ALWAYS_SAMPLED,
        tail_keep_predicate=None,
    ),
    SpanSamplingDecision(
        span_name="subagent.span",
        head_sampling_rate=SamplingRate.BASE_RATE,
        tail_keep_predicate=_subagent_failed,
    ),
    SpanSamplingDecision(
        span_name="subagent.span.closed",
        head_sampling_rate=SamplingRate.ALWAYS_SAMPLED,
        tail_keep_predicate=None,
    ),
    SpanSamplingDecision(
        span_name="topology.fanout.closed",
        head_sampling_rate=SamplingRate.ALWAYS_SAMPLED,
        tail_keep_predicate=None,
    ),
)
"""The §14.3 per-span sampling discipline: `topology.fanout.*` and
`subagent.span.closed` always-sampled; `subagent.span` base-rate with
tail-keep on FAILED result status."""
