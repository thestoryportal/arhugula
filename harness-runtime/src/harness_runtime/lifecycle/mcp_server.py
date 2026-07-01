"""U-RT-62 — FastMCP server hosting (H_T-as-MCP-server topology).

Per `Spec_Harness_Runtime_v1.md` v1.12 §14.8.3 v1.12 workflow-initiation
topology pin: H_T runtime is the **MCP server**; Claude Code is the
registered MCP client; workflow execution is invoked by Claude Code
calling the `run_workflow` MCP tool; HITL `ctx.elicit(...)` rides the
active server session outbound back to Claude Code.

Sibling to `lifecycle/mcp_host.py` (U-RT-15 — H_T-as-MCP-client surface).
The two MCP roles are orthogonal per Q3 + Q5 disjointness ratification at
fork `class_1_tension_c_rt_18_mcp_workflow_initiation_topology_underspec.md`.

Composition surface
-------------------

- `HarnessMCPServer`: frozen dataclass wrapping a `mcp.server.fastmcp.FastMCP`
  instance + lifecycle state. The `started` field is set `True` after the
  `run_workflow` tool registration completes at bootstrap stage 2.
- `workflow_registry`: `dict[str, WorkflowObject]` — mutable holder keyed
  by `workflow.workflow_id`. `api.run()` pre-registers each workflow before
  invoking the in-process `run_workflow` tool.
- `_state`: mutable holder dict carrying post-bootstrap state that the
  `run_workflow` tool handler reads:
    - `_harness_ctx`: the full post-bootstrap `HarnessContext` —
      consumed by the tool body to reach `ctx.step_dispatchers` +
      `ctx.audit_writer` etc. when dispatching `execute_workflow`.
- `_CURRENT_TOOL_CTX` (module-level `contextvars.ContextVar`): the
  in-flight `Context[ServerSession, None]` — set by the `run_workflow`
  tool handler on entry; read by `ServerCtxElicitCallback` (per AC #4)
  to invoke `await ctx.elicit(...)` outbound on the active server session.

The `_state` holder is required because the frozen dataclass is
constructed at stage 2 — before stage 5 LOOP_INIT wiring completes — yet
the tool handler (registered at stage 2) consumes state populated at
`api.run()` time (post-bootstrap).

**Per-session ctx isolation (spec v1.36 §14.18 chapeau).** The active
MCP tool ctx is held in a module-level `contextvars.ContextVar`, NOT a
`_state` dict key. This is required by the spec-MUST invariant that
concurrent `run_workflow` invocations from distinct MCP client sessions
are INDEPENDENT runs (not concurrent re-entry of the same `api.run()`).
A shared `_state` key would race across concurrent invocations and route
the wrong client's ctx into `ServerCtxElicitCallback`. ContextVar gives
each asyncio task its own value; propagation across the
`asyncio.to_thread` → `SyncDispatcherFacade.run_coroutine_threadsafe`
bridge is preserved (verified empirically at
`tests/test_contextvar_bridge_propagation.py`).
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from harness_runtime.lifecycle.inter_step_output_channel import (
    INTER_STEP_CHANNEL_VAR,
    InterStepOutputChannel,
)

_MANIFEST_PATH_SUFFIXES: frozenset[str] = frozenset({".yaml", ".yml", ".toml"})
_HARNESS_MCP_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1",
    "127.0.0.1:*",
    "localhost",
    "localhost:*",
    "[::1]",
    "[::1]:*",
    "0.0.0.0",
    "0.0.0.0:*",
)
_HARNESS_MCP_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)


def _looks_like_manifest_path(workflow_id: str) -> bool:
    """Discriminator for the daemon-mode workflow_id-as-path widening.

    Per `.harness/class_1_fork_u_rt_107_daemon_run_workflow_signature_
    underspec.md` Reading (A) + Q2=(i) ratification 2026-05-28: workflow_id
    is treated as a filesystem path iff it contains '/' OR ends in one of
    `.yaml` / `.yml` / `.toml`.
    """
    if "/" in workflow_id:
        return True
    # Fast-path suffix check without constructing Path objects.
    lower = workflow_id.lower()
    return any(lower.endswith(suffix) for suffix in _MANIFEST_PATH_SUFFIXES)


if TYPE_CHECKING:
    from harness_runtime.api import WorkflowObject

__all__ = [
    "HarnessMCPServer",
    "materialize_mcp_server_stage",
]


# Per-session ctx isolation per spec v1.36 §14.18 chapeau. Each concurrent
# `run_workflow` invocation gets its own asyncio task; the ContextVar binds
# the in-flight MCP tool ctx for the duration of that task and is read by
# `ServerCtxElicitCallback` via `HarnessMCPServer.get_current_tool_ctx()`.
# Bridge propagation (asyncio.to_thread → run_coroutine_threadsafe) verified
# at `tests/test_contextvar_bridge_propagation.py`.
_CURRENT_TOOL_CTX: contextvars.ContextVar[Context[Any, Any] | None] = contextvars.ContextVar(
    "harness.current_tool_ctx", default=None
)

# B-INTERSTEP-PERRUN-ISOLATION (runtime spec §14.21 C-RT-34 invariant 7;
# B-INTERSTEP fork §3/§5) — SUPERSEDES B-INTERSTEP's per-loop single-flight
# lock. The run-scoped inter-step channel + cost accumulator are now ISOLATED
# per run via ContextVars (`INTER_STEP_CHANNEL_VAR` / `COST_ACCUM_VAR`, set in
# the `run_workflow` handler below). Two concurrent `run_workflow` invocations
# on the ONE reused bootstrap `HarnessContext` (daemon-client mode, U-RT-108 —
# one ctx serves many invocations) each run in their own asyncio task → own
# `contextvars` copy → own holders, so they cannot interleave WITHOUT
# serialization, and a run that exceeds `drain_timeout_seconds` leaves a
# non-cancellable `to_thread` zombie that writes only the holder captured in
# ITS context copy — never a following run's. The (7b) single-flight lock AND
# the (7c) timeout-zombie residual are both closed; the ctx-bound proxies
# (`RunScopedInterStepOutputChannel` / `RunScopedCostRecordAccumulator`)
# transparently resolve each run's holder for the dispatcher + driver readers.


@dataclass(frozen=True)
class HarnessMCPServer:
    """Runtime composition primitive — H_T-as-MCP-server hosting.

    Distinct from `MCPHost` (U-RT-15 H_T-as-MCP-client surface). The
    `HarnessContext` schema carries both: `mcp_host` for the client
    surface (consumes filesystem / GitHub / sandbox MCP servers) and
    `mcp_server` for the server surface (hosts the `run_workflow` tool
    that Claude Code invokes to execute workflows).

    The dataclass is frozen at the binding level; mutable state required
    by the tool handler lives in `workflow_registry` + `_state` dicts.
    """

    server: Any
    """The wrapped `mcp.server.fastmcp.FastMCP` instance.

    Typed `Any` to avoid pulling the FastMCP type into the static
    binding here (FastMCP is a heavyweight class with non-trivial type
    surface); the `lifecycle/mcp_server.py` constructor pins the type
    at materialization, and consumers (tool handler body,
    `ServerCtxElicitCallback`) operate against the runtime instance.
    """

    started: bool = False
    """Lifecycle flag — `True` after `materialize_mcp_server_stage()`
    registers the `run_workflow` tool. Bootstrap stage 2 rollback
    discipline (per existing 9-stage error handling) is preserved:
    on tool registration failure, the stage raises and the
    constructed `HarnessMCPServer(started=False)` is discarded."""

    workflow_registry: dict[str, WorkflowObject] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    """Workflow lookup table keyed by `workflow.workflow_id`.

    `api.run()` writes the operator-supplied `WorkflowObject` here
    before invoking the `run_workflow` MCP tool. The tool handler
    body reads back by id and dispatches via `execute_workflow`.
    Mutable on a frozen dataclass: the field binding is frozen but
    the dict's contents are not.
    """

    _state: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    """Post-bootstrap state holder. Written once by `api.run()` before
    invoking the `run_workflow` tool; read by the tool handler body.

    Keys:
    - `_harness_ctx`: the full post-bootstrap `HarnessContext` — set by
      `api.run()` before opening the in-process `ClientSession`. Singleton
      per process (mandated by C-RT-06 set_tracer_provider one-per-process
      invariant; shared across concurrent daemon-mode client sessions).

    NOTE: the in-flight MCP tool ctx is NOT held on `_state` — it lives on
    the module-level `_CURRENT_TOOL_CTX` ContextVar so concurrent
    `run_workflow` invocations from distinct MCP clients see isolated ctx
    values per spec v1.36 §14.18 chapeau. Access via the
    `get_current_tool_ctx()` / `set_current_tool_ctx()` /
    `reset_current_tool_ctx()` methods below.
    """

    def get_current_tool_ctx(self) -> Context[Any, Any] | None:
        """Return the in-flight MCP tool ctx for the current asyncio task,
        or None if no `run_workflow` invocation is in flight on this task.

        Read site for `ServerCtxElicitCallback`. ContextVar semantics
        guarantee each concurrent `run_workflow` task sees its own value.
        """
        return _CURRENT_TOOL_CTX.get()

    def set_current_tool_ctx(
        self, ctx: Context[Any, Any]
    ) -> contextvars.Token[Context[Any, Any] | None]:
        """Bind the in-flight MCP tool ctx for the current asyncio task.

        Returns a `contextvars.Token` that must be passed to
        `reset_current_tool_ctx` in a `finally` block to release the binding.
        Per spec v1.36 §14.18 chapeau per-session ctx isolation.
        """
        return _CURRENT_TOOL_CTX.set(ctx)

    def reset_current_tool_ctx(self, token: contextvars.Token[Context[Any, Any] | None]) -> None:
        """Release a binding previously installed by `set_current_tool_ctx`.

        Mirrors the `try/finally` discipline that `_state.pop(...)` used in
        the pre-isolation implementation, but operates on task-local
        ContextVar state instead of shared dict state.
        """
        _CURRENT_TOOL_CTX.reset(token)


def materialize_mcp_server_stage(
    *,
    drain_timeout_seconds: float,
) -> HarnessMCPServer:
    """Bootstrap stage 2 AS — construct FastMCP server + register `run_workflow`.

    Per `Spec_Harness_Runtime_v1.md` v1.12 §14.8.3 v1.12 workflow-initiation
    topology pin + Phase 2 Session 3 plan v2.10 §2 L9-quinquies U-RT-62 AC #2.

    Steps
    -----
    1. Construct `mcp.server.fastmcp.FastMCP(name="harness-runtime")`.
    2. Allocate mutable `workflow_registry` + `_state` dicts (captured by
       the tool handler's closure; the same instances are placed on the
       returned `HarnessMCPServer` dataclass so post-bootstrap `api.run()`
       can write to them by reference).
    3. Register the `run_workflow` MCP tool via `@fastmcp.tool()` decorator.
       The handler body dispatches `execute_workflow` from the CP axis on
       a worker thread (via `asyncio.to_thread`); the HITL gate composer
       inside `execute_workflow` bridges back to the main loop via
       `SyncDispatcherFacade.run_coroutine_threadsafe` and awaits
       `ctx.ask_user_question_surface.ask(...)` — which routes through
       `ServerCtxElicitCallback` (per AC #4) to call `await ctx.elicit(...)`
       outbound on the active server session per the v1.12 topology pin.
    4. Return `HarnessMCPServer(started=True, ...)` after registration
       completes. Bootstrap stage 2 rollback discipline preserved: on
       FastMCP constructor failure OR tool registration failure, this
       function raises and the partial state is discarded.

    Returns
    -------
    HarnessMCPServer
        Frozen dataclass with `started=True` after tool registration.
    """
    # Lazy import to keep the `lifecycle/mcp_server.py` → `harness_cp` edge
    # at runtime invocation (the CP driver does not need to load at module
    # import time when the FastMCP server is constructed at stage 2 but
    # not yet exercised; the import resolves on first tool invocation via
    # the closure lookup).
    from harness_cp.workflow_driver import execute_workflow as _execute_workflow

    fastmcp = FastMCP(
        name="harness-runtime",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(_HARNESS_MCP_ALLOWED_HOSTS),
            allowed_origins=list(_HARNESS_MCP_ALLOWED_ORIGINS),
        ),
    )
    workflow_registry: dict[str, Any] = {}
    state: dict[str, Any] = {}

    @fastmcp.tool()
    async def run_workflow(  # pyright: ignore[reportUnusedFunction] — registered via @fastmcp.tool() decorator
        workflow_id: str, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Execute one workflow per the v1.12 H_T-as-MCP-server topology.

        The workflow body executes inside this tool handler's `ctx` per the
        topology pin. The HITL gate composer (per U-RT-60) bridges back to
        the main loop via `SyncDispatcherFacade.run_coroutine_threadsafe`
        and reaches `await ctx.elicit(...)` through
        `ServerCtxElicitCallback` (per AC #4) which reads this `ctx` from
        the module-level `_CURRENT_TOOL_CTX` ContextVar bound below. Each
        concurrent `run_workflow` invocation runs in its own asyncio task
        and sees its own ContextVar value per spec v1.36 §14.18 chapeau
        per-session ctx isolation.

        Parameters
        ----------
        workflow_id
            Key into `workflow_registry`; the operator-supplied
            `WorkflowObject` was pre-registered by `api.run()` per AC #5.
        ctx
            In-flight FastMCP tool handler context; carries the active
            server session for outbound `ctx.elicit(...)` calls.

        Returns
        -------
        dict[str, Any]
            JSON-serializable form of the CP driver's `RunResult`. The
            `api.run()` caller per AC #5 re-parses into the CP model and
            projects to the runtime-facing `RunResult` per C-RT-09.
        """
        harness_ctx = state.get("_harness_ctx")
        if harness_ctx is None:
            raise RuntimeError(
                "`run_workflow` invoked before `api.run()` bound the post-bootstrap "
                "`HarnessContext` on `HarnessMCPServer._state['_harness_ctx']`. "
                "The tool is intended for in-process invocation from `api.run()` "
                "per spec v1.12 §14.8.3 topology pin (Reading α)."
            )

        # workflow_id discriminator per `.harness/class_1_fork_u_rt_107_daemon_
        # run_workflow_signature_underspec.md` Reading (A) + Q2=(i) ratification
        # 2026-05-28. workflow_id is treated as a filesystem path iff it
        # contains '/' OR ends in one of `.yaml` / `.yml` / `.toml`; otherwise
        # registry key. Path-input invocations (daemon-client mode per U-RT-108)
        # load the manifest via `WorkflowManifestLoader.load_workflow(path)` on
        # every invocation (Q3=(a) no-cache). Registry-key path preserves the
        # in-process `api.run()` pre-registration semantics verbatim.
        if _looks_like_manifest_path(workflow_id):
            from harness_runtime.lifecycle.workflow_manifest_loader import (
                WorkflowManifestLoader,
                WorkflowManifestLoadError,
            )

            try:
                workflow = WorkflowManifestLoader.load_workflow(Path(workflow_id))
            except WorkflowManifestLoadError as exc:
                raise RuntimeError(
                    f"daemon-client run_workflow failed to load manifest at "
                    f"path {workflow_id!r}: {exc.FAIL_CLASS}: {exc.reason}"
                ) from exc
        else:
            workflow = workflow_registry.get(workflow_id)
            if workflow is None:
                raise RuntimeError(
                    f"workflow {workflow_id!r} not registered in "
                    f"`HarnessMCPServer.workflow_registry`; `api.run()` writes the "
                    f"`WorkflowObject` keyed by `workflow.workflow_id` before "
                    f"invoking the `run_workflow` tool per AC #5."
                )

        # C-RT-35 (R-CC-1 arc #3) resume path. `api.resume()` binds an
        # in-process `_resume_pause_snapshot` on `_state` (NOT over the MCP
        # wire — mirrors `_harness_ctx`). When present, this invocation is a
        # resume: reuse the snapshot's `run_id` for audit/ledger coherence
        # (resume *position* comes from `snapshot.step_index` via the driver's
        # entry-point override, not from run_id) + thread the snapshot to the
        # driver as `pause_snapshot_input=` so entry-point resume detection
        # (`workflow_driver.py` C-RT-24 §14.14.3) fires.
        _resume_snapshot = state.get("_resume_pause_snapshot")
        run_id = _resume_snapshot.run_id if _resume_snapshot is not None else uuid.uuid4().hex
        # One-shot resume-context delivery to the resumed-step HITL gate
        # (CP spec v1.16 §26.8.5 → runtime ResumeContextHolder sidecar).
        _resume_context = state.get("_resume_context")
        if _resume_context is not None:
            _holder = getattr(harness_ctx, "resume_context_holder", None)
            if _holder is not None:
                _holder.set(_resume_context)
        # B-INTERSTEP-PERRUN-ISOLATION (runtime spec §14.21 C-RT-34 invariant 7;
        # B-INTERSTEP fork §3/§5) — establish THIS run's isolated holders in their
        # ContextVars before dispatch. The set propagates into the
        # `asyncio.to_thread(execute_workflow)` worker via `contextvars.copy_
        # context()` (the CP driver + LLM dispatcher read the ctx-bound proxies,
        # which resolve these vars), so two concurrent `run_workflow` invocations
        # on the ONE reused bootstrap `HarnessContext` (daemon-client mode) each
        # see their own holders WITHOUT serialization, and a timed-out
        # non-cancellable `to_thread` zombie writes only the holder captured in ITS
        # context copy — closing the (7b) lock-serialization AND the (7c)
        # timeout-zombie. The handler runs per-invocation in its own asyncio task,
        # so the var binding is task-local + discarded when the task ends (no
        # explicit reset needed; cf. the `_CURRENT_TOOL_CTX` token below, which
        # IS reset because `ServerCtxElicitCallback` reads it on the SAME task).
        #
        # Channel (opt-in): a fresh isolated channel per invocation when the proxy
        # is bound. A resume starts fresh (empty) — cross-step resume rehydration
        # is the registered B-ENGINE-OUTPUT-REPLAY arc (the replayed prefix is not
        # re-dispatched); the resumed run's intra-invocation EO data flow is
        # unaffected. Opt-out default (proxy None) → no set, byte-identical.
        _channel_token: contextvars.Token[InterStepOutputChannel | None] | None = None
        if getattr(harness_ctx, "inter_step_output_channel", None) is not None:
            _channel_token = INTER_STEP_CHANNEL_VAR.set(InterStepOutputChannel())
        # Cost accumulator (always-on): a fresh one per run ONLY when no caller has
        # already established one. `api.run`/`resume` set it around `[invoke +
        # read]` so their post-run `ctx.cost_record_accumulator.records` read
        # resolves to the SAME accumulator the wrappers append to (caller-set
        # propagates DOWN to this handler + the worker; a handler-set value would
        # NOT propagate back UP to that caller). The daemon path has no such caller
        # → this handler establishes the per-run accumulator so concurrent daemon
        # runs do not share one. Direct/child paths (no run boundary) leave the var
        # unset → the proxy falls back to its bootstrap default (byte-identical).
        from harness_runtime.types import COST_ACCUM_VAR, CostRecordAccumulator

        _cost_token: contextvars.Token[CostRecordAccumulator | None] | None = None
        if COST_ACCUM_VAR.get() is None:
            _cost_token = COST_ACCUM_VAR.set(CostRecordAccumulator())
        # Bind the in-flight tool ctx for the duration of the workflow
        # execution per spec v1.36 §14.18 chapeau per-session ctx isolation.
        # `ServerCtxElicitCallback` (per AC #4) reads via the module-level
        # ContextVar to reach `await ctx.elicit(...)` from the HITL gate
        # composer; each concurrent invocation sees its own value.
        ctx_token = _CURRENT_TOOL_CTX.set(ctx)

        # Workflow-supplied dispatcher override (test-fixture surface);
        # falls back to `ctx.step_dispatchers` from stage 5 LOOP_INIT.
        workflow_step_dispatchers = getattr(workflow, "step_dispatchers", None)
        effective_step_dispatchers = (
            workflow_step_dispatchers
            if workflow_step_dispatchers is not None
            else getattr(harness_ctx, "step_dispatchers", None)
        )

        try:
            # The per-run inter-step channel + cost accumulator were isolated in
            # their ContextVars above; the `asyncio.to_thread` worker inherits them
            # via `copy_context()` (no lock needed — B-INTERSTEP-PERRUN-ISOLATION).
            # Same composition pattern as the v1.11 `api.run()` baseline
            # (asyncio.to_thread for the sync CP driver; asyncio.wait_for to enforce
            # `RT-FAIL-DRAIN-TIMEOUT` per C-RT-14 + U-RT-44 AC #2).
            cp_result = await asyncio.wait_for(
                asyncio.to_thread(
                    _execute_workflow,
                    workflow.manifest_entry,
                    workflow.steps,
                    run_id,
                    harness_ctx,
                    default_model_binding=workflow.default_model_binding,
                    step_dispatchers=cast(Any, effective_step_dispatchers),
                    pause_snapshot_input=_resume_snapshot,
                ),
                timeout=drain_timeout_seconds,
            )
            return cp_result.model_dump(mode="json")
        except TimeoutError:
            # `RT-FAIL-DRAIN-TIMEOUT` projection per U-RT-44 AC #2;
            # the api.run caller per AC #5 unmarshals and re-builds the
            # runtime RunResult with status='drained'.
            from harness_cp.workflow_driver_types import (
                RunResult as _CpRunResult,
            )
            from harness_cp.workflow_driver_types import (
                RunStatus as _CpRunStatus,
            )

            drained = _CpRunResult(
                workflow_id=workflow.manifest_entry.workflow_id,
                run_id=run_id,
                status=_CpRunStatus.DRAINED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class="RT-FAIL-DRAIN-TIMEOUT",
            )
            return drained.model_dump(mode="json")
        finally:
            _CURRENT_TOOL_CTX.reset(ctx_token)
            # Reset the per-run holder vars THIS handler set (Codex pre-merge
            # [P2]): if the same asyncio task serves a later `run_workflow`
            # invocation, a surviving binding would make that call skip its fresh
            # allocation (the cost var's `if None` guard) and share a sink across
            # runs. Mirrors the `ctx_token` discipline. A CALLER-set cost var
            # (api.run/resume) is reset by that caller — this handler captured no
            # token for it (the `if None` guard was False), so it is left intact.
            if _channel_token is not None:
                INTER_STEP_CHANNEL_VAR.reset(_channel_token)
            if _cost_token is not None:
                COST_ACCUM_VAR.reset(_cost_token)

    return HarnessMCPServer(
        server=fastmcp,
        started=True,
        workflow_registry=workflow_registry,
        _state=state,
    )
