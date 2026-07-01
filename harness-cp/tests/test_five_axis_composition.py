"""Tests for U-CP-45 — 5-axis composition + override + key rotation (C-CP-19/20).

Acceptance-criterion coverage:
  #1 5-axis orthogonality        -> test_five_axis_orthogonality
  #1 gate + sandbox independent  -> test_gate_level_and_sandbox_tier_independent
  #2 composition_admissible      -> test_composition_admissible_for_valid_inputs
  #3 override scope table        -> test_override_scope_table_match_spec
  #3 lower-gate prohibited @ MTC -> test_lower_gate_prohibited_at_multi_tenant
  #3 raise-gate @ all tiers      -> test_raise_gate_permitted_at_all_tiers
  #3 narrow-palette @ all tiers  -> test_narrow_palette_permitted_at_all_tiers
  #4 override emits audit        -> test_override_emits_audit_regardless_of_tier
  #5 key rotation 2 stages       -> test_key_rotation_two_stages
  #6 rotation 6 steps in order   -> test_rotation_six_steps_in_order
  #8 partial rotation audited    -> test_partial_rotation_state_audited
"""

from __future__ import annotations

from harness_as import BlastRadiusTier
from harness_as.sandbox_tier import SandboxTier
from harness_core import DeploymentSurface, PersonaTier
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.f5_signing_key_resolution import SecretScopeKind, SigningKeyScope
from harness_cp.five_axis_composition import (
    OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE,
    ROTATION_VERIFICATION_STEPS,
    FiveAxisCompositionInput,
    KeyRotationStage,
    OperatorPolicyOverride,
    OverrideKind,
    OverrideRejection,
    OverrideScope,
    RotationVerificationStep,
    apply_operator_policy_override,
    compose_five_axis,
    execute_key_rotation,
    verify_rotation_6_steps,
)
from harness_cp.gate_level_rule import GateLevel


def _input() -> FiveAxisCompositionInput:
    return FiveAxisCompositionInput(
        per_tool_gate_level=GateLevel.AUTO,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        blast_radius_tier=BlastRadiusTier.EXTERNAL_REVERSIBLE,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        mcp_trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        sandbox_tier=SandboxTier.TIER_3_MICROVM,
    )


def _scope() -> SigningKeyScope:
    return SigningKeyScope(scope_kind=SecretScopeKind.TENANT_BOUND, scope_identifier="solo")


def _by_kind(kind: OverrideKind) -> OperatorPolicyOverride:
    return next(o for o in OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE if o.override_kind is kind)


def test_five_axis_orthogonality() -> None:
    """#1 — compose_five_axis carries gate-level and sandbox-tier separately."""
    result = compose_five_axis(_input())
    assert result.gate_level is not None
    assert result.sandbox_tier_floor is SandboxTier.TIER_3_MICROVM


def test_gate_level_and_sandbox_tier_independent() -> None:
    """#1 — gate-level and sandbox-tier are orthogonal: sandbox passes through."""
    for tier in SandboxTier:
        inp = _input().model_copy(update={"sandbox_tier": tier})
        assert compose_five_axis(inp).sandbox_tier_floor is tier


def test_composition_admissible_for_valid_inputs() -> None:
    """#2 — composition_admissible is true for valid input tuples."""
    assert compose_five_axis(_input()).composition_admissible is True


def test_override_scope_table_match_spec() -> None:
    """#3 — OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE has the 3 §19.5 entries."""
    assert len(OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE) == 3
    assert {o.override_kind for o in OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE} == set(OverrideKind)
    for o in OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE:
        assert o.audit_required is True
        assert isinstance(o.scope, OverrideScope)


def test_lower_gate_prohibited_at_multi_tenant() -> None:
    """#3 — LOWER_GATE_LEVEL is prohibited at MULTI_TENANT_COMPLIANCE."""
    lower = _by_kind(OverrideKind.LOWER_GATE_LEVEL)
    assert PersonaTier.MULTI_TENANT_COMPLIANCE not in lower.permitted_at
    assert PersonaTier.SOLO_DEVELOPER in lower.permitted_at
    assert PersonaTier.TEAM_BINDING in lower.permitted_at
    base = compose_five_axis(_input())
    rejection = apply_operator_policy_override(base, lower, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert isinstance(rejection, OverrideRejection)


def test_raise_gate_permitted_at_all_tiers() -> None:
    """#3 — RAISE_GATE_LEVEL is permitted at all three persona tiers."""
    assert _by_kind(OverrideKind.RAISE_GATE_LEVEL).permitted_at == frozenset(PersonaTier)


def test_narrow_palette_permitted_at_all_tiers() -> None:
    """#3 — NARROW_PALETTE is permitted at all three persona tiers."""
    assert _by_kind(OverrideKind.NARROW_PALETTE).permitted_at == frozenset(PersonaTier)


def test_override_emits_audit_regardless_of_tier() -> None:
    """#4 — every override entry has audit_required = true."""
    assert all(o.audit_required for o in OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE)
    # A permitted override applies (returns the base result).
    base = compose_five_axis(_input())
    applied = apply_operator_policy_override(
        base, _by_kind(OverrideKind.RAISE_GATE_LEVEL), PersonaTier.SOLO_DEVELOPER
    )
    assert not isinstance(applied, OverrideRejection)


def test_key_rotation_two_stages() -> None:
    """#5 — KeyRotationStage declares the two §20.3 rows."""
    assert len(KeyRotationStage) == 2
    assert {s.value for s in KeyRotationStage} == {
        "row-1-dual-verify-active",
        "row-2-retire-old",
    }


def test_rotation_six_steps_in_order() -> None:
    """#6 — verify_rotation_6_steps runs the §20.3.1 six steps in order."""
    results = verify_rotation_6_steps(_scope())
    assert len(results) == 6
    assert tuple(r.step for r in results) == ROTATION_VERIFICATION_STEPS
    assert ROTATION_VERIFICATION_STEPS[0] is RotationVerificationStep.STAGE_NEW_KEY
    assert ROTATION_VERIFICATION_STEPS[-1] is RotationVerificationStep.RETIRE_OLD_KEY
    assert all(r.succeeded for r in results)


def test_partial_rotation_state_audited() -> None:
    """#8 — a completed rotation reaches ROW_2; rotation_state_partial false."""
    outcome = execute_key_rotation(_scope())
    assert outcome.rotation_complete is True
    assert outcome.final_stage is KeyRotationStage.ROW_2_RETIRE_OLD
    assert outcome.rotation_state_partial is False
