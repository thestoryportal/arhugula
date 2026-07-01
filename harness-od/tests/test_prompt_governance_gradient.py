"""Tests for C-OD-34 — per-persona-tier prompt-governance posture (R-PM-1 PR #4).

Acceptance criteria (R-PM-1 design §4.4 / AC #4):
  #1 — PER_PERSONA_TIER_PROMPT_GOVERNANCE maps each of the 3 tiers to one posture.
  #2 — approval_required: solo=False, team=True, multi=True.
  #3 — the posture is NON-VACUOUSLY tier-distinct (SOLO != TEAM, SOLO != MULTI):
       the binding tiers genuinely differ from solo, not just "a table exists".
  #4 — redaction dimension DERIVES from PER_PERSONA_TIER_REDACTION (single source);
       no re-declared redaction flag on the posture.
  #5 — the prompt artifact class (gen_ai.system_instructions) is redaction-covered:
       it is a DEFAULT_OFF_CONTENT_ATTRIBUTES member, and prompt_content_redaction_
       enforced tracks the gradient's toggleability.
"""

from __future__ import annotations

from harness_core import PersonaTier
from harness_od.content_structure_discipline import DEFAULT_OFF_CONTENT_ATTRIBUTES
from harness_od.prompt_governance_gradient import (
    PER_PERSONA_TIER_PROMPT_GOVERNANCE,
    PromptGovernancePosture,
    prompt_content_redaction_enforced,
    resolve_prompt_governance,
)
from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION


def test_posture_maps_every_tier() -> None:
    """Acceptance #1 — total over the closed 3-value PersonaTier enum."""
    assert set(PER_PERSONA_TIER_PROMPT_GOVERNANCE) == set(PersonaTier)
    assert len(PER_PERSONA_TIER_PROMPT_GOVERNANCE) == 3
    for tier, posture in PER_PERSONA_TIER_PROMPT_GOVERNANCE.items():
        assert isinstance(posture, PromptGovernancePosture)
        assert posture.persona_tier is tier


def test_approval_required_per_tier() -> None:
    """Acceptance #2 — solo no approval; team + multi require approval."""
    assert resolve_prompt_governance(PersonaTier.SOLO_DEVELOPER).approval_required is False
    assert resolve_prompt_governance(PersonaTier.TEAM_BINDING).approval_required is True
    assert resolve_prompt_governance(PersonaTier.MULTI_TENANT_COMPLIANCE).approval_required is True


def test_posture_is_non_vacuously_tier_distinct() -> None:
    """Acceptance #3 — the binding tiers' posture genuinely differs from solo.

    Mirrors #481's "TEAM != both neighbours" non-vacuity check: a table whose
    rows are all identical would technically "map every tier" yet carry no
    governance signal. SOLO must differ from both binding tiers on the declared
    approval dimension.
    """
    solo = resolve_prompt_governance(PersonaTier.SOLO_DEVELOPER)
    team = resolve_prompt_governance(PersonaTier.TEAM_BINDING)
    multi = resolve_prompt_governance(PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert solo != team
    assert solo != multi
    # solo is the lone "no governance burden" tier on the declared dimension.
    assert solo.approval_required != team.approval_required
    assert solo.approval_required != multi.approval_required


def test_posture_declares_no_redaction_flag() -> None:
    """Acceptance #4 — the redaction dimension is NOT a re-declared field.

    The posture model carries only persona_tier + approval_required; a
    redaction_required field would be a second source of truth duplicating
    PER_PERSONA_TIER_REDACTION.
    """
    assert set(PromptGovernancePosture.model_fields) == {"persona_tier", "approval_required"}


def test_redaction_dimension_derives_from_gradient() -> None:
    """Acceptance #4 — prompt_content_redaction_enforced derives from the gradient.

    It is exactly the negation of the gradient's per-tier toggleability — never an
    independent restatement.
    """
    for tier in PersonaTier:
        assert prompt_content_redaction_enforced(tier) == (
            not PER_PERSONA_TIER_REDACTION[tier].toggleable
        )
    # Concretely: solo toggleable (not enforced); team + multi non-toggleable.
    assert prompt_content_redaction_enforced(PersonaTier.SOLO_DEVELOPER) is False
    assert prompt_content_redaction_enforced(PersonaTier.TEAM_BINDING) is True
    assert prompt_content_redaction_enforced(PersonaTier.MULTI_TENANT_COMPLIANCE) is True


def test_prompt_artifact_class_is_redaction_covered() -> None:
    """Acceptance #5 — the prompt-content attribute is already a default-off class.

    The injected system prompt's content surfaces (when emitted) as the OTel GenAI
    attribute gen_ai.system_instructions, which is a member of
    DEFAULT_OFF_CONTENT_ATTRIBUTES — so RedactionSpanProcessor already strips it at
    the non-toggleable (binding) tiers. PR #4 composes with this existing real
    consumer rather than re-implementing redaction.
    """
    assert "gen_ai.system_instructions" in DEFAULT_OFF_CONTENT_ATTRIBUTES


def test_posture_is_frozen_hashable() -> None:
    """The posture is frozen → hashable + usable in sets.

    All 3 postures are distinct values: each carries its own persona_tier, so
    team + multi (both approval_required=True) are still distinct on the tier.
    """
    postures = set(PER_PERSONA_TIER_PROMPT_GOVERNANCE.values())
    assert len(postures) == 3
