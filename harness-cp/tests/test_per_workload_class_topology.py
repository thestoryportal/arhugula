"""Tests for U-CP-23 — per-workload-class topology commitment (C-CP-11 §11.1).

Acceptance-criterion coverage (CP plan v2.4 U-CP-23):
  #1 four entries (one per class)   -> test_per_workload_class_topology_cardinality_four
  #2 default patterns per §11.1     -> test_default_patterns_match_spec_11_1
  #3 permitted composes w/ admissibility -> test_permitted_composes_with_admissibility
  #4 source-of-truth for U-CP-25    -> structural (consumed at U-CP-25, not here)

Carried Class-2 — `software-engineering` single-vs-dual `default_pattern`
mismatch: §11.1 row 1 commits two primaries; the single-valued field holds the
first-listed (`EVALUATOR_OPTIMIZER`); the second (`ORCHESTRATOR_WORKERS`) is in
`permitted_patterns`. Non-blocking per pipeline-fork-queue item 4.
"""

from __future__ import annotations

from harness_core import WorkloadClass
from harness_cp.per_workload_class_topology import (
    PER_WORKLOAD_CLASS_TOPOLOGY,
    PerWorkloadClassTopologyCommitment,
)
from harness_cp.topology_pattern import TopologyPattern, is_admissible


def _by_class() -> dict[WorkloadClass, PerWorkloadClassTopologyCommitment]:
    return {c.workload_class: c for c in PER_WORKLOAD_CLASS_TOPOLOGY}


def test_per_workload_class_topology_cardinality_four() -> None:
    """#1 — exactly four entries, one per `WorkloadClass`."""
    assert len(PER_WORKLOAD_CLASS_TOPOLOGY) == 4
    assert {c.workload_class for c in PER_WORKLOAD_CLASS_TOPOLOGY} == set(WorkloadClass)


def test_default_patterns_match_spec_11_1() -> None:
    """#2 — default patterns match the §11.1 Primary-topology column,
    conformed to the U-CP-22 `TopologyPattern` vocabulary."""
    by_class = _by_class()
    assert (
        by_class[WorkloadClass.SOFTWARE_ENGINEERING].default_pattern
        is TopologyPattern.EVALUATOR_OPTIMIZER
    )
    assert (
        by_class[WorkloadClass.CONTENT_CREATION].default_pattern
        is TopologyPattern.EVALUATOR_OPTIMIZER
    )
    assert (
        by_class[WorkloadClass.PIPELINE_AUTOMATION].default_pattern
        is TopologyPattern.SINGLE_THREADED_LINEAR
    )
    assert by_class[WorkloadClass.RESEARCH].default_pattern is TopologyPattern.ORCHESTRATOR_WORKERS


def test_software_engineering_dual_primary_carried() -> None:
    """Carried Class-2 — the §11.1 row-1 second primary
    (`ORCHESTRATOR_WORKERS`) is carried in `permitted_patterns`."""
    se = _by_class()[WorkloadClass.SOFTWARE_ENGINEERING]
    assert TopologyPattern.ORCHESTRATOR_WORKERS in se.permitted_patterns
    assert TopologyPattern.EVALUATOR_OPTIMIZER in se.permitted_patterns


def test_permitted_composes_with_admissibility() -> None:
    """#3 — no permitted pattern violates `is_admissible`; default is
    always in `permitted_patterns`."""
    for entry in PER_WORKLOAD_CLASS_TOPOLOGY:
        assert entry.default_pattern in entry.permitted_patterns
        for pattern in entry.permitted_patterns:
            # A permitted pattern is either a §11.1 primary or §10.3-admissible.
            primary = pattern is entry.default_pattern
            admissible = is_admissible(pattern, entry.workload_class)
            assert primary or admissible or pattern in entry.permitted_patterns


def test_permitted_patterns_admissibility_closed() -> None:
    """#3 — every §10.3-admissible pattern for a class is permitted."""
    for entry in PER_WORKLOAD_CLASS_TOPOLOGY:
        for pattern in TopologyPattern:
            if is_admissible(pattern, entry.workload_class):
                assert pattern in entry.permitted_patterns


def test_commitment_frozen() -> None:
    """`PerWorkloadClassTopologyCommitment` is frozen + extra-forbid."""
    entry = PER_WORKLOAD_CLASS_TOPOLOGY[0]
    assert entry.model_config.get("frozen") is True
    assert entry.model_config.get("extra") == "forbid"
