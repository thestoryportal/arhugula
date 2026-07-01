"""Tests for U-CP-22 — 6-pattern TopologyPattern + admissibility (C-CP-10).

Test set per the U-CP-22 v2.5 `Tests:` field — covers acceptance #1-#4 and the
v2.5 `Depends on: [U-CP-00]` amendment.
"""

from __future__ import annotations

from harness_core.workload_class import WorkloadClass
from harness_cp.topology_pattern import (
    CascadePolicy,
    TopologyPattern,
    is_admissible,
)

# Verbatim from C-CP-10 §10.1 six-pattern taxonomy "Pattern" column.
_SPEC_TOPOLOGY_PATTERNS = {
    "single-threaded-linear",
    "orchestrator-workers",
    "decentralized-handoff",
    "hierarchical-delegation",
    "evaluator-optimizer",
    "parallelization",
}

# Verbatim from C-CP-10 §10.2 `cascade_policy` field domain.
_SPEC_CASCADE_POLICY = {"pause", "proceed", "cascade-cancel"}


def test_topology_pattern_cardinality_six() -> None:
    """§10.1 — exactly 6 topology patterns."""
    assert len(TopologyPattern) == 6


def test_topology_pattern_values_match_spec_10_1_verbatim() -> None:
    """§10.1 — member string values are the taxonomy patterns, verbatim."""
    assert {p.value for p in TopologyPattern} == _SPEC_TOPOLOGY_PATTERNS


def test_cascade_policy_cardinality_three() -> None:
    """§10.2 — the `cascade_policy` field domain has exactly 3 literals."""
    assert len(CascadePolicy) == 3


def test_cascade_policy_values_match_spec_10_2_verbatim() -> None:
    """§10.2 — member string values are the domain literals, verbatim."""
    assert {c.value for c in CascadePolicy} == _SPEC_CASCADE_POLICY


def test_admissibility_per_workload_class_match_spec_10_3() -> None:
    """§10.3 — `is_admissible` is `True` for exactly the 5 cross-pattern
    admissible cells the §10.3 annotation block declares."""
    admissible = {
        (TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.SOFTWARE_ENGINEERING),
        (TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.RESEARCH),
        (TopologyPattern.DECENTRALIZED_HANDOFF, WorkloadClass.PIPELINE_AUTOMATION),
        (TopologyPattern.PARALLELIZATION, WorkloadClass.RESEARCH),
        (TopologyPattern.PARALLELIZATION, WorkloadClass.CONTENT_CREATION),
    }
    for pattern in TopologyPattern:
        for workload in WorkloadClass:
            expected = (pattern, workload) in admissible
            assert is_admissible(pattern, workload) is expected


def test_taxonomy_closed() -> None:
    """§10.1 / acceptance #4 — the taxonomy is closed: its member set is
    exactly the 6 §10.1 patterns, no more."""
    assert {p.name for p in TopologyPattern} == {
        "SINGLE_THREADED_LINEAR",
        "ORCHESTRATOR_WORKERS",
        "DECENTRALIZED_HANDOFF",
        "HIERARCHICAL_DELEGATION",
        "EVALUATOR_OPTIMIZER",
        "PARALLELIZATION",
    }


def test_is_admissible_accepts_workload_class_from_u_cp_00() -> None:
    """v2.5 — the `workload` parameter binds the U-CP-00 `WorkloadClass` enum
    (the Tension 003 `Depends on: [U-CP-00]` edge)."""
    assert WorkloadClass.__module__ == "harness_core.workload_class"
    # A WorkloadClass value flows through is_admissible without error.
    result = is_admissible(TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.RESEARCH)
    assert result is True
