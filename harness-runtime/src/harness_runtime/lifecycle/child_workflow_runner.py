"""In-process recursive child sub-workflow invocation primitive (U-RT-59 AC #7).

Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7.4 (C-RT-17 contract). The
"child workflow runner" callable is injected at `RuntimeSubAgentDispatcher`
construction; the composer invokes it at §14.7.2 step 6 to re-enter
`execute_workflow()` for the child sub-workflow body.

**v1.6 MVP composition (§14.7.4).**

- The child shares the parent's `HarnessContext` for substrate access (state
  ledger, tracer provider, audit ledger writer, retry/breaker registry,
  providers, sandbox tier dispatcher). Full child-context isolation is a
  v1.7+ scope question (CP-AL-1-adjacent boundary).
- The child's spans nest inside the current `subagent.span` via OTel context
  propagation (the runner is invoked from within the composer's
  `start_as_current_span("subagent.span")` block, so the child's
  `workflow.start` becomes a span-context child of `subagent.span`).
- The child's audit-ledger entries write to the same ledger via the same
  `ctx.audit_writer` (no separate ledger primitive at v1.6).
- The child's `RunResult` is returned verbatim to the composer.

**Sync surface (operator-ratified 2026-05-20).** Spec §14.7.4 declares the
`ChildWorkflowRunner` Protocol with `async def __call__`; operator ratified
sync end-to-end per the Stage 1 Protocol freeze at `workflow_driver.py:175`
(`StepDispatcher.dispatch` is sync; `execute_workflow` is sync). Rolled into
the U-RT-59 Class 3 spec-prose-drift note at landing. Recursive sync re-entry
is sufficient at v1.6 MVP because the spec-pinned scope is "single-sub-agent
within linear parent" — no fan-out concurrency at the composer level.

**`default_model_binding` additive extension.** Spec §14.7.4 lists 5 kwargs
on the runner Protocol (`workflow_id`, `manifest_entry`, `steps`,
`handoff_context`, `descent`). `execute_workflow` requires a
`default_model_binding` for the child's per-step binding resolution per
C-CP-06 §6.2; the composer forwards its parent `binding.model_binding`
(per C-CP-13 §13.3 brief-authoring inheritance MVP reading). Additive vs
spec; rolled into the same Class 3 note.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Protocol, cast, runtime_checkable

from harness_cp.cp_shared_types import ModelBinding
from harness_cp.handoff_context import HandoffContext
from harness_cp.pause_resume_protocol_types import PauseSnapshot
from harness_cp.sub_agent_gate_level_descent import SubAgentGateLevelDescent
from harness_cp.workflow_driver import DriverContext as _CpDriverContext
from harness_cp.workflow_driver import execute_workflow
from harness_cp.workflow_driver_types import RunResult, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

from harness_runtime.types import HarnessContext

__all__ = [
    "ChildWorkflowRunner",
    "compose_child_workflow_runner",
]


@runtime_checkable
class ChildWorkflowRunner(Protocol):
    """In-process recursive sub-workflow invocation surface (§14.7.4).

    Constructed at bootstrap stage 5 via `compose_child_workflow_runner(ctx)`;
    injected into `RuntimeSubAgentDispatcher.__init__`. The composer invokes
    it at §14.7.2 step 6 to run the child sub-workflow.

    Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7.4 with two sync-vs-spec
    adjustments documented in the module docstring: (1) sync `__call__`
    instead of `async def __call__`; (2) additive `default_model_binding`
    kwarg.
    """

    def __call__(
        self,
        *,
        workflow_id: str,
        manifest_entry: WorkflowManifestEntry,
        steps: Sequence[WorkflowStep],
        handoff_context: HandoffContext,
        descent: SubAgentGateLevelDescent,
        default_model_binding: ModelBinding,
        pause_snapshot_input: PauseSnapshot | None = None,
        child_run_id_seed: str | None = None,
    ) -> RunResult:
        """Run the child sub-workflow and return its terminal `RunResult`.

        B-HIERARCHICAL-PAUSE (R-FS-1): `pause_snapshot_input` (additive, default
        `None`) — when the parent fan-out is RESUMING a previously-paused child, the
        child's own `PauseSnapshot` is threaded here so the child re-enters at its
        cursor (`execute_workflow(pause_snapshot_input=...)`) rather than re-running
        from scratch. `None` on a first (non-resume) child dispatch → byte-identical
        to the pre-arc behavior.

        B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1): `child_run_id_seed`
        (additive, default `None`) — a DETERMINISTIC first-dispatch child run_id
        (vs the legacy fresh `uuid`). The composer derives it from the spawning
        SUB_AGENT_DISPATCH worker's stable, recoverable per-branch idempotency key
        (`step_context.parent_idempotency_key`) + `child_workflow_id`, so it
        RE-DERIVES IDENTICALLY when the parent fan-out re-dispatches a maybe-ran
        SUB_AGENT_DISPATCH worker on crash-resume. That makes the child's durable
        store + effect-fence reserves RECOVERABLE under a stable key — a plain
        re-dispatch (no `pause_snapshot_input`) re-enters the child, whose OWN
        crash-resume auto-resumes from the shared durable store (at-most-once is
        compositional: it bottoms out at the child's recursively-classified steps).
        The composer passes it ONLY when the child is recoverable
        (`subagent_child_recoverable` — `{ESR,WAL}` ∧ LINEAR ∧ leaf); a
        non-recoverable child gets `None` → legacy fresh-`uuid` (no auto-resume,
        pre-existing behavior — no suffix-only-reconstruction corruption). Ignored
        on a resume (`pause_snapshot_input` non-None reuses the snapshot's run_id).
        """
        ...


def compose_child_workflow_runner(ctx: HarnessContext) -> ChildWorkflowRunner:
    """Build a `ChildWorkflowRunner` closing over the parent `HarnessContext`.

    Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7.4 "Composer module residence":
    bootstrap stage 5 calls this factory + injects the result into
    `RuntimeSubAgentDispatcher` construction.

    The returned callable re-enters `execute_workflow()` (the same C-CP-25
    §25.3 driver loop the top-level workflow uses per C-RT-08). Child shares
    parent `HarnessContext` per v1.6 MVP — substrate access (state ledger,
    audit writer, tracer provider, retry/breaker registry, providers,
    sandbox tier dispatcher) flows through `ctx` unchanged.

    The child's `step_dispatchers` is the parent's `ctx.step_dispatchers`
    registry — recursive `SUB_AGENT_DISPATCH` steps in the child route
    through the same composer (unbounded stack depth at v1.6 MVP; bounded
    by the operator-authored workflow shape).

    Parameters
    ----------
    ctx
        The parent `HarnessContext`. The runner closes over this context;
        `ctx.step_dispatchers` must be populated (which it is post stage 5
        per the bootstrap orchestrator's stage ordering).
    """

    def _runner(
        *,
        workflow_id: str,
        manifest_entry: WorkflowManifestEntry,
        steps: Sequence[WorkflowStep],
        handoff_context: HandoffContext,
        descent: SubAgentGateLevelDescent,
        default_model_binding: ModelBinding,
        pause_snapshot_input: PauseSnapshot | None = None,
        child_run_id_seed: str | None = None,
    ) -> RunResult:
        # B-HIERARCHICAL-PAUSE — on a RESUME (pause_snapshot_input non-None), FAIL CLOSED
        # if the snapshot's workflow_id does not match the child being invoked (Codex
        # [P2], mirroring the root `api.resume` workflow-id guard): if the parent is
        # edited between pause + resume so the same SUB_AGENT_DISPATCH step_id points to
        # a DIFFERENT child workflow, the parent resume guard still passes, and applying
        # the old child's cursor/run_id to the new child would silently corrupt lineage.
        if pause_snapshot_input is not None and pause_snapshot_input.workflow_id != workflow_id:
            raise ValueError(
                "child resume workflow-id mismatch: snapshot.workflow_id="
                f"{pause_snapshot_input.workflow_id!r}, resume child workflow_id="
                f"{workflow_id!r} (the paused child's snapshot cannot resume a different "
                "child workflow)"
            )
        # Reuse the paused child's ORIGINAL run_id (not a fresh uuid) so the resumed
        # child's run/step idempotency keys + ledger/audit lineage stay coherent with
        # the original run — the same discipline the root resume path follows
        # (it threads `snapshot.run_id`). A fresh id on resume would re-key the child's
        # per-step idempotency + sever its run lineage (Codex [P2]).
        #
        # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — on a FIRST dispatch
        # (no `pause_snapshot_input`), prefer the composer-supplied DETERMINISTIC
        # `child_run_id_seed` over a fresh `uuid`. The seed is derived from the
        # spawning worker's stable, recoverable per-branch idempotency key, so a
        # parent-crash re-dispatch of a maybe-ran SUB_AGENT_DISPATCH worker
        # RE-DERIVES the SAME child run_id → the child's durable store + effect-fence
        # reserves are recoverable → the child's own crash-resume auto-resumes
        # (at-most-once compositional). The composer passes a seed ONLY for a
        # recoverable child (`{ESR,WAL}` ∧ LINEAR ∧ leaf); a non-recoverable child
        # gets `None` → legacy fresh-`uuid` (byte-identical to pre-arc; no auto-resume
        # so no suffix-only-reconstruction corruption).
        child_run_id = (
            pause_snapshot_input.run_id
            if pause_snapshot_input is not None
            else child_run_id_seed
            if child_run_id_seed is not None
            else uuid.uuid4().hex
        )
        # The CP driver consumes `ctx` via its structural `DriverContext`
        # Protocol (subset of HarnessContext). Cast for the type layer; the
        # runtime objects satisfy both Protocols — same pattern as
        # `harness_runtime.api.run` per the existing api.py:386 invocation.
        #
        # B-HIERARCHICAL-PAUSE — forward the child's resume snapshot (None on a
        # first dispatch) so a resumed child re-enters at its own cursor.
        #
        # B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT (R-FS-1) — opt the child run into
        # final_state reconstruction: on a durable-engine-class (EVENT_SOURCED_REPLAY /
        # WAL_SEGMENT) child resume over a committed prefix, the CP driver returns a
        # suffix-only `final_state` (the loop starts at `resume_at` with `accumulated`
        # empty); the parent fold (`sub_agent_dispatch` SUCCESS → `step_output =
        # child_result.final_state`; the B-HIERARCHICAL-PAUSE re-enter fold) would
        # otherwise consume that truncated state and silently corrupt the parent
        # aggregate. The opt-in seeds the committed prefix from the durable output store
        # so the child's `final_state` reconstructs the COMPLETE terminal state. ALL FOUR
        # durable resumable engine classes reconstruct (the EngineOutputStore is
        # class-agnostic: ESR/WAL #766, SAVE_POINT_CHECKPOINT v1.79 #779, RECONCILER_LOOP
        # v1.80 #781); only PURE_PATTERN_NO_ENGINE (non-durable) degrades to suffix-only.
        # A first (non-resume) dispatch is unaffected.
        # Top-level runs (`harness_runtime.api.run`) do NOT pass this → their accepted
        # suffix-only resume semantic is untouched (the fork-bearing top-level
        # reconstruction is a separate registered arc).
        return execute_workflow(
            manifest_entry,
            steps,
            child_run_id,
            cast(_CpDriverContext, ctx),
            default_model_binding=default_model_binding,
            step_dispatchers=cast(Any, ctx.step_dispatchers),
            pause_snapshot_input=pause_snapshot_input,
            reconstruct_final_state=True,
        )

    return _runner
