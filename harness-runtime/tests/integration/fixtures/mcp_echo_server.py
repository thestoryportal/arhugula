"""Fixture MCP server for U-RT-86 e2e test — stdio transport, one echo tool.

Runs a FastMCP server over stdio with a single deterministic `echo` tool.
The U-RT-86 e2e test spawns this script as a subprocess via the production
factory's stdio_client invocation chain (operator-supplied
`MCPClientConfig.connection_url='stdio://<python> <this_script>'`).

Per workspace `CLAUDE.md` §3.1 stack commitment, MCP host + client use
`modelcontextprotocol/python-sdk` (FastMCP). The fixture imports FastMCP
from the same package the production code uses; no shim.

Invocation: `python -m harness_runtime.tests.integration.fixtures.mcp_echo_server`
OR `python <abs path to this file>`. Both forms work because the module
declares an entry-point at `__main__` calling `mcp.run()`.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp: Any = FastMCP("u-rt-86-fixture-echo-server")


@mcp.tool()
def echo(value: str) -> str:
    """Return the input value verbatim. Deterministic — no LLM in the loop."""
    return value


if __name__ == "__main__":
    mcp.run()
