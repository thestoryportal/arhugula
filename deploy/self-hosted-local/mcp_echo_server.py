"""R-420 local self-hosted echo MCP server.

Runs a single deterministic read-only tool over stdio for the live
SELF_HOSTED_SERVER daemon e2e. This avoids any hosted-provider inference while
still exercising the daemon, MCP host, tool registry, tool dispatcher, and OTLP
export path against the local self-hosted telemetry backend.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp: Any = FastMCP("r420-self-hosted-echo-server")


@mcp.tool()
def echo(value: str) -> str:
    """Return the input value verbatim."""
    return value


if __name__ == "__main__":
    mcp.run()
