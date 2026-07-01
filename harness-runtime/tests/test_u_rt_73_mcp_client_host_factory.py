"""U-RT-73 — Stage 3a factory `materialize_mcp_client_host_stage` tests.

ACs per Implementation_Plan_Harness_Runtime_v2_13.md §1B U-RT-73 (preserved
from v2.12). Spec contract: Spec_Harness_Runtime_v1.md v1.16 §14.9.3
stage-3a factory contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_core import ClientName
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.factories.mcp_client_host_factory import (
    materialize_mcp_client_host_stage,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.types import (
    CollectorConfig,
    MCPClientConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
    ServerName,
)


def _config(mcp_clients: list[MCPClientConfig] | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=mcp_clients or [],
    )


def _stdio_client(name: str = "test-stdio") -> MCPClientConfig:
    return MCPClientConfig(
        client_name=ClientName(name),
        transport=MCPTransport.STDIO,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="stdio:///bin/echo",
    )


@pytest.mark.asyncio
async def test_returns_mcp_client_host_when_mcp_clients_populated() -> None:
    # AC #1 (U-RT-73) + U-RT-125/126 carrier-shape AC — non-empty mcp_clients →
    # a 1-entry `dict[ServerName, MCPClientHost]` keyed on the host's server_name.
    cfg = _config([_stdio_client()])
    hosts = await materialize_mcp_client_host_stage(cfg)
    assert set(hosts) == {ServerName("test-stdio")}
    host = hosts[ServerName("test-stdio")]
    assert isinstance(host, MCPClientHost)
    assert host.server_name == "test-stdio"


@pytest.mark.asyncio
async def test_returns_empty_dict_when_mcp_clients_empty() -> None:
    # AC #2 / §14.9.10 D1 — empty mcp_clients → empty dict `{}` (no sentinel
    # host; a TOOL_STEP then raises RT-FAIL-TOOL-CONTRACT-UNKNOWN at dispatch).
    cfg = _config([])
    hosts = await materialize_mcp_client_host_stage(cfg)
    assert hosts == {}


@pytest.mark.asyncio
async def test_factory_returns_unstarted_host() -> None:
    """Factory returns unstarted host(s); the stage 3a body invokes `.start()`
    per host. Tested by reading the private `_started` flag through Python's
    introspection (no public predicate at U-RT-63 MVP)."""
    cfg = _config([_stdio_client()])
    hosts = await materialize_mcp_client_host_stage(cfg)
    host = hosts[ServerName("test-stdio")]
    assert getattr(host, "_started", True) is False


@pytest.mark.asyncio
async def test_stage_3a_body_binds_factory_return_to_ctx() -> None:
    """AC #3 — stage 3a body invokes factory exactly once and binds the
    return value to `ctx.mcp_client_hosts`. Tested via direct import of
    the stage shim and an isolated config."""
    from harness_core.workload_class import WorkloadClass
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
    from harness_runtime.bootstrap.stage_3a_cp_clients import execute as stage_3a_execute

    builder = _MutableHarnessContext()

    # Stub keyring_resolver — stage 0 normally populates this. We bypass by
    # patching the providers factory to avoid network calls; the
    # mcp_client_hosts path is what we're verifying.
    builder.keyring_resolver = object()

    cfg = _config([])

    # Monkey-patch `materialize_provider_clients_stage` to avoid touching
    # the real providers. Direct module-level swap.
    import harness_runtime.bootstrap.stage_3a_cp_clients as stage_3a_module

    class _StubStage:
        providers: dict[str, Any] = {}

    async def _stub_providers(*args: Any, **kwargs: Any) -> _StubStage:
        return _StubStage()

    original = stage_3a_module.materialize_provider_clients_stage
    stage_3a_module.materialize_provider_clients_stage = _stub_providers  # type: ignore[assignment]
    try:
        await stage_3a_execute(builder, cfg, WorkloadClass.SOFTWARE_ENGINEERING)
    finally:
        stage_3a_module.materialize_provider_clients_stage = original  # type: ignore[assignment]

    # Empty config → stage 3a binds an empty dict (§14.9.10 D1).
    assert builder.mcp_client_hosts == {}


@pytest.mark.asyncio
async def test_materializes_all_configured_hosts_keyed_by_server_name() -> None:
    """U-RT-126-full / §14.9.10 D1 — a multi-server config materializes ONE host
    per entry (retires the single-server `[0]`), keyed by `server_name`."""
    cfg = _config(
        [
            _stdio_client("first-server"),
            _stdio_client("second-server"),
        ]
    )
    hosts = await materialize_mcp_client_host_stage(cfg)
    assert set(hosts) == {ServerName("first-server"), ServerName("second-server")}
    assert hosts[ServerName("first-server")].server_name == "first-server"
    assert hosts[ServerName("second-server")].server_name == "second-server"


@pytest.mark.asyncio
async def test_duplicate_server_name_fails_loud() -> None:
    """§14.9.10 D1 — two entries sharing a client_name (→ same server_name) fail
    loud at materialize (detect-then-refuse; no silent host drop)."""
    cfg = _config([_stdio_client("dup"), _stdio_client("dup")])
    with pytest.raises(ValueError, match="duplicate MCP server_name"):
        await materialize_mcp_client_host_stage(cfg)


# ---------------------------------------------------------------------------
# spec v1.40 Reading B — stage-3a factory builds a default-policy converter
# per `.harness/class_1_fork_tool_step_no_operator_supplied_converter.md`.
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal `mcp.types.Tool` stand-in for converter unit tests."""

    def __init__(
        self,
        name: str,
        description: str | None,
        input_schema: dict[str, object] | None,
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema  # mirrors mcp.types.Tool field name


def _policy_client(
    *,
    name: str = "policy-server",
    minimum_tier: SandboxTier = SandboxTier.TIER_2_CONTAINER,
    blast_radius: BlastRadiusTier = BlastRadiusTier.READ_ONLY,
) -> MCPClientConfig:
    return MCPClientConfig(
        client_name=ClientName(name),
        transport=MCPTransport.STDIO,
        trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
        blast_radius=BlastRadiusTier.READ_ONLY,
        connection_url="stdio:///bin/echo",
        default_minimum_tier=minimum_tier,
        default_blast_radius=blast_radius,
    )


@pytest.mark.asyncio
async def test_factory_wires_non_default_converter() -> None:
    """v1.40 — a configured server's host carries a real converter, NOT the
    raise-on-every-call default stub."""
    cfg = _config([_policy_client()])
    host = next(iter((await materialize_mcp_client_host_stage(cfg)).values()))
    converter = host._tool_contract_converter  # type: ignore[attr-defined]
    contract = converter(_FakeTool("echo", "echo a string", {"type": "object"}))
    assert contract.name == "echo"


@pytest.mark.asyncio
async def test_converter_stamps_per_server_default_policy() -> None:
    """v1.40 — converter stamps the entry's default tier + blast radius onto
    every discovered tool's `ToolContract`."""
    cfg = _config(
        [
            _policy_client(
                minimum_tier=SandboxTier.TIER_1_PROCESS,
                blast_radius=BlastRadiusTier.LOCAL_MUTATION,
            )
        ]
    )
    host = next(iter((await materialize_mcp_client_host_stage(cfg)).values()))
    converter = host._tool_contract_converter  # type: ignore[attr-defined]
    contract = converter(_FakeTool("write_file", "writes a file", {"type": "object"}))
    assert contract.minimum_tier is SandboxTier.TIER_1_PROCESS
    assert contract.blast_radius_tier is BlastRadiusTier.LOCAL_MUTATION
    assert contract.description == "writes a file"


@pytest.mark.asyncio
async def test_converter_stamps_per_server_forcing_discriminators() -> None:
    """B6 Slice 2 (runtime spec v1.56 §14.9.11) — the stage-3a converter stamps the
    entry's per-server `ToolMetadata` forcing discriminators onto every MCP-discovered
    tool's `ToolContract`. Without this, MCP-advertised tools would always carry the
    safe `False` defaults, leaving the C-AS-02 §2.3 forcing rows (1-2) + row 7 reachable
    ONLY for manually-built contracts (the production-path gap). With it, an operator
    declaring a computer-use MCP server raises ALL its discovered tools to the per-tool
    TIER_4 forcing path at the resolver."""
    cfg = _config(
        [
            MCPClientConfig(
                client_name=ClientName("computer-use-server"),
                transport=MCPTransport.STDIO,
                trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="stdio:///bin/echo",
                default_forces_computer_use=True,
                default_is_deterministic_inhouse=True,
            )
        ]
    )
    host = next(iter((await materialize_mcp_client_host_stage(cfg)).values()))
    converter = host._tool_contract_converter  # type: ignore[attr-defined]
    contract = converter(_FakeTool("browse", "drive a browser", {"type": "object"}))
    assert contract.forces_computer_use is True
    assert contract.is_deterministic_inhouse is True
    assert contract.forces_code_execution is False  # left at the conservative default


@pytest.mark.asyncio
async def test_converter_stamps_per_server_idempotent_default() -> None:
    """B-EFFECT-FENCE-PER-TOOL (AS spec C-AS-03 §3.1 v1.12 / runtime §14.22.7) — the
    stage-3a converter stamps the entry's per-server `default_idempotent` onto every
    MCP-discovered tool's `ToolContract.idempotent` (MCP advertisements carry no
    idempotency semantics, so the per-server default is the policy source for discovered
    tools — the production path the runtime effect fence reads to EXEMPT idempotent
    tools). Without this, discovered tools would always carry `False` (fenced); the
    over-applying direction (a wrong stamp → wrongly-exempt → fence silently disabled)
    is the unsafe one, so this pins the stamp. Default (omitted) stays `False`."""
    cfg = _config(
        [
            MCPClientConfig(
                client_name=ClientName("read-only-data-server"),
                transport=MCPTransport.STDIO,
                trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="stdio:///bin/echo",
                default_idempotent=True,
            )
        ]
    )
    host = next(iter((await materialize_mcp_client_host_stage(cfg)).values()))
    converter = host._tool_contract_converter  # type: ignore[attr-defined]
    contract = converter(_FakeTool("fetch", "pure read", {"type": "object"}))
    assert contract.idempotent is True


@pytest.mark.asyncio
async def test_converter_idempotent_defaults_false_when_unset() -> None:
    """Negative control — an entry that does NOT declare `default_idempotent` stamps
    `idempotent=False` (the conservative fence-by-default; discovered tools stay fenced)."""
    cfg = _config(
        [
            MCPClientConfig(
                client_name=ClientName("mutating-server"),
                transport=MCPTransport.STDIO,
                trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="stdio:///bin/echo",
            )
        ]
    )
    host = next(iter((await materialize_mcp_client_host_stage(cfg)).values()))
    converter = host._tool_contract_converter  # type: ignore[attr-defined]
    contract = converter(_FakeTool("append", "appends", {"type": "object"}))
    assert contract.idempotent is False


@pytest.mark.asyncio
async def test_converter_tolerates_none_description_and_schema() -> None:
    """v1.40 — `mcp.types.Tool.description` may be None and `inputSchema` may
    be absent; the converter substitutes safe defaults."""
    cfg = _config([_policy_client()])
    host = next(iter((await materialize_mcp_client_host_stage(cfg)).values()))
    converter = host._tool_contract_converter  # type: ignore[attr-defined]
    contract = converter(_FakeTool("noisy", None, None))
    assert contract.description == ""
    assert contract.input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_default_policy_field_defaults_defer_to_surface_policy() -> None:
    """v1.43 §14.9.9 / fork §7.1 (Reading A+) — a client that omits the per-server
    sandbox tier fields leaves them None (deferred to the deployment-surface-aware
    default policy resolved at the factory). `default_blast_radius` keeps the
    conservative READ_ONLY default (unchanged). Supersedes the v1.40 static-TIER_2
    default (operator-ratified 2026-06-11)."""
    entry = _stdio_client()
    assert entry.default_minimum_tier is None
    assert entry.default_sandbox_tier is None
    assert entry.default_blast_radius is BlastRadiusTier.READ_ONLY


# ---------------------------------------------------------------------------
# B-MCP-HOST-REMOTE-TRANSPORT — the granular `MCPTransport` config enum is
# projected onto the host's coarse connection-mechanism selector (runtime spec
# §14.9.6 inv 5 "STDIO + HTTP + SSE all supported at v1"). Before this fix, the
# factory passed `entry.transport.value` through a `cast`, so every remote
# `streamable_http_l*` value reached `MCPClientHost.__init__` (whose
# `_VALID_TRANSPORTS = {"stdio", "streamable_http", "sse"}` rejects the granular
# string) → `ValueError: unknown MCP transport`. No remote MCP host could
# materialize through the factory.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("granular", "expected_coarse"),
    [
        (MCPTransport.STDIO, "stdio"),
        (MCPTransport.STREAMABLE_HTTP_L0_REFUSE, "streamable_http"),
        (MCPTransport.STREAMABLE_HTTP_L1_PINNED, "streamable_http"),
        (MCPTransport.STREAMABLE_HTTP_L2_SANDBOX, "streamable_http"),
        (MCPTransport.STREAMABLE_HTTP_L3_AUDIT, "streamable_http"),
    ],
)
def test_coarse_transport_projects_granular_to_mechanism(
    granular: MCPTransport, expected_coarse: str
) -> None:
    """`_coarse_transport` collapses the granular trust-bearing `MCPTransport`
    onto the coarse connection mechanism the host consumes: `stdio → stdio`;
    all four `streamable_http_l*` (the L-level is a trust dimension, not a
    mechanism) → `streamable_http`. Total over the closed 5-value enum."""
    from harness_runtime.bootstrap.factories.mcp_client_host_factory import _coarse_transport

    assert _coarse_transport(granular) == expected_coarse


@pytest.mark.asyncio
async def test_factory_materializes_remote_streamable_http_host() -> None:
    """B-MCP-HOST-REMOTE-TRANSPORT — a remote `streamable_http_l1` MCP server
    materializes through the stage-3a factory (previously raised
    `ValueError: unknown MCP transport 'streamable_http_l1'`). The host carries
    the coarse `streamable_http` mechanism; its connection URL is the remote
    HTTP endpoint (not a stdio command line)."""
    cfg = _config(
        [
            MCPClientConfig(
                client_name=ClientName("remote-echo"),
                transport=MCPTransport.STREAMABLE_HTTP_L1_PINNED,
                trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="http://127.0.0.1:9999/mcp",
                default_minimum_tier=SandboxTier.TIER_1_PROCESS,
                default_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            )
        ]
    )
    hosts = await materialize_mcp_client_host_stage(cfg)
    host = hosts[ServerName("remote-echo")]
    assert isinstance(host, MCPClientHost)
    assert host.transport == "streamable_http"
    # the HTTP transport_config carries the remote URL (not a stdio command).
    assert host._transport_config == {"url": "http://127.0.0.1:9999/mcp"}  # type: ignore[attr-defined]
