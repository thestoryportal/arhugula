"""Tests for U-CP-00 — `WorkloadClass` closed 4-value enum (C-CP-07 §7.3).

Test set per the U-CP-00 `Tests:` field — covers acceptance #1-#4.
"""

from __future__ import annotations

from harness_core.workload_class import WorkloadClass

# Verbatim from C-CP-07 §7.3 workload-class taxonomy.
_SPEC_WORKLOAD_CLASSES = {
    "software-engineering",
    "content-creation",
    "pipeline-automation",
    "research",
}


def test_workload_class_cardinality_four() -> None:
    """§7.3 — exactly 4 workload classes."""
    assert len(WorkloadClass) == 4


def test_workload_class_values_match_spec_7_3_verbatim() -> None:
    """§7.3 — member string values are the taxonomy identifiers, verbatim."""
    assert {wc.value for wc in WorkloadClass} == _SPEC_WORKLOAD_CLASSES


def test_workload_class_resides_in_harness_core() -> None:
    """U-CP-00 residence — `WorkloadClass` is a `harness-core` shared type."""
    assert WorkloadClass.__module__ == "harness_core.workload_class"


def test_workload_class_closed_no_extension_class_member() -> None:
    """§7.3 `extension-class` is the open-extension option, NOT a member of the
    closed 4-value enum (acceptance #2)."""
    member_values = {wc.value for wc in WorkloadClass}
    assert "extension-class" not in member_values
    assert len(member_values) == 4
