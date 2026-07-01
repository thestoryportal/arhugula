"""U-RT-11 — worktree isolation manager + shadow-Git supervisor binding.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 (C-RT-02 stage 1 IS post-conditions:
`ctx.worktree_manager`, `ctx.shadow_git` non-None) and Phase 2 Session 3 plan
v2.1 §2 L2, this module:

- Constructs `harness_is.WorktreeIsolationManager(repository_root,
  worktree_base, opt_ins)` from the `PathBindingConfig` opt-ins.
- Binds a `ShadowGitSupervisor` runtime wrapper around
  `harness_is.create_shadow_git_checkpoint` and
  `harness_is.rollback_to_checkpoint`, carrying the ledger handle the
  rollback flow writes to per C-IS-08 §8.3 acceptance #4.

Both objects flow into the post-bootstrap `HarnessContext`:
- `HarnessContext.worktree_manager` ← `WorktreeIsolationManager`
- `HarnessContext.shadow_git` ← `ShadowGitSupervisor` (satisfies the
  Protocol stub declared at L0 per Class 2 Tension 2026-05-19)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.shadow_git_checkpoint import (
    CheckpointResult,
    CheckpointTriggerContext,
    create_shadow_git_checkpoint,
)
from harness_is.shadow_git_rollback import RollbackResult, rollback_to_checkpoint
from harness_is.state_ledger_entry_schema import Identifier
from harness_is.workload_manifest_opt_in_schema import WorkloadManifestOptIns
from harness_is.worktree_isolation import WorktreeIsolationManager

from harness_runtime.lifecycle.state_ledger import LedgerWriter

__all__ = [
    "IsolationStage",
    "ShadowGitSupervisor",
    "materialize_isolation_stage",
]


@dataclass(frozen=True)
class ShadowGitSupervisor:
    """Runtime wrapper around the IS shadow-Git checkpoint/rollback surface.

    Bound to a fixed repository root + ledger handle at construction time;
    the rollback flow needs both per C-IS-08 §8.3 (the ledger is preserved
    across rollback and a rollback event is appended on success).
    """

    repository_root: Path
    ledger_handle: JsonlLedgerHandle

    def create_checkpoint(
        self,
        workflow_run_id: Identifier,
        trigger_context: CheckpointTriggerContext,
    ) -> CheckpointResult:
        """Snapshot working state into a shadow-Git ref (C-IS-08 §8.4 pass-through)."""
        return create_shadow_git_checkpoint(
            self.repository_root,
            workflow_run_id,
            trigger_context,
        )

    def rollback(
        self,
        checkpoint_id: Identifier,
        workflow_run_id: Identifier,
    ) -> RollbackResult:
        """Restore working tree to a shadow-Git checkpoint (C-IS-08 §8.3 pass-through)."""
        return rollback_to_checkpoint(
            self.repository_root,
            self.ledger_handle,
            checkpoint_id,
            workflow_run_id,
        )


@dataclass(frozen=True)
class IsolationStage:
    """Result of `materialize_isolation_stage`: manager + supervisor pair."""

    worktree_manager: WorktreeIsolationManager
    shadow_git: ShadowGitSupervisor


def materialize_isolation_stage(
    *,
    repository_root: Path,
    worktree_base: Path,
    opt_ins: WorkloadManifestOptIns,
    ledger_writer: LedgerWriter,
) -> IsolationStage:
    """Build the worktree manager + shadow-Git supervisor for stage 1 IS.

    Parameters
    ----------
    repository_root :
        Absolute path to the harness's host git repository.
    worktree_base :
        Filesystem base under which `WorktreeIsolationManager` allocates new
        worktrees (typically `.harness/worktrees/`).
    opt_ins :
        Workload-manifest opt-ins declaration (gates worktree allocation +
        shadow-Git checkpoint cadence).
    ledger_writer :
        The runtime's `LedgerWriter` (U-RT-12); the supervisor needs its
        ledger handle so `rollback_to_checkpoint` can append the rollback
        event per C-IS-08 §8.3 acceptance #4.
    """
    manager = WorktreeIsolationManager(repository_root, worktree_base, opt_ins)
    supervisor = ShadowGitSupervisor(
        repository_root=repository_root,
        ledger_handle=ledger_writer.handle,
    )
    return IsolationStage(worktree_manager=manager, shadow_git=supervisor)
