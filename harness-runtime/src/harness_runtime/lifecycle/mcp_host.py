"""U-RT-15 — MCP host startup + client connect.

Per `Spec_Harness_Runtime_v1.md` v1.1 §4 (C-RT-04 `mcp_host` + `mcp_clients`)
and Phase 2 Session 3 plan v2.1 §2 L3 U-RT-15.

Class 1 risk-flag absorption (plan §2 L3 U-RT-15):
- Plan flagged: 'AS spec may not pin host startup lifecycle — Class 1 candidate.'
- Pre-flight reading: AS shipped `mcp_transport_floor()` (transport-floor
  invariant check) + `MCPServerTrustLevel` / `MCPTransport` enums. NO
  MCPHost class, NO startup lifecycle.
- Per the L0 Class 2 Tension authorization, the runtime defines `MCPHost`
  and `MCPClient` as runtime composition primitives. Real FastMCP server
  startup is heavyweight; L3 ships the COMPOSITION SURFACE (placeholders
  that satisfy the Protocol shape + pre-validate transport-floor invariants).
  Real server startup binds at a later unit when a workload demands it.

Scope at L3:
- `MCPHost`: frozen placeholder dataclass (no real server started yet).
- `MCPClient`: frozen dataclass wrapping the config + ready state.
- `MCPServerRefusedError`: raised when `mcp_transport_floor` returns
  REFUSE for a configured client (C-AS-10 §10.1 row 2 — remote trust-L0).
- `materialize_mcp_stage(mcp_client_configs)`: pre-validates each client
  against `mcp_transport_floor`; builds the host + a `dict[ClientName,
  MCPClient]` registry of READY clients. Empty input → empty registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_as.mcp_transport_floor import mcp_transport_floor
from harness_as.sandbox_tier_floor import SandboxTierFloorOutcome

from harness_runtime.types import ClientName, MCPClientConfig

__all__ = [
    "MCPClient",
    "MCPHost",
    "MCPServerRefusedError",
    "MCPStage",
    "materialize_mcp_stage",
]


class MCPServerRefusedError(Exception):
    """Raised when `mcp_transport_floor` rejects a configured MCP client.

    Per C-AS-10 §10.1 row 2: remote MCP with `L0_REFUSE_REMOTE` trust →
    `SandboxTierFloorOutcome.REFUSE` → rejected at registration.
    """

    def __init__(self, client_name: ClientName) -> None:
        super().__init__(f"MCP client {client_name!r} rejected at registration (REFUSE)")
        self.client_name = client_name


@dataclass(frozen=True)
class MCPHost:
    """Runtime composition primitive — placeholder for the FastMCP host.

    L3 ships the composition surface; real FastMCP server startup is
    deferred to a later unit when a workload demands it. `started=False`
    until that wiring lands.
    """

    started: bool = False


@dataclass(frozen=True)
class MCPClient:
    """Runtime composition primitive — wraps a configured client + ready state."""

    config: MCPClientConfig
    ready: bool = True
    """Per L3 AC 'configured clients reach READY' — set at materialize
    time after transport-floor invariants clear."""


@dataclass(frozen=True)
class MCPStage:
    """Result of `materialize_mcp_stage`: host + indexed client registry."""

    host: MCPHost
    clients: dict[ClientName, MCPClient]


def materialize_mcp_stage(
    mcp_client_configs: tuple[MCPClientConfig, ...] | list[MCPClientConfig],
) -> MCPStage:
    """Build the MCP stage 2 AS-bootstrap composition.

    Steps per L3 plan AC:
    1. Construct the MCPHost (placeholder at L3).
    2. For each configured client, run `mcp_transport_floor(transport,
       trust_level, blast_radius)`. REFUSE → `MCPServerRefusedError`.
    3. Wrap each cleared client in a READY `MCPClient` and index by name.

    Raises
    ------
    MCPServerRefusedError
        Any configured client's transport-floor lookup returns REFUSE
        (typically remote MCP with `L0_REFUSE_REMOTE` trust level).
    """
    clients: dict[ClientName, MCPClient] = {}
    for config in mcp_client_configs:
        result = mcp_transport_floor(
            transport=config.transport,
            trust_level=config.trust_level,
            blast_radius=config.blast_radius,
        )
        if result.outcome is SandboxTierFloorOutcome.REFUSE:
            raise MCPServerRefusedError(config.client_name)
        clients[config.client_name] = MCPClient(config=config, ready=True)
    return MCPStage(host=MCPHost(started=False), clients=clients)
