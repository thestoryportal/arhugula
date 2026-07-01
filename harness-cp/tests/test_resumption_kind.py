"""Tests for U-CP-19 — resumption-kind taxonomy + bindings (C-CP-08 §8.1)."""

from __future__ import annotations

from harness_cp.engine_class import EngineClass
from harness_cp.resumption_kind import (
    RESUMPTION_KIND_BINDINGS,
    ResumptionKind,
    resumption_kind_for,
)


def test_resumption_kind_cardinality_five() -> None:
    """Acceptance #1, #3 — taxonomy closed at exactly five values."""
    assert len(ResumptionKind) == 5


def test_resumption_kind_values_match_spec_8_1_verbatim() -> None:
    """Acceptance #1 — value strings are the §8.1 `resumption.kind` column."""
    assert ResumptionKind.ENGINE_REPLAY.value == "engine_replay"
    assert ResumptionKind.SAVE_POINT_RESUME.value == "save_point_resume"
    assert ResumptionKind.JOURNAL_RESUME.value == "journal_resume"
    assert ResumptionKind.RECONCILER_CONVERGE.value == "reconciler_converge"
    assert ResumptionKind.SEGMENT_REPLAY.value == "segment_replay"


def test_resumption_kind_bindings_1to1_with_engine_class() -> None:
    """Acceptance #2 — 1:1 EngineClass → ResumptionKind mapping per §8.1."""
    assert len(RESUMPTION_KIND_BINDINGS) == 5
    assert {b.engine_class for b in RESUMPTION_KIND_BINDINGS} == set(EngineClass)
    expected = {
        EngineClass.EVENT_SOURCED_REPLAY: ResumptionKind.ENGINE_REPLAY,
        EngineClass.SAVE_POINT_CHECKPOINT: ResumptionKind.SAVE_POINT_RESUME,
        EngineClass.PURE_PATTERN_NO_ENGINE: ResumptionKind.JOURNAL_RESUME,
        EngineClass.RECONCILER_LOOP: ResumptionKind.RECONCILER_CONVERGE,
        EngineClass.WAL_SEGMENT: ResumptionKind.SEGMENT_REPLAY,
    }
    assert {b.engine_class: b.resumption_kind for b in RESUMPTION_KIND_BINDINGS} == expected


def test_resumption_kind_for_total_over_engine_class() -> None:
    """Acceptance #2 — resumption_kind_for resolves every EngineClass value."""
    for ec in EngineClass:
        assert isinstance(resumption_kind_for(ec), ResumptionKind)
