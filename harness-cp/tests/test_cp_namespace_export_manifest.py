"""Tests for U-CP-54 — CP-axis namespace export manifest (C-CP-24 §24.1).

Acceptance-criterion coverage:
  #1 manifest cardinality 11     -> test_cp_export_manifest_cardinality_eleven
  #2 §24.1.A 6 entries           -> test_section_24_1_a_six_entries
  #3 §24.1.B 4 entries           -> test_section_24_1_b_four_entries
  #4 §24.1.C 1 entry             -> test_section_24_1_c_one_entry
  #5 per-namespace attr count    -> test_per_namespace_attribute_count_match_spec
  #6 total 63 attributes         -> test_total_attribute_count_sixty_three
  #8 harness.breaker posture     -> test_harness_breaker_substrate_anchored_outside_cp
"""

from __future__ import annotations

from harness_cp.cp_namespace_export_manifest import (
    CP_EXPORTED_ATTRIBUTE_COUNT,
    CP_NAMESPACE_EXPORT_MANIFEST,
    IngestionTarget,
    SourceAuthorityPosture,
)


def _by_name(name: str):
    return next(e for e in CP_NAMESPACE_EXPORT_MANIFEST if e.namespace_name == name)


def test_cp_export_manifest_cardinality_eleven() -> None:
    """#1 — CP_NAMESPACE_EXPORT_MANIFEST declares exactly 11 entries."""
    assert len(CP_NAMESPACE_EXPORT_MANIFEST) == 11


def test_section_24_1_a_six_entries() -> None:
    """#2 — §24.1.A specialization-layer namespaces: 6 entries → D6 §1.2."""
    a = [
        e
        for e in CP_NAMESPACE_EXPORT_MANIFEST
        if e.ingestion_target is IngestionTarget.OD_PLAN_SESSION_4_D6_SECTION_1_2
    ]
    assert len(a) == 6
    assert {e.namespace_name for e in a} == {
        "engine.*",
        "topology.*",
        "subagent.*",
        "hitl.*",
        "audit.*",
        "validator.fail.*",
    }


def test_section_24_1_b_four_entries() -> None:
    """#3 — §24.1.B F3-lifecycle-event namespaces: 4 entries → D6 §1.4."""
    b = [
        e
        for e in CP_NAMESPACE_EXPORT_MANIFEST
        if e.ingestion_target is IngestionTarget.OD_PLAN_SESSION_4_D6_SECTION_1_4
    ]
    assert len(b) == 4
    assert {e.namespace_name for e in b} == {
        "fallback.*",
        "retry.*",
        "lease.*",
        "harness.breaker.*",
    }


def test_section_24_1_c_one_entry() -> None:
    """#4 — §24.1.C inheritance-composition: 1 entry (routing.*) → D6 §1.5."""
    c = [
        e
        for e in CP_NAMESPACE_EXPORT_MANIFEST
        if e.ingestion_target is IngestionTarget.OD_PLAN_SESSION_4_D6_SECTION_1_5
    ]
    assert len(c) == 1
    assert c[0].namespace_name == "routing.*"


def test_per_namespace_attribute_count_match_spec() -> None:
    """#5 — per-namespace attribute counts match C-CP-24 §24.1 verbatim."""
    expected = {
        "engine.*": 3,
        "topology.*": 10,
        "subagent.*": 7,
        "hitl.*": 4,
        "audit.*": 7,
        "validator.fail.*": 3,
        "fallback.*": 9,
        "retry.*": 4,
        "lease.*": 5,
        "harness.breaker.*": 7,
        "routing.*": 4,
    }
    for name, count in expected.items():
        assert _by_name(name).attribute_count == count


def test_total_attribute_count_sixty_three() -> None:
    """#6 — total exported attribute count is 63 (34 + 25 + 4)."""
    assert CP_EXPORTED_ATTRIBUTE_COUNT == 63


def test_harness_breaker_substrate_anchored_outside_cp() -> None:
    """#8 — harness.breaker.* posture is SUBSTRATE_ANCHORED_OUTSIDE_CP per F2-16."""
    breaker = _by_name("harness.breaker.*")
    assert breaker.source_authority_posture is SourceAuthorityPosture.SUBSTRATE_ANCHORED_OUTSIDE_CP
    # Every other namespace is CP-owned.
    others = [e for e in CP_NAMESPACE_EXPORT_MANIFEST if e.namespace_name != "harness.breaker.*"]
    assert all(e.source_authority_posture is SourceAuthorityPosture.OWNED_BY_CP for e in others)
