"""Tests for U-AS-02 — forced-tier resolution predicates (C-AS-01 §1.3)."""

from __future__ import annotations

from harness_as.forced_tier_resolution import (
    ForcedTierCause,
    ToolContext,
    forced_tier,
)
from harness_as.sandbox_tier import SandboxTier


def test_forced_tier_computer_use_returns_tier_4_full_vm() -> None:
    """Acceptance #1 — computer-use binding forces TIER_4_FULL_VM."""
    result = forced_tier(ToolContext(computer_use_bound=True, code_execution_beta_invoked=False))
    assert result is not None
    assert result.tier is SandboxTier.TIER_4_FULL_VM
    assert result.cause is ForcedTierCause.COMPUTER_USE_BOUND


def test_forced_tier_code_execution_beta_returns_tier_4_full_vm() -> None:
    """Acceptance #2 — code-execution beta forces TIER_4_FULL_VM."""
    result = forced_tier(ToolContext(computer_use_bound=False, code_execution_beta_invoked=True))
    assert result is not None
    assert result.tier is SandboxTier.TIER_4_FULL_VM
    assert result.cause is ForcedTierCause.CODE_EXECUTION_BETA


def test_forced_tier_neither_returns_none() -> None:
    """Acceptance #4 — neither flag set yields no forced tier."""
    result = forced_tier(ToolContext(computer_use_bound=False, code_execution_beta_invoked=False))
    assert result is None


def test_forced_tier_both_set_computer_use_cause_precedes() -> None:
    """Acceptance #3 — both flags set: COMPUTER_USE_BOUND cause wins."""
    result = forced_tier(ToolContext(computer_use_bound=True, code_execution_beta_invoked=True))
    assert result is not None
    assert result.tier is SandboxTier.TIER_4_FULL_VM
    assert result.cause is ForcedTierCause.COMPUTER_USE_BOUND


def test_forced_tier_pure_function() -> None:
    """Acceptance #6 — pure function: same input yields equal output."""
    ctx = ToolContext(computer_use_bound=True, code_execution_beta_invoked=False)
    assert forced_tier(ctx) == forced_tier(ctx)
