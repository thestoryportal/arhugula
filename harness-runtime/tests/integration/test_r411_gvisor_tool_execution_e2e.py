"""R-411 - real TIER_3 gVisor/runsc TOOL_STEP execution driver e2e.

This test intentionally uses a local Docker image only. It never pulls from the
network; if Docker/runsc or ``alpine:3.20`` is unavailable, the test skips at
the operator/infra boundary. Set ``R411_GVISOR_DOCKER_COMMAND`` to target a
non-default Docker host, for example a Lima VM:

``env LIMA_HOME=/path/to/lima-home limactl shell r411-gvisor sudo docker``
"""

from __future__ import annotations

import shlex
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
    GVisorRunscToolRunnerExecutionDriver,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

_IMAGE = "alpine:3.20"


def _docker_command() -> tuple[str, ...]:
    raw = __import__("os").environ.get("R411_GVISOR_DOCKER_COMMAND", "docker")
    return tuple(shlex.split(raw))


def _runner(host_probe_path: str) -> str:
    quoted_host_probe_path = shlex.quote(host_probe_path)
    return f"""
network_blocked=true
if wget -T 2 -qO- http://example.com >/tmp/r411-net.out 2>/tmp/r411-net.err; then
  network_blocked=false
fi
host_path_visible=false
if test -e {quoted_host_probe_path}; then
  host_path_visible=true
fi
printf '{{"content":[{{"type":"text","text":"gvisor:hello-r411"}}],"isError":false,"structuredContent":{{"sandbox_tier":"tier-3-microvm","network_blocked":%s,"host_path_visible":%s}}}}\\n' "$network_blocked" "$host_path_visible"
"""


def _require_local_gvisor_runtime() -> None:
    docker_command = _docker_command()
    proc = subprocess.run(
        [
            *docker_command,
            "images",
            "--format",
            "{{.Repository}}:{{.Tag}} {{.ID}}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    image_available = any(line.partition(" ")[0] == _IMAGE for line in proc.stdout.splitlines())
    if proc.returncode != 0 or not image_available:
        pytest.skip(
            f"Docker image {_IMAGE!r} unavailable locally or Docker is not reachable; "
            "R-411 e2e does not pull images"
        )

    info = subprocess.run(
        [*docker_command, "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if info.returncode != 0 or '"runsc"' not in info.stdout:
        pytest.skip("Docker runsc runtime is unavailable; R-411 gVisor e2e cannot run")


def _server() -> FastMCP:
    server = FastMCP(name="r411-gvisor-host-registry")

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
        minimum_tier=SandboxTier.TIER_3_MICROVM,
        blast_radius_tier=BlastRadiusTier.EXTERNAL_REVERSIBLE,
    )


async def _started_host() -> MCPClientHost:
    host = MCPClientHost(
        transport="stdio",
        server_name="r411-gvisor-host-registry",
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
        allow_list=frozenset({"r411-gvisor-host-registry"}),
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
        tier=SandboxTier.TIER_3_MICROVM,
        tech="gvisor-runsc",
        provider="local-gvisor",
        assigned_tier_reason="r411-gvisor-live-e2e",
        cost_tier_overhead_ms=0,
    )


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-r411",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-r411",
        parent_action_id="workflow:wf-r411:step:0",
        parent_gate_level=GateLevel.ASK,
        parent_sandbox_tier=SandboxTier.TIER_2_CONTAINER,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        parent_idempotency_key="r411-parent-idem",
        tenant_id=None,
        step_index=0,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id="step-r411",
        step_kind=StepKind.TOOL_STEP,
        step_payload={
            "tool_id": "echo",
            "tool_args": {
                "message": "hello-r411",
                "host_probe_path": str(Path.cwd()),
            },
        },
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_r411_tier3_tool_step_executes_under_gvisor_runsc() -> None:
    _require_local_gvisor_runtime()
    host = await _started_host()
    driver = GVisorRunscToolRunnerExecutionDriver(
        image=_IMAGE,
        command=("sh", "-c", _runner(str(Path.cwd()))),
        docker_command=_docker_command(),
        timeout_seconds=30,
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

    assert result["sandbox_tier"] == "tier-3-microvm"
    response = result["response"]
    assert response["content"][0]["text"] == "gvisor:hello-r411"
    assert response["structuredContent"]["sandbox_tier"] == "tier-3-microvm"
    assert response["structuredContent"]["network_blocked"] is True
    assert response["structuredContent"]["host_path_visible"] is False
