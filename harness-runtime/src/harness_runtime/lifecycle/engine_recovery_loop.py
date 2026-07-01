"""Runtime engine recovery loop producer for R-CXA-2.

Authority: C-CP-22 engine-layer pause/resume free functions, U-CP-78
`cp.pause-captured`, and U-CP-79 `cp.resume-attempted`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from harness_core import WorkflowID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.engine_class import EngineClass
from harness_cp.pause_resume_protocol import (
    EnginePauseResumeSubstrate,
    PauseEvent,
    PauseReason,
    ResumeAttempt,
    ResumeOutcome,
    attempt_resume,
    bind_engine_pause_resume_substrate,
    capture_pause_snapshot,
)
from harness_is.state_ledger_write import WriteResult

from harness_runtime.lifecycle.cp_is_wiring import RuntimeCpIsWiring


def run_scoped_substrate_key(workflow_id: str, run_id: str) -> WorkflowID:
    """The run-scoped DURABLE STORAGE key for the engine pause record (NOT the
    ledger ``workflow_id``).

    The WAL engine pause record and the F2 replay prefix are two halves of ONE
    resumption mechanism, so they MUST share a scope. The F2 prefix is run-scoped
    (the workflow driver's ``run_idempotency_key`` includes ``run_id``); this keys
    the durable engine substrate by the same ``(workflow_id, run_id)`` pair. Resume
    reuses the SAME ``run_id`` (the e2e drives run 1 + run 2 under one ``run_id``),
    so a DIFFERENT ``run_id`` is by construction a NEW execution — never a
    legitimate resume of an older pause. Run-scoping therefore isolates fresh runs
    without ever breaking a real resume (Codex round-4 [P2]: a lingering corrupt
    record from an abandoned run would otherwise fail-close EVERY future run of the
    same ``workflow_id`` — a permanent DoS).

    SHA-256 over 0x1E-delimited parts is unconditionally collision-free across
    ``(workflow_id, run_id)`` (unlike a naive ``f"{wf}::{run}"`` concat). The LEDGER
    entries (``cp.pause-captured`` / ``cp.resume-attempted``) keep the PLAIN
    ``workflow_id`` so the audit reads ``wf-X`` — matching the ``workflow.envelope``
    ``workflow.id`` attribute — never this storage digest.
    """
    digest = hashlib.sha256(
        b"engine-pause\x1e" + workflow_id.encode("utf-8") + b"\x1e" + run_id.encode("utf-8")
    ).hexdigest()
    return WorkflowID(digest)


class ResumableEngineSubstrate(EnginePauseResumeSubstrate, Protocol):
    """Runtime-side narrowing of the cleared C-CP-22 ``EnginePauseResumeSubstrate``
    that additionally exposes the non-emitting presence probe ``has_pause_record``.

    Defined HERE (harness_runtime), NOT by widening the cleared harness-cp
    Protocol: the C-CP-22 ``EnginePauseResumeSubstrate`` contract is
    design-substrate-cleared, so adding a method to it would be an X-AL-3 design
    extension. Both concrete substrates — ``DeterministicEnginePauseResumeSubstrate``
    (harness-cp) and ``WALSegmentEnginePauseResumeSubstrate`` /
    ``JournalEnginePauseResumeSubstrate`` (harness-runtime) — structurally satisfy
    this narrowing, so the recovery loop probes presence with a compile-time
    guarantee rather than a runtime ``hasattr`` fallback.
    """

    def has_pause_record(self, workflow_id: WorkflowID) -> bool: ...


@dataclass(frozen=True, slots=True)
class EnginePauseCaptureEmission:
    """Pause capture plus its CP→IS write result."""

    pause_event: PauseEvent
    write_result: WriteResult


@dataclass(frozen=True, slots=True)
class EngineResumeAttemptEmission:
    """Resume outcome plus its CP→IS write result."""

    resume_outcome: ResumeOutcome
    write_result: WriteResult


class EngineRecoverySubstrateNotBoundError(Exception):
    """No durable engine substrate is bound for the requested engine class.

    Raised by ``RuntimeEngineRecoveryLoop._substrate_for`` when a driver firing
    branch requests an engine class the R-CXA-2 factory (U-RT-124) did not bind a
    substrate for. Fail-loud (detect-then-refuse) rather than a silent no-op or a
    raw ``KeyError``: a missing binding means the engine-class-aware
    materialization is incomplete, which must surface — never corrupt the recovery
    path. The driver only fires for a gated, in-scope engine class, so in a
    correct bind this raise is preserved-but-unreachable.
    """

    def __init__(self, engine_class: EngineClass) -> None:
        super().__init__(f"no engine recovery substrate bound for engine class {engine_class!r}")
        self.engine_class = engine_class


@dataclass(frozen=True, slots=True)
class RuntimeEngineRecoveryLoop:
    """Bind engine substrates engine-class-aware and emit R-CXA-2 engine-layer entries.

    The substrate is selected per engine class (U-RT-124 / O-RT-4): WAL_SEGMENT
    fires the WAL segment-log substrate, RECONCILER_LOOP fires the etcd-style
    reconciler substrate. The per-engine-class map is the single source of routing
    truth, so each engine class reads/writes ONLY its own durable store — a WAL
    pause can never land in the reconciler store, nor vice versa (the U-RT-124
    no-cross-contamination AC, enforced by construction). Every firing call passes
    the workflow's ``engine_class`` (the driver firing branches are already gated
    on it); an unbound class fails loud.
    """

    wiring: RuntimeCpIsWiring
    substrate_by_engine_class: Mapping[EngineClass, ResumableEngineSubstrate]
    actor: ActorIdentity

    def _substrate_for(self, engine_class: EngineClass) -> ResumableEngineSubstrate:
        """Select the durable substrate bound for ``engine_class`` — fail loud if none."""
        substrate = self.substrate_by_engine_class.get(engine_class)
        if substrate is None:
            raise EngineRecoverySubstrateNotBoundError(engine_class)
        return substrate

    def has_pause_record(self, *, engine_class: EngineClass, workflow_id: str, run_id: str) -> bool:
        """True iff the bound substrate holds a pause RECORD for this run —
        **presence, NOT validity**; a pure, non-emitting read.

        Keyed by the run-scoped storage key (``workflow_id`` + ``run_id``) so a
        fresh execution of the same ``workflow_id`` never sees an earlier run's
        lingering record (Codex round-4 [P2]). Gates the U-CP-95 resume firing. An
        ABSENT record → driver does not fire resume (an ordinary step-prefix crash
        recovery, where steps materialized but NO engine pause was captured, must
        not record a spurious resume entry — Codex [P2]). A PRESENT-but-corrupt
        record still returns ``True`` here so the driver DOES fire
        ``attempt_resume`` — which classifies it ``ABORT_SNAPSHOT_CORRUPTED`` and
        lets the driver fail the run closed. Conflating presence with validity (the
        prior ``has_captured_pause`` bug, Codex [P1-r3-a]) would silently skip the
        resume for a corrupt snapshot, losing the abort record AND resuming past
        unrecoverable state.

        Routed to the substrate bound for ``engine_class`` so the probe reads only
        that engine class's durable store (the no-cross-contamination invariant).
        """
        return self._substrate_for(engine_class).has_pause_record(
            run_scoped_substrate_key(workflow_id, run_id)
        )

    async def capture_pause(
        self,
        *,
        engine_class: EngineClass,
        workflow_id: str,
        run_id: str,
        step_id: str,
        pause_reason: PauseReason,
    ) -> EnginePauseCaptureEmission:
        """Capture an engine-layer pause and emit `cp.pause-captured`.

        The durable substrate is keyed run-scoped (``workflow_id`` + ``run_id``);
        the ledger entry keeps the PLAIN ``workflow_id`` so the audit reads the
        real workflow id (Codex round-4 [P2]). The capture + resume key compositions
        MUST be identical — a mismatch would silently never-resume. The pause is
        written to the substrate bound for ``engine_class`` only.
        """
        with bind_engine_pause_resume_substrate(self._substrate_for(engine_class)):
            pause_event = capture_pause_snapshot(
                run_scoped_substrate_key(workflow_id, run_id), pause_reason
            )

        write_result = await self.wiring.emit_pause_captured_state_ledger_entry(
            workflow_id=workflow_id,
            step_id=step_id,
            pause_event=pause_event,
            actor=self.actor,
        )
        return EnginePauseCaptureEmission(
            pause_event=pause_event,
            write_result=write_result,
        )

    async def attempt_resume(
        self,
        *,
        engine_class: EngineClass,
        workflow_id: str,
        run_id: str,
        step_id: str,
        resume_event_id: str,
        resume_attempt_count: int,
        resume_at: str,
        resume_request_actor: ActorIdentity | None = None,
    ) -> EngineResumeAttemptEmission:
        """Attempt engine-layer resume and emit `cp.resume-attempted`.

        Reads the durable substrate bound for ``engine_class`` at the run-scoped key
        (identical composition to ``capture_pause``); the ledger entry keeps the
        PLAIN ``workflow_id``.
        """
        attempt = ResumeAttempt(
            paused_workflow_id=run_scoped_substrate_key(workflow_id, run_id),
            resume_at=resume_at,
            resume_request_actor=resume_request_actor or self.actor,
        )
        with bind_engine_pause_resume_substrate(self._substrate_for(engine_class)):
            resume_outcome = attempt_resume(attempt)

        write_result = await self.wiring.emit_resume_attempted_state_ledger_entry(
            workflow_id=workflow_id,
            step_id=step_id,
            resume_event_id=resume_event_id,
            resume_attempt_count=resume_attempt_count,
            resume_outcome=resume_outcome,
            actor=self.actor,
        )
        return EngineResumeAttemptEmission(
            resume_outcome=resume_outcome,
            write_result=write_result,
        )
