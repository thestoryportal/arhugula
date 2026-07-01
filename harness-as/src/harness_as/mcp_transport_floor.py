"""Per-MCP-transport sandbox-tier floor lookup — U-AS-13.

Implements C-AS-10 §10.1 (per-MCP-transport floor lookup table), §10.2
(composition with C-AS-02), §10.3 (MCP server trust-tier framework — names).
Declares `mcp_transport_floor` and `rejects_at_registration`.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-13 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §10 C-AS-10; ADR-D2 v1.2 §1.3.

Depends on: U-AS-01 (`SandboxTier`, `BlastRadiusTier`); U-AS-04 (`MCPTransport`);
U-AS-05 (`blast_radius_floor`); U-AS-06 (`MCPServerTrustLevel`,
`SandboxTierFloorResult`, the `REFUSE` sentinel).

The v1 body's `trust_level: MCPTrustLevel` parameter type is `MCPServerTrustLevel`
(U-AS-06) — the v1.2 C-AS-02 reconciliation removed the standalone `MCPTrustLevel`
enum; the four-valued trust level is `MCPServerTrustLevel`. This unit's table is
the §10.1 surface that U-AS-06's §2.3 rows 3-6 align with (AC5).
"""

from __future__ import annotations

from harness_as.blast_radius_floor import blast_radius_floor
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import (
    REFUSE,
    MCPServerTrustLevel,
    SandboxTierFloorOutcome,
    SandboxTierFloorResult,
)

_TIER_RANK: dict[SandboxTier, int] = {tier: rank for rank, tier in enumerate(SandboxTier)}


def _tier_max(a: SandboxTier, b: SandboxTier) -> SandboxTier:
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def _resolved(tier: SandboxTier) -> SandboxTierFloorResult:
    return SandboxTierFloorResult(outcome=SandboxTierFloorOutcome.RESOLVED, tier=tier)


def mcp_transport_floor(
    transport: MCPTransport,
    trust_level: MCPServerTrustLevel,
    blast_radius: BlastRadiusTier,
) -> SandboxTierFloorResult:
    """Resolve the per-MCP-transport sandbox-tier floor (C-AS-10 §10.1).

    The §10.1 five-row table: STDIO → `max(tier-3, blast_radius_floor)`; remote
    trust level 0 → `REFUSE`; level 1 → `blast_radius_floor`; level 2 →
    `max(tier-4, blast_radius_floor)`; level 3 → `blast_radius_floor` (with a
    downstream audit-ledger entry, §10.1 row 5). STDIO is keyed on `transport`;
    the remote rows are keyed on `trust_level`. Semantically aligned with the
    U-AS-06 `sandbox_tier_floor` §2.3 rows 3-6 (AC5).
    """
    floor = blast_radius_floor(blast_radius)
    if transport is MCPTransport.STDIO:
        return _resolved(_tier_max(SandboxTier.TIER_3_MICROVM, floor))
    match trust_level:
        case MCPServerTrustLevel.L0_REFUSE_REMOTE:
            return REFUSE
        case MCPServerTrustLevel.L2_SANDBOX_ALL:
            return _resolved(_tier_max(SandboxTier.TIER_4_FULL_VM, floor))
        case MCPServerTrustLevel.L1_SIGNED_PINNED | MCPServerTrustLevel.L3_ALLOW_WITH_AUDIT:
            return _resolved(floor)


def rejects_at_registration(transport: MCPTransport, trust_level: MCPServerTrustLevel) -> bool:
    """True exactly when `mcp_transport_floor` returns `REFUSE` — the remote-MCP
    trust-level-0 case; the harness rejects the connection at registration
    (C-AS-10 §10.1 row 2 / §10.2)."""
    result = mcp_transport_floor(transport, trust_level, BlastRadiusTier.READ_ONLY)
    return result.outcome is SandboxTierFloorOutcome.REFUSE
