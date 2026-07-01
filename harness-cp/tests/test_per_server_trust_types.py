"""Tests for U-CP-66 (carriers) + U-CP-67 (schemas) — C-CP-27
PerServerTrustEvaluator + MCPClientNamespaceEmitter type carriers.

U-CP-66 ACs (CP plan v2.15 §1, preserved at v2.17):
  AC #1 MCPPrimitive 4-class enum matches §27.2 verbatim
  AC #2 TrustDecisionReason 6-class enum includes UNKNOWN_SERVER_TIER_FLOOR_PASS
        + UNKNOWN_SERVER_TIER_FLOOR_VIOLATION per Decision 3.D1 RATIFIED
  AC #3 TierDerivationRule 3-class enum (CONSERVATIVE / PROTOCOL_VERSION_TABLE /
        OPERATOR_HOOK)
  AC #4 All enums frozen + hashable
  AC #5 pyright strict mode passes (project-level type-check; not unit-tested
        here — verified via uv-workspace lint task)

U-CP-67 ACs (CP plan v2.15 §1, preserved at v2.17):
  AC #1 TrustPolicy includes `tier_derivation: TierDerivationRule` per Decision
        3.D1 ALLOW-with-tier-floor
  AC #2 TrustPolicy.allow_list and deny_list are frozenset[str]
  AC #3 TrustEvaluation.audit_required bool field
  AC #4 Both dataclasses frozen
  AC #5 Pydantic v2 validation
"""

from __future__ import annotations

import pytest
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.per_server_trust_types import (
    MCPPrimitive,
    TierDerivationRule,
    TrustDecisionReason,
    TrustEvaluation,
    TrustPolicy,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# U-CP-66 AC #1 — MCPPrimitive 4-class verbatim
# ---------------------------------------------------------------------------


def test_mcp_primitive_has_exactly_four_members() -> None:
    """U-CP-66 AC #1 — MCPPrimitive declares exactly four members."""
    assert len(MCPPrimitive) == 4


def test_mcp_primitive_member_values_verbatim() -> None:
    """U-CP-66 AC #1 — values match C-AS-14 §14.3 + CP §27.2 verbatim."""
    assert {p.value for p in MCPPrimitive} == {
        "tool",
        "resource",
        "prompt",
        "sampling",
    }


def test_mcp_primitive_is_str_subclass() -> None:
    """StrEnum members are str subclasses (consumable as attribute values)."""
    assert isinstance(MCPPrimitive.TOOL, str)
    assert MCPPrimitive.TOOL == "tool"


# ---------------------------------------------------------------------------
# U-CP-66 AC #2 — TrustDecisionReason 6-class with both UNKNOWN_SERVER_*
# ---------------------------------------------------------------------------


def test_trust_decision_reason_has_exactly_six_members() -> None:
    """U-CP-66 AC #2 — TrustDecisionReason declares exactly six members."""
    assert len(TrustDecisionReason) == 6


def test_trust_decision_reason_includes_unknown_server_pass() -> None:
    """U-CP-66 AC #2 — UNKNOWN_SERVER_TIER_FLOOR_PASS present (Decision 3.D1)."""
    assert TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS.value == (
        "unknown_server_tier_floor_pass"
    )


def test_trust_decision_reason_includes_unknown_server_violation() -> None:
    """U-CP-66 AC #2 — UNKNOWN_SERVER_TIER_FLOOR_VIOLATION present (Decision 3.D1)."""
    assert TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_VIOLATION.value == (
        "unknown_server_tier_floor_violation"
    )


def test_trust_decision_reason_member_values_verbatim() -> None:
    """U-CP-66 AC #2 — all 6 values match CP spec v1.10 §27.2 verbatim."""
    assert {r.value for r in TrustDecisionReason} == {
        "explicit_allow",
        "explicit_deny",
        "tier_floor_pass",
        "tier_floor_violation",
        "unknown_server_tier_floor_pass",
        "unknown_server_tier_floor_violation",
    }


# ---------------------------------------------------------------------------
# U-CP-66 AC #3 — TierDerivationRule 3-class
# ---------------------------------------------------------------------------


def test_tier_derivation_rule_has_exactly_three_members() -> None:
    """U-CP-66 AC #3 — TierDerivationRule declares exactly three members."""
    assert len(TierDerivationRule) == 3


def test_tier_derivation_rule_member_values_verbatim() -> None:
    """U-CP-66 AC #3 — values match CP spec v1.10 §27.2 verbatim."""
    assert {r.value for r in TierDerivationRule} == {
        "conservative",
        "protocol_version_table",
        "operator_hook",
    }


# ---------------------------------------------------------------------------
# U-CP-66 AC #4 — all enums frozen + hashable
# ---------------------------------------------------------------------------


def test_enums_members_are_hashable() -> None:
    """U-CP-66 AC #4 — enum members usable as dict keys / set members."""
    bag = {
        MCPPrimitive.TOOL,
        TrustDecisionReason.EXPLICIT_ALLOW,
        TierDerivationRule.CONSERVATIVE,
    }
    assert len(bag) == 3


def test_enums_members_are_immutable() -> None:
    """U-CP-66 AC #4 — StrEnum members reject attribute reassignment."""
    with pytest.raises((AttributeError, TypeError)):
        MCPPrimitive.TOOL.value = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# U-CP-67 AC #1 — TrustPolicy.tier_derivation field
# ---------------------------------------------------------------------------


def test_trust_policy_has_tier_derivation_field() -> None:
    """U-CP-67 AC #1 — TrustPolicy carries tier_derivation: TierDerivationRule."""
    policy = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    assert policy.tier_derivation == TierDerivationRule.CONSERVATIVE


# ---------------------------------------------------------------------------
# U-CP-67 AC #2 — allow_list / deny_list are frozenset[str]
# ---------------------------------------------------------------------------


def test_trust_policy_allow_list_is_frozenset() -> None:
    """U-CP-67 AC #2 — allow_list typed as frozenset[str]."""
    policy = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset({"server-a", "server-b"}),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    assert isinstance(policy.allow_list, frozenset)
    assert "server-a" in policy.allow_list


def test_trust_policy_deny_list_is_frozenset() -> None:
    """U-CP-67 AC #2 — deny_list typed as frozenset[str]."""
    policy = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset({"bad-server"}),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    assert isinstance(policy.deny_list, frozenset)
    assert "bad-server" in policy.deny_list


# ---------------------------------------------------------------------------
# U-CP-67 AC #3 — TrustEvaluation.audit_required bool
# ---------------------------------------------------------------------------


def test_trust_evaluation_has_audit_required_bool() -> None:
    """U-CP-67 AC #3 — TrustEvaluation.audit_required is a bool field."""
    eval_ = TrustEvaluation(
        permitted=True,
        trust_tier_evaluated=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        decision_reason=TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS,
        audit_required=True,
    )
    assert eval_.audit_required is True


# ---------------------------------------------------------------------------
# U-CP-67 AC #4 — both dataclasses frozen
# ---------------------------------------------------------------------------


def test_trust_policy_is_frozen() -> None:
    """U-CP-67 AC #4 — TrustPolicy rejects mutation."""
    policy = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    with pytest.raises(ValidationError):
        policy.default_tier = MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT  # type: ignore[misc]


def test_trust_evaluation_is_frozen() -> None:
    """U-CP-67 AC #4 — TrustEvaluation rejects mutation."""
    eval_ = TrustEvaluation(
        permitted=True,
        trust_tier_evaluated=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        decision_reason=TrustDecisionReason.EXPLICIT_ALLOW,
        audit_required=False,
    )
    with pytest.raises(ValidationError):
        eval_.permitted = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# U-CP-67 AC #5 — Pydantic v2 validation rejects bad inputs
# ---------------------------------------------------------------------------


def test_trust_policy_rejects_extra_fields() -> None:
    """U-CP-67 AC #5 — extra fields trigger ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        TrustPolicy(  # type: ignore[call-arg]
            default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            per_server_overrides={},
            allow_list=frozenset(),
            deny_list=frozenset(),
            require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            tier_derivation=TierDerivationRule.CONSERVATIVE,
            extra_field="nope",
        )


def test_trust_evaluation_rejects_extra_fields() -> None:
    """U-CP-67 AC #5 — TrustEvaluation extra='forbid'."""
    with pytest.raises(ValidationError):
        TrustEvaluation(  # type: ignore[call-arg]
            permitted=True,
            trust_tier_evaluated=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            decision_reason=TrustDecisionReason.EXPLICIT_ALLOW,
            audit_required=False,
            extra="nope",
        )


def test_trust_policy_rejects_wrong_type_for_tier_derivation() -> None:
    """U-CP-67 AC #5 — tier_derivation must be a TierDerivationRule (or str value)."""
    with pytest.raises(ValidationError):
        TrustPolicy(
            default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            per_server_overrides={},
            allow_list=frozenset(),
            deny_list=frozenset(),
            require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
            tier_derivation="not_a_rule",  # type: ignore[arg-type]
        )
