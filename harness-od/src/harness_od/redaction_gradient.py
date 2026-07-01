"""Per-persona-tier content-capture override gradient — U-OD-16.

Implements C-OD-13 §13.1 (per-persona-tier override gradient — 3-tier posture
matrix) and §13.2 (pre-collector redaction at multi-tenant-compliance cells).

The override gradient maps each of the 3 persona tiers to exactly one
content-capture posture, with a per-tier toggleability flag:

  - `solo-developer` → `OPERATOR_SELF_REDACT`, toggleable per session.
  - `team-binding` → `REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY`,
    non-toggleable; enabling content capture emits a hash-chained audit-ledger
    entry.
  - `multi-tenant-compliance` → `PRE_COLLECTOR_EVAL_GRADE_PIPELINE`,
    non-toggleable; redaction applies at the SDK / wrapper boundary at
    attribute-set time, BEFORE the BatchSpanProcessor buffer (§13.2). This
    eliminates the buffer window where un-redacted content would otherwise
    sit — a compliance-readiness gap per Persona §10.4.

The gradient is the design-time committed surface: deployment-binding-time
selections occur only within the toggleable tier (solo-developer). The
posture ordering is consumed by U-OD-17's cross-deployment monotonic-tightening
invariant (C-OD-13 §13.3); the pre-collector posture composes with U-OD-31's
runtime pre-collector redaction enforcement at multi-tenant cells.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.4.6 U-OD-16
(preserved verbatim through v2.5 §0.3 + v2.6 §3 — no delta; v2.6 §3 pointer
table line 159); Spec_Operational_Discipline_v1_2.md §13 C-OD-13 §13.1 / §13.2
(preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.4
per-persona-tier override gradient.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

__all__ = [
    "PER_PERSONA_TIER_REDACTION",
    "ContentCapturePosture",
    "PerPersonaTierRedactionPosture",
]


class ContentCapturePosture(StrEnum):
    """The 3 content-capture postures of the override gradient (C-OD-13 §13.1).

    Ordered weakest → strongest along the persona-tier axis (the ordering
    U-OD-17's §13.3 monotonic-tightening invariant enforces):
    `OPERATOR_SELF_REDACT` < `REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY`
    < `PRE_COLLECTOR_EVAL_GRADE_PIPELINE`.
    """

    OPERATOR_SELF_REDACT = "OPERATOR_SELF_REDACT"
    """Solo-developer cells — operator-self-redact discipline."""

    REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY = (
        "REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY"
    )
    """Team-binding cells — redaction processor at the OTLP collector boundary."""

    PRE_COLLECTOR_EVAL_GRADE_PIPELINE = "PRE_COLLECTOR_EVAL_GRADE_PIPELINE"
    """Multi-tenant-compliance cells — pre-collector eval-grade redaction
    pipeline at the SDK / wrapper boundary (C-OD-13 §13.2)."""


class PerPersonaTierRedactionPosture(BaseModel):
    """A persona tier's content-capture posture + its toggleability.

    Frozen → `Eq` + `Hash`, stable under serialization. `toggleable` is `True`
    only at the solo-developer tier; team-binding and multi-tenant-compliance
    tiers are non-toggleable (C-OD-13 §13.1 — acceptance #3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    posture: ContentCapturePosture
    toggleable: bool
    """`True` at solo-developer; `False` at team-binding + multi-tenant."""

    def __hash__(self) -> int:
        return hash((self.persona_tier, self.posture, self.toggleable))


#: The per-persona-tier override gradient (C-OD-13 §13.1 verbatim). Each of the
#: 3 persona tiers maps to exactly one posture; the design-time committed
#: surface (acceptance #2, #6).
PER_PERSONA_TIER_REDACTION: dict[PersonaTier, PerPersonaTierRedactionPosture] = {
    PersonaTier.SOLO_DEVELOPER: PerPersonaTierRedactionPosture(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        posture=ContentCapturePosture.OPERATOR_SELF_REDACT,
        toggleable=True,
    ),
    PersonaTier.TEAM_BINDING: PerPersonaTierRedactionPosture(
        persona_tier=PersonaTier.TEAM_BINDING,
        posture=ContentCapturePosture.REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY,
        toggleable=False,
    ),
    PersonaTier.MULTI_TENANT_COMPLIANCE: PerPersonaTierRedactionPosture(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        posture=ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE,
        toggleable=False,
    ),
}
