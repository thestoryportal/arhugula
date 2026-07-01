"""Tests for U-IS-15 — shadow-Git rollback primitive (C-IS-08 §8.3).

Test set per the U-IS-15 `Tests:` field — 7 tests covering acceptance #1-#6.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.shadow_git_checkpoint import (
    CheckpointTriggerContext,
    create_shadow_git_checkpoint,
)
from harness_is.shadow_git_rollback import RollbackStatus, rollback_to_checkpoint
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteKey, append_ledger_entry, read_ledger
from harness_is.workload_manifest_opt_in_schema import CheckpointCadence

_RUN = Identifier("wf-run-1")
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-1")


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
    (repo / "app.py").write_text("v1\n")
    (repo / "lib.py").write_text("lib-v1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _ledger(repo: Path) -> JsonlLedgerHandle:
    """A ledger handle (untracked file inside the repo) with one prior entry."""
    handle = JsonlLedgerHandle(canonical_path=repo / "state.jsonl", exists=False, entry_count=0)
    append_ledger_entry(
        handle,
        EntryPayload(
            action_id=Identifier("act-pre"),
            idempotency_key=Identifier("idem-pre"),
            actor=_ACTOR,
            timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
        ),
        WriteKey(
            thread_id=_RUN, step_id=Identifier("step-0"), idempotency_key=Identifier("idem-pre")
        ),
    )
    return handle


def _checkpoint(repo: Path) -> Identifier:
    result = create_shadow_git_checkpoint(
        repo,
        _RUN,
        CheckpointTriggerContext(
            cadence=CheckpointCadence.PER_EXPLICIT_MARKER, explicit_marker="m"
        ),
    )
    return result.checkpoint_id


def test_rollback_restores_filesystem(tmp_path: Path) -> None:
    """Acceptance #2/#3 — rollback restores tracked filesystem state."""
    repo = _repo(tmp_path)
    checkpoint_id = _checkpoint(repo)
    (repo / "app.py").write_text("v2-broken\n")
    result = rollback_to_checkpoint(repo, _ledger(repo), checkpoint_id, _RUN)
    assert result.status is RollbackStatus.RESTORED
    assert (repo / "app.py").read_text() == "v1\n"


def test_rollback_does_not_restore_ledger(tmp_path: Path) -> None:
    """Acceptance #2 — the state-ledger is not rolled back."""
    repo = _repo(tmp_path)
    checkpoint_id = _checkpoint(repo)
    handle = _ledger(repo)
    (repo / "app.py").write_text("v2\n")
    rollback_to_checkpoint(repo, handle, checkpoint_id, _RUN)
    # The pre-rollback entry survives (the ledger was not restored).
    action_ids = [e.action_id for e in read_ledger(handle)]
    assert "act-pre" in action_ids


def test_rollback_writes_rollback_event_to_ledger(tmp_path: Path) -> None:
    """Acceptance #4 — rollback appends an event carrying the checkpoint_id."""
    repo = _repo(tmp_path)
    checkpoint_id = _checkpoint(repo)
    handle = _ledger(repo)
    rollback_to_checkpoint(repo, handle, checkpoint_id, _RUN)
    assert read_ledger(handle)[-1].action_id == f"rollback:{checkpoint_id}"


def test_rollback_atomic_full_or_none(tmp_path: Path) -> None:
    """Acceptance #1 — rollback restores every checkpointed file (full)."""
    repo = _repo(tmp_path)
    checkpoint_id = _checkpoint(repo)
    (repo / "app.py").write_text("app-broken\n")
    (repo / "lib.py").write_text("lib-broken\n")
    rollback_to_checkpoint(repo, _ledger(repo), checkpoint_id, _RUN)
    assert (repo / "app.py").read_text() == "v1\n"
    assert (repo / "lib.py").read_text() == "lib-v1\n"


def test_rollback_checkpoint_not_found(tmp_path: Path) -> None:
    """Acceptance #5 — an unknown checkpoint ⇒ CHECKPOINT_NOT_FOUND, no change."""
    repo = _repo(tmp_path)
    handle = _ledger(repo)
    (repo / "app.py").write_text("v2\n")
    entries_before = len(read_ledger(handle))
    result = rollback_to_checkpoint(repo, handle, Identifier("no-such-ckpt"), _RUN)
    assert result.status is RollbackStatus.CHECKPOINT_NOT_FOUND
    assert result.rollback_entry_id is None
    assert (repo / "app.py").read_text() == "v2\n"  # filesystem unchanged
    assert len(read_ledger(handle)) == entries_before  # ledger unchanged


def test_rollback_filesystem_bounded(tmp_path: Path) -> None:
    """Acceptance #2 — rollback restores the FS but the ledger is preserved."""
    repo = _repo(tmp_path)
    checkpoint_id = _checkpoint(repo)
    handle = _ledger(repo)
    (repo / "app.py").write_text("v2\n")
    rollback_to_checkpoint(repo, handle, checkpoint_id, _RUN)
    assert (repo / "app.py").read_text() == "v1\n"  # FS restored
    assert "act-pre" in [e.action_id for e in read_ledger(handle)]  # ledger kept


def test_rollback_does_not_modify_inference_state(tmp_path: Path) -> None:
    """Acceptance #3 — rollback is filesystem-bounded: it returns a
    RollbackResult and touches only tracked FS + the ledger event; there is no
    inference-state restoration (IS holds no inference state)."""
    repo = _repo(tmp_path)
    checkpoint_id = _checkpoint(repo)
    result = rollback_to_checkpoint(repo, _ledger(repo), checkpoint_id, _RUN)
    assert result.status is RollbackStatus.RESTORED
    assert result.rollback_entry_id is not None
