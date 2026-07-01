"""Tests for U-OD-07 — CP-source namespace set verification (6 rows).

Test set per the U-OD-07 `Tests:` field (Implementation_Plan_Operational_Discipline_v2_1.md
§3.2.4). Every acceptance criterion maps to at least one test.

Acceptance criteria (C-OD-05 §5.1 CP-source rows):
  #1 — CP_SOURCE_NAMESPACE_PREFIXES cardinality 6 per §5.1.
  #2 — per-prefix attribute count per CP plan U-CP-54 manifest.
  #3 — routing.* excluded from the CP-source set.
  #4 — Pattern P1 byte-exact discipline against U-CP-54 manifest.
  #5 — cross-axis edge annotation (U-CP-54; C-CP-24 §24.1.A + §24.1.B).
"""

from __future__ import annotations

import pytest
from harness_od.cp_source_namespace_verification import (
    CP_SOURCE_NAMESPACE_PREFIXES,
    AttributeCountMismatch,
    NamespaceSetMismatch,
    assert_namespace_attribute_count,
    verify_cp_source_namespace_set,
)


def test_cp_source_namespace_prefixes_cardinality_six() -> None:
    """Acceptance #1 — exactly 6 CP-source prefixes per §5.1 rows 6-11."""
    assert len(CP_SOURCE_NAMESPACE_PREFIXES) == 6


def test_namespace_prefixes_byte_exact_per_section_5_1() -> None:
    """Acceptance #1 / #4 — the prefix set is byte-exact against §5.1.

    The topology prefix is `topology.fanout.` per §5.1 row 7 (spec canonical;
    the plan §3.2.4 `"topology."` transcription is a typo conformed here).
    """
    assert CP_SOURCE_NAMESPACE_PREFIXES == frozenset(
        {
            "hitl.",
            "topology.fanout.",
            "subagent.",
            "engine.",
            "audit.",
            "validator.fail.",
        }
    )


def test_routing_namespace_excluded_from_cp_source_set() -> None:
    """Acceptance #3 — routing.* is NOT a CP-source ingestion prefix."""
    assert "routing." not in CP_SOURCE_NAMESPACE_PREFIXES
    with pytest.raises(NamespaceSetMismatch):
        verify_cp_source_namespace_set(CP_SOURCE_NAMESPACE_PREFIXES | {"routing."})


def test_verify_cp_source_namespace_set_match_ok() -> None:
    """Acceptance #4 — verify returns None (Ok) for an exact-match set."""
    assert verify_cp_source_namespace_set(CP_SOURCE_NAMESPACE_PREFIXES) is None


def test_verify_cp_source_namespace_set_mismatch_err() -> None:
    """Acceptance #4 — verify raises NamespaceSetMismatch on a missing prefix."""
    with pytest.raises(NamespaceSetMismatch):
        verify_cp_source_namespace_set(CP_SOURCE_NAMESPACE_PREFIXES - {"engine."})


def test_assert_namespace_attribute_count_per_prefix() -> None:
    """Acceptance #2 — assert returns None (Ok) when observed == expected.

    Per-prefix expected counts per the CP plan U-CP-54 manifest as ingested at
    the landed U-OD-05 namespace map (`hitl.` 11, `topology.fanout.` 10,
    `subagent.` 7, `engine.` 3, `audit.` 7, `validator.fail.` 3).
    """
    expected = {
        "hitl.": 11,
        "topology.fanout.": 10,
        "subagent.": 7,
        "engine.": 3,
        "audit.": 7,
        "validator.fail.": 3,
    }
    for prefix, count in expected.items():
        assert assert_namespace_attribute_count(prefix, count, count) is None


def test_assert_namespace_attribute_count_mismatch_err() -> None:
    """Acceptance #2 — assert raises AttributeCountMismatch on a count drift."""
    with pytest.raises(AttributeCountMismatch):
        assert_namespace_attribute_count("audit.", 7, 6)


def test_assert_namespace_attribute_count_rejects_routing() -> None:
    """Acceptance #3 — routing.* cannot be count-verified as CP-source."""
    with pytest.raises(AttributeCountMismatch):
        assert_namespace_attribute_count("routing.", 1, 1)


def test_cross_axis_edge_to_u_cp_54_declared() -> None:
    """Acceptance #5 — the cross-axis edge to U-CP-54 (C-CP-24 §24.1.A +
    §24.1.B) is declared in the module's cross-axis posture statement."""
    import harness_od.cp_source_namespace_verification as mod

    assert mod.__doc__ is not None
    assert "U-CP-54" in mod.__doc__
    assert "C-CP-24 §24.1.A + §24.1.B" in mod.__doc__
