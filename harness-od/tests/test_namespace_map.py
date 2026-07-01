"""Tests for U-OD-05 — 15-row namespace ingestion map (C-OD-05 §5.1 / §5.2).

Test set per the U-OD-05 `Tests:` field (Implementation_Plan_Operational_Discipline_v2_1.md
§3.2.2). Every acceptance criterion maps to at least one test.
"""

from __future__ import annotations

import pytest
from harness_od.namespace_map import (
    NAMESPACE_MAP,
    AuthorityViolation,
    NamespaceMapRow,
    NamespaceSourceAxis,
    assert_source_authoritative_declarer,
    lookup_namespace,
)


def test_namespace_source_axis_cardinality_four() -> None:
    """Acceptance #1 — NamespaceSourceAxis enumerates exactly 4 values."""
    assert len(NamespaceSourceAxis) == 4
    assert set(NamespaceSourceAxis) == {
        NamespaceSourceAxis.OD_CANONICAL,
        NamespaceSourceAxis.AS_SOURCE,
        NamespaceSourceAxis.CP_SOURCE,
        NamespaceSourceAxis.SUBSTRATE_ANCHORED_OUTSIDE_CP,
    }


def test_namespace_map_cardinality_fifteen() -> None:
    """Acceptance #2 — NAMESPACE_MAP declares exactly 15 entries per §5.1."""
    assert len(NAMESPACE_MAP) == 15


def test_namespace_source_axis_breakdown_1_7_6_1() -> None:
    """Acceptance #3 — per-row source-axis breakdown matches §5.1 verbatim:
    1 OD_CANONICAL + 7 AS_SOURCE + 6 CP_SOURCE + 1 SUBSTRATE_ANCHORED_OUTSIDE_CP."""
    counts = {axis: 0 for axis in NamespaceSourceAxis}
    for row in NAMESPACE_MAP:
        counts[row.source_axis] += 1
    assert counts[NamespaceSourceAxis.OD_CANONICAL] == 1
    assert counts[NamespaceSourceAxis.AS_SOURCE] == 7
    assert counts[NamespaceSourceAxis.CP_SOURCE] == 6
    assert counts[NamespaceSourceAxis.SUBSTRATE_ANCHORED_OUTSIDE_CP] == 1


def test_as_source_namespace_set() -> None:
    """Acceptance #3 — the 7 AS-source namespaces per §5.1 verbatim."""
    as_prefixes = {
        row.namespace_prefix
        for row in NAMESPACE_MAP
        if row.source_axis is NamespaceSourceAxis.AS_SOURCE
    }
    assert as_prefixes == {
        "anthropic.",
        "mcp.",
        "skill.",
        "managed_agents.",
        "sandbox.",
        "files.",
        "memory.",
    }


def test_cp_source_namespace_set() -> None:
    """Acceptance #3 — the 6 CP-source namespaces per §5.1 verbatim."""
    cp_prefixes = {
        row.namespace_prefix
        for row in NAMESPACE_MAP
        if row.source_axis is NamespaceSourceAxis.CP_SOURCE
    }
    assert cp_prefixes == {
        "hitl.",
        "topology.fanout.",
        "subagent.",
        "engine.",
        "audit.",
        "validator.fail.",
    }


def test_provider_discriminator_od_canonical() -> None:
    """Acceptance #3 — provider_discriminator is the sole OD_CANONICAL row."""
    row = lookup_namespace("provider_discriminator")
    assert row is not None
    assert row.source_axis is NamespaceSourceAxis.OD_CANONICAL
    assert row.attribute_count == 1


def test_anthropic_namespace_as_source() -> None:
    """Acceptance #3 — anthropic.* is AS-source with 10 attributes per §5.1."""
    row = lookup_namespace("anthropic.")
    assert row is not None
    assert row.source_axis is NamespaceSourceAxis.AS_SOURCE
    assert row.attribute_count == 10


def test_hitl_namespace_cp_source() -> None:
    """Acceptance #3 — hitl.* is CP-source per §5.1."""
    row = lookup_namespace("hitl.")
    assert row is not None
    assert row.source_axis is NamespaceSourceAxis.CP_SOURCE


def test_harness_breaker_substrate_anchored_outside_cp() -> None:
    """Acceptance #3 — harness.breaker.* is the sole SUBSTRATE_ANCHORED_OUTSIDE_CP
    row per F-CP-01 Stage 3b alignment."""
    row = lookup_namespace("harness.breaker.")
    assert row is not None
    assert row.source_axis is NamespaceSourceAxis.SUBSTRATE_ANCHORED_OUTSIDE_CP
    assert row.attribute_count == 7


def test_source_contract_ref_present_per_row() -> None:
    """Acceptance #4 — every row carries a non-empty source_contract_ref."""
    for row in NAMESPACE_MAP:
        assert row.source_contract_ref
        assert isinstance(row.source_contract_ref, str)


def test_lookup_namespace_existing_returns_some() -> None:
    """Acceptance #5 — lookup_namespace returns the row for any declared prefix."""
    for declared in NAMESPACE_MAP:
        found = lookup_namespace(declared.namespace_prefix)
        assert found is not None
        assert found == declared


def test_lookup_namespace_missing_returns_none() -> None:
    """Acceptance #5 — lookup_namespace returns None for an undeclared prefix."""
    assert lookup_namespace("nonexistent.") is None
    assert lookup_namespace("") is None


def test_assert_source_authoritative_declarer_match_ok() -> None:
    """Acceptance #6 — assert_source_authoritative_declarer returns None on match."""
    assert assert_source_authoritative_declarer("anthropic.", NamespaceSourceAxis.AS_SOURCE) is None
    assert assert_source_authoritative_declarer("hitl.", NamespaceSourceAxis.CP_SOURCE) is None
    assert (
        assert_source_authoritative_declarer(
            "provider_discriminator", NamespaceSourceAxis.OD_CANONICAL
        )
        is None
    )


def test_assert_source_authoritative_declarer_mismatch_err() -> None:
    """Acceptance #6 — assert_source_authoritative_declarer raises AuthorityViolation
    when a namespace is claimed by an axis that does not match its declared
    source_axis (Pattern P1 mechanical-alignment discipline)."""
    # anthropic.* is AS_SOURCE — claiming it as CP_SOURCE is a violation.
    with pytest.raises(AuthorityViolation):
        assert_source_authoritative_declarer("anthropic.", NamespaceSourceAxis.CP_SOURCE)
    # hitl.* is CP_SOURCE — claiming it as AS_SOURCE is a violation.
    with pytest.raises(AuthorityViolation):
        assert_source_authoritative_declarer("hitl.", NamespaceSourceAxis.AS_SOURCE)


def test_assert_source_authoritative_declarer_undeclared_err() -> None:
    """Acceptance #6 / #7 — an undeclared namespace has no authoritative declarer;
    asserting any axis for it raises AuthorityViolation."""
    with pytest.raises(AuthorityViolation):
        assert_source_authoritative_declarer("nonexistent.", NamespaceSourceAxis.AS_SOURCE)


def test_single_authoritative_declarer_per_namespace() -> None:
    """Acceptance #7 — each namespace has exactly one authoritative declarer:
    each prefix appears exactly once in the map (no duplicate rows)."""
    prefixes = [row.namespace_prefix for row in NAMESPACE_MAP]
    assert len(prefixes) == len(set(prefixes))


def test_namespace_map_row_serialization_round_trip() -> None:
    """Acceptance #8 — namespace map rows are stable under serialization
    (the map is preserved structurally under the F2-12 carry-forward)."""
    for row in NAMESPACE_MAP:
        assert NamespaceMapRow.model_validate_json(row.model_dump_json()) == row
