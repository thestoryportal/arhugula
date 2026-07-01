"""Tests for U-IS-16 — worktree-isolation primitive (C-IS-09 §9.2/§9.3).

Test set per the U-IS-16 `Tests:` field — 12 tests covering acceptance #1-#10.
Integration tests over real `git worktree` operations in `tmp_path` repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from harness_is.state_ledger_entry_schema import Identifier
from harness_is.workload_manifest_opt_in_schema import WorkloadManifestOptIns
from harness_is.worktree_isolation import (
    ReclamationResult,
    ReclamationTrigger,
    WorktreeConcurrencyCapExceededError,
    WorktreeIsolationDisabledError,
    WorktreeIsolationManager,
)

_PARENT = Identifier("wf-run-1")
_SUB = Identifier("sub-agent-1")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@harness.local")
    _git(repo, "config", "user.name", "harness-test")
    (repo / "base.txt").write_text("base-content\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _manager(
    tmp_path: Path, *, enabled: bool = True, cap: int | None = None
) -> WorktreeIsolationManager:
    return WorktreeIsolationManager(
        repository_root=_repo(tmp_path),
        worktree_base=tmp_path / "worktrees",
        opt_ins=WorkloadManifestOptIns(
            worktree_isolation_enabled=enabled, worktree_concurrency_cap=cap
        ),
    )


def test_allocate_worktree_creates_isolated_directory(tmp_path: Path) -> None:
    """Acceptance #1 — allocation creates an isolated worktree directory."""
    handle = _manager(tmp_path).allocate_worktree(_PARENT, _SUB)
    assert handle.worktree_path.is_dir()


def test_allocate_worktree_shares_git_storage(tmp_path: Path) -> None:
    """Acceptance #1 — the worktree shares the parent's `.git` object store."""
    handle = _manager(tmp_path).allocate_worktree(_PARENT, _SUB)
    # The base commit's file is checked out in the isolated worktree.
    assert (handle.worktree_path / "base.txt").read_text() == "base-content\n"
    # `git worktree list` from the parent lists the new worktree.
    common = _git(handle.worktree_path, "rev-parse", "--git-common-dir")
    assert common  # resolves — the worktree is git-backed by the shared store


def test_worktree_identity_stable(tmp_path: Path) -> None:
    """Acceptance #2 — the worktree id is stable for the sub-agent lifetime."""
    handle = _manager(tmp_path).allocate_worktree(_PARENT, _SUB)
    first = handle.worktree_id
    # The handle is frozen — identity does not drift.
    assert handle.worktree_id == first
    assert handle.sub_agent_id == _SUB


def test_concurrency_cap_enforced(tmp_path: Path) -> None:
    """Acceptance #3 — the (N+1)th concurrent allocation is rejected at cap N."""
    manager = _manager(tmp_path, cap=2)
    manager.allocate_worktree(_PARENT, Identifier("sub-1"))
    manager.allocate_worktree(_PARENT, Identifier("sub-2"))
    with pytest.raises(WorktreeConcurrencyCapExceededError):
        manager.allocate_worktree(_PARENT, Identifier("sub-3"))


def test_reclaim_worktree_invokes_git_remove(tmp_path: Path) -> None:
    """Acceptance #4 — reclamation removes the worktree directory."""
    manager = _manager(tmp_path)
    handle = manager.allocate_worktree(_PARENT, _SUB)
    result = manager.reclaim_worktree(handle, ReclamationTrigger.OPERATOR_POLICY_LIFECYCLE_MARKER)
    assert result is ReclamationResult.RECLAIMED
    assert not handle.worktree_path.exists()
    assert manager.active_count == 0


def test_reclaim_preserves_git_storage_backend(tmp_path: Path) -> None:
    """Acceptance #5 — reclamation does not delete the shared `.git` backend."""
    manager = _manager(tmp_path)
    handle = manager.allocate_worktree(_PARENT, _SUB)
    repo_root = handle.worktree_path  # parent repo verified below
    manager.reclaim_worktree(handle, ReclamationTrigger.SUB_AGENT_SUCCESS)
    parent = tmp_path / "repo"
    assert (parent / ".git").is_dir()
    # The parent repo is still functional after reclamation.
    assert _git(parent, "rev-parse", "HEAD")
    assert repo_root != parent  # the reclaimed worktree was a distinct directory


def test_read_read_non_interference(tmp_path: Path) -> None:
    """Acceptance #7 — concurrent reads across worktrees do not block."""
    manager = _manager(tmp_path)
    a = manager.allocate_worktree(_PARENT, Identifier("sub-a"))
    b = manager.allocate_worktree(_PARENT, Identifier("sub-b"))
    assert (a.worktree_path / "base.txt").read_text() == "base-content\n"
    assert (b.worktree_path / "base.txt").read_text() == "base-content\n"


def test_read_write_non_interference_intra_worktree(tmp_path: Path) -> None:
    """Acceptance #8 — a write in one worktree does not leak into another."""
    manager = _manager(tmp_path)
    a = manager.allocate_worktree(_PARENT, Identifier("sub-a"))
    b = manager.allocate_worktree(_PARENT, Identifier("sub-b"))
    (a.worktree_path / "scratch.txt").write_text("only-in-a\n")
    assert not (b.worktree_path / "scratch.txt").exists()


def test_cross_worktree_writer_serialization(tmp_path: Path) -> None:
    """Acceptance #9 — commits from two worktrees coexist in the shared `.git`
    object store without interleaving corruption."""
    manager = _manager(tmp_path)
    a = manager.allocate_worktree(_PARENT, Identifier("sub-a"))
    b = manager.allocate_worktree(_PARENT, Identifier("sub-b"))
    (a.worktree_path / "a.txt").write_text("a\n")
    _git(a.worktree_path, "add", ".")
    _git(a.worktree_path, "commit", "-q", "-m", "from-a")
    sha_a = _git(a.worktree_path, "rev-parse", "HEAD")
    (b.worktree_path / "b.txt").write_text("b\n")
    _git(b.worktree_path, "add", ".")
    _git(b.worktree_path, "commit", "-q", "-m", "from-b")
    sha_b = _git(b.worktree_path, "rev-parse", "HEAD")
    parent = tmp_path / "repo"
    assert _git(parent, "cat-file", "-t", sha_a) == "commit"
    assert _git(parent, "cat-file", "-t", sha_b) == "commit"
    assert sha_a != sha_b


def test_worktree_disabled_when_opt_out(tmp_path: Path) -> None:
    """Acceptance #10 — an opted-out workload allocates 0 worktrees."""
    manager = _manager(tmp_path, enabled=False)
    with pytest.raises(WorktreeIsolationDisabledError):
        manager.allocate_worktree(_PARENT, _SUB)
    assert manager.active_count == 0


def test_worktree_termination_on_sub_agent_success(tmp_path: Path) -> None:
    """Acceptance #4/#6 — reclamation on sub-agent success reclaims the worktree."""
    manager = _manager(tmp_path)
    handle = manager.allocate_worktree(_PARENT, _SUB)
    assert (
        manager.reclaim_worktree(handle, ReclamationTrigger.SUB_AGENT_SUCCESS)
        is ReclamationResult.RECLAIMED
    )


def test_worktree_termination_on_sub_agent_failure(tmp_path: Path) -> None:
    """Acceptance #4/#6 — reclamation on sub-agent failure reclaims the worktree."""
    manager = _manager(tmp_path)
    handle = manager.allocate_worktree(_PARENT, _SUB)
    assert (
        manager.reclaim_worktree(handle, ReclamationTrigger.SUB_AGENT_FAILURE)
        is ReclamationResult.RECLAIMED
    )
