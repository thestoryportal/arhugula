"""Tests for U-CP-10 — lifecycle-event span-name map + `ParentRelation`.

Acceptance-criterion coverage:
  #1 8-class enum struck (lives at U-CORE-01) -> test_no_local_lifecycle_event_class_enum
  #2 span-name map per §5.1                   -> test_span_name_map_match_spec_5_1
  #3 map closed at 8                          -> test_metadata_cardinality_eight
  #4 D6 ingestion delegated                   -> structural
  #5 ParentRelation 3 values                  -> test_parent_relation_cardinality_three,
                                                 test_parent_relation_values
  #6 consumes harness-core WorkflowEventClass  -> test_metadata_consumes_workflow_event_class
"""

from __future__ import annotations

import harness_cp.lifecycle_event_span_map as mod
from harness_core import WorkflowEventClass
from harness_cp.lifecycle_event_span_map import (
    LIFECYCLE_EVENT_CLASS_METADATA,
    ParentRelation,
)

# C-CP-05 §5.1 "Span name" column, verbatim.
_SPEC_SPAN_NAMES = {
    WorkflowEventClass.WORKFLOW_START: "workflow.start",
    WorkflowEventClass.STEP_BOUNDARY: "step.boundary",
    WorkflowEventClass.FALLBACK_TRIGGER: "fallback.triggered",
    WorkflowEventClass.RETRY_ATTEMPT: "retry.attempt",
    WorkflowEventClass.BREAKER_TRIP: "breaker.tripped",
    WorkflowEventClass.LEASE_ACQUIRED: "lease.acquired",
    WorkflowEventClass.LEASE_RELEASED: "lease.released",
    WorkflowEventClass.RESUMPTION: "workflow.resumption",
}


def test_metadata_cardinality_eight() -> None:
    """Acceptance #3 — exactly eight span-name-map entries."""
    assert len(LIFECYCLE_EVENT_CLASS_METADATA) == 8


def test_span_name_map_match_spec_5_1() -> None:
    """Acceptance #2 — each class maps to its C-CP-05 §5.1 span name verbatim."""
    actual = {m.event_class: m.span_name for m in LIFECYCLE_EVENT_CLASS_METADATA}
    assert actual == _SPEC_SPAN_NAMES


def test_metadata_consumes_workflow_event_class() -> None:
    """Acceptance #6 — every entry is keyed on `harness-core`'s WorkflowEventClass.

    Exactly one entry per `WorkflowEventClass` value — one nominal type.
    """
    classes = {m.event_class for m in LIFECYCLE_EVENT_CLASS_METADATA}
    assert classes == set(WorkflowEventClass)
    for m in LIFECYCLE_EVENT_CLASS_METADATA:
        assert isinstance(m.event_class, WorkflowEventClass)


def test_no_local_lifecycle_event_class_enum() -> None:
    """Acceptance #1 — the former U-CP-10-local `LifecycleEventClass` is gone."""
    assert not hasattr(mod, "LifecycleEventClass")


def test_parent_relation_cardinality_three() -> None:
    """Acceptance #5 — `ParentRelation` declares exactly three values."""
    assert len(ParentRelation) == 3


def test_parent_relation_values() -> None:
    """Acceptance #5 — values are `{ROOT, CHILD_OF, DELEGATED_TO}`."""
    assert {p.name for p in ParentRelation} == {"ROOT", "CHILD_OF", "DELEGATED_TO"}


def test_workflow_start_is_root() -> None:
    """`workflow-start` is the workflow-root event — ParentRelation.ROOT."""
    start = next(
        m
        for m in LIFECYCLE_EVENT_CLASS_METADATA
        if m.event_class == WorkflowEventClass.WORKFLOW_START
    )
    assert start.parent_relation == ParentRelation.ROOT
