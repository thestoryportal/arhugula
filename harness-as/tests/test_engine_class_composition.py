"""Tests for U-AS-29 — engine-class composition + model-binding matrix (C-AS-13 §13.3-§13.4)."""

from __future__ import annotations

from harness_as.engine_class_composition import (
    ENGINE_CLASS_COMPOSITION_OVERLAY,
    MODEL_BINDING_MATRIX,
    MODEL_TIER_ESCALATION_CHAIN,
    PRE_HITL_ESCALATION_ORDER,
    AnthropicModel,
    D1EngineClass,
    PreHITLEscalationStep,
    SubAgentRole,
    model_binding,
)
from harness_core import WorkloadClass


def test_d1_engine_class_cardinality_five() -> None:
    """Acceptance #1 — D1EngineClass declares exactly 5 values."""
    assert len(D1EngineClass) == 5


def test_engine_class_composition_overlay_per_spec_row_by_row() -> None:
    """Acceptance #2 — the overlay declares 5 rows, one per engine class."""
    assert len(ENGINE_CLASS_COMPOSITION_OVERLAY) == 5
    for engine_class in D1EngineClass:
        comp = ENGINE_CLASS_COMPOSITION_OVERLAY[engine_class]
        assert comp.engine_class is engine_class
        assert comp.prompt_cache_scope
        assert comp.batch_api_integration
    assert (
        ENGINE_CLASS_COMPOSITION_OVERLAY[D1EngineClass.EVENT_SOURCED_REPLAY].prompt_cache_scope
        == "Activity-internal"
    )
    assert (
        ENGINE_CLASS_COMPOSITION_OVERLAY[D1EngineClass.WAL_SEGMENT].prompt_cache_scope
        == "Per-segment"
    )


def test_skills_filesystem_residence_uniform_across_engine_classes() -> None:
    """Acceptance #3 — every engine class references SKILL.md filesystem residence."""
    for comp in ENGINE_CLASS_COMPOSITION_OVERLAY.values():
        assert "SKILL.md" in comp.skills_filesystem_residence


def test_sub_agent_role_cardinality_five() -> None:
    """Acceptance #4 — SubAgentRole declares 5 values."""
    assert len(SubAgentRole) == 5


def test_anthropic_model_cardinality_four() -> None:
    """Acceptance #4 — AnthropicModel declares 4 values."""
    assert len(AnthropicModel) == 4


def test_model_binding_matrix_cardinality_20_cells() -> None:
    """Acceptance #5 — MODEL_BINDING_MATRIX declares exactly 20 cells."""
    assert len(MODEL_BINDING_MATRIX) == 20
    assert len(WorkloadClass) * len(SubAgentRole) == 20


def test_model_binding_software_engineering_lead_is_sonnet_4_6_with_opus_qualifier() -> None:
    """Acceptance #5 — SE lead/orchestrator is Sonnet 4.6 with the Opus qualifier."""
    binding = model_binding(WorkloadClass.SOFTWARE_ENGINEERING, SubAgentRole.LEAD_ORCHESTRATOR)
    assert binding is not None
    assert binding.primary_model is AnthropicModel.SONNET_4_6
    assert binding.qualifier is not None
    assert "Opus 4.6" in binding.qualifier


def test_model_binding_content_creation_reviewer_is_none() -> None:
    """Acceptance #5 — the content-creation reviewer cell is n/a → None."""
    assert model_binding(WorkloadClass.CONTENT_CREATION, SubAgentRole.REVIEWER) is None


def test_model_binding_pipeline_automation_evaluator_is_none() -> None:
    """Acceptance #5 — the pipeline-automation evaluator cell is n/a → None."""
    assert model_binding(WorkloadClass.PIPELINE_AUTOMATION, SubAgentRole.EVALUATOR) is None


def test_model_binding_research_generator_is_none() -> None:
    """Acceptance #5 — the research generator cell is n/a → None."""
    assert model_binding(WorkloadClass.RESEARCH, SubAgentRole.GENERATOR) is None


def test_lead_orchestrator_not_reducible_to_haiku() -> None:
    """Acceptance #6 — no lead/orchestrator cell binds to Haiku."""
    for workload in WorkloadClass:
        binding = model_binding(workload, SubAgentRole.LEAD_ORCHESTRATOR)
        assert binding is not None
        assert binding.primary_model is not AnthropicModel.HAIKU_4_5


def test_pre_hitl_escalation_order_three_steps() -> None:
    """Acceptance #7 — the pre-HITL staircase is the three-step C9 → C6 → C11 order."""
    assert PRE_HITL_ESCALATION_ORDER == (
        PreHITLEscalationStep.STEP_1_C9_BACKOFF,
        PreHITLEscalationStep.STEP_2_C6_MODEL_TIER_ESCALATION,
        PreHITLEscalationStep.STEP_3_C11_HITL,
    )


def test_model_tier_escalation_chain_ascending() -> None:
    """Acceptance #8 — the C6 escalation chain ascends Haiku → Sonnet → Opus 4.6 → Opus 4.7."""
    assert MODEL_TIER_ESCALATION_CHAIN == (
        AnthropicModel.HAIKU_4_5,
        AnthropicModel.SONNET_4_6,
        AnthropicModel.OPUS_4_6,
        AnthropicModel.OPUS_4_7,
    )
