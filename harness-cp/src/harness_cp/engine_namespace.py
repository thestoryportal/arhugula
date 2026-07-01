"""`engine.*` span-attribute namespace + 4-attribute schema — U-CP-21.

Implements C-CP-09 §9.1 (the `engine.*` namespace declaration per the
four-attribute schema). Declares `EngineAttributeSchema`, the 4-entry
`ENGINE_NAMESPACE_SCHEMA`, the `ReplayDisposition` 5-value enum, and the
closed-and-total `REPLAY_DISPOSITION_MAPPING`.

The 4-attribute schema (v2.2 amendment from 3) absorbs F2-12 sub-scope (i)
closure — `engine.replay_disposition` is the 4th attribute, closed-mapped to
`engine.class` per ADR-D1 v1.2 §1.1.1. The mapping is **total** over
`EngineClass`: every engine class has exactly one replay disposition; no
cross-class sharing.

D6 ingestion delegates to U-CP-54 §24.1.A (specialization-layer namespace);
ingestion is out of scope at this unit.

Authority: Implementation_Plan_Control_Plane_v2_2.md §2.3 U-CP-21 (v2.2
4-attribute amendment; v2.6 §0.11 `[U-CP-00b]` edge-add — `value_type`/
`cardinality` resolve to harness-core); Spec_Control_Plane_v1_3.md §9 C-CP-09
§9.1 (v1.3 4-attribute extension); ADR-D1 v1.2 §1.1.1.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass


class ReplayDisposition(StrEnum):
    """The 5 engine replay dispositions (ADR-D1 v1.2 §1.1.1).

    Closed-mapped 1:1 to `EngineClass` per `REPLAY_DISPOSITION_MAPPING`.
    Member string values are the `engine.replay_disposition` attribute
    enumeration per CP spec v1.3 §9.1.
    """

    DETERMINISTIC_REPLAY = "deterministic_replay"
    CHECKPOINT_RESUME = "checkpoint_resume"
    NO_REPLAY = "no_replay"
    RECONCILER_ITERATION = "reconciler_iteration"
    WAL_CONSUME = "wal_consume"


class EngineAttributeSchema(BaseModel):
    """One `engine.*` span attribute (C-CP-09 §9.1 attribute-table row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality

    enum_values_when_enum: tuple[str, ...] | None
    """The closed value set when `value_type` is enum-valued — declared for
    `engine.class`, `engine.event_history.tier`, `engine.replay_disposition`;
    `None` for `engine.event.id` (opaque per-event ID)."""


# --- Registry population (spec C-CP-09 §9.1 4-attribute table) --------------

ENGINE_NAMESPACE_SCHEMA: tuple[EngineAttributeSchema, ...] = (
    EngineAttributeSchema(
        attribute_name="engine.class",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        enum_values_when_enum=tuple(c.value for c in EngineClass),
    ),
    EngineAttributeSchema(
        attribute_name="engine.event_history.tier",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        enum_values_when_enum=("Tier-3", "Tier-5"),
    ),
    EngineAttributeSchema(
        attribute_name="engine.event.id",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.PER_REQUEST,
        enum_values_when_enum=None,
    ),
    EngineAttributeSchema(
        attribute_name="engine.replay_disposition",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        enum_values_when_enum=tuple(d.value for d in ReplayDisposition),
    ),
)
"""The 4 `engine.*` attributes per CP spec v1.3 §9.1 verbatim — `engine.class`,
`engine.event_history.tier`, `engine.event.id`, `engine.replay_disposition`."""


# --- engine.class -> engine.replay_disposition closed mapping ---------------
# ADR-D1 v1.2 §1.1.1: total over EngineClass; one disposition per class; no
# cross-class sharing.
REPLAY_DISPOSITION_MAPPING: dict[EngineClass, ReplayDisposition] = {
    EngineClass.EVENT_SOURCED_REPLAY: ReplayDisposition.DETERMINISTIC_REPLAY,
    EngineClass.SAVE_POINT_CHECKPOINT: ReplayDisposition.CHECKPOINT_RESUME,
    EngineClass.PURE_PATTERN_NO_ENGINE: ReplayDisposition.NO_REPLAY,
    EngineClass.RECONCILER_LOOP: ReplayDisposition.RECONCILER_ITERATION,
    EngineClass.WAL_SEGMENT: ReplayDisposition.WAL_CONSUME,
}
"""The closed, total `engine.class -> engine.replay_disposition` mapping
(ADR-D1 v1.2 §1.1.1; U-CP-21 acceptance #3)."""
