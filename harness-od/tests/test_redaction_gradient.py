"""Tests for U-OD-16 — per-persona-tier content-capture override gradient.

Test set per the U-OD-16 `Tests:` field (Implementation_Plan_Operational_Discipline_v2_1.md
§3.4.6). Every acceptance criterion maps to at least one test.

Acceptance criteria (C-OD-13 §13.1 / §13.2):
  #1 — ContentCapturePosture enumerates exactly 3 values per §13.1.
  #2 — PER_PERSONA_TIER_REDACTION maps each tier to one posture per §13.1.
  #3 — toggleable True only at solo-developer.
  #4 — pre-collector eval-grade pipeline at multi-tenant cells per §13.2.
  #5 — team-binding redaction-processor-at-boundary posture (buffer window).
  #6 — per-persona-tier posture is the design-time committed surface.
"""

from __future__ import annotations

from harness_core import PersonaTier
from harness_od.redaction_gradient import (
    PER_PERSONA_TIER_REDACTION,
    ContentCapturePosture,
    PerPersonaTierRedactionPosture,
)


def test_content_capture_posture_cardinality_three() -> None:
    """Acceptance #1 — ContentCapturePosture enumerates exactly 3 values."""
    assert len(ContentCapturePosture) == 3
    assert set(ContentCapturePosture) == {
        ContentCapturePosture.OPERATOR_SELF_REDACT,
        ContentCapturePosture.REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY,
        ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE,
    }


def test_per_persona_tier_redaction_maps_all_three_tiers() -> None:
    """Acceptance #2 / #6 — the map covers each of the 3 persona tiers exactly
    once, the design-time committed surface."""
    assert set(PER_PERSONA_TIER_REDACTION.keys()) == set(PersonaTier)
    assert len(PER_PERSONA_TIER_REDACTION) == 3
    for tier, posture in PER_PERSONA_TIER_REDACTION.items():
        assert isinstance(posture, PerPersonaTierRedactionPosture)
        assert posture.persona_tier is tier


def test_solo_developer_posture_operator_self_redact() -> None:
    """Acceptance #2 — solo-developer → OPERATOR_SELF_REDACT."""
    assert (
        PER_PERSONA_TIER_REDACTION[PersonaTier.SOLO_DEVELOPER].posture
        is ContentCapturePosture.OPERATOR_SELF_REDACT
    )


def test_team_binding_posture_redaction_processor() -> None:
    """Acceptance #2 / #5 — team-binding → redaction-processor at OTLP
    collector boundary (the buffer-window posture, acceptable at team tier)."""
    assert (
        PER_PERSONA_TIER_REDACTION[PersonaTier.TEAM_BINDING].posture
        is ContentCapturePosture.REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY
    )


def test_multi_tenant_posture_pre_collector_eval_grade() -> None:
    """Acceptance #2 / #4 — multi-tenant-compliance → pre-collector eval-grade
    pipeline (redaction before the BatchSpanProcessor buffer per §13.2)."""
    assert (
        PER_PERSONA_TIER_REDACTION[PersonaTier.MULTI_TENANT_COMPLIANCE].posture
        is ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE
    )


def test_solo_toggleable_true() -> None:
    """Acceptance #3 — toggleable is True at the solo-developer tier."""
    assert PER_PERSONA_TIER_REDACTION[PersonaTier.SOLO_DEVELOPER].toggleable is True


def test_team_toggleable_false() -> None:
    """Acceptance #3 — toggleable is False at the team-binding tier."""
    assert PER_PERSONA_TIER_REDACTION[PersonaTier.TEAM_BINDING].toggleable is False


def test_multi_tenant_toggleable_false() -> None:
    """Acceptance #3 — toggleable is False at the multi-tenant-compliance tier."""
    assert PER_PERSONA_TIER_REDACTION[PersonaTier.MULTI_TENANT_COMPLIANCE].toggleable is False


def test_pre_collector_pipeline_composes_with_u_od_31() -> None:
    """Acceptance #4 — the multi-tenant posture is the pre-collector eval-grade
    pipeline, the design-time surface U-OD-31 runtime enforcement composes with.

    Asserts the data shape (the posture value) — not the downstream U-OD-31
    runtime behavior, which is a later-level unit (no forward-reach).
    """
    multi_tenant = PER_PERSONA_TIER_REDACTION[PersonaTier.MULTI_TENANT_COMPLIANCE]
    assert multi_tenant.posture is ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE
    assert multi_tenant.toggleable is False


def test_posture_model_frozen_and_hashable() -> None:
    """The PerPersonaTierRedactionPosture record is frozen → Eq + Hash."""
    posture = PER_PERSONA_TIER_REDACTION[PersonaTier.SOLO_DEVELOPER]
    assert hash(posture) == hash(posture)
    duplicate = PerPersonaTierRedactionPosture(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        posture=ContentCapturePosture.OPERATOR_SELF_REDACT,
        toggleable=True,
    )
    assert posture == duplicate
