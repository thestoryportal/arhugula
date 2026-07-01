"""U-RT-11 — `materialize_isolation_stage` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L2:
- Manager initialized.
- Isolation invariants asserted at boot (opt-ins default off → allocation
  rejected with typed error).
- Round-trip checkpoint → rollback against tmp `.harness/` returns to
  byte-identical pre-checkpoint state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.shadow_git_checkpoint import CheckpointTriggerContext
from harness_is.shadow_git_rollback import RollbackStatus
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.workload_manifest_opt_in_schema import (
    CheckpointCadence,
    WorkloadManifestOptIns,
)
from harness_is.worktree_isolation import (
    WorktreeIsolationDisabledError,
    WorktreeIsolationManager,
)
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.shadow_git import (
    IsolationStage,
    ShadowGitSupervisor,
    materialize_isolation_stage,
)
from harness_runtime.lifecycle.state_ledger import materialize_state_ledger
from harness_runtime.types import PathBindingConfig


def _git(repo: Path, *args: str) -> str:
    """Helper — run git in the repo, return stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    """Initialize a git repo with one initial commit."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("original\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "initial")


def _resolver_for(tmp_path: Path) -> PathResolver:
    config = PathBindingConfig(
        raw_entries=(
            {
                "path_class": PathClass.STATE_LEDGER,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(tmp_path / "state.jsonl"),
            },
        ),
    )
    return PathResolver(build_path_binding(config))


def _actor() -> Actor:
    return Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime")


def _opt_ins_full() -> WorkloadManifestOptIns:
    return WorkloadManifestOptIns(
        shadow_git_enabled=True,
        shadow_git_cadence=CheckpointCadence.PER_STEP,
        worktree_isolation_enabled=True,
    )


# ---------------------------------------------------------------------------
# Manager initialized (plan AC).
# ---------------------------------------------------------------------------


def test_manager_initialized(tmp_path: Path) -> None:
    """`materialize_isolation_stage` produces a `WorktreeIsolationManager`."""
    _init_repo(tmp_path / "repo")
    ledger_writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    stage = materialize_isolation_stage(
        repository_root=tmp_path / "repo",
        worktree_base=tmp_path / "worktrees",
        opt_ins=_opt_ins_full(),
        ledger_writer=ledger_writer,
    )
    assert isinstance(stage, IsolationStage)
    assert isinstance(stage.worktree_manager, WorktreeIsolationManager)
    assert isinstance(stage.shadow_git, ShadowGitSupervisor)


# ---------------------------------------------------------------------------
# Isolation invariants asserted at boot (plan AC).
# ---------------------------------------------------------------------------


def test_worktree_allocation_rejected_when_opt_out(tmp_path: Path) -> None:
    """Default (off) opt-ins → `allocate_worktree` raises `WorktreeIsolationDisabledError`."""
    _init_repo(tmp_path / "repo")
    ledger_writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    stage = materialize_isolation_stage(
        repository_root=tmp_path / "repo",
        worktree_base=tmp_path / "worktrees",
        opt_ins=WorkloadManifestOptIns(),  # default — all off.
        ledger_writer=ledger_writer,
    )
    with pytest.raises(WorktreeIsolationDisabledError):
        stage.worktree_manager.allocate_worktree(
            parent_workflow_run_id=Identifier("run-1"),
            sub_agent_id=Identifier("sub-1"),
        )


# ---------------------------------------------------------------------------
# Round-trip checkpoint → rollback (plan AC verbatim).
# ---------------------------------------------------------------------------


def test_checkpoint_rollback_round_trip_byte_identical(tmp_path: Path) -> None:
    """Modify → checkpoint → modify-again → rollback → file matches checkpoint state.

    Plan §2 L2 AC: 'round-trip checkpoint → rollback against tmp `.harness/`
    returns to byte-identical pre-checkpoint state.'
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    ledger_writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    stage = materialize_isolation_stage(
        repository_root=repo,
        worktree_base=tmp_path / "worktrees",
        opt_ins=_opt_ins_full(),
        ledger_writer=ledger_writer,
    )

    # 1. Modify the file (working-tree dirty).
    target = repo / "file.txt"
    target.write_text("at-checkpoint\n")
    pre_checkpoint_bytes = target.read_bytes()

    # 2. Create checkpoint capturing this state.
    workflow_run_id = Identifier("run-1")
    result = stage.shadow_git.create_checkpoint(
        workflow_run_id=workflow_run_id,
        trigger_context=CheckpointTriggerContext(cadence=CheckpointCadence.PER_STEP),
    )
    checkpoint_id = result.checkpoint_id

    # 3. Modify again (post-checkpoint state).
    target.write_text("post-checkpoint-MUTATED\n")
    assert target.read_bytes() != pre_checkpoint_bytes

    # 4. Rollback.
    rollback_result = stage.shadow_git.rollback(
        checkpoint_id=checkpoint_id,
        workflow_run_id=workflow_run_id,
    )
    assert rollback_result.status is RollbackStatus.RESTORED

    # 5. File is byte-identical to pre-checkpoint state.
    assert target.read_bytes() == pre_checkpoint_bytes


def test_rollback_nonexistent_checkpoint_returns_not_found(tmp_path: Path) -> None:
    """A bogus checkpoint_id yields `CHECKPOINT_NOT_FOUND` with no side effects."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    ledger_writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    stage = materialize_isolation_stage(
        repository_root=repo,
        worktree_base=tmp_path / "worktrees",
        opt_ins=_opt_ins_full(),
        ledger_writer=ledger_writer,
    )
    result = stage.shadow_git.rollback(
        checkpoint_id=Identifier("bogus-checkpoint-id"),
        workflow_run_id=Identifier("run-1"),
    )
    assert result.status is RollbackStatus.CHECKPOINT_NOT_FOUND


# ---------------------------------------------------------------------------
# Supervisor + stage handle surface.
# ---------------------------------------------------------------------------


def test_isolation_stage_is_frozen(tmp_path: Path) -> None:
    """`IsolationStage` is a frozen dataclass."""
    _init_repo(tmp_path / "repo")
    ledger_writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    stage = materialize_isolation_stage(
        repository_root=tmp_path / "repo",
        worktree_base=tmp_path / "worktrees",
        opt_ins=_opt_ins_full(),
        ledger_writer=ledger_writer,
    )
    with pytest.raises((AttributeError, Exception)):
        stage.shadow_git = None  # type: ignore[misc,assignment]


def test_shadow_git_supervisor_is_frozen(tmp_path: Path) -> None:
    """`ShadowGitSupervisor` is a frozen dataclass."""
    _init_repo(tmp_path / "repo")
    ledger_writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    stage = materialize_isolation_stage(
        repository_root=tmp_path / "repo",
        worktree_base=tmp_path / "worktrees",
        opt_ins=_opt_ins_full(),
        ledger_writer=ledger_writer,
    )
    with pytest.raises((AttributeError, Exception)):
        stage.shadow_git.repository_root = Path("/")  # type: ignore[misc]
