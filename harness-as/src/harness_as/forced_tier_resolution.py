"""Forced-tier resolution predicates — U-AS-02.

Implements C-AS-01 §1.3 (forced-tier rules). Resolves the two conditions
that force `TIER_4_FULL_VM` regardless of a tool's declared blast-radius:
computer-use binding and code-execution beta.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-02;
Spec_Action_Surface_v1.md §1.3 C-AS-01; ADR-D2 v1.1 §1.1.

Depends on: U-AS-01 (`SandboxTier`).

`ToolContext` is named in the U-AS-02 signature but not defined there — the
unit's `Inputs` field specifies it carries `computer_use_bound: bool` and
`code_execution_beta_invoked: bool`. Modelled here minimally with exactly
those two fields (same referenced-but-undefined resolution pattern as
U-IS-02 `WorkflowClass` / U-IS-04 `ContractID`).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_tier import SandboxTier


class ToolContext(BaseModel):
    """Per-tool-invocation context consumed by forced-tier resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    computer_use_bound: bool
    code_execution_beta_invoked: bool


class ForcedTierCause(StrEnum):
    """The cause of a forced-tier resolution (C-AS-01 §1.3)."""

    COMPUTER_USE_BOUND = "COMPUTER_USE_BOUND"
    CODE_EXECUTION_BETA = "CODE_EXECUTION_BETA"


class ForcedTierResult(BaseModel):
    """A forced-tier resolution: the forced tier and its cause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: SandboxTier
    cause: ForcedTierCause


def forced_tier(ctx: ToolContext) -> ForcedTierResult | None:
    """Resolve the forced sandbox tier for a tool invocation, if any.

    Pure function over `ctx`. Computer-use binding and code-execution beta
    each force `TIER_4_FULL_VM` (C-AS-01 §1.3). When both flags are set,
    `COMPUTER_USE_BOUND` wins — full-VM ephemeral + network-egress-restricted
    is strictly stronger. Returns `None` when neither flag is set; a forced
    tier supersedes any per-tool authoring-time `minimum_tier`.
    """
    if ctx.computer_use_bound:
        return ForcedTierResult(
            tier=SandboxTier.TIER_4_FULL_VM,
            cause=ForcedTierCause.COMPUTER_USE_BOUND,
        )
    if ctx.code_execution_beta_invoked:
        return ForcedTierResult(
            tier=SandboxTier.TIER_4_FULL_VM,
            cause=ForcedTierCause.CODE_EXECUTION_BETA,
        )
    return None
