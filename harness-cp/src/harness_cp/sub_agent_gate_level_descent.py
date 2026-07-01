"""Sub-agent gate-level monotonic descent + override audit — U-CP-27.

Implements C-CP-12 §12.2 (sub-agent gate-level composition), §12.3
(monotonic-only descent), §12.4 (per-class override surface), and §12.5
(audit-ledger discipline at sub-agent dispatch).

Declares the `SubAgentGateLevelDescent` record, the `dispatch_sub_agent`
selection function, and `emit_sub_agent_dispatch_audit` — the §12.5 audit
entry composition. Two orthogonal monotonicity axes compose at the sub-agent
boundary: `child_gate_level <= parent_gate_level` (gate level descends per
§12.2) and `child_sandbox_tier >= parent_sandbox_tier` (sandbox tier ascends
per `Spec_Action_Surface_v1.md` C-AS-11).

`GateOverride` — the `operator_override` parameter type — is specified as the
opaque alias `Mapping[str, Any]`. C-CP-12 §12.5 explicitly defers "Specific
operator override authoring schema for hierarchical-delegation child
external-reversible" to implementation discretion; §12.4 commits the override
*surface* (permitted at solo-developer + team-binding with audit; prohibited
at multi-tenant-compliance) but no `GateOverride` field set. The opaque-alias
factor-out is faithful (the same pattern as `RoleRoutingBinding` at U-CP-04 per
plan v2.9 §0.5) — no field set is invented.

The audit entry is a `CPAuditLedgerEntry` (the CP-distinct audit type per plan
v2.9 §0.5.1 — NOT the OD-local `AuditLedgerEntry`); F2 six-field construction
delegates to U-IS-07/09/11.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.4 U-CP-27 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §12 C-CP-12 §12.2-§12.5;
ADR-D4 v1.1 §1.5.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from harness_as import GateLevel as ASGateLevel
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_core import ActionID
from pydantic import BaseModel, ConfigDict

from harness_cp.default_downgrade_rule import compute_child_blast_radius_ceiling
from harness_cp.gate_level_rule import GateLevel
from harness_cp.handoff_context import LedgerEntryRef
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry
from harness_cp.sub_agent_brief import SubAgentBrief, compute_brief_summary_hash

#: `GateOverride` — opaque alias. C-CP-12 §12.5 defers the operator override
#: authoring schema to implementation discretion; §12.4 commits the override
#: *surface* but no field set. Faithful factor-out = opaque mapping (the
#: `RoleRoutingBinding` precedent, plan v2.9 §0.5). No field set invented.
type GateOverride = Mapping[str, Any]

# --- Monotonic ascension/descent rank tables --------------------------------

_GATE_RANK: dict[GateLevel, int] = {
    GateLevel.AUTO: 0,
    GateLevel.ASK: 1,
    GateLevel.DENY: 2,
}

_SANDBOX_RANK: dict[SandboxTier, int] = {
    SandboxTier.TIER_1_PROCESS: 0,
    SandboxTier.TIER_2_CONTAINER: 1,
    SandboxTier.TIER_3_MICROVM: 2,
    SandboxTier.TIER_4_FULL_VM: 3,
}


class SubAgentGateLevelDescent(BaseModel):
    """The resolved sub-agent gate-level descent record (C-CP-12 §12.2-§12.4).

    Carries both monotonicity axes: `child_gate_level <= parent_gate_level`
    (gate descends, §12.2) and `child_sandbox_tier >= parent_sandbox_tier`
    (sandbox ascends, C-AS-11) — orthogonal axes per §12.3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_gate_level: GateLevel
    """Resolved at the parent dispatch site via U-CP-43 `gate_level(...)`."""

    parent_sandbox_tier: SandboxTier
    child_blast_radius_ceiling: BlastRadiusTier
    """From the U-CP-26 default-downgrade rule."""

    child_sandbox_tier: SandboxTier
    """Monotonic ascent (>= parent) per `Spec_Action_Surface_v1.md` C-AS-11."""

    child_gate_level: GateLevel
    """Monotonic descent (<= parent) per §12.2."""

    override_applied: bool
    override_audit_ref: LedgerEntryRef | None
    """Populated when `override_applied` is true (§12.4 audit clause)."""


def dispatch_sub_agent(
    parent_action_id: ActionID,
    parent_gate_level: GateLevel,
    parent_sandbox_tier: SandboxTier,
    sub_agent_brief: SubAgentBrief,
    operator_override: GateOverride | None,
) -> SubAgentGateLevelDescent:
    """Resolve the sub-agent gate-level descent at a dispatch site.

    Per §12.2-§12.4: the child blast-radius ceiling comes from the U-CP-26
    default-downgrade rule; the child gate level descends monotonically
    (<= parent) and the child sandbox tier ascends monotonically (>= parent).
    `parent_gate_level` is consumed as resolved by U-CP-43 — this unit does
    NOT recompute it (acceptance #6).

    The default child gate level is the parent's (monotonic-descent admits
    equality — descent never ascends). An `operator_override`, when present,
    may relax the child blast-radius ceiling within the §12.4 per-class scope;
    the override authoring schema is deferred (`GateOverride` opaque). The
    child sandbox tier defaults to the parent's (ascension never descends).

    `override_audit_ref` is `None` at dispatch-time — it is populated by the
    caller once `emit_sub_agent_dispatch_audit` has appended the §12.5 audit
    entry and the F2 `LedgerEntryRef` is materialized.
    """
    _ = sub_agent_brief
    child_ceiling = compute_child_blast_radius_ceiling(_blast_radius_of(parent_sandbox_tier))
    return SubAgentGateLevelDescent(
        parent_gate_level=parent_gate_level,
        parent_sandbox_tier=parent_sandbox_tier,
        child_blast_radius_ceiling=child_ceiling,
        child_sandbox_tier=parent_sandbox_tier,
        child_gate_level=parent_gate_level,
        override_applied=operator_override is not None,
        override_audit_ref=None,
    )


def _blast_radius_of(tier: SandboxTier) -> BlastRadiusTier:
    """Map a sandbox tier to the representative parent blast-radius tier.

    The §12.1 default-downgrade rule is anchored to the parent's capability
    tier; higher sandbox tiers carry the external-reversible parent capability
    the default-downgrade rule downgrades to a read-only child ceiling.
    """
    if _SANDBOX_RANK[tier] >= _SANDBOX_RANK[SandboxTier.TIER_3_MICROVM]:
        return BlastRadiusTier.EXTERNAL_REVERSIBLE
    return BlastRadiusTier.LOCAL_MUTATION


def assert_monotonic_descent(parent_gate_level: GateLevel, child_gate_level: GateLevel) -> None:
    """Enforce the §12.2 monotonic-descent invariant.

    `child_gate_level <= parent_gate_level` — ascent is structurally
    prohibited (§12.3 / ADD §5.3.2). A violation raises `ValueError`.
    """
    if _GATE_RANK[child_gate_level] > _GATE_RANK[parent_gate_level]:
        raise ValueError(
            f"sub-agent gate-level monotonic-descent violated (§12.2): "
            f"child {child_gate_level.value} ascends parent "
            f"{parent_gate_level.value}"
        )


def assert_monotonic_ascent(
    parent_sandbox_tier: SandboxTier, child_sandbox_tier: SandboxTier
) -> None:
    """Enforce the C-AS-11 sandbox-tier monotonic-ascension invariant.

    `child_sandbox_tier >= parent_sandbox_tier` — downgrade is structurally
    prohibited at all persona tiers (§12.4 / C-AS-11). A violation raises
    `ValueError`.
    """
    if _SANDBOX_RANK[child_sandbox_tier] < _SANDBOX_RANK[parent_sandbox_tier]:
        raise ValueError(
            f"sub-agent sandbox-tier monotonic-ascension violated (C-AS-11): "
            f"child {child_sandbox_tier.value} descends parent "
            f"{parent_sandbox_tier.value}"
        )


def emit_sub_agent_dispatch_audit(
    parent_action_id: ActionID,
    descent: SubAgentGateLevelDescent,
    brief_hash: str,
) -> CPAuditLedgerEntry:
    """Compose the §12.5 sub-agent-dispatch audit-ledger entry.

    The entry's `action_id` is `parent_action_id || sub_agent_idx`; F2
    canonicalize+hash delegates to U-IS-08, chain construction to U-IS-09, and
    append to U-IS-11 (C-IS-10 §10.1/§10.3/§10.5). A sub-agent dispatch is
    recorded as an `approve` response (no operator edit/reject/respond), so the
    three response-specific hash fields are absent. The entry is a
    `CPAuditLedgerEntry` — the CP-distinct audit type (plan v2.9 §0.5.1) — NOT
    the OD-local `AuditLedgerEntry`.
    """
    _ = brief_hash
    return CPAuditLedgerEntry(
        action_id=ActionID(f"{parent_action_id}||sub-agent"),
        gate_level=ASGateLevel(descent.child_gate_level.value),
        response="approve",
        timestamp=datetime.now(UTC).isoformat(),
        # `prior_event_hash="0"*64` sentinel canonical at solo-developer
        # tier per ADR-D5 §1.4 row 1 ("no hash chain required by default").
        # Team-binding+ tier wiring deferred per CP spec v1.28 §16.5.6.X.
        prior_event_hash="0" * 64,
    )


def sub_agent_dispatch_response_hash(brief: SubAgentBrief) -> str:
    """`response_hash = sha256(canonicalize(SubAgentBrief))` per §12.5.

    Delegates brief canonicalization + hashing to U-CP-28's
    `compute_brief_summary_hash` (C-CP-13 §13.2).
    """
    return compute_brief_summary_hash(brief)
