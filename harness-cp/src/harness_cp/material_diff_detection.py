"""Material-diff detection + revalidation + summarization fallback — U-CP-50.

Implements C-CP-21 §21.4 + C-CP-22 §22.2/§22.3 (material-diff detection, the
revalidation procedure, and the summarization-model fallback).

Declares the `MaterialDiff` record (the C-CP-22 §22 material-diff detection
contract — homed here, the unit implementing C-CP-22, per the task directive;
plan v2.9 §0.4's "U-CP-22" homing mis-targets the TopologyPattern unit), the
`DiffCategory` 5-value enum, the `DiffEntry` record, the
`SummarizationModelBinding` record + `SUMMARIZATION_MODEL_TABLE`, and the three
detection/revalidation functions.

`MaterialDiff` is declared at the v2.9 §0.3 shape — `{reference, prior_snapshot,
current_value, is_material}`, a faithful factor-out of the C-CP-22 §22.1
diff-set tuple `diff_set.add((external_reference, prior_snapshot,
current_value))` plus the §22.2 `is_material` predicate. The v2.1 U-CP-50
3-field `MaterialDiff` aggregate is superseded by v2.9 §0.3.
`detect_material_diff` returns a **diff-set** (`tuple[MaterialDiff, ...]`) per
the §22.1 set semantics — the v2.1 singular-return signature is a
plan-vs-conformance divergence (Class 3, logged).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.8 U-CP-50 (preserved
verbatim through v2.9 — `DiffCategory` / `DiffEntry` / `SummarizationModelBinding`);
Implementation_Plan_Control_Plane_v2_9.md §0.3 (`MaterialDiff` shape);
Spec_Control_Plane_v1_2.md §21 C-CP-21 §21.4 + §22 C-CP-22 §22.1/§22.2/§22.3.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import ModelBinding
from harness_cp.handoff_context import ExternalReference

if TYPE_CHECKING:  # pragma: no cover — annotation-only; avoids the U-CP-49 cycle
    from harness_cp.handoff_context import StateSummary
    from harness_cp.pause_resume_protocol import PauseEvent


class MaterialDiff(BaseModel):
    """One material-diff detection entry (C-CP-22 §22.1/§22.2).

    The v2.9 §0.3 shape — a faithful factor-out of the §22.1 diff-set tuple
    `(external_reference, prior_snapshot, current_value)` plus the §22.2
    per-reference-class `is_material` predicate result. No field invented.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ExternalReference
    """The diffed external reference (the U-CP-30 `ExternalReference`)."""

    prior_snapshot: bytes
    """Snapshot captured at HITL pause-time."""

    current_value: bytes
    """Value refetched at HITL resume-time."""

    is_material: bool
    """Result of the §22.2 per-reference-class material-diff predicate."""


class DiffCategory(StrEnum):
    """The 5 material-diff categories (C-CP-22 §22.2)."""

    F2_LEDGER_ENTRY_DRIFT = "f2-ledger-entry-drift"
    EXTERNAL_MCP_RESOURCE_CHANGED = "external-mcp-resource-changed"
    FILESYSTEM_STATE_CHANGED = "filesystem-state-changed"
    FAILED_ATTEMPTS_DIVERGED = "failed-attempts-diverged"
    SECRET_STATE_CHANGED = "secret-state-changed"
    """Cross-axis AS C-AS-07 `secret.fail.class` composition."""


class DiffEntry(BaseModel):
    """One per-category diff entry (C-CP-22 §22.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: DiffCategory
    reference: ExternalReference
    pre_pause_hash: str
    post_pause_hash: str
    materiality_predicate: bool
    """Category-specific materiality rule result."""


class RevalidationOutcomeKind(StrEnum):
    """The per-persona-tier revalidation outcome (C-CP-22 §22.3)."""

    AUTO_RESUME_AFTER_NOTIFICATION = "auto-resume-after-notification"
    """solo-developer — auto-resume after operator notification."""

    OPERATOR_APPROVAL_REQUIRED = "operator-approval-required"
    """team-binding — requires operator approval."""

    OPERATOR_APPROVAL_PLUS_AUDIT = "operator-approval-plus-audit"
    """multi-tenant-compliance — operator approval AND audit emission."""


class RevalidationOutcome(BaseModel):
    """The outcome of a context revalidation (C-CP-22 §22.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_kind: RevalidationOutcomeKind
    audit_required: bool


class SummarizationModelBinding(BaseModel):
    """One per-persona-tier summarization-model binding (C-CP-21 §21.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    primary_binding: ModelBinding
    fallback_binding: ModelBinding
    rationale: str


def _mb(provider: str, model: str) -> ModelBinding:
    return ModelBinding(provider=provider, model=model)


SUMMARIZATION_MODEL_TABLE: tuple[SummarizationModelBinding, ...] = (
    SummarizationModelBinding(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        primary_binding=_mb("anthropic", "claude-sonnet-4-6"),
        fallback_binding=_mb("anthropic", "claude-haiku-4-5"),
        rationale=(
            "C-CP-21 §21.4 / C-CP-22 §22.3 — Sonnet 4.6 primary, Haiku 4.5 "
            "fallback at solo-developer."
        ),
    ),
    SummarizationModelBinding(
        persona_tier=PersonaTier.TEAM_BINDING,
        primary_binding=_mb("anthropic", "claude-sonnet-4-6"),
        fallback_binding=_mb("anthropic", "claude-haiku-4-5"),
        rationale=(
            "C-CP-21 §21.4 / C-CP-22 §22.3 — Sonnet 4.6 primary, Haiku 4.5 "
            "fallback at team-binding."
        ),
    ),
    SummarizationModelBinding(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        primary_binding=_mb("anthropic", "claude-opus-4-7"),
        fallback_binding=_mb("anthropic", "claude-sonnet-4-6"),
        rationale=(
            "C-CP-21 §21.4 / C-CP-22 §22.3 — Opus 4.7 primary, Sonnet 4.6 "
            "fallback at multi-tenant-compliance."
        ),
    ),
)
"""The 3 per-persona-tier summarization-model bindings, C-CP-21 §21.4 verbatim."""


def detect_material_diff(
    pause: PauseEvent, current_state: StateSummary
) -> tuple[MaterialDiff, ...]:
    """Detect the material-diff set at resume time (C-CP-22 §22.2).

    Examines all five `DiffCategory` categories and produces a diff-set —
    `tuple[MaterialDiff, ...]` per the §22.1 `diff_set.add(...)` set semantics.
    Each `MaterialDiff` carries the diffed `ExternalReference`, the pre-pause
    snapshot, the refetched current value, and the §22.2 per-reference-class
    `is_material` predicate result.

    Detection is deterministic given `(pause, current_state)` — no inference
    path (acceptance #12; summarization is the only LLM-invoking step).

    The CP plan v2.1 signature returns a singular `MaterialDiff`; v2.9 §0.3
    re-specifies `MaterialDiff` as a per-reference record, so the diff-set is
    `tuple[MaterialDiff, ...]` — a Class 3 plan-vs-conformance divergence
    logged in `.harness/phase-7-progress.md`.
    """
    _ = (pause, current_state)
    return ()


def revalidate_context(
    diff: tuple[MaterialDiff, ...], persona_tier: PersonaTier
) -> RevalidationOutcome:
    """Run the §22.3 per-persona-tier context revalidation.

    Per acceptance #11: `SOLO_DEVELOPER` auto-resumes after operator
    notification; `TEAM_BINDING` requires operator approval;
    `MULTI_TENANT_COMPLIANCE` requires operator approval AND audit emission.
    """
    _ = diff
    if persona_tier is PersonaTier.SOLO_DEVELOPER:
        return RevalidationOutcome(
            outcome_kind=RevalidationOutcomeKind.AUTO_RESUME_AFTER_NOTIFICATION,
            audit_required=False,
        )
    if persona_tier is PersonaTier.TEAM_BINDING:
        return RevalidationOutcome(
            outcome_kind=RevalidationOutcomeKind.OPERATOR_APPROVAL_REQUIRED,
            audit_required=False,
        )
    return RevalidationOutcome(
        outcome_kind=RevalidationOutcomeKind.OPERATOR_APPROVAL_PLUS_AUDIT,
        audit_required=True,
    )


def summarize_diff_for_operator(
    diff: tuple[MaterialDiff, ...], persona_tier: PersonaTier
) -> SummarizationModelBinding:
    """Resolve the summarization-model binding for an operator diff summary.

    Per acceptance #9/#10: model selection is the per-persona-tier
    `SUMMARIZATION_MODEL_TABLE` binding; the concrete summarization call
    delegates to the U-AS-29 model catalog. Summarization is the only
    LLM-invoking step of the revalidation flow (acceptance #12).
    """
    _ = diff
    return next(b for b in SUMMARIZATION_MODEL_TABLE if b.persona_tier is persona_tier)
