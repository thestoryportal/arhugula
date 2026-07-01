"""Tests for U-AS-13 — per-MCP-transport sandbox-tier floor (C-AS-10 §10)."""

from __future__ import annotations

from harness_as.discriminators import MCPTransport
from harness_as.mcp_transport_floor import mcp_transport_floor, rejects_at_registration
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel, SandboxTierFloorOutcome

_L1 = MCPServerTrustLevel.L1_SIGNED_PINNED


def test_mcp_transport_floor_stdio_with_read_only_returns_tier_3() -> None:
    """§10.1 — STDIO + read-only → max(tier-3, tier-1) = tier-3."""
    result = mcp_transport_floor(MCPTransport.STDIO, _L1, BlastRadiusTier.READ_ONLY)
    assert result.tier is SandboxTier.TIER_3_MICROVM


def test_mcp_transport_floor_stdio_with_external_irreversible_returns_tier_4() -> None:
    """§10.1 — STDIO + external-irreversible → max(tier-3, tier-4) = tier-4."""
    result = mcp_transport_floor(MCPTransport.STDIO, _L1, BlastRadiusTier.EXTERNAL_IRREVERSIBLE)
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_mcp_transport_floor_l0_returns_refuse() -> None:
    """§10.1 — remote trust level 0 → REFUSE."""
    result = mcp_transport_floor(
        MCPTransport.STREAMABLE_HTTP_L0_REFUSE,
        MCPServerTrustLevel.L0_REFUSE_REMOTE,
        BlastRadiusTier.READ_ONLY,
    )
    assert result.outcome is SandboxTierFloorOutcome.REFUSE


def test_mcp_transport_floor_l1_returns_blast_radius_floor() -> None:
    """§10.1 — remote trust level 1 → blast_radius_floor."""
    result = mcp_transport_floor(
        MCPTransport.STREAMABLE_HTTP_L1_PINNED, _L1, BlastRadiusTier.LOCAL_MUTATION
    )
    assert result.tier is SandboxTier.TIER_2_CONTAINER


def test_mcp_transport_floor_l2_returns_tier_4() -> None:
    """§10.1 — remote trust level 2 → max(tier-4, blast_radius_floor) = tier-4."""
    result = mcp_transport_floor(
        MCPTransport.STREAMABLE_HTTP_L2_SANDBOX,
        MCPServerTrustLevel.L2_SANDBOX_ALL,
        BlastRadiusTier.READ_ONLY,
    )
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_mcp_transport_floor_l3_returns_blast_radius_floor_with_audit_marker() -> None:
    """§10.1 — remote trust level 3 → blast_radius_floor."""
    result = mcp_transport_floor(
        MCPTransport.STREAMABLE_HTTP_L3_AUDIT,
        MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT,
        BlastRadiusTier.EXTERNAL_REVERSIBLE,
    )
    assert result.tier is SandboxTier.TIER_3_MICROVM


def test_rejects_at_registration_only_for_l0() -> None:
    """§10.1 — rejects_at_registration is true exactly for the trust-level-0 case."""
    assert rejects_at_registration(
        MCPTransport.STREAMABLE_HTTP_L0_REFUSE, MCPServerTrustLevel.L0_REFUSE_REMOTE
    )
    assert not rejects_at_registration(MCPTransport.STDIO, _L1)
    assert not rejects_at_registration(
        MCPTransport.STREAMABLE_HTTP_L2_SANDBOX, MCPServerTrustLevel.L2_SANDBOX_ALL
    )


def test_mcp_floor_alignment_with_u_as_06_sandbox_tier_floor() -> None:
    """AC5 — this table is semantically aligned with U-AS-06 §2.3 rows 3-6."""
    from harness_as.sandbox_tier_floor import (
        MCPServer,
        ToolMetadata,
        sandbox_tier_floor,
    )

    plain = ToolMetadata(
        is_deterministic_inhouse=False,
        forces_computer_use=False,
        forces_code_execution=False,
    )
    from harness_as.discriminators import DeploymentSurface

    surface = DeploymentSurface.LOCAL_DEVELOPMENT
    # STDIO row alignment.
    via_floor = sandbox_tier_floor(
        plain, surface, BlastRadiusTier.READ_ONLY, MCPTransport.STDIO, None
    )
    via_transport = mcp_transport_floor(MCPTransport.STDIO, _L1, BlastRadiusTier.READ_ONLY)
    assert via_floor.tier is via_transport.tier
    # Remote L2 row alignment.
    server = MCPServer(server_id="s", trust_level=MCPServerTrustLevel.L2_SANDBOX_ALL)
    via_floor_l2 = sandbox_tier_floor(
        plain,
        surface,
        BlastRadiusTier.READ_ONLY,
        MCPTransport.STREAMABLE_HTTP_L2_SANDBOX,
        server,
    )
    via_transport_l2 = mcp_transport_floor(
        MCPTransport.STREAMABLE_HTTP_L2_SANDBOX,
        MCPServerTrustLevel.L2_SANDBOX_ALL,
        BlastRadiusTier.READ_ONLY,
    )
    assert via_floor_l2.tier is via_transport_l2.tier
