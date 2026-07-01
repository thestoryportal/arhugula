"""Tests for U-CP-21 — `engine.*` namespace + 4-attribute schema (C-CP-09 §9.1).

Acceptance-criterion coverage:
  #1 4 attributes per §9.1       -> test_engine_namespace_cardinality_four,
                                    test_engine_attributes_match_spec_v1_3_verbatim
  #2 per-attribute cardinality   -> test_engine_class_cardinality_bounded_five,
                                    test_engine_event_history_tier_enum_two_values,
                                    test_engine_event_id_opaque_string,
                                    test_engine_replay_disposition_enum_five_values
  #3 closed total mapping        -> test_engine_replay_disposition_closed_mapped,
                                    test_replay_disposition_mapping_total_over_engine_class
  #4 D6 ingestion delegated      -> structural (no ingestion surface at this unit)
"""

from __future__ import annotations

from harness_cp.engine_class import EngineClass
from harness_cp.engine_namespace import (
    ENGINE_NAMESPACE_SCHEMA,
    REPLAY_DISPOSITION_MAPPING,
    ReplayDisposition,
)

_SPEC_ATTRIBUTES = {
    "engine.class",
    "engine.event_history.tier",
    "engine.event.id",
    "engine.replay_disposition",
}


def _by_name(name: str) -> object:
    return next(a for a in ENGINE_NAMESPACE_SCHEMA if a.attribute_name == name)


def test_engine_namespace_cardinality_four() -> None:
    """Acceptance #1 — exactly four `engine.*` attributes."""
    assert len(ENGINE_NAMESPACE_SCHEMA) == 4


def test_engine_attributes_match_spec_v1_3_verbatim() -> None:
    """Acceptance #1 — attribute names match CP spec v1.3 §9.1 verbatim."""
    assert {a.attribute_name for a in ENGINE_NAMESPACE_SCHEMA} == _SPEC_ATTRIBUTES


def test_engine_class_cardinality_bounded_five() -> None:
    """Acceptance #2 — `engine.class` enum-valued, bounded-5 (matches EngineClass)."""
    attr = _by_name("engine.class")
    assert attr.enum_values_when_enum is not None  # type: ignore[attr-defined]
    assert set(attr.enum_values_when_enum) == {c.value for c in EngineClass}  # type: ignore[attr-defined]
    assert len(attr.enum_values_when_enum) == 5  # type: ignore[attr-defined]


def test_engine_event_history_tier_enum_two_values() -> None:
    """Acceptance #2 — `engine.event_history.tier` is bounded-2 (Tier-3/Tier-5)."""
    attr = _by_name("engine.event_history.tier")
    assert attr.enum_values_when_enum == ("Tier-3", "Tier-5")  # type: ignore[attr-defined]


def test_engine_event_id_opaque_string() -> None:
    """Acceptance #2 — `engine.event.id` is opaque (no enum value set)."""
    attr = _by_name("engine.event.id")
    assert attr.enum_values_when_enum is None  # type: ignore[attr-defined]


def test_engine_replay_disposition_enum_five_values() -> None:
    """Acceptance #2 — `engine.replay_disposition` is bounded-5."""
    attr = _by_name("engine.replay_disposition")
    assert attr.enum_values_when_enum is not None  # type: ignore[attr-defined]
    assert len(attr.enum_values_when_enum) == 5  # type: ignore[attr-defined]
    assert set(attr.enum_values_when_enum) == {  # type: ignore[attr-defined]
        d.value for d in ReplayDisposition
    }


def test_replay_disposition_cardinality_five() -> None:
    """`ReplayDisposition` declares exactly five values (ADR-D1 v1.2 §1.1.1)."""
    assert len(ReplayDisposition) == 5
    assert {d.value for d in ReplayDisposition} == {
        "deterministic_replay",
        "checkpoint_resume",
        "no_replay",
        "reconciler_iteration",
        "wal_consume",
    }


def test_engine_replay_disposition_closed_mapped() -> None:
    """Acceptance #3 — the §1.1.1 closed mapping per engine class verbatim."""
    assert REPLAY_DISPOSITION_MAPPING == {
        EngineClass.EVENT_SOURCED_REPLAY: ReplayDisposition.DETERMINISTIC_REPLAY,
        EngineClass.SAVE_POINT_CHECKPOINT: ReplayDisposition.CHECKPOINT_RESUME,
        EngineClass.PURE_PATTERN_NO_ENGINE: ReplayDisposition.NO_REPLAY,
        EngineClass.RECONCILER_LOOP: ReplayDisposition.RECONCILER_ITERATION,
        EngineClass.WAL_SEGMENT: ReplayDisposition.WAL_CONSUME,
    }


def test_replay_disposition_mapping_total_over_engine_class() -> None:
    """Acceptance #3 — mapping is total: every EngineClass has exactly one entry."""
    assert set(REPLAY_DISPOSITION_MAPPING) == set(EngineClass)
    # No cross-class sharing — distinct dispositions for distinct classes.
    assert len(set(REPLAY_DISPOSITION_MAPPING.values())) == 5
