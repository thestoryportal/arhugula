"""Tests for U-AS-06 — sandbox_tier_floor lookup table (C-AS-02 §2.3)."""

from __future__ import annotations

import inspect

from harness_as.discriminators import DeploymentSurface, MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import (
    MCPServer,
    MCPServerTrustLevel,
    SandboxTierFloorOutcome,
    ToolMetadata,
    sandbox_tier_floor,
)

_PLAIN = ToolMetadata(
    is_deterministic_inhouse=False,
    forces_computer_use=False,
    forces_code_execution=False,
)
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT


def _server(level: MCPServerTrustLevel) -> MCPServer:
    return MCPServer(server_id="srv", trust_level=level)


def test_sandbox_tier_floor_computer_use_returns_tier_4() -> None:
    """§2.3 row 1 — computer-use forcing → tier-4-full-vm."""
    tool = ToolMetadata(
        is_deterministic_inhouse=False,
        forces_computer_use=True,
        forces_code_execution=False,
    )
    result = sandbox_tier_floor(tool, _SURFACE, BlastRadiusTier.READ_ONLY, None, None)
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_sandbox_tier_floor_code_execution_returns_tier_4() -> None:
    """§2.3 row 2 — code-execution forcing → tier-4-full-vm."""
    tool = ToolMetadata(
        is_deterministic_inhouse=False,
        forces_computer_use=False,
        forces_code_execution=True,
    )
    result = sandbox_tier_floor(tool, _SURFACE, BlastRadiusTier.READ_ONLY, None, None)
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_sandbox_tier_floor_stdio_with_read_only_returns_tier_3() -> None:
    """§2.3 row 3 — STDIO + read-only → max(tier-3, tier-1) = tier-3."""
    result = sandbox_tier_floor(
        _PLAIN, _SURFACE, BlastRadiusTier.READ_ONLY, MCPTransport.STDIO, None
    )
    assert result.tier is SandboxTier.TIER_3_MICROVM


def test_sandbox_tier_floor_stdio_with_external_irreversible_returns_tier_4() -> None:
    """§2.3 row 3 — STDIO + external-irreversible → max(tier-3, tier-4) = tier-4."""
    result = sandbox_tier_floor(
        _PLAIN,
        _SURFACE,
        BlastRadiusTier.EXTERNAL_IRREVERSIBLE,
        MCPTransport.STDIO,
        None,
    )
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_sandbox_tier_floor_remote_l0_returns_refuse() -> None:
    """§2.3 row 4 — remote MCP trust level 0 → REFUSE sentinel."""
    result = sandbox_tier_floor(
        _PLAIN,
        _SURFACE,
        BlastRadiusTier.READ_ONLY,
        MCPTransport.STREAMABLE_HTTP_L0_REFUSE,
        _server(MCPServerTrustLevel.L0_REFUSE_REMOTE),
    )
    assert result.outcome is SandboxTierFloorOutcome.REFUSE
    assert result.tier is None


def test_sandbox_tier_floor_remote_l2_returns_tier_4() -> None:
    """§2.3 row 5 — remote MCP trust level 2 → max(tier-4, blast-radius) = tier-4."""
    result = sandbox_tier_floor(
        _PLAIN,
        _SURFACE,
        BlastRadiusTier.READ_ONLY,
        MCPTransport.STREAMABLE_HTTP_L2_SANDBOX,
        _server(MCPServerTrustLevel.L2_SANDBOX_ALL),
    )
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_sandbox_tier_floor_remote_l1_returns_blast_radius_floor() -> None:
    """§2.3 row 6 — remote MCP trust level 1 → blast_radius_floor."""
    result = sandbox_tier_floor(
        _PLAIN,
        _SURFACE,
        BlastRadiusTier.LOCAL_MUTATION,
        MCPTransport.STREAMABLE_HTTP_L1_PINNED,
        _server(MCPServerTrustLevel.L1_SIGNED_PINNED),
    )
    assert result.tier is SandboxTier.TIER_2_CONTAINER


def test_sandbox_tier_floor_remote_l3_returns_blast_radius_floor() -> None:
    """§2.3 row 6 — remote MCP trust level 3 → blast_radius_floor."""
    result = sandbox_tier_floor(
        _PLAIN,
        _SURFACE,
        BlastRadiusTier.EXTERNAL_REVERSIBLE,
        MCPTransport.STREAMABLE_HTTP_L3_AUDIT,
        _server(MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT),
    )
    assert result.tier is SandboxTier.TIER_3_MICROVM


def test_sandbox_tier_floor_read_only_deterministic_returns_tier_1() -> None:
    """§2.3 row 7 — read-only deterministic in-house → tier-1-process."""
    tool = ToolMetadata(
        is_deterministic_inhouse=True,
        forces_computer_use=False,
        forces_code_execution=False,
    )
    result = sandbox_tier_floor(tool, _SURFACE, BlastRadiusTier.READ_ONLY, None, None)
    assert result.tier is SandboxTier.TIER_1_PROCESS


def test_sandbox_tier_floor_local_mutation_returns_tier_2() -> None:
    """§2.3 row 8 — local-mutation → tier-2-container."""
    result = sandbox_tier_floor(_PLAIN, _SURFACE, BlastRadiusTier.LOCAL_MUTATION, None, None)
    assert result.tier is SandboxTier.TIER_2_CONTAINER


def test_sandbox_tier_floor_external_reversible_returns_tier_3() -> None:
    """§2.3 row 9 — external-reversible → tier-3-microvm."""
    result = sandbox_tier_floor(_PLAIN, _SURFACE, BlastRadiusTier.EXTERNAL_REVERSIBLE, None, None)
    assert result.tier is SandboxTier.TIER_3_MICROVM


def test_sandbox_tier_floor_external_irreversible_returns_tier_4() -> None:
    """§2.3 row 10 — external-irreversible → tier-4-full-vm."""
    result = sandbox_tier_floor(_PLAIN, _SURFACE, BlastRadiusTier.EXTERNAL_IRREVERSIBLE, None, None)
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_sandbox_tier_floor_forcing_precedence_over_blast_radius() -> None:
    """Acceptance #2 — forcing conditions win over the blast-radius default rows."""
    tool = ToolMetadata(
        is_deterministic_inhouse=False,
        forces_computer_use=True,
        forces_code_execution=False,
    )
    # Blast-radius read-only would default to tier-1; forcing lifts it to tier-4.
    result = sandbox_tier_floor(tool, _SURFACE, BlastRadiusTier.READ_ONLY, None, None)
    assert result.tier is SandboxTier.TIER_4_FULL_VM


def test_sandbox_tier_floor_refuse_is_distinct_from_tier_values() -> None:
    """Acceptance #3 — the REFUSE sentinel is not a SandboxTier value."""
    result = sandbox_tier_floor(
        _PLAIN,
        _SURFACE,
        BlastRadiusTier.READ_ONLY,
        MCPTransport.STREAMABLE_HTTP_L0_REFUSE,
        _server(MCPServerTrustLevel.L0_REFUSE_REMOTE),
    )
    assert not isinstance(result, SandboxTier)
    assert result.outcome is SandboxTierFloorOutcome.REFUSE


def test_tool_metadata_record_three_fields() -> None:
    """Acceptance #7 — ToolMetadata carries exactly the three §2.3 discriminators."""
    assert set(ToolMetadata.model_fields) == {
        "is_deterministic_inhouse",
        "forces_computer_use",
        "forces_code_execution",
    }


def test_mcp_server_declared_at_u_as_06() -> None:
    """Acceptance #8 — MCPServer is declared in this unit as the rows-4-6 carrier."""
    server = _server(MCPServerTrustLevel.L1_SIGNED_PINNED)
    assert isinstance(server, MCPServer)
    assert server.trust_level is MCPServerTrustLevel.L1_SIGNED_PINNED


def test_sandbox_tier_floor_signature_is_five_arg() -> None:
    """Acceptance — sandbox_tier_floor is the canonical 5-argument form."""
    params = list(inspect.signature(sandbox_tier_floor).parameters)
    assert params == [
        "tool",
        "deployment_surface",
        "blast_radius_tier",
        "mcp_transport",
        "mcp_server",
    ]
