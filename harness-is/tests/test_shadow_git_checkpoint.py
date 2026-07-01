"""Tests for U-IS-14 — shadow-Git checkpoint primitive (C-IS-08 §8.2/§8.4).

Test set per the U-IS-14 `Tests:` field — 9 tests covering acceptance #1-#8.
The deferred `on_workflow_event` hook is not exercised (see module docstring).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness_is.shadow_git_checkpoint import (
    CheckpointTriggerContext,
    create_shadow_git_checkpoint,
    should_checkpoint,
)
from harness_is.state_ledger_entry_schema import Identifier
from harness_is.workload_manifest_opt_in_schema import (
    CheckpointCadence,
    WorkloadManifestOptIns,
)

_RUN = Identifier("wf-run-1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path, *, dirty: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@harness.local")
    _git(repo, "config", "user.name", "harness-test")
    (repo / "code.py").write_text("v1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    if dirty:
        (repo / "code.py").write_text("v2-uncommitted\n")
    return repo


def _ctx(cadence: CheckpointCadence) -> CheckpointTriggerContext:
    return CheckpointTriggerContext(cadence=cadence, explicit_marker="m")


def test_checkpoint_creates_shadow_ref(tmp_path: Path) -> None:
    """Acceptance #1 — a shadow ref is created in the same repository."""
    repo = _repo(tmp_path)
    result = create_shadow_git_checkpoint(repo, _RUN, _ctx(CheckpointCadence.PER_STEP))
    assert result.shadow_ref.startswith("refs/shadow/")
    # The ref exists and resolves.
    assert _git(repo, "rev-parse", "--verify", result.shadow_ref)


def test_checkpoint_not_in_main_branch_history(tmp_path: Path) -> None:
    """Acceptance #2 — the shadow ref does not appear in the branch history."""
    repo = _repo(tmp_path, dirty=True)
    log_before = _git(repo, "log", "--oneline")
    result = create_shadow_git_checkpoint(repo, _RUN, _ctx(CheckpointCadence.PER_STEP))
    assert _git(repo, "log", "--oneline") == log_before
    assert "shadow" not in _git(repo, "branch")
    # The dirty-tree snapshot commit is not an ancestor of the branch.
    snapshot = _git(repo, "rev-parse", result.shadow_ref)
    head = _git(repo, "rev-parse", "HEAD")
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", snapshot, head]
        ).returncode
        != 0
    )


def test_checkpoint_per_step_cadence(tmp_path: Path) -> None:
    """Acceptance #3 — PER_STEP cadence fires and is recorded."""
    opt_ins = WorkloadManifestOptIns(
        shadow_git_enabled=True, shadow_git_cadence=CheckpointCadence.PER_STEP
    )
    ctx = _ctx(CheckpointCadence.PER_STEP)
    assert should_checkpoint(opt_ins, ctx) is True
    result = create_shadow_git_checkpoint(_repo(tmp_path), _RUN, ctx)
    assert result.triggered_by is CheckpointCadence.PER_STEP


def test_checkpoint_per_tool_call_cadence(tmp_path: Path) -> None:
    """Acceptance #4 — PER_TOOL_CALL cadence fires and is recorded."""
    opt_ins = WorkloadManifestOptIns(
        shadow_git_enabled=True, shadow_git_cadence=CheckpointCadence.PER_TOOL_CALL
    )
    ctx = _ctx(CheckpointCadence.PER_TOOL_CALL)
    assert should_checkpoint(opt_ins, ctx) is True
    result = create_shadow_git_checkpoint(_repo(tmp_path), _RUN, ctx)
    assert result.triggered_by is CheckpointCadence.PER_TOOL_CALL


def test_checkpoint_per_significant_change_cadence(tmp_path: Path) -> None:
    """Acceptance #5 — PER_SIGNIFICANT_CHANGE cadence fires and is recorded."""
    opt_ins = WorkloadManifestOptIns(
        shadow_git_enabled=True,
        shadow_git_cadence=CheckpointCadence.PER_SIGNIFICANT_CHANGE,
    )
    ctx = _ctx(CheckpointCadence.PER_SIGNIFICANT_CHANGE)
    assert should_checkpoint(opt_ins, ctx) is True
    result = create_shadow_git_checkpoint(_repo(tmp_path), _RUN, ctx)
    assert result.triggered_by is CheckpointCadence.PER_SIGNIFICANT_CHANGE


def test_checkpoint_per_explicit_marker_cadence(tmp_path: Path) -> None:
    """Acceptance #6 — PER_EXPLICIT_MARKER cadence fires and is recorded."""
    opt_ins = WorkloadManifestOptIns(
        shadow_git_enabled=True,
        shadow_git_cadence=CheckpointCadence.PER_EXPLICIT_MARKER,
    )
    ctx = _ctx(CheckpointCadence.PER_EXPLICIT_MARKER)
    assert should_checkpoint(opt_ins, ctx) is True
    result = create_shadow_git_checkpoint(_repo(tmp_path), _RUN, ctx)
    assert result.triggered_by is CheckpointCadence.PER_EXPLICIT_MARKER


def test_checkpoint_disabled_when_opt_out() -> None:
    """Acceptance #7 — `shadow_git_enabled = false` ⇒ no checkpoint fires."""
    opt_out = WorkloadManifestOptIns(shadow_git_enabled=False)
    assert should_checkpoint(opt_out, _ctx(CheckpointCadence.PER_STEP)) is False
    # A cadence mismatch under an enabled opt-in also does not fire.
    mismatched = WorkloadManifestOptIns(
        shadow_git_enabled=True, shadow_git_cadence=CheckpointCadence.PER_STEP
    )
    assert should_checkpoint(mismatched, _ctx(CheckpointCadence.PER_TOOL_CALL)) is False


def test_checkpoint_atomic(tmp_path: Path) -> None:
    """Acceptance #8 — the shadow ref is created atomically: post-create it
    resolves to a single valid commit object."""
    repo = _repo(tmp_path)
    result = create_shadow_git_checkpoint(repo, _RUN, _ctx(CheckpointCadence.PER_STEP))
    obj_type = _git(repo, "cat-file", "-t", _git(repo, "rev-parse", result.shadow_ref))
    assert obj_type == "commit"


def test_checkpoint_orthogonal_to_deploys(tmp_path: Path) -> None:
    """Acceptance — checkpoint creation is orthogonal to deploys: it does not
    move HEAD or alter the branch."""
    repo = _repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    create_shadow_git_checkpoint(repo, _RUN, _ctx(CheckpointCadence.PER_EXPLICIT_MARKER))
    assert _git(repo, "rev-parse", "HEAD") == head_before
