"""Alignment-floor drift detection + drift-detection emission shape — U-OD-25.

Implements C-OD-18 §18.1 (alignment-floor drift detection — the four
alignment-floor primitives and their re-baselining trigger) and §18.2
(drift-detection emission shape — the `gen_ai.eval.alignment_floor.
drift_detected` span event with four attributes, always-sampled per C-OD-09
§9.2).

`AlignmentFloorPrimitive` enumerates the four alignment-floor primitives per
§18.1; `AlignmentFloorThreshold` carries one primitive's operator-tunable
threshold over an `ObservationWindow`. `DRIFT_DETECTED_EVENT_NAME` is the
byte-exact §18.2 event name; `DRIFT_DETECTED_SAMPLING_HEAD_RATE == 1.0` is the
always-sampled head rate. `DriftDetectedEventAttributes` carries the four
§18.2 event attributes; `DRIFT_DETECTED_ATTRIBUTE_NAMES` carries the four
canonical `gen_ai.eval.*` attribute names byte-exact. `detect_drift` returns
the event attributes when the current value is below threshold;
`emit_drift_event` emits the span event at head=1.0.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.6.3 U-OD-25
(v2.6 M-1 revision — `SpanRef` + `EventEmission` at `emit_drift_event`
re-pointed to the U-OD-04 carrier; `[U-OD-04]` edge added; all v2.1 surfaces
preserved verbatim from v2.1 §3.6.3);
Spec_Operational_Discipline_v1_2.md §18 C-OD-18 §18.1 + §18.2 (preserved
verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.6.

Depends on: [U-OD-11, U-OD-23, U-OD-24, U-OD-04]. `SpanRef` / `EventEmission`
from U-OD-04 (`otel_genai_base`). U-OD-11 (sampling discipline) is the
always-sampled regime reference — the §18.2 drift event emits at head=1.0
per the C-OD-09 §9.2 always-sampled discipline (rare, load-bearing for
meta-eval correctness); `DRIFT_DETECTED_SAMPLING_HEAD_RATE` is the
materialized always-sampled head rate. U-OD-23 (operator-burden eval
primitives) and U-OD-24 (eval dashboard binding) are structural-composition
references — three of the four alignment-floor primitives overlap with
U-OD-23's `OperatorBurdenEvalPrimitive` set, but the §18.1 primitive set is
its own enum (the fourth, judge-human Cohen's kappa, has no U-OD-23 member).

Plan-vs-spec note. The §9.2 always-sampled exception set (18 rows, landed at
U-OD-11) is enumerated by namespace and does not contain a literal
`gen_ai.eval.alignment_floor.drift_detected` entry. The §18.2 drift event is a
`gen_ai.eval.*` span event whose always-sampled head=1.0 posture is asserted
by §18.2 itself ("Always-sampled per C-OD-09 §9.2 — rare, load-bearing for
meta-eval correctness"). Acceptance #4 is satisfied via the
`DRIFT_DETECTED_SAMPLING_HEAD_RATE == 1.0` constant — the §9.2 reference is to
the always-sampled *discipline*, not to literal §9.2-set membership; the §9.2
set stays closed at 18 rows.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.otel_genai_base import EventEmission, SpanRef

__all__ = [
    "DRIFT_DETECTED_ATTRIBUTE_NAMES",
    "DRIFT_DETECTED_EVENT_NAME",
    "DRIFT_DETECTED_SAMPLING_HEAD_RATE",
    "AlignmentFloorPrimitive",
    "AlignmentFloorThreshold",
    "DriftDetectedEventAttributes",
    "DriftEmissionError",
    "ObservationWindow",
    "ObservationWindowKind",
    "detect_drift",
    "emit_drift_event",
]


class AlignmentFloorPrimitive(StrEnum):
    """The four alignment-floor drift-detection primitives (C-OD-18 §18.1).

    Exactly 4 values per the §18.1 table. Three overlap with the U-OD-23
    operator-burden eval primitive set (`CACHE_HIT_RATE_ALIGNMENT_FLOOR`,
    `ROUTING_ACCURACY_HOLDOUT`, `SANDBOX_TIER_ROUTING_ACCURACY`); the fourth,
    `JUDGE_HUMAN_COHENS_KAPPA`, is anchored at the `c8-eval-engineer` SKILL.md
    meta-eval discipline and has no U-OD-23 counterpart.
    """

    JUDGE_HUMAN_COHENS_KAPPA = "judge_human_cohens_kappa"
    CACHE_HIT_RATE_ALIGNMENT_FLOOR = "cache_hit_rate_alignment_floor"
    ROUTING_ACCURACY_HOLDOUT = "routing_accuracy_holdout"
    SANDBOX_TIER_ROUTING_ACCURACY = "sandbox_tier_routing_accuracy"


class ObservationWindowKind(StrEnum):
    """The kind discriminator for an `ObservationWindow` (C-OD-18 §18.1)."""

    TIME_WINDOW = "TIME_WINDOW"
    SAMPLE_WINDOW = "SAMPLE_WINDOW"


class ObservationWindow(BaseModel):
    """The window over which drift is computed (C-OD-18 §18.1 / §18.2).

    A tagged union of the two §18.1 window forms: `TIME_WINDOW` carries a
    duration in seconds; `SAMPLE_WINDOW` carries a span-count. Exactly one of
    `time_window_seconds` / `sample_window_count` is populated, matching
    `kind`. Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: which window form this is.
    kind: ObservationWindowKind
    #: the time-window duration in seconds (`None` unless `kind` is
    #: `TIME_WINDOW`).
    time_window_seconds: float | None = None
    #: the sample-window span-count (`None` unless `kind` is `SAMPLE_WINDOW`).
    sample_window_count: int | None = None


class AlignmentFloorThreshold(BaseModel):
    """One alignment-floor primitive's operator-tunable threshold (§18.1).

    `threshold_value` is operator-tunable — deferred to deployment-binding time
    per the `c8-eval-engineer` SKILL.md ownership (§18.1 "operator-tunable").
    Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the alignment-floor primitive this threshold governs.
    primitive: AlignmentFloorPrimitive
    #: the operator-tunable drift threshold value.
    threshold_value: float
    #: the window over which drift is computed.
    observation_window: ObservationWindow


#: The §18.2 drift-detection span-event name, byte-exact.
DRIFT_DETECTED_EVENT_NAME: str = "gen_ai.eval.alignment_floor.drift_detected"

#: The §18.2 always-sampled head rate — the drift event emits at head=1.0
#: across all cells per the C-OD-09 §9.2 always-sampled discipline (rare,
#: load-bearing for meta-eval correctness).
DRIFT_DETECTED_SAMPLING_HEAD_RATE: float = 1.0

#: The four §18.2 drift-event attribute names, byte-exact, in §18.2 table
#: order. The `DriftDetectedEventAttributes` fields map one-to-one to these.
DRIFT_DETECTED_ATTRIBUTE_NAMES: tuple[str, str, str, str] = (
    "gen_ai.eval.primitive",
    "gen_ai.eval.alignment_floor.current",
    "gen_ai.eval.alignment_floor.threshold",
    "gen_ai.eval.alignment_floor.observation_window",
)


class DriftDetectedEventAttributes(BaseModel):
    """The four §18.2 drift-detection event attributes.

    Exactly four attributes per §18.2 verbatim — field order and canonical
    `gen_ai.eval.*` attribute names per `DRIFT_DETECTED_ATTRIBUTE_NAMES`.
    Frozen → `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `gen_ai.eval.primitive` — which primitive drifted.
    primitive: AlignmentFloorPrimitive
    #: `gen_ai.eval.alignment_floor.current` — the current ratio value.
    current_value: float
    #: `gen_ai.eval.alignment_floor.threshold` — the drift threshold.
    threshold: float
    #: `gen_ai.eval.alignment_floor.observation_window` — the window over which
    #: drift was computed.
    observation_window: ObservationWindow


class DriftEmissionError(Exception):
    """Raised when a drift-event emission is missing a required attribute.

    The Python materialization of the `Result<EventEmission, DriftEmissionError>`
    error arm in the U-OD-25 `emit_drift_event` signature — inline per the OD
    plan §0.8 error-type discipline; stack is Pydantic v2 + stdlib (no `Result`
    framework pull — CLAUDE.md §3.2 / I-6).
    """


def detect_drift(
    primitive: AlignmentFloorPrimitive,
    current_value: float,
    threshold: AlignmentFloorThreshold,
) -> DriftDetectedEventAttributes | None:
    """Detect alignment-floor drift for `primitive` (C-OD-18 §18.1).

    Returns `DriftDetectedEventAttributes` (the `Some` arm) when `current_value`
    has drifted below `threshold.threshold_value` over the threshold's
    observation window — the drift signal that triggers a `*_floor`
    re-baselining cycle per the `c8-eval-engineer` meta-eval discipline.
    Returns `None` (the `None` arm) when the current value is at or above the
    threshold — no drift.

    Re-baselining cycle invocation is deferred per §18.1 "Deferred to
    implementation discretion" — this function emits the drift signal; the
    downstream `c8-eval-engineer` workflow executes the cycle.
    """
    if current_value < threshold.threshold_value:
        return DriftDetectedEventAttributes(
            primitive=primitive,
            current_value=current_value,
            threshold=threshold.threshold_value,
            observation_window=threshold.observation_window,
        )
    return None


def emit_drift_event(
    parent_span_ref: SpanRef,
    attrs: DriftDetectedEventAttributes,
) -> EventEmission:
    """Emit the drift-detection span event at head=1.0 (C-OD-18 §18.2).

    Emits a `gen_ai.eval.alignment_floor.drift_detected` span event on
    `parent_span_ref` carrying the four §18.2 attributes. The event is always-
    sampled — emitted at `DRIFT_DETECTED_SAMPLING_HEAD_RATE` (1.0) across all
    cells per the C-OD-09 §9.2 always-sampled discipline. Returns the
    `EventEmission` return-record (the `Ok` arm).

    Raises `DriftEmissionError` if any of the four required §18.2 attributes is
    absent. `attrs` is a frozen `DriftDetectedEventAttributes` with
    `extra="forbid"`, so all four fields are present by construction; the guard
    re-verifies against `DRIFT_DETECTED_ATTRIBUTE_NAMES` as the explicit §18.2
    completeness check.
    """
    present = set(attrs.model_dump().keys())
    field_to_attr = {
        "primitive": "gen_ai.eval.primitive",
        "current_value": "gen_ai.eval.alignment_floor.current",
        "threshold": "gen_ai.eval.alignment_floor.threshold",
        "observation_window": "gen_ai.eval.alignment_floor.observation_window",
    }
    missing = [field_to_attr[f] for f in field_to_attr if f not in present]
    if missing:
        raise DriftEmissionError(
            f"drift-event emission is missing required §18.2 attribute(s): "
            f"{', '.join(missing)} (C-OD-18 §18.2 — all four "
            "gen_ai.eval.* attributes are required)"
        )
    parent_span_ref.add_event(
        name=DRIFT_DETECTED_EVENT_NAME,
        attributes={
            "gen_ai.eval.primitive": attrs.primitive.value,
            "gen_ai.eval.alignment_floor.current": attrs.current_value,
            "gen_ai.eval.alignment_floor.threshold": attrs.threshold,
            "gen_ai.eval.alignment_floor.observation_window": (attrs.observation_window.kind.value),
        },
    )
    return EventEmission(
        emitted_at_span=parent_span_ref,
        event_name=DRIFT_DETECTED_EVENT_NAME,
        attribute_count=len(DRIFT_DETECTED_ATTRIBUTE_NAMES),
        sampled=True,
    )
