"""Tests for U-CP-29 — brief-authoring inheritance table (C-CP-13 §13.3).

Acceptance-criterion coverage:
  #1 4 entries per §13.3 verbatim -> test_brief_authoring_inheritance_cardinality_four,
                                     test_inheritance_rule_per_workload_class
  #2 reducible_to_haiku == False  -> test_reducible_to_haiku_false_invariant
  #4 no independent override      -> test_no_independent_override

  #3 (`resolve_brief_authoring_model_binding` delegator) struck — cross-axis
     ModelBinding seam, routed to 7c
     (`.harness/class_1_tension_u_cp_29_cross_axis_modelbinding_seam.md`).
"""

from __future__ import annotations

from harness_core import WorkloadClass
from harness_cp.brief_authoring_inheritance import (
    BRIEF_AUTHORING_INHERITANCE,
    BriefAuthoringInheritance,
    InheritanceRule,
    inheritance_for,
)


def test_brief_authoring_inheritance_cardinality_four() -> None:
    """#1 — exactly four entries, one per WorkloadClass."""
    assert len(BRIEF_AUTHORING_INHERITANCE) == 4
    assert {e.workload_class for e in BRIEF_AUTHORING_INHERITANCE} == set(WorkloadClass)


def test_inheritance_rule_per_workload_class() -> None:
    """#1 — per-workload inheritance rule matches the §13.3 table verbatim."""
    assert (
        inheritance_for(WorkloadClass.SOFTWARE_ENGINEERING).inheritance_rule
        is InheritanceRule.INHERIT_LEAD_BINDING
    )
    assert (
        inheritance_for(WorkloadClass.CONTENT_CREATION).inheritance_rule
        is InheritanceRule.INHERIT_LEAD_BINDING
    )
    assert (
        inheritance_for(WorkloadClass.PIPELINE_AUTOMATION).inheritance_rule
        is InheritanceRule.INHERIT_PER_STAGE_LEAD_BINDING
    )
    assert (
        inheritance_for(WorkloadClass.RESEARCH).inheritance_rule
        is InheritanceRule.INHERIT_LEAD_BINDING
    )


def test_reducible_to_haiku_false_invariant() -> None:
    """#2 — reducible_to_haiku is False for all rows (ADR-D3 v1.2 §1.4)."""
    for e in BRIEF_AUTHORING_INHERITANCE:
        assert e.reducible_to_haiku is False


def test_no_independent_override() -> None:
    """#4 — the record carries no independent-override field (§13.3 closing)."""
    fields = set(BriefAuthoringInheritance.model_fields)
    assert fields == {"workload_class", "inheritance_rule", "reducible_to_haiku", "narrative"}
    # No "override" / "configurable" field — binding is not independently configurable.
    assert not any("override" in f or "configurable" in f for f in fields)
