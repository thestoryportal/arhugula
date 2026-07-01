"""U-RT-75 — Stage 5 factory materialize_runtime_tool_dispatcher_stage tests.

ACs per Implementation_Plan_Harness_Runtime_v2_13.md §1 U-RT-75 (cite-edited
at v2.13). Spec contract: Spec_Harness_Runtime_v1.md v1.16 §14.9.3 stage-5
factory contract + §14.11 C-RT-21.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_core import SandboxDecisionPolicy
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.mcp_client_namespace_emitter import MCPClientNamespaceEmitter
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.routing_manifest_residence import RetryPolicy
from harness_cp.topology_pattern import TopologyPattern
from harness_is.state_ledger_entry_schema import Identifier
from harness_runtime.bootstrap.factories.mcp_client_host_factory import (
    materialize_mcp_client_host_stage,
)
from harness_runtime.bootstrap.factories.runtime_tool_dispatcher_factory import (
    DEFAULT_TRUST_POLICY,
    PerTierToolExecutionDriver,
    materialize_runtime_tool_dispatcher_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.as_is_wiring import RuntimeAsIsWiring
from harness_runtime.lifecycle.retry_breaker import (
    DEFAULT_RETRY_POLICY,
    RuntimeRetryBreaker,
)
from harness_runtime.lifecycle.retry_breaker_tool import (
    RESERVED_TOOL_DISPATCH_KEY,
    RetryBreakerToolDispatcher,
)
from harness_runtime.lifecycle.tool_registry import ToolRegistry
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
    ServerName,
)
from opentelemetry.sdk.trace import TracerProvider


class _MockLedgerWriter:
    def __init__(self) -> None:
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> object:
        self.appends.append((payload, write_key))
        return object()


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
    )


async def _post_stage_3a_builder(
    cfg: RuntimeConfig, *, tools: list[Any] | None = None
) -> _MutableHarnessContext:
    """Construct a builder with the minimum stage-3a + stage-3b state the
    stage-5 factory consumes.

    `tools` (B6 Slice 2): ToolContracts registered into EACH host's started
    registry, so the stage-5 factory's per-tool reachable-tier scan (runtime spec
    v1.56 §14.9.11) has tools to resolve. Default `None` → empty registry (the
    pre-B6-Slice-2 behavior; a host with no tools needs no driver)."""
    builder = _MutableHarnessContext()
    # The stage-5 factory builds the routing index + per-tool driver registry from
    # each host's STARTED registry (production: stage-3a starts the hosts before
    # stage-5). These unit tests can't spawn a real MCP subprocess
    # (`stdio:///bin/echo` is not an MCP server), so each materialized host is mocked
    # started with a registry populated from `tools`.
    hosts = await materialize_mcp_client_host_stage(cfg)
    for host in hosts.values():
        host._started = True  # type: ignore[attr-defined]
        registry = ToolRegistry()
        for contract in tools or []:
            registry.register(contract)
        host._tool_registry = registry  # type: ignore[attr-defined]
    builder.mcp_client_hosts = hosts
    builder.retry_breaker = RuntimeRetryBreaker(
        retry_policies={
            RESERVED_TOOL_DISPATCH_KEY: RetryPolicy(
                max_attempts=3, backoff="full_jitter", jitter="full_jitter"
            ),
        },
        default_policy=DEFAULT_RETRY_POLICY,
        base_delay_seconds=0.0,
        delay_cap_seconds=0.01,
    )
    builder.tracer_provider = TracerProvider()
    builder.ledger_writer = _MockLedgerWriter()
    return builder


@pytest.mark.asyncio
async def test_factory_returns_retry_wrapper_instance() -> None:
    # AC #1 — returns RetryBreakerToolDispatcher.
    cfg = _config()
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert isinstance(wrapper, RetryBreakerToolDispatcher)


@pytest.mark.asyncio
async def test_step1_binds_per_server_trust_evaluator() -> None:
    # AC #2 — ctx.per_server_trust_evaluator bound to PerServerTrustEvaluator.
    cfg = _config()
    builder = await _post_stage_3a_builder(cfg)
    await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert isinstance(builder.per_server_trust_evaluator, PerServerTrustEvaluator)


@pytest.mark.asyncio
async def test_step2_binds_mcp_namespace_emitter() -> None:
    # AC #3 — ctx.mcp_namespace_emitter bound to MCPClientNamespaceEmitter.
    cfg = _config()
    builder = await _post_stage_3a_builder(cfg)
    await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert isinstance(builder.mcp_namespace_emitter, MCPClientNamespaceEmitter)


@pytest.mark.asyncio
async def test_step4_wrapper_inner_is_bare_runtime_tool_dispatcher() -> None:
    # AC #5 — wrapper.inner is a bare RuntimeToolDispatcher; bare is NOT
    # surfaced on the builder (private constructor arg per spec §14.9.6 inv 6).
    from harness_runtime.lifecycle.runtime_tool_dispatcher import (
        RuntimeToolDispatcher,
    )

    cfg = _config()
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert isinstance(wrapper.inner, RuntimeToolDispatcher)
    # bare dispatcher not surfaced on tool_dispatcher field (caller U-RT-68
    # binds the wrapper, not the bare); on the builder mid-factory it's
    # not present either.
    assert builder.tool_dispatcher is None  # caller binds, not the factory


@pytest.mark.asyncio
async def test_factory_default_constructs_fence_but_explicit_flag_off() -> None:
    # B-EFFECT-FENCE-DURABLE-AUTO (§14.22.7) — the factory now constructs the fence
    # UNCONDITIONALLY (lazy claim dir → no footprint until a reserve fires), and
    # carries `effect_fencing_explicit=False` for the default config. So the fence
    # is PRESENT (the auto-activation substrate for durable runs) but the operator's
    # blanket opt-in is OFF — a non-durable default run stays fence-free via the
    # per-run gate (the durable-engine reserve test covers the auto path).
    from harness_runtime.lifecycle.effect_fence import RuntimeEffectFence

    cfg = _config()
    assert cfg.effect_fencing is False
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert isinstance(wrapper.inner._effect_fence, RuntimeEffectFence)
    assert wrapper.inner._effect_fencing_explicit is False


@pytest.mark.asyncio
async def test_factory_binds_effect_fence_when_opted_in() -> None:
    # B-EFFECT-FENCE — `effect_fencing=True` → the factory constructs + threads a
    # RuntimeEffectFence to the bare dispatcher (the wiring link the dispatcher
    # unit tests assume; closes the factory side).
    from harness_runtime.lifecycle.effect_fence import RuntimeEffectFence

    cfg = _config().model_copy(update={"effect_fencing": True})
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert isinstance(wrapper.inner._effect_fence, RuntimeEffectFence)
    # B-EFFECT-FENCE-DURABLE-AUTO — explicit opt-in flag threads through → fence
    # EVERY tool step (the pre-v1.60 blanket semantic), regardless of engine class.
    assert wrapper.inner._effect_fencing_explicit is True


@pytest.mark.asyncio
async def test_factory_uses_runtime_default_trust_policy_when_config_omits() -> None:
    # AC #2 (extended) — config.trust_policy=None → factory uses DEFAULT_TRUST_POLICY.
    cfg = _config()
    assert cfg.trust_policy is None
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    # The bare dispatcher stores the trust policy as a private; verify via
    # the bound evaluator side-effect (evaluator constructed regardless).
    assert wrapper.inner is not None
    # And confirm the default policy itself is the canonical
    # conservative-tier-floor shape (sanity check on the module constant).
    assert DEFAULT_TRUST_POLICY.allow_list == frozenset()
    assert DEFAULT_TRUST_POLICY.deny_list == frozenset()


@pytest.mark.asyncio
async def test_factory_uses_sandbox_decision_policy_default_when_config_omits() -> None:
    # AC #4 (extended) — config.sandbox_decision_policy=None → factory uses
    # SandboxDecisionPolicy.default(). Verified by absence-of-error path
    # (empty-marker default is always constructible).
    cfg = _config()
    assert cfg.sandbox_decision_policy is None
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert wrapper is not None
    # Confirm SandboxDecisionPolicy.default() is the U-CORE-02 carrier.
    assert isinstance(SandboxDecisionPolicy.default(), SandboxDecisionPolicy)


@pytest.mark.asyncio
async def test_factory_does_not_bind_tool_dispatcher_directly() -> None:
    """Per spec §14.9.3 + U-RT-68's role: the factory returns the wrapper;
    the caller (stage 5 body / U-RT-68) binds it to ctx.tool_dispatcher.
    This separation preserves single-responsibility per atomic decomposition."""
    cfg = _config()
    builder = await _post_stage_3a_builder(cfg)
    assert builder.tool_dispatcher is None
    await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    # Factory does NOT bind tool_dispatcher; only intermediate carriers.
    assert builder.tool_dispatcher is None


@pytest.mark.asyncio
async def test_factory_threads_procedural_snapshot_resolver_to_secret_audit_emitter() -> None:
    """R-CXA-1 — TOOL_STEP secret-fetch audit emission receives the R-003 resolver."""
    cfg = _config()
    builder = await _post_stage_3a_builder(cfg)

    def _resolve() -> Identifier:
        return Identifier("b" * 64)

    builder.procedural_tier_snapshot_resolver = _resolve
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    emitter = wrapper.inner._secret_fetch_audit_emitter  # type: ignore[attr-defined]
    owner = getattr(emitter, "__self__", None)

    assert isinstance(owner, RuntimeAsIsWiring)
    assert owner.procedural_tier_snapshot_resolver is _resolve


# ---------------------------------------------------------------------------
# spec v1.41 §14.9.8 Reading B (Gap C) — the bootstrap factory wires a
# per-server default-policy sandbox_decision_resolver + (Gap E) the emitter
# info_lookup, for a configured server. (Replaces the former AC#2 xfail marker
# at this site — the resolver landed at v1.41.)
# ---------------------------------------------------------------------------


def _config_with_server(
    *,
    sandbox_tier: object = None,
) -> RuntimeConfig:
    from harness_as.discriminators import MCPTransport
    from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
    from harness_as.sandbox_tier_floor import MCPServerTrustLevel
    from harness_core import ClientName
    from harness_runtime.types import MCPClientConfig, SandboxDriverConfig

    tier = sandbox_tier if sandbox_tier is not None else SandboxTier.TIER_1_PROCESS
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[
            MCPClientConfig(
                client_name=ClientName("echo-server"),
                transport=MCPTransport.STDIO,
                trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="stdio:///bin/echo",
                default_minimum_tier=tier,  # type: ignore[arg-type]
                # R-FS-1 B6 Slice 1: a STDIO server floors to TIER_3 (ADR-D2 §1.3 /
                # runtime spec v1.54 §14.9.8), so it needs a tier-3-capable driver to
                # bootstrap. `default_sandbox_tier`/`tech`/`provider` are left unset so
                # the surface default + the transport floor compose (→ TIER_3, gvisor).
                sandbox_driver=SandboxDriverConfig(
                    command=("python", "-c", "pass"), image="echo:latest"
                ),
            )
        ],
    )


def _tool_contract_and_step(minimum_tier: object) -> tuple[object, object]:
    from harness_as.sandbox_tier import BlastRadiusTier
    from harness_as.tool_contract import ToolContract
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep

    contract = ToolContract(
        name="echo",
        description="echo a string",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=minimum_tier,  # type: ignore[arg-type]
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )
    step = WorkflowStep(
        step_id="step-1",
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": "echo", "tool_args": {"text": "hi"}},
    )
    return contract, step


def _read_only_tool(name: str = "echo") -> Any:
    """A READ_ONLY ToolContract — on a STDIO host it resolves to TIER_3 (sandbox_tier_floor
    row 3: max(TIER_3, blast_floor(READ_ONLY))). B6 Slice 2 per-tool fixture."""
    from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
    from harness_as.tool_contract import ToolContract

    return ToolContract(
        name=name,
        description="read-only",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )


def _forcing_tool(name: str = "browse") -> Any:
    """A `forces_computer_use` ToolContract — resolves to TIER_4 regardless of transport/blast
    (sandbox_tier_floor row 1). B6 Slice 2 per-tool fixture."""
    from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
    from harness_as.tool_contract import ToolContract

    return ToolContract(
        name=name,
        description="computer-use",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
        forces_computer_use=True,
    )


@pytest.mark.asyncio
async def test_factory_wires_per_tool_sandbox_resolver() -> None:
    """v1.41 Gap C → R-FS-1 B6 Slice 2 (runtime spec v1.56 §14.9.11): a configured
    server's bootstrap dispatcher carries a NON-raising PER-TOOL sandbox resolver.

    For a READ_ONLY tool on a STDIO server the per-tool `sandbox_tier_floor` row 3
    (STDIO → max(TIER_3, blast_floor)) drives the tier to TIER_3 with the gvisor/runsc
    labels, and the reason records the per-tool cause (C-AS-02 §2.3) — subsuming B6
    Slice 1's per-host transport floor per-tool."""
    from harness_as.sandbox_tier import SandboxTier

    cfg = _config_with_server(sandbox_tier=SandboxTier.TIER_1_PROCESS)
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)

    contract, step = _tool_contract_and_step(SandboxTier.TIER_1_PROCESS)
    resolver = wrapper.inner._sandbox_resolvers[ServerName("echo-server")]  # type: ignore[attr-defined]
    decision = resolver(contract, step)
    # B6 Slice 2: the per-tool floor (STDIO row 3) raises the READ_ONLY echo tool to
    # TIER_3; the reason records the per-tool cause (security telemetry honesty).
    assert decision.tier is SandboxTier.TIER_3_MICROVM
    assert decision.tech == "gvisor"
    assert decision.provider == "runsc"
    assert (
        decision.assigned_tier_reason
        == "per-tool-sandbox-floor: echo → tier-3-microvm (C-AS-02 §2.3)"
    )


@pytest.mark.asyncio
async def test_resolver_tier_floor_consistency_passes_when_equal() -> None:
    """v1.41 Gap C — when default_sandbox_tier == default_minimum_tier the
    §14.9.4 floor (resolved.tier >= contract.minimum_tier) is satisfied."""
    from harness_as.sandbox_tier import SandboxTier
    from harness_runtime.lifecycle.runtime_tool_dispatcher import _SANDBOX_TIER_RANK

    cfg = _config_with_server(sandbox_tier=SandboxTier.TIER_1_PROCESS)
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    contract, step = _tool_contract_and_step(SandboxTier.TIER_1_PROCESS)
    resolver = wrapper.inner._sandbox_resolvers[ServerName("echo-server")]  # type: ignore[attr-defined]
    decision = resolver(contract, step)
    # Floor passes: resolved tier is NOT below the tool's minimum.
    assert _SANDBOX_TIER_RANK[decision.tier] >= _SANDBOX_TIER_RANK[contract.minimum_tier]


@pytest.mark.asyncio
async def test_factory_wires_emitter_info_lookup_for_configured_server() -> None:
    """v1.41 Gap E — the emitter's info_lookup is wired from the host (does not
    raise on the dispatch step-7 path) for a configured server."""
    cfg = _config_with_server()
    builder = await _post_stage_3a_builder(cfg)
    await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    info = builder.mcp_namespace_emitter._info_lookup("echo-server")  # type: ignore[attr-defined]
    assert info.transport == "stdio"
    assert info.auth_present is False


@pytest.mark.asyncio
async def test_empty_server_set_leaves_no_per_host_resolver_or_driver() -> None:
    """§14.9.10 D1/D2 — with NO configured server, the per-host resolver/driver
    maps + the routing index are empty, and the emitter's info_lookup stays the
    raise-on-call default (a TOOL_STEP raises RT-FAIL-TOOL-CONTRACT-UNKNOWN at
    dispatch step 0 on the empty routing index)."""
    cfg = _config()  # empty mcp_clients
    builder = await _post_stage_3a_builder(cfg)
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    assert wrapper.inner._sandbox_resolvers == {}  # type: ignore[attr-defined]
    assert wrapper.inner._tool_execution_drivers == {}  # type: ignore[attr-defined]
    assert wrapper.inner._routing_index == {}  # type: ignore[attr-defined]
    with pytest.raises(LookupError):
        builder.mcp_namespace_emitter._info_lookup("nope")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# v1.43 §14.9.9 — tier→driver selection wired through the real materialize path.
# ---------------------------------------------------------------------------


def _bare_server_config(
    surface: DeploymentSurface, *, sandbox_driver: object = None
) -> RuntimeConfig:
    """A configured STDIO-server RuntimeConfig that leaves the per-server sandbox
    tier UNSET (None) so the deployment-surface-aware default policy applies.

    R-FS-1 B6 Slice 1: a STDIO server floors to TIER_3 (ADR-D2 §1.3) regardless of
    surface, so a bare config here (no `sandbox_driver`) fails loud at bootstrap. The
    pure tier→driver registry (TIER_1 in-process / TIER_2 Docker / etc.) is covered
    directly in `test_sandbox_tier_driver_selection.py`; this file exercises the
    floored path through the real stage-5 factory."""
    from harness_as.discriminators import MCPTransport
    from harness_as.sandbox_tier import BlastRadiusTier
    from harness_as.sandbox_tier_floor import MCPServerTrustLevel
    from harness_core import ClientName
    from harness_runtime.types import MCPClientConfig

    return RuntimeConfig(
        deployment_surface=surface,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[
            MCPClientConfig(
                client_name=ClientName("echo-server"),
                transport=MCPTransport.STDIO,
                trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                blast_radius=BlastRadiusTier.READ_ONLY,
                connection_url="stdio:///bin/echo",
                sandbox_driver=sandbox_driver,  # type: ignore[arg-type]
            )
        ],
    )


@pytest.mark.asyncio
async def test_bare_local_dev_stdio_server_with_tool_floored_to_tier3_fails_loud() -> None:
    """R-FS-1 B6 Slice 2 (runtime spec v1.56 §14.9.11) — the per-tool behavioral change:
    a bare local-development STDIO server with a READ_ONLY tool floors that tool to
    TIER_3 (sandbox_tier_floor row 3, ADR-D2 §1.3) per-tool, so the per-tier driver
    registry needs a TIER_3 driver — absent (bare config) → RAISES
    `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` (FR-2(i)) rather than silently under-sandboxing."""
    from harness_runtime.lifecycle.runtime_tool_dispatcher import SandboxDriverUnavailableError

    cfg = _bare_server_config(DeploymentSurface.LOCAL_DEVELOPMENT)
    builder = await _post_stage_3a_builder(cfg, tools=[_read_only_tool()])
    with pytest.raises(SandboxDriverUnavailableError):
        await materialize_runtime_tool_dispatcher_stage(builder, cfg)


@pytest.mark.asyncio
async def test_bare_managed_cloud_server_with_tool_fails_loud_not_in_process() -> None:
    """v1.43 §14.9.9 FR-2(i) + B6 Slice 2 — the security fix end-to-end through the real
    bootstrap factory: a bare managed-cloud STDIO server with a READ_ONLY tool (per-tool
    floored to TIER_3) and no driver configured RAISES `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE`
    rather than silently executing in-process."""
    from harness_runtime.lifecycle.runtime_tool_dispatcher import SandboxDriverUnavailableError

    cfg = _bare_server_config(DeploymentSurface.MANAGED_CLOUD)
    builder = await _post_stage_3a_builder(cfg, tools=[_read_only_tool()])
    with pytest.raises(SandboxDriverUnavailableError):
        await materialize_runtime_tool_dispatcher_stage(builder, cfg)


@pytest.mark.asyncio
async def test_toolless_stdio_server_needs_no_driver() -> None:
    """R-FS-1 B6 Slice 2 — per-tool refinement of B6 Slice 1: a STDIO server with NO tools
    exposes no dispatch surface, so its per-tier driver registry is EMPTY and a bare config
    (no `sandbox_driver`) does NOT fail-close (nothing to sandbox). The fail-close fires
    per-tool, where a tool actually reaches a driver-requiring tier."""
    cfg = _bare_server_config(DeploymentSurface.LOCAL_DEVELOPMENT)
    builder = await _post_stage_3a_builder(cfg)  # no tools
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    driver = wrapper.inner._tool_execution_drivers[ServerName("echo-server")]  # type: ignore[attr-defined]
    assert isinstance(driver, PerTierToolExecutionDriver)
    assert dict(driver.drivers) == {}


@pytest.mark.asyncio
async def test_stdio_server_with_driver_wires_gvisor_at_floored_tier3() -> None:
    """R-FS-1 B6 Slice 2 + v1.43 §14.9.9 FR-1 — a STDIO server with a READ_ONLY tool and a
    configured container driver wires a `PerTierToolExecutionDriver` whose TIER_3 entry is
    the gVisor (microVM) driver — delivering the TIER_3 the per-tool STDIO floor (row 3)
    raised the tool to (NOT the TIER_2 Docker the bare surface default would select)."""
    from harness_as.sandbox_tier import SandboxTier
    from harness_runtime.lifecycle.docker_tool_execution_driver import (
        GVisorRunscToolRunnerExecutionDriver,
    )
    from harness_runtime.types import SandboxDriverConfig

    cfg = _bare_server_config(
        DeploymentSurface.MANAGED_CLOUD,
        sandbox_driver=SandboxDriverConfig(command=("python", "runner.py"), image="echo:latest"),
    )
    builder = await _post_stage_3a_builder(cfg, tools=[_read_only_tool()])
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    driver = wrapper.inner._tool_execution_drivers[ServerName("echo-server")]  # type: ignore[attr-defined]
    assert isinstance(driver, PerTierToolExecutionDriver)
    inner = driver.drivers[SandboxTier.TIER_3_MICROVM]
    assert isinstance(inner, GVisorRunscToolRunnerExecutionDriver)
    assert inner.required_tier is SandboxTier.TIER_3_MICROVM


@pytest.mark.asyncio
async def test_mixed_tier_host_builds_per_tier_driver_registry() -> None:
    """R-FS-1 B6 Slice 2 — a host with a READ_ONLY tool (per-tool TIER_3 on STDIO) AND a
    `forces_computer_use` tool (per-tool TIER_4) builds a per-tier registry with BOTH
    drivers (gVisor@TIER_3 + E2B@TIER_4) — each tool delivers exactly its resolved tier."""
    from harness_as.sandbox_tier import SandboxTier
    from harness_runtime.lifecycle.docker_tool_execution_driver import (
        GVisorRunscToolRunnerExecutionDriver,
    )
    from harness_runtime.lifecycle.e2b_tool_execution_driver import (
        E2BManagedFullVMToolRunnerExecutionDriver,
    )
    from harness_runtime.types import SandboxDriverConfig

    cfg = _bare_server_config(
        DeploymentSurface.MANAGED_CLOUD,
        sandbox_driver=SandboxDriverConfig(command=("python", "runner.py"), image="echo:latest"),
    )
    builder = await _post_stage_3a_builder(
        cfg, tools=[_read_only_tool("ro"), _forcing_tool("browse")]
    )
    wrapper = await materialize_runtime_tool_dispatcher_stage(builder, cfg)
    driver = wrapper.inner._tool_execution_drivers[ServerName("echo-server")]  # type: ignore[attr-defined]
    assert isinstance(driver, PerTierToolExecutionDriver)
    assert set(driver.drivers) == {SandboxTier.TIER_3_MICROVM, SandboxTier.TIER_4_FULL_VM}
    assert isinstance(
        driver.drivers[SandboxTier.TIER_3_MICROVM], GVisorRunscToolRunnerExecutionDriver
    )
    assert isinstance(
        driver.drivers[SandboxTier.TIER_4_FULL_VM], E2BManagedFullVMToolRunnerExecutionDriver
    )


@pytest.mark.asyncio
async def test_per_tier_driver_selects_by_resolved_tier_and_fails_loud_on_missing() -> None:
    """R-FS-1 B6 Slice 2 — `PerTierToolExecutionDriver` selects the driver matching the
    per-tool resolved `SandboxDispatchDecision.tier` (delivered == resolved); a resolved
    tier with no registered driver raises RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE."""
    from harness_as.sandbox_tier import SandboxTier
    from harness_runtime.lifecycle.runtime_tool_dispatcher import (
        SandboxDispatchDecision,
        SandboxDriverUnavailableError,
    )

    calls: list[SandboxTier] = []

    class _Fake:
        def __init__(self, tier: SandboxTier) -> None:
            self.tier = tier

        async def call_tool(self, *, mcp_client_host: Any, sandbox_decision: Any, **_: Any) -> Any:
            calls.append(self.tier)
            return {"ok": sandbox_decision.tier.value}

    composite = PerTierToolExecutionDriver(
        drivers={SandboxTier.TIER_3_MICROVM: _Fake(SandboxTier.TIER_3_MICROVM)},  # type: ignore[dict-item]
        server_name="srv",
    )

    def _decision(tier: SandboxTier) -> Any:
        return SandboxDispatchDecision(
            tier=tier, tech="t", provider="p", assigned_tier_reason="x", cost_tier_overhead_ms=0
        )

    out = await composite.call_tool(
        mcp_client_host=object(),  # type: ignore[arg-type]
        sandbox_decision=_decision(SandboxTier.TIER_3_MICROVM),
        tool_id="t",
        tool_args={},
        idempotency_key="k",
    )
    assert out == {"ok": "tier-3-microvm"}
    assert calls == [SandboxTier.TIER_3_MICROVM]

    with pytest.raises(SandboxDriverUnavailableError):
        await composite.call_tool(
            mcp_client_host=object(),  # type: ignore[arg-type]
            sandbox_decision=_decision(SandboxTier.TIER_4_FULL_VM),
            tool_id="t",
            tool_args={},
            idempotency_key="k",
        )
