"""Tests for U-CP-38 — 3-placement HITL enum + `hitl_gate` + `HITLPlacement`.

Acceptance-criterion coverage:
  #1 HITLPlacementKind 3 values   -> test_placement_kind_cardinality_three
  #2 HITL_PLACEMENT_TRIGGERS 3    -> test_placement_triggers_match_spec
  #3 hitl_gate 5 params           -> test_hitl_gate_signature_five_parameters
  #3 HITLResult 6 fields          -> test_hitl_result_six_fields
  #4 response palette / EDIT/RESPOND -> test_hitl_result_edit_and_respond_fields
  #5 response_palette is Set      -> test_response_palette_is_set
  #6 HITLPlacement 4 fields       -> test_hitl_placement_four_fields
  #6 multiple placements          -> test_multiple_placements_permitted
"""

from __future__ import annotations

import inspect

import pytest
from harness_core.identity import EntryID
from harness_cp.handoff_context import (
    ActionKind,
    HandoffContext,
    LedgerEntryRef,
    ProposedAction,
    RetryHistory,
    StateSummary,
)
from harness_cp.hitl_placement import (
    HITL_PLACEMENT_TRIGGERS,
    HITLPlacement,
    HITLPlacementKind,
    HITLResult,
    hitl_gate,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.topology_pattern import CascadePolicy

_PROPOSED = ProposedAction(action_kind=ActionKind.TOOL_CALL, payload={})
_LEDGER_REF = LedgerEntryRef(
    action_id="a0",  # type: ignore[arg-type]
    entry_hash="0" * 64,
    actor="lead",  # type: ignore[arg-type]
)
_STATE = StateSummary(
    relevant_entries=(),
    summary_text="s",
    summary_hash="0" * 64,
    idempotency_key="k0",  # type: ignore[arg-type]
    external_references=(),
)
_HANDOFF = HandoffContext(
    proposed_action=_PROPOSED,
    failed_attempts=(),
    alternatives_considered=(),
    state_summary=_STATE,
    audit_trail_link=_LEDGER_REF,
    retry_history=RetryHistory(attempts=(), retry_count=0),
)


def test_placement_kind_cardinality_three() -> None:
    assert len(list(HITLPlacementKind)) == 3
    assert {p.value for p in HITLPlacementKind} == {
        "pre-action",
        "sub-agent-boundary",
        "validator-escalation",
    }


def test_placement_triggers_match_spec() -> None:
    assert len(HITL_PLACEMENT_TRIGGERS) == 3
    assert {t.placement_kind for t in HITL_PLACEMENT_TRIGGERS} == set(HITLPlacementKind)


def test_hitl_gate_signature_six_parameters() -> None:
    # v1.10 §17.4 (U-CP-71): signature rewritten — async, 6 parameters
    # (placement, step, step_context, *, surface, palette, timeout);
    # returns HITLGateResult per spec §17.4 typed return envelope.
    sig = inspect.signature(hitl_gate)
    assert list(sig.parameters) == [
        "placement",
        "step",
        "step_context",
        "surface",
        "palette",
        "timeout",
    ]
    assert sig.return_annotation == "HITLGateResult"


def test_hitl_result_six_fields() -> None:
    assert set(HITLResult.model_fields) == {
        "response",
        "edited_proposal",
        "response_text",
        "timestamp",
        "audit_ledger_entry_id",
        "response_summary_hash",
    }


def test_hitl_result_edit_and_respond_fields() -> None:
    edit = HITLResult(
        response=HITLResponse.EDIT,
        edited_proposal=_PROPOSED,
        timestamp="2026-05-16T00:00:00Z",
        audit_ledger_entry_id=EntryID("e0"),
        response_summary_hash="0" * 64,
    )
    assert edit.edited_proposal is _PROPOSED
    respond = HITLResult(
        response=HITLResponse.RESPOND,
        response_text="continue",
        timestamp="2026-05-16T00:00:00Z",
        audit_ledger_entry_id=EntryID("e1"),
        response_summary_hash="0" * 64,
    )
    assert respond.response_text == "continue"


def test_palette_annotation_is_frozenset() -> None:
    # v1.10 §17.4 (U-CP-71): the palette parameter is now `frozenset` per
    # spec §17.4 canonical signature (was `set` at v1.9 §17.1.1).
    sig = inspect.signature(hitl_gate)
    assert sig.parameters["palette"].annotation == "frozenset[HITLResponse] | None"


def test_hitl_placement_four_fields() -> None:
    assert set(HITLPlacement.model_fields) == {
        "position",
        "tool_filter",
        "cascade_policy",
        "timeout",
    }


def test_multiple_placements_permitted() -> None:
    placements = [
        HITLPlacement(position=HITLPlacementKind.PRE_ACTION),
        HITLPlacement(
            position=HITLPlacementKind.VALIDATOR_ESCALATION,
            cascade_policy=CascadePolicy.PAUSE,
        ),
    ]
    assert len(placements) == 2


def test_hitl_gate_is_signature_only_for_runtime_composer() -> None:
    # v1.10 §17.4 (U-CP-71): the canonical CP-side signature is signature-
    # only; production gate body composition lives at C-RT-18 §14.8
    # (`RuntimeHITLGateComposer`). Calling the CP-side signature directly
    # raises NotImplementedError with the composer pointer.
    import asyncio

    from harness_cp.hitl_placement import (
        HITLPlacement,
    )
    from harness_cp.hitl_placement import (
        HITLPlacementKind as _HPK,
    )
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep

    class _Surface:
        async def ask(self, *args: object, **kwargs: object) -> object:
            return None

    from harness_as.sandbox_tier import SandboxTier
    from harness_core.identity import StepID
    from harness_cp.gate_level_rule import GateLevel
    from harness_cp.workflow_driver_types import StepExecutionContext
    from harness_is.state_ledger_entry_schema import Actor, ActorClass

    placement = HITLPlacement(position=_HPK.PRE_ACTION)
    step = WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )
    step_context = StepExecutionContext(
        workflow_id="test",
        parent_action_id="workflow:test:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.AGENT, actor_id="test"),
        parent_entry_hash="",
        parent_idempotency_key="k",
        tenant_id=None,
        step_index=0,
    )
    with pytest.raises(NotImplementedError, match="RuntimeHITLGateComposer"):
        asyncio.run(
            hitl_gate(
                placement=placement,
                step=step,
                step_context=step_context,
                surface=_Surface(),
            )
        )


def test_loosenable_placement_kind_cardinality_one_sub_agent_boundary() -> None:
    """B-HITL-PLACEMENT-PER-STEP-LOOSEN (CP spec v1.53 §6.2): the loosenable set is a
    CLOSED one-member enum — SUB_AGENT_BOUNDARY ONLY. PRE_ACTION (the §19.1 floor-
    evaluation bypass-seam) and VALIDATOR_ESCALATION (the §14.15-path wrong-layer)
    are STRUCTURALLY unrepresentable (foreclosed at the type, not a runtime guard).
    The member value equals HITLPlacementKind.SUB_AGENT_BOUNDARY.value verbatim so a
    removed_placements set keys cleanly against a placement's position."""
    from harness_cp.hitl_placement import HITLPlacementKind, LoosenablePlacementKind

    assert set(LoosenablePlacementKind) == {LoosenablePlacementKind.SUB_AGENT_BOUNDARY}
    assert (
        LoosenablePlacementKind.SUB_AGENT_BOUNDARY.value
        == HITLPlacementKind.SUB_AGENT_BOUNDARY.value
    )
    assert "pre-action" not in {k.value for k in LoosenablePlacementKind}
    assert "validator-escalation" not in {k.value for k in LoosenablePlacementKind}
