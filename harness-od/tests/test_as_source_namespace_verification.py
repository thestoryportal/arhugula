"""Tests for U-OD-06 — AS-source namespace set verification (7 rows).

Test set per the U-OD-06 `Tests:` field (Implementation_Plan_Operational_Discipline_v2_1.md
§3.2.3). Every acceptance criterion maps to at least one test.

Acceptance criteria (C-OD-05 §5.1 AS-source rows):
  #1 — AS_SOURCE_NAMESPACE_PREFIXES cardinality 7 per §5.1.
  #2 — per-prefix attribute count per AS plan U-AS-33 manifest.
  #3 — verify_as_source_namespace_set returns Err on set drift.
  #4 — assert_namespace_attribute_count returns Err on count drift.
  #5 — Pattern P1 byte-exact discipline against U-AS-33 manifest.
  #6 — cross-axis edge annotation (U-AS-33; C-AS-16 §16.1 + §16.4).
"""

from __future__ import annotations

import pytest
from harness_od.as_source_namespace_verification import (
    AS_SOURCE_NAMESPACE_PREFIXES,
    AttributeCountMismatch,
    NamespaceSetMismatch,
    assert_namespace_attribute_count,
    verify_as_source_namespace_set,
)


def test_as_source_namespace_prefixes_cardinality_seven() -> None:
    """Acceptance #1 — exactly 7 AS-source prefixes per §5.1 rows 1-5, 12, 13."""
    assert len(AS_SOURCE_NAMESPACE_PREFIXES) == 7


def test_namespace_prefixes_byte_exact() -> None:
    """Acceptance #1 / #5 — the prefix set is byte-exact against §5.1."""
    assert AS_SOURCE_NAMESPACE_PREFIXES == frozenset(
        {
            "anthropic.",
            "mcp.",
            "skill.",
            "managed_agents.",
            "sandbox.",
            "files.",
            "memory.",
        }
    )


def test_verify_as_source_namespace_set_match_ok() -> None:
    """Acceptance #3 — verify returns None (Ok) for an exact-match set."""
    assert verify_as_source_namespace_set(AS_SOURCE_NAMESPACE_PREFIXES) is None


def test_verify_as_source_namespace_set_mismatch_err() -> None:
    """Acceptance #3 / #5 — verify raises NamespaceSetMismatch on drift.

    Covers a missing prefix, an extra prefix, and a rename.
    """
    with pytest.raises(NamespaceSetMismatch):
        verify_as_source_namespace_set(AS_SOURCE_NAMESPACE_PREFIXES - {"mcp."})
    with pytest.raises(NamespaceSetMismatch):
        verify_as_source_namespace_set(AS_SOURCE_NAMESPACE_PREFIXES | {"routing."})
    with pytest.raises(NamespaceSetMismatch):
        verify_as_source_namespace_set((AS_SOURCE_NAMESPACE_PREFIXES - {"mcp."}) | {"mcp"})


def test_assert_namespace_attribute_count_per_prefix() -> None:
    """Acceptance #2 — assert returns None (Ok) when observed == expected.

    Per-prefix expected counts per the AS plan U-AS-33 manifest as ingested at
    the landed U-OD-05 namespace map (`anthropic.` 10, `mcp.` 7, `skill.` 6,
    `managed_agents.` 3, `sandbox.` 7, `files.` 8, `memory.` 6).
    """
    expected = {
        "anthropic.": 10,
        "mcp.": 7,
        "skill.": 6,
        "managed_agents.": 3,
        "sandbox.": 7,
        "files.": 8,
        "memory.": 6,
    }
    for prefix, count in expected.items():
        assert assert_namespace_attribute_count(prefix, count, count) is None


def test_assert_namespace_attribute_count_mismatch_err() -> None:
    """Acceptance #4 — assert raises AttributeCountMismatch on a count drift."""
    with pytest.raises(AttributeCountMismatch):
        assert_namespace_attribute_count("anthropic.", 10, 9)


def test_assert_namespace_attribute_count_rejects_non_as_prefix() -> None:
    """Acceptance #4 — a non-AS-source prefix cannot be count-verified."""
    with pytest.raises(AttributeCountMismatch):
        assert_namespace_attribute_count("hitl.", 11, 11)


def test_cross_axis_edge_to_u_as_33_declared() -> None:
    """Acceptance #6 — the cross-axis edge to U-AS-33 (C-AS-16 §16.1 + §16.4)
    is declared in the module's cross-axis posture statement.

    Per Phase 7 7b discipline the cross-axis edge is cited by contract section;
    placeholder unit-ID resolution is a 7c concern. The module docstring is the
    OD-side declaration of the edge target + contract anchors.
    """
    import harness_od.as_source_namespace_verification as mod

    assert mod.__doc__ is not None
    assert "U-AS-33" in mod.__doc__
    assert "C-AS-16 §16.1 + §16.4" in mod.__doc__
