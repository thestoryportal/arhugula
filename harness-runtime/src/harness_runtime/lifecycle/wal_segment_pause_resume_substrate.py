"""Durable WAL segment-log engine pause/resume substrate (R-FS-1 E-2 / U-RT-121).

A hand-rolled (I-6 — no vendored Kafka/WAL framework) append-only **WAL
segment-log** ``EnginePauseResumeSubstrate`` for the ``WAL_SEGMENT`` engine class
(C-CP-07 §7.1 row 5: "lifecycle = Harness; append-only segment log with
per-segment resume"). It **extends the proven #475
``JournalEnginePauseResumeSubstrate``** (the real durable filesystem-journal
substrate, append-JSONL / fsync / fail-closed) into a per-segment WAL: each
captured ``PauseEvent`` is written as a **checksummed, monotonically-indexed
segment record**, and replay recovers the **contiguous valid segment prefix**.

**What WAL_SEGMENT adds over the #475 JOURNAL substrate** (the load-bearing
distinction, not a cosmetic swap):

- **Per-segment framing.** Each record carries a ``segment_index`` (the append
  ordinal, monotonic per workflow) + a SHA-256 ``checksum`` over the exact
  serialized payload. The index makes this a genuine *segment log*, not a flat
  journal; the checksum makes torn writes detectable per segment.
- **Torn-write detection + WAL recovery semantics.** ``attempt_resume`` replays
  the **contiguous valid prefix**, stopping at the FIRST record whose checksum /
  ordering / parse fails. A half-written trailing segment (a crash mid-append:
  truncated line, partial JSON, bad checksum) is **discarded** and replay
  recovers to the last *committed* segment.

  This **deliberately diverges** from the #475 base's "read the latest record
  only; fail closed (``ABORT_SNAPSHOT_CORRUPTED``) if it is torn" rule. The
  divergence is safe — and is the standard WAL recovery property — because a torn
  tail is an **uncommitted** write: the segment was never ``fsync``-acknowledged,
  so recovering to the last committed segment loses nothing the caller was told
  was durable. (The base's stricter rule guards a *different* hazard — never
  silently resuming an older *snapshot* in a single-record-per-workflow store;
  in a multi-segment log the last *valid* segment IS the authoritative latest
  committed state, so resuming it is correct, not stale.)

  A corrupt **middle** segment is *not* skipped — the prefix scan stops at the
  gap, so replay never resumes *past* a corruption (gap-safe; recovering past a
  hole would silently drop committed-then-lost segments).
- **Per-segment dedup is the idempotent-read property + the CP-side F2 join.**
  ``attempt_resume`` is a pure read (no mutation), so repeated calls return the
  same outcome; the CP driver's segment-prefix ``resume_at`` (U-CP-94, the F2
  ``idempotency_key`` join, C-CP-08 §8.2 row 5) is where step-level dedup lives.
  This substrate does not "apply" segments, so it grows no speculative
  replay-applier API (that would be built-but-vacuous).

Drop-in for ``RuntimeEngineRecoveryLoop`` (implements the same C-CP-22
``EnginePauseResumeSubstrate`` Protocol: ``capture_pause_snapshot`` /
``attempt_resume``). PathClass placement: the on-disk segment log maps to the
existing closed-enum ``PathClass.STATE_LEDGER`` member — IS-AL-1-clean, no IS
extension.

Authority: C-CP-07 §7.1 row 5 + §7.4 ("specific WAL implementation" deferred to
impl-discretion); C-CP-08 §8.1 ``segment_replay`` + §8.2 row 5;
``.harness/r-fs-1-e-engine-classes-design-v1.md`` §4.2 (extend #475 via a REAL
driver); ``.harness/r-fs-1-e-plan-decomposition.md`` §2 (U-RT-121). WAL torn-write
+ fsync hazards: ``Pattern_Reference_Catalog_v1.0.md`` WAL cluster (cluster-4).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

from harness_core import WorkflowID
from harness_cp.pause_resume_protocol import PauseEvent
from pydantic import ValidationError

from harness_runtime.lifecycle.journal_pause_resume_substrate import (
    JournalEnginePauseResumeSubstrate,
    json_default,
    json_object_hook,
)

__all__ = ["WALSegmentEnginePauseResumeSubstrate"]


class WALSegmentEnginePauseResumeSubstrate(JournalEnginePauseResumeSubstrate):
    """Durable WAL segment-log ``EnginePauseResumeSubstrate`` (extends #475).

    ``capture_pause_snapshot`` appends one checksummed, ``segment_index``-stamped
    segment record (durably ``fsync``-ed) to the workflow's segment-log file;
    ``attempt_resume`` replays the contiguous valid segment prefix and resumes
    from the last committed segment, discarding a torn trailing segment. Because
    the log is on disk, a fresh instance over the same directory resumes a pause
    captured by a prior process (the #475 durability property, extended
    per-segment).

    ``segment_log_dir`` is the durable directory (aliases the base's
    ``journal_dir``); the injected providers (``state_summary_provider`` required,
    plus optional ``diff_provider`` / ``revalidation_succeeded`` /
    ``pause_audit_entry_id_provider`` / ``resume_audit_entry_id_provider``) mirror
    the base so this is a drop-in durable replacement.
    """

    @property
    def segment_log_dir(self) -> Path:
        """The durable segment-log directory (the base's ``journal_dir``)."""
        return self._journal_dir

    # -- durable per-segment WAL I/O (overrides the base JSONL primitives) ----

    @staticmethod
    def _canonical_payload(workflow_id: WorkflowID, segment_index: int, event: PauseEvent) -> str:
        """The exact serialized segment payload the checksum is computed over."""
        return json.dumps(
            {
                "workflow_id": str(workflow_id),
                "segment_index": segment_index,
                "pause_event": event.model_dump(mode="python"),
            },
            default=json_default,
            sort_keys=True,
        )

    def _valid_prefix(self, workflow_id: WorkflowID) -> tuple[list[PauseEvent], int]:
        """Scan the contiguous valid segment prefix → (events, byte offset of its end).

        Byte-robust (reads bytes, decodes each newline-terminated chunk
        individually) so an invalid-UTF-8 or partial trailing segment ends the
        prefix without crashing — it does NOT read the whole file as UTF-8. A
        *complete* segment is a ``b"...\\n"`` chunk that decodes, is non-blank,
        passes checksum, matches its expected ``segment_index`` (= position), and
        validates as a ``PauseEvent``. The scan stops at:

        - a **torn tail** (trailing bytes with no final ``\\n``) — an incomplete,
          un-``fsync``-acknowledged write;
        - a **corrupt segment** (bad checksum / decode / ordering / validation) —
          replay must never resume *past* a gap (gap-safe).

        The returned byte offset is where the valid prefix ends — the truncation
        point that discards any torn/garbage tail before the next append, and the
        boundary `attempt_resume` resumes from (the last list element).
        """
        path = self._journal_file(workflow_id)
        if not path.exists():
            return [], 0
        try:
            raw = path.read_bytes()
        except OSError:
            return [], 0
        events: list[PauseEvent] = []
        offset = 0
        index = 0
        total = len(raw)
        while offset < total:
            newline = raw.find(b"\n", offset)
            if newline == -1:
                break  # torn tail: no trailing newline → incomplete write
            chunk = raw[offset:newline]
            try:
                line = chunk.decode("utf-8")
            except UnicodeDecodeError:
                break  # invalid UTF-8 in this segment → corruption, stop the prefix
            if not line.strip():
                break  # blank line ends the valid prefix (defensive)
            parsed = self._parse_segment(line, str(workflow_id), index)
            if parsed is None:
                break  # first corruption ends the contiguous valid prefix
            events.append(parsed)
            offset = newline + 1  # include the newline in the committed extent
            index += 1
        return events, offset

    def _append(self, workflow_id: WorkflowID, event: PauseEvent) -> None:
        """Append one checksummed WAL segment record to the workflow's log, durably.

        **WAL recovery-on-open:** before appending, truncate any torn/garbage tail
        back to the contiguous valid segment prefix (Codex [P1]). Otherwise a
        crash that left a partial trailing segment (no final ``\\n``) would cause
        the next append to concatenate onto the torn bytes, permanently corrupting
        replay — the exact crash scenario this substrate must tolerate. The new
        ``segment_index`` is the count of valid prefix segments, so it stays
        monotonic over *committed* segments (the torn write is discarded, not
        counted).

        Write-ahead durability: the record is ``fsync``-ed to stable storage
        before returning; a new file's directory entry is also ``fsync``-ed
        (best-effort, POSIX) so the file itself survives a crash.
        """
        path = self._journal_file(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix, valid_extent = self._valid_prefix(workflow_id)
        segment_index = len(prefix)
        if path.exists() and path.stat().st_size != valid_extent:
            # A torn/garbage tail extends beyond the valid prefix — discard it
            # (and fsync the truncation) before the new segment lands cleanly.
            with path.open("r+b") as handle:
                handle.truncate(valid_extent)
                handle.flush()
                os.fsync(handle.fileno())
        payload = self._canonical_payload(workflow_id, segment_index, event)
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        record = json.dumps({"checksum": checksum, "payload": payload}, sort_keys=True)
        is_new_file = not path.exists()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new_file:
            self._fsync_dir(path.parent)

    def _read_latest(self, workflow_id: WorkflowID) -> PauseEvent | None:
        """Return the last committed segment's ``PauseEvent``, or ``None``.

        Replays the **contiguous valid prefix** of the segment log (a torn tail is
        discarded; a corrupt middle segment is NOT skipped — replay never resumes
        past a gap). ``None`` when the workflow has no log OR not even its first
        segment is valid (→ ``ABORT_SNAPSHOT_CORRUPTED`` via the inherited
        ``attempt_resume``). See the module docstring for the WAL-recovery
        rationale vs the #475 base's stricter latest-record rule.
        """
        prefix, _ = self._valid_prefix(workflow_id)
        return prefix[-1] if prefix else None

    @staticmethod
    def _parse_segment(
        line: str, expected_workflow_id: str, expected_index: int
    ) -> PauseEvent | None:
        """Parse + integrity-check one segment record; ``None`` if invalid.

        Validates, in order: outer JSON shape (``checksum`` + ``payload`` string);
        SHA-256 checksum over the exact payload bytes; payload JSON shape;
        ``workflow_id`` match; ``segment_index`` == position (ordering integrity);
        ``PauseEvent`` validation. Any failure → ``None`` (fail closed).
        """
        try:
            outer = json.loads(line)
            if not isinstance(outer, dict):
                return None
            frame = cast("dict[str, object]", outer)
            checksum = frame.get("checksum")
            payload = frame.get("payload")
            if not isinstance(checksum, str) or not isinstance(payload, str):
                return None
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != checksum:
                return None
            loaded = json.loads(payload, object_hook=json_object_hook)
            if not isinstance(loaded, dict):
                return None
            record = cast("dict[str, object]", loaded)
            if record.get("workflow_id") != expected_workflow_id:
                return None
            if record.get("segment_index") != expected_index:
                return None
            return PauseEvent.model_validate(record["pause_event"])
        except (ValueError, ValidationError, KeyError, TypeError):
            return None
