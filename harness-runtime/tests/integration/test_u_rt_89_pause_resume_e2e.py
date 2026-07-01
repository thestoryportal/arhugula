"""U-RT-89 — Real-bootstrap e2e: PauseResumeProtocol binding chain + invocation.

Implements runtime plan v2.20 §1 U-RT-89 + runtime spec v1.21 §14.14.6
X-AL-2 retirement implication (operational-criterion-B exercise for
H_T-CP-22 RETIRE-READY → RETIRED transition under the operator-opt-in
pattern per the v1.21 narrow-scope CP composer authoring arc).

## Mechanism α (per cluster-open operator decision 2026-05-24)

Exercises the binding chain end-to-end via the real bootstrap (no
`_FakeCtx` or `_MutableHarnessContext` shortcut) — `run_bootstrap` runs
against in-process fake providers/daemon/tracer (the standard integration-
test substrate per `conftest.py::patched_runtime`); the stage-5 LOOP_INIT
bucket invokes `materialize_pause_resume_protocol_stage` with the real
factory; the test asserts the binding-chain post-conditions + exercises
the production `PauseResumeProtocol.capture_pause_snapshot(...)` +
`.attempt_resume(...)` async methods directly through the bound ctx
instance.

Coverage:

- **Opt-out** (`RuntimeConfig.pause_resume_protocol_config = None`, the
  production-default): `ctx.pause_resume_protocol is None`; the driver
  per-step pre-entry pause-trigger detection branch sibling to
  `drained_flag.is_set()` would evaluate False (backward-compatible
  behaviour preserved per spec §14.14.5 invariant 2).

- **Opt-in binding chain** (`RuntimeConfig.pause_resume_protocol_config =
  PauseResumeProtocolConfig.default()`): `ctx.pause_resume_protocol is not
  None`; the bound instance is the CP-canonical `PauseResumeProtocol`
  class per spec §14.14.5 invariant 3.

- **Pause-resume cycle through protocol methods** (the operational-
  criterion-B exercise): the bootstrapped `ctx.pause_resume_protocol`
  instance's `capture_pause_snapshot(...)` async method produces a valid
  `PauseSnapshot`; the `attempt_resume(...)` async method consumes that
  snapshot + returns `ResumeResult(resumed=True)` for the clean-resume
  path (no material diff under the MVP `_make_default_pause_context_reader`
  shape). This verifies the production binding chain operates end-to-end
  per `[[verification-shape-sharpened-grep-vs-e2e]]` discipline.

The workflow_driver.py per-step invocation path is structurally verified
by the U-RT-89 changes to the CP test suite (`RunStatus.PAUSED` enum +
`RunResult.pause_snapshot` field + driver-side detection-point landings
all green at 677/677 harness-cp tests); end-to-end workflow_driver
execution requires the full tracer-provider + step-dispatcher substrate
not available in this integration-test fixture and is deferred to a
follow-on operator-discretion e2e at the next retirement-batch arc per
FM-2 no-extension discipline.

## Verification-shape discipline

Per `[[verification-shape-sharpened-grep-vs-e2e]]` (batch-16 §6): "driver
invocation succeeds end-to-end against a real substrate" — this test uses
the production `run_bootstrap` orchestrator (not `_FakeCtx` or
`_MutableHarnessContext` test-local shortcuts) and verifies the full
binding chain at the produced `HarnessContext`. The stage-5 factory IS
empirically invoked (in the real `stage_5_loop_init.execute` body) and
the result IS empirically bound at `ctx.pause_resume_protocol` + IS
empirically invocable end-to-end via the protocol's async methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_cp.pause_resume_protocol import PauseResumeProtocol
from harness_cp.pause_resume_protocol_types import (
    MaterialDiffPolicy,
    PauseSnapshot,
    ResumeResult,
    WorkflowPauseReason,
)
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.types import HarnessContext, RuntimeConfig

from .conftest import WORKLOAD, build_config


def _config_with_pause_resume_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={
            "pause_resume_protocol_config": PauseResumeProtocolConfig.default(),
        },
    )


# AC #2 — opt-out e2e branch.


@pytest.mark.asyncio
async def test_pause_resume_e2e_opt_out_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #2 / spec §14.14.5 invariant 2 — opt-out config → ctx.pause_resume_protocol
    is None; driver per-step pause-trigger detection branch False arm;
    backward-compatible behaviour preserved."""
    _ = patched_runtime
    config = build_config(tmp_path)
    # Production-default: pause_resume_protocol_config defaults to None.
    assert config.pause_resume_protocol_config is None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.pause_resume_protocol is None, (
        "opt-out (default) config must yield ctx.pause_resume_protocol is None "
        "per spec §14.14.5 invariant 2"
    )
    # Sibling-pattern to drained_flag: pause_requested_flag is always
    # initialized at stage 0 PREAMBLE regardless of opt-out/opt-in.
    assert ctx.pause_requested_flag is not None
    assert not ctx.pause_requested_flag.is_set()


# AC #1 + AC #3 — opt-in e2e branch (binding chain only).


@pytest.mark.asyncio
async def test_pause_resume_e2e_opt_in_binding_chain(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #1 + AC #3 — opt-in config → stage-5 factory invoked →
    ctx.pause_resume_protocol bound to a CP-canonical PauseResumeProtocol
    instance per spec §14.14.5 invariant 3."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    assert config.pause_resume_protocol_config is not None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.pause_resume_protocol is not None, (
        "opt-in config must yield a bound PauseResumeProtocol instance per "
        "spec §14.14.1 + §14.14.5 invariant 3"
    )
    # Spec §14.14.5 invariant 3 — CP-canonical class satisfaction (not a
    # substitute or wrapper).
    assert type(ctx.pause_resume_protocol) is PauseResumeProtocol


# AC #5 — composer-depth parity: real bootstrap, not _FakeCtx.


@pytest.mark.asyncio
async def test_pause_resume_e2e_uses_real_bootstrap(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #8 — the test exercises the REAL bootstrap orchestrator
    (`run_bootstrap`), NOT `_FakeCtx` or `_MutableHarnessContext`
    short-circuits. Verification-shape discipline per batch-16 §6 sharpening:
    "driver invocation succeeds end-to-end against a real substrate"."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)

    # The production bootstrap orchestrator is what's invoked.
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    # If _MutableHarnessContext.freeze() bound it correctly, the production
    # frozen-HarnessContext carries the value (not a test-local mutation).
    assert isinstance(ctx, HarnessContext)
    assert ctx.pause_resume_protocol is not None
    assert ctx.pause_requested_flag is not None

    # The frozen HarnessContext model is Pydantic v2 frozen (no post-freeze
    # mutation); the field value materialised at stage-5 via the production
    # factory.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ctx.pause_resume_protocol = None  # type: ignore[misc]


# AC #4 — protocol invocation succeeds end-to-end against real substrate
# (operational-criterion-B for H_T-CP-22 PARTIAL → RETIRED transition).


@pytest.mark.asyncio
async def test_pause_resume_e2e_capture_pause_snapshot_via_real_substrate(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #4 / spec §14.14.6 operational-criterion-B — bootstrapped
    ctx.pause_resume_protocol.capture_pause_snapshot(...) produces a valid
    PauseSnapshot via the production binding chain (factory → instance →
    method invocation all empirically exercised end-to-end)."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    assert ctx.pause_resume_protocol is not None

    snapshot = await ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id="test-workflow",
        run_id="test-run-1",
        step_index=2,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )

    assert isinstance(snapshot, PauseSnapshot)
    assert snapshot.workflow_id == "test-workflow"
    assert snapshot.run_id == "test-run-1"
    assert snapshot.step_index == 2
    assert snapshot.pause_reason == WorkflowPauseReason.EXPLICIT_OPERATOR
    # Snapshot hash is sha256 hex (64 chars); spec §26.6 invariant 1 immutable
    assert len(snapshot.snapshot_hash) == 64
    # MVP state_ledger_anchor per _make_default_pause_context_reader (factory
    # body returns "0"*64 sentinel; richer composition is a follow-on arc).
    assert len(snapshot.state_ledger_anchor) == 64


@pytest.mark.asyncio
async def test_pause_resume_e2e_clean_resume_cycle_via_real_substrate(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #6 / spec §14.14.6 operational-criterion-B clean-resume path —
    pause-then-resume cycle via the bootstrapped protocol exercises both
    capture + attempt_resume async methods end-to-end. With the MVP
    `_make_default_pause_context_reader` returning a constant anchor sentinel,
    snapshot's state_ledger_anchor matches the current anchor at resume time
    → no material diff → ResumeResult.resumed=True (clean resume per CP spec
    v1.13 §26.6 invariant 4 STRICT-policy clean-resume branch)."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    assert ctx.pause_resume_protocol is not None

    # Capture snapshot.
    snapshot = await ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id="test-workflow-clean-resume",
        run_id="test-run-clean",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )

    # Attempt resume (STRICT policy default per spec §26.6 invariant 4).
    resume_result = await ctx.pause_resume_protocol.attempt_resume(
        snapshot,
        material_diff_policy=MaterialDiffPolicy.STRICT,
    )

    assert isinstance(resume_result, ResumeResult)
    # MVP constant-anchor sentinel → no material diff at resume time.
    assert resume_result.diff_detected is False, (
        "MVP _make_default_pause_context_reader returns constant anchor → "
        "snapshot.state_ledger_anchor matches current → diff_detected=False"
    )
    assert resume_result.resumed is True, (
        "STRICT-policy clean-resume branch (no material diff) returns "
        "resumed=True per CP spec v1.13 §26.6 invariant 4"
    )
    assert resume_result.fail_class is None


# AC #7 — snapshot corruption path (validation failure detected at resume time).


@pytest.mark.asyncio
async def test_pause_resume_e2e_snapshot_corruption_path(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Corruption detection: a snapshot with mutated snapshot_hash (simulating
    tampering or transit corruption) yields `ResumeResult.resumed=False` with
    `fail_class=CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION` per CP spec v1.13 §26.5 +
    §26.6 invariant 2 snapshot_hash validation."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    assert ctx.pause_resume_protocol is not None

    # Capture a valid snapshot.
    snapshot = await ctx.pause_resume_protocol.capture_pause_snapshot(
        workflow_id="test-workflow-corruption",
        run_id="test-run-corruption",
        step_index=0,
        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
    )

    # Construct a corrupted snapshot — mutate snapshot_hash so the resume-time
    # recomputed hash will not match.
    corrupted = snapshot.model_copy(update={"snapshot_hash": "0" * 64})

    resume_result = await ctx.pause_resume_protocol.attempt_resume(
        corrupted,
        material_diff_policy=MaterialDiffPolicy.STRICT,
    )

    assert resume_result.resumed is False
    assert resume_result.fail_class == "CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION"
