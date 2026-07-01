"""U-RT-63 — `MCPClientHost` class skeleton + transport selector.
U-RT-64 — `MCPClientHost.start()` STDIO subprocess lifecycle + list_tools.

Per `Implementation_Plan_Harness_Runtime_v2_11.md` §1 U-RT-63 + U-RT-64 ACs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.tool_contract import ToolContract
from harness_cp.cp_shared_types import MCPTrustTier
from harness_runtime.lifecycle.mcp_client_host import (
    MCPClientHost,
    MCPHostAlreadyStartedError,
    MCPHostHealth,
    MCPHostNotStartedError,
    MCPHostStartupError,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

# ---------- AC #1 — transport literal validation ----------------------------


@pytest.mark.parametrize("transport", ["stdio", "streamable_http", "sse"])
def test_init_accepts_each_valid_transport(transport: str) -> None:
    host = MCPClientHost(
        transport=transport,  # type: ignore[arg-type]
        server_name="srv-1",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},
    )
    assert host.transport == transport
    assert host.server_name == "srv-1"
    assert host.trust_tier is MCPTrustTier.LEVEL_2_SANDBOX_ALL
    assert host.started is False


def test_init_rejects_unknown_transport() -> None:
    with pytest.raises(ValueError, match="unknown MCP transport 'websocket'"):
        MCPClientHost(
            transport="websocket",  # type: ignore[arg-type]
            server_name="srv-1",
            trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            transport_config={},
        )


def test_init_rejects_empty_string_transport() -> None:
    with pytest.raises(ValueError, match="unknown MCP transport ''"):
        MCPClientHost(
            transport="",  # type: ignore[arg-type]
            server_name="srv-1",
            trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            transport_config={},
        )


# ---------- AC #2 — MCPHostHealth dataclass shape ---------------------------


def test_mcp_host_health_instantiates_with_six_fields() -> None:
    health = MCPHostHealth(
        alive=True,
        last_ping_ms=42,
        protocol_version="2025-06-18",
        transport="stdio",
        server_name="srv-1",
        trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    )
    assert health.alive is True
    assert health.last_ping_ms == 42
    assert health.protocol_version == "2025-06-18"
    assert health.transport == "stdio"
    assert health.server_name == "srv-1"
    assert health.trust_tier is MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT


def test_mcp_host_health_is_frozen() -> None:
    health = MCPHostHealth(
        alive=False,
        last_ping_ms=0,
        protocol_version="2025-06-18",
        transport="sse",
        server_name="srv-2",
        trust_tier=MCPTrustTier.LEVEL_1_SIGNED_PINNED,
    )
    with pytest.raises(Exception):
        health.alive = True  # type: ignore[misc]


# ---------- AC #3 — tool_registry raises pre-start --------------------------


def test_tool_registry_raises_pre_start() -> None:
    host = MCPClientHost(
        transport="stdio",
        server_name="srv-1",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},
    )
    with pytest.raises(MCPHostNotStartedError, match="srv-1"):
        _ = host.tool_registry


# ---------- AC #4 — importability (covered by module-level imports) ---------


def test_module_exports_public_surface() -> None:
    from harness_runtime.lifecycle import mcp_client_host as mod

    assert "MCPClientHost" in mod.__all__
    assert "MCPHostHealth" in mod.__all__
    assert "MCPHostNotStartedError" in mod.__all__
    assert "MCPHostAlreadyStartedError" in mod.__all__
    assert "MCPTransport" in mod.__all__


# ---------- typed errors ----------------------------------------------------


def test_typed_errors_are_runtime_error_subclasses() -> None:
    assert issubclass(MCPHostAlreadyStartedError, RuntimeError)
    assert issubclass(MCPHostNotStartedError, RuntimeError)
    assert issubclass(MCPHostStartupError, RuntimeError)


@pytest.mark.asyncio
async def test_health_check_pre_start_raises_not_started() -> None:
    host = MCPClientHost(
        transport="streamable_http",
        server_name="srv-1",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},
    )
    with pytest.raises(MCPHostNotStartedError):
        await host.health_check()


@pytest.mark.asyncio
async def test_call_tool_pre_start_raises_not_started() -> None:
    host = MCPClientHost(
        transport="sse",
        server_name="srv-1",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},
    )
    with pytest.raises(MCPHostNotStartedError):
        await host.call_tool("some_tool", {}, "idempotency-1")


@pytest.mark.asyncio
async def test_shutdown_before_start_is_noop() -> None:
    host = MCPClientHost(
        transport="stdio",
        server_name="srv-1",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},
    )
    # Should not raise — graceful no-op per impl.
    await host.shutdown()
    assert host.started is False


# ---------- U-RT-64 — end-to-end start() via in-memory session --------------


def _build_test_fastmcp_server() -> FastMCP:
    """Construct an in-memory FastMCP server with a known tool registered."""
    server = FastMCP(name="test-mcp-srv")

    @server.tool(description="A test echo tool — returns the input verbatim.")
    def echo(message: str) -> str:
        return f"echoed: {message}"

    @server.tool(description="A test add tool — returns x + y.")
    def add(x: int, y: int) -> int:
        return x + y

    return server


def _make_tool_contract_converter():
    """Build a converter that maps MCP tools → AS ToolContract with a
    conservative default (TIER_2 + READ_ONLY) for testing."""

    def convert(tool: object) -> ToolContract:
        name = tool.name
        description = getattr(tool, "description", "")
        input_schema = getattr(tool, "inputSchema", None) or {"type": "object"}
        output_schema = getattr(tool, "outputSchema", None) or {"type": "object"}
        return ToolContract(
            name=name,
            description=description or "",
            input_schema=input_schema,
            output_schema=output_schema,
            minimum_tier=SandboxTier.TIER_2_CONTAINER,
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
        )

    return convert


def _make_session_factory(server: FastMCP):
    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            yield session

    return factory


@pytest.mark.asyncio
async def test_start_populates_registry_and_completes_handshake() -> None:
    """AC #1 + #2 (STDIO branch): start() spawns + handshake + list_tools."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused-bypassed-by-session-factory"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
    )
    try:
        await host.start()
        assert host.started is True
        registry = host.tool_registry
        assert len(registry) == 2
        assert "echo" in {str(n) for n in registry.names()}
        assert "add" in {str(n) for n in registry.names()}
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_double_start_raises_already_started() -> None:
    """AC #4: idempotent re-start() raises MCPHostAlreadyStartedError."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
    )
    try:
        await host.start()
        with pytest.raises(MCPHostAlreadyStartedError):
            await host.start()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_start_failure_wraps_in_startup_error() -> None:
    """AC #3: start() failure raises RT-FAIL-MCP-HOST-STARTUP."""

    @asynccontextmanager
    async def failing_factory():
        raise RuntimeError("simulated transport failure")
        yield  # pragma: no cover

    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=failing_factory,
    )
    with pytest.raises(MCPHostStartupError) as exc_info:
        await host.start()
    assert "RT-FAIL-MCP-HOST-STARTUP" in str(exc_info.value)
    assert "simulated transport failure" in str(exc_info.value)
    assert host.started is False


@pytest.mark.asyncio
async def test_default_converter_raises_on_production_misconfig() -> None:
    """The default converter raises (loud-on-misconfig discipline)."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        # NOT supplying tool_contract_converter — should hit default.
        session_context_factory=_make_session_factory(server),
    )
    with pytest.raises(MCPHostStartupError) as exc_info:
        await host.start()
    # The startup error wraps a LookupError from the default converter.
    assert isinstance(exc_info.value.__cause__, LookupError)


@pytest.mark.asyncio
async def test_health_check_post_start_returns_alive() -> None:
    """AC #2 (parity for HTTP/SSE units): health_check returns liveness."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
    )
    try:
        await host.start()
        health = await host.health_check()
        assert health.alive is True
        assert health.transport == "stdio"
        assert health.server_name == "test-mcp-srv"
        assert health.trust_tier is MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT
        assert health.last_ping_ms >= 0
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_call_tool_post_start_invokes_tool() -> None:
    """AC #5: integration test against in-memory mock — call_tool works."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
    )
    try:
        await host.start()
        result = await host.call_tool("echo", {"message": "hello"}, "idem-1")
        assert result["isError"] is False
        # The FastMCP echo returns "echoed: hello"; surfaced in content blocks.
        content_text = "".join(
            (b.get("text") if isinstance(b, dict) else "") or "" for b in result["content"]
        )
        assert "echoed: hello" in content_text
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_call_tool_arithmetic_via_in_memory_server() -> None:
    """Second call_tool exercise — verifies multi-tool dispatch + arg shapes."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="stdio",
        server_name="test-mcp-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
    )
    try:
        await host.start()
        result = await host.call_tool("add", {"x": 2, "y": 3}, "idem-2")
        assert result["isError"] is False
    finally:
        await host.shutdown()


# ---------- production-path STDIO transport_config validation ---------------


def test_stdio_transport_config_requires_command_str() -> None:
    """Direct unit on the STDIO connection-context constructor — validates
    that `transport_config` carries a non-empty str `command`."""
    host = MCPClientHost(
        transport="stdio",
        server_name="srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},  # missing 'command'
        tool_contract_converter=_make_tool_contract_converter(),
    )
    # Build the context manager and drive its first step to surface the
    # ValueError out of the context body.
    cm = host._stdio_connection_context()
    with pytest.raises(ValueError, match="requires str 'command'"):
        # __aenter__ is what would be invoked by AsyncExitStack; calling
        # it bypasses the need for an event loop here.
        import asyncio

        asyncio.run(cm.__aenter__())


# ---------- U-RT-65 — HTTP transport unit-level ----------------------------


def test_http_transport_config_requires_url_str() -> None:
    """HTTP connection-context constructor validates 'url' is non-empty str."""
    host = MCPClientHost(
        transport="streamable_http",
        server_name="srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},  # missing 'url'
        tool_contract_converter=_make_tool_contract_converter(),
    )
    cm = host._http_connection_context()
    with pytest.raises(ValueError, match="requires str 'url'"):
        import asyncio

        asyncio.run(cm.__aenter__())


def test_http_transport_config_rejects_non_str_url() -> None:
    host = MCPClientHost(
        transport="streamable_http",
        server_name="srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"url": 42},
        tool_contract_converter=_make_tool_contract_converter(),
    )
    cm = host._http_connection_context()
    with pytest.raises(ValueError, match="requires str 'url'"):
        import asyncio

        asyncio.run(cm.__aenter__())


@pytest.mark.asyncio
async def test_http_transport_dispatch_via_session_factory_injection() -> None:
    """HTTP transport — end-to-end start() + list_tools + call_tool +
    health_check + shutdown via session_context_factory injection (the
    HTTP connection path is exercised at integration time; the dispatch
    path is exercised here via the in-memory session test seam)."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="streamable_http",
        server_name="http-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"url": "http://mock-server.test"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
        auth_present=True,
    )
    try:
        await host.start()
        assert host.transport == "streamable_http"
        health = await host.health_check()
        assert health.transport == "streamable_http"
        assert health.alive is True
        result = await host.call_tool("echo", {"message": "via-http"}, "idem-http")
        assert result["isError"] is False
    finally:
        await host.shutdown()


# ---------- U-RT-66 — SSE transport unit-level ----------------------------


def test_sse_transport_config_requires_url_str() -> None:
    host = MCPClientHost(
        transport="sse",
        server_name="srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={},  # missing 'url'
        tool_contract_converter=_make_tool_contract_converter(),
    )
    cm = host._sse_connection_context()
    with pytest.raises(ValueError, match="requires str 'url'"):
        import asyncio

        asyncio.run(cm.__aenter__())


@pytest.mark.asyncio
async def test_sse_transport_dispatch_via_session_factory_injection() -> None:
    """SSE transport — end-to-end start() + list_tools + health_check via
    session_context_factory injection."""
    server = _build_test_fastmcp_server()
    host = MCPClientHost(
        transport="sse",
        server_name="sse-srv",
        trust_tier=MCPTrustTier.LEVEL_1_SIGNED_PINNED,
        transport_config={"url": "http://mock-sse.test/events"},
        tool_contract_converter=_make_tool_contract_converter(),
        session_context_factory=_make_session_factory(server),
    )
    try:
        await host.start()
        assert host.transport == "sse"
        health = await host.health_check()
        assert health.transport == "sse"
        assert health.alive is True
        assert health.trust_tier is MCPTrustTier.LEVEL_1_SIGNED_PINNED
    finally:
        await host.shutdown()
