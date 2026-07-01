"""Shadow-Git rollback primitive — U-IS-15.

Implements C-IS-08 §8.3 (shadow-Git rollback). Declares `rollback_to_checkpoint`
— restores tracked filesystem state to a shadow-Git checkpoint snapshot
(created by U-IS-14) and records the rollback as a state-ledger entry.

Rollback is **filesystem-bounded** (C-IS-08 §8.3 row 2): it restores tracked
working-tree files to the checkpoint, but the state-ledger is NOT restored —
the ledger's bytes are preserved across the `git checkout` and a rollback
event is then appended to it. Inference state is not restored (rollback only
touches the filesystem). Git is accessed by shelling out to the `git` CLI
(Meta-Architecture shell-out substitution; no Python git library —
`CLAUDE.md` §3.2).

The plan-grade signature names `checkpoint_id` / `workflow_run_id`; the
repository root and the JSONL ledger handle (declared Inputs — the git target
and the `append_ledger_entry` target) are threaded as the leading parameters.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.1 U-IS-15
(preserved verbatim from v2.1 §2); Spec_Information_Substrate_v1.md C-IS-08
§8.3; ADR-F2 v1.2 §Rationale (a).
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    Actor,
    ActorClass,
    Identifier,
    Timestamp,
)
from harness_is.state_ledger_write import EntryPayload, WriteKey, append_ledger_entry

_SHADOW_REF_PREFIX = "refs/shadow"
_ROLLBACK_ACTOR = Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-rollback")


class RollbackStatus(StrEnum):
    """The outcome status of a `rollback_to_checkpoint` call (C-IS-08 §8.3)."""

    RESTORED = "restored"
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    ROLLBACK_FAILED = "rollback_failed"


class RollbackResult(BaseModel):
    """The result of a shadow-Git rollback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RollbackStatus
    restored_at: Timestamp
    rollback_entry_id: Identifier | None


def _git(repository_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository_root), *args], capture_output=True, text=True
    )


def rollback_to_checkpoint(
    repository_root: Path,
    ledger_handle: JsonlLedgerHandle,
    checkpoint_id: Identifier,
    workflow_run_id: Identifier,
) -> RollbackResult:
    """Roll the working tree back to a shadow-Git checkpoint (C-IS-08 §8.3).

    Restores tracked filesystem state to the checkpoint snapshot atomically
    (git-native, acceptance #1) and filesystem-bounded — the state-ledger is
    preserved, not restored (acceptance #2/#3). On success a rollback event
    carrying `checkpoint_id` is appended to the ledger (acceptance #4). A
    non-existent checkpoint yields `CHECKPOINT_NOT_FOUND` with no FS or ledger
    change (acceptance #5); a mid-rollback git failure yields `ROLLBACK_FAILED`
    with no ledger entry (acceptance #6).
    """
    now = datetime.now(UTC)
    shadow_ref = f"{_SHADOW_REF_PREFIX}/{workflow_run_id}/{checkpoint_id}"
    if _git(repository_root, "rev-parse", "--verify", "--quiet", shadow_ref).returncode != 0:
        return RollbackResult(
            status=RollbackStatus.CHECKPOINT_NOT_FOUND,
            restored_at=now,
            rollback_entry_id=None,
        )

    # Preserve the ledger bytes across the restore — the ledger is NOT rolled back.
    ledger_path = ledger_handle.canonical_path
    ledger_bytes = ledger_path.read_bytes() if ledger_path.exists() else None

    checkout = _git(repository_root, "checkout", shadow_ref, "--", ".")
    if ledger_bytes is not None:
        ledger_path.write_bytes(ledger_bytes)
    if checkout.returncode != 0:
        return RollbackResult(
            status=RollbackStatus.ROLLBACK_FAILED,
            restored_at=now,
            rollback_entry_id=None,
        )

    # Record the rollback as a state-ledger entry (acceptance #4).
    # R-003: `procedural_tier_snapshot_ref` is left `None`-canonical here
    # (IS spec v1.3 §C-IS-05 §5.1). A rollback is an administrative / recovery
    # operation, not an active-workflow-context producer emission — so the
    # D-derivative sidecar does not apply.
    rollback_entry_id = Identifier(str(uuid.uuid4()))
    append_ledger_entry(
        ledger_handle,
        EntryPayload(
            action_id=Identifier(f"rollback:{checkpoint_id}"),
            idempotency_key=rollback_entry_id,
            actor=_ROLLBACK_ACTOR,
            timestamp=now,
        ),
        WriteKey(
            thread_id=workflow_run_id,
            step_id=Identifier(f"rollback-{checkpoint_id}"),
            idempotency_key=rollback_entry_id,
        ),
    )
    return RollbackResult(
        status=RollbackStatus.RESTORED,
        restored_at=now,
        rollback_entry_id=rollback_entry_id,
    )
