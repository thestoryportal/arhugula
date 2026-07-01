"""Per-persona-tier prompt-governance posture — R-PM-1 cascade PR #4 (C-OD-34).

Closes the R-PM-1 layer-(d) gap: the prompt artifact class (the operator-supplied
active prompt that PR #1 injects, PR #2 versions, PR #3 selects) had **no
per-persona-tier governance posture**. This module declares one, owned by OD
(approval + redaction are OD primitives), composing with — *not duplicating* —
the existing tier-distinct posture surfaces.

**The two governance dimensions, and why only one is declared here.**

1. **Approval** (declared here — the genuinely net-new dimension). At a
   binding tier a shared / tenant prompt is a governed artifact whose activation
   should require an operator attestation; at the local-first solo tier it should
   not. ``PromptGovernancePosture.approval_required`` carries this:
   ``False`` at solo-developer; ``True`` at team-binding + multi-tenant-compliance.
   The enforcement consumer is the runtime stage-0 selection reconciler
   (``harness_runtime.bootstrap.stage_0_preamble`` →
   ``RT-FAIL-PROMPT-VERSION-UNAPPROVED``); this module owns only the *posture*
   (pure, no runtime/CP import) per the OD ``PER_PERSONA_TIER_REDACTION`` ⊳
   ``RedactionSpanProcessor`` split.

2. **Redaction** (NOT re-declared here — DERIVED). The prompt-content attribute
   class is ``gen_ai.system_instructions``, already a member of
   ``DEFAULT_OFF_CONTENT_ATTRIBUTES`` (C-OD-12 §12.1) and therefore already
   stripped by ``RedactionSpanProcessor`` per the per-tier
   ``PER_PERSONA_TIER_REDACTION`` gradient (C-OD-13 §13.1): solo-developer
   *toggleable* (operator-self-redact), team-binding + multi-tenant-compliance
   *non-toggleable* (always redacted, at the collector boundary / pre-collector
   eval-grade pipeline respectively). Re-declaring a ``redaction_required`` flag
   here would be a **second source of truth** that could drift from the gradient
   (CLAUDE.md §4 one-source-of-truth). So the redaction dimension is exposed as a
   *derived* accessor, ``prompt_content_redaction_enforced``, reading
   ``PER_PERSONA_TIER_REDACTION`` — the prompt artifact class extends the existing
   redaction discipline, it does not restate it.

**Composition, not duplication** (R-PM-1 design §4.4). The posture *wraps* the
landed prompt surface and *extends* the R-CL-P3 (#481) tier-distinct posture
(sampler base-rate + gate synchrony) and the C-OD-13 redaction gradient to the
prompt artifact class — adding the one missing dimension (approval) and deriving
the other (redaction) from its canonical owner.

Authority: Spec_Operational_Discipline_v1_29.md C-OD-34 (this posture); R-PM-1
design §4.4; composes with C-OD-13 §13.1 ``PER_PERSONA_TIER_REDACTION`` +
C-OD-12 §12.1 ``DEFAULT_OFF_CONTENT_ATTRIBUTES`` (``gen_ai.system_instructions``);
ADR-D5 v1.3 §1.5 (persona-tier ladder). No new ADR (additive OD posture).
"""

from __future__ import annotations

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION

__all__ = [
    "PER_PERSONA_TIER_PROMPT_GOVERNANCE",
    "PromptGovernancePosture",
    "prompt_content_redaction_enforced",
    "resolve_prompt_governance",
]


class PromptGovernancePosture(BaseModel):
    """A persona tier's prompt-governance posture (C-OD-34).

    Declares the **approval** dimension only — the genuinely net-new governance
    surface for the prompt artifact class. The **redaction** dimension is
    intentionally absent: it derives from ``PER_PERSONA_TIER_REDACTION`` via
    :func:`prompt_content_redaction_enforced` (single source of truth — a
    re-declared redaction flag could drift from the gradient).

    Frozen → ``Eq`` + ``Hash``, stable under serialization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    approval_required: bool
    """``True`` at team-binding + multi-tenant-compliance — a shared / tenant
    prompt is a governed artifact whose activation requires an operator
    attestation. ``False`` at solo-developer — local-first, minimal burden."""

    def __hash__(self) -> int:
        return hash((self.persona_tier, self.approval_required))


#: The per-persona-tier prompt-governance posture (C-OD-34). Each of the 3
#: persona tiers maps to exactly one posture. The approval dimension is the
#: design-time committed surface; the redaction dimension derives from
#: ``PER_PERSONA_TIER_REDACTION`` (see :func:`prompt_content_redaction_enforced`).
PER_PERSONA_TIER_PROMPT_GOVERNANCE: dict[PersonaTier, PromptGovernancePosture] = {
    PersonaTier.SOLO_DEVELOPER: PromptGovernancePosture(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        approval_required=False,
    ),
    PersonaTier.TEAM_BINDING: PromptGovernancePosture(
        persona_tier=PersonaTier.TEAM_BINDING,
        approval_required=True,
    ),
    PersonaTier.MULTI_TENANT_COMPLIANCE: PromptGovernancePosture(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        approval_required=True,
    ),
}


def resolve_prompt_governance(persona_tier: PersonaTier) -> PromptGovernancePosture:
    """Return the prompt-governance posture for ``persona_tier``.

    Total over the closed 3-value ``PersonaTier`` enum — every tier has exactly
    one posture in ``PER_PERSONA_TIER_PROMPT_GOVERNANCE``.
    """
    return PER_PERSONA_TIER_PROMPT_GOVERNANCE[persona_tier]


def prompt_content_redaction_enforced(persona_tier: PersonaTier) -> bool:
    """Whether the prompt-content artifact class is *non-toggleably* redaction-
    covered at ``persona_tier``.

    The prompt-content OTel attribute is ``gen_ai.system_instructions``, a member
    of ``DEFAULT_OFF_CONTENT_ATTRIBUTES`` (C-OD-12 §12.1) and therefore stripped by
    ``RedactionSpanProcessor`` unless the per-session content-capture toggle is in
    scope AND honored — which it is only at the *toggleable* tier. This accessor
    **derives** the prompt-class redaction posture from ``PER_PERSONA_TIER_REDACTION``
    (the single source of truth): ``True`` at team-binding + multi-tenant-compliance
    (non-toggleable — always redacted); ``False`` at solo-developer
    (operator-toggleable per session). It does NOT re-declare a redaction flag.
    """
    return not PER_PERSONA_TIER_REDACTION[persona_tier].toggleable
