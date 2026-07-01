"""Per-class attribute composition + per-class sampling discipline — U-CP-12.

Implements C-CP-05 §5.2 (per-class minimum attribute set) and §5.4 (sampling
discipline per event class), with the v2.3 retry-surface absorption and the
v2.4 `WorkflowEventClass` token conformance.

Declares:
  - `PER_CLASS_ATTRIBUTE_SETS` — 8 entries, one per `WorkflowEventClass`, the
    §5.2 per-class minimum-attribute composition.
  - `SAMPLING_DISPOSITIONS` — 8 entries, the §5.4 per-event-class sampling rate.
  - `RETRY_SURFACE_SAMPLING_DISPOSITIONS` — 2 entries (v2.3 retry-surface
    absorption per CP spec v1.3 §5.4 + ADR-D6 v1.2 §1.2.2.4): the
    `retry.attempt` parent event + the retry-attempt child span.

**Token conformance (v2.4).** The lifecycle event-class taxonomy is the
8-value `WorkflowEventClass` enum (homed in `harness-core` per U-CORE-01 /
operator decision D9). The §5.2 / §5.4 tables key on the §5.1 span names; this
module composes the (event-class -> span-name) join via U-CP-10's
`LIFECYCLE_EVENT_CLASS_METADATA`.

**Sampling-rate conformance (v2.4 acc #4).** The v2.3 text incorrectly asserted
all eight entries `ALWAYS_SAMPLED`. v2.4 conforms the per-row rate to the §5.4
table: `step.boundary` is `BASE_RATE` (tail-keep on failure classification);
`lease.acquired` / `lease.released` are `BASE_RATE`; `retry.attempt` is
`BASE_RATE` at the lifecycle surface (the staircase always-sampled overrides
live at the retry-surface table). `workflow.start`, `fallback.triggered`,
`breaker.tripped`, `workflow.resumption` are `ALWAYS_SAMPLED`.

**Dual-emission discipline (acc #9).** The runtime invariant — at each retry
emit BOTH the parent `retry.attempt` event AND the retry-attempt child span,
with sampling decisions applied per-path independently — is recorded as the
`DUAL_EMISSION_DISCIPLINE` declarative constant. The live span-emission
behaviour is a downstream runtime surface; this unit declares the discipline
and the two-path sampling table that the emitter consumes.

Authority: Implementation_Plan_Control_Plane_v2_4.md §2.2 U-CP-12 (v2.4
amendment; v2.3 F2-02 + F2-03 retry-surface absorption carried verbatim);
Spec_Control_Plane_v1_2.md §5 C-CP-05 §5.2 + §5.4 (preserved into v1.3 with the
retry-surface rows); ADR-D6 v1.2 §1.2.2.3 + §1.2.2.4 + §1.3.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import WorkflowEventClass
from pydantic import BaseModel, ConfigDict

from harness_cp.lifecycle_event_span_map import LIFECYCLE_EVENT_CLASS_METADATA


class SamplingRate(StrEnum):
    """The sampling-rate discriminator (C-CP-05 §5.4)."""

    ALWAYS_SAMPLED = "always_sampled"
    """head = 1.0."""

    BASE_RATE = "base_rate"
    """head per deployment-bound base rate."""


class PerClassAttributeSet(BaseModel):
    """The minimum + optional attribute set for one lifecycle event class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_class: WorkflowEventClass
    required_attributes: frozenset[str]
    optional_attributes: frozenset[str]


class SamplingDisposition(BaseModel):
    """The §5.4 sampling disposition for one lifecycle event class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_class: WorkflowEventClass
    head_rate: SamplingRate
    tail_keep: bool


class RetrySurfaceKind(StrEnum):
    """The retry-surface entity discriminator (v2.3; CP spec v1.3 §5.4)."""

    PARENT_EVENT = "parent_event"
    """`retry.attempt` event emitted on the parent operation span."""

    CHILD_SPAN = "child_span"
    """retry-attempt child span (one per attempt)."""


class SamplingOverrideRule(BaseModel):
    """An ordered always-sampled override rule (first match wins)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    condition_predicate: str
    """Human-readable predicate per CP spec v1.3 §5.4."""

    override_rate: SamplingRate
    """At v1.3 always `ALWAYS_SAMPLED` on match."""


class RetrySurfaceSamplingDisposition(BaseModel):
    """The retry-surface sampling disposition (v2.3 absorption)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    entity_kind: RetrySurfaceKind
    default_rate: SamplingRate
    always_sampled_overrides: tuple[SamplingOverrideRule, ...]
    tail_keep_on_attribute: str | None


# --- §5.2 per-class minimum attribute set -----------------------------------
# Keyed by span name (§5.1); composed against the §5.2 table. `workflow.checkpoint`
# / fan-out classes carry the F2 six-field anchor attributes per acceptance #2.

_SPAN_BY_CLASS = {m.event_class: m.span_name for m in LIFECYCLE_EVENT_CLASS_METADATA}

_REQUIRED_ATTRIBUTES: dict[WorkflowEventClass, frozenset[str]] = {
    WorkflowEventClass.WORKFLOW_START: frozenset(
        {"workflow.id", "workflow.class", "engine.class", "manifest.entry_id", "idempotency_key"}
    ),
    WorkflowEventClass.STEP_BOUNDARY: frozenset(
        {"workflow.id", "step.index", "step.kind", "idempotency_key"}
    ),
    WorkflowEventClass.FALLBACK_TRIGGER: frozenset({"workflow.id", "fallback.cause"}),
    WorkflowEventClass.RETRY_ATTEMPT: frozenset(
        {"workflow.id", "retry.attempt_number", "retry.fail_class"}
    ),
    WorkflowEventClass.BREAKER_TRIP: frozenset({"workflow.id", "breaker.state"}),
    WorkflowEventClass.LEASE_ACQUIRED: frozenset(
        {"lease.key", "lease.holder", "lease.ttl_ms", "lease.mechanism"}
    ),
    WorkflowEventClass.LEASE_RELEASED: frozenset(
        {"lease.key", "lease.holder", "lease.release_cause"}
    ),
    # §5.2 (v1.2 lineage) declared the carrier attribute as `resumption.kind`;
    # §9.1 v1.3 amendment (F2-12 sub-scope (i) closure) introduced
    # `engine.replay_disposition` as the canonical 4th `engine.*` attribute +
    # superseded the §8.1/§5.2 `resumption.kind` carrier name at the at-emission
    # layer. Plan U-CP-20 acceptance #2 + U-CP-21 4-attribute namespace adopted
    # `engine.replay_disposition` as the required attribute at
    # `workflow.resumption`. CP spec v1.23 canonical-reading amendment
    # harmonizes §5.2 + §8.1 + §8.3 to cite `engine.replay_disposition` per
    # §9.1 v1.3 supersession.
    WorkflowEventClass.RESUMPTION: frozenset(
        {"workflow.id", "engine.class", "engine.replay_disposition", "idempotency_key"}
    ),
}

PER_CLASS_ATTRIBUTE_SETS: tuple[PerClassAttributeSet, ...] = tuple(
    PerClassAttributeSet(
        event_class=ec,
        required_attributes=_REQUIRED_ATTRIBUTES[ec],
        optional_attributes=frozenset(),
    )
    for ec in WorkflowEventClass
)
"""The 8 per-class attribute sets — one per `WorkflowEventClass` (C-CP-05 §5.2)."""


# --- §5.4 per-event-class sampling discipline -------------------------------

_SAMPLING: dict[WorkflowEventClass, tuple[SamplingRate, bool]] = {
    WorkflowEventClass.WORKFLOW_START: (SamplingRate.ALWAYS_SAMPLED, False),
    WorkflowEventClass.STEP_BOUNDARY: (SamplingRate.BASE_RATE, True),
    WorkflowEventClass.FALLBACK_TRIGGER: (SamplingRate.ALWAYS_SAMPLED, False),
    WorkflowEventClass.RETRY_ATTEMPT: (SamplingRate.BASE_RATE, True),
    WorkflowEventClass.BREAKER_TRIP: (SamplingRate.ALWAYS_SAMPLED, False),
    WorkflowEventClass.LEASE_ACQUIRED: (SamplingRate.BASE_RATE, False),
    WorkflowEventClass.LEASE_RELEASED: (SamplingRate.BASE_RATE, False),
    WorkflowEventClass.RESUMPTION: (SamplingRate.ALWAYS_SAMPLED, False),
}

SAMPLING_DISPOSITIONS: tuple[SamplingDisposition, ...] = tuple(
    SamplingDisposition(event_class=ec, head_rate=_SAMPLING[ec][0], tail_keep=_SAMPLING[ec][1])
    for ec in WorkflowEventClass
)
"""The 8 per-event-class sampling dispositions (C-CP-05 §5.4 per-row table)."""


# --- v2.3 retry-surface sampling discipline ---------------------------------

RETRY_SURFACE_SAMPLING_DISPOSITIONS: tuple[RetrySurfaceSamplingDisposition, ...] = (
    RetrySurfaceSamplingDisposition(
        entity_id="retry.attempt",
        entity_kind=RetrySurfaceKind.PARENT_EVENT,
        default_rate=SamplingRate.BASE_RATE,
        always_sampled_overrides=(
            SamplingOverrideRule(
                condition_predicate="retry.attempt_number >= 2",
                override_rate=SamplingRate.ALWAYS_SAMPLED,
            ),
            SamplingOverrideRule(
                condition_predicate="parent.attempts_remaining == 0",
                override_rate=SamplingRate.ALWAYS_SAMPLED,
            ),
        ),
        tail_keep_on_attribute=None,
    ),
    RetrySurfaceSamplingDisposition(
        entity_id="retry-attempt-child-span",
        entity_kind=RetrySurfaceKind.CHILD_SPAN,
        default_rate=SamplingRate.BASE_RATE,
        always_sampled_overrides=(),
        tail_keep_on_attribute="retry.fail_class",
    ),
)
"""The 2 retry-surface sampling dispositions per CP spec v1.3 §5.4 + ADR-D6
v1.2 §1.2.2.4 (v2.3 F2-03 absorption)."""


DUAL_EMISSION_DISCIPLINE: tuple[str, str] = ("retry.attempt", "retry-attempt-child-span")
"""Dual-emission discipline (acc #9; ADR-D6 v1.2 §1.2.2.3): at each retry the
runtime MUST emit BOTH the parent `retry.attempt` event AND the retry-attempt
child span. Collapse to event-only or span-only is forbidden. Sampling
decisions apply per-path independently."""


def required_attributes_for(event_class: WorkflowEventClass) -> frozenset[str]:
    """Return the §5.2 required-attribute set for a lifecycle event class.

    Deterministic given input (acceptance #5)."""
    for entry in PER_CLASS_ATTRIBUTE_SETS:
        if entry.event_class is event_class:
            return entry.required_attributes
    raise KeyError(event_class)  # pragma: no cover — total over the closed enum
