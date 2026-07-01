"""Durable WAL segment-log engine pause/resume substrate tests (R-FS-1 E-2 / U-RT-121).

The load-bearing proofs are the WAL-recovery semantics that distinguish this
substrate from the #475 ``JournalEnginePauseResumeSubstrate`` it extends:

- ``test_resume_after_fresh_instance`` — cross-process durability (a fresh
  instance resumes a pause captured by a prior one; the #475 property, per
  segment).
- ``test_torn_tail_segment_recovers_to_last_committed`` — a half-written trailing
  segment is DISCARDED and replay recovers to the last committed segment
  (RESUME_CLEAN), the WAL recovery property that DIVERGES from #475's
  fail-closed-on-torn-latest rule (a torn tail is an uncommitted write).
- ``test_corrupt_middle_segment_stops_at_gap`` — a corrupt middle segment is NOT
  skipped: replay stops at the gap and recovers the prefix BEFORE it, never
  resuming *past* a corruption (gap-safe).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from harness_core import WorkflowID
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
from harness_runtime.lifecycle.journal_pause_resume_substrate import json_default
from harness_runtime.lifecycle.wal_segment_pause_resume_substrate import (
    WALSegmentEnginePauseResumeSubstrate,
)

_ACTOR = ActorIdentity("engine-loop")


def _state_summary(version: str = "v1") -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text=version,
        summary_hash=hashlib.sha256(version.encode()).hexdigest(),
        idempotency_key=Identifier("idem-" + version),
        external_references=(
            ExternalReference(
                reference_class=ReferenceClass.FILESYSTEM_STATE,
                reference_id="state-" + version,
                snapshot_capture_at_pause=b"snapshot-" + version.encode("utf-8"),
            ),
        ),
    )


def _resume_attempt(workflow_id: str) -> ResumeAttempt:
    return ResumeAttempt(
        paused_workflow_id=WorkflowID(workflow_id),
        resume_at="2026-06-15T12:00:00Z",
        resume_request_actor=_ACTOR,
    )


def _substrate(
    segment_log_dir: Path,
    *,
    state_summary_provider: Callable[[], StateSummary] | None = None,
    **kwargs: object,
) -> WALSegmentEnginePauseResumeSubstrate:
    return WALSegmentEnginePauseResumeSubstrate(
        journal_dir=segment_log_dir,
        state_summary_provider=state_summary_provider or _state_summary,
        **kwargs,  # type: ignore[arg-type]
    )


def _versioned_substrate(segment_log_dir: Path) -> WALSegmentEnginePauseResumeSubstrate:
    versions = iter(f"v{i}" for i in range(1, 100))
    return WALSegmentEnginePauseResumeSubstrate(
        journal_dir=segment_log_dir,
        state_summary_provider=lambda: _state_summary(next(versions)),
    )


# --- capture / durability ---------------------------------------------------


def test_capture_writes_durable_checksummed_segment(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)
    event = substrate.capture_pause_snapshot(
        WorkflowID("wf-1"), PauseReason.OPERATOR_INITIATED_PAUSE
    )

    log = substrate._journal_file(WorkflowID("wf-1"))  # type: ignore[attr-defined]
    assert log.exists(), "capture must persist to disk"
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    # The record is the checksum + payload WAL frame (not the bare #475 JSONL).
    outer = json.loads(lines[0])
    assert set(outer) == {"checksum", "payload"}
    assert hashlib.sha256(outer["payload"].encode("utf-8")).hexdigest() == outer["checksum"]
    assert json.loads(outer["payload"])["segment_index"] == 0
    assert substrate._read_latest(WorkflowID("wf-1")) == event  # type: ignore[attr-defined]


def test_segment_index_is_monotonic_per_workflow(tmp_path: Path) -> None:
    substrate = _versioned_substrate(tmp_path)
    for _ in range(3):
        substrate.capture_pause_snapshot(WorkflowID("wf-seq"), PauseReason.ENGINE_NATIVE_PAUSE)

    log = substrate._journal_file(WorkflowID("wf-seq"))  # type: ignore[attr-defined]
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    indices = [json.loads(json.loads(line)["payload"])["segment_index"] for line in lines]
    assert indices == [0, 1, 2]


def test_resume_after_fresh_instance(tmp_path: Path) -> None:
    """Cross-process durability — a fresh instance resumes a prior-process pause."""
    capturing = _substrate(tmp_path)
    captured = capturing.capture_pause_snapshot(
        WorkflowID("wf-crash"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    del capturing

    resuming = _substrate(tmp_path)
    outcome = resuming.attempt_resume(_resume_attempt("wf-crash"))

    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert resuming._read_latest(WorkflowID("wf-crash")) == captured  # type: ignore[attr-defined]


def test_multiple_segments_resume_latest_committed(tmp_path: Path) -> None:
    substrate = _versioned_substrate(tmp_path)
    first = substrate.capture_pause_snapshot(
        WorkflowID("wf-multi"), PauseReason.OPERATOR_INITIATED_PAUSE
    )
    second = substrate.capture_pause_snapshot(
        WorkflowID("wf-multi"), PauseReason.OPERATOR_INITIATED_PAUSE
    )
    assert first != second
    assert substrate._read_latest(WorkflowID("wf-multi")) == second  # type: ignore[attr-defined]


def test_attempt_resume_is_idempotent_read(tmp_path: Path) -> None:
    """Per-segment dedup property: repeated resume reads return the same outcome
    (the substrate is a pure read; it never double-applies a segment)."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-idem"), PauseReason.ENGINE_NATIVE_PAUSE)

    first = substrate.attempt_resume(_resume_attempt("wf-idem"))
    second = substrate.attempt_resume(_resume_attempt("wf-idem"))
    assert first.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert second.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert substrate._read_latest(WorkflowID("wf-idem")) is not None  # type: ignore[attr-defined]


# --- WAL torn-write / recovery semantics ------------------------------------


def test_torn_tail_segment_recovers_to_last_committed(tmp_path: Path) -> None:
    """WAL recovery (DIVERGES from #475): a half-written trailing segment is
    discarded and replay recovers to the last committed segment, RESUME_CLEAN —
    NOT ABORT. A torn tail is an uncommitted (un-fsync'd) write, so the last
    committed segment is the authoritative latest state."""
    substrate = _versioned_substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-torn"), PauseReason.OPERATOR_INITIATED_PAUSE)
    second = substrate.capture_pause_snapshot(
        WorkflowID("wf-torn"), PauseReason.OPERATOR_INITIATED_PAUSE
    )

    # Crash partway through the THIRD append (torn trailing segment).
    log = substrate._journal_file(WorkflowID("wf-torn"))  # type: ignore[attr-defined]
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"checksum": "deadbeef", "payload": "{partial')

    outcome = substrate.attempt_resume(_resume_attempt("wf-torn"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    # Recovered to the last COMMITTED segment (the 2nd), not the torn tail.
    assert substrate._read_latest(WorkflowID("wf-torn")) == second  # type: ignore[attr-defined]


def test_torn_tail_then_append_recovers_and_continues(tmp_path: Path) -> None:
    """[P1] (Codex) WAL recovery-on-open: after a crash leaves a torn trailing
    segment (no final newline), the NEXT capture must truncate the torn tail and
    append cleanly — replay then sees the NEW committed segment, not a
    permanently-corrupt merged line (which would freeze resume on the older one).
    """
    substrate = _versioned_substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-recover"), PauseReason.OPERATOR_INITIATED_PAUSE)
    log = substrate._journal_file(WorkflowID("wf-recover"))  # type: ignore[attr-defined]
    # Crash mid-second-append: a torn trailing segment with NO trailing newline.
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"checksum": "x", "payload": "{torn-no-newline')

    # The next capture recovers (discards the torn tail) + appends segment 1.
    third = substrate.capture_pause_snapshot(
        WorkflowID("wf-recover"), PauseReason.OPERATOR_INITIATED_PAUSE
    )

    # Replay sees the NEW committed segment — not the older one, not corruption.
    assert substrate._read_latest(WorkflowID("wf-recover")) == third  # type: ignore[attr-defined]
    outcome = substrate.attempt_resume(_resume_attempt("wf-recover"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    # On disk: exactly 2 clean, monotonically-indexed segments (the torn tail gone).
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    indices = [json.loads(json.loads(line)["payload"])["segment_index"] for line in lines]
    assert indices == [0, 1]


def test_corrupt_middle_segment_stops_at_gap(tmp_path: Path) -> None:
    """Gap-safe: a corrupt MIDDLE segment is not skipped — replay stops at the
    gap and recovers the prefix BEFORE it, never resuming past a corruption."""
    substrate = _versioned_substrate(tmp_path)
    first = substrate.capture_pause_snapshot(
        WorkflowID("wf-gap"), PauseReason.OPERATOR_INITIATED_PAUSE
    )
    substrate.capture_pause_snapshot(WorkflowID("wf-gap"), PauseReason.OPERATOR_INITIATED_PAUSE)
    substrate.capture_pause_snapshot(WorkflowID("wf-gap"), PauseReason.OPERATOR_INITIATED_PAUSE)

    # Tamper the MIDDLE segment (index 1): break its checksum-vs-payload match.
    log = substrate._journal_file(WorkflowID("wf-gap"))  # type: ignore[attr-defined]
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    corrupt = json.loads(lines[1])
    corrupt["checksum"] = "0" * 64  # no longer matches payload
    lines[1] = json.dumps(corrupt, sort_keys=True)
    log.write_text("\n".join(lines) + "\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-gap"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    # Recovered to segment 0 (before the gap), NOT segment 2 (past it).
    assert substrate._read_latest(WorkflowID("wf-gap")) == first  # type: ignore[attr-defined]


def test_checksum_mismatch_on_first_segment_aborts(tmp_path: Path) -> None:
    """A tampered payload (checksum no longer matches) on the only segment is
    corruption with no valid prefix → ABORT_SNAPSHOT_CORRUPTED."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-cs"), PauseReason.ENGINE_NATIVE_PAUSE)

    log = substrate._journal_file(WorkflowID("wf-cs"))  # type: ignore[attr-defined]
    record = json.loads(log.read_text().splitlines()[0])
    # The stored checksum no longer matches the (unchanged) payload bytes.
    record["checksum"] = "0" * 64
    log.write_text(json.dumps(record, sort_keys=True) + "\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-cs"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_out_of_order_segment_index_aborts(tmp_path: Path) -> None:
    """A record whose segment_index does not match its position (ordering
    integrity break) ends the valid prefix."""
    substrate = _substrate(tmp_path)
    log = substrate._journal_file(WorkflowID("wf-ord"))  # type: ignore[attr-defined]
    log.parent.mkdir(parents=True, exist_ok=True)
    # A well-formed payload but with segment_index=5 at physical position 0.
    payload = json.dumps(
        {
            "workflow_id": "wf-ord",
            "segment_index": 5,
            "pause_event": _make_pause_event().model_dump(mode="python"),
        },
        default=json_default,
        sort_keys=True,
    )
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    log.write_text(json.dumps({"checksum": checksum, "payload": payload}, sort_keys=True) + "\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-ord"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_resume_absent_pause_aborts(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)
    outcome = substrate.attempt_resume(_resume_attempt("never-paused"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_has_pause_record_is_presence_not_validity(tmp_path: Path) -> None:
    """`has_pause_record` reports PRESENCE — a pure read of "is a record on disk",
    NOT whether resume would succeed. Absent → False; a captured pause → True; and
    a present-but-CORRUPT segment (tampered checksum, so `attempt_resume` ABORTs)
    STILL returns True. This presence-not-validity contract is what lets the driver
    fire `attempt_resume` on a corrupt snapshot (recording the abort + failing
    closed) instead of misreading it as absent and silently skipping the resume."""
    substrate = _substrate(tmp_path)
    # Absent workflow → no record.
    assert substrate.has_pause_record(WorkflowID("wf-none")) is False

    # A captured (valid) pause → present.
    substrate.capture_pause_snapshot(WorkflowID("wf-rec"), PauseReason.ENGINE_NATIVE_PAUSE)
    assert substrate.has_pause_record(WorkflowID("wf-rec")) is True

    # Corrupt the only segment so `attempt_resume` ABORTs — yet the record is STILL
    # present on disk (presence ≠ validity).
    log = substrate._journal_file(WorkflowID("wf-rec"))  # type: ignore[attr-defined]
    record = json.loads(log.read_text().splitlines()[0])
    record["checksum"] = "0" * 64
    log.write_text(json.dumps(record, sort_keys=True) + "\n")
    assert (
        substrate.attempt_resume(_resume_attempt("wf-rec")).outcome_kind
        is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED
    )
    assert substrate.has_pause_record(WorkflowID("wf-rec")) is True


def test_corrupt_segment_does_not_block_other_workflow(tmp_path: Path) -> None:
    """Per-workflow files isolate corruption: a bad log never blocks another workflow."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-good"), PauseReason.OPERATOR_INITIATED_PAUSE)

    other = substrate._journal_file(WorkflowID("wf-other"))  # type: ignore[attr-defined]
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not even a wal frame at all\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-good"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN


# --- inherited behavior still holds (material diff / Protocol conformance) ---


def test_material_diff_revalidates(tmp_path: Path) -> None:
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


def test_conforms_to_protocol_via_bound_free_functions(tmp_path: Path) -> None:
    """Pin the intended use: bound as the engine substrate, driven via the
    module-level C-CP-22 free functions (how RuntimeEngineRecoveryLoop drives it).
    Passing to the ``EnginePauseResumeSubstrate``-typed binder also gives pyright a
    typed context that verifies Protocol conformance."""
    substrate = _substrate(tmp_path)
    with bind_engine_pause_resume_substrate(substrate):
        event = capture_pause_snapshot(WorkflowID("wf-bound"), PauseReason.ENGINE_NATIVE_PAUSE)
        outcome = attempt_resume(_resume_attempt("wf-bound"))

    assert event.pause_reason is PauseReason.ENGINE_NATIVE_PAUSE
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN


def test_segment_log_dir_property_exposes_directory(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)
    assert substrate.segment_log_dir == tmp_path


def _make_pause_event() -> PauseEvent:
    from harness_core import EntryID

    summary = _state_summary("ord")
    return PauseEvent(
        paused_at="2026-06-15T12:00:00Z",
        pause_reason=PauseReason.ENGINE_NATIVE_PAUSE,
        state_summary_snapshot=summary,
        external_refs_captured=summary.external_references,
        pause_audit_entry_id=EntryID("ord-entry"),
    )
