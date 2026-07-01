"""U-CP-71 — `hitl_gate` canonical signature materialization tests.

ACs per Implementation_Plan_Control_Plane_v2_15.md U-CP-71. Spec contract:
Spec_Control_Plane_v1_10.md §17.4 (preserved at v1.11).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core.identity import StepID
from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_placement import (
    DEFAULT_HITL_PALETTE,
    AskUserQuestionSurface,
    HITLGateResult,
    HITLPlacement,
    HITLPlacementKind,
    hitl_gate,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass


def _placement() -> HITLPlacement:
    return HITLPlacement(position=HITLPlacementKind.PRE_ACTION)


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID("step-001"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )


def _step_context() -> StepExecutionContext:
    return StepExecutionContext(
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


class _Surface:
    """Minimal AskUserQuestionSurface — structural conformance."""

    async def ask(self, *args: Any, **kwargs: Any) -> Any:
        return None


def test_signature_closes_not_implemented_at_line_178() -> None:
    """AC #1 — original NotImplementedError closed. Surface assertion: the
    canonical signature accepts the new parameters; calling with surface
    set raises a NotImplementedError with the C-RT-18 composer pointer
    (production callers route through runtime). The historical
    "interface signature (C-CP-17 §17.1.1)" wording is gone."""
    sig = inspect.signature(hitl_gate)
    params = list(sig.parameters)
    assert params == ["placement", "step", "step_context", "surface", "palette", "timeout"]


def test_palette_default_is_c_cp_16_four_response_palette() -> None:
    """AC #2 — palette defaults to C-CP-16 §16.1 4-response palette."""
    assert DEFAULT_HITL_PALETTE == frozenset(
        {
            HITLResponse.APPROVE,
            HITLResponse.EDIT,
            HITLResponse.REJECT,
            HITLResponse.RESPOND,
        }
    )
    # The signature itself default-Nones palette; the canonical default
    # is exposed at module level for composer consumption.
    sig = inspect.signature(hitl_gate)
    assert sig.parameters["palette"].default is None


def test_surface_protocol_structural_satisfaction() -> None:
    """AC #3 — surface is injected per U-RT-60. The CP-side Protocol is
    runtime-checkable; the test surface satisfies it structurally."""
    surface = _Surface()
    assert isinstance(surface, AskUserQuestionSurface)


@pytest.mark.asyncio
async def test_raises_value_error_when_surface_none() -> None:
    """AC #5 — raises if surface=None."""
    with pytest.raises(ValueError, match="surface"):
        await hitl_gate(
            placement=_placement(),
            step=_step(),
            step_context=_step_context(),
            surface=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_raises_not_implemented_for_production_callers() -> None:
    """AC #4 — composer body owned by C-RT-18 §14.8 (existing); this unit
    is signature-only materialization. Production callers go through the
    runtime composer; calling the CP-side signature directly raises with
    the composer pointer."""
    with pytest.raises(NotImplementedError, match="RuntimeHITLGateComposer"):
        await hitl_gate(
            placement=_placement(),
            step=_step(),
            step_context=_step_context(),
            surface=_Surface(),
        )


def test_hitl_gate_result_envelope_shape() -> None:
    """HITLGateResult — typed return envelope per spec §17.4."""
    result = HITLGateResult(
        response=HITLResponse.APPROVE,
        edited_proposal=None,
        rejection_reason=None,
        response_text=None,
        response_latency_ms=42,
        timed_out=False,
    )
    assert result.response == HITLResponse.APPROVE
    assert result.response_latency_ms == 42
    assert result.timed_out is False


def test_hitl_gate_is_async() -> None:
    """AC #1 — signature is async per spec §17.4 canonical signature."""
    assert inspect.iscoroutinefunction(hitl_gate)
