"""U-RT-62 — `HarnessMCPServer` primitive declaration tests (AC #1).

Per Phase 2 Session 3 plan v2.10 §2 L9-quinquies U-RT-62 AC #1:
- `HarnessMCPServer` is a frozen dataclass distinct from `MCPHost`.
- Carries `server` (FastMCP instance handle), `started: bool`, mutable
  `workflow_registry` + `_state` holders.
- `HarnessContext` schema admits both `mcp_host: MCPHost` (existing,
  H_T-as-MCP-client) AND `mcp_server: HarnessMCPServer | None` (new,
  H_T-as-MCP-server). The two roles are orthogonal per Q3 + Q4
  sibling-primitive ratification at the C-RT-18 v1.12 fork.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from harness_runtime.lifecycle.mcp_host import MCPHost
from harness_runtime.lifecycle.mcp_server import HarnessMCPServer


def test_harness_mcp_server_is_frozen_dataclass() -> None:
    """AC #1: frozen dataclass; binding immutable post-construction."""
    server = HarnessMCPServer(server=object(), started=False)
    assert dataclasses.is_dataclass(server)
    assert dataclasses.fields(server)  # has fields
    with pytest.raises(dataclasses.FrozenInstanceError):
        server.started = True  # type: ignore[misc]


def test_harness_mcp_server_carries_required_fields() -> None:
    """AC #1: server + started + workflow_registry + _state present."""
    server_instance = object()
    server = HarnessMCPServer(server=server_instance)
    assert server.server is server_instance
    assert server.started is False  # default
    assert server.workflow_registry == {}
    assert server._state == {}


def test_workflow_registry_is_mutable_on_frozen_dataclass() -> None:
    """AC #1: dict-valued field mutable even on frozen dataclass.

    Required because `api.run()` writes the operator-supplied
    `WorkflowObject` to the registry per workflow.workflow_id before
    invoking the `run_workflow` MCP tool (per AC #5 thin-wrapper reframe).
    """
    server = HarnessMCPServer(server=object())
    server.workflow_registry["wf-1"] = object()  # type: ignore[assignment]
    assert "wf-1" in server.workflow_registry


def test_state_holder_is_mutable_on_frozen_dataclass() -> None:
    """AC #1 + AC #5 prerequisite: `_state` holder accepts the post-bootstrap
    `_harness_ctx` write from `api.run()` before the in-process
    `run_workflow` tool fires. The in-flight tool ctx is NOT held on `_state`
    (moved to a module-level ContextVar per spec v1.36 §14.18 chapeau
    per-session ctx isolation); see `test_current_tool_ctx_binding_lifecycle`
    below for the ContextVar-backed accessor lifecycle.
    """
    server = HarnessMCPServer(server=object())
    sentinel_harness_ctx = object()
    server._state["_harness_ctx"] = sentinel_harness_ctx
    assert server._state["_harness_ctx"] is sentinel_harness_ctx


@pytest.mark.asyncio
async def test_concurrent_set_current_tool_ctx_is_task_isolated() -> None:
    """Per spec v1.36 §14.18 chapeau: two concurrent `run_workflow`-shaped
    asyncio tasks each bind their own ctx via `set_current_tool_ctx`; each
    task's `get_current_tool_ctx` reads back its OWN binding (NOT the other
    task's). This is the production-grade variant of
    `test_contextvar_bridge_propagation.py` exercised against the actual
    `HarnessMCPServer` accessor API.

    Failure of this test indicates either (a) the accessor methods are
    not actually backed by a ContextVar, OR (b) asyncio task isolation
    broke. Both would route the wrong client's ctx into HITL elicitation.
    """
    server = HarnessMCPServer(server=object())

    async def _tool_handler_simulant(value: str) -> object | None:
        token = server.set_current_tool_ctx(value)  # type: ignore[arg-type]
        try:
            # Yield to the loop so the OTHER concurrent task gets a turn
            # to set its own value; if the accessor were not task-isolated,
            # this is where cross-talk would surface.
            await asyncio.sleep(0)
            return server.get_current_tool_ctx()
        finally:
            server.reset_current_tool_ctx(token)

    results = await asyncio.gather(
        _tool_handler_simulant("client-alpha"),
        _tool_handler_simulant("client-beta"),
    )
    assert results == ["client-alpha", "client-beta"], (
        f"concurrent set_current_tool_ctx cross-talked: {results!r}"
    )

    # Post-condition: both tokens reset → no binding leaks into the test task
    assert server.get_current_tool_ctx() is None


def test_current_tool_ctx_binding_lifecycle() -> None:
    """The `get_current_tool_ctx` / `set_current_tool_ctx` /
    `reset_current_tool_ctx` accessors round-trip a ctx value via a
    module-level ContextVar per spec v1.36 §14.18 chapeau per-session ctx
    isolation. Replaces the prior `_state['_current_tool_ctx']` dict-key
    pattern that would have raced across concurrent daemon-mode clients.
    """
    server = HarnessMCPServer(server=object())
    assert server.get_current_tool_ctx() is None

    sentinel_ctx = object()
    token = server.set_current_tool_ctx(sentinel_ctx)  # type: ignore[arg-type]
    try:
        assert server.get_current_tool_ctx() is sentinel_ctx
    finally:
        server.reset_current_tool_ctx(token)

    assert server.get_current_tool_ctx() is None


def test_harness_mcp_server_distinct_from_mcp_host() -> None:
    """AC #1 + AC #7 (Q4 ratification): `HarnessMCPServer` is a separate
    sibling primitive — NOT a refinement of `MCPHost`. The two MCP roles
    (H_T-as-client per U-RT-15; H_T-as-server per U-RT-62) coexist.
    """
    server = HarnessMCPServer(server=object())
    host = MCPHost(started=False)
    assert type(server) is not type(host)
    assert not isinstance(server, MCPHost)
    assert not isinstance(host, HarnessMCPServer)


def test_materialize_mcp_server_stage_returns_started_server() -> None:
    """AC #2: `materialize_mcp_server_stage()` constructs FastMCP server +
    registers `run_workflow` tool; returns `HarnessMCPServer(started=True)`.
    """
    from harness_runtime.lifecycle.mcp_server import materialize_mcp_server_stage

    server = materialize_mcp_server_stage(drain_timeout_seconds=30.0)
    assert server.started is True
    assert server.server is not None
    # FastMCP instance check (loose — server.server is typed `Any`; we
    # verify the duck-typed attribute used at AC #3 — `.tool()` decorator).
    assert hasattr(server.server, "tool")
    # Mutable holders empty at materialization time.
    assert server.workflow_registry == {}
    assert server._state == {}


def test_materialize_mcp_server_stage_allows_uds_host_header() -> None:
    """Daemon UDS transport arrives at FastMCP with Host `0.0.0.0`; keep DNS
    rebinding protection enabled while allowing that local socket request."""
    from harness_runtime.lifecycle.mcp_server import materialize_mcp_server_stage

    server = materialize_mcp_server_stage(drain_timeout_seconds=30.0)
    security = server.server.settings.transport_security

    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1" in security.allowed_hosts
    assert "0.0.0.0" in security.allowed_hosts
    assert "0.0.0.0:*" in security.allowed_hosts
    assert "127.0.0.1:*" in security.allowed_hosts
    assert "localhost" in security.allowed_hosts
    assert "localhost:*" in security.allowed_hosts


def test_materialize_mcp_server_stage_registers_run_workflow_tool() -> None:
    """AC #3: the `run_workflow` MCP tool is discoverable on the FastMCP
    server after `materialize_mcp_server_stage()` completes.
    """
    import asyncio

    from harness_runtime.lifecycle.mcp_server import materialize_mcp_server_stage

    server = materialize_mcp_server_stage(drain_timeout_seconds=30.0)

    # FastMCP exposes `list_tools()` as an async method on the underlying
    # server. We verify discoverability via the tool manager surface.
    async def _list() -> list[str]:
        tools = await server.server.list_tools()
        return [tool.name for tool in tools]

    tool_names = asyncio.run(_list())
    assert "run_workflow" in tool_names


def test_run_workflow_tool_rejects_unknown_workflow_id() -> None:
    """AC #3 failure semantics: tool body raises `RuntimeError` when
    `workflow_id` is not in the registry. `api.run()` per AC #5 pre-
    registers before invoking; an unknown id indicates a usage defect.
    """
    import asyncio

    from harness_runtime.lifecycle.mcp_server import materialize_mcp_server_stage

    server = materialize_mcp_server_stage(drain_timeout_seconds=30.0)

    async def _call() -> None:
        # Direct invocation through the FastMCP tool manager (bypassing
        # the in-memory transport, for unit-test economy). Production
        # invocation is exercised at AC #6 e2e integration test.
        tool = server.server._tool_manager.get_tool("run_workflow")  # type: ignore[attr-defined]
        assert tool is not None
        # We can't easily synth a real Context here; verify the registry
        # gate fires first by writing _harness_ctx but NOT the workflow.
        server._state["_harness_ctx"] = object()
        # The fn closure raises on lookup before ever touching ctx.
        await tool.fn(workflow_id="missing", ctx=object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="not registered"):
        asyncio.run(_call())


def test_run_workflow_tool_rejects_unbound_harness_ctx() -> None:
    """AC #3 failure semantics: tool body raises `RuntimeError` when
    `_state['_harness_ctx']` is not bound. Indicates the tool was called
    outside the `api.run()` flow per AC #5 (the only sanctioned caller).
    """
    import asyncio

    from harness_runtime.lifecycle.mcp_server import materialize_mcp_server_stage

    server = materialize_mcp_server_stage(drain_timeout_seconds=30.0)

    async def _call() -> None:
        tool = server.server._tool_manager.get_tool("run_workflow")  # type: ignore[attr-defined]
        assert tool is not None
        # No `_harness_ctx` bound on `_state`; the tool body MUST raise
        # before reaching the workflow_registry lookup.
        await tool.fn(workflow_id="any", ctx=object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="bound the post-bootstrap"):
        asyncio.run(_call())


def test_harness_context_field_admits_both_mcp_roles() -> None:
    """AC #1: `HarnessContext.mcp_server` field admits `HarnessMCPServer`
    sibling to existing `mcp_host: MCPHost`. The `mcp_server` field is
    Optional (`None` default) for transitional bootstrap-builder shapes;
    post-U-RT-62 bootstrap completion writes a populated instance.
    """
    from harness_runtime.types import HarnessContext

    fields = HarnessContext.model_fields
    assert "mcp_host" in fields
    assert "mcp_server" in fields
    # mcp_server is Optional (default None) per AC #1 transitional shape.
    mcp_server_field = fields["mcp_server"]
    assert mcp_server_field.default is None
