"""R-410 — real TIER_2 container tool execution driver e2e.

This test intentionally uses a local Docker image only. It never pulls from the
network; if Docker or ``python:3.11-slim`` is unavailable, the test skips at the
operator/infra boundary.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

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
from harness_runtime.lifecycle.docker_tool_execution_driver import (
    DockerToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

_IMAGE = "python:3.11-slim"
_RUNNER = """
import json
import os
import socket
import sys

payload = json.load(sys.stdin)
message = payload["tool_args"]["message"]
host_probe_path = payload["tool_args"].get("host_probe_path", "")

try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.25).close()
    network_blocked = False
except OSError:
    network_blocked = True

json.dump(
    {
        "content": [{"type": "text", "text": f"container:{message}"}],
        "isError": False,
        "structuredContent": {
            "tool_id": payload["tool_id"],
            "idempotency_key": payload["idempotency_key"],
            "sandbox_tier": payload["sandbox"]["tier"],
            "host_path_visible": bool(host_probe_path and os.path.exists(host_probe_path)),
            "network_blocked": network_blocked,
        },
    },
    sys.stdout,
)
"""


def _require_local_docker_image() -> None:
    proc = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    image_available = any(line.partition(" ")[0] == _IMAGE for line in proc.stdout.splitlines())
    if proc.returncode != 0 or not image_available:
        pytest.skip(
            f"Docker image {_IMAGE!r} unavailable locally or Docker is not reachable; "
            "R-410 e2e does not pull images"
        )


def _server() -> FastMCP:
    server = FastMCP(name="r410-container-host-registry")

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
        minimum_tier=SandboxTier.TIER_2_CONTAINER,
        blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
    )


async def _started_host() -> MCPClientHost:
    host = MCPClientHost(
        transport="stdio",
        server_name="r410-container-host-registry",
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
        allow_list=frozenset({"r410-container-host-registry"}),
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
        tier=SandboxTier.TIER_2_CONTAINER,
        tech="docker",
        provider="local-docker",
        assigned_tier_reason="r410-local-container-e2e",
        cost_tier_overhead_ms=0,
    )


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-r410",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-r410",
        parent_action_id="workflow:wf-r410:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="r410-parent-idem",
        tenant_id=None,
        step_index=0,
    )


def _step(message: str) -> WorkflowStep:
    return WorkflowStep(
        step_id="step-r410",
        step_kind=StepKind.TOOL_STEP,
        step_payload={
            "tool_id": "echo",
            "tool_args": {
                "message": message,
                "host_probe_path": str(Path.cwd()),
            },
        },
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_r410_tier2_tool_step_executes_inside_local_docker_container() -> None:
    _require_local_docker_image()
    host = await _started_host()
    driver = DockerToolRunnerExecutionDriver(
        image=_IMAGE,
        command=("python", "-c", _RUNNER),
        timeout_seconds=20,
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
            _step("hello-r410"),
            step_context=_step_context(),
        )
    finally:
        await host.shutdown()

    assert result["sandbox_tier"] == "tier-2-container"
    response = result["response"]
    assert response["content"][0]["text"] == "container:hello-r410"
    assert response["structuredContent"]["sandbox_tier"] == "tier-2-container"
    assert response["structuredContent"]["network_blocked"] is True
    assert response["structuredContent"]["host_path_visible"] is False
