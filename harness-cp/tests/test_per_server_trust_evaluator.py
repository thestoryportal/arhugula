"""Tests for U-CP-68 — C-CP-27 PerServerTrustEvaluator.evaluate() +
ALLOW-with-tier-floor (Decision 3.D1 RATIFIED).

ACs from CP plan v2.15 §1 U-CP-68 (preserved at v2.17):
  AC #1 Deny-list wins over allow-list (per §27.6 invariant 3)
  AC #2 Unknown-server with resolved tier >= TrustPolicy.default_tier →
        permitted (per Decision 3.D1)
  AC #3 UNKNOWN_SERVER_* decisions ALWAYS set audit_required=true (per §27.6
        invariant 4)
  AC #4 Trust policy immutable per workflow (loaded at bootstrap; verified by
        TrustPolicy model frozen ConfigDict at U-CP-67 — not unit-tested here)
  AC #5 Unit test: each of 6 TrustDecisionReason values exercised
"""

from __future__ import annotations

import pytest
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.per_server_trust_evaluator import (
    PerServerTrustEvaluator,
)
from harness_cp.per_server_trust_types import (
    MCPPrimitive,
    TierDerivationRule,
    TrustDecisionReason,
    TrustPolicy,
)

# ---------------------------------------------------------------------------
# Fixtures — TrustPolicy variants exercising each branch
# ---------------------------------------------------------------------------


def _policy(
    *,
    default_tier: MCPTrustTier = MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    overrides: dict[str, MCPTrustTier] | None = None,
    allow: frozenset[str] = frozenset(),
    deny: frozenset[str] = frozenset(),
    audit_below: MCPTrustTier = MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    tier_rule: TierDerivationRule = TierDerivationRule.CONSERVATIVE,
) -> TrustPolicy:
    return TrustPolicy(
        default_tier=default_tier,
        per_server_overrides=overrides or {},
        allow_list=allow,
        deny_list=deny,
        require_audit_below_tier=audit_below,
        tier_derivation=tier_rule,
    )


# ---------------------------------------------------------------------------
# AC #1 — Deny-list wins over allow-list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_list_wins_over_allow_list() -> None:
    """AC #1 — server in both deny + allow → EXPLICIT_DENY."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(
        deny=frozenset({"both-listed"}),
        allow=frozenset({"both-listed"}),
    )
    result = await evaluator.evaluate("both-listed", MCPPrimitive.TOOL, None, policy)
    assert result.permitted is False
    assert result.decision_reason == TrustDecisionReason.EXPLICIT_DENY
    assert result.audit_required is True


# ---------------------------------------------------------------------------
# AC #2 — Unknown server with resolved tier >= floor → permitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_server_above_floor_permitted() -> None:
    """AC #2 — Decision 3.D1 ALLOW-with-tier-floor for unknown servers."""

    def high_tier_resolver(
        server_name: str, contract: object | None, rule: TierDerivationRule
    ) -> MCPTrustTier:
        return MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT

    evaluator = PerServerTrustEvaluator(tier_resolver=high_tier_resolver)
    policy = _policy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_rule=TierDerivationRule.OPERATOR_HOOK,
    )
    result = await evaluator.evaluate("unknown-server", MCPPrimitive.TOOL, None, policy)
    assert result.permitted is True
    assert result.decision_reason == (TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS)


# ---------------------------------------------------------------------------
# AC #3 — UNKNOWN_SERVER_* always audit_required=true
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_server_pass_always_audit_required() -> None:
    """AC #3 — UNKNOWN_SERVER_TIER_FLOOR_PASS forces audit_required=True
    per §27.6 invariant 4."""

    def high_tier_resolver(
        server_name: str, contract: object | None, rule: TierDerivationRule
    ) -> MCPTrustTier:
        return MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT

    evaluator = PerServerTrustEvaluator(tier_resolver=high_tier_resolver)
    # Even with require_audit_below_tier at a LOW threshold (so the
    # known-server path would NOT audit at tier 3), the UNKNOWN branch
    # still forces audit_required=True.
    policy = _policy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        audit_below=MCPTrustTier.LEVEL_0_REFUSE_REMOTE,
        tier_rule=TierDerivationRule.OPERATOR_HOOK,
    )
    result = await evaluator.evaluate("unknown-srv", MCPPrimitive.TOOL, None, policy)
    assert result.audit_required is True


@pytest.mark.asyncio
async def test_unknown_server_violation_always_audit_required() -> None:
    """AC #3 — UNKNOWN_SERVER_TIER_FLOOR_VIOLATION sets audit_required=True."""
    evaluator = PerServerTrustEvaluator()  # default CONSERVATIVE → LEVEL_0
    policy = _policy(default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL)
    result = await evaluator.evaluate("unknown", MCPPrimitive.TOOL, None, policy)
    assert result.audit_required is True
    assert result.decision_reason == (TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_VIOLATION)


# ---------------------------------------------------------------------------
# AC #5 — each of 6 TrustDecisionReason values exercised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_reason_explicit_deny() -> None:
    """AC #5 — EXPLICIT_DENY surfaced when server in deny_list."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(deny=frozenset({"bad"}))
    result = await evaluator.evaluate("bad", MCPPrimitive.TOOL, None, policy)
    assert result.decision_reason == TrustDecisionReason.EXPLICIT_DENY


@pytest.mark.asyncio
async def test_decision_reason_explicit_allow() -> None:
    """AC #5 — EXPLICIT_ALLOW surfaced when server in allow_list (not denied)."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(allow=frozenset({"good"}))
    result = await evaluator.evaluate("good", MCPPrimitive.TOOL, None, policy)
    assert result.decision_reason == TrustDecisionReason.EXPLICIT_ALLOW
    assert result.permitted is True


@pytest.mark.asyncio
async def test_decision_reason_tier_floor_pass() -> None:
    """AC #5 — known server with tier >= floor → TIER_FLOOR_PASS."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(
        overrides={"known": MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT},
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    result = await evaluator.evaluate("known", MCPPrimitive.TOOL, None, policy)
    assert result.decision_reason == TrustDecisionReason.TIER_FLOOR_PASS
    assert result.permitted is True


@pytest.mark.asyncio
async def test_decision_reason_tier_floor_violation() -> None:
    """AC #5 — known server with tier < floor → TIER_FLOOR_VIOLATION."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(
        overrides={"low": MCPTrustTier.LEVEL_0_REFUSE_REMOTE},
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    result = await evaluator.evaluate("low", MCPPrimitive.TOOL, None, policy)
    assert result.decision_reason == TrustDecisionReason.TIER_FLOOR_VIOLATION
    assert result.permitted is False
    assert result.audit_required is True


@pytest.mark.asyncio
async def test_decision_reason_unknown_server_tier_floor_pass() -> None:
    """AC #5 — unknown server with resolved tier >= floor →
    UNKNOWN_SERVER_TIER_FLOOR_PASS (covered also by AC #2 test above; this
    repeats the assertion under different fixture pattern for clarity)."""

    def resolver(s: str, c: object | None, r: TierDerivationRule) -> MCPTrustTier:
        return MCPTrustTier.LEVEL_2_SANDBOX_ALL

    evaluator = PerServerTrustEvaluator(tier_resolver=resolver)
    policy = _policy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_rule=TierDerivationRule.PROTOCOL_VERSION_TABLE,
    )
    result = await evaluator.evaluate("novel", MCPPrimitive.RESOURCE, None, policy)
    assert result.decision_reason == (TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS)
    assert result.permitted is True


@pytest.mark.asyncio
async def test_decision_reason_unknown_server_tier_floor_violation() -> None:
    """AC #5 — unknown server with resolved tier < floor →
    UNKNOWN_SERVER_TIER_FLOOR_VIOLATION (CONSERVATIVE default → LEVEL_0 <
    any LEVEL_>=1 floor)."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(default_tier=MCPTrustTier.LEVEL_1_SIGNED_PINNED)
    result = await evaluator.evaluate("anyone", MCPPrimitive.PROMPT, None, policy)
    assert result.decision_reason == (TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_VIOLATION)
    assert result.permitted is False


# ---------------------------------------------------------------------------
# Default resolver behavior (CONSERVATIVE-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_resolver_raises_for_non_conservative_rules() -> None:
    """The default resolver only handles CONSERVATIVE per §27.7. Operator
    must inject TierResolver when policy declares PROTOCOL_VERSION_TABLE
    or OPERATOR_HOOK — else default raises ValueError on unknown server."""
    evaluator = PerServerTrustEvaluator()  # default resolver
    policy = _policy(tier_rule=TierDerivationRule.PROTOCOL_VERSION_TABLE)
    with pytest.raises(ValueError, match="requires an operator-supplied TierResolver"):
        await evaluator.evaluate("unknown", MCPPrimitive.TOOL, None, policy)


@pytest.mark.asyncio
async def test_default_resolver_returns_min_tier_for_conservative() -> None:
    """CONSERVATIVE default resolver returns LEVEL_0_REFUSE_REMOTE (MIN)."""
    evaluator = PerServerTrustEvaluator()
    # Use a floor of LEVEL_0 — unknown server passes (MIN == floor).
    policy = _policy(default_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE)
    result = await evaluator.evaluate("unknown", MCPPrimitive.TOOL, None, policy)
    assert result.trust_tier_evaluated == MCPTrustTier.LEVEL_0_REFUSE_REMOTE
    assert result.permitted is True


# ---------------------------------------------------------------------------
# Audit-below-tier discipline for known/allow branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_server_audit_below_tier_triggers_audit() -> None:
    """`require_audit_below_tier` forces audit_required=True when known-tier
    is below the audit floor (even on permitted TIER_FLOOR_PASS)."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(
        overrides={"mid": MCPTrustTier.LEVEL_2_SANDBOX_ALL},
        default_tier=MCPTrustTier.LEVEL_1_SIGNED_PINNED,
        audit_below=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    )
    result = await evaluator.evaluate("mid", MCPPrimitive.TOOL, None, policy)
    assert result.permitted is True
    assert result.decision_reason == TrustDecisionReason.TIER_FLOOR_PASS
    assert result.audit_required is True  # tier 2 < audit_below tier 3


@pytest.mark.asyncio
async def test_allow_list_no_audit_when_tier_above_audit_floor() -> None:
    """EXPLICIT_ALLOW with tier >= audit_below → no audit required."""
    evaluator = PerServerTrustEvaluator()
    policy = _policy(
        allow=frozenset({"allowed"}),
        overrides={"allowed": MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT},
        audit_below=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    result = await evaluator.evaluate("allowed", MCPPrimitive.TOOL, None, policy)
    assert result.decision_reason == TrustDecisionReason.EXPLICIT_ALLOW
    assert result.audit_required is False
