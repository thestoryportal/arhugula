"""Durable etcd-style reconciler engine pause/resume substrate (R-FS-1 E-3 / U-RT-123).

A hand-rolled (I-6 — **no vendored K8s / etcd-operator**) level-triggered
read/diff/converge reconciler ``EnginePauseResumeSubstrate`` for the
``RECONCILER_LOOP`` engine class (C-CP-07 §7.1 row 4 / §7.4 floor (iii), v1_33
substrate-impl-discretion). It **extends the proven #475
``JournalEnginePauseResumeSubstrate``** (the real durable filesystem-journal
substrate: append-JSONL / fsync / fail-closed) into a
**resource-version-stamped converged-state store with a hand-rolled etcd-style
compare-and-swap (CAS) lease**, per the operator-ratified v1_33 §7.4
reconciliation note: *"a level-triggered, read/diff/converge reconcile loop with
a compare-and-swap lease over an own-format durable store, joined to the F2
state-ledger on ``idempotency_key``."*

**The CAS is on the revision, not an owner (the council-ratified correction).**
etcd's compare-and-swap compares ``mod_revision`` — it is *optimistic concurrency
on the resource revision*, NOT an owner-identity mutex. The prior owner-token
model was a category error: ``resume_request_actor`` defaults to one shared
``harness-runtime`` actor for every process, so two concurrent distinct
reconcilers carried the SAME token, both passed a same-owner re-entry check, and
both re-executed (a double-execution [P1]). The fix is the genuine primitive:
``attempt_resume`` claims the observed ``resource_version`` via an atomic
``O_EXCL``/``os.link`` create — the FIRST resume of a revision wins; ANY later
resume of the SAME revision loses (``ABORT_REVALIDATION_FAILED``; → §22.1). No
owner token.

**What RECONCILER_LOOP adds over the #475 JOURNAL / U-RT-121 WAL substrates:**

- **Resource-version stamping (the etcd revision analogue).** Each captured
  converged-state ``PauseEvent`` is a checksummed, monotonically-
  ``resource_version``-stamped record. The version makes this a genuine
  reconciler convergence log (each converge bumps the revision); the SHA-256
  checksum makes torn writes detectable per record.
- **Level-triggered converge replay + torn-write recovery + gap-safety**
  (shared with U-RT-121): ``attempt_resume`` re-derives the latest committed
  converged state (the highest valid ``resource_version`` = the last record of
  the contiguous valid prefix). A torn trailing record is discarded; a corrupt
  middle record stops the prefix scan (replay never resumes past a corruption).
- **Write-back-conditional CAS on ``resource_version`` (the genuine NEW floor-(iii)
  capability).** The concurrent-resume mitigation: two distinct reconcilers
  converging the same workflow at the same committed revision → exactly one wins
  the atomic claim and proceeds; the other ``ABORT_REVALIDATION_FAILED``.

**Lease backend (parameterize-and-assert; ``harness_runtime``-private).**

- ``LOCAL_SINGLE_HOST`` (default, zero-config): the common solo-dev ×
  local-development case. The per-workflow ``flock`` is the kernel-authoritative
  same-host serializer + liveness witness (released on process death); the
  ``os.link`` claim is atomic on a local filesystem. Safe and unconfigured.
- ``SHARED_STORE_CAS`` (multi-host over a shared volume): runs a one-shot
  startup atomicity assertion (``_assert_atomic_link_or_fail_closed``) and FAILS
  CLOSED (``RuntimeError``) if the backing store cannot even same-process back an
  atomic create-exclusive. **That probe is a SAME-HOST sanity check, NOT a
  cross-host CAS verifier** — a single process cannot establish that two distinct
  HOSTS racing ``os.link`` serialize (object-store FUSE / NFSv3-without-locking
  silently fail that). The real cross-host guarantee reduces to the operator's
  config-time declaration that ``reconcile_log_dir`` is on a store with atomic
  ``link``/``O_EXCL`` (NFSv4/lockd or a shared block volume).

**The honest limits (council + adversarial + Codex + advisor, 4-way converged;
ledger at ``.harness/council/u-rt-123-cas-lease/``). These are NOT silently
absorbed — they are committed FULL-SPEC build arcs (hand-rolled, I-6 preserved):**

- **F-1 — single-host crash-mid-resume + the zombie (CP-scope).** This substrate
  gates the resume at the CLAIM (upstream of the driver step-loop); a lost claim
  ``ABORT``s before any step re-executes. But a holder that WON the claim, then
  crashed (or GC-paused) mid-re-execution, is already inside the CP driver
  step-loop and does not re-enter ``attempt_resume``. AUTO-recovering that window
  needs the CP driver to hold the engine lock across the full suffix (or consult
  the engine generation per step-commit) — CP scope (U-CP-96/97). Until then a
  retry of an already-claimed revision ``ABORT``s → §22.1 HITL (fail-closed; never
  a double-execution).
- **F-2 — at-most-once EXECUTION of non-idempotent step side-effects.** The CAS
  guarantees at-most-once *claim of a revision*, not at-most-once *execution* of
  the workflow steps the resume re-runs (effect fires, THEN a stale claimant can
  fail). Exactly-once external effects need fencing at every side-effect sink.
- **F-CC — multi-host AUTO crash-recovery is distributed-systems-impossible**
  under {I-6 no-vendored-consensus ∧ no-unsafe-TTL}: safely reclaiming a maybe-
  dead cross-host holder's claim requires a death-detector (a vendored lease or a
  timeout). HITL-mediated multi-host recovery (fail-closed to §22.1) is the
  spec-faithful posture (C-CP-07 §7.4 / ADR-D1 §1.1 name the durable-store
  *mechanism*, not auto-vs-HITL). Genuine AUTO multi-host recovery is reachable
  hand-rolled only after F-2 (a fenced bounded-synchrony lease, fenced-safe at
  every sink) — a committed arc, carried to the already-deferred O-E3-2.

PathClass placement: the on-disk convergence log + claim files map to the
existing closed-enum ``PathClass.STATE_LEDGER`` member (IS-AL-1-clean, no IS
extension). A lost race maps onto the **closed** ``ResumeOutcomeKind`` enum
(X-AL-3-clean; no new primitive). The store-unverifiable fail-closed is a startup
``RuntimeError``, NOT a ``ResumeOutcome`` (it is a substrate-precondition, not a
resume-time revalidation failure).

Drop-in for ``RuntimeEngineRecoveryLoop`` (same C-CP-22 ``EnginePauseResumeSubstrate``
Protocol). Engine-class-aware binding is U-RT-124 (O-E3-1). The live-K8s e2e +
§7.2/ADR-D1 deployment-admissibility (O-E3-2/3) are separate downstream gates.

Authority: C-CP-07 §7.1 row 4 + §7.4 (v1_33 substrate-deferral, hand-rolled
etcd-style per I-6); C-CP-08 §8.1 ``reconciler_converge`` + §8.2 row 4;
``.harness/r-fs-1-e3-plan-decomposition.md`` §2 (U-RT-123);
``.harness/council/u-rt-123-cas-lease/DELIVERABLE.md`` (the council ratification).
Mirrors the ``WALSegmentEnginePauseResumeSubstrate`` (U-RT-121) structure.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import cast

from harness_core import EntryID, WorkflowID
from harness_cp.handoff_context import StateSummary
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

from harness_runtime.lifecycle.journal_pause_resume_substrate import (
    JournalEnginePauseResumeSubstrate,
    json_default,
    json_object_hook,
)

__all__ = ["LeaseBackend", "ReconcilerEnginePauseResumeSubstrate"]


class LeaseBackend(Enum):
    """The CAS-lease deployment backend (``harness_runtime``-private substrate config).

    Declares the operator's deployment intent so the substrate's shared-storage
    atomicity assumption is asserted-not-silently-assumed. Kept private to
    ``harness_runtime`` (it must NOT leak into ``harness_cp.pause_resume_protocol``
    or a cleared Protocol signature — it is a substrate choice within the v1_33
    §7.4 impl-discretion latitude, not a new contract / X-AL-3 primitive).
    """

    #: Single-host (the default; the solo-dev × local-development common case).
    #: flock is a genuine kernel-mediated single-writer + liveness witness; the
    #: local FS backs atomic os.link. Zero config; no startup probe.
    LOCAL_SINGLE_HOST = "local-single-host"
    #: Multi-host over a shared volume. Runs the startup atomicity assertion +
    #: fails closed if the store cannot back atomic create-exclusive.
    SHARED_STORE_CAS = "shared-store-cas"


class ReconcilerEnginePauseResumeSubstrate(JournalEnginePauseResumeSubstrate):
    """Durable etcd-style reconciler ``EnginePauseResumeSubstrate`` (extends #475).

    ``capture_pause_snapshot`` appends one checksummed, ``resource_version``-stamped
    converged-state record (durably ``fsync``-ed); ``attempt_resume`` replays the
    contiguous valid prefix to the last committed convergence, then performs a
    **write-back-conditional CAS on that ``resource_version``** (the etcd
    ``mod_revision`` analogue, atomic ``O_EXCL``/``os.link`` claim) — the first
    resume of the revision wins, a later resume of the same revision aborts
    (``ABORT_REVALIDATION_FAILED``). Because the log is on disk, a fresh instance
    over the same directory resumes a convergence captured by a prior process (the
    #475 durability property, extended per-revision with the CAS concurrent-resume
    mitigation).
    """

    def __init__(
        self,
        *,
        journal_dir: Path,
        state_summary_provider: Callable[[], StateSummary],
        diff_provider: EngineDiffProvider | None = None,
        revalidation_succeeded: EngineRevalidationPolicy | None = None,
        pause_audit_entry_id_provider: (Callable[[WorkflowID, PauseReason], EntryID] | None) = None,
        resume_audit_entry_id_provider: (
            Callable[[ResumeAttempt, ResumeOutcomeKind], EntryID | None] | None
        ) = None,
        lease_backend: LeaseBackend = LeaseBackend.LOCAL_SINGLE_HOST,
    ) -> None:
        super().__init__(
            journal_dir=journal_dir,
            state_summary_provider=state_summary_provider,
            diff_provider=diff_provider,
            revalidation_succeeded=revalidation_succeeded,
            pause_audit_entry_id_provider=pause_audit_entry_id_provider,
            resume_audit_entry_id_provider=resume_audit_entry_id_provider,
        )
        self._lease_backend = lease_backend
        if lease_backend is LeaseBackend.SHARED_STORE_CAS:
            self._assert_atomic_link_or_fail_closed()

    @property
    def reconcile_log_dir(self) -> Path:
        """The durable convergence-log directory (the base's ``journal_dir``)."""
        return self._journal_dir

    @property
    def lease_backend(self) -> LeaseBackend:
        """The configured CAS-lease deployment backend."""
        return self._lease_backend

    # -- startup atomicity assertion (shared-store-cas; fail-closed) ---------

    def _assert_atomic_link_or_fail_closed(self) -> None:
        """SAME-HOST sanity check that the backing store backs atomic ``os.link``.

        **NOT a cross-host CAS verifier.** A single-process probe cannot establish
        that two distinct HOSTS racing ``os.link`` on this store serialize — that is
        the actual ``shared-store-cas`` safety property, and it reduces to the
        operator's config-time declaration that ``reconcile_log_dir`` is on a store
        with atomic ``link``/``O_EXCL`` (NFSv4/lockd or a shared block volume). This
        probe only turns an obviously-unsupported primitive (e.g. some object-store
        FUSE mounts, NFSv3-without-locking) into a startup ``RuntimeError`` instead
        of a silently-unsafe lease. It fails CLOSED.
        """
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        # A uuid-unique probe TARGET per invocation: concurrent SHARED_STORE_CAS
        # startups over the same shared dir (the normal multi-host bring-up) must NOT
        # collide on a fixed probe name — a fixed target would make B's first link
        # hit A's probe (FileExistsError) and spuriously fail-closed on a store that
        # actually supports atomic link. The property is still tested by linking the
        # SAME tmp to the SAME unique target twice (second must raise FileExistsError).
        token = uuid.uuid4().hex
        probe = self._journal_dir / f".cas-atomicity-probe.{token}"
        tmp = self._journal_dir / f".cas-atomicity-probe.{token}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                os.link(tmp, probe)
            except OSError as exc:
                raise RuntimeError(
                    f"shared-store-cas backend: store at {self._journal_dir} does not "
                    f"support atomic os.link ({exc!r}); the cross-process CAS lease is "
                    "UNSAFE here. Use LOCAL_SINGLE_HOST or a POSIX-atomic shared store."
                ) from exc
            try:
                os.link(tmp, probe)
            except FileExistsError:
                return  # atomic create-exclusive holds same-process — sanity passed
            raise RuntimeError(
                f"shared-store-cas backend: store at {self._journal_dir} did NOT raise "
                "FileExistsError on a second os.link to an existing name — atomic "
                "create-exclusive is not enforced; the cross-process CAS lease is "
                "UNSAFE here. Use LOCAL_SINGLE_HOST or a store with atomic link "
                "(NFSv4/lockd or a shared block volume)."
            )
        finally:
            for leftover in (probe, tmp):
                try:
                    leftover.unlink()
                except OSError:
                    pass

    # -- per-workflow serialization (read-modify-write atomicity) -----------

    def _lock_file(self, workflow_id: WorkflowID) -> Path:
        """The per-workflow advisory-lock file (filesystem-safe, collision-free)."""
        digest = hashlib.sha256(str(workflow_id).encode("utf-8")).hexdigest()
        return self._journal_dir / f"{digest}.lock"

    @contextmanager
    def _workflow_lock(self, workflow_id: WorkflowID) -> Generator[None, None, None]:
        """Hold an exclusive per-workflow advisory ``flock`` for a critical section.

        Makes the substrate's read-modify-write operations atomic w.r.t. concurrent
        **same-host** operations on the same workflow: ``_append`` (read-prefix →
        assign ``resource_version`` → write) and the resume's read-prefix → CAS-claim
        both run under this lock, so (a) two concurrent captures cannot pick the same
        ``resource_version`` and clobber each other's replay, and (b) a converge
        cannot interleave between a resume's prefix read and its claim (so the CAS
        always claims the then-current head). Released promptly (the resume holds it
        only around read + claim, NOT around the injected diff/revalidate providers —
        so a provider can never deadlock on re-entry).

        ``flock`` is POSIX advisory + **same-host**; it provides same-host
        single-writer + a kernel-authoritative liveness witness (released on process
        death). It does NOT span hosts — cross-host coordination rests solely on the
        atomic ``os.link`` revision-claim (``SHARED_STORE_CAS`` over a store with
        atomic link). flock is never relied on as a cross-host safety primitive.
        """
        path = self._lock_file(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- write-back-conditional CAS on resource_version --------------------

    def _claim_file(self, workflow_id: WorkflowID, resource_version: int) -> Path:
        """The atomic CAS claim file for one (workflow, resource_version) pair."""
        digest = hashlib.sha256(str(workflow_id).encode("utf-8")).hexdigest()
        return self._journal_dir / f"{digest}.v{resource_version}.claim"

    def _claim_resume_revision(self, workflow_id: WorkflowID, resource_version: int) -> bool:
        """Write-back-conditional CAS on ``resource_version`` — the etcd ``mod_revision``
        analogue. Returns ``True`` iff THIS call won the right to resume the revision.

        The genuine floor-(iii) concurrent-resume mitigation, hand-rolled (I-6, no
        vendored etcd) via the POSIX atomic create-exclusive primitive: the FIRST
        resume to create the per-``(workflow, resource_version)`` claim WINS; any
        LATER resume of the SAME revision LOSES (``FileExistsError`` → ``False`` →
        the caller maps to ``ABORT_REVALIDATION_FAILED``). There is **NO owner
        token** — the CAS is on the revision itself (etcd compares ``mod_revision``,
        not an owner identity; the prior owner-token model was the defeated [P1]).

        **Crash-atomic publish (no empty-claim / no torn claim).** Write a
        best-effort claimant incarnation stamp (``host:pid`` — for observability +
        the future fenced-auto-recovery seam; NOT the win/lose discriminator) to a
        unique temp (``fsync``-ed), then ``os.link`` it into place — ``link`` is
        atomic and raises ``FileExistsError`` if the claim already exists, so a crash
        can only ever leave an orphan temp, never a half-published claim. The won
        claim's dirent is ``fsync``-ed so a crash cannot lose it and let a second
        resume re-create the claim and double-resume. The temp name is randomized
        (``uuid4``) so a stale orphan temp can never collide with a fresh claim.

        **Honest limit (F-1 — out of this substrate, see the module docstring):** a
        crash AFTER winning but BEFORE the resume completes leaves the claim in
        place; a retry of the SAME revision then LOSES → ``ABORT_REVALIDATION_FAILED``
        → §22.1 HITL. AUTO-recovering that crash-mid-resume window is CP-scope
        (single-host flock-across-suffix) or needs a fenced lease (multi-host). This
        substrate fail-closes the window to HITL rather than risk a double-execution.
        """
        path = self._claim_file(workflow_id, resource_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = f"{socket.gethostname()}:{os.getpid()}".encode()
        tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, stamp)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False  # the revision is already claimed → lose the CAS
            self._fsync_dir(path.parent)
            return True
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- Protocol: resume with the revision CAS -----------------------------

    def attempt_resume(self, attempt: ResumeAttempt) -> ResumeOutcome:
        """Replay the latest committed convergence, then CAS-claim its revision.

        Order: (1) under the per-workflow flock, read the latest committed converged
        state + its ``resource_version`` (``None`` → ``ABORT_SNAPSHOT_CORRUPTED``,
        fail closed); (2) attempt the write-back-conditional CAS claim on that
        revision — the FIRST resume wins; a LATER resume of the SAME revision
        ``ABORT_REVALIDATION_FAILED`` (a concurrent-resume race, or an already-
        resumed revision; → §22.1); (3) on a won claim, run the inherited diff /
        revalidate / classify flow.

        The flock is held across read-prefix → claim so the claimed revision is the
        then-current head (no converge interleaves; fixes the resume-side TOCTOU) and
        a concurrent capture cannot clobber the version. It is released BEFORE the
        injected diff/revalidate providers (so a provider can never deadlock on lock
        re-entry).
        """
        with self._workflow_lock(attempt.paused_workflow_id):
            prefix = self._valid_prefix(attempt.paused_workflow_id)
            if not prefix:
                return ResumeOutcome(
                    outcome_kind=ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED,
                    material_diff=(),
                    context_revalidated=False,
                    resume_audit_entry_id=None,
                )
            resource_version = len(prefix) - 1
            event = prefix[-1]
            claimed = self._claim_resume_revision(attempt.paused_workflow_id, resource_version)
        if not claimed:
            # A resume of this revision already won the CAS → a concurrent-resume
            # race lost (or a retry of an already-resumed/claimed revision). Maps
            # onto the CLOSED ResumeOutcomeKind enum (no new primitive): the
            # converged state is being / was resumed by another reconciler, so this
            # attempt must not also apply (→ §22.1 escalation, the honest surface
            # for a genuine two-reconciler anomaly / a fail-closed crash-mid-resume).
            return ResumeOutcome(
                outcome_kind=ResumeOutcomeKind.ABORT_REVALIDATION_FAILED,
                material_diff=(),
                context_revalidated=False,
                resume_audit_entry_id=(
                    self._resume_audit_entry_id_provider(
                        attempt, ResumeOutcomeKind.ABORT_REVALIDATION_FAILED
                    )
                    if self._resume_audit_entry_id_provider is not None
                    else None
                ),
            )
        diff = self._diff_provider(event, attempt)
        revalidated = self._revalidation_succeeded(attempt, diff)
        # Reuse the canonical C-CP-22 §22.1 decision core (one source of truth;
        # it keys on `d.is_material`, not bare emptiness) — identical to the base
        # `attempt_resume` classify, so the only divergence is the CAS claim above.
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

    # -- durable per-revision convergence-log I/O (overrides the base) ------

    @staticmethod
    def _canonical_payload(
        workflow_id: WorkflowID, resource_version: int, event: PauseEvent
    ) -> str:
        """The exact serialized record payload the checksum is computed over."""
        return json.dumps(
            {
                "workflow_id": str(workflow_id),
                "resource_version": resource_version,
                "pause_event": event.model_dump(mode="python"),
            },
            default=json_default,
            sort_keys=True,
        )

    def _valid_prefix(self, workflow_id: WorkflowID) -> list[PauseEvent]:
        """Scan the contiguous valid convergence-record prefix → events.

        Byte-robust (reads bytes, decodes each newline-terminated chunk
        individually) so an invalid-UTF-8 or partial trailing record ends the prefix
        without crashing. A *complete* record is a ``b"...\\n"`` chunk that decodes,
        is non-blank, passes checksum, matches its expected ``resource_version``
        (= position), and validates as a ``PauseEvent``. The scan stops at a torn
        tail (no final ``\\n``) or the first corrupt record (gap-safe — replay never
        resumes past a corruption).
        """
        path = self._journal_file(workflow_id)
        if not path.exists():
            return []
        try:
            raw = path.read_bytes()
        except OSError:
            return []
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
                break  # invalid UTF-8 → corruption, stop the prefix
            if not line.strip():
                break  # blank line ends the valid prefix (defensive)
            parsed = self._parse_record(line, str(workflow_id), index)
            if parsed is None:
                break  # first corruption ends the contiguous valid prefix
            events.append(parsed)
            offset = newline + 1
            index += 1
        return events

    def _valid_extent(self, workflow_id: WorkflowID) -> int:
        """Byte offset where the contiguous valid record prefix ends (truncation point)."""
        path = self._journal_file(workflow_id)
        if not path.exists():
            return 0
        try:
            raw = path.read_bytes()
        except OSError:
            return 0
        offset = 0
        index = 0
        total = len(raw)
        while offset < total:
            newline = raw.find(b"\n", offset)
            if newline == -1:
                break
            chunk = raw[offset:newline]
            try:
                line = chunk.decode("utf-8")
            except UnicodeDecodeError:
                break
            if not line.strip():
                break
            if self._parse_record(line, str(workflow_id), index) is None:
                break
            offset = newline + 1
            index += 1
        return offset

    def _append(self, workflow_id: WorkflowID, event: PauseEvent) -> None:
        """Append one checksummed convergence record to the workflow's log, durably.

        **Recovery-on-open:** before appending, truncate any torn/garbage tail back
        to the contiguous valid prefix (otherwise a crash that left a partial
        trailing record would corrupt the next append). The new ``resource_version``
        is the count of valid prefix records, so it stays monotonic over *committed*
        convergences (a torn write is discarded, not counted). Write-ahead
        durability: the record is ``fsync``-ed before returning; a new file's
        directory entry is also ``fsync``-ed (best-effort, POSIX).
        """
        # Serialize the read-prefix → assign-version → append critical section per
        # workflow so two concurrent captures cannot pick the same resource_version
        # (which replay would treat as a corrupt gap, silently losing a
        # durably-returned capture). The lock file lives beside the log.
        with self._workflow_lock(workflow_id):
            path = self._journal_file(workflow_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            prefix = self._valid_prefix(workflow_id)
            resource_version = len(prefix)
            if path.exists():
                valid_extent = self._valid_extent(workflow_id)
                if path.stat().st_size != valid_extent:
                    with path.open("r+b") as handle:
                        handle.truncate(valid_extent)
                        handle.flush()
                        os.fsync(handle.fileno())
            payload = self._canonical_payload(workflow_id, resource_version, event)
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
        """Return the last committed convergence's ``PauseEvent``, or ``None``.

        Replays the contiguous valid prefix (torn tail discarded; corrupt middle NOT
        skipped — gap-safe). ``None`` when the workflow has no log OR not even its
        first record is valid. (``attempt_resume`` overrides the base to add the CAS
        claim, but this primitive stays consistent for ``has_pause_record`` parity.)
        """
        prefix = self._valid_prefix(workflow_id)
        return prefix[-1] if prefix else None

    @staticmethod
    def _parse_record(
        line: str, expected_workflow_id: str, expected_index: int
    ) -> PauseEvent | None:
        """Parse + integrity-check one convergence record; ``None`` if invalid.

        Validates, in order: outer JSON shape (``checksum`` + ``payload`` string);
        SHA-256 checksum over the exact payload bytes; payload JSON shape;
        ``workflow_id`` match; ``resource_version`` == position (ordering integrity);
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
            if record.get("resource_version") != expected_index:
                return None
            return PauseEvent.model_validate(record["pause_event"])
        except (ValueError, ValidationError, KeyError, TypeError):
            return None
