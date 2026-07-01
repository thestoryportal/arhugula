"""Worktree-isolation primitive — U-IS-16.

Implements C-IS-09 §9.2 (per-sub-agent worktree allocation + reclamation) +
§9.3 (concurrent-read isolation invariants). Provides the
workload-class-opt-in worktree isolation that gives each sub-agent its own
working directory over a shared `.git` object store.

`WorktreeIsolationManager` is the §9 "worktree lifecycle manager" — a stateful
manager so the concurrency cap (acceptance #3) and opt-out (#10) can be
enforced across allocations. The plan-grade signatures `allocate_worktree` /
`reclaim_worktree` are realized as its methods; the repository root, the
worktree base directory (§9.4-deferred location, configuration-supplied), and
the `WorkloadManifestOptIns` input are constructor-injected.

Git worktrees are created/removed by shelling out to `git worktree`
(Meta-Architecture shell-out substitution; no Python git library —
`CLAUDE.md` §3.2). `git worktree` gives directory isolation over one shared
`.git` backend natively — the §9.3 read/write isolation invariants hold by its
construction.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.1 U-IS-16
(preserved verbatim from v2.1 §2); Spec_Information_Substrate_v1.md C-IS-09
§9.2 / §9.3; ADR-F2 v1.2 §Decision; ADR-D4 v1.1.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from harness_is.state_ledger_entry_schema import Identifier, Timestamp
from harness_is.workload_manifest_opt_in_schema import WorkloadManifestOptIns


class ReclamationTrigger(StrEnum):
    """What triggered a worktree reclamation (C-IS-09 §9.2)."""

    SUB_AGENT_SUCCESS = "sub_agent_success"
    SUB_AGENT_FAILURE = "sub_agent_failure"
    OPERATOR_POLICY_LIFECYCLE_MARKER = "operator_policy_lifecycle_marker"


class ReclamationResult(StrEnum):
    """The outcome of a worktree reclamation (C-IS-09 §9.2)."""

    RECLAIMED = "reclaimed"
    RECLAMATION_FAILED = "reclamation_failed"


class WorktreeHandle(BaseModel):
    """A handle to an allocated per-sub-agent worktree (C-IS-09 §9.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worktree_id: Identifier
    worktree_path: Path
    parent_run_id: Identifier
    sub_agent_id: Identifier
    allocated_at: Timestamp


class WorktreeIsolationDisabledError(RuntimeError):
    """Raised when allocation is attempted under `worktree_isolation_enabled = false`.

    Materializes acceptance #10 — an opted-out workload allocates 0 worktrees.
    """


class WorktreeConcurrencyCapExceededError(RuntimeError):
    """Raised when allocation would exceed `worktree_concurrency_cap`.

    Materializes acceptance #3 — the (N+1)th concurrent allocation is rejected
    when the cap is N.
    """


class WorktreeIsolationManager:
    """Stateful per-sub-agent worktree lifecycle manager (C-IS-09 §9.2 / §9.3)."""

    def __init__(
        self,
        repository_root: Path,
        worktree_base: Path,
        opt_ins: WorkloadManifestOptIns,
    ) -> None:
        self._repository_root = repository_root
        self._worktree_base = worktree_base
        self._opt_ins = opt_ins
        self._active: dict[Identifier, WorktreeHandle] = {}

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self._repository_root), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def allocate_worktree(
        self,
        parent_workflow_run_id: Identifier,
        sub_agent_id: Identifier,
    ) -> WorktreeHandle:
        """Allocate an isolated worktree for a sub-agent (C-IS-09 §9.2).

        The worktree is an isolated directory sharing the parent's `.git`
        object store (acceptance #1). Raises `WorktreeIsolationDisabledError`
        when the workload opted out (#10) and `WorktreeConcurrencyCapExceededError`
        when the cap would be exceeded (#3).
        """
        if not self._opt_ins.worktree_isolation_enabled:
            raise WorktreeIsolationDisabledError(
                "worktree allocation rejected: worktree_isolation_enabled is false"
            )
        cap = self._opt_ins.worktree_concurrency_cap
        if cap is not None and len(self._active) >= cap:
            raise WorktreeConcurrencyCapExceededError(
                f"worktree allocation rejected: concurrency cap {cap} reached"
            )
        worktree_id = Identifier(str(uuid.uuid4()))
        worktree_path = self._worktree_base / worktree_id
        self._git("worktree", "add", "--detach", str(worktree_path), "HEAD")
        handle = WorktreeHandle(
            worktree_id=worktree_id,
            worktree_path=worktree_path,
            parent_run_id=parent_workflow_run_id,
            sub_agent_id=sub_agent_id,
            allocated_at=datetime.now(UTC),
        )
        self._active[worktree_id] = handle
        return handle

    def reclaim_worktree(
        self,
        worktree_handle: WorktreeHandle,
        reclamation_trigger: ReclamationTrigger,
    ) -> ReclamationResult:
        """Reclaim a worktree (C-IS-09 §9.2).

        Invokes `git worktree remove` + directory-contents removal
        (acceptance #4); the shared `.git` object backend is preserved
        (acceptance #5). `reclamation_trigger` records the operator-policy /
        lifecycle context (acceptance #6). Returns `RECLAMATION_FAILED` rather
        than raising on a git-level failure.
        """
        try:
            self._git("worktree", "remove", "--force", str(worktree_handle.worktree_path))
        except subprocess.CalledProcessError:
            return ReclamationResult.RECLAMATION_FAILED
        if worktree_handle.worktree_path.exists():
            shutil.rmtree(worktree_handle.worktree_path, ignore_errors=True)
        self._active.pop(worktree_handle.worktree_id, None)
        return ReclamationResult.RECLAIMED

    @property
    def active_count(self) -> int:
        """The number of currently-allocated worktrees (concurrency-cap state)."""
        return len(self._active)
