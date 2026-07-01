"""Durable filesystem-journal engine pause/resume substrate tests (R-CXA-2 S3).

The load-bearing proof is ``test_resume_after_fresh_instance``: a *new*
substrate instance (simulating a process restart after a crash) resumes a pause
captured by a prior instance, reading the on-disk journal — the durability the
in-memory ``DeterministicEnginePauseResumeSubstrate`` cannot provide and that the
R-CXA-2 bounded-residual was recorded against (brief §3.5/§3.8).

The fail-closed tests cover the crash-during-append corruption surface (codex
review): a torn latest record must ABORT, never silently resume stale state, and
must never crash or block another workflow.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from harness_core import EntryID, WorkflowID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import ExternalReference, ReferenceClass, StateSummary
from harness_cp.material_diff_detection import MaterialDiff
from harness_cp.pause_resume_protocol import (
    PauseEvent,
    PauseReason,
    ResumeAttempt,
    ResumeOutcomeKind,
    attempt_resume,
    bind_engine_pause_resume_substrate,
    capture_pause_snapshot,
)
from harness_is.state_ledger_entry_schema import Identifier
from harness_runtime.lifecycle.journal_pause_resume_substrate import (
    JournalEnginePauseResumeSubstrate,
)

_ACTOR = ActorIdentity("engine-loop")


def _state_summary(version: str = "v1", snapshot: bytes | None = None) -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text=version,
        summary_hash=hashlib.sha256(version.encode()).hexdigest(),
        idempotency_key=Identifier("idem-" + version),
        external_references=(
            ExternalReference(
                reference_class=ReferenceClass.FILESYSTEM_STATE,
                reference_id="state-" + version,
                snapshot_capture_at_pause=(
                    snapshot if snapshot is not None else b"snapshot-" + version.encode("utf-8")
                ),
            ),
        ),
    )


def _resume_attempt(workflow_id: str) -> ResumeAttempt:
    return ResumeAttempt(
        paused_workflow_id=WorkflowID(workflow_id),
        resume_at="2026-06-10T12:00:00Z",
        resume_request_actor=_ACTOR,
    )


def _substrate(journal_dir: Path, **kwargs: object) -> JournalEnginePauseResumeSubstrate:
    return JournalEnginePauseResumeSubstrate(
        journal_dir=journal_dir,
        state_summary_provider=_state_summary,
        **kwargs,  # type: ignore[arg-type]
    )


def test_capture_writes_durable_journal(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)

    event = substrate.capture_pause_snapshot(
        WorkflowID("wf-1"), PauseReason.OPERATOR_INITIATED_PAUSE
    )

    journal = substrate._journal_file(WorkflowID("wf-1"))  # type: ignore[attr-defined]
    assert journal.exists(), "capture must persist to disk"
    lines = [line for line in journal.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    rehydrated = substrate._read_latest(WorkflowID("wf-1"))  # type: ignore[attr-defined]
    assert rehydrated == event


def test_resume_after_fresh_instance(tmp_path: Path) -> None:
    """The cross-process durability proof — a fresh instance resumes a prior pause."""
    # Process A captures, then "crashes" (instance dropped).
    capturing = _substrate(tmp_path)
    captured = capturing.capture_pause_snapshot(
        WorkflowID("wf-crash"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    del capturing

    # Process B starts fresh over the same on-disk journal dir and resumes.
    resuming = _substrate(tmp_path)
    outcome = resuming.attempt_resume(_resume_attempt("wf-crash"))

    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    rehydrated = resuming._read_latest(WorkflowID("wf-crash"))  # type: ignore[attr-defined]
    assert rehydrated == captured


def test_resume_absent_pause_aborts(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)

    outcome = substrate.attempt_resume(_resume_attempt("never-paused"))

    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_resume_with_material_diff_revalidates(tmp_path: Path) -> None:
    def _material(_event: PauseEvent, _attempt: ResumeAttempt) -> tuple[MaterialDiff, ...]:
        return (
            MaterialDiff(
                reference=ExternalReference(
                    reference_class=ReferenceClass.FILESYSTEM_STATE,
                    reference_id="state-v1",
                ),
                prior_snapshot=b"prior",
                current_value=b"current",
                is_material=True,
            ),
        )

    revalidates = _substrate(tmp_path, diff_provider=_material)
    revalidates.capture_pause_snapshot(WorkflowID("wf-diff"), PauseReason.OPERATOR_INITIATED_PAUSE)
    assert (
        revalidates.attempt_resume(_resume_attempt("wf-diff")).outcome_kind
        is ResumeOutcomeKind.RESUME_AFTER_REVALIDATION
    )

    aborts = _substrate(
        tmp_path,
        diff_provider=_material,
        revalidation_succeeded=lambda _attempt, _diff: False,
    )
    assert (
        aborts.attempt_resume(_resume_attempt("wf-diff")).outcome_kind
        is ResumeOutcomeKind.ABORT_REVALIDATION_FAILED
    )


def test_multiple_pauses_resume_latest(tmp_path: Path) -> None:
    versions = iter(("v1", "v2"))
    substrate = JournalEnginePauseResumeSubstrate(
        journal_dir=tmp_path,
        state_summary_provider=lambda: _state_summary(next(versions)),
    )

    first = substrate.capture_pause_snapshot(
        WorkflowID("wf-multi"), PauseReason.OPERATOR_INITIATED_PAUSE
    )
    second = substrate.capture_pause_snapshot(
        WorkflowID("wf-multi"), PauseReason.OPERATOR_INITIATED_PAUSE
    )
    assert first != second

    latest = substrate._read_latest(WorkflowID("wf-multi"))  # type: ignore[attr-defined]
    assert latest == second


def test_journal_roundtrip_preserves_pause_event_fields(tmp_path: Path) -> None:
    substrate = _substrate(
        tmp_path,
        pause_audit_entry_id_provider=lambda _wf, _reason: EntryID("pause-entry-fixed"),
    )

    captured = substrate.capture_pause_snapshot(
        WorkflowID("wf-roundtrip"), PauseReason.HITL_INVOCATION_PENDING
    )
    rehydrated = substrate._read_latest(WorkflowID("wf-roundtrip"))  # type: ignore[attr-defined]

    assert rehydrated is not None
    assert rehydrated.pause_reason is PauseReason.HITL_INVOCATION_PENDING
    assert rehydrated.paused_at == captured.paused_at
    assert rehydrated.pause_audit_entry_id == "pause-entry-fixed"
    assert (
        rehydrated.state_summary_snapshot.summary_hash
        == captured.state_summary_snapshot.summary_hash
    )
    assert rehydrated.external_refs_captured == captured.external_refs_captured
    assert rehydrated.external_refs_captured[0].snapshot_capture_at_pause == b"snapshot-v1"


def test_non_utf8_snapshot_bytes_roundtrip(tmp_path: Path) -> None:
    """Arbitrary (non-UTF-8) snapshot anchors must journal + rehydrate, not crash."""
    arbitrary = b"\xff\xfe\x00\x80-not-utf8"
    substrate = JournalEnginePauseResumeSubstrate(
        journal_dir=tmp_path,
        state_summary_provider=lambda: _state_summary(snapshot=arbitrary),
    )

    captured = substrate.capture_pause_snapshot(
        WorkflowID("wf-bytes"), PauseReason.OPERATOR_INITIATED_PAUSE
    )
    rehydrated = substrate._read_latest(WorkflowID("wf-bytes"))  # type: ignore[attr-defined]

    assert captured.external_refs_captured[0].snapshot_capture_at_pause == arbitrary
    assert rehydrated == captured


def test_default_pause_audit_entry_id_matches_deterministic_derivation(tmp_path: Path) -> None:
    """The default id digest mirrors DeterministicEnginePauseResumeSubstrate."""
    substrate = _substrate(tmp_path)

    event = substrate.capture_pause_snapshot(
        WorkflowID("wf-id"), PauseReason.OPERATOR_INITIATED_PAUSE
    )

    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-id",
                PauseReason.OPERATOR_INITIATED_PAUSE.value.encode("utf-8"),
                _state_summary().summary_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    assert event.pause_audit_entry_id == expected


def test_torn_latest_append_aborts_not_stale_resume(tmp_path: Path) -> None:
    """A crash mid-second-pause must ABORT, not silently resume the older snapshot."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-torn"), PauseReason.OPERATOR_INITIATED_PAUSE)

    # Crash partway through the *second* append for the same workflow.
    journal = substrate._journal_file(WorkflowID("wf-torn"))  # type: ignore[attr-defined]
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"workflow_id": "wf-torn", "pause_event": {partial')

    outcome = substrate.attempt_resume(_resume_attempt("wf-torn"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_only_corrupt_record_aborts(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)
    journal = substrate._journal_file(WorkflowID("wf-y"))  # type: ignore[attr-defined]
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"workflow_id": "wf-y", "pause_event": {not-json\n')

    outcome = substrate.attempt_resume(_resume_attempt("wf-y"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_invalid_base64_snapshot_aborts(tmp_path: Path) -> None:
    """Valid JSON but a malformed bytes sentinel is corruption → fail closed."""
    substrate = _substrate(tmp_path)
    journal = substrate._journal_file(WorkflowID("wf-b64"))  # type: ignore[attr-defined]
    journal.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON; the snapshot bytes wrapper carries non-base64 garbage.
    journal.write_text(
        '{"workflow_id": "wf-b64", "pause_event": '
        '{"state_summary_snapshot": {"external_references": '
        '[{"snapshot_capture_at_pause": {"__bytes_b64__": "%%%%"}}]}}}\n'
    )

    outcome = substrate.attempt_resume(_resume_attempt("wf-b64"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_non_utf8_journal_file_aborts(tmp_path: Path) -> None:
    """A journal file corrupted with raw non-UTF-8 bytes fails closed, not crashes."""
    substrate = _substrate(tmp_path)
    journal = substrate._journal_file(WorkflowID("wf-raw"))  # type: ignore[attr-defined]
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"\xff\xfe\x00garbage\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-raw"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_invalid_utf8_inside_valid_json_aborts(tmp_path: Path) -> None:
    """A bad byte inside otherwise-valid JSON must ABORT, not resume altered state."""
    substrate = _substrate(tmp_path)
    journal = substrate._journal_file(WorkflowID("wf-u"))  # type: ignore[attr-defined]
    journal.parent.mkdir(parents=True, exist_ok=True)
    # Structurally JSON-shaped, but a non-UTF-8 byte sits inside a string value.
    journal.write_bytes(b'{"workflow_id": "wf-u", "pause_event": {"summary_text": "\xff"}}\n')

    outcome = substrate.attempt_resume(_resume_attempt("wf-u"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_corrupt_record_does_not_block_other_workflow(tmp_path: Path) -> None:
    """Per-workflow files isolate corruption: a bad file never blocks another workflow."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-good"), PauseReason.OPERATOR_INITIATED_PAUSE)

    # Corrupt a *different* workflow's journal file entirely.
    other = substrate._journal_file(WorkflowID("wf-other"))  # type: ignore[attr-defined]
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not even json at all\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-good"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN


def test_conforms_to_protocol_via_bound_free_functions(tmp_path: Path) -> None:
    """Pin the *intended* use: bound as the engine substrate, driven via free functions.

    ``RuntimeEngineRecoveryLoop`` never calls the substrate methods directly — it
    binds the substrate with ``bind_engine_pause_resume_substrate`` and invokes the
    module-level ``capture_pause_snapshot`` / ``attempt_resume`` free functions.
    Passing the substrate to that ``EnginePauseResumeSubstrate``-typed binder also
    gives pyright a typed context that verifies Protocol conformance.
    """
    substrate = _substrate(tmp_path)

    with bind_engine_pause_resume_substrate(substrate):
        event = capture_pause_snapshot(WorkflowID("wf-bound"), PauseReason.ENGINE_NATIVE_PAUSE)
        outcome = attempt_resume(_resume_attempt("wf-bound"))

    assert event.pause_reason is PauseReason.ENGINE_NATIVE_PAUSE
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
