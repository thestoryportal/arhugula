"""U-RT-50 — bootstrap-stage isolation test suite.

Per session-3 atomic decomposition L11 U-RT-50:

- Scope: per-stage tests bring up only through stage N and assert
  invariants; rollback test injects failure at each stage.
- AC: each of the 9 stages has a focused integration test; rollback
  verified at each.

Implementation
--------------
For each of the 9 BootstrapStage members, this file lands two tests:

1. `test_through_stage_N_post_conditions` — bring bootstrap up to and
   including stage N by injecting a failure at stage N+1. The captured
   builder state asserts stage N's post-conditions. Stage 7 (terminal)
   uses successful bootstrap + post-freeze inspection.

2. `test_failure_at_stage_N_rolls_back_stages_0_through_N_minus_1` —
   inject failure at stage N; assert reverse-order rollback handler
   invocation across `completed_stages`.

Some surfaces already covered at unit level in `test_bootstrap.py`
(stages 0/1/3a/5 rollback paths). This file lands the systematic per-stage
coverage U-RT-50 requires.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from harness_runtime.bootstrap import (
    BootstrapFailure,
    run_bootstrap,
    stage_0_preamble,
    stage_1_is,
    stage_2_as,
    stage_3a_cp_clients,
    stage_3b_cp_routing,
    stage_4_od,
    stage_5_loop_init,
    stage_6_cxa_wiring,
    stage_7_ingress,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.types import BootstrapStage

from tests.integration.conftest import WORKLOAD, build_config

# ---------------------------------------------------------------------------
# Per-stage canonical metadata.
# ---------------------------------------------------------------------------

# (BootstrapStage, stage module, attributes required to be non-None on builder
# after the stage completes, attributes required to STILL be None before the
# stage runs)
_STAGES: tuple[tuple[BootstrapStage, Any, tuple[str, ...]], ...] = (
    (
        BootstrapStage.PREAMBLE,
        stage_0_preamble,
        ("config", "drained_flag", "actor", "keyring_resolver"),
    ),
    (
        BootstrapStage.IS,
        stage_1_is,
        (
            "ledger_writer",
            "index",
            "cache",
            "path_resolver",
            "worktree_manager",
            "shadow_git",
        ),
    ),
    (
        BootstrapStage.AS,
        stage_2_as,
        ("skills", "tool_contracts", "mcp_host", "mcp_clients", "sandbox_dispatch"),
    ),
    (
        BootstrapStage.CP_CLIENTS,
        stage_3a_cp_clients,
        ("providers",),
    ),
    (
        BootstrapStage.CP_ROUTING,
        stage_3b_cp_routing,
        (
            "routing_manifest",
            "engine_selector",
            "fallback_chain",
            "retry_breaker",
            "hitl_registry",
            "handoff_registry",
        ),
    ),
    (
        BootstrapStage.OD,
        stage_4_od,
        ("tracer_provider", "collector_daemon", "cost_chain", "audit_writer"),
    ),
    (
        BootstrapStage.LOOP_INIT,
        stage_5_loop_init,
        ("override_evaluator", "topology_dispatcher", "lifecycle_emitter"),
    ),
    # Stage 6 populates `cxa_stages` dict, not direct builder fields.
    (BootstrapStage.CXA_WIRING, stage_6_cxa_wiring, ()),
    # Stage 7 populates `frozen` (terminal stage).
    (BootstrapStage.INGRESS_ACCEPT, stage_7_ingress, ()),
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _patch_stage_to_explode(monkeypatch: pytest.MonkeyPatch, stage_module: Any) -> None:
    """Replace `<stage_module>.execute` with one that raises RuntimeError."""

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"injected failure at {stage_module.__name__}")

    monkeypatch.setattr(stage_module, "execute", _boom)


def _capture_completed_stages_then_explode(
    monkeypatch: pytest.MonkeyPatch,
    target_stage_module: Any,
    captured: list[_MutableHarnessContext],
) -> None:
    """Patch target stage to capture the builder state and explode.

    Allows the test to inspect the builder AFTER all prior stages succeeded
    and BEFORE the target stage runs.
    """

    async def _capture_and_boom(
        ctx: _MutableHarnessContext, config: Any, workload_class: Any
    ) -> None:
        _ = config, workload_class
        captured.append(ctx)
        raise RuntimeError(f"injected at {target_stage_module.__name__}")

    monkeypatch.setattr(target_stage_module, "execute", _capture_and_boom)


def _spy_rollback_order(
    monkeypatch: pytest.MonkeyPatch,
) -> list[BootstrapStage]:
    """Patch all rollback handlers to record their invocation order."""
    invoked: list[BootstrapStage] = []

    import harness_runtime.bootstrap as _boot

    def _make_spy(
        stage: BootstrapStage,
        original: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        async def _spy(ctx: _MutableHarnessContext) -> None:
            invoked.append(stage)
            await original(ctx)

        return _spy

    new_handlers: dict[BootstrapStage, Callable[[_MutableHarnessContext], Awaitable[None]]] = {
        stage: _make_spy(stage, original)
        for stage, original in _boot._ROLLBACK_HANDLERS.items()  # type: ignore[attr-defined]
    }

    monkeypatch.setattr(_boot, "_ROLLBACK_HANDLERS", new_handlers)
    return invoked


# ---------------------------------------------------------------------------
# Per-stage post-condition tests (stages 0..6 via failure-at-next-stage).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage_index",
    range(8),  # stages 0..7 — through-stage-N captured by failing at N+1
)
async def test_through_stage_n_post_conditions(
    stage_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_runtime: dict[str, Any],
) -> None:
    """Bring up bootstrap through stage N; assert stage N's post-conditions.

    Pattern: patch stage N+1's `execute` to capture the post-stage-N builder
    state and raise. The captured builder shows what stage N populated.
    """
    _ = patched_runtime
    stage, _stage_mod, attrs = _STAGES[stage_index]
    next_stage_mod = _STAGES[stage_index + 1][1]

    captured: list[_MutableHarnessContext] = []
    _capture_completed_stages_then_explode(monkeypatch, next_stage_mod, captured)

    with pytest.raises(BootstrapFailure):
        await run_bootstrap(build_config(tmp_path), workload_class=WORKLOAD)

    assert len(captured) == 1, "next-stage executor should have been called once"
    ctx = captured[0]

    # All prior stages (0..N) should be in completed_stages.
    expected_completed = [s[0] for s in _STAGES[: stage_index + 1]]
    assert ctx.completed_stages == expected_completed, (
        f"completed_stages mismatch through stage {stage.name}: "
        f"got {ctx.completed_stages}, expected {expected_completed}"
    )

    # Each declared post-condition attribute should be non-None.
    for attr in attrs:
        value = getattr(ctx, attr, None)
        assert value is not None, (
            f"stage {stage.name} post-condition violated: builder.{attr} is None"
        )


# ---------------------------------------------------------------------------
# Per-stage rollback tests — failure at stage N rolls back stages 0..N-1.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_index", range(9))  # 0..8 (all 9 stages)
async def test_failure_at_stage_n_rolls_back_prior_stages(
    stage_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_runtime: dict[str, Any],
) -> None:
    """Inject failure at stage N; assert rollback handlers for 0..N-1 fire."""
    _ = patched_runtime
    stage, stage_mod, _attrs = _STAGES[stage_index]

    # Spy rollback handlers BEFORE patching the failure stage so the
    # `_ROLLBACK_HANDLERS` dict swap happens cleanly.
    invoked = _spy_rollback_order(monkeypatch)

    _patch_stage_to_explode(monkeypatch, stage_mod)

    with pytest.raises(BootstrapFailure) as exc_info:
        await run_bootstrap(build_config(tmp_path), workload_class=WORKLOAD)

    assert exc_info.value.failed_stage == stage

    # Rollback should fire for stages 0..N-1 in REVERSE order.
    expected_rollback = [s[0] for s in _STAGES[:stage_index]][::-1]
    assert invoked == expected_rollback, (
        f"rollback order mismatch for stage {stage.name}: "
        f"got {invoked}, expected {expected_rollback}"
    )


# ---------------------------------------------------------------------------
# Terminal-stage post-condition test (stage 7 via successful bootstrap).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_7_post_conditions_via_successful_bootstrap(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Stage 7 has no successor — post-conditions checked via full success."""
    _ = patched_runtime
    ctx = await run_bootstrap(build_config(tmp_path), workload_class=WORKLOAD)

    # Stage 7 freezes the builder; HarnessContext returned.
    # Pidfile written per U-RT-48.
    pidfile = tmp_path / ".harness/runtime.pid"
    assert pidfile.is_file()
    # Drained flag exists per U-RT-44 stage 0 initialization, unaffected by stage 7.
    assert ctx.drained_flag is not None


# ---------------------------------------------------------------------------
# Sanity: all 9 stages enumerated.
# ---------------------------------------------------------------------------


def test_all_nine_stages_covered() -> None:
    """Defensive: the _STAGES table covers exactly 9 BootstrapStage members."""
    assert len(_STAGES) == 9
    assert [s[0] for s in _STAGES] == list(BootstrapStage)
