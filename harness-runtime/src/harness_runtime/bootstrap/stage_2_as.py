"""Stage 2 AS — skills load, tool registry, MCP host, sandbox dispatch.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 stage 2 post-conditions:
`ctx.skills`, `ctx.tool_contracts`, `ctx.mcp_host`, `ctx.mcp_clients`,
`ctx.sandbox_dispatch` all non-None; MCP clients in READY state.

Composer call sequence:
1. `load_skills_from_dir(path_resolver.resolve(PathClass.SKILLS, ...))`
2. `materialize_tool_registry(skills)` → ToolRegistry (a dict-mapping handle).
3. `materialize_mcp_stage(config.mcp_clients)` → MCPHost + dict[ClientName, MCPClient].
4. `materialize_sandbox_dispatch()` → SandboxDispatchTable.
"""

from __future__ import annotations

from harness_core.workload_class import WorkloadClass
from harness_is.path_class_registry import PathClass

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.mcp_host import materialize_mcp_stage
from harness_runtime.lifecycle.mcp_server import materialize_mcp_server_stage
from harness_runtime.lifecycle.sandbox_dispatch import materialize_sandbox_dispatch
from harness_runtime.lifecycle.skills import load_skills_from_dir
from harness_runtime.lifecycle.tool_registry import materialize_tool_registry
from harness_runtime.types import RuntimeConfig

__all__ = ["execute"]


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate stage 2 AS fields on `ctx`."""
    assert ctx.path_resolver is not None, "stage 1 IS must precede stage 2 AS"

    # 1. Skills filesystem load. PathResolver returns the SKILLS-class path
    # for the runtime's (workload_class, deployment_surface) cell.
    skills_dir = ctx.path_resolver.resolve_path(
        PathClass.SKILLS,
        workload_class,
        config.deployment_surface,
    )
    ctx.skills = load_skills_from_dir(skills_dir)

    # 2. Tool contract registry from loaded skills.
    tool_registry = materialize_tool_registry(ctx.skills)
    # ToolRegistry concretely exposes `.contracts: dict[ToolName, ToolContract]`;
    # narrow to the dict for HarnessContext compatibility.
    contracts = getattr(tool_registry, "contracts", None)
    ctx.tool_contracts = dict(contracts) if contracts is not None else {}

    # 3. MCP host + clients (U-RT-15 — H_T-as-MCP-client surface).
    mcp_stage = materialize_mcp_stage(tuple(config.mcp_clients))
    ctx.mcp_host = mcp_stage.host
    ctx.mcp_clients = dict(mcp_stage.clients)

    # 3-bis. MCP server hosting (U-RT-62 — H_T-as-MCP-server surface per
    # spec v1.12 §14.8.3 v1.12 workflow-initiation topology pin). Sibling
    # to step 3 per Q4 sibling-primitive ratification — the two MCP roles
    # are orthogonal. FastMCP server constructed + `run_workflow` tool
    # registered; the tool's closure references the mutable holders carried
    # on the returned `HarnessMCPServer` so post-bootstrap `api.run()` can
    # populate `workflow_registry` + `_state['_harness_ctx']` per AC #5.
    ctx.mcp_server = materialize_mcp_server_stage(
        drain_timeout_seconds=config.drain_timeout_seconds,
    )

    # 4. Sandbox dispatch table.
    ctx.sandbox_dispatch = materialize_sandbox_dispatch()
