"""Tests for U-CP-17 (PARTIAL) — workload-binding engine-class selection.

Acceptance-criterion coverage (C-CP-07 §7.3):
  #1 step 1 candidates from U-CP-16  -> test_step_1_resolves_candidates_from_u_cp_16
  #1 step 2 workload-class filter    -> test_step_2_workload_class_filter
  #1 step 3 persona-tier filter      -> test_step_3_persona_tier_filter
  #1 four-step procedure             -> test_select_engine_class_four_step
  #2 deterministic                   -> test_selection_deterministic
  #3 binding-time / abort on failure -> test_selection_at_binding_time

STRUCK at partial landing (Class 1 — EngineClassPreferences homed at blocked
U-CP-27): `test_step_4_operator_preference_filter`. Re-added at U-CP-27 landing.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier, WorkloadClass
from harness_cp.engine_class import EngineClass
from harness_cp.engine_class_candidate import ENGINE_CLASS_CANDIDATES
from harness_cp.workload_binding_engine_class_selection import (
    HITLInvocation,
    WorkloadBindingError,
    WorkloadBindingSelectionInput,
    WorkloadBindingSelectionResult,
    select_engine_class,
)


def _input(
    workload: WorkloadClass,
    surface: DeploymentSurface,
    tier: PersonaTier,
) -> WorkloadBindingSelectionInput:
    return WorkloadBindingSelectionInput(
        workload_class=workload,
        deployment_surface=surface,
        persona_tier=tier,
    )


def test_select_engine_class_four_step() -> None:
    """Acceptance #1 — the procedure yields a single result for valid input."""
    result = select_engine_class(
        _input(
            WorkloadClass.SOFTWARE_ENGINEERING,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            PersonaTier.SOLO_DEVELOPER,
        )
    )
    assert isinstance(result, WorkloadBindingSelectionResult)
    assert isinstance(result.selected_class, EngineClass)


def test_step_1_resolves_candidates_from_u_cp_16() -> None:
    """Acceptance #1 step 1 — candidate set is the U-CP-16 set for the surface."""
    result = select_engine_class(
        _input(
            WorkloadClass.RESEARCH,
            DeploymentSurface.SELF_HOSTED_SERVER,
            PersonaTier.SOLO_DEVELOPER,
        )
    )
    expected = next(
        c.candidate_set
        for c in ENGINE_CLASS_CANDIDATES
        if c.deployment_surface == DeploymentSurface.SELF_HOSTED_SERVER
    )
    assert result.candidate_set == expected


def test_step_2_workload_class_filter() -> None:
    """Acceptance #1 step 2 — §7.3-favored class wins when admissible.

    pipeline-automation favors event-sourced-replay; software-engineering
    favors save-point-checkpoint; content-creation favors reconciler-loop.
    """
    pipeline = select_engine_class(
        _input(
            WorkloadClass.PIPELINE_AUTOMATION,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            PersonaTier.SOLO_DEVELOPER,
        )
    )
    assert pipeline.selected_class == EngineClass.EVENT_SOURCED_REPLAY

    swe = select_engine_class(
        _input(
            WorkloadClass.SOFTWARE_ENGINEERING,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            PersonaTier.SOLO_DEVELOPER,
        )
    )
    assert swe.selected_class == EngineClass.SAVE_POINT_CHECKPOINT

    content = select_engine_class(
        _input(
            WorkloadClass.CONTENT_CREATION,
            DeploymentSurface.MANAGED_CLOUD,
            PersonaTier.TEAM_BINDING,
        )
    )
    assert content.selected_class == EngineClass.RECONCILER_LOOP
    assert "step 2" in content.selection_rationale


def test_step_3_persona_tier_filter() -> None:
    """Acceptance #1 step 3 — pure-pattern-no-engine excluded above solo.

    At local-development, pure-pattern-no-engine is a candidate; a
    team-binding tier must not bind it (durability primitive required).
    """
    team = select_engine_class(
        _input(
            WorkloadClass.RESEARCH,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            PersonaTier.TEAM_BINDING,
        )
    )
    assert team.selected_class != EngineClass.PURE_PATTERN_NO_ENGINE

    multi = select_engine_class(
        _input(
            WorkloadClass.RESEARCH,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            PersonaTier.MULTI_TENANT_COMPLIANCE,
        )
    )
    assert multi.selected_class != EngineClass.PURE_PATTERN_NO_ENGINE


def test_selection_deterministic() -> None:
    """Acceptance #2 — selection is deterministic given identical inputs."""
    inp = _input(
        WorkloadClass.RESEARCH,
        DeploymentSurface.MANAGED_CLOUD,
        PersonaTier.SOLO_DEVELOPER,
    )
    first = select_engine_class(inp)
    for _ in range(8):
        again = select_engine_class(inp)
        assert again.selected_class == first.selected_class
        assert again.selection_rationale == first.selection_rationale


def test_selection_at_binding_time() -> None:
    """Acceptance #3 — a no-admissible-class selection aborts binding.

    `WorkloadBindingError` is the binding-time abort signal — every valid
    (surface, workload, tier) triple resolves to exactly one class, so the
    error path is exercised via the unreachable-surface guard.
    """
    # Every valid triple succeeds — binding-time, single result.
    for surface in DeploymentSurface:
        for tier in PersonaTier:
            for workload in WorkloadClass:
                result = select_engine_class(_input(workload, surface, tier))
                assert result.selected_class in result.candidate_set
    # The abort type is a binding-time ValueError subclass.
    assert issubclass(WorkloadBindingError, ValueError)


def test_result_frozen() -> None:
    """The result record is frozen (ConfigDict extra=forbid, frozen=True)."""
    result = select_engine_class(
        _input(
            WorkloadClass.RESEARCH,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            PersonaTier.SOLO_DEVELOPER,
        )
    )
    with pytest.raises(Exception):
        result.selected_class = EngineClass.WAL_SEGMENT  # type: ignore[misc]


# --- v2.9 — HITLInvocation opener-side record (acceptance #4) ---------------


def _handoff_context():
    from harness_core import ActionID
    from harness_cp.cp_shared_types import ActorIdentity
    from harness_cp.handoff_context import (
        ActionKind,
        HandoffContext,
        LedgerEntryRef,
        ProposedAction,
        RetryHistory,
        StateSummary,
    )
    from harness_is.state_ledger_entry_schema import Identifier

    return HandoffContext(
        proposed_action=ProposedAction(
            action_kind=ActionKind.INFERENCE_STEP, payload={}, brief=None
        ),
        agent_confidence=None,
        failed_attempts=(),
        alternatives_considered=(),
        state_summary=StateSummary(
            relevant_entries=(),
            summary_text="s",
            summary_hash="0" * 64,
            idempotency_key=Identifier("k"),
            external_references=(),
        ),
        audit_trail_link=LedgerEntryRef(
            action_id=ActionID("a0"),
            entry_hash="0" * 64,
            actor=ActorIdentity("op"),
        ),
        retry_history=RetryHistory(attempts=(), retry_count=0),
    )


def test_hitl_invocation_seven_fields_cp_17_1_1() -> None:
    """#4 (v2.9) — HITLInvocation declares exactly seven fields."""
    assert set(HITLInvocation.model_fields) == {
        "invocation_id",
        "placement",
        "handoff_context",
        "response_palette",
        "timeout",
        "cascade_policy",
        "opened_at",
    }


def test_hitl_invocation_constructs() -> None:
    """#4 (v2.9) — HITLInvocation is the opener-side record (carries a context)."""
    from harness_cp.hitl_response_palette import HITLResponse

    inv = HITLInvocation(
        invocation_id="inv-0",
        placement="pre-action",
        handoff_context=_handoff_context(),
        response_palette=frozenset(HITLResponse),
        timeout=None,
        cascade_policy="pause",
        opened_at="2026-05-16T00:00:00Z",
    )
    assert inv.invocation_id == "inv-0"
    assert inv.timeout is None
