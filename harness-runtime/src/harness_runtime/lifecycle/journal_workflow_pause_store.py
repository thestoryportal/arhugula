"""Durable filesystem-journal store for workflow-layer ``PauseSnapshot``s.

R-CC-1 arc #3 cascade step 2 (`.harness/r-cc-1-arc-3-workflow-durable-resume-design-v1.md`
§7b). The **harness-owned** durable persistence that makes a DURABLE_ASYNC
workflow-layer pause survivable across a process restart — so the caller need
NOT persist the ``PauseSnapshot`` itself (cascade step 1 / #513 surfaced the
public ``api.resume`` with a *caller-supplied* snapshot; this owns the
durability).

**Reused-by-pattern from #475, NOT bound.** This applies the *crash-survivable
journal mechanism* proven + tested in ``JournalEnginePauseResumeSubstrate``
(``journal_pause_resume_substrate.py``) — per-workflow JSONL file, ``fsync`` +
directory-fsync, latest-record semantics, fail-closed-on-corruption — to the
**workflow-layer** ``PauseSnapshot`` (CP spec v1.11 §26.2) rather than the
engine-layer ``PauseEvent``. The engine-layer #475 substrate stays the ratified
CXA-2 bounded-residual (line 181), untouched; this is its workflow-layer sibling
where a *real producer* (the DURABLE_ASYNC HITL / EXPLICIT_OPERATOR pause at
``workflow_driver.py:795/:951``) and a *real caller* (``api.resume``) exist.

**One journal file per workflow.** Each workflow's pauses append to a dedicated
``<journal_dir>/<sha256(workflow_id)>.jsonl`` file (mirrors #475). The last line
of a workflow's file is its authoritative latest pause; per-workflow files
isolate workflows (a corrupt record for one workflow cannot block resuming
another). The resume handle is the ``workflow_id`` (the identifier the caller
always knows after a crash — the fresh-uuid ``run_id`` of a lost ``RunResult``
is not knowable post-crash; design §7b keying decision). The ``run_id`` is
carried *inside* the persisted snapshot for audit continuity, and resume
correctness is preserved by the ``api.resume`` detect-then-refuse guards
(``workflow_id`` match + ``step_index`` range) + the ``snapshot_hash``
validation in ``attempt_resume``. Track-A serial bootstrap-per-call (``_run_lock``)
bounds this to ≤1 active pause per workflow per process.

**Fails closed.** A crash *during* an append can leave a torn trailing line.
``read_latest`` reads the **latest** record only; if that record is malformed
(bad JSON / invalid UTF-8 / a ``PauseSnapshot`` that no longer validates / a
mismatched ``workflow_id``), it returns ``None`` rather than silently resuming an
older (stale) snapshot or raising — the ``api.resume`` caller surfaces this as
``RT-FAIL-RESUME-HANDLE-UNKNOWN``.

**Residence (D2-bis, design §7b).** The journal directory co-locates under the
bootstrap-resolved ``STATE_LEDGER`` directory as a ``pause-journal/`` subdir.
This is harness-internal *recovery substrate* (like #475's engine journal), NOT
one of the four canonical *artifact* classes the ``PathClass`` registry
enumerates (C-IS-01 §1 "Four canonical artifact classes"), so it needs no new
``PathClass`` (IS-AL-1 forecloses inventing a canonical artifact class, not
every internal file). Co-location is restart-deterministic via ``PathResolver``,
glob-safe (nothing globs the ``STATE_LEDGER`` dir; the ledger opens the specific
file ``state.jsonl``), and visibility-coherent (inherits operator-during-run +
maintainer-post-run readability), sitting beside the ``cp.pause-captured``
integrity anchor that already writes there.

**Anchor-validation-deferred (U-CP-22).** A fresh-bootstrap resume has a fresh
ledger; the MVP ``pause_context_reader`` returns the constant sentinel
(``"0"*64``) → no material diff → STRICT admits. Position-only resume is correct
*because* the execution model is data-stateless between steps (design §1.1); the
``state_ledger_anchor`` reachability check stays diff-detection-fidelity-only,
NOT a resume-correctness hole. Real anchor-reachability validation is the
deferred U-CP-22 arc.

Authority: runtime spec v1.46 (R-CC-1 arc #3 cascade step 2); CP spec v1.11
§26.2 (``PauseSnapshot``); design ``r-cc-1-arc-3-workflow-durable-resume-design-v1.md``
§7b. Mechanism mirrors ``JournalEnginePauseResumeSubstrate`` (#475).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

from harness_cp.pause_resume_protocol_types import PauseSnapshot
from pydantic import ValidationError

__all__ = [
    "PAUSE_JOURNAL_SUBDIR",
    "JournalWorkflowPauseStore",
    "pause_journal_dir_for",
]

#: Subdirectory under the resolved ``STATE_LEDGER`` directory that holds the
#: per-workflow pause journals (design §7b D2-bis co-location).
PAUSE_JOURNAL_SUBDIR = "pause-journal"


def pause_journal_dir_for(state_ledger_dir: Path) -> Path:
    """Return the pause-journal directory co-located under the STATE_LEDGER dir.

    The single source of truth for the journal directory location, consumed by
    BOTH the stage-5 factory (which derives ``state_ledger_dir`` from
    ``ctx.ledger_writer.handle.canonical_path.parent``) and ``api.resume`` (which
    resolves it from ``config`` via ``PathResolver``) — both compute the same
    ``<state_ledger_dir>/pause-journal`` path for the same
    ``(workflow_class, deployment_surface)``, so a capture-side write and a
    resume-side read over a fresh bootstrap find the same file (restart-survival).
    """
    return state_ledger_dir / PAUSE_JOURNAL_SUBDIR


class JournalWorkflowPauseStore:
    """Durable per-workflow ``PauseSnapshot`` journal (workflow-layer F2/JOURNAL).

    ``capture`` appends the captured ``PauseSnapshot`` as one JSON line to the
    workflow's ``<journal_dir>/<sha256(workflow_id)>.jsonl`` file (durably,
    ``fsync``-ed); ``read_latest`` reads that file's **latest** line and
    re-validates it into a ``PauseSnapshot``. Because the journal is on disk, a
    fresh store over the same directory (a new process after a restart) resumes a
    pause captured by a prior process.
    """

    def __init__(self, *, journal_dir: Path) -> None:
        self._journal_dir = Path(journal_dir)

    def capture(self, snapshot: PauseSnapshot) -> None:
        """Append one ``PauseSnapshot`` record to the workflow's file, durably."""
        self._append(snapshot)

    def read_latest(self, workflow_id: str) -> PauseSnapshot | None:
        """Return the workflow's latest journaled ``PauseSnapshot``, or ``None``.

        ``None`` when the workflow has no journal file OR its latest record is
        unparseable (fail closed). Only the last record is consulted: a torn
        latest append must NOT silently resume an older snapshot.
        """
        path = self._journal_file(workflow_id)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A read-level failure (incl. an invalid-UTF-8 byte that would
            # otherwise be silently replaced) fails closed rather than crashing
            # or resuming altered state.
            return None
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        return self._parse_snapshot(lines[-1], workflow_id)

    # -- durable journal I/O ------------------------------------------------

    def _journal_file(self, workflow_id: str) -> Path:
        """The per-workflow journal file (filesystem-safe, collision-free name)."""
        digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
        return self._journal_dir / f"{digest}.jsonl"

    def _append(self, snapshot: PauseSnapshot) -> None:
        """Append one JSONL record to the workflow's file, durably.

        The record is ``fsync``-ed to stable storage before returning so a host
        crash / power loss immediately after ``capture`` cannot lose an
        already-accepted pause. Two directory-fsyncs persist the new dirents
        (best-effort — POSIX) so the journal survives a first-capture crash:
        when the journal *file* is created, its directory entry is fsync-ed; and
        when the ``pause-journal`` *directory* itself is created (the very first
        durable pause), its parent (the ``STATE_LEDGER`` dir) is also fsync-ed —
        otherwise fsyncing only the new child dir persists the file entry inside
        it but NOT the child dir's own entry in its parent, so a crash could lose
        the entire ``pause-journal`` directory (→ a spurious
        ``RT-FAIL-RESUME-HANDLE-UNKNOWN`` despite the durability guarantee).

        **Torn-append self-healing.** A crash *during* a prior append can leave a
        partial trailing line with no terminating newline. To prevent the next
        append from concatenating onto that fragment (which would make the latest
        line `fragment+record` → unparseable → ``read_latest`` returns ``None``
        *permanently* until manual repair), a leading newline is written first
        when the existing file is non-empty and does not already end with ``\\n``.
        The torn fragment then becomes its own (ignored, non-latest) line and the
        new record is the clean latest line.

        Both durability hardenings caught by out-of-family Codex review
        (R-CC-1 arc #3 cascade step 2).
        """
        record = {
            "workflow_id": snapshot.workflow_id,
            "pause_snapshot": snapshot.model_dump(mode="json"),
        }
        line = json.dumps(record, sort_keys=True)
        path = self._journal_file(snapshot.workflow_id)
        journal_dir = path.parent
        dir_is_new = not journal_dir.exists()
        journal_dir.mkdir(parents=True, exist_ok=True)
        is_new_file = not path.exists()
        needs_leading_newline = (not is_new_file) and self._last_byte_is_not_newline(path)
        with path.open("a", encoding="utf-8") as handle:
            if needs_leading_newline:
                handle.write("\n")
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new_file:
            self._fsync_dir(journal_dir)
        if dir_is_new:
            # Durably link the freshly-created `pause-journal` dirent into its
            # parent (the STATE_LEDGER dir), else a crash could lose the dir.
            self._fsync_dir(journal_dir.parent)

    @staticmethod
    def _last_byte_is_not_newline(path: Path) -> bool:
        """Return ``True`` iff the file's last byte is not ``\\n`` (a torn trailing
        append). Reads only the final byte; on any read error, conservatively
        returns ``True`` so the next append is newline-separated rather than
        risking a concatenation onto a fragment."""
        try:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except OSError:
            return True

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

    @staticmethod
    def _parse_snapshot(line: str, expected_workflow_id: str) -> PauseSnapshot | None:
        """Parse one journal line into a ``PauseSnapshot``, or ``None`` if corrupt.

        Guards against the record belonging to an unexpected workflow as a
        defensive integrity check (per-workflow files make this near-impossible,
        but a mismatched record is treated as corruption — fail closed).
        """
        try:
            loaded = json.loads(line)
            if not isinstance(loaded, dict):
                return None
            record = cast("dict[str, object]", loaded)
            if record.get("workflow_id") != expected_workflow_id:
                return None
            return PauseSnapshot.model_validate(record["pause_snapshot"])
        except (ValueError, ValidationError, KeyError, TypeError):
            # ValueError covers json.JSONDecodeError; any value-level failure
            # means a corrupt record → fail closed.
            return None
