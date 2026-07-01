"""U-RT-127 / U-RT-128 — multi-server tool→server routing e2e (spec §14.9.10 D2).

The load-bearing proof of the B2-impl-2b multi-server reshape: every other
dispatcher test is single-host and passes vacuously for routing. This exercises
≥2 hosts (each owning a DISTINCT tool) through the real production carriers —
the routing index, collision detection, dispatch resolution, per-host sandbox,
and blast-radius search:

- **route-to-each-host + per-host-sandbox-distinct** — a `TOOL_STEP` for host A's
  tool dispatches to host A under host A's sandbox tier; host B's tool → host B
  under host B's (different) tier. Proven via a recording driver capturing
  `(server_name, sandbox_decision.tier)` per dispatch.
- **collision → fail-loud abort** — two hosts advertising the SAME tool raise
  `RT-FAIL-MCP-TOOL-NAME-COLLISION` at `build_tool_routing_index` (bootstrap).
- **unknown tool → RT-FAIL-TOOL-CONTRACT-UNKNOWN** — a tool in no host's registry
  is absent from the routing index → dispatch step 0 raises.
- **blast-radius searches all hosts** — `resolve_step_blast_radius` for host B's
  tool finds host B's `blast_radius_tier` (not host A's), proving the
  search-all-registries reshape (the dispatcher-held index isn't reachable from
  `ctx`; the collision guarantee makes first-match unambiguous).

Hosts are mock-STARTED with a populated registry (no real MCP I/O) — these tests
prove the routing/dispatch/collision LOGIC, not transport. The real-transport
single-host path is covered by `test_u_rt_86_mcp_client_external_server_e2e`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.tool_contract import ToolContract
from harness_core import PersonaTier
from harness_cp.cp_shared_types import MCPTrustTier, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.mcp_client_namespace_emitter import (
    MCPClientNamespaceEmitter,
    MCPServerInfo,
)
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.per_server_trust_types import TierDerivationRule, TrustPolicy
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.bootstrap.factories.runtime_tool_dispatcher_factory import (
    build_tool_routing_index,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    MCPToolNameCollisionError,
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
    ToolContractUnknownError,
)
from harness_runtime.lifecycle.step_blast_radius import resolve_step_blast_radius
from harness_runtime.lifecycle.tool_registry import ToolRegistry
from harness_runtime.types import ServerName, ToolName

# ---------- mock STARTED hosts (populated registry, no real MCP I/O) --------


def _mock_host(*, server_name: str, tool_name: str, blast_radius: BlastRadiusTier) -> MCPClientHost:
    """A mock STARTED `MCPClientHost` whose registry holds a single tool. The
    `_started`/`_tool_registry` privates are forced so `tool_registry` is
    readable without a real `start()` (no subprocess / FastMCP session)."""
    registry = ToolRegistry()
    registry.register(
        ToolContract(
            name=tool_name,
            description=f"{tool_name} on {server_name}",
            input_schema={"type": "object"},
            output_schema={"type": "object"},  # empty → no response validation
            minimum_tier=SandboxTier.TIER_1_PROCESS,
            blast_radius_tier=blast_radius,
        )
    )
    host = MCPClientHost(
        transport="stdio",
        server_name=server_name,
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
    )
    host._started = True  # type: ignore[attr-defined]
    host._tool_registry = registry  # type: ignore[attr-defined]
    return host


# ---------- dispatch carriers ----------------------------------------------


def _trust_policy(*server_names: str) -> TrustPolicy:
    return TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        allow_list=frozenset(server_names),
        deny_list=frozenset(),
        per_server_overrides={},
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )


def _emitter() -> MCPClientNamespaceEmitter:
    def lookup(_server_name: str) -> MCPServerInfo:
        return MCPServerInfo(
            transport="stdio",
            protocol_version="2025-06-18",
            auth_present=False,
            trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        )

    return MCPClientNamespaceEmitter(info_lookup=lookup)


def _tier_resolver(tier: SandboxTier):
    def resolve(_contract: ToolContract, _step: WorkflowStep) -> SandboxDispatchDecision:
        return SandboxDispatchDecision(
            tier=tier,
            tech="test",
            provider="test",
            assigned_tier_reason=f"per-host-{tier.value}",
            cost_tier_overhead_ms=0,
        )

    return resolve


class _RecordingDriver:
    """Captures `(server_name, resolved tier)` per dispatch (proving routing +
    per-host sandbox) and returns an empty result — no real tool I/O."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, SandboxTier]] = []

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Any,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.calls.append((mcp_client_host.server_name, sandbox_decision.tier))
        return {}


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-1",
        parent_action_id="workflow:wf-1:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="run-idem-key-abc",
        tenant_id=None,
        step_index=0,
    )


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-1",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step(tool_id: str) -> WorkflowStep:
    return WorkflowStep(
        step_id="step-1",
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": tool_id, "tool_args": {"text": "hi"}},
    )


def _dispatcher(
    hosts: dict[ServerName, MCPClientHost],
    *,
    resolvers: dict[ServerName, Any],
    drivers: dict[ServerName, Any],
) -> RuntimeToolDispatcher:
    return RuntimeToolDispatcher(
        mcp_client_hosts=hosts,
        routing_index=build_tool_routing_index(hosts),
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_emitter(),
        trust_policy=_trust_policy(*[str(s) for s in hosts]),
        sandbox_decision_resolvers=resolvers,
        tool_execution_drivers=drivers,
    )


# ---------- tests ----------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_each_tool_to_its_owning_host_under_per_host_sandbox() -> None:
    """AC — a TOOL_STEP for host A's tool dispatches to host A under host A's tier;
    host B's tool → host B under host B's (different) tier (route-to-each-host +
    per-host-sandbox-distinct in ONE proof via the recording driver)."""
    host_a = _mock_host(
        server_name="server-a", tool_name="alpha", blast_radius=BlastRadiusTier.READ_ONLY
    )
    host_b = _mock_host(
        server_name="server-b", tool_name="beta", blast_radius=BlastRadiusTier.READ_ONLY
    )
    hosts = {ServerName("server-a"): host_a, ServerName("server-b"): host_b}

    assert build_tool_routing_index(hosts) == {
        ToolName("alpha"): ServerName("server-a"),
        ToolName("beta"): ServerName("server-b"),
    }

    recorder = _RecordingDriver()
    dispatcher = _dispatcher(
        hosts,
        resolvers={
            ServerName("server-a"): _tier_resolver(SandboxTier.TIER_1_PROCESS),
            ServerName("server-b"): _tier_resolver(SandboxTier.TIER_2_CONTAINER),
        },
        drivers={ServerName("server-a"): recorder, ServerName("server-b"): recorder},
    )

    await dispatcher.dispatch(_binding(), _step("alpha"), step_context=_step_context())
    await dispatcher.dispatch(_binding(), _step("beta"), step_context=_step_context())

    # Each tool reached its OWNING host, under that host's OWN sandbox tier.
    assert recorder.calls == [
        ("server-a", SandboxTier.TIER_1_PROCESS),
        ("server-b", SandboxTier.TIER_2_CONTAINER),
    ]


def test_duplicate_tool_across_hosts_fails_loud_at_index_build() -> None:
    """AC — two hosts advertising the SAME tool abort bootstrap with
    RT-FAIL-MCP-TOOL-NAME-COLLISION (a contrasting-baseline: the disjoint case
    above succeeds; this collision case fails-loud)."""
    host_a = _mock_host(
        server_name="server-a", tool_name="dup", blast_radius=BlastRadiusTier.READ_ONLY
    )
    host_b = _mock_host(
        server_name="server-b", tool_name="dup", blast_radius=BlastRadiusTier.READ_ONLY
    )
    hosts = {ServerName("server-a"): host_a, ServerName("server-b"): host_b}
    with pytest.raises(MCPToolNameCollisionError, match="RT-FAIL-MCP-TOOL-NAME-COLLISION"):
        build_tool_routing_index(hosts)


@pytest.mark.asyncio
async def test_unknown_tool_raises_tool_contract_unknown() -> None:
    """AC — a tool_id in no host's registry is absent from the routing index →
    dispatch step 0 raises RT-FAIL-TOOL-CONTRACT-UNKNOWN."""
    host_a = _mock_host(
        server_name="server-a", tool_name="alpha", blast_radius=BlastRadiusTier.READ_ONLY
    )
    hosts = {ServerName("server-a"): host_a}
    dispatcher = _dispatcher(
        hosts,
        resolvers={ServerName("server-a"): _tier_resolver(SandboxTier.TIER_1_PROCESS)},
        drivers={ServerName("server-a"): _RecordingDriver()},
    )
    with pytest.raises(ToolContractUnknownError, match="RT-FAIL-TOOL-CONTRACT-UNKNOWN"):
        await dispatcher.dispatch(_binding(), _step("nonexistent"), step_context=_step_context())


def test_blast_radius_search_finds_host_2_tool() -> None:
    """Advisor point 1 — blast-radius resolution takes `ctx` (not the
    dispatcher), so it searches ALL hosts' registries; host B's tool resolves to
    host B's blast_radius_tier (not host A's), proving search-all + the
    collision-guarantee unambiguity (a sole-host read would miss host 2's tool)."""
    host_a = _mock_host(
        server_name="server-a", tool_name="alpha", blast_radius=BlastRadiusTier.READ_ONLY
    )
    host_b = _mock_host(
        server_name="server-b",
        tool_name="beta",
        blast_radius=BlastRadiusTier.EXTERNAL_IRREVERSIBLE,
    )
    ctx = SimpleNamespace(
        mcp_client_hosts={
            ServerName("server-a"): host_a,
            ServerName("server-b"): host_b,
        },
        tool_contracts=None,
    )
    # host B's tool (the SECOND host) resolves to host B's distinct tier.
    assert (
        resolve_step_blast_radius(_step("beta"), ctx)  # type: ignore[arg-type]
        is BlastRadiusTier.EXTERNAL_IRREVERSIBLE
    )
    assert (
        resolve_step_blast_radius(_step("alpha"), ctx)  # type: ignore[arg-type]
        is BlastRadiusTier.READ_ONLY
    )
