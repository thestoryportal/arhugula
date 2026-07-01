"""Stage 5 factory — `materialize_runtime_tool_dispatcher_stage(ctx, config)
→ RetryBreakerToolDispatcher`.

Per `Spec_Harness_Runtime_v1.md` v1.16 §14.9.3 stage-5 factory contract +
§14.11 C-RT-21 (added at v1.15 per U-RT-68 fork Q1=B + Q2=B2 ratification).

5-step composition body per spec §14.9.3 stage-5 prose:

  1. Construct `PerServerTrustEvaluator` (consumes `config.trust_policy`,
     or a runtime-supplied conservative default if `None`).
  2. Construct `MCPClientNamespaceEmitter` (consumes a multi-host info-lookup
     over `ctx.mcp_client_hosts`, resolving the RESOLVED host's info by
     `server_name` downstream at `emit_mcp_call_span` time — U-RT-127/128).
  3. Construct the bare `RuntimeToolDispatcher` (C-RT-19) with `ctx.mcp_client_hosts`
     + the U-RT-127 `routing_index` (collision → fail-loud) + per-host sandbox
     resolvers/drivers (U-RT-130) + the new evaluator + the new emitter + the
     trust policy + `config.sandbox_decision_policy` (or
     `SandboxDecisionPolicy.default()` if `None`).
  4. Construct the `RetryBreakerToolDispatcher` (C-RT-21 §14.11)
     wrapping the bare dispatcher with `inner=<bare>` +
     `retry_breaker=ctx.retry_breaker` + `tracer_provider=ctx.tracer_provider`.
  5. Return the wrapper. The caller (U-RT-68 stage-5 wire-up at
     `stage_5_loop_init.py`) binds the wrapper to `ctx.tool_dispatcher`;
     intermediate carriers (evaluator + emitter) are bound to
     `ctx.per_server_trust_evaluator` + `ctx.mcp_namespace_emitter` by
     this factory (mutates `ctx` directly).

The bare `RuntimeToolDispatcher` is private to the wrapper per spec
§14.9.6 invariant 6 — not surfaced on `HarnessContext`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from harness_as.sandbox_tier import SandboxTier
from harness_as.tool_contract import ToolContract
from harness_core import SandboxDecisionPolicy
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.mcp_client_namespace_emitter import (
    MCPClientNamespaceEmitter,
    MCPServerInfo,
    MCPServerInfoLookup,
)
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.per_server_trust_types import (
    TierDerivationRule,
    TrustPolicy,
)
from harness_cp.workflow_driver_types import WorkflowStep

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.config.sandbox_defaults import (
    EffectiveSandboxDefaults,
    resolve_effective_sandbox_defaults,
    resolve_per_tool_sandbox_defaults,
)
from harness_runtime.lifecycle.as_is_wiring import RuntimeAsIsWiring
from harness_runtime.lifecycle.docker_tool_execution_driver import (
    DockerToolRunnerExecutionDriver,
    GVisorRunscToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.e2b_tool_execution_driver import (
    E2BManagedFullVMToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.effect_fence import RuntimeEffectFence
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.retry_breaker_tool import RetryBreakerToolDispatcher
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    MCPHostToolExecutionDriver,
    MCPToolNameCollisionError,
    RuntimeToolDispatcher,
    SandboxDecisionResolver,
    SandboxDispatchDecision,
    SandboxDriverUnavailableError,
    ToolExecutionDriver,
)
from harness_runtime.types import (
    MCPClientConfig,
    RuntimeConfig,
    SandboxDriverConfig,
    ServerName,
    ToolName,
)

__all__ = [
    "DEFAULT_TRUST_POLICY",
    "materialize_runtime_tool_dispatcher_stage",
]


def _build_per_tool_sandbox_resolver(
    entry: MCPClientConfig,
    deployment_surface: DeploymentSurface,
    surface_default: EffectiveSandboxDefaults,
) -> SandboxDecisionResolver:
    """Build a PER-TOOL `SandboxDecisionResolver` (runtime spec v1.56 §14.9.11 — B6
    Slice 2, `B-PER-TOOL-SANDBOX-TIER`).

    Each `(contract, step)` resolves via `resolve_per_tool_sandbox_defaults` —
    `max(surface_default.sandbox_tier, sandbox_tier_floor(per-tool ...))` — so the
    C-AS-02 §2.3 per-tool forcing rows (1-2) + per-tool blast rows (7-10) become
    reachable per tool, while rows 3-6 subsume B6 Slice 1's per-host transport floor
    per-tool (STDIO→TIER_3 preserved). The `step` is unused (the resolved tier is a
    function of `(contract, host, surface)`); the `SandboxDecisionResolver` signature
    is unchanged. The resolved `tier` is compared against `contract.minimum_tier` at
    the §14.9.4 tier-floor check (a per-tool tier ≥ the surface default never spuriously
    violates a bare config's floor).
    """

    def resolve(contract: ToolContract, _step: WorkflowStep) -> SandboxDispatchDecision:
        eff = resolve_per_tool_sandbox_defaults(
            contract, entry, deployment_surface, surface_default
        )
        return SandboxDispatchDecision(
            tier=eff.sandbox_tier,
            tech=eff.sandbox_tech,
            provider=eff.sandbox_provider,
            assigned_tier_reason=eff.assigned_tier_reason,
            cost_tier_overhead_ms=0,
        )

    return resolve


@dataclass(frozen=True)
class PerTierToolExecutionDriver:
    """Per-host per-tier driver registry — selects the `ToolExecutionDriver` matching the
    per-tool resolved `SandboxDispatchDecision.tier` at dispatch (runtime spec v1.56
    §14.9.11 — B6 Slice 2, Option A; the §14.9.9/§14.9.10 inv-3 per-server-uniform→per-dispatch
    relaxation).

    Implements the `ToolExecutionDriver` protocol so the bare `RuntimeToolDispatcher`'s
    `dict[ServerName, ToolExecutionDriver]` field + dispatch body are byte-unchanged — the
    per-dispatch driver selection lives here, inside `call_tool`. The registry holds exactly
    the tiers reachable by this host's tools (built once at the stage-5 factory;
    `tool_registry` is immutable after `start()`, §14.9.1). A resolved tier with no
    registered driver is a defensive `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` (the factory
    builds every reachable tier's driver, so this is unreachable by construction).
    """

    drivers: Mapping[SandboxTier, ToolExecutionDriver]
    server_name: str

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        driver = self.drivers.get(sandbox_decision.tier)
        if driver is None:
            raise SandboxDriverUnavailableError(
                f"RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE: server={self.server_name!r} "
                f"tool={tool_id!r} resolved tier {sandbox_decision.tier.value!r} has no "
                f"registered driver — the per-tier registry built only the tiers reachable "
                f"by this host's tools at stage 5 (runtime spec §14.9.11)"
            )
        return await driver.call_tool(
            mcp_client_host=mcp_client_host,
            sandbox_decision=sandbox_decision,
            tool_id=tool_id,
            tool_args=tool_args,
            idempotency_key=idempotency_key,
        )


def _select_tool_execution_driver(
    *, tier: SandboxTier, driver_config: SandboxDriverConfig | None
) -> ToolExecutionDriver:
    """Select the `ToolExecutionDriver` delivering `tier` — spec v1.43 §14.9.9 FR-1/FR-2.

    `TIER_1_PROCESS` → the in-process host driver (no substrate; the
    local-development out-of-box default). Higher tiers require a `sandbox_driver`
    config; its absence — or a missing field the tier's driver requires — is
    fail-loud `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` (FR-2(i)). The factory NEVER
    falls through to in-process for a `> TIER_1_PROCESS` claim (the silent-in-process
    defect this closes); invariant: delivered-tier ≥ resolved-tier, no silent downgrade.
    """
    if tier is SandboxTier.TIER_1_PROCESS:
        return MCPHostToolExecutionDriver()

    if driver_config is None:
        raise SandboxDriverUnavailableError(
            f"resolved sandbox tier {tier.value!r} requires an execution driver but "
            f"MCPClientConfig.sandbox_driver is not configured — configure a driver or set "
            f"the per-server sandbox tier to TIER_1_PROCESS (runtime spec v1.43 §14.9.9 FR-2(i))"
        )

    if tier is SandboxTier.TIER_2_CONTAINER:
        if driver_config.image is None:
            raise SandboxDriverUnavailableError(
                f"resolved tier {tier.value!r} (container) requires sandbox_driver.image"
            )
        return DockerToolRunnerExecutionDriver(
            image=driver_config.image,
            command=driver_config.command,
            docker_binary=driver_config.docker_binary,
            timeout_seconds=driver_config.timeout_seconds,
            network=driver_config.network,
        )

    if tier is SandboxTier.TIER_3_MICROVM:
        if driver_config.image is None:
            raise SandboxDriverUnavailableError(
                f"resolved tier {tier.value!r} (microVM/gVisor) requires sandbox_driver.image"
            )
        return GVisorRunscToolRunnerExecutionDriver(
            image=driver_config.image,
            command=driver_config.command,
            docker_binary=driver_config.docker_binary,
            timeout_seconds=driver_config.timeout_seconds,
            network=driver_config.network,
        )

    if tier is SandboxTier.TIER_4_FULL_VM:
        return E2BManagedFullVMToolRunnerExecutionDriver(
            command=driver_config.command,
            timeout_seconds=int(driver_config.timeout_seconds),
            sandbox_timeout_seconds=driver_config.sandbox_timeout_seconds,
            allow_internet_access=driver_config.allow_internet_access,
        )

    # Exhaustive over the closed 4-value SandboxTier enum — defensive only.
    raise SandboxDriverUnavailableError(  # pragma: no cover
        f"no execution driver registered for resolved tier {tier.value!r}"
    )


def _build_hosts_info_lookup(
    hosts: dict[ServerName, MCPClientHost],
) -> MCPServerInfoLookup:
    """Build a sync `MCPServerInfoLookup` over ALL started hosts (U-RT-127/128;
    spec §14.9.10 D2 — re-reads the v1.41 §14.9.8 Gap-E single-host lookup).

    The emitter's `info_lookup` is sync and fires per dispatch (step 7) with the
    RESOLVED host's `server_name`; it returns THAT host's already-resolved fields
    (no async `health_check`). All four `MCPServerInfo` fields are host-derivable.
    """

    def lookup(server_name: str) -> MCPServerInfo:
        host = hosts[cast(ServerName, server_name)]
        return MCPServerInfo(
            transport=host.transport,
            protocol_version=host.protocol_version,
            auth_present=host.auth_present,
            trust_tier=host.trust_tier,
        )

    return lookup


def build_tool_routing_index(
    hosts: dict[ServerName, MCPClientHost],
) -> dict[ToolName, ServerName]:
    """U-RT-127 — the derived tool→server routing index (spec §14.9.10 D2).

    Walks each host's `list_tools`-populated `tool_registry` and maps each tool
    to its owning host's `server_name`. The per-host registries remain the
    authority for each tool's `ToolContract` (one-source-of-truth — this index is
    a synchronized derived lookup, never a 2nd authority).

    Collision policy — fail-loud at bootstrap: a `tool_id` advertised by ≥2 hosts
    raises `RT-FAIL-MCP-TOOL-NAME-COLLISION` (permanent; bootstrap aborts) per the
    §14.9.9 FR-2 detect-then-refuse posture, so routing stays deterministic.
    """
    index: dict[ToolName, ServerName] = {}
    for server_name, host in hosts.items():
        for tool_name in host.tool_registry.names():
            existing = index.get(tool_name)
            if existing is not None:
                raise MCPToolNameCollisionError(
                    f"RT-FAIL-MCP-TOOL-NAME-COLLISION: tool {tool_name!r} "
                    f"advertised by ≥2 MCP hosts ({existing!r} and "
                    f"{server_name!r}) — bootstrap aborts (server-qualified "
                    f"addressing is a registered forward item)"
                )
            index[tool_name] = server_name
    return index


DEFAULT_TRUST_POLICY = TrustPolicy(
    default_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE,
    per_server_overrides={},
    allow_list=frozenset(),
    deny_list=frozenset(),
    require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    tier_derivation=TierDerivationRule.CONSERVATIVE,
)
"""Runtime-supplied conservative default `TrustPolicy` per spec §14.11
"TrustPolicy.default() if None" prose — refuses all unknown remote servers
+ requires audit below the highest non-refuse tier."""


async def materialize_runtime_tool_dispatcher_stage(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    *,
    rate_table: Any = None,
) -> RetryBreakerToolDispatcher:
    """Compose the C-RT-21 retry-wrap around the bare C-RT-19 tool dispatcher.

    Mutates `ctx` in-place: binds `ctx.per_server_trust_evaluator` and
    `ctx.mcp_namespace_emitter` to the constructed sibling carriers. The
    caller (U-RT-68) binds the returned wrapper to `ctx.tool_dispatcher`.

    Per spec v1.16 §14.9.3 stage-5 factory contract + U-RT-75 AC
    (Implementation_Plan_Harness_Runtime_v2_13.md).
    """
    assert ctx.mcp_client_hosts is not None, (
        "stage 3a (U-RT-73/126) must populate ctx.mcp_client_hosts before stage 5"
    )
    assert ctx.retry_breaker is not None, (
        "stage 3b (U-RT-24) must populate ctx.retry_breaker before stage 5"
    )
    assert ctx.ledger_writer is not None, (
        "stage 1 IS must populate ctx.ledger_writer before stage 5 TOOL_STEP dispatch"
    )

    # U-RT-127: build the cross-host tool→server routing index. A `tool_id`
    # advertised by ≥2 hosts fails loud here (RT-FAIL-MCP-TOOL-NAME-COLLISION,
    # bootstrap aborts). Empty config → empty index → every TOOL_STEP raises
    # RT-FAIL-TOOL-CONTRACT-UNKNOWN at dispatch.
    routing_index = build_tool_routing_index(ctx.mcp_client_hosts)

    trust_policy = config.trust_policy if config.trust_policy is not None else DEFAULT_TRUST_POLICY
    sandbox_decision_policy = (
        config.sandbox_decision_policy
        if config.sandbox_decision_policy is not None
        else SandboxDecisionPolicy.default()
    )
    # `sandbox_decision_policy` is committed to the bare dispatcher's
    # interface via the runtime spec v1.16 §3 contract, but the existing
    # C-RT-19 dispatcher predates the field and does not yet consume it
    # (§14.9.1 step 5 reads only `sandbox.tier ≥ ToolContract.minimum_tier`
    # — dangling marker per spec v1.16 finding (i)). The policy is
    # received here for spec-contract conformance + future-arc consumption.
    _ = sandbox_decision_policy

    # --- Step 1: per-server trust evaluator ----------------------------------
    per_server_trust_evaluator = PerServerTrustEvaluator()
    ctx.per_server_trust_evaluator = per_server_trust_evaluator

    # --- Step 2: MCP namespace emitter (Gap E — info_lookup over ALL hosts) ---
    # spec §14.9.10 D2: the emitter's per-dispatch step-7 info_lookup spans ALL
    # hosts, resolving the RESOLVED host's info by `server_name` at dispatch.
    # Bare `MCPClientNamespaceEmitter()` (default-raise lookup) is preserved only
    # when no host is configured (empty `{}`; dispatch never reaches step 7
    # because step 0 raises TOOL-CONTRACT-UNKNOWN on the empty routing index).
    info_lookup: MCPServerInfoLookup | None = (
        _build_hosts_info_lookup(ctx.mcp_client_hosts) if ctx.mcp_client_hosts else None
    )
    mcp_namespace_emitter = MCPClientNamespaceEmitter(info_lookup=info_lookup)
    ctx.mcp_namespace_emitter = mcp_namespace_emitter

    # --- Step 2b: PER-HOST surface-aware sandbox resolver/driver (U-RT-130) ----
    # Each host's resolver/driver is built from its OWN `MCPClientConfig`
    # default_sandbox_* / sandbox_driver (replacing the single-server `[0]`),
    # keyed by `server_name`. The §14.9.8 deployment-surface-aware policy
    # (Reading A+: local-development → honest TIER_1_PROCESS; production →
    # fail-safe-high TIER_2_CONTAINER) + the §14.9.9 FR-1 (delivered-tier ≥
    # resolved-tier) + FR-2 (fail-loud RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE, never
    # silent in-process) all apply PER host, independently. Empty config → empty
    # maps (every TOOL_STEP raises TOOL-CONTRACT-UNKNOWN before either is reached).
    cfg_by_server: dict[ServerName, MCPClientConfig] = {
        ServerName(entry.client_name): entry for entry in config.mcp_clients
    }
    sandbox_decision_resolvers: dict[ServerName, SandboxDecisionResolver] = {}
    tool_execution_drivers: dict[ServerName, ToolExecutionDriver] = {}
    for server_name in ctx.mcp_client_hosts:
        entry = cfg_by_server[server_name]
        host = ctx.mcp_client_hosts[server_name]
        # R-FS-1 B6 Slice 2 (runtime spec v1.56 §14.9.11): PER-TOOL sandbox resolution.
        # The surface-aware default (`resolve_effective_sandbox_defaults`) is the floor;
        # the full per-tool `sandbox_tier_floor` (rows 1-2 forcing + 3-6 transport/trust —
        # subsuming B6 Slice 1's per-host `compose_transport_floor` per-tool — + 7-10
        # per-tool blast) drives the rest. The per-host loop no longer composes
        # `compose_transport_floor` (its host-blast transport floor is subsumed per-tool;
        # using it would bake the host's coarse blast into every tool, defeating per-tool).
        surface_default = resolve_effective_sandbox_defaults(entry, config.deployment_surface)
        sandbox_decision_resolvers[server_name] = _build_per_tool_sandbox_resolver(
            entry, config.deployment_surface, surface_default
        )
        # Per-host per-tier driver registry (Option A) — one driver per tier reachable by
        # this host's tools (computable: `tool_registry` is immutable after start()). A
        # reachable tier with no registrable driver fail-closes (RT-FAIL-SANDBOX-DRIVER-
        # UNAVAILABLE, §14.9.9 FR-2(i)), now raised per-tier. The composite selects the
        # delivering driver per dispatch by the per-tool resolved tier (delivered == resolved).
        reachable_tiers = {
            resolve_per_tool_sandbox_defaults(
                host.tool_registry.get(cast("ToolName", tool_name)),
                entry,
                config.deployment_surface,
                surface_default,
            ).sandbox_tier
            for tool_name in host.tool_registry.names()
        }
        tool_execution_drivers[server_name] = PerTierToolExecutionDriver(
            drivers={
                tier: _select_tool_execution_driver(tier=tier, driver_config=entry.sandbox_driver)
                for tier in reachable_tiers
            },
            server_name=server_name,
        )

    # B-EFFECT-FENCE (§14.22 C-RT-31) + B-EFFECT-FENCE-DURABLE-AUTO (§14.22.7) —
    # construct the durable at-most-once effect fence UNCONDITIONALLY. Its claim
    # files live under `repository_root/.harness/effect-fence` (the §14.21
    # `repository_root` basis for `.harness/`) and the claim DIR is created LAZILY
    # on first reserve, so a run that never reserves leaves no directory (the
    # pre-v1.60 byte-identical footprint). The dispatcher's per-run gate decides
    # whether a reserve fires: `effect_fencing_explicit` (the operator opt-in,
    # fence every step) OR an AUTO-activation when the run's `binding.engine_class`
    # is durable (§14.22.7). Constructing always is what lets a durable run
    # auto-fence WITHOUT the operator flag (the daemon-reused dispatcher cannot
    # know the per-run engine class at bootstrap, so the gate is per-dispatch).
    effect_fence = RuntimeEffectFence(
        fence_dir=config.repository_root / ".harness" / "effect-fence"
    )

    # --- Step 3: bare RuntimeToolDispatcher (C-RT-19) ------------------------
    # U-OD-39: thread cost-attribution substrate (cost_chain + audit_writer
    # from ctx; rate_table from caller kwarg sourced from RATE_TABLE_V1 at
    # stage_5_loop_init.py). All 3 None-safe at unit-test path; production
    # bootstrap binds all 3 per `_attribute_tool_cost_best_effort` semantics.
    bare_dispatcher = RuntimeToolDispatcher(
        mcp_client_hosts=ctx.mcp_client_hosts,
        routing_index=routing_index,
        per_server_trust_evaluator=per_server_trust_evaluator,
        mcp_namespace_emitter=mcp_namespace_emitter,
        trust_policy=trust_policy,
        sandbox_decision_resolvers=sandbox_decision_resolvers,
        tool_execution_drivers=tool_execution_drivers,
        tracer_provider=ctx.tracer_provider,
        cost_chain=ctx.cost_chain,
        audit_writer=ctx.audit_writer,
        rate_table=rate_table,
        # R-FS-1 arc CA + B-INTERSTEP-PERRUN-ISOLATION — thread the run-scoped
        # accumulator PROXY (not its `.records` list — that capture defeated per-run
        # isolation) so per-tool-dispatch SpanCostRecords `append` through to the
        # current run's accumulator → `RunResult.cost_attribution` (runtime spec
        # v1.53 §9 C-RT-09).
        cost_record_sink=ctx.cost_record_accumulator,
        provider_secret_resolver=ctx.keyring_resolver,
        secret_fetch_audit_emitter=RuntimeAsIsWiring(
            ctx.ledger_writer,
            procedural_tier_snapshot_resolver=ctx.procedural_tier_snapshot_resolver,
        ).emit_secret_fetch_audit_entry,
        secret_fetch_backend=config.provider_secrets.backend.value,
        effect_fence=effect_fence,
        effect_fencing_explicit=config.effect_fencing,
    )

    # --- Step 4: RetryBreakerToolDispatcher (C-RT-21 §14.11) -----------------
    wrapper = RetryBreakerToolDispatcher(
        inner=bare_dispatcher,
        retry_breaker=ctx.retry_breaker,
        tracer_provider=ctx.tracer_provider,
    )

    # --- Step 5: return wrapper (caller binds to ctx.tool_dispatcher) --------
    return wrapper
