"""Per-persona-tier cryptographic shape composition + trace-inspection read — U-CP-36.

Implements C-CP-15 §15.5 (cross-sibling cryptographic shape composition — one
composition per persona tier) and §15.6 (audit-ledger read at trace
inspection).

Declares:
  - `CrossSiblingCryptographicComposition` — the per-persona-tier composition
    pairing the `SiblingLedgerEntry` cryptographic shape with the
    `ParentFanoutCloseEntry` cryptographic shape; `CROSS_SIBLING_CRYPTOGRAPHIC_
    COMPOSITION` is the 3-entry §15.5 table (one per persona tier).
  - `TraceInspectionSurface` + `CROSS_SIBLING_TRACE_INSPECTION` — the §15.6
    trace-inspection surfaces (`cascade_decision_audit_ledger_id` resolution,
    per-sibling `action_id` join, multi-tenant signature verification).
  - `compose_per_persona_tier_cryptographic_shape` — the §15.5 composition
    rule; delegates the per-tier cryptographic-shape lookup to U-CP-42's
    `PERSONA_TIER_CRYPTOGRAPHIC_SHAPES` registry.
  - `resolve_audit_ledger_entry_from_trace` — §15.6 trace-inspection-time
    resolution of a `ParentFanoutCloseEntry` from a
    `cascade_decision_audit_ledger_id`.
  - `verify_multi_tenant_compliance_signature` — §15.6 signature verification.

**Acceptance #5 — delegation deferred.** The plan acc #5 states signature
verification "delegates to U-CP-45 verifier". U-CP-45 is not landed at this
Phase 7 batch. `verify_multi_tenant_compliance_signature` is landed as a
documented delegation-pending stub — it accepts the §15.6 inputs and raises
`NotImplementedError` citing the pending U-CP-45 verifier. This is a
within-axis forward-reference to an unlanded CP unit, NOT a missing type or a
cross-axis seam; it materializes once U-CP-45 lands. See
`.harness/class_1_tension_cp_scope_discrepancy.md`.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.5 U-CP-36 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §15 C-CP-15 §15.5 + §15.6
(preserved verbatim into v1.3); C-CP-20 §20.2.
"""

from __future__ import annotations

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.parent_fanout_close_entry import ParentFanoutCloseEntry
from harness_cp.per_persona_tier_audit_cryptographic_shape import (
    PERSONA_TIER_CRYPTOGRAPHIC_SHAPES,
    CryptographicShape,
)
from harness_cp.sibling_ledger_entry_composition import SiblingLedgerEntry


class CrossSiblingCryptographicComposition(BaseModel):
    """The cross-sibling cryptographic-shape composition for one persona tier.

    C-CP-15 §15.5 — pairs the per-sibling `SiblingLedgerEntry` cryptographic
    shape with the `ParentFanoutCloseEntry` cryptographic shape. Per §15.5 both
    halves carry the same per-persona-tier shape (the fanout-close primitive
    inherits the tier's audit-ledger cryptographic posture).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    sibling_ledger_entry_cryptographic_shape: CryptographicShape
    parent_fanout_close_entry_cryptographic_shape: CryptographicShape


def _shape_for(persona_tier: PersonaTier) -> CryptographicShape:
    """Look up the §20.1/§20.2 cryptographic shape for a tier (delegates to U-CP-42)."""
    for row in PERSONA_TIER_CRYPTOGRAPHIC_SHAPES:
        if row.persona_tier is persona_tier:
            return row.cryptographic_shape
    raise KeyError(f"no U-CP-42 cryptographic-shape row for {persona_tier.value}")


CROSS_SIBLING_CRYPTOGRAPHIC_COMPOSITION: tuple[CrossSiblingCryptographicComposition, ...] = tuple(
    CrossSiblingCryptographicComposition(
        persona_tier=tier,
        sibling_ledger_entry_cryptographic_shape=_shape_for(tier),
        parent_fanout_close_entry_cryptographic_shape=_shape_for(tier),
    )
    for tier in (
        PersonaTier.SOLO_DEVELOPER,
        PersonaTier.TEAM_BINDING,
        PersonaTier.MULTI_TENANT_COMPLIANCE,
    )
)
"""The §15.5 cross-sibling cryptographic composition — exactly 3 entries.

Per C-CP-15 §15.5 + C-CP-20 §20.2: `solo-developer` → append-only SQLite;
`team-binding` → hash-chained SQLite; `multi-tenant-compliance` → hash-chained
SQLite + signature per entry. Each row delegates the per-tier shape to the
U-CP-42 `PERSONA_TIER_CRYPTOGRAPHIC_SHAPES` registry; this unit declares the
composition rule (the two-halves pairing).
"""


class TraceInspectionSurface(BaseModel):
    """One §15.6 trace-inspection surface (audit-ledger read at trace inspection)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_name: str
    resolution_mechanism: str
    """§15.6 verbatim resolution mechanism."""


CROSS_SIBLING_TRACE_INSPECTION: tuple[TraceInspectionSurface, ...] = (
    TraceInspectionSurface(
        surface_name="cascade_decision_audit_ledger_id_resolution",
        resolution_mechanism=(
            "Resolve the parent_fanout_close_entry from the "
            "topology.cascade_decision_audit_ledger_id span attribute (U-CP-31)."
        ),
    ),
    TraceInspectionSurface(
        surface_name="per_sibling_action_id_join",
        resolution_mechanism=(
            "Join each per-sibling F2 ledger entry to the fanout via the "
            "sibling's action_id (the ParentActionID concatenation per U-CP-34)."
        ),
    ),
    TraceInspectionSurface(
        surface_name="multi_tenant_signature_verification",
        resolution_mechanism=(
            "Verify the multi-tenant-compliance per-entry signature against the "
            "merkle-root over the sibling ledger entries (delegates to the "
            "U-CP-45 verifier)."
        ),
    ),
)
"""The §15.6 trace-inspection surfaces — 3 surfaces per spec verbatim."""


class ResolutionError(BaseModel):
    """A trace-inspection resolution failure (`resolve_audit_ledger_entry_from_trace`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cascade_decision_audit_ledger_id: str
    reason: str


class VerificationResult(BaseModel):
    """The outcome of a multi-tenant-compliance signature verification (§15.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verified: bool
    detail: str


def compose_per_persona_tier_cryptographic_shape(
    persona_tier: PersonaTier,
) -> CrossSiblingCryptographicComposition:
    """Compose the §15.5 cross-sibling cryptographic shape for a persona tier.

    The composition rule: both the per-sibling ledger entry and the parent
    fanout-close entry carry the persona tier's cryptographic shape (delegated
    to the U-CP-42 registry). One composition per persona tier (acceptance #1).
    """
    shape = _shape_for(persona_tier)
    return CrossSiblingCryptographicComposition(
        persona_tier=persona_tier,
        sibling_ledger_entry_cryptographic_shape=shape,
        parent_fanout_close_entry_cryptographic_shape=shape,
    )


def resolve_audit_ledger_entry_from_trace(
    cascade_decision_audit_ledger_id: str,
    fanout_close_entries: dict[str, ParentFanoutCloseEntry],
) -> ParentFanoutCloseEntry | ResolutionError:
    """Resolve a `ParentFanoutCloseEntry` from a trace audit-ledger id (§15.6).

    The `topology.cascade_decision_audit_ledger_id` span attribute (U-CP-31)
    keys into the fanout-close entry store. Returns `ResolutionError` when the
    id resolves to no entry — a `Result`-shaped return per the plan signature.
    """
    entry = fanout_close_entries.get(cascade_decision_audit_ledger_id)
    if entry is None:
        return ResolutionError(
            cascade_decision_audit_ledger_id=cascade_decision_audit_ledger_id,
            reason="no parent_fanout_close_entry for the given audit-ledger id",
        )
    return entry


def verify_multi_tenant_compliance_signature(
    parent_close_entry: ParentFanoutCloseEntry,
    sibling_entries: list[SiblingLedgerEntry],
) -> VerificationResult:
    """Verify the multi-tenant-compliance per-entry signature (§15.6).

    **Delegation pending — acceptance #5.** The plan acc #5 delegates signature
    verification to the U-CP-45 verifier. U-CP-45 is not landed at this Phase 7
    batch; this function is a documented delegation-pending stub. It raises
    `NotImplementedError` citing the pending U-CP-45 verifier rather than
    inventing a verification implementation (which would be an X-AL-3 silent
    design extension). See `.harness/class_1_tension_cp_scope_discrepancy.md`.
    """
    raise NotImplementedError(
        "U-CP-36 §15.6 signature verification delegates to the U-CP-45 "
        "verifier (acc #5); U-CP-45 is not landed at this Phase 7 batch — "
        "see .harness/class_1_tension_cp_scope_discrepancy.md"
    )
