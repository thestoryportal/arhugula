"""U-RT-124 — R-CXA-2 engine-layer go-live for RECONCILER_LOOP (E-impl-3c).

The NEW non-live (in-memory/filesystem) reconciler e2e — the buildable AC for
U-RT-124. It proves, BY EXECUTION (not grep), that the full production chain for
the RECONCILER_LOOP path fires:

    driver RECONCILER_LOOP pause-trigger (U-CP-97 firing branch)
      -> RuntimeEngineRecoveryLoop.capture_pause / .attempt_resume
        -> emit_pause_captured_state_ledger_entry (C-CP-49)
           / emit_resume_attempted_state_ledger_entry (C-CP-50)
          -> the F2 state-ledger

lands `cp.pause-captured` / `cp.resume-attempted` against the DURABLE U-RT-123
etcd-style reconciler substrate (U-RT-124 binds it engine-class-aware) — giving
`RuntimeEngineRecoveryLoop` its SECOND durable production driver (the reconciler
path; WAL was the first at U-RT-122).

This is a NEW file (the U-RT-122 `test_u_rt_95` is WAL-only and explicitly
EXCLUDES RECONCILER_LOOP; it is NOT touched/repurposed here). The live-K8s e2e +
§7.2 deployment-admissibility are SEPARATE deferred deployment-surface gates
(§6 O-RT-5/6); this unit's buildable proof is non-live filesystem.

## What each test pins (U-RT-124 AC)

- ``test_reconciler_loop_engine_recovery_pause_resume_cycle`` — the go-live
  pause/resume (reconverge) cycle: C-CP-49/C-CP-50 entries land with the
  engine-layer action_ids (distinct from the workflow-layer
  `cp.pause-resume-protocol`); ZERO `CPAuditLedgerEntry` greenfield (CP §16.5.9
  invariant 5). The TAUTOLOGY GUARD is built in: reaching PAUSED (not
  `EngineClassNotYetMaterializedError`) proves RECONCILER_LOOP is materialized
  (`_IN_SCOPE_ENGINE_CLASSES`, U-CP-96) AND the C-CP-49/50 entries landing proves
  the U-CP-97 firing branch actually ran — a tautological test that did neither
  would pass without the feature; this one cannot.
- ``test_reconciler_durable_across_restart`` — F3 floor (i): a FRESH substrate
  instance over the same on-disk reconciler store reconverges a pause the
  PRODUCTION driver captured (the #475 durability property; RESUME_CLEAN on the
  fresh instance's first claim of the captured revision).
- ``test_no_cross_contamination_reconciler_vs_wal_stores`` — the LOAD-BEARING new
  constraint vs U-RT-122: a RECONCILER + a WAL pause driven in ONE process land in
  their RESPECTIVE durable stores — the reconciler pause is NOT in the WAL
  segment-log and the WAL pause is NOT in the reconciler store.
- ``test_reconciler_completed_run_retry_is_idempotent`` — C-CP-07 §7.4 floor (ii)
  parity with WAL: a fully-completed run re-driven with the SAME run_id is idempotent
  SUCCESS (the `resume_at < len(steps)` completed-run guard), NOT a CAS-abort FAILED.
- ``test_reconciler_incomplete_crash_retry_fails_closed`` — U-CP-97's fail-closed AC
  by execution: an INCOMPLETE run whose revision was already claimed (crash mid-resume)
  fails closed (ABORT_REVALIDATION_FAILED → FAILED → §22.1 HITL) — the ratified U-RT-123
  F-1 limit, DISTINCT from the completed-run idempotent path.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from harness_core.identity import StepID
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ActorIdentity, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import (
    CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED,
    ResumeAttempt,
    ResumeOutcomeKind,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import execute_workflow
from harness_cp.workflow_driver_types import RunStatus, StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Identifier
from harness_is.state_ledger_write import read_ledger
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.engine_recovery_loop import run_scoped_substrate_key
from harness_runtime.lifecycle.reconciler_pause_resume_substrate import (
    ReconcilerEnginePauseResumeSubstrate,
)
from harness_runtime.lifecycle.wal_segment_pause_resume_substrate import (
    WALSegmentEnginePauseResumeSubstrate,
)
from harness_runtime.types import RuntimeConfig

from .conftest import WORKLOAD, build_config

# --- Fixtures (self-contained; the WAL-only test_u_rt_95 is NOT imported) ----

_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)
_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")


def _attach_get_tracer_to_ctx(ctx: Any) -> None:
    """The `patched_runtime` fixture's `FakeTracerProvider` lacks `get_tracer`
    (U-RT-89/95 e2e precedent)."""
    from opentelemetry.trace import NoOpTracer

    ctx.tracer_provider.get_tracer = lambda _name, /: NoOpTracer()  # type: ignore[attr-defined,method-assign]


def _manifest(workflow_id: str, *, engine_class: EngineClass) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WORKLOAD,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=engine_class,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _two_inference_steps() -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep(step_id=StepID("step-0"), step_kind=StepKind.INFERENCE_STEP, step_payload={}),
        WorkflowStep(step_id=StepID("step-1"), step_kind=StepKind.INFERENCE_STEP, step_payload={}),
    )


class _PauseAfterFirstStepDispatcher:
    """Request a pause AFTER step 0 commits — modelling a convergence boundary.

    Step 0 is durably committed to the F2 ledger, THEN a pause is requested before
    the next step. So on resume the driver finds a materialized prefix (resume_at=1)
    + a present engine pause record — the signal the U-CP-97 RESUME firing fires
    `attempt_resume` on. (Setting the flag pre-run would pause at step 0 with
    resume_at=0 and no committed prefix.)
    """

    def __init__(self, pause_flag: Any) -> None:
        self._pause_flag = pause_flag
        self.dispatched: list[str] = []

    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = binding, step_context
        self.dispatched.append(str(step.step_id))
        if len(self.dispatched) == 1:
            self._pause_flag.set()
        return {"step_id": str(step.step_id), "ok": True}


class _SingleKindRegistry:
    def __init__(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: Any) -> Any:
        _ = step_kind
        return self._dispatcher


def _reconciler_dir(config: RuntimeConfig) -> Path:
    return config.repository_root / ".harness" / "engine-recovery-reconciler"


def _wal_segment_dir(config: RuntimeConfig) -> Path:
    return config.repository_root / ".harness" / "engine-recovery-segments"


def _engine_state_summary() -> StateSummary:
    """Minimal valid StateSummary for a fresh probe/restart substrate (capture-only;
    `attempt_resume` does not invoke the provider, so the body is never read on the
    resume path)."""
    return StateSummary(
        relevant_entries=(),
        summary_text="",
        summary_hash="0" * 64,
        idempotency_key=Identifier("reconciler-e2e"),
        external_references=(),
    )


async def _drive_pause(
    ctx: Any,
    *,
    workflow_id: str,
    run_id: str,
    engine_class: EngineClass,
) -> tuple[Any, _PauseAfterFirstStepDispatcher]:
    """Drive a workflow to its engine-layer pause (after step 0 commits).

    `execute_workflow` is sync and its firing branches run `asyncio.run` via
    `_run_protocol_method_sync`, so it MUST run off the test's event loop (a worker
    thread) — the production `api.run` worker-thread posture.
    """
    manifest = _manifest(workflow_id, engine_class=engine_class)
    dispatcher = _PauseAfterFirstStepDispatcher(ctx.pause_requested_flag)
    registry = _SingleKindRegistry(dispatcher)
    ctx.pause_requested_flag.clear()
    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=_two_inference_steps(),
            run_id=run_id,
            ctx=ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,
        )
    )
    return result, dispatcher


@pytest.mark.asyncio
async def test_reconciler_loop_engine_recovery_pause_resume_cycle(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Go-live: a RECONCILER_LOOP workflow drives the engine-layer recovery loop
    through a real pause -> reconverge cycle against the DURABLE reconciler store.
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    # The recovery loop binds the DURABLE reconciler substrate for RECONCILER_LOOP
    # (U-RT-124 engine-class-aware bind), not the in-memory Deterministic placeholder.
    assert ctx.engine_recovery_loop is not None
    assert isinstance(
        ctx.engine_recovery_loop.substrate_by_engine_class[EngineClass.RECONCILER_LOOP],
        ReconcilerEnginePauseResumeSubstrate,
    )
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle
    manifest = _manifest("wf-recon", engine_class=EngineClass.RECONCILER_LOOP)
    steps = _two_inference_steps()
    dispatcher = _PauseAfterFirstStepDispatcher(ctx.pause_requested_flag)
    registry = _SingleKindRegistry(dispatcher)

    # --- Run 1: pause at the convergence boundary (after step 0 commits). ------
    # Reaching PAUSED (rather than EngineClassNotYetMaterializedError at the
    # `:1397` gate) is the by-execution proof RECONCILER_LOOP is materialized
    # (U-CP-96 `_IN_SCOPE_ENGINE_CLASSES`) — the tautology guard's first half.
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-recon",
            ctx=ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,
        )
    )
    assert paused.status is RunStatus.PAUSED, (
        f"expected PAUSED, got {paused.status}; fail_class={paused.fail_class}"
    )
    assert paused.terminal_step_index == 0  # step 0 committed before the pause
    assert dispatcher.dispatched == ["step-0"]

    after_pause = [entry.action_id for entry in read_ledger(handle)]
    # The U-CP-97 PAUSE firing branch actually ran (cp.pause-captured landed) — the
    # tautology guard's second half. Engine layer != workflow layer.
    assert "cp.pause-captured" in after_pause
    assert "cp.pause-resume-protocol" not in after_pause
    # The pause is durably on disk in the RECONCILER store (not in-memory).
    assert any(_reconciler_dir(config).glob("*")), "reconciler store has no durable file"

    # --- Run 2: resume -> the committed prefix fires attempt_resume (reconverge).
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-recon",
            ctx=ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,
        )
    )
    assert resumed.status is RunStatus.SUCCESS, (
        f"expected SUCCESS, got {resumed.status}; fail_class={resumed.fail_class}"
    )
    # Only step 1 ran on resume — step 0 (the converged prefix) was NOT re-dispatched.
    assert dispatcher.dispatched == ["step-0", "step-1"]

    after_resume = [entry.action_id for entry in read_ledger(handle)]
    assert "cp.resume-attempted" in after_resume
    # ZERO CPAuditLedgerEntry greenfield: the engine layer emits ONLY the C-CP-49/50
    # engine-layer action_ids, never the workflow-layer cp.pause-resume-protocol
    # (CP §16.5.9 invariant 5).
    assert "cp.pause-resume-protocol" not in after_resume


@pytest.mark.asyncio
async def test_reconciler_durable_across_restart(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """F3 floor (i): a FRESH reconciler substrate over the same on-disk store
    reconverges a pause the PRODUCTION driver captured — the #475 durability
    property, extended to the reconciler store.

    The fresh instance does the FIRST `attempt_resume` of the captured revision
    (RESUME_CLEAN). It is a SEPARATE workflow from the go-live cycle's: the
    reconciler CAS makes the first resume of a revision win and any later resume of
    the SAME revision abort, so the restart proof must own its own capture (not
    re-resume one the production driver already claimed).
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)

    paused, dispatcher = await _drive_pause(
        ctx,
        workflow_id="wf-restart",
        run_id="run-restart",
        engine_class=EngineClass.RECONCILER_LOOP,
    )
    assert paused.status is RunStatus.PAUSED
    assert dispatcher.dispatched == ["step-0"]

    # A fresh substrate instance over the same durable reconciler dir resumes the
    # convergence the production driver captured — RESUME_CLEAN (first claim of the
    # captured revision). The production driver stored the record at the run-scoped
    # key; the fresh substrate resumes it with the SAME key.
    fresh = ReconcilerEnginePauseResumeSubstrate(
        journal_dir=_reconciler_dir(config),
        state_summary_provider=_engine_state_summary,
    )
    outcome = fresh.attempt_resume(
        ResumeAttempt(
            paused_workflow_id=run_scoped_substrate_key("wf-restart", "run-restart"),
            resume_at="2026-06-15T12:00:00Z",
            resume_request_actor=ActorIdentity("reconciler-restart"),
        )
    )
    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN, (
        f"fresh-instance reconverge did not RESUME_CLEAN: {outcome.outcome_kind}"
    )


@pytest.mark.asyncio
async def test_no_cross_contamination_reconciler_vs_wal_stores(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """LOAD-BEARING (vs U-RT-122): a RECONCILER + a WAL pause driven in ONE process
    land in their RESPECTIVE durable stores — the reconciler pause is NOT in the WAL
    segment-log and the WAL pause is NOT in the reconciler store.

    Enforced by construction (distinct journal dirs in the engine-class registry);
    asserted by execution (a fresh substrate over each dir sees only its own class's
    record).
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)

    recon_paused, _ = await _drive_pause(
        ctx,
        workflow_id="wf-recon-x",
        run_id="run-recon-x",
        engine_class=EngineClass.RECONCILER_LOOP,
    )
    assert recon_paused.status is RunStatus.PAUSED
    wal_paused, _ = await _drive_pause(
        ctx, workflow_id="wf-wal-x", run_id="run-wal-x", engine_class=EngineClass.WAL_SEGMENT
    )
    assert wal_paused.status is RunStatus.PAUSED

    recon_key = run_scoped_substrate_key("wf-recon-x", "run-recon-x")
    wal_key = run_scoped_substrate_key("wf-wal-x", "run-wal-x")
    fresh_recon = ReconcilerEnginePauseResumeSubstrate(
        journal_dir=_reconciler_dir(config),
        state_summary_provider=_engine_state_summary,
    )
    fresh_wal = WALSegmentEnginePauseResumeSubstrate(
        journal_dir=_wal_segment_dir(config),
        state_summary_provider=_engine_state_summary,
    )

    # Each engine class's pause lives ONLY in its own durable store.
    assert fresh_recon.has_pause_record(recon_key) is True
    assert fresh_recon.has_pause_record(wal_key) is False, (
        "WAL pause leaked into the reconciler store"
    )
    assert fresh_wal.has_pause_record(wal_key) is True
    assert fresh_wal.has_pause_record(recon_key) is False, (
        "reconciler pause leaked into the WAL store"
    )


@pytest.mark.asyncio
async def test_reconciler_completed_run_retry_is_idempotent(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Floor (ii) parity with WAL: a FULLY-COMPLETED reconciler run RE-DRIVEN with the
    SAME run_id (at-least-once redelivery / crash-after-completion) is idempotent
    SUCCESS — NOT a CAS-abort FAILED.

    The completed-run guard (`resume_at < len(steps)`) skips the engine-resume fire when
    every step is already committed, so the empty step loop returns SUCCESS instead of
    claiming the already-claimed revision again → ABORT. Without the guard the reconciler
    CAS lease would spuriously FAIL a finished run; WAL (re-resumable substrate) is
    already idempotent here, which is the parity this restores (C-CP-07 §7.4 floor (ii)
    "idempotency-keyed exactly-once via the F2 ledger").
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    manifest = _manifest("wf-done", engine_class=EngineClass.RECONCILER_LOOP)
    steps = _two_inference_steps()
    dispatcher = _PauseAfterFirstStepDispatcher(ctx.pause_requested_flag)
    registry = _SingleKindRegistry(dispatcher)

    def _run() -> Any:
        return execute_workflow(
            manifest_entry=manifest,
            steps=steps,
            run_id="run-done",
            ctx=ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,
        )

    paused = await asyncio.to_thread(_run)
    assert paused.status is RunStatus.PAUSED
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(_run)
    assert resumed.status is RunStatus.SUCCESS, (
        f"expected SUCCESS, got {resumed.status}; fail_class={resumed.fail_class}"
    )
    # r3: re-drive the COMPLETED run (same run_id) — idempotent SUCCESS, not CAS-FAILED.
    ctx.pause_requested_flag.clear()
    retried = await asyncio.to_thread(_run)
    assert retried.status is RunStatus.SUCCESS, (
        f"completed-run retry must be idempotent SUCCESS, got {retried.status}; "
        f"fail_class={retried.fail_class}"
    )
    assert retried.fail_class is None
    # No step re-dispatched on the retry (resume_at == len(steps); empty step loop).
    assert dispatcher.dispatched == ["step-0", "step-1"]


@pytest.mark.asyncio
async def test_reconciler_incomplete_crash_retry_fails_closed(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """U-CP-97 fail-closed-by-execution AC: an INCOMPLETE run whose convergence revision
    was already claimed (a crash mid-resume) FAILS CLOSED on re-drive — never proceeds
    past unrecoverable engine state.

    Pre-claim the captured revision with a fresh substrate (simulating the crash-mid-
    resume window), then drive the production resume of the STILL-INCOMPLETE run
    (resume_at=1 < len(steps)=2 → the engine resume FIRES) → a SECOND claim of the
    revision → ABORT_REVALIDATION_FAILED → the driver returns FAILED with the CP
    fail-class (→ §22.1 HITL). This is the ratified U-RT-123 "HONEST F-1 limit",
    DISTINCT from the completed-run idempotent path: it is gated IN (incomplete), where
    the completed-run retry is gated OUT (nothing to reconverge).
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    paused, _ = await _drive_pause(
        ctx, workflow_id="wf-crash", run_id="run-crash", engine_class=EngineClass.RECONCILER_LOOP
    )
    assert paused.status is RunStatus.PAUSED

    # Simulate a crash mid-resume: a fresh substrate claims the captured revision while
    # the production run is still incomplete (step 1 uncommitted).
    fresh = ReconcilerEnginePauseResumeSubstrate(
        journal_dir=_reconciler_dir(config),
        state_summary_provider=_engine_state_summary,
    )
    claimed = fresh.attempt_resume(
        ResumeAttempt(
            paused_workflow_id=run_scoped_substrate_key("wf-crash", "run-crash"),
            resume_at="2026-06-15T12:00:00Z",
            resume_request_actor=ActorIdentity("crash-claimer"),
        )
    )
    assert claimed.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN  # first claim wins

    # Drive the production resume of the still-incomplete run → fires → second claim →
    # ABORT → FAIL CLOSED (before any step re-dispatch).
    ctx.pause_requested_flag.clear()
    manifest = _manifest("wf-crash", engine_class=EngineClass.RECONCILER_LOOP)
    dispatcher = _PauseAfterFirstStepDispatcher(ctx.pause_requested_flag)
    registry = _SingleKindRegistry(dispatcher)
    failed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=_two_inference_steps(),
            run_id="run-crash",
            ctx=ctx,
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,
        )
    )
    assert failed.status is RunStatus.FAILED, (
        f"expected FAILED (fail-closed), got {failed.status}; fail_class={failed.fail_class}"
    )
    assert failed.fail_class == CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED
