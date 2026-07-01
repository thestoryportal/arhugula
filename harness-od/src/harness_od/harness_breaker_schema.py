"""`harness.breaker.*` 7-attribute canonical schema — U-OD-09.

Implements C-OD-07 §7.1 (seven-attribute schema), §7.2 (quality-of-emission
invariants), §7.3 (C9↔C10 subscription contract reference).

`HARNESS_BREAKER_ATTRIBUTES` declares the seven `harness.breaker.*` attribute
names per §7.1. `BreakerScope` / `BreakerState` enumerate the §7.1 enum-typed
attribute value sets. `HarnessBreakerEvent` is the breaker-trip event record.
`emit_breaker_trip_span_event` emits the event at the parent span.

substrate-anchored-outside-CP: per F-CP-01 Stage 3b alignment, the
`harness.breaker.*` namespace is substrate-anchored at the OD axis rather than
the CP axis. The OD plan exports `harness.breaker.*` to the CP plan as a
CP-consuming seam — the OD → CP exporter direction; edge target = U-CP-54,
contract anchor = C-CP-24 §24.1.C; resolved at Phase 7 sub-phase 7c.

v2.8 (D-3): the v2.5/v2.6 acc #2 "4 Required / 3 Conditional tier
classification" is STRUCK — OD spec C-OD-07 §7.1 declares no tier
classification (the §7.1 table columns are Attribute | Type | Source |
Definition, with no tier column). `HARNESS_BREAKER_ATTRIBUTES` is re-typed
`List<GenAiAttribute>` → `tuple[str, ...]` (the seven §7.1 attribute names).
Per CLAUDE.md I-2 / X-AL-3 there is no spec basis to conform a tier split to.
Note: the U-OD-04 `AttributeTier` enum carries `REQUIRED` + `RECOMMENDED` +
`OPT_IN` members (v1.2-lineage DERIVATIVE names `REQUIRED_STABLE` /
`RECOMMENDED_DEVELOPMENT` / `OPT_IN_CONTENT` retired at OD spec v1.24 §1.2)
and gained `CONDITIONALLY_REQUIRED` at OD spec v1.16 §1.2 (populated with 3
attributes at v1.19 §1.1 per-attribute tier redistribution); the STRIKE
rationale is anchored at the §7.1 absence-of-tier-column at C-OD-07, NOT at
absence of enum members at C-OD-04.

Spec/plan typing nuance: spec §7.1 types `harness.breaker.permanent_fail_repeats`
as `bool`, while the plan signature (v2.1, preserved verbatim through v2.8)
types `HarnessBreakerEvent.permanent_fail_repeats` as `Option<int>`. The plan
signature is execution authority for the materialization and v2.8 explicitly
preserved `HarnessBreakerEvent` with three `Option`-typed fields; this module
follows the plan (`int | None`). Not a contradiction — the field carries the
repeat count; absence (`None`) reads as "not from repeated permanent-fails".

Authority: Implementation_Plan_Operational_Discipline_v2_8.md §3.3.1 U-OD-09
(v2.8 D-3 revision — acc #2 STRUCK, `HARNESS_BREAKER_ATTRIBUTES` re-typed to
`List<string>`, acc #9 re-worded; all other surfaces preserved verbatim from
the v2.5/v2.6 body); Spec_Operational_Discipline_v1_2.md §7 C-OD-07 §7.1 +
§7.2 + §7.3 (preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.2.1.

Depends on: [U-OD-07, U-OD-04] — `SpanRef` / `EventEmission` from
`otel_genai_base` (v2.6 M-1). `HARNESS_BREAKER_ATTRIBUTES` no longer consumes
`GenAiAttribute`, but the `[U-OD-04]` edge is independently justified by
`SpanRef` / `EventEmission` and is preserved.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import EventEmission, SpanRef

__all__ = [
    "HARNESS_BREAKER_ATTRIBUTES",
    "BreakerEmissionError",
    "BreakerScope",
    "BreakerState",
    "HarnessBreakerEvent",
    "emit_breaker_trip_span_event",
]


#: The seven `harness.breaker.*` attribute names per §7.1 verbatim (acc #1).
#: v2.8 (D-3): re-typed `List<GenAiAttribute>` → `tuple[str, ...]` — the §7.1
#: table declares no tier classification, so the attributes are plain names.
HARNESS_BREAKER_ATTRIBUTES: tuple[str, ...] = (
    "harness.breaker.scope",
    "harness.breaker.from_state",
    "harness.breaker.to_state",
    "harness.breaker.trigger_count",
    "harness.breaker.permanent_fail_repeats",
    "harness.breaker.tool_id",
    "harness.breaker.model_version",
)


class BreakerScope(StrEnum):
    """The 2 `harness.breaker.scope` values (C-OD-07 §7.1).

    `harness.breaker.scope ∈ {per_model, per_provider}` per §7.1 verbatim.
    """

    PER_MODEL = "per_model"
    PER_PROVIDER = "per_provider"


class BreakerState(StrEnum):
    """The 3 breaker states (C-OD-07 §7.1).

    `harness.breaker.from_state` / `harness.breaker.to_state` ∈
    `{closed, open, half_open}` per §7.1 verbatim.
    """

    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


class HarnessBreakerEvent(BaseModel):
    """The breaker-trip event record (C-OD-07 §7.1).

    Frozen → `Eq`. Carries the seven §7.1 attributes: four non-optional
    (`scope`, `from_state`, `to_state`, `trigger_count`) and three optional
    (`permanent_fail_repeats`, `tool_id`, `model_version`) per the §7.1
    Definition-column conditional reading — preserved verbatim from v2.1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `harness.breaker.scope` — the breaker scope.
    scope: BreakerScope
    #: `harness.breaker.from_state` — source state.
    from_state: BreakerState
    #: `harness.breaker.to_state` — destination state.
    to_state: BreakerState
    #: `harness.breaker.trigger_count` — consecutive failures that tripped.
    trigger_count: int
    #: `harness.breaker.permanent_fail_repeats` — repeated-permanent-fail count;
    #: `None` when the trip is not from repeated C5 permanent-fail-exits.
    permanent_fail_repeats: int | None = None
    #: `harness.breaker.tool_id` — set when scope correlates with a tool (§7.1).
    tool_id: str | None = None
    #: `harness.breaker.model_version` — set when available (§7.1).
    model_version: str | None = None


class BreakerEmissionError(Exception):
    """Raised when a breaker-trip event cannot be emitted (C-OD-07 §7.2).

    The Python materialization of the `Result<EventEmission, BreakerEmissionError>`
    error arm of `emit_breaker_trip_span_event` — stack is Pydantic v2 + stdlib,
    no `Result` framework pull (CLAUDE.md §3.2 / I-6).
    """


#: The four non-optional `HarnessBreakerEvent` attributes — the §7.2
#: presence-enforced set (acc #9, v2.8 D-3 re-worded).
_NON_OPTIONAL_ATTRIBUTES: tuple[str, ...] = (
    "scope",
    "from_state",
    "to_state",
    "trigger_count",
)


def emit_breaker_trip_span_event(
    parent_span_ref: SpanRef,
    event: HarnessBreakerEvent,
) -> EventEmission:
    """Emit a `breaker.tripped` event at the parent span (C-OD-07 §7.1 / §7.2).

    Returns an `EventEmission` recording the emission. Raises
    `BreakerEmissionError` (the `Err` arm) if any of the four non-optional
    `HarnessBreakerEvent` attributes — `scope`, `from_state`, `to_state`,
    `trigger_count` — is missing (acc #9, v2.8 D-3). The three optional
    attributes (`permanent_fail_repeats`, `tool_id`, `model_version`) are
    populated when applicable per the §7.1 Definition-column conditional
    language.

    `breaker.tripped` events are always-sampled at all cells per §7.2 — the
    emitted `EventEmission.sampled` is `True` (composes with the U-OD-11
    always-sampled set). `parent_span_ref` and the return resolve to the
    U-OD-04 OTel-handle alias family; no `Span*` type is materialized here.
    """
    for attribute_name in _NON_OPTIONAL_ATTRIBUTES:
        if getattr(event, attribute_name, None) is None:
            raise BreakerEmissionError(
                f"breaker.tripped emission rejected: required attribute "
                f"'{attribute_name}' is missing (C-OD-07 §7.2)"
            )
    populated_attribute_count = sum(
        1
        for attribute_name in (
            "scope",
            "from_state",
            "to_state",
            "trigger_count",
            "permanent_fail_repeats",
            "tool_id",
            "model_version",
        )
        if getattr(event, attribute_name) is not None
    )
    return EventEmission(
        emitted_at_span=parent_span_ref,
        event_name="breaker.tripped",
        attribute_count=populated_attribute_count,
        sampled=True,  # always-sampled per §7.2 / C-OD-09
    )
