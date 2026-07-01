"""5-axis gate-level multiplicative tunable composition — U-AS-14.

Implements C-AS-12 §12.1 (5-axis multiplicative tunable), §12.2 reference,
§12.5 (multiplicative discipline). Declares `GateLevel`,
`GateLevelFloorInterfaces`, `tier_to_gate_level_floor`, and the `gate_level`
composition function.

Authority: Implementation_Plan_Action_Surface_v1_1.md §5.3 U-AS-14 (R3-revised
body — `[U-AS-08]` edge added so `MCPServer` / `CallSiteContext` are in-cone;
v1 base body at Implementation_Plan_Action_Surface_v1.md §2 U-AS-14);
Spec_Action_Surface_v1.md §12 C-AS-12; ADR-D2 v1.2 §1.5.

Depends on: U-AS-01 (`SandboxTier`); U-AS-04 (`PersonaTier`); U-AS-05; U-AS-06
(`ToolMetadata`, `MCPServer`, `sandbox_tier_floor`); U-AS-08 (`CallSiteContext`).

Two execution-grade resolutions (documented discretion, not a fork):

  (1) `tier_to_gate_level_floor`'s SandboxTier → GateLevel mapping is not given
      by the spec — §12.1 writes the fifth `max()` axis as `sandbox_tier_floor`
      (a `SandboxTier`), and the plan introduces `tier_to_gate_level_floor` as
      the adapter onto `GateLevel` without a table. A monotone mapping is
      chosen: `tier-1`/`tier-2` → AUTO, `tier-3` → ASK, `tier-4` → DENY (and a
      `REFUSE` floor → DENY). It satisfies §12.5 (higher tier → higher-or-equal
      gate); the AUTO/ASK/DENY boundary is a documented Class 3 discretion —
      revisit if a different security-posture boundary is wanted.
  (2) The §12.1 fifth axis calls `sandbox_tier_floor`, which takes a
      `ToolMetadata` (U-AS-06); `gate_level`'s signature carries both the
      `ToolContract` and a `tool_metadata: ToolMetadata` — implementation-grade
      parameter threading, as at U-AS-08.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from harness_as.discriminators import PersonaTier
from harness_as.sandbox_tier import SandboxTier
from harness_as.sandbox_tier_composition import CallSiteContext
from harness_as.sandbox_tier_floor import (
    MCPServer,
    SandboxTierFloorOutcome,
    ToolMetadata,
    sandbox_tier_floor,
)
from harness_as.tool_contract import ToolContract


class GateLevel(StrEnum):
    """The 3-valued tool-invocation gate level (C-AS-12 §12.1).

    Ordering AUTO < ASK < DENY by definition order; the higher gate wins.
    """

    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"


_GATE_RANK: dict[GateLevel, int] = {g: rank for rank, g in enumerate(GateLevel)}


class GateLevelFloorInterfaces(Protocol):
    """Forward-declared gate-level floor interfaces (C-AS-12 §12.1).

    The first four `max()` axes; implementations injected by the CP plan
    (Session 3). `gate_level` is pure given this interface (acceptance #9).
    """

    def per_tool_gate_level(self, tool: ToolContract) -> GateLevel: ...

    def blast_radius_gate_floor(self, tool: ToolContract) -> GateLevel: ...

    def per_mcp_server_trust_floor(self, mcp_server: MCPServer | None) -> GateLevel: ...

    def persona_tier_floor(self, persona_tier: PersonaTier) -> GateLevel: ...


# tier_to_gate_level_floor mapping — monotone; see module docstring resolution (1).
_TIER_TO_GATE: dict[SandboxTier, GateLevel] = {
    SandboxTier.TIER_1_PROCESS: GateLevel.AUTO,
    SandboxTier.TIER_2_CONTAINER: GateLevel.AUTO,
    SandboxTier.TIER_3_MICROVM: GateLevel.ASK,
    SandboxTier.TIER_4_FULL_VM: GateLevel.DENY,
}


def tier_to_gate_level_floor(tier: SandboxTier) -> GateLevel:
    """Map a resolved sandbox tier to its gate-level floor (C-AS-12 §12.1).

    Monotone: a higher sandbox tier yields a higher-or-equal gate level. The
    fifth `max()` axis of the §12.1 composition enters via this adapter.
    """
    return _TIER_TO_GATE[tier]


def gate_level(
    tool: ToolContract,
    tool_metadata: ToolMetadata,
    ctx: CallSiteContext,
    floors: GateLevelFloorInterfaces,
) -> GateLevel:
    """Resolve the tool-invocation gate level (C-AS-12 §12.1 five-axis `max()`).

    `max(per_tool_gate_level, blast_radius_gate_floor, per_mcp_server_trust_floor,
    persona_tier_floor, tier_to_gate_level_floor(sandbox_tier_floor(...)))`.
    The highest gate level wins (acceptance #3); every floor expresses its
    concern, none is suppressed (acceptance #4 / §12.5). Pure given `floors`
    (acceptance #9). A `REFUSE` from `sandbox_tier_floor` maps to `DENY`.
    """
    stf = sandbox_tier_floor(
        tool_metadata,
        ctx.deployment_surface,
        ctx.blast_radius_tier,
        ctx.mcp_transport,
        ctx.mcp_server,
    )
    if stf.outcome is SandboxTierFloorOutcome.REFUSE:
        sandbox_axis = GateLevel.DENY
    else:
        assert stf.tier is not None  # a RESOLVED outcome carries a tier
        sandbox_axis = tier_to_gate_level_floor(stf.tier)
    axes = (
        floors.per_tool_gate_level(tool),
        floors.blast_radius_gate_floor(tool),
        floors.per_mcp_server_trust_floor(ctx.mcp_server),
        floors.persona_tier_floor(ctx.persona_tier),
        sandbox_axis,
    )
    return max(axes, key=lambda g: _GATE_RANK[g])
