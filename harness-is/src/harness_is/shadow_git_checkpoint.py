"""Shadow-Git checkpoint primitive — U-IS-14.

Implements C-IS-08 §8.2 (cadence-driven checkpoint firing) + §8.4 (shadow-ref
construction). Creates workload-class-opt-in shadow-Git checkpoint snapshots —
git refs under `refs/shadow/` that capture working state without polluting the
main branch history. Snapshot creation only; rollback is U-IS-15.

The shadow ref is created via `git update-ref` (git-native ref atomicity);
the snapshot commit is produced by `git stash create` (a working-state commit
object that touches nothing), falling back to `HEAD` for a clean tree. Git is
accessed by shelling out to the `git` CLI (Meta-Architecture shell-out
substitution; no Python git library — `CLAUDE.md` §3.2).

**`on_workflow_event` — deferred (Class 1, downstream of U-CORE-01 carrier-thin).**
IS plan v2.3 §2.2 U-IS-14 declares `on_workflow_event(event: WorkflowEvent) ->
Optional[CheckpointResult]` — an event-hook a cadence driver subscribes to. Its
`WorkflowEvent` payload type was struck when U-CORE-01 landed carrier-thin
(only `WorkflowEventClass` exists). Per operator ruling 2026-05-16 the hook is
**deferred** with the `WorkflowEvent` payload model; it lands when that payload
is defined. Tracked on `.harness/class_1_tension_u_core_01_workflow_event.md`
(open item F-3). U-IS-14's checkpoint primitive + cadence-driver decision land
here; none of the unit's 9 tests exercise `on_workflow_event`.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.2 U-IS-14
(REVISED — R2); Spec_Information_Substrate_v1.md C-IS-08 §8.2 / §8.4;
ADR-F2 v1.2 §Rationale (a).
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from harness_is.state_ledger_entry_schema import Identifier, Timestamp
from harness_is.workload_manifest_opt_in_schema import (
    CheckpointCadence,
    WorkloadManifestOptIns,
)

_SHADOW_REF_PREFIX = "refs/shadow"


class CheckpointTriggerContext(BaseModel):
    """The cadence + marker context that triggers a checkpoint (C-IS-08 §8.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cadence: CheckpointCadence
    workflow_step_id: Identifier | None = None
    tool_call_id: Identifier | None = None
    significant_change_marker: str | None = None
    explicit_marker: str | None = None


class CheckpointResult(BaseModel):
    """The result of a shadow-Git checkpoint snapshot creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: Identifier
    shadow_ref: str
    created_at: Timestamp
    triggered_by: CheckpointCadence


def _git(repository_root: Path, *args: str) -> str:
    """Run a `git` command in the repo, returning stdout (shell-out substitution)."""
    return subprocess.run(
        ["git", "-C", str(repository_root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def should_checkpoint(
    opt_ins: WorkloadManifestOptIns,
    trigger_context: CheckpointTriggerContext,
) -> bool:
    """The cadence-driver firing decision (C-IS-08 §8.2 / §8.1).

    A checkpoint fires iff shadow-Git checkpointing is opted in AND the
    manifest's declared cadence matches the trigger context's cadence. An
    opted-out workload (`shadow_git_enabled = false`) never checkpoints
    (acceptance #7).
    """
    return opt_ins.shadow_git_enabled and opt_ins.shadow_git_cadence == trigger_context.cadence


def create_shadow_git_checkpoint(
    repository_root: Path,
    workflow_run_id: Identifier,
    trigger_context: CheckpointTriggerContext,
) -> CheckpointResult:
    """Create a shadow-Git checkpoint snapshot (C-IS-08 §8.4).

    Snapshots working state into a `refs/shadow/<workflow-run>/<checkpoint>`
    ref in the same repository as the versioning sub-role (acceptance #1).
    The ref lives outside the main branch history — invisible to `git log`
    on a branch (acceptance #2/#9). `git update-ref` is atomic (acceptance #8).
    The plan-grade signature names only `workflow_run_id` / `trigger_context`;
    the repository root is threaded as the leading parameter.
    """
    checkpoint_id = Identifier(str(uuid.uuid4()))
    snapshot = _git(repository_root, "stash", "create") or _git(
        repository_root, "rev-parse", "HEAD"
    )
    shadow_ref = f"{_SHADOW_REF_PREFIX}/{workflow_run_id}/{checkpoint_id}"
    _git(repository_root, "update-ref", shadow_ref, snapshot)
    return CheckpointResult(
        checkpoint_id=checkpoint_id,
        shadow_ref=shadow_ref,
        created_at=datetime.now(UTC),
        triggered_by=trigger_context.cadence,
    )
