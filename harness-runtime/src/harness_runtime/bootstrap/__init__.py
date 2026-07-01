"""`harness_runtime.bootstrap` — 9-stage bootstrap orchestrator (U-RT-43, opens L9).

Per `Spec_Harness_Runtime_v1.md` v1.1 §1 (C-RT-01 9-value `BootstrapStage` enum)
+ §2 (C-RT-02 orchestrator + stage-ordering invariants) + §4 (C-RT-04
`HarnessContext` schema) and Phase 2 Session 3 atomic decomposition §3.8.4.

The orchestrator runs 9 stages in fixed order, populating a
`_MutableHarnessContext` builder; at stage 7 INGRESS_ACCEPT the builder freezes
into the post-bootstrap `HarnessContext`. Stage failures trigger reverse-order
rollback of stages 0..N-1.

**Lifecycle event emission discipline.** AC #3 (each stage emits exactly one
lifecycle event) is satisfied via a buffer: stages 0-4 complete before the
emitter materializes (stage 5 LOOP_INIT). The orchestrator buffers per-stage
`BootstrapStageCompleteEvent` records; on stage 5 success, all buffered events
flush through `emit_bootstrap_stage_complete`; stages 6-7 emit synchronously
thereafter. Total = 9 events post-bootstrap.

**WorkflowEventClass-vs-BootstrapStage separation.** Bootstrap-stage lifecycle
events use a runtime-local `BootstrapStageCompleteEvent` record. The CP
`WorkflowEventClass` enum is closed at cardinality 8 (per `[[fork-drained-
event-class]]`) and addresses workflow lifecycle, not bootstrap lifecycle.
Surface growth bounded to this package.

**Tracer rollback note (Class 3 informational).** OTel does not expose an
`unset_tracer_provider` API. Once globally registered (stage 4), rollback
leaves the provider registered; subsequent process invocations replace via
`set_tracer_provider`. U-RT-44/45 shutdown work surfaces if a true unregister
API is needed.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from harness_core.workload_class import WorkloadClass

from harness_runtime.bootstrap import (
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
from harness_runtime.bootstrap.mutable_context import (
    BootstrapStageCompleteEvent,
    IncompleteBootstrapError,
    _MutableHarnessContext,
)
from harness_runtime.types import BootstrapStage, HarnessContext, RuntimeConfig

__all__ = [
    "BootstrapFailure",
    "BootstrapStageCompleteEvent",
    "IncompleteBootstrapError",
    "run_bootstrap",
]


# ---------------------------------------------------------------------------
# Typed failure surface (C-RT-14 RT-FAIL-BOOTSTRAP / RT-FAIL-PARTIAL-ROLLBACK).
# ---------------------------------------------------------------------------


class BootstrapFailure(Exception):  # noqa: N818 — domain-anchored name (mirrors `ConcurrentRunNotSupported`)
    """`RT-FAIL-BOOTSTRAP` — one of the 9 stages raised; rollback executed.

    Carries the failed stage + original cause for operator diagnosis. Subclass
    of `Exception` (not `NotImplementedError`) — bootstrap failure is a real
    runtime fault, not a not-yet-landed surface.
    """

    def __init__(self, failed_stage: BootstrapStage, cause: BaseException) -> None:
        self.failed_stage = failed_stage
        self.cause = cause
        super().__init__(
            f"bootstrap failed at stage {failed_stage.name} ({failed_stage.value}): "
            f"{type(cause).__name__}: {cause}"
        )


# ---------------------------------------------------------------------------
# Stage executor type — every stage_N_*.execute conforms to this.
# ---------------------------------------------------------------------------


StageExecutor = Callable[
    [_MutableHarnessContext, RuntimeConfig, WorkloadClass],
    Awaitable[None],
]


# Stage → module mapping. Executors are looked up dynamically via `getattr` at
# orchestrator call time (not cached at import) so test monkey-patching of
# `stage_N_*.execute` takes effect.
_STAGE_MODULES: tuple[tuple[BootstrapStage, object], ...] = (
    (BootstrapStage.PREAMBLE, stage_0_preamble),
    (BootstrapStage.IS, stage_1_is),
    (BootstrapStage.AS, stage_2_as),
    (BootstrapStage.CP_CLIENTS, stage_3a_cp_clients),
    (BootstrapStage.CP_ROUTING, stage_3b_cp_routing),
    (BootstrapStage.OD, stage_4_od),
    (BootstrapStage.LOOP_INIT, stage_5_loop_init),
    (BootstrapStage.CXA_WIRING, stage_6_cxa_wiring),
    (BootstrapStage.INGRESS_ACCEPT, stage_7_ingress),
)


# ---------------------------------------------------------------------------
# Rollback handler table — one per BootstrapStage. Best-effort (exceptions
# swallowed); reverse-order execution over `ctx.completed_stages`.
# ---------------------------------------------------------------------------


async def _rollback_preamble(ctx: _MutableHarnessContext) -> None:
    # drained_flag is an empty asyncio.Event; clearing it is a no-op semantically.
    # Reference for symmetry.
    if ctx.drained_flag is not None:
        ctx.drained_flag.clear()


async def _rollback_is(ctx: _MutableHarnessContext) -> None:
    # Ledger reattach is non-destructive; shadow_git checkpoint is opt-in per
    # workload manifest and does not require teardown. Worktree/index handles
    # close cleanly on garbage collection. No active resources to release.
    return None


async def _rollback_as(ctx: _MutableHarnessContext) -> None:
    # MCP clients have no clean disconnect API at HEAD; deferred to U-RT-46.
    return None


async def _rollback_cp_clients(ctx: _MutableHarnessContext) -> None:
    # Drain each STARTED MCP client host stage 3a started (U-RT-126:
    # `mcp_client_hosts`). A post-stage-3a failure — e.g. a stage-5
    # RT-FAIL-MCP-TOOL-NAME-COLLISION abort — must not leak their
    # subprocesses/sessions (the symmetric teardown to stage_3a's per-host
    # start loop; mirrors `shutdown()` step 4). Best-effort, per-resource isolation.
    if ctx.mcp_client_hosts is not None:
        for host in ctx.mcp_client_hosts.values():
            if getattr(host, "started", False):
                try:
                    await host.shutdown()
                except Exception:
                    pass
    # Close each provider that exposes an awaitable `aclose()` per C-RT-05.
    if ctx.providers is None:
        return
    for provider in ctx.providers.values():
        aclose = getattr(provider, "aclose", None)
        if aclose is None or not inspect.iscoroutinefunction(aclose):
            continue
        try:
            await aclose()
        except Exception:
            pass


async def _rollback_cp_routing(ctx: _MutableHarnessContext) -> None:
    # Pure data structures; no resources to release.
    return None


async def _rollback_od(ctx: _MutableHarnessContext) -> None:
    # Stop the collector daemon (idempotent per supervisor STOPPED terminal state).
    # Tracer provider rollback deferred to U-RT-44/45 (no OTel unset API).
    if ctx.collector_daemon is None:
        return
    stop = getattr(ctx.collector_daemon, "stop", None)
    if stop is None:
        return
    try:
        result = stop()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


async def _rollback_loop_init(ctx: _MutableHarnessContext) -> None:
    # Pure data structures; no resources to release.
    return None


async def _rollback_cxa_wiring(ctx: _MutableHarnessContext) -> None:
    # Module imports + read-only manifest reference resolution cannot be undone.
    return None


async def _rollback_ingress(ctx: _MutableHarnessContext) -> None:
    # Per U-RT-44: stage 7 calls `freeze()` then `install_signal_handlers()`.
    # If install raises (e.g., `DrainPlatformError` on Windows), stage 7 is
    # recorded as failed and never appended to `completed_stages`, so this
    # handler is unreachable in practice. Wired defensively as the
    # rollback-symmetric site for handler removal.
    import asyncio as _asyncio

    from harness_runtime.drain import uninstall_signal_handlers

    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return
    uninstall_signal_handlers(loop)


_ROLLBACK_HANDLERS: dict[BootstrapStage, Callable[[_MutableHarnessContext], Awaitable[None]]] = {
    BootstrapStage.PREAMBLE: _rollback_preamble,
    BootstrapStage.IS: _rollback_is,
    BootstrapStage.AS: _rollback_as,
    BootstrapStage.CP_CLIENTS: _rollback_cp_clients,
    BootstrapStage.CP_ROUTING: _rollback_cp_routing,
    BootstrapStage.OD: _rollback_od,
    BootstrapStage.LOOP_INIT: _rollback_loop_init,
    BootstrapStage.CXA_WIRING: _rollback_cxa_wiring,
    BootstrapStage.INGRESS_ACCEPT: _rollback_ingress,
}


async def _rollback(ctx: _MutableHarnessContext) -> None:
    """Reverse-order shutdown of stages 0..N-1 (stages that completed)."""
    for stage in reversed(ctx.completed_stages):
        handler = _ROLLBACK_HANDLERS.get(stage)
        if handler is None:
            continue
        try:
            await handler(ctx)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Orchestrator entry point.
# ---------------------------------------------------------------------------


async def run_bootstrap(
    config: RuntimeConfig,
    *,
    workload_class: WorkloadClass,
    requires_inference: bool = True,
) -> HarnessContext:
    """Execute the 9-stage bootstrap; return frozen `HarnessContext`.

    Per C-RT-02 stage-ordering invariants: stages run in
    `list(BootstrapStage)` order; each stage's post-conditions are committed
    before the next stage executes. On stage N failure, all stages 0..N-1
    roll back in reverse order and a typed `BootstrapFailure` is raised with
    the failed stage + original cause attached.

    Parameters
    ----------
    config :
        L1-enriched `RuntimeConfig` (path bindings, provider secrets, OTel,
        collector — all sub-configs populated).
    workload_class :
        The runtime's current workload class (typically `workflow.workload_class`
        from the caller-supplied `WorkflowObject`).
    requires_inference :
        Runtime spec v1.47 §2.1 — whether the workflow is inference-bearing
        (contains an `INFERENCE_STEP` / `SUB_AGENT_DISPATCH` step). Derived by
        `run()`/`resume()` from `workflow.steps`. When `False`, stage 3a
        tolerates an empty `providers` dict (no `ProviderNoneConfiguredError`)
        and stage 5 binds fail-loud sentinels for the LLM/sub-agent dispatchers
        + omits their step-dispatcher registry rows, so a tool-only workflow
        bootstraps provider-free. Defaults `True` (behavior-preserving).

    Raises
    ------
    BootstrapFailure
        Stage N raised; stages 0..N-1 rolled back in reverse order.
    """
    ctx = _MutableHarnessContext()
    ctx.requires_inference = requires_inference
    pending_events: list[BootstrapStageCompleteEvent] = []

    for stage, module in _STAGE_MODULES:
        executor: StageExecutor = module.execute  # type: ignore[attr-defined]
        try:
            await executor(ctx, config, workload_class)
        except Exception as exc:
            await _rollback(ctx)
            if isinstance(exc, BootstrapFailure):
                raise
            raise BootstrapFailure(failed_stage=stage, cause=exc) from exc

        ctx.completed_stages.append(stage)
        pending_events.append(BootstrapStageCompleteEvent(stage=stage))

        # Drain the buffer once the emitter exists (stage 5 LOOP_INIT).
        if ctx.lifecycle_emitter is not None:
            for event in pending_events:
                ctx.lifecycle_emitter.emit_bootstrap_stage_complete(event.stage)
            ctx.emitted_bootstrap_events.extend(pending_events)
            pending_events.clear()

    # Stage 7 freezes the context. If we reach here, freeze succeeded —
    # the frozen result lives on ctx.frozen.
    assert ctx.frozen is not None, "stage_7_ingress.execute must populate ctx.frozen"
    return ctx.frozen
