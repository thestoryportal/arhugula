"""U-RT-95 — Driver catch + e2e for HITL durable-async pause-trigger cycle.

Implements runtime plan v2.25 §7.3 U-RT-95 ACs (preserved v2.23 #1-#6 +
v2.24 #7 + v2.25 #8/#9). Exercises the §14.8.8.4 driver-side signal
handling discipline + the §14.8.8.1 step 0 OR-form precondition at
production bootstrap substrate.

## 7-case path matrix per runtime plan v2.25 §7.3

| Path | Operator config              | Cell synchrony   | Expected outcome                              |
|------|------------------------------|------------------|-----------------------------------------------|
| (i)  | WAL_SEGMENT engine class     | DURABLE_ASYNC    | Driver WAL_SEGMENT pause-trigger fires the    |
|      |                              |                  | engine-layer recovery loop (U-CP-95):         |
|      |                              |                  | capture_pause -> cp.pause-captured (C-CP-49)  |
|      |                              |                  | -> RunStatus.PAUSED; on resume attempt_resume |
|      |                              |                  | -> cp.resume-attempted (C-CP-50) against the  |
|      |                              |                  | durable WAL segment-log substrate (U-RT-121). |
|      |                              |                  | R-CXA-2 CP->IS engine-layer seam LIVE.        |
| (iii)| neither                      | (any)            | §14.8.8.1 step 0 precondition fails (both     |
|      |                              |                  | sides None) → sync-blocking fall-through      |
| (v)  | webhook only                 | (any)            | §14.8.8.1 step 0 OR-form arm fails on         |
|      |                              |                  | pause_resume_protocol is None → sync          |
| (vi) | pause-resume only            | (any)            | §14.8.8.1 step 0 OR-form arm fails on         |
|      |                              |                  | webhook_delivery_composer is None → sync      |
| (vii)| (any)                        | (n/a)            | Bare StepEffectiveBinding without persona_tier|
|      |                              |                  | → Pydantic ValidationError at construction    |
|      |                              |                  | (CP spec v1.17 §6.5 regression gate)          |

Paths (ii) resume-consume-cycle + (iv) webhook-exhausted are deferred to a
follow-on arc per FM-2 — they require richer HTTP test-double + resume
substrate beyond the empty-marker v1.26 config.

Path (i) is MATERIALIZED (R-FS-1 E-impl-2 / U-RT-122): it drives a WAL_SEGMENT
workflow through the engine-layer recovery-loop pause/resume cycle (the
DURABLE_ASYNC cell per CP §18.1) against the durable U-RT-121 segment-log
substrate, asserting the C-CP-49/C-CP-50 state-ledger entries land with the
engine-layer action_ids (distinct from the workflow-layer
`cp.pause-resume-protocol`). It supersedes the prior HITL-composer framing of
path (i): the workflow-layer HITL durable-async signal cycle
(HITLPauseRequestedSignal at step 4-bis) is unit-tested separately
(`harness-cp/tests/test_workflow_driver.py` driver catch +
`harness-runtime/tests/test_lifecycle_hitl_gate_composer.py` composer body);
this e2e exercises the *engine-layer* C-CP-22 surface, which is what the
DURABLE_ASYNC engine class (WAL_SEGMENT) materialization actually activates. See
`.harness/r-fs-1-e-impl-2-finding.md`.

## Verification-shape discipline

Per `[[verification-shape-sharpened-grep-vs-e2e]]` (batch-16 §6 + applied at
batch-17 U-RT-85 + batch-18 U-RT-89): the test uses production
`run_bootstrap` orchestrator + production `execute_workflow(...)` invocation
path. The HITL composer is wired at stage-5 LOOP_INIT per the production
binding chain landed at L9-quaterdecies (U-RT-96/97/98) + L9-undecies
(U-RT-87/88/89). NO `_FakeCtx` shortcuts; NO `_MutableHarnessContext`
test-local mutation.
"""

from __future__ import annotations

import asyncio
import json
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
    CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION,
    ResumeAttempt,
    ResumeOutcomeKind,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import execute_workflow
from harness_cp.workflow_driver_types import RunStatus, StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Identifier
from harness_is.state_ledger_write import read_ledger
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.engine_recovery_loop import run_scoped_substrate_key
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.lifecycle.wal_segment_pause_resume_substrate import (
    WALSegmentEnginePauseResumeSubstrate,
)
from harness_runtime.lifecycle.webhook_delivery_composer_types import (
    WebhookDeliveryComposerConfig,
)
from harness_runtime.types import RuntimeConfig

from .conftest import WORKLOAD, build_config

# --- Test fixtures ----------------------------------------------------------


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


class _NoopDispatcher:
    def dispatch(
        self,
        binding: Any,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        _ = binding, step_context
        return {"step_id": str(step.step_id), "ok": True}


class _SingleKindRegistry:
    def __init__(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: Any) -> Any:
        _ = step_kind
        return self._dispatcher


def _attach_get_tracer_to_ctx(ctx: Any) -> None:
    """Per U-RT-89 e2e precedent (`test_u_rt_89_pause_resume_full_execution_path.py`)
    — the `patched_runtime` fixture's `FakeTracerProvider` lacks `get_tracer`."""
    from opentelemetry.trace import NoOpTracer

    ctx.tracer_provider.get_tracer = lambda _name, /: NoOpTracer()  # type: ignore[attr-defined,method-assign]


def _manifest(
    workflow_id: str,
    *,
    persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER,
    engine_class: EngineClass = EngineClass.PURE_PATTERN_NO_ENGINE,
) -> WorkflowManifestEntry:
    """Build a manifest with materialized engine class.

    Default (SOLO_DEVELOPER + PURE_PATTERN_NO_ENGINE) maps to SYNC_BLOCKING
    per CP §18.1. WAL_SEGMENT (the canonical DURABLE_ASYNC cell per CP §18.1)
    is materialized at runtime as of R-FS-1 E-impl-2 (U-CP-94) — path (i)
    drives it through the engine-layer recovery loop. RECONCILER_LOOP's
    resumption semantics are materialized at the CP gate as of R-FS-1 E-impl-3a
    (U-CP-96 — added to `_IN_SCOPE_ENGINE_CLASSES`, so the gate no longer raises),
    but its engine-layer recovery-loop firing + the hand-rolled etcd-style
    CAS-lease substrate are E-impl-3b (U-CP-97 + U-RT-123/124) and this WAL-only
    path (i) e2e does NOT exercise RECONCILER_LOOP (the other §18.1 DURABLE_ASYNC
    cell gets its own non-live recovery e2e at U-RT-124).
    """
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WORKLOAD,
        persona_tier=persona_tier,
        engine_class=engine_class,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _single_inference_step() -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep(
            step_id=StepID("step-0"),
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={"index": 0},
        ),
    )


def _config_joint_opt_in(tmp_path: Path) -> RuntimeConfig:
    """Both pause-resume + webhook opt-in (path (i) substrate)."""
    base = build_config(tmp_path)
    return base.model_copy(
        update={
            "pause_resume_protocol_config": PauseResumeProtocolConfig.default(),
            "webhook_delivery_composer_config": WebhookDeliveryComposerConfig.default(),
        },
    )


def _config_pause_resume_only(tmp_path: Path) -> RuntimeConfig:
    """pause-resume opt-in; webhook opt-out (path (vi) substrate)."""
    base = build_config(tmp_path)
    return base.model_copy(
        update={"pause_resume_protocol_config": PauseResumeProtocolConfig.default()},
    )


def _config_webhook_only(tmp_path: Path) -> RuntimeConfig:
    """webhook opt-in; pause-resume opt-out (path (v) substrate)."""
    base = build_config(tmp_path)
    return base.model_copy(
        update={"webhook_delivery_composer_config": WebhookDeliveryComposerConfig.default()},
    )


# ---------------------------------------------------------------------------
# Path (iii) — neither binding present → sync-blocking fall-through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_iii_neither_binding_falls_through_to_sync(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Path (iii) — operator opts out of BOTH pause-resume and webhook.
    Production-default state: §14.8.8.1 step 0 OR-form precondition AND-arm
    evaluates False on both sides → composer falls through to sync-blocking
    path. No durable-async signal raised; workflow completes normally.
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    # Production-default opt-out on both sides.
    assert config.pause_resume_protocol_config is None
    assert config.webhook_delivery_composer_config is None
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.pause_resume_protocol is None
    assert ctx.webhook_delivery_composer is None

    # Without an actual HITL placement on the step, the composer body never
    # enters the placement loop — the workflow runs to completion. This
    # verifies that bootstrap-default state (no opt-in) produces a clean
    # workflow execution without surfacing the durable-async substrate.
    manifest = _manifest("wf-path-iii")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())
    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-path-iii",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.SUCCESS, (
        f"expected SUCCESS, got {result.status}; fail_class={result.fail_class}"
    )


# ---------------------------------------------------------------------------
# Path (v) — webhook only; pause_resume_protocol None (operator opt-out).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_v_webhook_only_falls_through_to_sync(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Path (v) — operator binds webhook BUT not pause-resume (v2.24 AC #7).
    §14.8.8.1 step 0 OR-form precondition AND-arm at
    `ctx.pause_resume_protocol is None` evaluates True → falls through to
    sync-blocking. Verifies the orphan-response-bug-prevention closure per
    v1.25 D9 + v1.26 OR-form extension.
    """
    _ = patched_runtime
    config = _config_webhook_only(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.pause_resume_protocol is None
    assert ctx.webhook_delivery_composer is not None

    manifest = _manifest("wf-path-v")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())
    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-path-v",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    # No pause/signal raised — workflow completes via inner dispatcher.
    assert result.status == RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Path (vi) — pause-resume only; webhook_delivery_composer None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_vi_pause_resume_only_falls_through_to_sync(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Path (vi) — operator binds pause-resume BUT not webhook (v2.25 AC #8;
    symmetric to v2.24 AC #7 path (v)).
    §14.8.8.1 step 0 OR-form precondition AND-arm at
    `ctx.webhook_delivery_composer is None` evaluates True → falls through
    to sync-blocking. Verifies the v1.26 canonical-reading amendment
    extending the v1.25 single-binding check to joint-binding requirement.
    """
    _ = patched_runtime
    config = _config_pause_resume_only(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.pause_resume_protocol is not None
    assert ctx.webhook_delivery_composer is None

    manifest = _manifest("wf-path-vi")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())
    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-path-vi",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    # No pause/signal raised — workflow completes via inner dispatcher.
    assert result.status == RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Path (vii) — bare-StepEffectiveBinding regression gate (v2.25 AC #9).
# ---------------------------------------------------------------------------


def test_path_vii_bare_step_effective_binding_without_persona_tier_raises() -> None:
    """Path (vii) — post-CP-v1.17 §6.5, StepEffectiveBinding declares
    `persona_tier: PersonaTier` as a required field. Any future regression
    to a bare binding shape (e.g., test fixture forgetting persona_tier OR
    downgrade to pre-v1.17 carrier shape) must surface as a Pydantic
    ValidationError at construction — NOT a silent fallback to sync-blocking
    via getattr-tolerance. Negative test: attempts bare construction;
    verifies ValidationError raised with `persona_tier` in the error message.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        StepEffectiveBinding(  # type: ignore[call-arg]
            step_id="step-1",
            model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            override_applied=False,
        )
    assert "persona_tier" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Path (i) — DURABLE_ASYNC pause-trigger, MATERIALIZED via the WAL_SEGMENT
# engine-layer recovery loop (R-FS-1 E-impl-2 / U-RT-122; U-CP-94/95 + U-RT-121).
# ---------------------------------------------------------------------------


class _PauseAfterFirstStepDispatcher:
    """Dispatch that requests a pause AFTER the first step commits.

    This models a WAL segment boundary: a segment (step 0) is durably committed
    to the F2 ledger, THEN a pause is requested before the next segment. So on
    resume the driver finds a materialized segment prefix (resume_at=1) — the
    signal U-CP-95 fires `attempt_resume` on. (Setting the flag pre-run would
    pause at step 0 with resume_at=0 and the engine resume would never fire —
    the exact fragility the e2e must rule out, per advisor.)
    """

    def __init__(self, pause_flag: asyncio.Event) -> None:
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


def _two_inference_steps() -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep(step_id=StepID("step-0"), step_kind=StepKind.INFERENCE_STEP, step_payload={}),
        WorkflowStep(step_id=StepID("step-1"), step_kind=StepKind.INFERENCE_STEP, step_payload={}),
    )


def _engine_segment_dir(config: RuntimeConfig) -> Path:
    return config.repository_root / ".harness" / "engine-recovery-segments"


def _engine_state_summary() -> StateSummary:
    """Minimal valid StateSummary for the fresh restart-substrate (capture-only;
    `attempt_resume` does not invoke the provider, so the body is never used on
    the read path — it only satisfies the required constructor arg)."""
    return StateSummary(
        relevant_entries=(),
        summary_text="",
        summary_hash="0" * 64,
        idempotency_key=Identifier("wal-e2e-restart"),
        external_references=(),
    )


@pytest.mark.asyncio
async def test_path_i_wal_segment_engine_recovery_pause_resume_cycle(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Path (i) MATERIALIZED — a WAL_SEGMENT workflow drives the engine-layer
    recovery loop through a real pause→resume cycle by execution (NOT grep).

    Run 1 pauses at the segment boundary: the U-CP-95 driver branch fires
    `ctx.engine_recovery_loop.capture_pause` → `cp.pause-captured` (C-CP-49) →
    RunStatus.PAUSED with step 0 committed. Run 2 resumes: the segment prefix
    (resume_at=1, U-CP-94) fires `attempt_resume` → `cp.resume-attempted`
    (C-CP-50). Both land against the DURABLE U-RT-121 segment-log substrate
    (U-RT-122 factory bind), with engine-layer action_ids DISTINCT from the
    workflow-layer `cp.pause-resume-protocol` (CP §16.5.9 invariant 5; ZERO
    CPAuditLedgerEntry greenfield). The R-CXA-2 CP→IS engine-layer seam is LIVE:
    `RuntimeEngineRecoveryLoop` has its first production driver.
    """
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    # U-RT-122 — the recovery loop is bound against the DURABLE WAL substrate
    # (not the in-memory Deterministic placeholder).
    assert ctx.engine_recovery_loop is not None
    assert isinstance(
        ctx.engine_recovery_loop.substrate_by_engine_class[EngineClass.WAL_SEGMENT],
        WALSegmentEnginePauseResumeSubstrate,
    )

    manifest = _manifest("wf-wal", engine_class=EngineClass.WAL_SEGMENT)
    steps = _two_inference_steps()
    dispatcher = _PauseAfterFirstStepDispatcher(ctx.pause_requested_flag)
    registry = _SingleKindRegistry(dispatcher)
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle

    # --- Run 1: pause at the segment boundary (after step 0 commits). ---------
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-wal",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,  # type: ignore[arg-type]
        )
    )
    assert paused.status is RunStatus.PAUSED, (
        f"expected PAUSED, got {paused.status}; fail_class={paused.fail_class}"
    )
    # Step 0 committed BEFORE the pause (proves the flag fired after step 0, not
    # pre-run — the resume_at>0 prerequisite for the engine resume firing).
    assert paused.terminal_step_index == 0
    assert dispatcher.dispatched == ["step-0"]

    after_pause = [entry.action_id for entry in read_ledger(handle)]
    assert "cp.pause-captured" in after_pause
    assert "cp.pause-resume-protocol" not in after_pause  # engine layer ≠ workflow layer

    # --- Run 2: resume — the committed segment prefix fires attempt_resume. ---
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-wal",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=registry,  # type: ignore[arg-type]
        )
    )
    assert resumed.status is RunStatus.SUCCESS, (
        f"expected SUCCESS, got {resumed.status}; fail_class={resumed.fail_class}"
    )
    # Only step 1 ran on resume — the materialized segment prefix (step 0) was
    # NOT re-dispatched (segment_replay "no re-execution").
    assert dispatcher.dispatched == ["step-0", "step-1"]

    after_resume = [entry.action_id for entry in read_ledger(handle)]
    assert "cp.resume-attempted" in after_resume
    # ZERO CPAuditLedgerEntry greenfield: the engine layer emits ONLY state-ledger
    # entries with the C-CP-49/50 engine-layer action_ids — never the
    # workflow-layer cp.pause-resume-protocol (CP §16.5.9 invariant 5).
    assert "cp.pause-resume-protocol" not in after_resume

    # --- Durable-across-restart (F3 floor (i)): a FRESH substrate instance over
    # the same on-disk segment dir resumes the pause the PRODUCTION driver
    # captured in run 1 — proving the segment is genuinely durable (crash-
    # survivable), not the in-memory Deterministic placeholder. -----------------
    fresh = WALSegmentEnginePauseResumeSubstrate(
        journal_dir=_engine_segment_dir(config),
        state_summary_provider=_engine_state_summary,
    )
    restart = fresh.attempt_resume(
        ResumeAttempt(
            # The production driver stored the record at the run-scoped key
            # (workflow_id + run_id); a fresh substrate resumes it with the SAME
            # key (Codex [P2] run-scoping).
            paused_workflow_id=run_scoped_substrate_key("wf-wal", "run-wal"),
            resume_at="2026-06-15T12:00:00Z",
            resume_request_actor=ActorIdentity("engine-loop"),
        )
    )
    assert restart.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN


@pytest.mark.asyncio
async def test_path_i_engine_firing_is_wal_segment_gated_not_universal(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Contrasting baseline (tautology guard): the SAME pause-after-step-0 flow
    on a NON-WAL_SEGMENT engine class fires NO engine-layer recovery loop. The
    U-CP-95 firing is gated on `engine_class == WAL_SEGMENT`, so a
    PURE_PATTERN_NO_ENGINE workflow (no `pause_resume_protocol` bound) runs to
    completion with NO `cp.pause-captured` entry — proving the materialized
    path's cp.pause-captured comes from the WAL_SEGMENT-gated branch, not a
    universal behavior (it would FAIL if the engine firing were ungated)."""
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.engine_recovery_loop is not None

    manifest = _manifest("wf-pure", engine_class=EngineClass.PURE_PATTERN_NO_ENGINE)
    steps = _two_inference_steps()
    dispatcher = _PauseAfterFirstStepDispatcher(ctx.pause_requested_flag)
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-pure",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(dispatcher),  # type: ignore[arg-type]
        )
    )
    assert result.status is RunStatus.SUCCESS
    assert "cp.pause-captured" not in [entry.action_id for entry in read_ledger(handle)]


@pytest.mark.asyncio
async def test_path_i_clean_prefix_recovery_emits_no_spurious_resume(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """[P2] (Codex) An ordinary WAL_SEGMENT step-prefix crash recovery — resume_at>0
    but NO engine pause captured — must NOT emit a spurious
    `cp.resume-attempted = ABORT_SNAPSHOT_CORRUPTED`. The U-CP-95 resume firing is
    gated on a PRESENT pause record (`has_pause_record`), so a clean prefix
    recovery records no engine resume entry (it would otherwise make ordinary
    crash recovery look corrupted in the state ledger)."""
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle

    # Run 1: a single WAL_SEGMENT step commits to the F2 ledger and completes —
    # no pause requested, so NO engine pause is captured.
    one_step = (
        WorkflowStep(step_id=StepID("step-0"), step_kind=StepKind.INFERENCE_STEP, step_payload={}),
    )
    first = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=_manifest("wf-clean", engine_class=EngineClass.WAL_SEGMENT),
            steps=one_step,
            run_id="run-clean",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert first.status is RunStatus.SUCCESS
    assert "cp.pause-captured" not in [entry.action_id for entry in read_ledger(handle)]

    # Run 2: re-enter with a 2-step workflow + the SAME run_id → resume_at=1 (the
    # committed step-0 prefix), but no engine pause exists to resume.
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=_manifest("wf-clean", engine_class=EngineClass.WAL_SEGMENT),
            steps=_two_inference_steps(),
            run_id="run-clean",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert resumed.status is RunStatus.SUCCESS
    # No spurious engine resume entry for the clean (no-engine-pause) prefix recovery.
    assert "cp.resume-attempted" not in [entry.action_id for entry in read_ledger(handle)]


@pytest.mark.asyncio
async def test_path_i_engine_pause_before_first_step_is_resumed(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """[P2.b] (Codex) A WAL_SEGMENT engine pause captured BEFORE step 0 (resume_at==0,
    no committed step prefix) must still be resumed. The engine resume is gated on
    a PRESENT pause record (`has_pause_record`), NOT on resume_at>0 — gating on the
    step prefix would silently never resume a step-0 pause."""
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle
    manifest = _manifest("wf-step0", engine_class=EngineClass.WAL_SEGMENT)
    steps = _two_inference_steps()

    # Run 1: pause requested at run ENTRY → capture fires at step 0, BEFORE any
    # step commits (so the next run sees resume_at == 0).
    ctx.pause_requested_flag.set()
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-step0",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.terminal_step_index is None  # paused before step 0 committed
    assert "cp.pause-captured" in [entry.action_id for entry in read_ledger(handle)]

    # Run 2: resume_at == 0 (no committed step prefix) but a captured pause EXISTS
    # → the engine resume still fires (cp.resume-attempted) and the run completes.
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-step0",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert resumed.status is RunStatus.SUCCESS
    assert "cp.resume-attempted" in [entry.action_id for entry in read_ledger(handle)]


@pytest.mark.asyncio
async def test_path_i_present_but_corrupt_pause_fails_closed(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """[P1-r3-a/b] (Codex) A PRESENT-but-corrupt WAL pause record must FAIL the run
    CLOSED — not silently skip the resume. The driver gates the resume firing on
    PRESENCE (`has_pause_record`), so a corrupt snapshot still FIRES
    `attempt_resume` (recording the abort as `cp.resume-attempted`) and the
    aborting outcome (ABORT_SNAPSHOT_CORRUPTED) drives RunStatus.FAILED with
    fail_class=CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION. The prior `has_captured_pause`
    used the resume OUTCOME (validity) as a presence proxy → misread the corrupt
    snapshot as absent → skipped the resume → lost the abort record AND resumed
    past unrecoverable state (the two-part Codex round-3 finding this guards)."""
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle
    manifest = _manifest("wf-corrupt", engine_class=EngineClass.WAL_SEGMENT)
    steps = _two_inference_steps()

    # Run 1: pause at run entry → a single WAL segment is durably captured.
    ctx.pause_requested_flag.set()
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-corrupt",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert paused.status is RunStatus.PAUSED
    assert "cp.pause-captured" in [entry.action_id for entry in read_ledger(handle)]

    # Corrupt the (single) committed segment: tamper its checksum so the WAL
    # contiguous-valid-prefix scan rejects it → `attempt_resume` classifies the
    # snapshot ABORT_SNAPSHOT_CORRUPTED. The file stays present + non-empty, so
    # `has_pause_record` still reports the record PRESENT (presence ≠ validity).
    seg_files = list(_engine_segment_dir(config).glob("*.jsonl"))
    assert len(seg_files) == 1
    seg_file = seg_files[0]
    records = [json.loads(line) for line in seg_file.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    records[0]["checksum"] = "0" * 64  # no longer matches the payload digest
    seg_file.write_text(json.dumps(records[0], sort_keys=True) + "\n")
    # Presence-not-validity: the record is still PRESENT at its run-scoped key
    # despite being corrupt (the substrate file is keyed workflow_id + run_id).
    assert (
        ctx.engine_recovery_loop.substrate_by_engine_class[
            EngineClass.WAL_SEGMENT
        ].has_pause_record(run_scoped_substrate_key("wf-corrupt", "run-corrupt"))
        is True
    )

    # Run 2: resume → present-but-corrupt → fire attempt_resume → ABORT → FAILED.
    ctx.pause_requested_flag.clear()
    failed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-corrupt",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert failed.status is RunStatus.FAILED, (
        f"expected FAILED on corrupt-snapshot resume, got {failed.status}"
    )
    assert failed.fail_class == CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION
    # The abort is RECORDED, not lost: the fire emitted cp.resume-attempted BEFORE
    # the driver failed the run closed (the [P1-r3-a] "lost abort record" guard).
    assert "cp.resume-attempted" in [entry.action_id for entry in read_ledger(handle)]


@pytest.mark.asyncio
async def test_path_i_pause_record_is_run_scoped_not_workflow_scoped(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """[P2 round-4] (Codex) A fresh run of the SAME workflow_id under a DIFFERENT
    run_id must NOT pick up an earlier run's lingering pause record. Run 1 pauses
    and is then abandoned with a CORRUPTED segment on disk; run 2 (different run_id)
    must run to SUCCESS — NOT fail closed on run 1's corrupt record. Without
    run-scoping (the bug), run 2's presence check would find run 1's
    workflow_id-keyed record, fire attempt_resume, classify it ABORT, and
    fail-close every future run of this workflow_id (a permanent DoS). The engine
    pause record is keyed run-scoped to match the F2 replay prefix's run scope."""
    _ = patched_runtime
    config = build_config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    handle = ctx.engine_recovery_loop.wiring.ledger_writer.handle
    steps = _two_inference_steps()

    # Run 1 (run_id="run-A1"): pause at entry → a single WAL segment is captured.
    ctx.pause_requested_flag.set()
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=_manifest("wf-xrun", engine_class=EngineClass.WAL_SEGMENT),
            steps=steps,
            run_id="run-A1",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert paused.status is RunStatus.PAUSED

    # Corrupt run 1's (abandoned) segment so it would ABORT if ever resumed.
    seg_files = list(_engine_segment_dir(config).glob("*.jsonl"))
    assert len(seg_files) == 1
    seg_file = seg_files[0]
    record = json.loads(seg_file.read_text().splitlines()[0])
    record["checksum"] = "0" * 64
    seg_file.write_text(json.dumps(record, sort_keys=True) + "\n")

    # Run 2 (run_id="run-B2", DIFFERENT): a fresh execution of the SAME workflow_id,
    # no pause requested. Run-scoping isolates it from run 1's corrupt record.
    ctx.pause_requested_flag.clear()
    fresh_run = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=_manifest("wf-xrun", engine_class=EngineClass.WAL_SEGMENT),
            steps=steps,
            run_id="run-B2",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=_SingleKindRegistry(_NoopDispatcher()),  # type: ignore[arg-type]
        )
    )
    assert fresh_run.status is RunStatus.SUCCESS, (
        f"fresh run of same workflow_id must NOT fail-close on run 1's corrupt "
        f"record; got {fresh_run.status} fail_class={fresh_run.fail_class}"
    )
    # No engine resume fired for run-B2 (it has no record of its own) — so the only
    # engine entry on the ledger is run 1's cp.pause-captured; no cp.resume-attempted.
    action_ids = [entry.action_id for entry in read_ledger(handle)]
    assert "cp.pause-captured" in action_ids
    assert "cp.resume-attempted" not in action_ids
