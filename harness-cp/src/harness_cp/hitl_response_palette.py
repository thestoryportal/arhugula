"""4-response HITL palette + per-response audit entry shapes — U-CP-37.

Implements C-CP-16 §16.1 (closed four-value palette), §16.2 (per-response
audit-ledger entry shape), §16.3 (palette completeness invariance), §16.4
(`hitl.response.class` span attribute).

Declares the closed 4-value `HITLResponse` enum, the `HITL_RESPONSE_SEMANTICS`
table, the `PER_RESPONSE_AUDIT_ENTRY_SHAPES` table, the 3-entry
`PALETTE_INVARIANTS`, and the `HITL_RESPONSE_CLASS_ATTRIBUTE` declaration.

The palette is **closed** at D5 — extension is a Workflow §4.1.2 Class-2 D5
revision. The per-response audit entry `prior_event_hash` field chains per IS
C-IS-06 hash-chain construction at team-binding+ persona tiers; the chain-link
construction itself delegates to U-IS-09 (cross-axis IS) — this unit declares
only the audit-entry *shape*, not the chaining mechanism. `value_type`/
`cardinality` on the response-class attribute resolve to `harness-core`'s
`AttributeValueType` / `Cardinality` (U-CP-00b carrier).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.6 U-CP-37 (preserved
verbatim through v2.6 — `[U-CP-00b]` edge-add only, §0.11); Spec_Control_Plane_v1_2.md
§16 C-CP-16 §16.1-§16.4 (preserved verbatim into v1.3); ADR-D5 v1.3 §1.1 +
§1.8 + §1.4.1.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class HITLResponse(StrEnum):
    """The closed 4-value HITL operator-response palette (C-CP-16 §16.1).

    Closed at D5 per ADR-D5 v1.3 §1.1; extension is a Workflow §4.1.2
    Class-2 D5 revision.
    """

    APPROVE = "approve"
    """Proceed with proposed action as-is."""

    EDIT = "edit"
    """Proceed with operator-modified proposed action."""

    REJECT = "reject"
    """Cancel proposed action; agent receives rejection signal."""

    RESPOND = "respond"
    """Continue dialogue with the agent without action commitment — distinct
    from `REJECT` ("cancel action") per §16.3 closing sentence."""


class CellApplicabilityScope(StrEnum):
    """Scope of cells (of the C-CP-18 matrix) a palette response applies at."""

    ALL_CELLS = "all_cells"


class HITLResponseSemantic(BaseModel):
    """One `HITLResponse` value's §16.1 semantic + cell applicability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response: HITLResponse
    semantic: str
    cell_applicability: CellApplicabilityScope


HITL_RESPONSE_SEMANTICS: tuple[HITLResponseSemantic, ...] = (
    HITLResponseSemantic(
        response=HITLResponse.APPROVE,
        semantic="Proceed with proposed action as-is",
        cell_applicability=CellApplicabilityScope.ALL_CELLS,
    ),
    HITLResponseSemantic(
        response=HITLResponse.EDIT,
        semantic="Proceed with operator-modified proposed action",
        cell_applicability=CellApplicabilityScope.ALL_CELLS,
    ),
    HITLResponseSemantic(
        response=HITLResponse.REJECT,
        semantic="Cancel proposed action; agent receives rejection signal",
        cell_applicability=CellApplicabilityScope.ALL_CELLS,
    ),
    HITLResponseSemantic(
        response=HITLResponse.RESPOND,
        semantic="Continue dialogue with the agent without action commitment",
        cell_applicability=CellApplicabilityScope.ALL_CELLS,
    ),
)
"""The 4 §16.1 palette semantics — all applicable at every C-CP-18 cell."""


class AuditFieldName(StrEnum):
    """The audit-ledger entry field names of the §16.2 per-response shapes."""

    ACTION_ID = "action_id"
    GATE_LEVEL = "gate_level"
    RESPONSE = "response"
    EDITED_PROPOSAL_HASH = "edited_proposal_hash"
    REJECTION_REASON_HASH = "rejection_reason_hash"
    RESPONSE_TEXT_HASH = "response_text_hash"
    TIMESTAMP = "timestamp"
    PRIOR_EVENT_HASH = "prior_event_hash"


class PerResponseAuditEntryShape(BaseModel):
    """One `HITLResponse` value's §16.2 audit-ledger entry shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response: HITLResponse
    required_fields: frozenset[AuditFieldName]
    optional_fields: frozenset[AuditFieldName]


# The §16.2 base required-field set common to all four responses.
_AUDIT_BASE: frozenset[AuditFieldName] = frozenset(
    {
        AuditFieldName.ACTION_ID,
        AuditFieldName.GATE_LEVEL,
        AuditFieldName.RESPONSE,
        AuditFieldName.TIMESTAMP,
        AuditFieldName.PRIOR_EVENT_HASH,
    }
)

PER_RESPONSE_AUDIT_ENTRY_SHAPES: tuple[PerResponseAuditEntryShape, ...] = (
    PerResponseAuditEntryShape(
        response=HITLResponse.APPROVE,
        required_fields=_AUDIT_BASE,
        optional_fields=frozenset(),
    ),
    PerResponseAuditEntryShape(
        response=HITLResponse.EDIT,
        required_fields=_AUDIT_BASE | {AuditFieldName.EDITED_PROPOSAL_HASH},
        optional_fields=frozenset(),
    ),
    PerResponseAuditEntryShape(
        response=HITLResponse.REJECT,
        required_fields=_AUDIT_BASE,
        optional_fields=frozenset({AuditFieldName.REJECTION_REASON_HASH}),
    ),
    PerResponseAuditEntryShape(
        response=HITLResponse.RESPOND,
        required_fields=_AUDIT_BASE | {AuditFieldName.RESPONSE_TEXT_HASH},
        optional_fields=frozenset(),
    ),
)
"""The 4 §16.2 per-response audit-ledger entry shapes — `approve` base set;
`edit` adds `edited_proposal_hash`; `reject` adds optional
`rejection_reason_hash`; `respond` adds required `response_text_hash`."""


class InvariantEnforcementPoint(StrEnum):
    """The point at which a §16.3 palette invariant is enforced."""

    EVERY_HITL_INVOCATION = "every_hitl_invocation"
    CELL_SYNCHRONY_DELIVERY = "cell_synchrony_delivery"
    PRE_HITL_ESCALATION = "pre_hitl_escalation"


class PaletteCompletenessInvariant(BaseModel):
    """One §16.3 palette-completeness invariant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invariant_name: str
    invariant_statement: str
    enforced_at: InvariantEnforcementPoint


PALETTE_INVARIANTS: tuple[PaletteCompletenessInvariant, ...] = (
    PaletteCompletenessInvariant(
        invariant_name="palette_completeness",
        invariant_statement=(
            "Every HITL invocation surface presents all four response options "
            "at every cell of the C-CP-18 persona-tier × engine-class matrix."
        ),
        enforced_at=InvariantEnforcementPoint.EVERY_HITL_INVOCATION,
    ),
    PaletteCompletenessInvariant(
        invariant_name="synchrony_class_does_not_narrow_palette",
        invariant_statement=(
            "The synchrony class per cell determines how the palette is "
            "delivered — not what the operator can express."
        ),
        enforced_at=InvariantEnforcementPoint.CELL_SYNCHRONY_DELIVERY,
    ),
    PaletteCompletenessInvariant(
        invariant_name="pre_hitl_escalation_may_narrow_palette",
        invariant_statement=(
            "At pre-HITL escalation invocations the palette MAY be narrowed per C-CP-21 §21.2."
        ),
        enforced_at=InvariantEnforcementPoint.PRE_HITL_ESCALATION,
    ),
)
"""The 3 §16.3 palette invariants — completeness; synchrony does not narrow;
pre-HITL escalation MAY narrow."""


class HITLResponseClassAttribute(BaseModel):
    """The `hitl.response.class` span attribute declaration (C-CP-16 §16.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality
    emitted_on: str


HITL_RESPONSE_CLASS_ATTRIBUTE: HITLResponseClassAttribute = HITLResponseClassAttribute(
    attribute_name="hitl.response.class",
    value_type=AttributeValueType.ENUM_REF,
    cardinality=Cardinality.LOW,
    emitted_on="hitl.invocation.responded",
)
"""The §16.4 `hitl.response.class` span attribute — bounded-4 enum (the
`HITLResponse` palette), emitted on the `hitl.invocation.responded` event."""
