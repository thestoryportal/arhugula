"""Tests for U-CP-41 — both-by-tier overlay + two-agent-observer (C-CP-18).

Acceptance-criterion coverage:
  #1 BOTH_BY_TIER_OVERLAY 3 props -> test_both_by_tier_overlay_three_properties
  #2 evaluate_both_by_tier_overlay -> test_overlay_outcome_per_tool_tier
  #3 TWO_AGENT_OBSERVER 3 props    -> test_two_agent_observer_three_properties
  #4 persona-tier-binding 5-step   -> test_persona_tier_binding_selection
  #5 VerifierResult 3 fields       -> test_verifier_result_three_fields_cp_18_4
                                      test_verifier_verdict_cardinality_two
                                      test_verifier_fail_class_in_cp_21_5_set
  #6 OverlayResolution 3 fields    -> test_overlay_resolution_three_fields_cp_18_3
                                      test_overlay_outcome_cardinality_three
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harness_as.sandbox_tier import BlastRadiusTier
from harness_core import DeploymentSurface, PersonaTier, WorkloadClass
from harness_cp.both_by_tier_overlay import (
    BOTH_BY_TIER_OVERLAY,
    TWO_AGENT_OBSERVER,
    BothByTierOverlay,
    OverlayOutcome,
    OverlayResolution,
    PersonaTierBindingSelectionInput,
    ToolTier,
    VerifierResult,
    VerifierVerdict,
    compose_persona_tier_binding_selection,
    dispatch_two_agent_observer,
    evaluate_both_by_tier_overlay,
)
from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import ActionKind, ProposedAction
from harness_cp.persona_engine_hitl_matrix import matrix_cell_for
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass

# The C-CP-21 §21.5 5-value validator.fail.class set.
_CP_21_5_FAIL_CLASSES = {c.value for c in ValidatorRetryExitClass}


def _action() -> ProposedAction:
    payload: Mapping[str, Any] = {}
    return ProposedAction(action_kind=ActionKind.TOOL_CALL, payload=payload, brief=None)


def _cell():
    return matrix_cell_for(PersonaTier.SOLO_DEVELOPER, EngineClass.PURE_PATTERN_NO_ENGINE)


def test_both_by_tier_overlay_three_properties() -> None:
    """#1 — BOTH_BY_TIER_OVERLAY declares three §18.3 properties."""
    assert set(BothByTierOverlay.model_fields) == {
        "scope",
        "composition_rule",
        "audit_composition",
    }
    assert "auto" in BOTH_BY_TIER_OVERLAY.scope
    assert "does NOT replace" in BOTH_BY_TIER_OVERLAY.composition_rule


def test_overlay_outcome_per_tool_tier() -> None:
    """#2 — evaluate_both_by_tier_overlay maps tool tier to overlay outcome."""
    cell = _cell()
    auto = evaluate_both_by_tier_overlay(ToolTier.AUTO, cell)
    assert auto.overlay_outcome is OverlayOutcome.AUTO_NO_GATE
    assert auto.gate_invoked is False

    ask = evaluate_both_by_tier_overlay(ToolTier.ASK, cell)
    assert ask.overlay_outcome is OverlayOutcome.ASK_GATE_VIA_SYNCHRONY
    assert ask.gate_invoked is True

    deny = evaluate_both_by_tier_overlay(ToolTier.DENY, cell)
    assert deny.overlay_outcome is OverlayOutcome.DENY_STRUCTURAL_REJECT
    assert deny.palette_restricted is True


def test_two_agent_observer_three_properties() -> None:
    """#3 — TWO_AGENT_OBSERVER declares three §18.4 properties + predicate."""
    assert "Tier-3" in TWO_AGENT_OBSERVER.trigger_condition
    assert "validator-escalation" in TWO_AGENT_OBSERVER.composition_with_primary
    assert "subagent.span[verifier]" in TWO_AGENT_OBSERVER.audit_composition
    assert TWO_AGENT_OBSERVER.applicable_cell_predicate(_cell()) is True


def test_persona_tier_binding_selection() -> None:
    """#4 — compose_persona_tier_binding_selection runs the §18.5 procedure."""
    result = compose_persona_tier_binding_selection(
        PersonaTierBindingSelectionInput(
            operator_persona_tier=PersonaTier.SOLO_DEVELOPER,
            operator_deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            operator_engine_choice=EngineClass.PURE_PATTERN_NO_ENGINE,
            operator_workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        )
    )
    assert result.composition_with_c_cp_19 == "U-CP-43"
    assert result.composition_with_c_cp_20 == "U-CP-42"
    assert result.composition_with_c_cp_21 == "U-CP-47"
    assert result.composition_with_c_cp_22 == "U-CP-49"
    assert isinstance(result.binding_valid, bool)


def test_verifier_result_three_fields_cp_18_4() -> None:
    """#5 — VerifierResult declares exactly three fields."""
    assert set(VerifierResult.model_fields) == {
        "verifier_verdict",
        "validator_fail_class",
        "verifier_span_id",
    }


def test_verifier_verdict_cardinality_two() -> None:
    """#5 — VerifierVerdict is a 2-value enum."""
    assert len(VerifierVerdict) == 2
    assert {v.value for v in VerifierVerdict} == {"agree", "disagree"}


def test_verifier_fail_class_in_cp_21_5_set() -> None:
    """#5 — a verifier fail class, when present, is in the §21.5 5-value set."""
    tier3 = dispatch_two_agent_observer(_action(), BlastRadiusTier.EXTERNAL_REVERSIBLE)
    assert tier3.verifier_span_id == "subagent.span[verifier]"
    if tier3.validator_fail_class is not None:
        assert tier3.validator_fail_class in _CP_21_5_FAIL_CLASSES
    # Sub-Tier-3 actions admit no verification.
    sub = dispatch_two_agent_observer(_action(), BlastRadiusTier.READ_ONLY)
    assert sub.verifier_span_id == ""


def test_overlay_resolution_three_fields_cp_18_3() -> None:
    """#6 — OverlayResolution declares exactly three fields."""
    assert set(OverlayResolution.model_fields) == {
        "overlay_outcome",
        "gate_invoked",
        "palette_restricted",
    }


def test_overlay_outcome_cardinality_three() -> None:
    """#6 — OverlayOutcome is a 3-value enum."""
    assert len(OverlayOutcome) == 3
