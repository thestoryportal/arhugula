"""`sandbox_tier` per-tool max() composition — U-AS-08.

Implements C-AS-02 §2.1 (composition signature), §2.2 (the five-floor `max()`
formula), §2.5 (composition output verification). Declares `TaintState`,
`CallSiteContext`, `FloorInterfaces`, `AssignedTierReason`,
`SandboxTierCompositionResult`, and the `sandbox_tier` composition function.

Authority: Implementation_Plan_Action_Surface_v1_1.md §5.3 U-AS-08 (R3-revised
body — `AssignedTierReason` conformed to the spec §15.2 7-member set; v1 base
body at Implementation_Plan_Action_Surface_v1.md §2 U-AS-08);
Spec_Action_Surface_v1.md §2 C-AS-02; ADR-F4 v1.1 §Decision; ADR-D2 v1.2 §1.5.1.

Depends on: U-AS-01 (`SandboxTier`, `BlastRadiusTier`); U-AS-02 (`ToolContext`,
`forced_tier`, `ForcedTierCause`); U-AS-04 (`DeploymentSurface`,
`MCPTransport`, `PersonaTier`); U-AS-05 (`blast_radius_floor`); U-AS-06
(`ToolMetadata`, `MCPServer`, `sandbox_tier_floor`); U-AS-07 (`ToolContract`).

Four execution-grade resolutions of v1-body / v1.2-propagation gaps (each
traces to an already-settled decision or a spec-deferred surface — documented
implementation discretion, not a fork):

  (1) The v1 `CallSiteContext.mcp_trust_level: Optional<MCPTrustLevel>` field is
      dropped — the v1.2 C-AS-02 reconciliation (operator-ratified G-1) removed
      the `MCPTrustLevel` scalar; trust level travels inside `MCPServer` and
      `mcp_server_trust_tier_floor` reads `mcp_server`. The v1.2 §0.5
      propagation note moved the `MCPServer` carrier but left this vestigial
      field; dropping it completes that reconciliation.
  (2) The §2.2 formula writes `blast_radius_floor(call_site_context.taint_state)`;
      §2.4 (and the landed U-AS-05) key `blast_radius_floor` on `BlastRadiusTier`.
      U-AS-05 landed §2.4 as authoritative — the composition calls
      `blast_radius_floor(ctx.blast_radius_tier)`. `taint_state` is retained on
      `CallSiteContext` as the §2.5-deferred taint surface.
  (3) `TaintState` is referenced but not enumerated by the spec (§2 deferral
      clause); resolved minimally as a closed two-pole enum.
  (4) The §2.2 formula's `sandbox_tier_floor(tool, ...)` first argument is a
      `ToolMetadata` (U-AS-06's signature); `sandbox_tier`'s signature carries
      both the `ToolContract` (for `contract.minimum_tier`) and a
      `tool_metadata: ToolMetadata` — implementation-grade parameter threading.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from harness_as.blast_radius_floor import blast_radius_floor
from harness_as.discriminators import DeploymentSurface, MCPTransport, PersonaTier
from harness_as.forced_tier_resolution import ForcedTierCause, ToolContext, forced_tier
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import (
    MCPServer,
    SandboxTierFloorOutcome,
    ToolMetadata,
    sandbox_tier_floor,
)
from harness_as.tool_contract import ToolContract

# Tier-monotonic rank — definition order is ascending isolation strength.
_TIER_RANK: dict[SandboxTier, int] = {tier: rank for rank, tier in enumerate(SandboxTier)}


class TaintState(StrEnum):
    """Call-site taint state (C-AS-02 §2 deferral clause).

    Minimal resolution of the referenced-but-unenumerated `TaintState` — a
    closed two-pole enum. The taint-state propagation mechanism (per-call
    dataflow analysis / operator-annotated / hybrid) is deferred per §2.5.
    """

    UNTAINTED = "UNTAINTED"
    TAINTED = "TAINTED"


class CallSiteContext(BaseModel):
    """Per-call-site context for `sandbox_tier` composition (C-AS-02 §2.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    taint_state: TaintState
    mcp_server: MCPServer | None
    deployment_surface: DeploymentSurface
    blast_radius_tier: BlastRadiusTier
    mcp_transport: MCPTransport | None
    persona_tier: PersonaTier
    computer_use_bound: bool
    code_execution_beta_invoked: bool


class FloorInterfaces(Protocol):
    """Forward-declared floor interfaces injected at composition (C-AS-02 §2.2).

    `mcp_server_trust_tier_floor` and `operator_policy_floor` implementations
    are injected by the CP plan (Session 3); `sandbox_tier` is pure given this
    interface (acceptance #7).
    """

    def mcp_server_trust_tier_floor(self, mcp_server: MCPServer | None) -> SandboxTier: ...

    def operator_policy_floor(self, persona_tier: PersonaTier) -> SandboxTier: ...


class AssignedTierReason(StrEnum):
    """The winning `max()` floor source (C-AS-02 §2.5 / spec §15.2 audit enum).

    The §15.2 seven-member audit-surface set. The `sandbox_tier` composition
    assigns one of the five floors it computes; `PERSONA_TIER_FLOOR` and
    `SUB_AGENT_MONOTONIC_ASCENSION` are assigned by downstream units (U-AS-09 /
    U-AS-15). Forced-tier outcomes are reported via `ForcedTierCause` (U-AS-02),
    not via `AssignedTierReason`.
    """

    CONTRACT_MINIMUM = "CONTRACT_MINIMUM"
    BLAST_RADIUS_FLOOR = "BLAST_RADIUS_FLOOR"
    MCP_SERVER_TRUST_FLOOR = "MCP_SERVER_TRUST_FLOOR"
    OPERATOR_POLICY_FLOOR = "OPERATOR_POLICY_FLOOR"
    SANDBOX_TIER_FLOOR = "SANDBOX_TIER_FLOOR"
    PERSONA_TIER_FLOOR = "PERSONA_TIER_FLOOR"
    SUB_AGENT_MONOTONIC_ASCENSION = "SUB_AGENT_MONOTONIC_ASCENSION"


class SandboxTierCompositionOutcome(StrEnum):
    """Discriminant of a `sandbox_tier` composition result (C-AS-02 §2.2)."""

    RESOLVED = "RESOLVED"
    REFUSE = "REFUSE"


class SandboxTierCompositionResult(BaseModel):
    """Result of the `sandbox_tier` composition (C-AS-02 §2.2 / §2.5).

    `RESOLVED` carries the resolved `tier` and either an `assigned_tier_reason`
    (the winning `max()` floor) or a `forced_tier_cause` (when a forcing
    condition from U-AS-02 took precedence). `REFUSE` propagates the
    remote-MCP Level-0 sentinel from `sandbox_tier_floor`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: SandboxTierCompositionOutcome
    tier: SandboxTier | None = None
    assigned_tier_reason: AssignedTierReason | None = None
    forced_tier_cause: ForcedTierCause | None = None


# AC4 tie-break precedence among the five floors — highest precedence first.
_FLOOR_PRECEDENCE: tuple[AssignedTierReason, ...] = (
    AssignedTierReason.SANDBOX_TIER_FLOOR,
    AssignedTierReason.OPERATOR_POLICY_FLOOR,
    AssignedTierReason.MCP_SERVER_TRUST_FLOOR,
    AssignedTierReason.BLAST_RADIUS_FLOOR,
    AssignedTierReason.CONTRACT_MINIMUM,
)


def sandbox_tier(
    tool: ToolContract,
    tool_metadata: ToolMetadata,
    ctx: CallSiteContext,
    floors: FloorInterfaces,
) -> SandboxTierCompositionResult:
    """Resolve the per-tool sandbox tier (C-AS-02 §2.2 five-floor `max()`).

    Forced-tier precedence (acceptance #2): a computer-use / code-execution
    forcing condition (U-AS-02) wins — `RESOLVED` with the `ForcedTierCause`.
    `REFUSE` propagation (acceptance #3): when `sandbox_tier_floor` returns the
    remote-MCP Level-0 sentinel, the composition returns `REFUSE`. Otherwise the
    tier is the monotonically-rising `max()` of the five floors (acceptance #1 /
    #5); `assigned_tier_reason` names the winning floor, tie-broken by the AC4
    precedence order. Pure given the injected `floors` (acceptance #7).
    """
    # Forced-tier precedence (acceptance #2).
    forced = forced_tier(
        ToolContext(
            computer_use_bound=ctx.computer_use_bound,
            code_execution_beta_invoked=ctx.code_execution_beta_invoked,
        )
    )
    if forced is not None:
        return SandboxTierCompositionResult(
            outcome=SandboxTierCompositionOutcome.RESOLVED,
            tier=forced.tier,
            forced_tier_cause=forced.cause,
        )

    # sandbox_tier_floor (U-AS-06) — REFUSE propagation (acceptance #3).
    stf = sandbox_tier_floor(
        tool_metadata,
        ctx.deployment_surface,
        ctx.blast_radius_tier,
        ctx.mcp_transport,
        ctx.mcp_server,
    )
    if stf.outcome is SandboxTierFloorOutcome.REFUSE:
        return SandboxTierCompositionResult(outcome=SandboxTierCompositionOutcome.REFUSE)
    assert stf.tier is not None  # a RESOLVED outcome carries a tier

    # Five-floor max() composition (acceptance #1 / #5).
    floor_tiers: dict[AssignedTierReason, SandboxTier] = {
        AssignedTierReason.CONTRACT_MINIMUM: tool.minimum_tier,
        AssignedTierReason.BLAST_RADIUS_FLOOR: blast_radius_floor(ctx.blast_radius_tier),
        AssignedTierReason.MCP_SERVER_TRUST_FLOOR: floors.mcp_server_trust_tier_floor(
            ctx.mcp_server
        ),
        AssignedTierReason.SANDBOX_TIER_FLOOR: stf.tier,
        AssignedTierReason.OPERATOR_POLICY_FLOOR: floors.operator_policy_floor(ctx.persona_tier),
    }
    winning_rank = max(_TIER_RANK[t] for t in floor_tiers.values())
    # AC4 tie-break: among floors tied at the max, pick by precedence order.
    reason = next(r for r in _FLOOR_PRECEDENCE if _TIER_RANK[floor_tiers[r]] == winning_rank)
    return SandboxTierCompositionResult(
        outcome=SandboxTierCompositionOutcome.RESOLVED,
        tier=floor_tiers[reason],
        assigned_tier_reason=reason,
    )
