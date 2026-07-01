"""Runtime model-driven HITL tool-loop producer for R-CXA-2.

Authority: C-CP-17 HITL placement / tool-call rewriting and U-CP-77
`cp.hitl-tool-call-rewriting`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from harness_core import PersonaTier
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import ActionKind, ProposedAction
from harness_cp.hitl_as_tool_call_rewriting import RewrittenToolCall
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_is.state_ledger_write import WriteResult

from harness_runtime.lifecycle.cp_is_wiring import RuntimeCpIsWiring
from harness_runtime.lifecycle.hitl_placement import RuntimeHITLPlacementRegistry


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """Provider-neutral model-emitted tool call."""

    tool_call_id: str
    tool: str
    server: str
    arguments: Mapping[str, Any]
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class HITLToolLoopContext:
    """Stable workflow and HITL placement context for one model tool turn."""

    workflow_id: str
    step_id: str
    persona_tier: PersonaTier
    cell_synchrony_class: SynchronyClass
    cross_trust_boundary_state: CrossTrustBoundaryState
    actor: ActorIdentity


@dataclass(frozen=True, slots=True)
class HITLGateDecision:
    """Gate outcome consumed by the tool loop before dispatch."""

    response: HITLResponse
    edited_arguments: Mapping[str, Any] | None = None
    response_text: str | None = None


@dataclass(frozen=True, slots=True)
class HITLToolLoopCallResult:
    """Per-tool-call loop result."""

    tool_call_id: str
    rewritten_tool_call: RewrittenToolCall
    rewrite_write_result: WriteResult | None
    gate_response: HITLResponse | None
    dispatched: bool
    dispatch_result: Mapping[str, Any] | None


class HITLToolDispatcher(Protocol):
    """Dispatch surface for an approved model tool call."""

    async def dispatch(
        self,
        call: ModelToolCall,
        context: HITLToolLoopContext,
    ) -> Mapping[str, Any]: ...


class HITLGateAdapter(Protocol):
    """Gate surface opened after a rewrite and before tool dispatch."""

    async def decide(
        self,
        *,
        call: ModelToolCall,
        context: HITLToolLoopContext,
    ) -> HITLGateDecision: ...


type HITLRequiredEvaluator = Callable[[ModelToolCall, HITLToolLoopContext], bool]


@dataclass(frozen=True, slots=True)
class RuntimeHITLToolLoop:
    """Iterate model-emitted tool calls through HITL rewrite and dispatch."""

    wiring: RuntimeCpIsWiring
    placement_registry: RuntimeHITLPlacementRegistry
    hitl_required: HITLRequiredEvaluator
    gate: HITLGateAdapter
    dispatcher: HITLToolDispatcher

    async def run_tool_calls(
        self,
        calls: Sequence[ModelToolCall],
        context: HITLToolLoopContext,
    ) -> tuple[HITLToolLoopCallResult, ...]:
        """Process one journaled model turn's tool calls in provider order."""
        results: list[HITLToolLoopCallResult] = []
        for call in calls:
            proposed_action = _proposed_action_from_tool_call(call)
            required = self.hitl_required(call, context)
            rewritten = self.placement_registry.rewrite_tool_call(
                tool=call.tool,
                server=call.server,
                persona_tier=context.persona_tier,
                proposed_action=proposed_action,
                cell_synchrony_class=context.cell_synchrony_class,
                cross_trust_boundary_state=context.cross_trust_boundary_state,
                hitl_required=required,
            )

            write_result: WriteResult | None = None
            gate_response: HITLResponse | None = None
            dispatch_call = call
            if rewritten.hitl_required:
                if rewritten.variant is None:
                    raise RuntimeError("HITL-required rewrite must include a semantic variant")
                write_result = await self.wiring.emit_hitl_tool_call_rewriting_state_ledger_entry(
                    workflow_id=context.workflow_id,
                    step_id=context.step_id,
                    tool_call_id=call.tool_call_id,
                    semantic_variant_binding_id=rewritten.variant.value,
                    rewritten_tool_call=rewritten,
                    actor=context.actor,
                )
                gate_decision = await self.gate.decide(call=call, context=context)
                gate_response = gate_decision.response
                if gate_decision.response is HITLResponse.REJECT:
                    results.append(
                        HITLToolLoopCallResult(
                            tool_call_id=call.tool_call_id,
                            rewritten_tool_call=rewritten,
                            rewrite_write_result=write_result,
                            gate_response=gate_response,
                            dispatched=False,
                            dispatch_result=None,
                        )
                    )
                    continue
                if gate_decision.response is HITLResponse.EDIT:
                    dispatch_call = ModelToolCall(
                        tool_call_id=call.tool_call_id,
                        tool=call.tool,
                        server=call.server,
                        arguments=gate_decision.edited_arguments or call.arguments,
                        provider=call.provider,
                        model=call.model,
                    )
                elif gate_decision.response is HITLResponse.RESPOND:
                    results.append(
                        HITLToolLoopCallResult(
                            tool_call_id=call.tool_call_id,
                            rewritten_tool_call=rewritten,
                            rewrite_write_result=write_result,
                            gate_response=gate_response,
                            dispatched=False,
                            dispatch_result={"response_text": gate_decision.response_text or ""},
                        )
                    )
                    continue

            dispatch_result = await self.dispatcher.dispatch(dispatch_call, context)
            results.append(
                HITLToolLoopCallResult(
                    tool_call_id=call.tool_call_id,
                    rewritten_tool_call=rewritten,
                    rewrite_write_result=write_result,
                    gate_response=gate_response,
                    dispatched=True,
                    dispatch_result=dispatch_result,
                )
            )
        return tuple(results)


def _proposed_action_from_tool_call(call: ModelToolCall) -> ProposedAction:
    return ProposedAction(
        action_kind=ActionKind.TOOL_CALL,
        payload={
            "tool_name": call.tool,
            "tool_args": dict(call.arguments),
            "server": call.server,
            "tool_call_id": call.tool_call_id,
            "provider": call.provider,
            "model": call.model,
        },
        brief=None,
    )
