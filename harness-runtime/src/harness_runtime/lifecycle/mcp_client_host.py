"""U-RT-63 — `MCPClientHost` class skeleton + transport selector.
U-RT-64 — `MCPClientHost.start()` STDIO subprocess lifecycle + list_tools.

Per `Spec_Harness_Runtime_v1.md` v1.13 §14.9.1 architectural surfaces +
§14.9.5 `RT-FAIL-MCP-HOST-STARTUP`. Per `Implementation_Plan_Harness_Runtime_v2_11.md`
§1 U-RT-63 + U-RT-64.

HTTP (U-RT-65) + SSE (U-RT-66) branches extend `_open_connection()`.

Distinct from `lifecycle/mcp_host.py` (U-RT-15 `MCPHost` — server-hosting
placeholder for the H_T-as-MCP-server topology per `lifecycle/mcp_server.py`
U-RT-62). `MCPClientHost` is the H_T-as-MCP-client surface: it owns the
client-side lifecycle for connecting *out* to MCP servers (filesystem /
GitHub / sandbox / etc.) that publish `ToolContract`s consumed by the
runtime tool dispatcher (U-RT-67 `RuntimeToolDispatcher`).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from harness_as.tool_contract import ToolContract
from harness_cp.cp_shared_types import MCPTrustTier

from harness_runtime.lifecycle.tool_registry import ToolRegistry

__all__ = [
    "MCPClientHost",
    "MCPHostAlreadyStartedError",
    "MCPHostHealth",
    "MCPHostNotStartedError",
    "MCPHostStartupError",
    "MCPToolContractConverter",
    "MCPTransport",
]


MCPToolContractConverter = Callable[[Any], ToolContract]
"""Operator-supplied converter from MCP `mcp.types.Tool` → AS `ToolContract`.

Injected at `MCPClientHost.__init__`. The MCP advertised-tool surface
(`name`, `description`, `inputSchema`, `outputSchema`) does NOT carry the
AS-side sandbox/blast-radius policy fields required by `ToolContract`
(`minimum_tier`, `blast_radius_tier`). The operator is responsible for
supplying a converter that knows the per-server sandbox policy.

Default converter (`_default_tool_contract_converter`) raises on every
invocation — mirrors the U-CP-68 `_default_tier_resolver` + U-CP-69
`_default_server_info_lookup` loud-on-misconfiguration discipline.
"""


MCPTransport = Literal["stdio", "streamable_http", "sse"]
"""Per spec §14.9.1 transport-neutral terminology block + §14.9.6 inv 5.

All 3 transports in scope at v1 per Decision 1.D4 RATIFIED (2026-05-21).
"""


class MCPHostNotStartedError(RuntimeError):
    """Raised when `tool_registry` is accessed before `start()` completes.

    Per spec §14.9.1: `tool_registry` is "immutable after start()" — i.e.,
    only populated once `start()` has invoked `list_tools` on the connected
    MCP server. Pre-start access is a contract violation; this typed error
    surfaces it deterministically rather than returning an empty registry
    that callers may silently consume.
    """


class MCPHostAlreadyStartedError(RuntimeError):
    """Raised when `start()` is invoked twice on the same `MCPClientHost`.

    Per spec §14.9.6 inv 1: "MCP host instance started exactly once per
    bootstrap. Stage 3a starts; stage 7 SHUTDOWN drains. ... Idempotent
    restart out of scope at v1 (deferred to operator-driven restart arc)."
    """


class MCPHostStartupError(RuntimeError):
    """`RT-FAIL-MCP-HOST-STARTUP` typed carrier.

    Per spec §14.9.5: raised when `MCPClientHost.start()` fails at any of
    (transport-specific connection open / protocol handshake / list_tools
    population / converter rejection). Wraps the originating exception via
    `__cause__` so operator-facing attribution preserves the underlying
    error (subprocess stderr / HTTP error / SSE stream error / schema
    rejection).

    Per spec §14.9.3 "stage 3a startup failure raises this →
    bootstrap aborts (fail-closed per ADR-F4 v1.1 §Consequences (c))".
    """


def _default_tool_contract_converter(_tool: Any) -> ToolContract:
    """Default `MCPToolContractConverter` — raises on every invocation.

    Mirrors the U-CP-68 `_default_tier_resolver` + U-CP-69
    `_default_server_info_lookup` loud-on-misconfiguration discipline. The
    operator MUST supply a converter at `MCPClientHost.__init__` that knows
    the per-server sandbox-tier policy; production materialization with the
    default converter is a misconfiguration.
    """
    raise LookupError(
        "default MCPToolContractConverter invoked — operator must supply a "
        "tool_contract_converter at MCPClientHost.__init__ that maps the "
        "advertised MCP Tool surface (name / description / inputSchema / "
        "outputSchema) to an AS ToolContract carrying minimum_tier + "
        "blast_radius_tier per the per-server sandbox policy"
    )


@dataclass(frozen=True)
class MCPHostHealth:
    """Liveness probe carrier per spec §14.9.1.

    6-field frozen dataclass. Returned by `MCPClientHost.health_check()` on
    a per-dispatch cadence (§14.9.2 invariant 3 — health check pre-call).

    `transport` literal mirrors `MCPTransport`; spec §14.9.6 invariant 5
    pins all 3 values in scope at v1 (the `mcp.transport` span attribute
    populates from this field).

    `trust_tier` is the `MCPTrustTier` from CP plan v2.8 U-CP-00c carrier —
    cross-axis read (CP→runtime via `harness_cp.cp_shared_types`). Used by
    the dispatcher (U-RT-67) to populate the `mcp.server.trust_tier` span
    attribute — **telemetry-only** (U-RT-129 / CP §27.8). The per-server-trust
    gate keys on `server_name` via the per-server `TrustPolicy` evaluator, NOT
    on this field.
    """

    alive: bool
    last_ping_ms: int
    protocol_version: str
    transport: MCPTransport
    server_name: str
    trust_tier: MCPTrustTier


class MCPClientHost:
    """Per-server MCP-client lifecycle host (H_T-as-MCP-client surface).

    Owns subprocess (STDIO) / HTTP-client-pool (streamable_http) /
    event-stream-consumer (SSE) lifecycle for a single connected MCP
    server. Materialized at bootstrap stage 3a per spec §14.9.3.

    Per spec §14.9.6 inv 1: one instance per MCP server; one transport per
    instance (transport is selected at `__init__` from per-server bootstrap
    config). For deployments with N MCP servers, the operator materializes
    N `MCPClientHost` instances at stage 3a (each transport-typed).

    At U-RT-63 (this unit) only the skeleton + transport-validation
    preconditions land. `start()` / `health_check()` / `shutdown()` /
    `call_tool()` raise `NotImplementedError` until the per-transport
    units (U-RT-64 / U-RT-65 / U-RT-66) extend the skeleton.
    """

    _VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "streamable_http", "sse"})

    def __init__(
        self,
        *,
        transport: MCPTransport,
        server_name: str,
        trust_tier: MCPTrustTier,
        transport_config: Mapping[str, Any],
        tool_contract_converter: MCPToolContractConverter | None = None,
        auth_present: bool = False,
        connection_factory: (Callable[[], Any] | None) = None,
        session_context_factory: (Callable[[], Any] | None) = None,
    ) -> None:
        """Construct an unstarted `MCPClientHost`.

        Parameters
        ----------
        transport:
            One of `"stdio"`, `"streamable_http"`, `"sse"`. Other values raise
            `ValueError` per AC #1.
        server_name:
            Per-deployment registry ID — populates `MCPHostHealth.server_name`
            + `mcp.server.name` span attribute per C-AS-14 §14.3.
        trust_tier:
            The per-server trust tier (cross-axis from CP plan v2.8 U-CP-00c).
            Populates `MCPHostHealth.trust_tier` + `mcp.server.trust_tier`
            span attribute.
        transport_config:
            Transport-specific bootstrap config (subprocess argv for STDIO;
            URL + auth headers for HTTP; URL for SSE). Schema is
            transport-specific; per-transport units validate.
        tool_contract_converter:
            Operator-supplied `MCPToolContractConverter`. Default raises
            on production misconfig per loud-on-misconfig discipline.
        auth_present:
            Whether the transport carries auth credentials. Populates
            `mcp.auth_present` span attribute at the dispatcher (U-RT-67).
        connection_factory:
            Test-injection seam for the per-transport stream-pair context
            manager factory. Default `None` uses the SDK-supplied transport
            client (`stdio_client` / `streamablehttp_client` / `sse_client`).
            Production callers leave this `None`; integration tests supply
            an in-memory pair via `mcp.shared.memory.create_client_streams`
            or similar.
        session_context_factory:
            Higher-level test-injection seam — returns an async context
            manager that yields a `ClientSession` directly (post-handshake).
            Bypasses both stream construction and `ClientSession.__init__`.
            Designed for tests using `mcp.shared.memory.\
create_connected_server_and_client_session`. Production callers leave
            this `None`; if both `connection_factory` and
            `session_context_factory` are set, `session_context_factory`
            takes precedence.
        """
        if transport not in self._VALID_TRANSPORTS:
            raise ValueError(
                f"unknown MCP transport {transport!r}; expected one of "
                f"{sorted(self._VALID_TRANSPORTS)}"
            )
        self._transport: MCPTransport = transport
        self._server_name: str = server_name
        self._trust_tier: MCPTrustTier = trust_tier
        self._transport_config: Mapping[str, Any] = transport_config
        self._tool_contract_converter: MCPToolContractConverter = (
            tool_contract_converter or _default_tool_contract_converter
        )
        self._auth_present: bool = auth_present
        self._connection_factory: Callable[[], Any] | None = connection_factory
        self._session_context_factory: Callable[[], Any] | None = session_context_factory
        self._started: bool = False
        self._tool_registry: ToolRegistry | None = None
        self._session: Any = None
        self._exit_stack: AsyncExitStack | None = None
        self._protocol_version: str = ""

    @property
    def transport(self) -> MCPTransport:
        return self._transport

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def trust_tier(self) -> MCPTrustTier:
        return self._trust_tier

    @property
    def protocol_version(self) -> str:
        """MCP protocol version negotiated at `start()` (empty string pre-start).

        Exposed for the stage-5 emitter `info_lookup` wiring (spec v1.41 §14.9.8
        arc, Gap E) — the sync `MCPServerInfoLookup` reads it without the async
        `health_check()`."""
        return self._protocol_version

    @property
    def auth_present(self) -> bool:
        """Whether the transport carries auth credentials (emitter `info_lookup`)."""
        return self._auth_present

    @property
    def started(self) -> bool:
        return self._started

    @property
    def tool_registry(self) -> ToolRegistry:
        """Return the populated tool registry; raise pre-start per AC #3.

        Per spec §14.9.1: "immutable after start()". `list_tools` populates
        the registry as part of `start()`; pre-start access surfaces an
        `MCPHostNotStartedError`.
        """
        if not self._started or self._tool_registry is None:
            raise MCPHostNotStartedError(
                f"MCPClientHost(server_name={self._server_name!r}) — "
                "tool_registry accessed before start() completed; per spec "
                "§14.9.1 the registry is immutable after start() and is not "
                "available pre-start"
            )
        return self._tool_registry

    async def start(self) -> None:
        """Per-transport startup — connection open + protocol handshake +
        `list_tools` registry population.

        Per spec §14.9.1 + §14.9.5 + §14.9.6 inv 1. Failure raises
        `MCPHostStartupError` (`RT-FAIL-MCP-HOST-STARTUP`) with the
        originating exception preserved via `__cause__`. The bootstrap
        orchestrator catches at stage 3a → fail-closed abort per ADR-F4
        v1.1 §Consequences (c).
        """
        if self._started:
            raise MCPHostAlreadyStartedError(
                f"MCPClientHost(server_name={self._server_name!r}) — start() "
                "invoked twice; per spec §14.9.6 inv 1 the host is started "
                "exactly once per bootstrap (idempotent restart out of scope "
                "at v1)"
            )
        stack = AsyncExitStack()
        try:
            await stack.__aenter__()
            if self._session_context_factory is not None:
                # Higher-level test injection — session is pre-built +
                # already initialized. Skip stream + ClientSession setup.
                session = await stack.enter_async_context(self._session_context_factory())
                # `create_connected_server_and_client_session` does NOT call
                # `initialize()` itself; ClientSession is constructed but the
                # protocol handshake is left to the consumer. Mirror the
                # production path: initialize the session here.
                init_result = await session.initialize()
                protocol_version = getattr(init_result, "protocolVersion", "2025-06-18")
            else:
                # Production path — open transport-specific stream pair +
                # wrap in ClientSession + perform protocol handshake.
                connection_cm = self._build_connection_context()
                connection = await stack.enter_async_context(connection_cm)
                read_stream, write_stream = self._extract_streams(connection)
                from mcp.client.session import ClientSession

                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                init_result = await session.initialize()
                protocol_version = getattr(init_result, "protocolVersion", "2025-06-18")

            # list_tools → ToolRegistry population.
            list_result = await session.list_tools()
            registry = ToolRegistry()
            for tool in list_result.tools:
                contract = self._tool_contract_converter(tool)
                registry.register(contract)

            # All steps succeeded — commit state.
            self._exit_stack = stack
            self._session = session
            self._tool_registry = registry
            self._protocol_version = protocol_version
            self._started = True
        except MCPHostAlreadyStartedError:
            # Defensive — already raised pre-stack-open above.
            await stack.aclose()
            raise
        except BaseException as exc:
            # Unwind stack to release any partially-acquired resources.
            await stack.aclose()
            raise MCPHostStartupError(
                f"RT-FAIL-MCP-HOST-STARTUP: server={self._server_name!r} "
                f"transport={self._transport!r} — {type(exc).__name__}: {exc}"
            ) from exc

    async def health_check(self) -> MCPHostHealth:
        """Per-spec §14.9.2 inv 3 — liveness probe invoked pre-dispatch.

        STDIO: `session.send_ping()` per MCP SDK liveness primitive.
        HTTP / SSE: same `send_ping` over the session (the transport-layer
        liveness is wrapped by the session abstraction at the SDK).
        """
        if not self._started or self._session is None:
            raise MCPHostNotStartedError(
                f"MCPClientHost(server_name={self._server_name!r}) — "
                "health_check() invoked before start() completed"
            )
        start_ns = time.perf_counter_ns()
        alive: bool
        try:
            await self._session.send_ping()
            alive = True
        except Exception:
            alive = False
        end_ns = time.perf_counter_ns()
        return MCPHostHealth(
            alive=alive,
            last_ping_ms=(end_ns - start_ns) // 1_000_000,
            protocol_version=self._protocol_version,
            transport=self._transport,
            server_name=self._server_name,
            trust_tier=self._trust_tier,
        )

    async def shutdown(self) -> None:
        """Per-transport graceful close.

        STDIO: subprocess termination via stdio_client exit. HTTP: connection
        pool close. SSE: event stream close. All routed through the
        AsyncExitStack established at start().
        """
        if self._exit_stack is None:
            return  # never started — no-op
        await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None
        self._started = False

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Invoke a tool via the MCP session.

        Per spec §14.9.1: `call_tool` is the per-dispatch entry point. The
        `idempotency_key` is consumed by the dispatcher (U-RT-67) for span
        attribution; MCP protocol itself does not propagate the key, so we
        pass it through `arguments` under the `_idempotency_key` convention
        for the server-side handler to honor if supported. This preserves
        deterministic re-dispatch under retry without breaking servers that
        ignore the key.
        """
        if not self._started or self._session is None:
            raise MCPHostNotStartedError(
                f"MCPClientHost(server_name={self._server_name!r}) — "
                "call_tool() invoked before start() completed"
            )
        merged_args = dict(args)
        merged_args.setdefault("_idempotency_key", idempotency_key)
        result = await self._session.call_tool(name, merged_args)
        # MCP `CallToolResult` has `.content` (list of content blocks) +
        # `.isError`. Surface as Mapping for the dispatcher; the dispatcher
        # (U-RT-67) validates against `ToolContract.output_schema`.
        return {
            "content": [
                block.model_dump() if hasattr(block, "model_dump") else block
                for block in result.content
            ],
            "isError": bool(getattr(result, "isError", False)),
            "structuredContent": getattr(result, "structuredContent", None),
        }

    # --- Per-transport connection-context construction ----------------------

    def _build_connection_context(self) -> Any:
        """Construct the transport-specific async context manager that yields
        a (read_stream, write_stream) pair. Test-injection seam.
        """
        if self._connection_factory is not None:
            return self._connection_factory()
        if self._transport == "stdio":
            return self._stdio_connection_context()
        if self._transport == "streamable_http":
            return self._http_connection_context()
        if self._transport == "sse":
            return self._sse_connection_context()
        raise AssertionError(
            f"unreachable — transport {self._transport!r} passed __init__ "
            "validation but not handled in _build_connection_context"
        )

    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def _stdio_connection_context(self) -> AsyncIterator[Any]:
        """STDIO transport connection context per U-RT-64.

        Uses `mcp.client.stdio.stdio_client` to spawn the configured
        subprocess + yield a stream pair. Subprocess termination is handled
        by the SDK on context exit.

        `transport_config` keys consumed:
        - `command`: subprocess argv[0] (REQUIRED)
        - `args`: list[str] of additional argv (default `[]`)
        - `env`: dict[str, str] | None (default `None` — inherits)
        - `cwd`: str | None (default `None`)
        """
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command = self._transport_config.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(f"STDIO transport_config requires str 'command' (got {command!r})")
        params = StdioServerParameters(
            command=command,
            args=list(self._transport_config.get("args") or []),
            env=self._transport_config.get("env"),
            cwd=self._transport_config.get("cwd"),
        )
        async with stdio_client(params) as connection:
            yield connection

    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def _http_connection_context(self) -> AsyncIterator[Any]:
        """HTTP transport per U-RT-65 — streamable_http via httpx.

        Per spec §14.9.1 HTTP branch + Decision 1.D4 scope expansion +
        §14.9.6 inv 5. Uses `mcp.client.streamable_http.streamablehttp_client`
        which opens an httpx client connection pool, performs the
        streamable-HTTP handshake, and yields `(read, write, get_session_id)`.
        `_extract_streams` consumes the first two; the session-id callback
        is reserved for future session-resumption arcs.

        `transport_config` keys consumed:
        - `url`: server URL (REQUIRED str)
        - `headers`: dict[str, str] of auth + custom headers (OPTIONAL)
        - `timeout`: connection timeout seconds (default 30)
        - `sse_read_timeout`: server-sent-event read timeout (default 300)
        """
        from mcp.client.streamable_http import streamable_http_client

        url = self._transport_config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"streamable_http transport_config requires str 'url' (got {url!r})")
        kwargs: dict[str, Any] = {}
        if "headers" in self._transport_config:
            kwargs["headers"] = self._transport_config["headers"]
        if "timeout" in self._transport_config:
            kwargs["timeout"] = self._transport_config["timeout"]
        if "sse_read_timeout" in self._transport_config:
            kwargs["sse_read_timeout"] = self._transport_config["sse_read_timeout"]
        async with streamable_http_client(url, **kwargs) as connection:
            yield connection

    @asynccontextmanager  # pyright: ignore[reportDeprecated]
    async def _sse_connection_context(self) -> AsyncIterator[Any]:
        """SSE transport per U-RT-66 — server-sent events via httpx.

        Per spec §14.9.1 SSE branch + Decision 1.D4 scope expansion. Uses
        `mcp.client.sse.sse_client` which opens an SSE event stream over
        httpx and yields `(read, write)` streams plus an internal session
        for the SSE protocol.

        `transport_config` keys consumed:
        - `url`: SSE endpoint URL (REQUIRED str)
        - `headers`: dict[str, Any] (OPTIONAL)
        - `timeout`: connection timeout seconds (default 5)
        - `sse_read_timeout`: event-stream read timeout (default 300)
        """
        from mcp.client.sse import sse_client

        url = self._transport_config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"sse transport_config requires str 'url' (got {url!r})")
        kwargs: dict[str, Any] = {}
        if "headers" in self._transport_config:
            kwargs["headers"] = self._transport_config["headers"]
        if "timeout" in self._transport_config:
            kwargs["timeout"] = self._transport_config["timeout"]
        if "sse_read_timeout" in self._transport_config:
            kwargs["sse_read_timeout"] = self._transport_config["sse_read_timeout"]
        async with sse_client(url, **kwargs) as connection:
            yield connection

    @staticmethod
    def _extract_streams(connection: Any) -> tuple[Any, Any]:
        """Normalize the connection-context yield into a (read, write) pair.

        `stdio_client` / `streamablehttp_client` / `sse_client` all yield
        tuples whose first two elements are the read + write streams; later
        elements (e.g., HTTP transport carries a per-session-id callback at
        index 2) are not consumed by the runtime.
        """
        if not isinstance(connection, tuple) or len(connection) < 2:  # pyright: ignore[reportUnknownArgumentType]
            raise TypeError(
                f"MCPClientHost connection-context yielded non-tuple or "
                f"under-length value: {type(connection).__name__}"  # pyright: ignore[reportUnknownArgumentType]
            )
        return connection[0], connection[1]  # pyright: ignore[reportUnknownVariableType]
