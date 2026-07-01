"""Lifecycle-event-class span-name-metadata map + `ParentRelation` — U-CP-10.

Implements C-CP-05 §5.1 (the "Span name" column of the eight-class lifecycle
event table). Declares the `ParentRelation` enum, the
`LifecycleEventClassMetadata` record, and the 8-entry
`LIFECYCLE_EVENT_CLASS_METADATA` map.

The 8-class event taxonomy itself resides in `harness-core` as
`WorkflowEventClass` (U-CORE-01) — operator decision D9 / Q-R4-7 retired the
former U-CP-10-local `LifecycleEventClass` enum (v2.6 declaration-site
conversion). U-CP-10 covers the §5.1 *span-name map* half; U-CORE-01 covers
the §5.1 *event-class enum* half (multi-unit coverage of one contract is
permitted). `ParentRelation` is promoted to a real `enum` (operator decision
D5 / Q-R4-3) — a faithful ADR-D4 v1.1 factor-out of the parent-ownership
semantics, carried at U-CP-10 per the Pattern-D resolution.

The map delegates to the ADR-D6 OTel ingestion contract per C-CP-05 §5.1.

Authority: Implementation_Plan_Control_Plane_v2_6.md §2.2 U-CP-10 (v2.6
declaration-site conversion — `LifecycleEventClass` retired, `WorkflowEventClass`
re-anchor, `ParentRelation` promotion); Spec_Control_Plane_v1_2.md §5 C-CP-05
§5.1 (preserved verbatim into v1.3); ADR-D4 v1.1 §1.1; ADR-D6 v1.2 §1.2.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import WorkflowEventClass
from pydantic import BaseModel, ConfigDict


class ParentRelation(StrEnum):
    """A lifecycle event's parent-span ownership relation (ADR-D4 v1.1 §1.1).

    Promoted to a real `enum` per operator decision D5 / Q-R4-3. Closed at
    cardinality 3.
    """

    ROOT = "root"
    """Event has no parent span — a workflow-root event."""

    CHILD_OF = "child_of"
    """Event is a child span of its workflow parent."""

    DELEGATED_TO = "delegated_to"
    """Event is a delegated sub-agent span (hierarchical-delegation /
    decentralized-handoff)."""


class LifecycleEventClassMetadata(BaseModel):
    """One `WorkflowEventClass` value's span-name + parent-relation metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_class: WorkflowEventClass
    """The lifecycle event class — resolves to `harness-core`'s
    `WorkflowEventClass` (one nominal type, U-CORE-01)."""

    span_name: str
    """Canonical OTel span name per the C-CP-05 §5.1 "Span name" column."""

    parent_relation: ParentRelation


# --- Registry population (spec C-CP-05 §5.1 "Span name" column) -------------

LIFECYCLE_EVENT_CLASS_METADATA: tuple[LifecycleEventClassMetadata, ...] = (
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.WORKFLOW_START,
        span_name="workflow.start",
        parent_relation=ParentRelation.ROOT,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.STEP_BOUNDARY,
        span_name="step.boundary",
        parent_relation=ParentRelation.CHILD_OF,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.FALLBACK_TRIGGER,
        span_name="fallback.triggered",
        parent_relation=ParentRelation.CHILD_OF,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.RETRY_ATTEMPT,
        span_name="retry.attempt",
        parent_relation=ParentRelation.CHILD_OF,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.BREAKER_TRIP,
        span_name="breaker.tripped",
        parent_relation=ParentRelation.CHILD_OF,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.LEASE_ACQUIRED,
        span_name="lease.acquired",
        parent_relation=ParentRelation.CHILD_OF,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.LEASE_RELEASED,
        span_name="lease.released",
        parent_relation=ParentRelation.CHILD_OF,
    ),
    LifecycleEventClassMetadata(
        event_class=WorkflowEventClass.RESUMPTION,
        span_name="workflow.resumption",
        parent_relation=ParentRelation.CHILD_OF,
    ),
)
"""The 8 lifecycle-event-class span-name-metadata entries — one per
`WorkflowEventClass` value, mapped to its C-CP-05 §5.1 "Span name"."""
