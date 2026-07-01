"""Full workflow_driver execution-path e2e for pause/resume cycle.

Authority: C-RT-24.

Closes the U-RT-89 docstring deferral at
`test_u_rt_89_pause_resume_e2e.py` lines 42-49:

    The workflow_driver.py per-step invocation path is structurally verified
    by the U-RT-89 changes to the CP test suite (...); end-to-end
    workflow_driver execution requires the full tracer-provider +
    step-dispatcher substrate not available in this integration-test
    fixture and is deferred to a follow-on operator-discretion e2e at the
    next retirement-batch arc per FM-2 no-extension discipline.

This substrate uses the `test_run_smoke.py` pattern
(`PURE_PATTERN_NO_ENGINE` manifest + `_NoopDispatcher` injected via a
single-kind test registry) wrapped around the U-RT-89 real-bootstrap
fixture (`patched_runtime` + `run_bootstrap`) to exercise the driver's
**per-step pre-entry pause-trigger detection** (`workflow_driver.py:678`)
AND **entry-point resume detection** (`workflow_driver.py:474`) end-to-end
through `workflow_driver.execute_workflow(...)`.

Two tests:

1. `test_pause_path_through_execute_workflow` — pre-set
   `ctx.pause_requested_flag`, call `execute_workflow(...)` → assert
   `RunStatus.PAUSED` + `pause_snapshot` populated + terminal_step_index
   reflects pre-step-0 capture.
2. `test_resume_path_through_execute_workflow` — pause-path first, then
   clear flag + call `execute_workflow(...)` with `pause_snapshot_input`
   → assert `attempt_resume` succeeded (no FAILED return) + dispatcher
   reached + RunStatus.SUCCESS terminal.

Scope per advisor pre-flight discipline (`[[advisor-before-substantive-
work-for-cross-axis-blockers]]` 11th application):

- Integration scope at `harness-runtime/tests/integration/` is correct —
  unit-test scope at `harness-cp/tests/test_workflow_driver.py` would
  mock the ctx, missing the bootstrap→driver call-site regression class
  this test exists to guard.
- 2 tests, not 5: pause-path + resume-path. Protocol semantics (capture
  + attempt_resume corruption-path) already covered by U-RT-89 direct-
  method tests; this arc covers the call-site path only.
- NO audit-emission assertions per fork §11 workflow-layer audit-write
  CLOSED-as-WON'T-FIX (commit `1b7bcb0`, 2026-05-24). Assertions are
  restricted to `RunStatus` + `pause_snapshot` field presence + state
  continuity.

Verification-shape per `[[verification-shape-sharpened-grep-vs-e2e]]`:
this test is the **operational-criterion-B sharpening** for the
workflow_driver call-site path — "driver invocation succeeds end-to-end
against a real substrate" — closing the verification-shape gap explicitly
flagged at the U-RT-89 docstring while the U-RT-89 e2e at `671f195`
covered the protocol-method path only.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from harness_core.identity import StepID
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.pause_resume_protocol_types import PauseSnapshot
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import execute_workflow
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.types import RuntimeConfig

from .conftest import WORKLOAD, build_config


# Reuse the U-RT-89 opt-in config helper shape (lifted by reference rather
# than imported to keep test files independent under FM-2 cross-test-file
# coupling discipline).
def _config_with_pause_resume_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={
            "pause_resume_protocol_config": PauseResumeProtocolConfig.default(),
        },
    )


# Single-kind step-dispatcher registry per `test_run_smoke.py:75`
# `_test_step_dispatchers` precedent. Overrides the bootstrap-bound
# `ctx.step_dispatchers` which binds SUB_AGENT_DISPATCH only at v1.6 MVP
# per the U-RT-59 async/sync Class 1 fork resolution.
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
    """The shared `patched_runtime` fixture's `FakeTracerProvider` lacks
    `get_tracer` (it only stubs `force_flush` + `shutdown`); the
    `test_run_smoke.py` file-local fixture adds it, but the shared
    integration-test fixture does not. The workflow envelope at
    `workflow_driver.py:517` requires `get_tracer` to be invocable.

    Attach a `NoOpTracer`-returning method directly to the bound instance
    rather than duplicating the entire fixture-setup block from
    `test_run_smoke.py`. NoOp is sufficient — these tests assert on
    `RunStatus` + `pause_snapshot`, not on emitted spans (audit-emission
    assertions explicitly excluded per fork §11 workflow-layer
    won't-fix closure).
    """
    from opentelemetry.trace import NoOpTracer

    ctx.tracer_provider.get_tracer = lambda _name, /: NoOpTracer()  # type: ignore[attr-defined,method-assign]


def _minimal_manifest(workflow_id: str) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WORKLOAD,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
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


# ---------------------------------------------------------------------------
# Test 1 — pause-path through execute_workflow.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_path_through_execute_workflow(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Driver per-step pre-entry pause-trigger detection fires end-to-end:
    pre-set `ctx.pause_requested_flag` + invoke `execute_workflow(...)` →
    `RunStatus.PAUSED` + populated `pause_snapshot` returned without ever
    reaching dispatcher.

    This guards against a regression where someone refactors
    `execute_workflow` and accidentally drops the line-678 pause detection
    branch — neither direct protocol-method tests (U-RT-89) nor mocked-ctx
    unit tests at `test_workflow_driver.py` catch that class of bug.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.pause_resume_protocol is not None
    assert ctx.pause_requested_flag is not None

    # Pre-set pause_requested_flag BEFORE execute_workflow. The driver
    # loop body (workflow_driver.py:651-697) iterates from step 0; the
    # per-step pre-entry pause check at line 678 fires before binding
    # resolution + dispatch (line 699+). Flag set + protocol bound →
    # capture_pause_snapshot → return RunStatus.PAUSED at step_index=0.
    ctx.pause_requested_flag.set()

    manifest = _minimal_manifest("wf-pause-path-e2e")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    # execute_workflow is sync; the driver's internal async-to-sync bridge
    # (workflow_driver.py:_run_protocol_method_sync at line 383) calls
    # asyncio.run(coro) which cannot run from a live event loop. The
    # @pytest.mark.asyncio fixture already holds a loop, so dispatch the
    # sync call into a thread per the api.run-internal asyncio.to_thread
    # pattern.
    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-pause-path-1",
            ctx=ctx,  # type: ignore[arg-type]  # HarnessContext satisfies DriverContext structurally
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]  # _SingleKindRegistry satisfies StepDispatcherRegistry structurally
        )
    )

    # RunStatus.PAUSED per workflow_driver.py:691.
    assert result.status == RunStatus.PAUSED, (
        f"expected PAUSED, got {result.status}; fail_class={result.fail_class}"
    )
    # pause_snapshot populated per workflow_driver.py:696.
    assert result.pause_snapshot is not None
    assert isinstance(result.pause_snapshot, PauseSnapshot)
    # Snapshot's workflow_id + step_index reflect the pre-step-0 capture
    # site per workflow_driver.py:682-685.
    assert result.pause_snapshot.workflow_id == "wf-pause-path-e2e"
    assert result.pause_snapshot.step_index == 0
    # terminal_step_index is None at pre-step-0 pause per
    # workflow_driver.py:692 (`step_index - 1 if step_index > 0 else None`).
    assert result.terminal_step_index is None
    # No steps executed → partial_state is empty dict per
    # workflow_driver.py:693.
    assert result.partial_state == {}
    assert result.fail_class is None


# ---------------------------------------------------------------------------
# Test 2 — resume-path through execute_workflow (pause-then-clean-resume).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_path_through_execute_workflow(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Driver entry-point resume detection fires end-to-end against the
    pause-path snapshot:

    1. Phase 1 — set flag + execute_workflow → PAUSED + snapshot.
    2. Phase 2 — clear flag + execute_workflow(pause_snapshot_input=snapshot)
       → driver entry-point resume detection (workflow_driver.py:474)
       fires `attempt_resume`; MVP `_make_default_pause_context_reader`
       returns constant anchor sentinel → no material diff → clean resume
       → resume_at_step_index=0 → loop entered + NoopDispatcher reached
       → RunStatus.SUCCESS.

    Verifies the full pause→resume→continue cycle through the driver's
    call-site path. Protocol-method semantics (clean-resume + corruption-
    path) already covered by U-RT-89 direct-method tests at
    `test_u_rt_89_pause_resume_e2e.py:221-298`; this test ensures the
    driver wires those semantics correctly at the entry-point detection
    branch.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.pause_resume_protocol is not None

    manifest = _minimal_manifest("wf-resume-path-e2e")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    # Phase 1 — pause to obtain a snapshot.
    ctx.pause_requested_flag.set()
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-resume-path-phase1",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert paused.status == RunStatus.PAUSED
    assert paused.pause_snapshot is not None

    # Phase 2 — clear flag + invoke execute_workflow with the snapshot.
    # workflow_driver.py:474 fires attempt_resume(snapshot, STRICT). MVP
    # constant-anchor pause_context_reader → snapshot.state_ledger_anchor
    # matches current anchor → diff_detected=False → resumed=True per
    # CP spec v1.13 §26.6 invariant 4. resume_at_step_index=0 is set per
    # workflow_driver.py:492; loop at line 651 enters from step 0; flag
    # now cleared → pause check at line 678 passes (False arm); binding
    # resolved + NoopDispatcher.dispatch invoked + step output recorded;
    # loop exits → post-loop SUCCESS branch.
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-resume-path-phase2",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
            pause_snapshot_input=paused.pause_snapshot,
        )
    )

    # Clean resume → SUCCESS terminal status (no FAILED corruption return
    # at line 482-491). NoopDispatcher reached → step output captured →
    # post-loop SUCCESS path.
    assert resumed.status == RunStatus.SUCCESS, (
        f"expected SUCCESS, got {resumed.status}; "
        f"fail_class={resumed.fail_class}; "
        f"terminal_step_index={resumed.terminal_step_index}"
    )
    assert resumed.fail_class is None
    # pause_snapshot is None on a SUCCESS terminal — pause path was not
    # taken in phase 2.
    assert resumed.pause_snapshot is None
