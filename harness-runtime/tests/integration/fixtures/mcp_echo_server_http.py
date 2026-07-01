"""Fixture MCP server for the B-MCP-HOST-REMOTE-TRANSPORT e2e — streamable-HTTP transport.

The remote-transport sibling of `mcp_echo_server.py` (stdio). Runs a FastMCP echo
server over **streamable-HTTP** (the granular `streamable_http_l*` transport family,
C-AS-10 §10.1) on a caller-supplied `127.0.0.1:<port>`, serving the MCP endpoint at
`http://127.0.0.1:<port>/mcp` (FastMCP's default mount path).

This exercises the remote-transport host path (runtime spec §14.9.6 inv 5 — "STDIO +
HTTP + SSE all supported at v1") that the `B-MCP-HOST-REMOTE-TRANSPORT` factory fix
unblocks: the stage-3a factory projects the granular `MCPTransport` enum onto the host's
coarse `streamable_http` connection mechanism, then `MCPClientHost.start()` connects via
`mcp.client.streamable_http.streamable_http_client`.

Per workspace `CLAUDE.md` §3.1 stack commitment, MCP host + client use
`modelcontextprotocol/python-sdk` (FastMCP). The fixture imports FastMCP from the same
package the production code uses; no shim.

Invocation: `python <abs path to this file> --port <N>`. The parent test allocates the
free port and polls `http://127.0.0.1:<N>/mcp` for readiness before connecting.
"""

from __future__ import annotations

import argparse
from typing import Any

from mcp.server.fastmcp import FastMCP


def _build_server(port: int) -> Any:
    mcp: Any = FastMCP(
        "b-mcp-remote-transport-fixture-echo-server",
        host="127.0.0.1",
        port=port,
    )

    @mcp.tool()
    def echo(value: str) -> str:
        """Return the input value verbatim. Deterministic — no LLM in the loop."""
        return value

    return mcp


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="streamable-HTTP echo MCP fixture server")
    parser.add_argument("--port", type=int, required=True, help="127.0.0.1 port to bind")
    args = parser.parse_args()
    _build_server(args.port).run(transport="streamable-http")
