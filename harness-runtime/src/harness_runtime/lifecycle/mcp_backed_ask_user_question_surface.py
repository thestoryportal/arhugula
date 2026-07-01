"""MCP-server-backed `AskUserQuestionSurface` — stage 5 LOOP_INIT (U-RT-60 AC #2).

Per `Spec_Harness_Runtime_v1.md` v1.11 §14.8.3 H_E binding mechanism pin
(RATIFIED at HEAD `fb545ec` per c_rt_18 binding-mechanism fork Q1). Concrete
implementation of the `AskUserQuestionSurface` Protocol that satisfies the
v1.11 MVP binding contract: H_E AskUserQuestion is delivered through the
MCP server process boundary per workspace `CLAUDE.md` invariant I-4 +
`Phase_7_Meta_Architecture_v1.md` §7 X-AL-1 ("H_E ↔ H_T substrate boundary
at MCP server process; process isolation, not convention").

**Architecture.** The surface holds an `mcp_host: MCPHost` reference
(stage-5-materialized; placeholder at this arc per `mcp_host.py:58` —
`started=False` until FastMCP transport lands) + an injectable async
delivery callback `mcp_callback`. The callback's signature matches
`AskUserQuestionSurface.ask` byte-exact — the surface is a thin adapter
that captures latency + wraps timeouts to the typed
`AskUserQuestionTimeoutError`.

**Substitution surface (H_T-CP-20).** At this arc the surface ships with a
placeholder MCP callback (`_PlaceholderMCPCallback`) that raises
`NotImplementedError` on invocation. The wire is in place; the FastMCP
transport-level handler registration is bounded substitution carry-forward
(retired RETIRE-READY at this fork APPLIED landing per AC #14 batch 8).
Operator override path: replace `mcp_callback` at construction with a
FastMCP-host-bound async callable that delivers the prompt to the operator
process + awaits their response. Tests substitute their own callback via
the same construction path.

**Per spec §14.8 deferred-list MUST-language.** A Protocol-level mock
MUST satisfy `AskUserQuestionSurface`. This concrete impl satisfies the
Protocol via `ask(prompt, options, timeout) -> AskUserQuestionResult`;
the `_PlaceholderMCPCallback` is sentinel-only and is not invoked under
test fixtures (tests inject their own callback).

**Callback-abstraction impl-discretion citation.** The injectable
`mcp_callback: MCPAskCallback` abstraction is authorized by
`Spec_Harness_Runtime_v1.md` v1.11 §14.8.3 Q3 ratification (v1.10
introduction; preserved verbatim at v1.11): "the integration-test harness
(MCP-host-side handler fixture against the MCP-server substitution-
mechanism category per §14.8.3 v1.10 pin) **is implementation discretion**
— mechanism-specific fixture shape (e.g., `InMemoryMCPHostFixture` or
equivalent) is not pinned at v1.10 to preserve future durable-async swap
testing flexibility." The callback shape preserves the future C-RT-19 /
U-RT-61 durable-async swap surface per Q4 ratification (transparent to
the H_T runtime above this surface).

**Future durable-async swap.** Per Q4 ratification at the c_rt_18 binding-
mechanism fork: durable-async swap surface (C-RT-19 / U-RT-61) stays
inside the MCP envelope — transparent to the H_T runtime above this
surface. The surface's Protocol contract is unchanged across the swap;
only the `mcp_callback` impl changes.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from harness_cp.hitl_response_palette import HITLResponse
from pydantic import BaseModel, ConfigDict

from harness_runtime.lifecycle.ask_user_question_surface import (
    AskUserQuestionResult,
    AskUserQuestionTimeoutError,
)
from harness_runtime.lifecycle.mcp_host import MCPHost

if TYPE_CHECKING:
    from mcp.server.elicitation import AcceptedElicitation

    from harness_runtime.lifecycle.mcp_server import HarnessMCPServer

__all__ = [
    "AskUserQuestionElicitationSchema",
    "MCPAskCallback",
    "MCPBackedAskUserQuestionSurface",
    "MCPSurfaceCallbackNotBoundError",
    "ServerCtxElicitCallback",
    "materialize_mcp_backed_ask_user_question_surface_stage",
]


# Type alias for the MCP-delivery callback.
MCPAskCallback = Callable[
    [str, Sequence[HITLResponse], float | None],
    Awaitable[AskUserQuestionResult],
]
"""Async delivery callback signature: `(prompt, options, timeout) -> result`.

Bootstrap stage 5 binds `_PlaceholderMCPCallback` by default; operator-
replaceable at construction time with a FastMCP-host-bound delivery
primitive. Signature matches `AskUserQuestionSurface.ask` byte-exact so
the surface body is a thin adapter."""


class MCPSurfaceCallbackNotBoundError(NotImplementedError):
    """Default MCP callback invoked — no operator-bound delivery primitive available.

    Per `Spec_Harness_Runtime_v1.md` v1.12 §14.8.3 v1.12 workflow-initiation
    topology pin (Reading α CC-initiates): the production default at stage 5
    binds `ServerCtxElicitCallback` (per U-RT-62 AC #4) when
    `ctx.mcp_server` is materialized. The sentinel `_PlaceholderMCPCallback`
    is retained as defensive fallback for transitional bootstrap-builder
    shapes that do NOT materialize the FastMCP server (e.g., test
    substrates that explicitly leave `ctx.mcp_server = None`).

    Raised at v1.12 when:
    - Both `harness_mcp_server=` AND explicit `mcp_callback=` are absent
      at `materialize_mcp_backed_ask_user_question_surface_stage(...)`,
      AND the composer subsequently fires (test fixture / partial
      bootstrap path).
    - `ServerCtxElicitCallback.__call__` is invoked but no in-flight
      `run_workflow` tool ctx is bound on the `_CURRENT_TOOL_CTX`
      ContextVar for the current asyncio task (out-of-`api.run()`-flow
      invocation — defensive failure rather than silent absorption).

    H_T-CP-20 substitution status at v1.12 with U-RT-62 landed (per Phase
    7d retirement batch 9 record): RETIRE-READY → RETIRED. The FastMCP
    server hosting + `run_workflow` tool + Claude Code MCP-client
    connection are operational; the H_E `AskUserQuestion` surface is
    reached only via the MCP envelope per criterion B (X-AL-2).
    """


class _PlaceholderMCPCallback:
    """Defensive fallback MCP callback — raises on invocation.

    **At v1.12 (U-RT-62 landed): NOT the production default.** Stage 5
    `materialize_mcp_backed_ask_user_question_surface_stage(...)` binds
    `ServerCtxElicitCallback(mcp_server=ctx.mcp_server)` as the default
    when `ctx.mcp_server` is non-None (post-U-RT-62 bootstrap completion).
    This placeholder is retained for:

    - Test substrates that explicitly leave `ctx.mcp_server = None` and
      do NOT exercise HITL gate firing (defensive failure on accidental
      composer fire vs silent absorption).
    - Pre-bootstrap shapes that construct the surface in isolation
      without a materialized FastMCP server.
    - Future test-fixture patterns that inject a custom non-MCP
      callback via the `mcp_callback=` explicit override path.

    Per U-RT-62 AC #9 — retention reading per impl-discretion option (a):
    "retained as documented fallback for test substrates explicitly
    injecting it (with a docstring note that production binding uses
    `ServerCtxElicitCallback` per v1.12)". The `MCPSurfaceCallbackNotBoundError`
    typed error continues to fire defensively when neither
    `ServerCtxElicitCallback` nor an operator-bound callback is wired.
    """

    async def __call__(
        self,
        prompt: str,
        options: Sequence[HITLResponse],
        timeout: float | None,
    ) -> AskUserQuestionResult:
        _ = (prompt, options, timeout)
        raise MCPSurfaceCallbackNotBoundError(
            "MCPBackedAskUserQuestionSurface: no operator-bound MCP callback "
            "installed and no `ServerCtxElicitCallback` wired (transitional "
            "bootstrap-builder shape — `ctx.mcp_server` was None at stage 5 "
            "default-binding). At v1.12 production bootstrap, stage 2 AS "
            "materializes `ctx.mcp_server` per U-RT-62 AC #2 + stage 5 "
            "rebinds the default to `ServerCtxElicitCallback` per AC #4; "
            "the placeholder is retained as defensive failure for test "
            "substrates that explicitly bypass the MCP-server path."
        )


class AskUserQuestionElicitationSchema(BaseModel):
    """MCP elicitation request schema for the 4-response palette.

    Per U-RT-62 AC #4 + spec v1.12 §14.8.3 v1.12 workflow-initiation
    topology pin: `ServerCtxElicitCallback.__call__` passes this schema
    to `ctx.elicit(message, schema)`; the MCP client (Claude Code per
    Reading α CC-initiates topology) renders the dialog + returns an
    `ElicitResult` with `data` conforming to this schema when the
    operator selects accept.

    MCP elicitation schema discipline (per `modelcontextprotocol` spec
    2025-06-18): "Servers requesting structured data from users MUST
    provide a JSON schema that describes the expected response
    structure. The schema MUST be restricted to flat objects with
    primitive properties only." This model uses string-typed primitive
    fields only; the 3 optional content fields are populated by the
    client per the operator's selected response class.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    response: str
    """Operator-selected response class — one of the 4-response palette
    values per `HITLResponse` StrEnum (`approve` / `edit` / `reject` /
    `respond`)."""

    edited_proposal: str | None = None
    """Populated when `response == 'edit'` — operator-authored replacement
    payload."""

    response_text: str | None = None
    """Populated when `response == 'respond'` — operator-authored response
    text for non-action-commitment continuation."""

    rejection_reason: str | None = None
    """Populated when `response == 'reject'` — operator-authored cancellation
    reason."""


@dataclass(frozen=True)
class ServerCtxElicitCallback:
    """MCP-server-ctx-bound `AskUserQuestion` delivery (U-RT-62 AC #4).

    Per `Spec_Harness_Runtime_v1.md` v1.12 §14.8.3 v1.12 workflow-initiation
    topology pin (Reading α CC-initiates): H_T runtime hosts a FastMCP
    server; Claude Code is the registered MCP client; when a HITL gate
    fires inside the workflow body executing within a `run_workflow` MCP
    tool handler's `ctx`, this callback invokes `await ctx.elicit(...)`
    outbound on the active server session back to Claude Code.

    Replaces `_PlaceholderMCPCallback` at v1.12 stage 5 default binding.
    The placeholder is retained for test substrates that explicitly inject
    it; production binding goes through this callback.

    Binding mechanism
    -----------------

    The in-flight tool handler's `ctx` lives in a module-level
    `contextvars.ContextVar` in `lifecycle/mcp_server.py` (read via
    `HarnessMCPServer.get_current_tool_ctx()`). The `run_workflow` tool
    handler body sets the ContextVar on entry + resets it in `finally`
    per `lifecycle/mcp_server.py:materialize_mcp_server_stage()`. The
    ContextVar gives each concurrent `run_workflow` task its own ctx
    binding per spec v1.36 §14.18 chapeau per-session ctx isolation —
    distinct MCP client sessions submit independent runs that DO NOT
    share `_current_tool_ctx`. Propagation across the
    `asyncio.to_thread` → `SyncDispatcherFacade.run_coroutine_threadsafe`
    bridge is preserved (verified empirically at
    `tests/test_contextvar_bridge_propagation.py`).

    Response mapping
    ----------------

    - `ElicitResult.action == "accept"` with `data` → unpack the schema
      fields into `AskUserQuestionResult`. Response class string is
      validated against the `HITLResponse` 4-response palette.
    - `ElicitResult.action == "decline"` → `AskUserQuestionResult`
      with `response=HITLResponse.REJECT` and rejection_reason
      synthesized as "operator declined elicitation".
    - `ElicitResult.action == "cancel"` → raise
      `AskUserQuestionTimeoutError` (composer step 4f maps to
      `RT-FAIL-HITL-GATE-TIMEOUT` per spec §14.8 failure-mode taxonomy).
    """

    mcp_server: HarnessMCPServer
    """The `HarnessMCPServer` whose `get_current_tool_ctx()` accessor
    surfaces the active tool handler `ctx` (held in a module-level
    ContextVar per spec v1.36 §14.18 chapeau per-session ctx isolation).
    Bound at bootstrap stage 5 (post stage 2 MCP-server materialization)."""

    async def __call__(
        self,
        prompt: str,
        options: Sequence[HITLResponse],
        timeout: float | None,
    ) -> AskUserQuestionResult:
        """Deliver the HITL prompt via `ctx.elicit(...)`."""
        _ = (options, timeout)  # options + timeout are placement-level
        # concerns; the MCP elicit primitive carries them implicitly via
        # the active server session lifetime. timeout enforcement at
        # the surface adapter happens at the wrapping
        # MCPBackedAskUserQuestionSurface.ask() body (via asyncio.wait_for
        # on the callback if needed; v1.12 MVP relies on the MCP client's
        # native elicitation UI to honor operator deadlines).

        ctx = self.mcp_server.get_current_tool_ctx()
        if ctx is None:
            raise MCPSurfaceCallbackNotBoundError(
                "ServerCtxElicitCallback invoked but no in-flight "
                "`run_workflow` tool ctx bound on the `_CURRENT_TOOL_CTX` "
                "ContextVar for the current asyncio task. The callback is "
                "intended for HITL gate invocation inside a `run_workflow` "
                "tool body per spec v1.12 §14.8.3 topology pin (Reading α)."
            )

        elicit_result = await ctx.elicit(
            message=prompt,
            schema=AskUserQuestionElicitationSchema,
        )

        action = elicit_result.action
        if action == "accept":
            # `ctx.elicit` returns a generic union discriminated by `action` at
            # runtime; cast (not isinstance) so test doubles + any non-nominal
            # object with the AcceptedElicitation shape still flow through, and
            # the cast recovers the schema type from the generic `data` field.
            accepted = cast("AcceptedElicitation[AskUserQuestionElicitationSchema]", elicit_result)
            data = cast("AskUserQuestionElicitationSchema | None", accepted.data)
            if data is None:
                raise MCPSurfaceCallbackNotBoundError(
                    "ctx.elicit returned action='accept' but data is None — "
                    "MCP client did not populate the elicitation schema"
                )
            try:
                response_class = HITLResponse(data.response)
            except ValueError as exc:
                raise MCPSurfaceCallbackNotBoundError(
                    f"ctx.elicit returned response={data.response!r} not in "
                    f"4-response palette per C-CP-16 §16.1"
                ) from exc
            return AskUserQuestionResult(
                response=response_class,
                latency_ms=0.0,  # filled by surface adapter via monotonic
                edited_proposal=data.edited_proposal,
                response_text=data.response_text,
                rejection_reason=data.rejection_reason,
            )
        if action == "decline":
            return AskUserQuestionResult(
                response=HITLResponse.REJECT,
                latency_ms=0.0,
                rejection_reason="operator declined elicitation",
            )
        # action == "cancel"
        raise AskUserQuestionTimeoutError(
            "MCP elicitation cancelled by operator (ElicitResult.action == 'cancel'); "
            "composer step 4f maps to RT-FAIL-HITL-GATE-TIMEOUT per spec §14.8."
        )


@dataclass(frozen=True)
class MCPBackedAskUserQuestionSurface:
    """AskUserQuestionSurface bound to an MCP-server-backed delivery callback.

    Satisfies `AskUserQuestionSurface` Protocol per spec §14.8.1 item 2.
    `ask(...)` delegates to `mcp_callback` + wraps `TimeoutError` (raised by
    the callback when the operator delivery deadline elapses) to the typed
    `AskUserQuestionTimeoutError` the composer step 4f catches.

    Fields
    ------
    mcp_host :
        Stage-5-materialized MCP host (placeholder at this arc per
        `mcp_host.py:58`). Field holds the reference for X-AL-1
        process-isolation discipline traceability; not invoked directly
        at v1.11 MVP (the callback layer handles transport).
    mcp_callback :
        Async delivery callback. Defaults to `_PlaceholderMCPCallback()`
        which raises `MCPSurfaceCallbackNotBoundError` on invocation.
        Tests + operator-bound production deployments replace at
        construction time.
    """

    mcp_host: MCPHost
    mcp_callback: MCPAskCallback = field(default_factory=_PlaceholderMCPCallback)

    async def ask(
        self,
        prompt: str,
        options: Sequence[HITLResponse],
        timeout: float | None,
    ) -> AskUserQuestionResult:
        """Deliver the prompt via the MCP-backed callback + return the result.

        Wraps the callback's `TimeoutError` (Python builtin, raised by
        `asyncio.wait_for` or equivalent in the callback impl) to the
        typed `AskUserQuestionTimeoutError` the composer body catches at
        step 4f per spec §14.8.2.

        Latency capture: if the callback's returned `result.latency_ms`
        is non-positive (sentinel zero), the surface fills it from the
        wall-clock elapsed at the call site.
        """
        start = time.monotonic()
        try:
            result = await self.mcp_callback(prompt, options, timeout)
        except TimeoutError as exc:
            raise AskUserQuestionTimeoutError(
                f"MCP-backed AskUserQuestion timed out after timeout={timeout}s"
            ) from exc

        if result.latency_ms <= 0.0:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return AskUserQuestionResult(
                response=result.response,
                latency_ms=elapsed_ms,
                edited_proposal=result.edited_proposal,
                response_text=result.response_text,
                rejection_reason=result.rejection_reason,
            )
        return result


def materialize_mcp_backed_ask_user_question_surface_stage(
    mcp_host: MCPHost,
    *,
    mcp_callback: MCPAskCallback | None = None,
    harness_mcp_server: HarnessMCPServer | None = None,
) -> MCPBackedAskUserQuestionSurface:
    """Construct the MCP-backed surface at bootstrap stage 5 LOOP_INIT.

    Default-binding precedence (highest to lowest):

    1. Explicit `mcp_callback=` override — operator-supplied delivery
       primitive (test fixture or operator-bound production deployment).
    2. `harness_mcp_server=` provided (U-RT-62 default at v1.12) →
       `ServerCtxElicitCallback(harness_mcp_server)` per spec §14.8.3
       v1.12 workflow-initiation topology pin (Reading α CC-initiates).
    3. Neither provided → `_PlaceholderMCPCallback()` sentinel that
       raises `MCPSurfaceCallbackNotBoundError` on invocation. Retained
       for test substrates that don't materialize the FastMCP server +
       for transitional bootstrap-builder shapes that don't yet write
       `ctx.mcp_server`.

    Per `Spec_Harness_Runtime_v1.md` v1.12 §14.8.3 v1.12 workflow-
    initiation topology pin: production stage 5 wiring passes
    `harness_mcp_server=ctx.mcp_server` so the surface routes through
    `ServerCtxElicitCallback` → `ctx.elicit(...)` outbound on the
    active server session per Reading α.
    """
    if mcp_callback is None and harness_mcp_server is not None:
        mcp_callback = ServerCtxElicitCallback(mcp_server=harness_mcp_server)
    if mcp_callback is None:
        return MCPBackedAskUserQuestionSurface(mcp_host=mcp_host)
    return MCPBackedAskUserQuestionSurface(
        mcp_host=mcp_host,
        mcp_callback=mcp_callback,
    )
