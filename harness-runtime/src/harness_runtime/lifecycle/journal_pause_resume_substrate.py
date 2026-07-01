"""Durable filesystem-journal engine pause/resume substrate (R-CXA-2, S3).

A crash-survivable implementation of the harness-cp ``EnginePauseResumeSubstrate``
Protocol for the **``PURE_PATTERN_NO_ENGINE`` / ``JOURNAL_RESUME``** engine class
(C-CP-07/08) — the one recovery class the harness owns end-to-end with **zero
external framework** per the R-CXA-2 post-MVP brief §3.2.

Where the ``DeterministicEnginePauseResumeSubstrate`` (harness-cp) stores captured
``PauseEvent``s in an in-memory dict — a test fixture that does **not** survive a
process crash — this substrate persists them to a **filesystem journal** (the
brief's "F2 = filesystem-journal + state-ledger + idempotency-key", §3.2/§3.5).
On resume, a *fresh* substrate instance (a new process after a crash) reads the
on-disk journal keyed by ``paused_workflow_id`` and re-derives the ``PauseEvent``,
so pause state genuinely persists across the process boundary (brief §3.8
``test_recovery_state_persists_across_process``).

**One journal file per workflow.** Each workflow's pauses are appended to a
dedicated ``<journal_dir>/<sha256(workflow_id)>.jsonl`` file. This makes the
**last line of a workflow's file its authoritative latest pause**, and it
isolates workflows: a corrupt record for one workflow can never block resuming
another, and a torn latest append fails the *right* workflow closed.

**Fails closed (C-CP-22 resume semantics).** A crash *during* an append can
leave a torn trailing line. ``attempt_resume`` reads the **latest** record only;
if that record is malformed (bad JSON / missing keys / a ``PauseEvent`` that no
longer validates), resume returns ``ABORT_SNAPSHOT_CORRUPTED`` rather than
silently resuming an older (stale) snapshot or raising.

**Boundaries (faithful to the ratified design):**

- The IS state ledger is a 6-field hash-chained *integrity anchor*, not a
  content store — it cannot rehydrate a ``PauseEvent`` (it holds a ``response_hash``,
  not the body) nor key by ``workflow_id``. So the durable *content* lives in this
  filesystem journal; the ``cp.pause-captured`` / ``cp.resume-attempted`` ledger
  entries the ``RuntimeEngineRecoveryLoop`` emits remain the separate integrity
  anchors. Snapshot serialization format is impl-discretion per C-CP-22 §22.1
  acceptance #9.
- This substrate is the harness-owned F2 path only. The
  ``EVENT_SOURCED_REPLAY`` / ``RECONCILER_LOOP`` / ``WAL_SEGMENT`` classes bind
  external engines (Temporal / K8s / Kafka) and compose via deployment-time
  adapters — they are NOT vendored here (I-6 framework-pull discipline; brief
  §3.2).
- Building this substrate does **not** retire H_T-CXA-2: the engine recovery
  loop has no production *driver* at MVP, so the primitive stays dormant and
  CXA-2 remains a counted bounded-residual. This is the durable *capability*,
  ready for when a real recovery driver lands.

Authority: C-CP-22 §22.1 (engine-layer pause/resume free functions);
C-CP-07/08 (EngineClass/ResumptionKind); R-CXA-2 post-MVP brief §3.

**Serialization.** The ``PauseEvent`` is dumped via Pydantic (``mode="python"``)
and JSON-encoded with arbitrary ``bytes`` (the optional
``ExternalReference.snapshot_capture_at_pause`` anchor) base64-wrapped, so
non-UTF-8 snapshot anchors round-trip losslessly rather than crashing capture.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness_core import EntryID, WorkflowID
from harness_cp.handoff_context import StateSummary
from harness_cp.material_diff_detection import MaterialDiff
from harness_cp.pause_resume_protocol import (
    EngineDiffProvider,
    EngineRevalidationPolicy,
    PauseEvent,
    PauseReason,
    ResumeAttempt,
    ResumeOutcome,
    ResumeOutcomeKind,
    classify_resume,
)
from pydantic import ValidationError

__all__ = ["JournalEnginePauseResumeSubstrate", "json_default", "json_object_hook"]

#: Per-entry record separator for the default ``pause_audit_entry_id`` digest
#: (mirrors ``DeterministicEnginePauseResumeSubstrate``).
_RECORD_SEPARATOR = b"\x1e"

#: Sentinel wrapper marking a base64-encoded ``bytes`` value in a journal record.
_BYTES_TAG = "__bytes_b64__"


def _empty_engine_diff(_event: PauseEvent, _attempt: ResumeAttempt) -> tuple[MaterialDiff, ...]:
    return ()


def _engine_revalidation_succeeds(_attempt: ResumeAttempt, _diff: tuple[MaterialDiff, ...]) -> bool:
    return True


def json_default(value: object) -> object:
    """JSON-encode ``bytes`` (e.g. a snapshot anchor) as a base64 sentinel object."""
    if isinstance(value, bytes):
        return {_BYTES_TAG: base64.b64encode(value).decode("ascii")}
    raise TypeError(f"unserializable journal value of type {type(value)!r}")


def json_object_hook(obj: dict[str, object]) -> object:
    """Reconstruct ``bytes`` from a base64 sentinel object on read.

    ``validate=True`` makes malformed base64 raise ``binascii.Error`` (a
    ``ValueError``) instead of silently dropping characters — so a corrupt
    sentinel is caught by the resume parser's fail-closed guard rather than
    producing altered bytes.
    """
    if len(obj) == 1 and _BYTES_TAG in obj:
        encoded = obj[_BYTES_TAG]
        if isinstance(encoded, str):
            return base64.b64decode(encoded, validate=True)
    return obj


class JournalEnginePauseResumeSubstrate:
    """Durable filesystem-journal ``EnginePauseResumeSubstrate`` (F2/JOURNAL_RESUME).

    ``capture_pause_snapshot`` appends the captured ``PauseEvent`` as one JSON line
    to the workflow's ``<journal_dir>/<sha256(workflow_id)>.jsonl`` file;
    ``attempt_resume`` reads that file's **latest** line and classifies the resume
    outcome exactly as the deterministic substrate does. Because the journal is on
    disk, a fresh instance over the same directory resumes a pause captured by a
    prior process.

    The injected providers mirror ``DeterministicEnginePauseResumeSubstrate`` so
    this is a drop-in durable replacement: ``state_summary_provider`` (required),
    plus optional ``diff_provider`` / ``revalidation_succeeded`` /
    ``pause_audit_entry_id_provider`` / ``resume_audit_entry_id_provider``.
    """

    def __init__(
        self,
        *,
        journal_dir: Path,
        state_summary_provider: Callable[[], StateSummary],
        diff_provider: EngineDiffProvider | None = None,
        revalidation_succeeded: EngineRevalidationPolicy | None = None,
        pause_audit_entry_id_provider: Callable[[WorkflowID, PauseReason], EntryID] | None = None,
        resume_audit_entry_id_provider: (
            Callable[[ResumeAttempt, ResumeOutcomeKind], EntryID | None] | None
        ) = None,
    ) -> None:
        self._journal_dir = Path(journal_dir)
        self._state_summary_provider = state_summary_provider
        self._diff_provider = diff_provider or _empty_engine_diff
        self._revalidation_succeeded = revalidation_succeeded or _engine_revalidation_succeeds
        self._pause_audit_entry_id_provider = pause_audit_entry_id_provider
        self._resume_audit_entry_id_provider = resume_audit_entry_id_provider

    def capture_pause_snapshot(
        self, workflow_id: WorkflowID, pause_reason: PauseReason
    ) -> PauseEvent:
        state_summary = self._state_summary_provider()
        pause_audit_entry_id = (
            self._pause_audit_entry_id_provider(workflow_id, pause_reason)
            if self._pause_audit_entry_id_provider is not None
            else EntryID(
                hashlib.sha256(
                    _RECORD_SEPARATOR.join(
                        (
                            str(workflow_id).encode("utf-8"),
                            pause_reason.value.encode("utf-8"),
                            state_summary.summary_hash.encode("utf-8"),
                        )
                    )
                ).hexdigest()
            )
        )
        event = PauseEvent(
            paused_at=datetime.now(UTC).isoformat(),
            pause_reason=pause_reason,
            state_summary_snapshot=state_summary,
            external_refs_captured=state_summary.external_references,
            pause_audit_entry_id=pause_audit_entry_id,
        )
        self._append(workflow_id, event)
        return event

    def attempt_resume(self, attempt: ResumeAttempt) -> ResumeOutcome:
        event = self._read_latest(attempt.paused_workflow_id)
        if event is None:
            return ResumeOutcome(
                outcome_kind=ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED,
                material_diff=(),
                context_revalidated=False,
                resume_audit_entry_id=None,
            )
        diff = self._diff_provider(event, attempt)
        revalidated = self._revalidation_succeeded(attempt, diff)
        outcome_kind = classify_resume(diff, revalidation_succeeded=revalidated)
        resume_audit_entry_id = (
            self._resume_audit_entry_id_provider(attempt, outcome_kind)
            if self._resume_audit_entry_id_provider is not None
            else None
        )
        return ResumeOutcome(
            outcome_kind=outcome_kind,
            material_diff=diff,
            context_revalidated=(outcome_kind is ResumeOutcomeKind.RESUME_AFTER_REVALIDATION),
            resume_audit_entry_id=resume_audit_entry_id,
        )

    def has_pause_record(self, workflow_id: WorkflowID) -> bool:
        """Report whether a pause record EXISTS for ``workflow_id`` (presence, not validity).

        A pure, non-emitting peek: ``True`` iff the workflow's journal file exists
        and is non-empty. This is deliberately a **presence** check, NOT a validity
        check — a torn/corrupt latest record still returns ``True`` here so the
        driver fires ``attempt_resume`` (which then classifies it
        ``ABORT_SNAPSHOT_CORRUPTED`` and fails closed). Conflating presence with
        validity would silently skip the resume attempt for a corrupt snapshot and
        lose the abort record — the failure mode this method exists to prevent.
        """
        path = self._journal_file(workflow_id)
        return path.exists() and path.stat().st_size > 0

    # -- durable journal I/O ------------------------------------------------

    def _journal_file(self, workflow_id: WorkflowID) -> Path:
        """The per-workflow journal file (filesystem-safe, collision-free name)."""
        digest = hashlib.sha256(str(workflow_id).encode("utf-8")).hexdigest()
        return self._journal_dir / f"{digest}.jsonl"

    def _append(self, workflow_id: WorkflowID, event: PauseEvent) -> None:
        """Append one JSONL pause record to the workflow's file, durably.

        The record is ``fsync``-ed to stable storage before returning so a host
        crash / power loss immediately after ``capture_pause_snapshot`` cannot
        lose an already-accepted pause. When the journal file is created, the
        directory entry is also fsync-ed (best-effort — POSIX) so the new file
        itself survives the crash.
        """
        record = {
            "workflow_id": str(workflow_id),
            "pause_event": event.model_dump(mode="python"),
        }
        line = json.dumps(record, default=json_default, sort_keys=True)
        path = self._journal_file(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not path.exists()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new_file:
            self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """fsync a directory so a freshly-created file's dirent is durable.

        Best-effort: directory fsync is unsupported on some platforms/filesystems
        (e.g. Windows), where it is a no-op rather than a failure.
        """
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    def _read_latest(self, workflow_id: WorkflowID) -> PauseEvent | None:
        """Return the workflow's **latest** journaled ``PauseEvent``, or ``None``.

        ``None`` when the workflow has no journal file OR its latest record is
        unparseable (fail closed → ``ABORT_SNAPSHOT_CORRUPTED``). Only the last
        record is consulted: a torn latest append must NOT silently resume an
        older snapshot (C-CP-22 latest-record semantics).

        Reads defensively + **strictly**: a journal file with any invalid UTF-8
        byte is treated as corruption (``UnicodeDecodeError`` → fail closed),
        not silently repaired with replacement characters — otherwise an
        invalid byte inside an otherwise-valid JSON string could resume altered
        state. A read-level ``OSError`` also fails closed rather than crashing.
        """
        path = self._journal_file(workflow_id)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        return self._parse_pause_event(lines[-1], str(workflow_id))

    @staticmethod
    def _parse_pause_event(line: str, expected_workflow_id: str) -> PauseEvent | None:
        """Parse one journal line into a ``PauseEvent``, or ``None`` if corrupt.

        Guards against the record belonging to an unexpected workflow as a
        defensive integrity check (per-workflow files make this near-impossible,
        but a mismatched record is treated as corruption — fail closed).
        """
        try:
            loaded = json.loads(line, object_hook=json_object_hook)
            if not isinstance(loaded, dict):
                return None
            record = cast("dict[str, object]", loaded)
            if record.get("workflow_id") != expected_workflow_id:
                return None
            return PauseEvent.model_validate(record["pause_event"])
        except (ValueError, ValidationError, KeyError, TypeError):
            # ValueError covers json.JSONDecodeError + binascii.Error (bad base64);
            # any value-level failure means a corrupt record → fail closed.
            return None
