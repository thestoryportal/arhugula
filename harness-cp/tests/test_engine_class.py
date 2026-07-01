"""Tests for U-CP-15 — engine-class taxonomy + capability floors (C-CP-07).

Test set per the U-CP-15 `Tests:` field. Acceptance-criterion coverage:
  #1 cardinality 5             -> test_engine_class_cardinality_five
  #1 values verbatim per §7.1  -> test_engine_class_values_match_spec_verbatim
  #2 substrate-citation narrative -> docstrings (pyright strict + #1 tests)
  #3 taxonomy closed at 5      -> test_taxonomy_closed
  #4 capability floors per §7.4 -> test_capability_floors_per_class_match_spec
"""

from __future__ import annotations

from harness_cp.engine_class import CAPABILITY_FLOORS, EngineClass

# 5 class values, verbatim from Spec_Control_Plane_v1_2.md §7.1 "Class" column.
_SPEC_CLASSES = {
    "event-sourced-replay",
    "save-point-checkpoint",
    "pure-pattern-no-engine",
    "reconciler-loop",
    "WAL-segment",
}

# The 4 F3 capability-floors per §7.4 (i)-(iv).
_SPEC_FLOOR_NAMES = {
    "durable_replay_across_restart",
    "idempotency_keyed_exactly_once",
    "lease_coordination",
    "observable_lifecycle",
}


def test_engine_class_cardinality_five() -> None:
    """Acceptance #1 — exactly five engine classes."""
    assert len(EngineClass) == 5


def test_engine_class_values_match_spec_verbatim() -> None:
    """Acceptance #1 — class values match C-CP-07 §7.1 verbatim."""
    assert {c.value for c in EngineClass} == _SPEC_CLASSES


def test_taxonomy_closed() -> None:
    """Acceptance #3 — taxonomy closed at cardinality 5 (no extension)."""
    assert len(EngineClass) == 5
    assert len(_SPEC_CLASSES) == 5


def test_capability_floors_per_class_match_spec() -> None:
    """Acceptance #4 — the 4 F3 floors per §7.4, each required at all 5 classes."""
    assert {f.capability_name for f in CAPABILITY_FLOORS} == _SPEC_FLOOR_NAMES
    for floor in CAPABILITY_FLOORS:
        assert floor.required_at_class == frozenset(EngineClass)
        assert "C-CP-07 §7.4" in floor.rationale
