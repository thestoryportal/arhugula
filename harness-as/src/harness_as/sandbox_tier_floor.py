"""`sandbox_tier_floor` lookup table + MCP-server type — U-AS-06.

Implements C-AS-02 §2.3 (the reconciled `sandbox_tier_floor` lookup table).
Declares the `ToolMetadata` discriminator carrier, the `MCPServer` identity /
trust carrier, the `SandboxTierFloorResult` discriminated result, and the
10-row `sandbox_tier_floor` lookup function.

Authority: Implementation_Plan_Action_Surface_v1_2.md §5.2 U-AS-06 (FINALIZED
at R3.1 — Q-R3-1 resolved G-1: 5-arg signature, AS spec v1.2/v1.3 canonical;
`ToolMetadata` carrier declared; `MCPServer` carrier re-homed here per §0.4);
Spec_Action_Surface_v1.md §2.3 C-AS-02; ADR-D2 v1.2 §1.5.1.

Depends on: U-AS-01 (`SandboxTier`, `BlastRadiusTier`); U-AS-04 (`MCPTransport`,
`DeploymentSurface`); U-AS-05 (`blast_radius_floor`).

The 5-arg canonical signature reads the remote-MCP trust level from the
`mcp_server` argument (G-1 explicit-argument resolution); the v1-body standalone
`MCPTrustLevel` scalar is removed — the four-valued trust level is an internal
field of `MCPServer`, typed by `MCPServerTrustLevel`.

`MCPServer` is a `{ ... }` ellipsis in the plan signature; resolved minimally
(no invented structure, X-AL-3 — same pattern as U-IS-01 `ResidenceContract`):
the §2.3 rows 4-6 read only the trust level, so `MCPServer` carries a server
identity + a `MCPServerTrustLevel` (spec §10.3 four-level posture).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.blast_radius_floor import blast_radius_floor
from harness_as.discriminators import DeploymentSurface, MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier

# Tier-monotonic rank — definition order is ascending isolation strength.
_TIER_RANK: dict[SandboxTier, int] = {tier: rank for rank, tier in enumerate(SandboxTier)}


def _tier_max(a: SandboxTier, b: SandboxTier) -> SandboxTier:
    """Return the stronger-isolation tier of two (tier-monotonic `max()`)."""
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


class ToolMetadata(BaseModel):
    """Tool-classification discriminators for the §2.3 lookup (Pattern B carrier).

    Carries the §2.3 row-1 / row-2 / row-7 discriminators.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    is_deterministic_inhouse: bool
    forces_computer_use: bool
    forces_code_execution: bool


class MCPServerTrustLevel(StrEnum):
    """MCP-server trust level — the spec §10.3 four-level posture.

    Internal field type of `MCPServer`; the §2.3 rows 4-6 lookup reads it.
    """

    L0_REFUSE_REMOTE = "L0_REFUSE_REMOTE"
    L1_SIGNED_PINNED = "L1_SIGNED_PINNED"
    L2_SANDBOX_ALL = "L2_SANDBOX_ALL"
    L3_ALLOW_WITH_AUDIT = "L3_ALLOW_WITH_AUDIT"


class MCPServer(BaseModel):
    """MCP-server identity + trust shape (spec §10 MCP transport/trust surface).

    Carrier re-homed to U-AS-06 per AS plan v1.2 §0.4 (U-AS-06 consumes it at
    the `sandbox_tier_floor` signature and is upstream of U-AS-08). Minimal
    resolution: a server identity + the §2.3-rows-4-6 trust-level discriminator.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str
    trust_level: MCPServerTrustLevel


class SandboxTierFloorOutcome(StrEnum):
    """Discriminant of a `sandbox_tier_floor` lookup result (C-AS-02 §2.3)."""

    RESOLVED = "RESOLVED"
    REFUSE = "REFUSE"


class SandboxTierFloorResult(BaseModel):
    """Result of a `sandbox_tier_floor` lookup (C-AS-02 §2.3).

    Discriminated result: `outcome` is `RESOLVED` (with `tier` populated) or
    `REFUSE` (the remote-MCP Level-0 sentinel, `tier` is `None`). The `REFUSE`
    sentinel is structurally distinct from any `SandboxTier` value — it is a
    `SandboxTierFloorResult`, never a tier (acceptance #3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: SandboxTierFloorOutcome
    tier: SandboxTier | None = None


def _resolved(tier: SandboxTier) -> SandboxTierFloorResult:
    return SandboxTierFloorResult(outcome=SandboxTierFloorOutcome.RESOLVED, tier=tier)


#: The remote-MCP Level-0 REFUSE sentinel.
REFUSE: SandboxTierFloorResult = SandboxTierFloorResult(outcome=SandboxTierFloorOutcome.REFUSE)


def sandbox_tier_floor(
    tool: ToolMetadata,
    deployment_surface: DeploymentSurface,
    blast_radius_tier: BlastRadiusTier,
    mcp_transport: MCPTransport | None,
    mcp_server: MCPServer | None,
) -> SandboxTierFloorResult:
    """Resolve the sandbox-tier floor for a tool call site (C-AS-02 §2.3).

    Implements the ten §2.3 rows. Row precedence (acceptance #2): forcing
    conditions → MCP-transport / MCP-server-trust rows → blast-radius default
    rows. Per the §2.3 row→argument keying contract: rows 1-2 key on `tool`,
    row 3 on `mcp_transport`, rows 4-6 on the trust level read from
    `mcp_server`, rows 7-10 on `blast_radius_tier` (via `blast_radius_floor`,
    U-AS-05). `deployment_surface` is a signature argument carried for the
    §2.3 "any deployment surface" row qualifiers.
    """
    # Rows 1-2 — forcing conditions (keyed on `tool`).
    if tool.forces_computer_use:
        return _resolved(SandboxTier.TIER_4_FULL_VM)
    if tool.forces_code_execution:
        return _resolved(SandboxTier.TIER_4_FULL_VM)

    floor = blast_radius_floor(blast_radius_tier)

    # Row 3 — STDIO MCP transport (keyed on `mcp_transport`).
    if mcp_transport is MCPTransport.STDIO:
        return _resolved(_tier_max(SandboxTier.TIER_3_MICROVM, floor))

    # Rows 4-6 — remote MCP (keyed on the trust level read from `mcp_server`).
    if mcp_server is not None:
        match mcp_server.trust_level:
            case MCPServerTrustLevel.L0_REFUSE_REMOTE:
                return REFUSE
            case MCPServerTrustLevel.L2_SANDBOX_ALL:
                return _resolved(_tier_max(SandboxTier.TIER_4_FULL_VM, floor))
            case MCPServerTrustLevel.L1_SIGNED_PINNED | MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT:
                return _resolved(floor)

    # Rows 7-10 — blast-radius default (keyed on `blast_radius_tier`).
    return _resolved(floor)
