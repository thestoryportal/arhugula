"""Durable etcd-style reconciler engine pause/resume substrate tests (R-FS-1 E-3 / U-RT-123).

The load-bearing proofs:

- ``test_resume_after_fresh_instance`` — cross-process durability (a fresh instance
  resumes a convergence captured by a prior one; the #475 property, per revision).
- ``test_torn_tail_recovers_to_last_committed`` / ``..._then_append_recovers`` /
  ``test_corrupt_middle_record_stops_at_gap`` — WAL-style torn-write + gap-safe
  recovery (shared with U-RT-121; the torn-tail = a crash-mid-append proof).
- **``test_cas_concurrent_resume_one_wins_one_aborts`` + the OS-process variant** —
  THE KEYSTONE: the genuine NEW capability over WAL. Two resumes of the SAME
  committed ``resource_version`` → the first wins the **write-back-conditional CAS
  on the revision** (the etcd ``mod_revision`` analogue; RESUME_CLEAN), any later
  resume of the same revision loses (ABORT_REVALIDATION_FAILED). NO owner token —
  the CAS is on the revision itself (the prior owner-token model was the defeated
  [P1]: a shared actor across processes let two concurrent reconcilers both re-enter
  → double-execution). Hand-rolled via the POSIX ``O_EXCL``/``os.link`` atomic-create
  primitive — no vendored etcd.
- ``test_crash_after_claim_retry_of_claimed_revision_aborts`` — the HONEST F-1
  limit: a retry of an already-claimed revision fail-closes to ABORT (→ §22.1 HITL),
  never a silent double-execution. AUTO-recovery of the crash-mid-resume window is
  the CP-scope flock-across-suffix (single-host) / fenced-lease (multi-host) build arc.
- ``test_shared_store_cas_backend_*`` — the parameterized backend: SHARED_STORE_CAS
  runs a SAME-HOST startup atomicity sanity check and FAILS CLOSED on a store that
  cannot back atomic create-exclusive; LOCAL_SINGLE_HOST (default) skips it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from harness_core import WorkflowID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import ExternalReference, ReferenceClass, StateSummary
from harness_cp.pause_resume_protocol import (
    PauseReason,
    ResumeAttempt,
    ResumeOutcomeKind,
)
from harness_is.state_ledger_entry_schema import Identifier
from harness_runtime.lifecycle import reconciler_pause_resume_substrate as _mod
from harness_runtime.lifecycle.reconciler_pause_resume_substrate import (
    LeaseBackend,
    ReconcilerEnginePauseResumeSubstrate,
)

_ACTOR = ActorIdentity("reconciler-loop")


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


def _resume_attempt(workflow_id: str, *, actor: ActorIdentity = _ACTOR) -> ResumeAttempt:
    return ResumeAttempt(
        paused_workflow_id=WorkflowID(workflow_id),
        resume_at="2026-06-15T12:00:00Z",
        resume_request_actor=actor,
    )


def _substrate(
    reconcile_log_dir: Path,
    *,
    state_summary_provider: Callable[[], StateSummary] | None = None,
    **kwargs: object,
) -> ReconcilerEnginePauseResumeSubstrate:
    return ReconcilerEnginePauseResumeSubstrate(
        journal_dir=reconcile_log_dir,
        state_summary_provider=state_summary_provider or _state_summary,
        **kwargs,  # type: ignore[arg-type]
    )


def _versioned_substrate(reconcile_log_dir: Path) -> ReconcilerEnginePauseResumeSubstrate:
    versions = iter(f"v{i}" for i in range(1, 100))
    return ReconcilerEnginePauseResumeSubstrate(
        journal_dir=reconcile_log_dir,
        state_summary_provider=lambda: _state_summary(next(versions)),
    )


#: A self-contained worker script for the genuinely-concurrent OS-process CAS race,
#: launched via ``subprocess`` (robust vs the spawn-Pool-under-pytest re-import
#: fragility). Each process attempts to resume the same committed revision; exactly
#: one wins the atomic ``os.link`` claim, the rest abort.
_MP_WORKER_SCRIPT = """
import hashlib
import sys
from pathlib import Path

from harness_core import WorkflowID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import ExternalReference, ReferenceClass, StateSummary
from harness_cp.pause_resume_protocol import ResumeAttempt
from harness_is.state_ledger_entry_schema import Identifier
from harness_runtime.lifecycle.reconciler_pause_resume_substrate import (
    ReconcilerEnginePauseResumeSubstrate,
)


def _ss():
    return StateSummary(
        relevant_entries=(),
        summary_text="v1",
        summary_hash=hashlib.sha256(b"v1").hexdigest(),
        idempotency_key=Identifier("idem-v1"),
        external_references=(
            ExternalReference(
                reference_class=ReferenceClass.FILESYSTEM_STATE,
                reference_id="s",
                snapshot_capture_at_pause=b"x",
            ),
        ),
    )


_s = ReconcilerEnginePauseResumeSubstrate(journal_dir=Path(sys.argv[1]), state_summary_provider=_ss)
print(
    _s.attempt_resume(
        ResumeAttempt(
            paused_workflow_id=WorkflowID("wf-mp"),
            resume_at="t",
            resume_request_actor=ActorIdentity("a"),
        )
    ).outcome_kind.name
)
"""


# --- capture / durability ---------------------------------------------------


def test_capture_writes_durable_checksummed_record(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)
    event = substrate.capture_pause_snapshot(WorkflowID("wf-1"), PauseReason.ENGINE_NATIVE_PAUSE)

    log = substrate._journal_file(WorkflowID("wf-1"))  # type: ignore[attr-defined]
    assert log.exists(), "capture must persist to disk"
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    outer = json.loads(lines[0])
    assert set(outer) == {"checksum", "payload"}
    assert hashlib.sha256(outer["payload"].encode("utf-8")).hexdigest() == outer["checksum"]
    assert json.loads(outer["payload"])["resource_version"] == 0
    assert substrate._read_latest(WorkflowID("wf-1")) == event  # type: ignore[attr-defined]


def test_resource_version_is_monotonic_per_workflow(tmp_path: Path) -> None:
    substrate = _versioned_substrate(tmp_path)
    for _ in range(3):
        substrate.capture_pause_snapshot(WorkflowID("wf-seq"), PauseReason.ENGINE_NATIVE_PAUSE)

    log = substrate._journal_file(WorkflowID("wf-seq"))  # type: ignore[attr-defined]
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    versions = [json.loads(json.loads(line)["payload"])["resource_version"] for line in lines]
    assert versions == [0, 1, 2]


def test_resume_after_fresh_instance(tmp_path: Path) -> None:
    """Cross-process durability — a fresh instance resumes a prior-process convergence."""
    capturing = _substrate(tmp_path)
    captured = capturing.capture_pause_snapshot(
        WorkflowID("wf-crash"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    del capturing

    resuming = _substrate(tmp_path)
    outcome = resuming.attempt_resume(_resume_attempt("wf-crash"))

    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert resuming._read_latest(WorkflowID("wf-crash")) == captured  # type: ignore[attr-defined]


def test_multiple_records_resume_latest_committed(tmp_path: Path) -> None:
    substrate = _versioned_substrate(tmp_path)
    first = substrate.capture_pause_snapshot(
        WorkflowID("wf-multi"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    second = substrate.capture_pause_snapshot(
        WorkflowID("wf-multi"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    assert first != second
    assert substrate._read_latest(WorkflowID("wf-multi")) == second  # type: ignore[attr-defined]


def test_no_record_aborts_snapshot_corrupted(tmp_path: Path) -> None:
    """No convergence record → ABORT_SNAPSHOT_CORRUPTED (fail closed)."""
    substrate = _substrate(tmp_path)
    outcome = substrate.attempt_resume(_resume_attempt("wf-absent"))
    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED


def test_has_pause_record_presence(tmp_path: Path) -> None:
    substrate = _substrate(tmp_path)
    assert not substrate.has_pause_record(WorkflowID("wf-h"))
    substrate.capture_pause_snapshot(WorkflowID("wf-h"), PauseReason.ENGINE_NATIVE_PAUSE)
    assert substrate.has_pause_record(WorkflowID("wf-h"))


# --- write-back-conditional CAS on resource_version (the new floor-(iii)) ----


def test_cas_concurrent_resume_one_wins_one_aborts(tmp_path: Path) -> None:
    """KEYSTONE — floor (iii) etcd compare-and-swap concurrent-resume mitigation.

    Two resumes of the SAME committed resource_version: the first wins the
    write-back-conditional CAS on the revision (RESUME_CLEAN), the second loses it
    (ABORT_REVALIDATION_FAILED — the converged state is being resumed by another
    reconciler, so this attempt must not also apply). NO owner token — the CAS is on
    the revision (the prior owner-token model was the defeated [P1]). Realized via a
    POSIX O_EXCL/os.link atomic-create claim (no vendored etcd).
    """
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-cas"), PauseReason.ENGINE_NATIVE_PAUSE)

    first = substrate.attempt_resume(_resume_attempt("wf-cas"))
    second = substrate.attempt_resume(_resume_attempt("wf-cas"))

    assert first.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN, (
        "first resume wins the revision CAS"
    )
    assert second.outcome_kind is ResumeOutcomeKind.ABORT_REVALIDATION_FAILED, (
        "a later resume of the SAME revision loses the CAS → aborts"
    )
    # The claim file for revision 0 exists, published ATOMICALLY (temp+link) — its
    # content is a best-effort incarnation stamp (NOT the win/lose discriminator).
    claim = substrate._claim_file(WorkflowID("wf-cas"), 0)  # type: ignore[attr-defined]
    assert claim.exists()
    assert claim.read_text().strip(), "claim carries a best-effort incarnation stamp"


def test_cas_concurrent_resume_across_os_processes_exactly_one_wins(tmp_path: Path) -> None:
    """Verification-shape: genuinely-concurrent OS PROCESSES (not threads under one GIL)
    racing the same revision claim — exactly ONE wins (RESUME_CLEAN), the rest abort.
    The real concurrent-resume safety property (the per-workflow flock + the atomic
    ``os.link`` claim serialize cross-process same-host). Launched via ``subprocess``
    (robust vs the spawn-Pool-under-pytest re-import fragility). NOTE: this proves
    cross-PROCESS atomicity on a local FS; cross-HOST atomicity rests on the operator's
    atomic-link store declaration and CANNOT be verified in single-host CI (F-03)."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-mp"), PauseReason.ENGINE_NATIVE_PAUSE)

    n = 3
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _MP_WORKER_SCRIPT, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(n)
    ]
    results = [proc.communicate(timeout=120) for proc in procs]
    outs = [out.strip() for out, _ in results]

    wins = [o for o in outs if o == "RESUME_CLEAN"]
    aborts = [o for o in outs if o == "ABORT_REVALIDATION_FAILED"]
    assert len(wins) == 1 and len(aborts) == n - 1, (
        f"exactly one process wins the revision CAS; outs={outs} "
        f"stderr={[err.strip() for _, err in results]}"
    )


def test_crash_after_claim_retry_of_claimed_revision_aborts(tmp_path: Path) -> None:
    """The HONEST F-1 limit (NOT a regression): a holder that WON the claim then
    crashed mid-resume leaves the claim; a retry of the SAME revision fail-closes to
    ABORT_REVALIDATION_FAILED (→ §22.1 HITL), never a silent double-execution. AUTO-
    recovery of that crash-mid-resume window is OUT of this substrate (CP-scope
    flock-across-suffix single-host / a fenced bounded-synchrony lease multi-host —
    the committed F-1/F-2 build arcs). A static owner-token re-entry here would be
    the defeated [P1] (it cannot tell a dead retry from a live concurrent resume)."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-crash-mid"), PauseReason.ENGINE_NATIVE_PAUSE)
    first = substrate.attempt_resume(_resume_attempt("wf-crash-mid"))
    assert first.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN

    # A restarted reconciler (fresh instance over the same durable dir) retries the
    # SAME revision whose claim already exists.
    restarted = _substrate(tmp_path)
    retry = restarted.attempt_resume(_resume_attempt("wf-crash-mid"))
    assert retry.outcome_kind is ResumeOutcomeKind.ABORT_REVALIDATION_FAILED, (
        "retry of an already-claimed revision fail-closes to ABORT (F-1: auto-recovery is CP-scope)"
    )


def test_cas_is_per_workflow(tmp_path: Path) -> None:
    """A CAS claim for one workflow's revision does NOT block another workflow's
    resume at the same revision number (the claim is keyed per (workflow, revision))."""
    substrate = _substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-a"), PauseReason.ENGINE_NATIVE_PAUSE)
    substrate.capture_pause_snapshot(WorkflowID("wf-b"), PauseReason.ENGINE_NATIVE_PAUSE)

    assert substrate.attempt_resume(_resume_attempt("wf-a")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )
    # wf-b at the same revision 0 is unaffected by wf-a's claim.
    assert substrate.attempt_resume(_resume_attempt("wf-b")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )


def test_cas_new_convergence_resumes_again(tmp_path: Path) -> None:
    """The CAS is per-revision, NOT a permanent freeze: after the first resume claims
    revision 0, a NEW converge (revision 1) is resumable again (RESUME_CLEAN) — the
    claim is revision-scoped (etcd semantics: a new revision is a fresh CAS target).
    The per-revision concurrent-resume guard still holds at revision 1."""
    substrate = _versioned_substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-rev"), PauseReason.ENGINE_NATIVE_PAUSE)
    # First resume claims revision 0.
    assert substrate.attempt_resume(_resume_attempt("wf-rev")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )
    # A new converge bumps to revision 1 → resumable again (fresh CAS target).
    substrate.capture_pause_snapshot(WorkflowID("wf-rev"), PauseReason.ENGINE_NATIVE_PAUSE)
    assert substrate.attempt_resume(_resume_attempt("wf-rev")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )
    # The mitigation still holds per-revision: a SECOND resume of revision 1 loses.
    assert substrate.attempt_resume(_resume_attempt("wf-rev")).outcome_kind is (
        ResumeOutcomeKind.ABORT_REVALIDATION_FAILED
    )


def test_concurrent_captures_serialize_no_lost_records(tmp_path: Path) -> None:
    """P2.2 — the per-workflow flock serializes concurrent same-workflow captures so
    no two pick the same resource_version. N threads each capture the same workflow;
    the flock serializes the read-assign-append critical section, so the log holds
    exactly N records with monotonic resource_versions 0..N-1 (a durably-returned
    capture is never lost to a same-version collision that replay would treat as a
    corrupt gap)."""
    substrate = _substrate(tmp_path)  # fixed (thread-safe) state-summary provider
    n = 12
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(
            pool.map(
                lambda _: substrate.capture_pause_snapshot(
                    WorkflowID("wf-concurrent"), PauseReason.ENGINE_NATIVE_PAUSE
                ),
                range(n),
            )
        )
    log = substrate._journal_file(WorkflowID("wf-concurrent"))  # type: ignore[attr-defined]
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    versions = [json.loads(json.loads(line)["payload"])["resource_version"] for line in lines]
    assert versions == list(range(n)), "all N captures land with monotonic versions (none lost)"
    # The full prefix replays cleanly — no gap from a collided/duplicate version.
    assert substrate.attempt_resume(_resume_attempt("wf-concurrent")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )


# --- parameterized lease backend (parameterize-and-assert; fail-closed) ------


def test_local_single_host_is_the_default_backend(tmp_path: Path) -> None:
    assert _substrate(tmp_path).lease_backend is LeaseBackend.LOCAL_SINGLE_HOST


def test_shared_store_cas_backend_startup_assert_passes_on_local_fs(tmp_path: Path) -> None:
    """The SAME-HOST startup atomicity sanity check passes on a local FS (atomic
    os.link), and the substrate is usable end-to-end on the shared-store backend."""
    substrate = _substrate(tmp_path, lease_backend=LeaseBackend.SHARED_STORE_CAS)
    assert substrate.lease_backend is LeaseBackend.SHARED_STORE_CAS
    substrate.capture_pause_snapshot(WorkflowID("wf-sc"), PauseReason.ENGINE_NATIVE_PAUSE)
    assert substrate.attempt_resume(_resume_attempt("wf-sc")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )


def test_local_single_host_backend_skips_startup_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOCAL_SINGLE_HOST (default) runs NO startup os.link probe (zero-config)."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("LOCAL_SINGLE_HOST must not probe os.link at startup")

    monkeypatch.setattr(_mod.os, "link", _boom)
    assert _substrate(tmp_path).lease_backend is LeaseBackend.LOCAL_SINGLE_HOST


def test_shared_store_cas_backend_fails_closed_when_link_not_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the store does NOT enforce atomic create-exclusive (a second os.link to an
    existing name does not raise FileExistsError), construction FAILS CLOSED with a
    RuntimeError — never a silently-unsafe lease. (Honest: a single-process probe is a
    same-host sanity check, NOT a cross-host verifier — see F-03.)"""
    real_link = os.link
    calls = {"n": 0}

    def _non_atomic_link(src: object, dst: object, *a: object, **k: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            real_link(src, dst)  # type: ignore[arg-type]
            return
        return  # second link silently "succeeds" → a non-atomic store

    monkeypatch.setattr(_mod.os, "link", _non_atomic_link)
    with pytest.raises(RuntimeError, match="UNSAFE"):
        _substrate(tmp_path, lease_backend=LeaseBackend.SHARED_STORE_CAS)


def test_shared_store_cas_startup_tolerates_stale_probe_file(tmp_path: Path) -> None:
    """[P2] regression (Codex + advisor, decorrelated — same finding both surfaced):
    a stale probe file left by a PRIOR crashed startup, OR a CONCURRENT peer's in-flight
    probe, must NOT make a fresh SHARED_STORE_CAS startup falsely fail-closed. The probe
    TARGET is uuid-unique per invocation, so a leftover probe can never collide with a
    new one. CONTRASTING BASELINE: this pre-creates the EXACT fixed name the pre-fix code
    used — the pre-fix fixed-target probe would hit FileExistsError on this leftover and
    spuriously raise the UNSAFE RuntimeError on a perfectly atomic store (revert the uuid
    token → this test fails)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cas-atomicity-probe").write_bytes(b"stale-from-a-prior-crashed-startup")
    substrate = _substrate(tmp_path, lease_backend=LeaseBackend.SHARED_STORE_CAS)
    assert substrate.lease_backend is LeaseBackend.SHARED_STORE_CAS
    # End-to-end still works on the shared-store backend despite the stale litter.
    substrate.capture_pause_snapshot(WorkflowID("wf-stale"), PauseReason.ENGINE_NATIVE_PAUSE)
    assert substrate.attempt_resume(_resume_attempt("wf-stale")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )


# --- torn-write / recovery semantics (shared with U-RT-121) -----------------


def test_torn_tail_recovers_to_last_committed(tmp_path: Path) -> None:
    """A half-written trailing record is discarded; replay recovers to the last
    committed convergence (RESUME_CLEAN — a torn tail is an uncommitted write)."""
    substrate = _versioned_substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-torn"), PauseReason.ENGINE_NATIVE_PAUSE)
    second = substrate.capture_pause_snapshot(
        WorkflowID("wf-torn"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    log = substrate._journal_file(WorkflowID("wf-torn"))  # type: ignore[attr-defined]
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"checksum": "deadbeef", "payload": "{partial')

    outcome = substrate.attempt_resume(_resume_attempt("wf-torn"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert substrate._read_latest(WorkflowID("wf-torn")) == second  # type: ignore[attr-defined]


def test_torn_tail_then_append_recovers_and_continues(tmp_path: Path) -> None:
    """Recovery-on-open: after a crash leaves a torn trailing record, the next
    capture truncates it and appends cleanly — replay sees the NEW committed record."""
    substrate = _versioned_substrate(tmp_path)
    substrate.capture_pause_snapshot(WorkflowID("wf-recover"), PauseReason.ENGINE_NATIVE_PAUSE)
    log = substrate._journal_file(WorkflowID("wf-recover"))  # type: ignore[attr-defined]
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"checksum": "x", "payload": "{torn-no-newline')

    third = substrate.capture_pause_snapshot(
        WorkflowID("wf-recover"), PauseReason.ENGINE_NATIVE_PAUSE
    )
    assert substrate._read_latest(WorkflowID("wf-recover")) == third  # type: ignore[attr-defined]
    assert substrate.attempt_resume(_resume_attempt("wf-recover")).outcome_kind is (
        ResumeOutcomeKind.RESUME_CLEAN
    )
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    versions = [json.loads(json.loads(line)["payload"])["resource_version"] for line in lines]
    assert versions == [0, 1]


def test_corrupt_middle_record_stops_at_gap(tmp_path: Path) -> None:
    """Gap-safe: a corrupt MIDDLE record is not skipped — replay stops at the gap and
    recovers the prefix BEFORE it, never resuming past a corruption."""
    substrate = _versioned_substrate(tmp_path)
    first = substrate.capture_pause_snapshot(WorkflowID("wf-gap"), PauseReason.ENGINE_NATIVE_PAUSE)
    substrate.capture_pause_snapshot(WorkflowID("wf-gap"), PauseReason.ENGINE_NATIVE_PAUSE)
    substrate.capture_pause_snapshot(WorkflowID("wf-gap"), PauseReason.ENGINE_NATIVE_PAUSE)

    log = substrate._journal_file(WorkflowID("wf-gap"))  # type: ignore[attr-defined]
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    corrupt = json.loads(lines[1])
    corrupt["checksum"] = "0" * 64
    lines[1] = json.dumps(corrupt, sort_keys=True)
    log.write_text("\n".join(lines) + "\n")

    outcome = substrate.attempt_resume(_resume_attempt("wf-gap"))
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert substrate._read_latest(WorkflowID("wf-gap")) == first  # type: ignore[attr-defined]
