"""R-412 — real TIER_4 managed full-VM TOOL_STEP execution through E2B.

This test is intentionally excluded from CI's default non-e2e lane. It creates
one usage-billed E2B sandbox and runs a deterministic shell runner with outbound
internet disabled at sandbox creation.
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import asynccontextmanager

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
from harness_cp.per_server_trust_types import (
    TierDerivationRule,
    TrustPolicy,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.e2b_tool_execution_driver import (
    E2BManagedFullVMToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

_RUNNER = r"""
payload="$(cat)"
case "$payload" in
  *'"tier": "tier-4-full-vm"'*) payload_carried_tier=true ;;
  *) payload_carried_tier=false ;;
esac
printf '{"content":[{"type":"text","text":"e2b:hello-r412"}],"isError":false,"structuredContent":{"sandbox_tier":"tier-4-full-vm","payload_carried_tier":%s}}' "$payload_carried_tier"
"""


def _require_e2b_key() -> None:
    if not os.environ.get("E2B_API_KEY"):
        pytest.skip("E2B_API_KEY is not set; R-412 E2B live e2e cannot run")
    if importlib.util.find_spec("e2b") is None:
        pytest.skip(
            "Python module 'e2b' is not installed; run via `just r412-e2b-full-vm-live-e2e`"
        )


def _server() -> FastMCP:
    server = FastMCP(name="r412-e2b-host-registry")

    @server.tool(description="host echo")
    def echo(message: str) -> str:
        return f"host:{message}"

    return server


def _session_factory(server: FastMCP):
    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as session:
            yield session

    return factory


def _converter(tool) -> ToolContract:
    return ToolContract(
        name=tool.name,
        description=tool.description or "",
        input_schema=tool.inputSchema or {"type": "object"},
        output_schema={},
        minimum_tier=SandboxTier.TIER_4_FULL_VM,
        blast_radius_tier=BlastRadiusTier.EXTERNAL_IRREVERSIBLE,
    )


async def _started_host() -> MCPClientHost:
    host = MCPClientHost(
        transport="stdio",
        server_name="r412-e2b-host-registry",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_converter,
        session_context_factory=_session_factory(_server()),
    )
    await host.start()
    return host


def _trust_policy() -> TrustPolicy:
    return TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        allow_list=frozenset({"r412-e2b-host-registry"}),
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


def _sandbox_resolver(_contract: ToolContract, _step: WorkflowStep) -> SandboxDispatchDecision:
    return SandboxDispatchDecision(
        tier=SandboxTier.TIER_4_FULL_VM,
        tech="e2b-firecracker",
        provider="e2b-managed",
        assigned_tier_reason="r412-managed-full-vm-e2e",
        cost_tier_overhead_ms=150,
    )


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-r412",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-r412",
        parent_action_id="workflow:wf-r412:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_3_MICROVM,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="r412-parent-idem",
        tenant_id=None,
        step_index=0,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id="step-r412",
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": "echo", "tool_args": {"message": "hello-r412"}},
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_r412_tier4_tool_step_executes_in_e2b_managed_full_vm() -> None:
    _require_e2b_key()
    host = await _started_host()
    driver = E2BManagedFullVMToolRunnerExecutionDriver(
        command=("sh", "-c", _RUNNER),
        timeout_seconds=20,
        sandbox_timeout_seconds=60,
        allow_internet_access=False,
    )
    dispatcher = RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_emitter(),
        trust_policy=_trust_policy(),
        sandbox_decision_resolver=_sandbox_resolver,
        tool_execution_driver=driver,
    )
    try:
        result = await dispatcher.dispatch(
            _binding(),
            _step(),
            step_context=_step_context(),
        )
    finally:
        await host.shutdown()

    assert result["sandbox_tier"] == "tier-4-full-vm"
    response = result["response"]
    assert response["content"][0]["text"] == "e2b:hello-r412"
    assert response["structuredContent"] == {
        "sandbox_tier": "tier-4-full-vm",
        "payload_carried_tier": True,
    }
