"""U-RT-62 AC #4 — `ServerCtxElicitCallback` unit tests.

Per `Spec_Harness_Runtime_v1.md` v1.12 §14.8.3 v1.12 workflow-initiation
topology pin (Reading α CC-initiates): the callback reads the in-flight
`run_workflow` tool ctx from `HarnessMCPServer._state['_current_tool_ctx']`,
invokes `await ctx.elicit(message, schema)`, and maps the ElicitResult to
an AskUserQuestionResult per the 4-response palette.

Covered cases:
- accept + valid data → AskUserQuestionResult per palette
- accept + invalid palette member → MCPSurfaceCallbackNotBoundError
- accept + missing data → MCPSurfaceCallbackNotBoundError
- decline → AskUserQuestionResult(REJECT, ...) with synthesized reason
- cancel → AskUserQuestionTimeoutError
- ctx not bound on _state → MCPSurfaceCallbackNotBoundError
- materialize_..._stage with harness_mcp_server → ServerCtxElicitCallback default
- materialize_..._stage without harness_mcp_server → placeholder fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from harness_cp.hitl_response_palette import HITLResponse
from harness_runtime.lifecycle.ask_user_question_surface import (
    AskUserQuestionTimeoutError,
)
from harness_runtime.lifecycle.mcp_backed_ask_user_question_surface import (
    AskUserQuestionElicitationSchema,
    MCPSurfaceCallbackNotBoundError,
    ServerCtxElicitCallback,
    _PlaceholderMCPCallback,  # type: ignore[attr-defined]
    materialize_mcp_backed_ask_user_question_surface_stage,
)
from harness_runtime.lifecycle.mcp_host import MCPHost
from harness_runtime.lifecycle.mcp_server import HarnessMCPServer


@dataclass
class _MockElicitResult:
    """Minimal stand-in for `mcp.types.ElicitResult`."""

    action: str
    data: Any = None


class _MockCtx:
    """Minimal stand-in for `mcp.server.fastmcp.Context`."""

    def __init__(self, result: _MockElicitResult) -> None:
        self._result = result
        self.calls: list[tuple[str, type]] = []

    async def elicit(self, message: str, schema: type) -> _MockElicitResult:
        self.calls.append((message, schema))
        return self._result


def _server_with_ctx(ctx: Any | None) -> HarnessMCPServer:
    server = HarnessMCPServer(server=object())
    if ctx is not None:
        # Per-session ctx isolation per spec v1.36 §14.18 chapeau: the
        # in-flight tool ctx lives on a module-level ContextVar (NOT
        # `server._state`). Test setup binds it for the current asyncio
        # task; the binding lives for the duration of the calling test
        # function (asyncio task termination releases the ContextVar).
        server.set_current_tool_ctx(ctx)
    return server


@pytest.mark.asyncio
async def test_accept_with_valid_data_returns_palette_result() -> None:
    """AC #4: action='accept' + valid palette response → AskUserQuestionResult."""
    schema_instance = AskUserQuestionElicitationSchema(
        response="approve",
        edited_proposal=None,
        response_text=None,
        rejection_reason=None,
    )
    ctx = _MockCtx(_MockElicitResult(action="accept", data=schema_instance))
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx))

    result = await callback("test prompt", [HITLResponse.APPROVE], None)

    assert result.response == HITLResponse.APPROVE
    assert len(ctx.calls) == 1
    assert ctx.calls[0] == ("test prompt", AskUserQuestionElicitationSchema)


@pytest.mark.asyncio
async def test_accept_edit_carries_edited_proposal() -> None:
    """AC #4: EDIT response carries the operator-authored replacement payload."""
    schema_instance = AskUserQuestionElicitationSchema(
        response="edit",
        edited_proposal="revised proposal text",
        response_text=None,
        rejection_reason=None,
    )
    ctx = _MockCtx(_MockElicitResult(action="accept", data=schema_instance))
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx))

    result = await callback("p", [HITLResponse.EDIT], None)

    assert result.response == HITLResponse.EDIT
    assert result.edited_proposal == "revised proposal text"


@pytest.mark.asyncio
async def test_decline_returns_synthesized_reject() -> None:
    """AC #4: action='decline' → AskUserQuestionResult(REJECT, ...) synthesized."""
    ctx = _MockCtx(_MockElicitResult(action="decline", data=None))
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx))

    result = await callback("p", [HITLResponse.APPROVE], None)

    assert result.response == HITLResponse.REJECT
    assert result.rejection_reason == "operator declined elicitation"


@pytest.mark.asyncio
async def test_cancel_raises_timeout_error() -> None:
    """AC #4: action='cancel' → AskUserQuestionTimeoutError (composer step 4f maps
    to RT-FAIL-HITL-GATE-TIMEOUT per spec §14.8 failure-mode taxonomy).
    """
    ctx = _MockCtx(_MockElicitResult(action="cancel", data=None))
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx))

    with pytest.raises(AskUserQuestionTimeoutError, match="cancelled by operator"):
        await callback("p", [HITLResponse.APPROVE], None)


@pytest.mark.asyncio
async def test_accept_with_missing_data_raises() -> None:
    """AC #4: action='accept' + data=None is a protocol-level defect from the
    MCP client (operator response not captured per schema).
    """
    ctx = _MockCtx(_MockElicitResult(action="accept", data=None))
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx))

    with pytest.raises(MCPSurfaceCallbackNotBoundError, match="data is None"):
        await callback("p", [HITLResponse.APPROVE], None)


@pytest.mark.asyncio
async def test_accept_with_off_palette_response_raises() -> None:
    """AC #4: response string outside 4-response palette is a protocol-level defect."""
    schema_instance = AskUserQuestionElicitationSchema(response="unknown")
    ctx = _MockCtx(_MockElicitResult(action="accept", data=schema_instance))
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx))

    with pytest.raises(MCPSurfaceCallbackNotBoundError, match="not in"):
        await callback("p", [HITLResponse.APPROVE], None)


@pytest.mark.asyncio
async def test_no_ctx_bound_raises() -> None:
    """AC #4: callback invoked outside a `run_workflow` tool body → typed error.
    Indicates the HITL composer fired without the topology pin's tool ctx
    available (programming defect or out-of-flow invocation).
    """
    callback = ServerCtxElicitCallback(mcp_server=_server_with_ctx(ctx=None))

    with pytest.raises(MCPSurfaceCallbackNotBoundError, match="no in-flight"):
        await callback("p", [HITLResponse.APPROVE], None)


def test_materialize_with_harness_mcp_server_defaults_to_server_ctx_callback() -> None:
    """AC #4: stage 5 default precedence — when `harness_mcp_server=` is
    provided + `mcp_callback=` is None, the surface binds
    `ServerCtxElicitCallback` (NOT the placeholder).
    """
    host = MCPHost(started=False)
    mcp_server = HarnessMCPServer(server=object())
    surface = materialize_mcp_backed_ask_user_question_surface_stage(
        host, harness_mcp_server=mcp_server
    )
    assert isinstance(surface.mcp_callback, ServerCtxElicitCallback)
    assert surface.mcp_callback.mcp_server is mcp_server


def test_materialize_without_harness_mcp_server_falls_back_to_placeholder() -> None:
    """AC #4 + AC #9: when neither override path applies, the surface binds
    `_PlaceholderMCPCallback` (defensive default; raises on invocation).
    """
    host = MCPHost(started=False)
    surface = materialize_mcp_backed_ask_user_question_surface_stage(host)
    assert isinstance(surface.mcp_callback, _PlaceholderMCPCallback)


def test_materialize_explicit_callback_overrides_harness_mcp_server() -> None:
    """AC #4: explicit `mcp_callback=` override beats `harness_mcp_server=`
    default (test-fixture path — operator can inject a custom callable).
    """
    host = MCPHost(started=False)
    mcp_server = HarnessMCPServer(server=object())

    async def _custom(prompt: str, options: Any, timeout: Any) -> Any:
        _ = (prompt, options, timeout)
        raise NotImplementedError("custom callback")

    surface = materialize_mcp_backed_ask_user_question_surface_stage(
        host, mcp_callback=_custom, harness_mcp_server=mcp_server
    )
    assert surface.mcp_callback is _custom
