"""Resumption-kind taxonomy + engine-class bindings — U-CP-19.

Implements C-CP-08 §8.1 (five-class `resumption.kind` taxonomy + the 1:1
`EngineClass → ResumptionKind` mapping). Declares the closed 5-value
`ResumptionKind` enum, the `ResumptionKindBinding` record, and the populated
`RESUMPTION_KIND_BINDINGS`.

The taxonomy is **closed** at cardinality 5 per C-CP-08 §8.1; extension is a
Workflow §4.1.2 Class-2 D1 revision.

Authority: Implementation_Plan_Control_Plane_v2_4.md §2.3 U-CP-19 (v2.4
verbatim-divergence conformance — enum + bindings conformed to CP spec §8.1);
Spec_Control_Plane_v1_3.md §8 C-CP-08 §8.1; ADR-D1 v1.2.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass


class ResumptionKind(StrEnum):
    """The 5 resumption kinds (C-CP-08 §8.1).

    Member string values are the §8.1 `resumption.kind` column verbatim. Each
    value is the resumption mechanism for exactly one `EngineClass`.
    """

    ENGINE_REPLAY = "engine_replay"
    """Event-sourced-replay engines (C-CP-08 §8.1)."""

    SAVE_POINT_RESUME = "save_point_resume"
    """Save-point-checkpoint engines (C-CP-08 §8.1)."""

    JOURNAL_RESUME = "journal_resume"
    """Pure-pattern-no-engine engines (C-CP-08 §8.1)."""

    RECONCILER_CONVERGE = "reconciler_converge"
    """Reconciler-loop engines (C-CP-08 §8.1)."""

    SEGMENT_REPLAY = "segment_replay"
    """WAL-segment engines (C-CP-08 §8.1)."""


class ResumptionKindBinding(BaseModel):
    """One `EngineClass → ResumptionKind` binding (C-CP-08 §8.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: EngineClass
    resumption_kind: ResumptionKind


# C-CP-08 §8.1 — 1:1 mapping, one binding per EngineClass value.
_RESUMPTION_KIND_BY_ENGINE: Mapping[EngineClass, ResumptionKind] = MappingProxyType(
    {
        EngineClass.EVENT_SOURCED_REPLAY: ResumptionKind.ENGINE_REPLAY,
        EngineClass.SAVE_POINT_CHECKPOINT: ResumptionKind.SAVE_POINT_RESUME,
        EngineClass.PURE_PATTERN_NO_ENGINE: ResumptionKind.JOURNAL_RESUME,
        EngineClass.RECONCILER_LOOP: ResumptionKind.RECONCILER_CONVERGE,
        EngineClass.WAL_SEGMENT: ResumptionKind.SEGMENT_REPLAY,
    }
)

RESUMPTION_KIND_BINDINGS: tuple[ResumptionKindBinding, ...] = tuple(
    ResumptionKindBinding(engine_class=ec, resumption_kind=rk)
    for ec, rk in _RESUMPTION_KIND_BY_ENGINE.items()
)
"""The 5 `EngineClass → ResumptionKind` bindings (C-CP-08 §8.1); 1:1 with
`EngineClass`."""


def resumption_kind_for(engine_class: EngineClass) -> ResumptionKind:
    """Return the resumption kind for an engine class (C-CP-08 §8.1).

    Total over `EngineClass` — the §8.1 mapping is 1:1.
    """
    return _RESUMPTION_KIND_BY_ENGINE[engine_class]
