"""F3 capability-floor (iv) lifecycle event-to-span-event mapping — U-OD-08.

Implements C-OD-06 §6.1 (lifecycle event mapping table), §6.2 (additive
composition with namespace specialization layers), §6.3 (F2-12 deferral
acknowledgement at `retry.attempt`).

`F3LifecycleEventClass` enumerates the eight F3 v1.1 capability-floor (iv)
observable-lifecycle event classes per §6.1. `LifecycleEventMapping` carries
the §6.1 four-column table row (event-class name / span-placement form /
attribute namespace(s) / sampling posture). `F3_LIFECYCLE_EVENT_MAPPINGS`
declares one mapping per event class.

v2.8 (D-2): `F3LifecycleEventClass` + `F3_LIFECYCLE_EVENT_MAPPINGS` are
re-authored to the spec C-OD-06 §6.1 eight-event lifecycle table — the v2.1
invocation-shaped taxonomy (`CHAT_INVOCATION` / `TOOL_INVOCATION` / …) diverged
5/8 from §6.1 and is replaced. `LifecycleEventMapping` is grown to carry the
§6.1 four-column table faithfully — `span_event_name` is renamed
`event_class_name` and two fields are added (`span_placement_form`,
`sampling_posture`); `attribute_namespaces` is retained.

Authority: Implementation_Plan_Operational_Discipline_v2_8.md §3.2.5 U-OD-08
(v2.8 D-2 revision — taxonomy conformed to spec C-OD-06 §6.1; acc #4-#8 +
`F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT` preserved verbatim from v2.1);
Spec_Operational_Discipline_v1_2.md §6 C-OD-06 §6.1 + §6.2 + §6.3 (preserved
verbatim into v1.3 per v1.3 §0.1); ADR-F3 v1.1 capability-floor (iv).

Depends on: [U-OD-04, U-OD-05, U-OD-06, U-OD-07, U-CP-54 (cross-axis: CP —
C-CP-24 §24.1.B F3 lifecycle event attributes)]. The U-CP-54 edge is a
cross-axis dependency resolved at Phase 7 sub-phase 7c — NOT a 7b blocker; no
typed surface is imported from U-CP-54 here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT",
    "F3_LIFECYCLE_EVENT_MAPPINGS",
    "F3LifecycleEventClass",
    "LifecycleEventMapping",
]


class F3LifecycleEventClass(StrEnum):
    """The 8 F3 capability-floor (iv) observable-lifecycle event classes (§6.1).

    Exactly 8 values per §6.1 verbatim — the F3 v1.1 capability-floor (iv)
    observable-lifecycle taxonomy. v2.8 (D-2): replaces the v2.1
    invocation-shaped taxonomy, which diverged 5/8 from §6.1.
    """

    WORKFLOW_START = "WORKFLOW_START"
    STEP_BOUNDARY = "STEP_BOUNDARY"
    FALLBACK_TRIGGERED = "FALLBACK_TRIGGERED"
    RETRY_ATTEMPT = "RETRY_ATTEMPT"
    BREAKER_TRIPPED = "BREAKER_TRIPPED"
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    LEASE_RELEASED = "LEASE_RELEASED"
    WORKFLOW_RESUMED = "WORKFLOW_RESUMED"


class LifecycleEventMapping(BaseModel):
    """One §6.1 lifecycle-event mapping-table row.

    Frozen → `Eq`. Carries the §6.1 four-column table row faithfully (v2.8
    D-2): `event_class_name` (col 1), `span_placement_form` (col 2),
    `attribute_namespaces` (col 3 — empty for `step.boundary`, which has no
    dedicated namespace), `sampling_posture` (col 4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the F3 event class this mapping is keyed by.
    f3_event_class: F3LifecycleEventClass
    #: §6.1 col 1 — the event-class name string, e.g. "workflow.start".
    event_class_name: str
    #: §6.1 col 2 — the span-placement form, e.g. "Span attribute on root span".
    span_placement_form: str
    #: §6.1 col 3 — the attribute namespace(s); empty for step.boundary.
    attribute_namespaces: frozenset[str]
    #: §6.1 col 4 — the sampling posture, e.g. "Always-sampled per C-OD-09".
    sampling_posture: str


#: The F3 lifecycle event mapping — exactly 8 entries, byte-exact with the
#: §6.1 four-column table (acceptance #2 / #3).
F3_LIFECYCLE_EVENT_MAPPINGS: dict[F3LifecycleEventClass, LifecycleEventMapping] = {
    F3LifecycleEventClass.WORKFLOW_START: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.WORKFLOW_START,
        event_class_name="workflow.start",
        span_placement_form="Span attribute on root span",
        attribute_namespaces=frozenset({"engine.*"}),
        sampling_posture="Per root span sampling (inherits)",
    ),
    F3LifecycleEventClass.STEP_BOUNDARY: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.STEP_BOUNDARY,
        event_class_name="step.boundary",
        span_placement_form="Span event on parent",
        attribute_namespaces=frozenset(),
        sampling_posture="Per parent sampling",
    ),
    F3LifecycleEventClass.FALLBACK_TRIGGERED: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.FALLBACK_TRIGGERED,
        event_class_name="fallback.triggered",
        span_placement_form="Span event on parent + new sibling fallback span",
        attribute_namespaces=frozenset({"fallback.*"}),
        sampling_posture="Always-sampled per C-OD-09",
    ),
    F3LifecycleEventClass.RETRY_ATTEMPT: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.RETRY_ATTEMPT,
        event_class_name="retry.attempt",
        span_placement_form="Span event on parent + new sibling retry span",
        attribute_namespaces=frozenset({"retry.*"}),
        sampling_posture="Base-rate at 1st attempt; always-sampled at 2nd onward per C-CP-03 §3.5",
    ),
    F3LifecycleEventClass.BREAKER_TRIPPED: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.BREAKER_TRIPPED,
        event_class_name="breaker.tripped",
        span_placement_form="Span event on parent",
        attribute_namespaces=frozenset({"harness.breaker.*"}),
        sampling_posture="Always-sampled per C-OD-09",
    ),
    F3LifecycleEventClass.LEASE_ACQUIRED: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.LEASE_ACQUIRED,
        event_class_name="lease.acquired",
        span_placement_form="Span event on parent",
        attribute_namespaces=frozenset({"lease.*"}),
        sampling_posture="Base-rate per C-CP-05 §5.4",
    ),
    F3LifecycleEventClass.LEASE_RELEASED: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.LEASE_RELEASED,
        event_class_name="lease.released",
        span_placement_form="Span event on parent",
        attribute_namespaces=frozenset({"lease.*"}),
        sampling_posture="Base-rate per C-CP-05 §5.4",
    ),
    F3LifecycleEventClass.WORKFLOW_RESUMED: LifecycleEventMapping(
        f3_event_class=F3LifecycleEventClass.WORKFLOW_RESUMED,
        event_class_name="workflow.resumed",
        span_placement_form="Span attribute on root span (post-resumption)",
        attribute_namespaces=frozenset({"engine.*"}),
        sampling_posture="Always-sampled per C-CP-05 §5.4",
    ),
}


#: §6.3 F2-12 deferral acknowledgement at `retry.attempt` — non-contract-bearing
#: forward-compatibility note (acceptance #5 / #6). Preserved verbatim from
#: v2.1. F2-12 ACTIVE contract-bearing engagement is exclusively at U-OD-20
#: §14.5.
F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT: str = (
    "retry.attempt sibling-span discipline at D6 ingestion is deferred per "
    "F2-12 carry-forward; v1 commits event + new sibling span per C-CP-03 §3.5; "
    "revisable at D6 v1.2"
)
