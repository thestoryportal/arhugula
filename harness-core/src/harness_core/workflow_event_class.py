"""Lifecycle event-class taxonomy — U-CORE-01.

Implements C-CP-05 §5.1 (the F3 capability-floor 8-class lifecycle event
taxonomy). Declares the closed 8-value `WorkflowEventClass` enum.

`WorkflowEventClass` is a **cross-axis shared type** — consumed by CP U-CP-10
(span-name-metadata map) and IS U-IS-14 — and therefore resides in
`harness-core` per `CLAUDE.md` §3.3 and the R-series carrier map. Per operator
decision D9 / Q-R4-7 (CP plan v2.6, R4), `WorkflowEventClass` is the surviving
name for this taxonomy; CP's former local `LifecycleEventClass` is retired and
U-CP-10 converts to a consuming site.

The taxonomy is **closed** at cardinality 8 — the `Event class` column of the
C-CP-05 §5.1 lifecycle event class table. Member string values are the §5.1
event-class identifiers verbatim (lowercase-hyphen).

**No `WorkflowEvent` payload model is declared here.** The U-CORE-01 v1.0 plan
specified a `WorkflowEvent` payload model carrying the C-CP-05 §5.2 per-class
minimum attribute set; that model was struck at plan v1.1 (carrier-thin Class 1
fork resolution, operator ruling 2026-05-15 —
`.harness/class_1_tension_u_core_01_workflow_event.md`). The §5.2 per-class
attribute schema is a CP-axis span-emission-site contract, not a `harness-core`
carrier type.

Authority: Implementation_Plan_Harness_Core_v1_1.md §2 U-CORE-01 (acceptance
criterion #4); Spec_Control_Plane_v1_2.md C-CP-05 §5.1 (preserved verbatim into
v1.3); ADR-F3 v1.1 §Decision capability-floor (iv).
"""

from __future__ import annotations

from enum import StrEnum


class WorkflowEventClass(StrEnum):
    """The 8 lifecycle event classes (C-CP-05 §5.1, verbatim).

    Closed at cardinality 8 — the §5.1 `Event class` column. Member string
    values are the §5.1 event-class identifiers byte-exact.
    """

    WORKFLOW_START = "workflow-start"
    STEP_BOUNDARY = "step-boundary"
    FALLBACK_TRIGGER = "fallback-trigger"
    RETRY_ATTEMPT = "retry-attempt"
    BREAKER_TRIP = "breaker-trip"
    LEASE_ACQUIRED = "lease-acquired"
    LEASE_RELEASED = "lease-released"
    RESUMPTION = "resumption"
