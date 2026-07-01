"""Tests for U-IS-13 — workload manifest opt-in schema (C-IS-08 / C-IS-09).

Test set per the U-IS-13 `Tests:` field — covers acceptance #1-#6.
"""

from __future__ import annotations

import pytest
from harness_is.workload_manifest_opt_in_schema import (
    CheckpointCadence,
    WorkloadManifestOptIns,
)
from pydantic import ValidationError

_SPEC_CADENCES = {
    "per_step",
    "per_tool_call",
    "per_significant_change",
    "per_explicit_marker",
}


def test_workload_manifest_default_values() -> None:
    """Acceptance #1/#2 — 4 fields; both opt-ins default false."""
    opt_ins = WorkloadManifestOptIns()
    assert set(WorkloadManifestOptIns.model_fields) == {
        "shadow_git_enabled",
        "shadow_git_cadence",
        "worktree_isolation_enabled",
        "worktree_concurrency_cap",
    }
    assert opt_ins.shadow_git_enabled is False
    assert opt_ins.worktree_isolation_enabled is False


def test_checkpoint_cadence_enum_completeness() -> None:
    """Acceptance #4 — CheckpointCadence has exactly 4 values matching §8.2."""
    assert len(CheckpointCadence) == 4
    assert {c.value for c in CheckpointCadence} == _SPEC_CADENCES


def test_shadow_git_enabled_requires_cadence() -> None:
    """Acceptance #5 — shadow_git_enabled == true requires a cadence."""
    with pytest.raises(ValidationError):
        WorkloadManifestOptIns(shadow_git_enabled=True)
    # With a cadence supplied, it validates.
    ok = WorkloadManifestOptIns(
        shadow_git_enabled=True, shadow_git_cadence=CheckpointCadence.PER_STEP
    )
    assert ok.shadow_git_cadence is CheckpointCadence.PER_STEP


def test_worktree_concurrency_cap_optional() -> None:
    """Acceptance #3 — worktree_concurrency_cap is optional; absent = unbounded."""
    assert WorkloadManifestOptIns().worktree_concurrency_cap is None
    capped = WorkloadManifestOptIns(worktree_isolation_enabled=True, worktree_concurrency_cap=4)
    assert capped.worktree_concurrency_cap == 4


def test_independent_opt_in_combinations() -> None:
    """Acceptance #2 — the two opt-ins are independent: any combination is valid
    (shadow-Git off / worktree on, etc.)."""
    worktree_only = WorkloadManifestOptIns(worktree_isolation_enabled=True)
    assert worktree_only.worktree_isolation_enabled is True
    assert worktree_only.shadow_git_enabled is False

    both = WorkloadManifestOptIns(
        shadow_git_enabled=True,
        shadow_git_cadence=CheckpointCadence.PER_EXPLICIT_MARKER,
        worktree_isolation_enabled=True,
    )
    assert both.shadow_git_enabled is True
    assert both.worktree_isolation_enabled is True
