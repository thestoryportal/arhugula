"""Durable variant of the CP ``PauseResumeProtocol`` (R-CC-1 arc #3, step 2).

The workflow-layer sibling of how ``JournalEnginePauseResumeSubstrate`` (#475) is
the durable variant of the engine substrate: a subclass of the CP-canonical
``harness_cp.pause_resume_protocol.PauseResumeProtocol`` whose
``capture_pause_snapshot`` ALSO persists the composed ``PauseSnapshot`` to a
``JournalWorkflowPauseStore`` before returning it. Everything else
(``attempt_resume``, the snapshot composition + ``snapshot_hash``) is inherited
unchanged.

Subclassing (not wrapping) is deliberate: the frozen ``HarnessContext``
``pause_resume_protocol`` field is ``is_instance_of``-validated as
``PauseResumeProtocol``, and the workflow driver consumes the protocol through a
duck-``cast`` — a subclass satisfies BOTH with **no edit to the frozen-context
field nor to the workflow driver** (design §7b runtime-only posture).

Resume reads happen at the ``api.resume`` boundary (a fresh
``JournalWorkflowPauseStore`` over the resolved journal dir resolved purely from
``config``, so it works across a restart), NOT inside ``attempt_resume`` —
``attempt_resume`` is the in-driver resume-admission gate and is inherited
verbatim. This subclass therefore only adds the *write* side (persist-on-capture);
the read side lives at the ``api.resume`` boundary.

Authority: runtime spec v1.46 §14.14 (durable variant of the C-RT-24
PauseResumeProtocol factory output); design
``r-cc-1-arc-3-workflow-durable-resume-design-v1.md`` §7b.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness_cp.pause_resume_protocol import PauseContextReader, PauseResumeProtocol
from harness_cp.pause_resume_protocol_types import (
    EffectFenceResumeState,
    EvaluatorOptimizerResumeState,
    FanOutResumeState,
    HandoffResumeState,
    OrchestratorEffectFencePausedResumeState,
    PauseSnapshot,
    PeerFanOutResumeState,
    WorkflowPauseReason,
)

if TYPE_CHECKING:
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
    )

__all__ = ["DurablePauseResumeProtocol"]


class DurablePauseResumeProtocol(PauseResumeProtocol):
    """A ``PauseResumeProtocol`` that durably persists captured snapshots.

    Constructed by the stage-5 factory with the same constructor refs as the
    bare CP protocol plus a ``JournalWorkflowPauseStore`` (co-located under the
    resolved ``STATE_LEDGER`` dir). ``isinstance(self, PauseResumeProtocol)`` is
    ``True``, so the frozen ``HarnessContext`` accepts it and the workflow driver
    consumes it unchanged.
    """

    def __init__(
        self,
        *,
        state_ledger_writer: object,
        state_ledger_reader: object,
        pause_context_reader: PauseContextReader,
        store: JournalWorkflowPauseStore,
    ) -> None:
        super().__init__(
            state_ledger_writer=state_ledger_writer,
            state_ledger_reader=state_ledger_reader,
            pause_context_reader=pause_context_reader,
        )
        self._store = store

    async def capture_pause_snapshot(
        self,
        workflow_id: str,
        run_id: str,
        step_index: int,
        pause_reason: WorkflowPauseReason,
        *,
        fan_out_resume: FanOutResumeState | None = None,
        peer_fan_out_resume: PeerFanOutResumeState | None = None,
        handoff_resume: HandoffResumeState | None = None,
        evaluator_optimizer_resume: EvaluatorOptimizerResumeState | None = None,
        effect_fence_resume: EffectFenceResumeState | None = None,
        orchestrator_effect_fence_resume: OrchestratorEffectFencePausedResumeState | None = None,
    ) -> PauseSnapshot:
        """Compose the snapshot via the parent, then durably persist it.

        The snapshot is journaled BEFORE it is returned to the driver, so a crash
        after capture (but before the caller serializes the ``RunResult``) still
        leaves a resumable record on disk for ``api.resume(resume_handle=...)``.

        ``fan_out_resume`` (B-FANOUT-PAUSE, ORCHESTRATOR_WORKERS),
        ``peer_fan_out_resume`` (B-FANOUT-PAUSE-PARALLELIZATION), ``handoff_resume``
        (B-HANDOFF-PAUSE, DECENTRALIZED_HANDOFF), and ``evaluator_optimizer_resume``
        (B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER, EVALUATOR_OPTIMIZER) are forwarded to the
        parent so a durable `cascade_policy=pause` snapshot carries (and journals) its
        resume state — without forwarding, the durable resume path would silently drop it
        (and the new kwarg would raise `TypeError` at the driver's capture call under
        durable config).
        """
        snapshot = await super().capture_pause_snapshot(
            workflow_id,
            run_id,
            step_index,
            pause_reason,
            fan_out_resume=fan_out_resume,
            peer_fan_out_resume=peer_fan_out_resume,
            handoff_resume=handoff_resume,
            evaluator_optimizer_resume=evaluator_optimizer_resume,
            effect_fence_resume=effect_fence_resume,
            orchestrator_effect_fence_resume=orchestrator_effect_fence_resume,
        )
        self._store.capture(snapshot)
        return snapshot
