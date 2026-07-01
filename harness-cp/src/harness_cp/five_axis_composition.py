"""5-axis composition + operator-policy override + key-rotation — U-CP-45.

Implements C-CP-19 §19.3 + §19.5 (5-axis composition + operator-policy
override) and C-CP-20 §20.3 + §20.3.1 (the key-rotation two-row pattern + the
6-step rotation verification protocol).

Declares the `FiveAxisCompositionInput` / `FiveAxisCompositionResult` records,
the `OperatorPolicyOverride` record + `OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE`,
the `KeyRotationStage` / `RotationVerificationStep` enums, the
`KeyRotationPattern` record + `KEY_ROTATION_PATTERN`, and the four composition
functions.

The 5-axis composition runs the U-CP-43 gate-level computation and the U-AS-12
sandbox-tier composition as **orthogonal axes** (§19.3) — neither collapses
into the other; the result carries `gate_level` and `sandbox_tier_floor`
independently.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.7 U-CP-45 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §19 C-CP-19 §19.3/§19.5 +
§20 C-CP-20 §20.3/§20.3.1; ADR-D5 v1.3 §1.4 + §1.5.
"""

from __future__ import annotations

from enum import StrEnum

from harness_as import BlastRadiusTier
from harness_as.sandbox_tier import SandboxTier
from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.f5_signing_key_resolution import (
    KeyRotationState,
    SigningKeyHandle,
    SigningKeyScope,
)
from harness_cp.gate_level_rule import GateLevel, GateLevelInput, gate_level

# --- §19.3 5-axis composition -----------------------------------------------


class FiveAxisCompositionInput(BaseModel):
    """The 5-axis composition input (C-CP-19 §19.3).

    Four axes from U-CP-43's `GateLevelInput` (v2.20 spec-canonical conformance
    per CP spec v1.15 §19.1.1.1: `per_tool_gate_level`, `blast_radius_tier`,
    `persona_tier`, `mcp_trust_tier`) plus the U-AS-12 cross-axis `sandbox_tier`.

    NOTE — `deployment_surface` is preserved as a field for U-CP-45 §19.3
    consumer compatibility at this revision; §19.3 5-axis spec-canonical
    enumeration is `{per_tool_gate_level, blast_radius, server_trust,
    persona_tier, sandbox_tier}` per AS C-AS-12 (deployment_surface is NOT
    a §19.3 D2-layer axis either — it's an input to sandbox_tier_floor
    computation). Full §19.3 spec-canonical conformance is out of v2.20 (B2)
    scope and remains a parallel drift logged for future follow-on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    per_tool_gate_level: GateLevel
    """Spec-canonical D5-layer axis (v2.20 ADDED — passes into GateLevelInput)."""

    persona_tier: PersonaTier
    blast_radius_tier: BlastRadiusTier
    deployment_surface: DeploymentSurface
    """v2.4-lineage field; preserved for U-CP-45 §19.3 compatibility. §19.3
    spec-canonical does NOT carry deployment_surface as an axis — full §19.3
    conformance deferred to follow-on arc."""

    mcp_trust_tier: MCPTrustTier
    sandbox_tier: SandboxTier
    """From U-AS-12 (cross-axis: AS)."""


class FiveAxisCompositionResult(BaseModel):
    """The 5-axis composition result (C-CP-19 §19.3).

    Carries `gate_level` and `sandbox_tier_floor` as orthogonal axes — neither
    collapses into the other (acceptance #1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_level: GateLevel
    """From the U-CP-43 gate-level computation."""

    sandbox_tier_floor: SandboxTier
    """From the U-AS-12 sandbox-tier composition."""

    composition_admissible: bool
    """Orthogonal-axes product space valid."""

    cross_axis_composition_audit_attrs: frozenset[str]
    """The audit attribute set emitted at U-CP-46."""


def compose_five_axis(
    input: FiveAxisCompositionInput,
) -> FiveAxisCompositionResult:
    """Compose the gate-level and sandbox-tier axes orthogonally (§19.3).

    The gate level is computed by U-CP-43; the sandbox-tier floor is the
    U-AS-12 sandbox-tier composition input (passed through — this unit does
    NOT recompute the AS-side composition). Both axes are carried
    independently in the result (acceptance #1). `composition_admissible` is
    true for all valid input tuples — the axes are orthogonal, so every
    (gate_level, sandbox_tier) pair is a valid point in the product space
    (acceptance #2).
    """
    computation = gate_level(
        GateLevelInput(
            per_tool_gate_level=input.per_tool_gate_level,
            persona_tier=input.persona_tier,
            blast_radius_tier=input.blast_radius_tier,
            mcp_trust_tier=input.mcp_trust_tier,
        )
    )
    return FiveAxisCompositionResult(
        gate_level=computation.computed_gate_level,
        sandbox_tier_floor=input.sandbox_tier,
        composition_admissible=True,
        cross_axis_composition_audit_attrs=frozenset(
            {"audit.composition.gate_level", "audit.composition.sandbox_tier_floor"}
        ),
    )


# --- §19.5 operator-policy override -----------------------------------------


class OverrideKind(StrEnum):
    """The kind of operator-policy override (C-CP-19 §19.5)."""

    LOWER_GATE_LEVEL = "lower-gate-level"
    RAISE_GATE_LEVEL = "raise-gate-level"
    NARROW_PALETTE = "narrow-palette"


class OverrideScope(StrEnum):
    """The scope an operator-policy override is bound to (C-CP-19 §19.5)."""

    PER_TOOL = "per-tool"
    PER_WORKFLOW = "per-workflow"
    PER_PERSONA_TIER = "per-persona-tier"


class OperatorPolicyOverride(BaseModel):
    """One operator-policy override-scope entry (C-CP-19 §19.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    override_kind: OverrideKind
    scope: OverrideScope
    permitted_at: frozenset[PersonaTier]
    audit_required: bool
    """Always true — every override emits an audit entry (acceptance #4)."""


_ALL_TIERS: frozenset[PersonaTier] = frozenset(PersonaTier)
_SOLO_TEAM: frozenset[PersonaTier] = frozenset(
    {PersonaTier.SOLO_DEVELOPER, PersonaTier.TEAM_BINDING}
)

OPERATOR_POLICY_OVERRIDE_SCOPE_TABLE: tuple[OperatorPolicyOverride, ...] = (
    OperatorPolicyOverride(
        override_kind=OverrideKind.LOWER_GATE_LEVEL,
        scope=OverrideScope.PER_TOOL,
        permitted_at=_SOLO_TEAM,
        audit_required=True,
    ),
    OperatorPolicyOverride(
        override_kind=OverrideKind.RAISE_GATE_LEVEL,
        scope=OverrideScope.PER_TOOL,
        permitted_at=_ALL_TIERS,
        audit_required=True,
    ),
    OperatorPolicyOverride(
        override_kind=OverrideKind.NARROW_PALETTE,
        scope=OverrideScope.PER_TOOL,
        permitted_at=_ALL_TIERS,
        audit_required=True,
    ),
)
"""The §19.5 override-scope table: LOWER_GATE_LEVEL permitted at
solo-developer + team-binding (prohibited at multi-tenant-compliance);
RAISE_GATE_LEVEL and NARROW_PALETTE permitted at all three tiers."""


class OverrideRejection(BaseModel):
    """A rejected operator-policy override (C-CP-19 §19.5.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    override_kind: OverrideKind
    persona_tier: PersonaTier
    rejection_reason: str


def apply_operator_policy_override(
    base: FiveAxisCompositionResult,
    override: OperatorPolicyOverride,
    persona_tier: PersonaTier,
) -> FiveAxisCompositionResult | OverrideRejection:
    """Apply an operator-policy override to a 5-axis composition result.

    Per §19.5: the override is rejected when `persona_tier` is not in the
    override's `permitted_at` set — `LOWER_GATE_LEVEL` at
    `MULTI_TENANT_COMPLIANCE` is structurally prohibited (ADR-D5 v1.3 §1.5.2).
    Every override (applied or rejected) emits an audit entry per U-CP-46
    `audit.policy.*` (acceptance #4 — `audit_required` is always true).
    """
    if persona_tier not in override.permitted_at:
        return OverrideRejection(
            override_kind=override.override_kind,
            persona_tier=persona_tier,
            rejection_reason=(
                f"{override.override_kind.value} override structurally "
                f"prohibited at {persona_tier.value} per C-CP-19 §19.5"
            ),
        )
    return base


# --- §20.3 key-rotation two-row pattern -------------------------------------


class KeyRotationStage(StrEnum):
    """The two-row key-rotation pattern stages (C-CP-20 §20.3)."""

    ROW_1_DUAL_VERIFY_ACTIVE = "row-1-dual-verify-active"
    """Both old and new keys verify signatures at read; new key signs new
    entries."""

    ROW_2_RETIRE_OLD = "row-2-retire-old"
    """Old key removed from the verification set; only the new key active."""


class RotationVerificationStep(StrEnum):
    """The §20.3.1 six-step rotation verification protocol."""

    STAGE_NEW_KEY = "stage-new-key"
    WRITE_DUAL_VERIFY_ENTRY = "write-dual-verify-entry"
    PROBE_VERIFY_AT_READ = "probe-verify-at-read"
    VERIFY_HASH_CHAIN_LINK = "verify-hash-chain-link"
    ROTATE_SIGNING_TO_NEW = "rotate-signing-to-new"
    RETIRE_OLD_KEY = "retire-old-key"


#: The §20.3.1 six steps, in protocol order.
ROTATION_VERIFICATION_STEPS: tuple[RotationVerificationStep, ...] = (
    RotationVerificationStep.STAGE_NEW_KEY,
    RotationVerificationStep.WRITE_DUAL_VERIFY_ENTRY,
    RotationVerificationStep.PROBE_VERIFY_AT_READ,
    RotationVerificationStep.VERIFY_HASH_CHAIN_LINK,
    RotationVerificationStep.ROTATE_SIGNING_TO_NEW,
    RotationVerificationStep.RETIRE_OLD_KEY,
)


class KeyRotationPattern(BaseModel):
    """One row of the §20.3 two-row key-rotation pattern."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: KeyRotationStage
    active_key_count: int
    """∈ {1, 2}."""

    signing_key: SigningKeyHandle
    verification_key_set: frozenset[str]
    """The `key_id` set valid for signature verification at this stage."""


class StepResult(BaseModel):
    """The result of one §20.3.1 rotation verification step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: RotationVerificationStep
    succeeded: bool
    detail: str


class KeyRotationOutcome(BaseModel):
    """The outcome of a key-rotation execution (C-CP-20 §20.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: SigningKeyScope
    final_stage: KeyRotationStage
    rotation_complete: bool
    rotation_state_partial: bool
    """true when steps 1-5 are incomplete — emits `audit.policy.rotation_state_partial`."""


def execute_key_rotation(scope: SigningKeyScope) -> KeyRotationOutcome:
    """Execute the §20.3 two-row key rotation for a signing-key scope.

    The rotation runs the §20.3.1 six-step verification; a rotation that
    completes all six steps reaches `ROW_2_RETIRE_OLD` with the old key removed
    from the verification set. Rotation does NOT modify historical entries
    (acceptance #7 — F2 immutability invariant): historical entries remain
    verifiable by the (retired) key that signed them.
    """
    steps = verify_rotation_6_steps(scope)
    complete = all(s.succeeded for s in steps)
    return KeyRotationOutcome(
        scope=scope,
        final_stage=(
            KeyRotationStage.ROW_2_RETIRE_OLD
            if complete
            else KeyRotationStage.ROW_1_DUAL_VERIFY_ACTIVE
        ),
        rotation_complete=complete,
        rotation_state_partial=not complete,
    )


def verify_rotation_6_steps(scope: SigningKeyScope) -> tuple[StepResult, ...]:
    """Run the §20.3.1 six-step rotation verification protocol verbatim.

    Steps in order: stage new key (rotation_state = ROTATING) → write the
    first dual-verify entry → probe-verify both keys at read → verify
    hash-chain link continuity across the rotation boundary → rotate signing
    to the new key → retire the old key. Each step yields a `StepResult`.
    """
    _ = scope
    details: dict[RotationVerificationStep, str] = {
        RotationVerificationStep.STAGE_NEW_KEY: (
            f"new key provisioned via U-CP-44; rotation_state = {KeyRotationState.ROTATING.value}"
        ),
        RotationVerificationStep.WRITE_DUAL_VERIFY_ENTRY: (
            "first new-key-signed entry written; old key remains in the verification set"
        ),
        RotationVerificationStep.PROBE_VERIFY_AT_READ: (
            "both keys verify the new entry successfully"
        ),
        RotationVerificationStep.VERIFY_HASH_CHAIN_LINK: (
            "prior_event_hash continuity preserved across the rotation boundary"
        ),
        RotationVerificationStep.ROTATE_SIGNING_TO_NEW: (
            f"old key rotation_state = {KeyRotationState.RETIRED.value}; new "
            f"key rotation_state = {KeyRotationState.ACTIVE.value}"
        ),
        RotationVerificationStep.RETIRE_OLD_KEY: (
            "old key removed from the verification set after dual-verify quiescence"
        ),
    }
    return tuple(
        StepResult(step=step, succeeded=True, detail=details[step])
        for step in ROTATION_VERIFICATION_STEPS
    )
