"""Stage-5 factory for R-CXA-2 CP->IS producer loops.

Materializes the model-driven HITL tool-loop primitive and the engine recovery
loop primitive against the CP->IS wiring that stage 3b has already bound.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_core.identity import StepID
from harness_cp.cp_shared_types import ActorIdentity, MCPTrustTier, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel, GateLevelInput
from harness_cp.handoff_context import StateSummary
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.cp_is_wiring import RuntimeCpIsWiring
from harness_runtime.lifecycle.engine_recovery_loop import RuntimeEngineRecoveryLoop
from harness_runtime.lifecycle.hitl_placement import RuntimeHITLPlacementRegistry
from harness_runtime.lifecycle.hitl_required_consumption import evaluate_hitl_required
from harness_runtime.lifecycle.hitl_tool_loop import (
    HITLGateDecision,
    HITLToolLoopContext,
    ModelToolCall,
    RuntimeHITLToolLoop,
)
from harness_runtime.lifecycle.reconciler_pause_resume_substrate import (
    ReconcilerEnginePauseResumeSubstrate,
)
from harness_runtime.lifecycle.wal_segment_pause_resume_substrate import (
    WALSegmentEnginePauseResumeSubstrate,
)
from harness_runtime.types import RuntimeConfig

__all__ = [
    "R_CXA_2_MODEL_TOOL_LOOP_PARENT_GATE_LEVEL",
    "RCXA2ProducerLoopMaterializeError",
    "RCXA2ProducerLoopStage",
    "materialize_r_cxa_2_producer_loop_stage",
]


R_CXA_2_MODEL_TOOL_LOOP_PARENT_GATE_LEVEL = GateLevel.AUTO
"""Synthetic TOOL_STEP parent gate used by the model-tool-call adapter.

The HITL decision has already fired in ``RuntimeHITLToolLoop`` before this
adapter dispatches an approved call. The wrapped C-RT-19 dispatcher does not
consume this field at HEAD; keeping it at AUTO avoids double-counting a gate
level at the synthetic tool-step bridge.
"""


class RCXA2ProducerLoopMaterializeError(Exception):
    """Stage-5 materialization failed for the R-CXA-2 producer loop bindings."""


@dataclass(frozen=True, slots=True)
class RCXA2ProducerLoopStage:
    """Frozen stage-5 result carrying the two R-CXA-2 runtime producers."""

    hitl_tool_loop: RuntimeHITLToolLoop
    engine_recovery_loop: RuntimeEngineRecoveryLoop


@dataclass(frozen=True, slots=True)
class _GateLevelHITLRequiredEvaluator:
    """Runtime default HITL-required evaluator for model-emitted tool calls."""

    per_tool_gate_level: GateLevel = GateLevel.AUTO
    blast_radius_tier: BlastRadiusTier = BlastRadiusTier.READ_ONLY
    # Post-U-CP-98 (CP spec v1.35 §19.1.2) this `mcp_trust_tier` is now a COMPOSED
    # gate axis (`L0→DENY`). It is deliberately the conservative `LEVEL_0` —
    # a model-emitted tool call with no resolved server trust defaults to untrusted
    # → HITL-required. The change is behavior-preserving: the persona floor is ASK
    # for all three tiers, so this evaluator already returned True for every input;
    # the L0 floor (DENY) only re-grounds that True in MCP-trust. (Distinct from the
    # composer's HOST-LESS gate sites, which feed the L3 no-floor default per
    # U-RT-131 — those have no owning host; a model tool call references a server.)
    mcp_trust_tier: MCPTrustTier = MCPTrustTier.LEVEL_0_REFUSE_REMOTE

    def __call__(self, call: ModelToolCall, context: HITLToolLoopContext) -> bool:
        _ = call
        return evaluate_hitl_required(
            GateLevelInput(
                per_tool_gate_level=self.per_tool_gate_level,
                persona_tier=context.persona_tier,
                blast_radius_tier=self.blast_radius_tier,
                mcp_trust_tier=self.mcp_trust_tier,
            )
        )


@dataclass(frozen=True, slots=True)
class _AskUserQuestionGateAdapter:
    """Adapt the stage-5 ask-user surface to the model tool-loop gate protocol."""

    ask_user_question_surface: Any
    timeout_seconds: float | None = None

    async def decide(
        self,
        *,
        call: ModelToolCall,
        context: HITLToolLoopContext,
    ) -> HITLGateDecision:
        _ = context
        result = await self.ask_user_question_surface.ask(
            prompt=f"HITL tool call {call.tool} on {call.server}",
            options=tuple(sorted(HITLResponse)),
            timeout=self.timeout_seconds,
        )
        edited_arguments = None
        if result.response is HITLResponse.EDIT and result.edited_proposal:
            edited_arguments = _parse_edited_arguments(result.edited_proposal)
        return HITLGateDecision(
            response=result.response,
            edited_arguments=edited_arguments,
            response_text=result.response_text,
        )


@dataclass(frozen=True, slots=True)
class _RuntimeToolDispatcherModelCallAdapter:
    """Project approved model tool calls onto the existing C-RT-19 dispatcher."""

    tool_dispatcher: Any
    tenant_id: str | None

    async def dispatch(
        self,
        call: ModelToolCall,
        context: HITLToolLoopContext,
    ) -> Mapping[str, Any]:
        synthetic_step_id = f"{context.step_id}:tool:{call.tool_call_id}"
        step = WorkflowStep(
            step_id=StepID(synthetic_step_id),
            step_kind=StepKind.TOOL_STEP,
            step_payload={
                "tool_id": call.tool,
                "tool_args": dict(call.arguments),
            },
        )
        binding = StepEffectiveBinding(
            step_id=synthetic_step_id,
            model_binding=ModelBinding(provider=call.provider, model=call.model),
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            override_applied=False,
            persona_tier=context.persona_tier,
        )
        step_context = StepExecutionContext(
            workflow_id=context.workflow_id,
            parent_action_id=f"workflow:{context.workflow_id}:step:{context.step_id}",
            parent_gate_level=R_CXA_2_MODEL_TOOL_LOOP_PARENT_GATE_LEVEL,
            parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            parent_actor=Actor(
                actor_class=ActorClass.AGENT,
                actor_id=str(context.actor),
            ),
            parent_entry_hash="",
            parent_idempotency_key=(
                f"model-tool:{context.workflow_id}:{context.step_id}:{call.tool_call_id}"
            ),
            tenant_id=self.tenant_id,
            step_index=0,
        )
        return await self.tool_dispatcher.dispatch(
            binding,
            step,
            step_context=step_context,
        )


def materialize_r_cxa_2_producer_loop_stage(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
) -> RCXA2ProducerLoopStage:
    """Bind R-CXA-2 producer loops at stage 5 LOOP_INIT."""
    if "cp_is_wiring" not in ctx.cxa_stages:
        raise RCXA2ProducerLoopMaterializeError(
            "ctx.cxa_stages['cp_is_wiring'] missing at stage 5; stage 3b "
            "must materialize CP->IS wiring before R-CXA-2 producer loops"
        )
    if ctx.hitl_registry is None:
        raise RCXA2ProducerLoopMaterializeError(
            "ctx.hitl_registry is None at stage 5; stage 3b must populate HITL placement"
        )
    if ctx.ask_user_question_surface is None:
        raise RCXA2ProducerLoopMaterializeError(
            "ctx.ask_user_question_surface is None at stage 5; HITL gate adapter "
            "requires the stage-5 ask-user surface"
        )
    if ctx.tool_dispatcher is None:
        raise RCXA2ProducerLoopMaterializeError(
            "ctx.tool_dispatcher is None at stage 5; model tool calls dispatch "
            "through the existing C-RT-19 tool dispatcher"
        )

    wiring = cast(RuntimeCpIsWiring, ctx.cxa_stages["cp_is_wiring"].wiring)
    placement_registry = cast(RuntimeHITLPlacementRegistry, ctx.hitl_registry)
    actor = _actor_identity_from_context(ctx)

    hitl_tool_loop = RuntimeHITLToolLoop(
        wiring=wiring,
        placement_registry=placement_registry,
        hitl_required=_GateLevelHITLRequiredEvaluator(),
        gate=_AskUserQuestionGateAdapter(ctx.ask_user_question_surface),
        dispatcher=_RuntimeToolDispatcherModelCallAdapter(
            tool_dispatcher=ctx.tool_dispatcher,
            tenant_id=config.tenant_id,
        ),
    )
    # U-RT-124 (R-FS-1 E-impl-3c) — R-CXA-2 engine-layer activation, engine-class-aware.
    # The engine recovery loop binds ONE durable substrate per engine class that
    # fires it (O-RT-4): WAL_SEGMENT → the U-RT-121 WAL segment-log substrate
    # (U-RT-122, E-impl-2); RECONCILER_LOOP → the U-RT-123 etcd-style reconciler
    # substrate (E-impl-3b). Each firing call passes the workflow's `engine_class`
    # (the U-CP-95 WAL + U-CP-97 reconciler driver branches are already gated on
    # it), so `ctx.engine_recovery_loop.capture_pause`/`.attempt_resume` persist
    # `cp.pause-captured` / `cp.resume-attempted` against the engine class's OWN
    # crash-survivable store — bringing the R-CXA-2 CP→IS engine-layer seam LIVE in
    # production for BOTH durable engine classes. The per-engine-class map is the
    # single source of routing truth: DISTINCT journal directories mean a reconciler
    # pause can never land in the WAL segment-log, nor a WAL pause in the reconciler
    # store (the U-RT-124 no-cross-contamination AC, enforced by construction). Each
    # store lives under the operator's repository_root (no new RuntimeConfig field;
    # §7.4 substrate-location impl-discretion) and is created lazily on first capture.
    # Non-firing engine classes (the 3 non-DURABLE_ASYNC classes) never invoke the
    # loop (the driver gates on engine_class), so no files are written for them.
    engine_recovery_loop = RuntimeEngineRecoveryLoop(
        wiring=wiring,
        substrate_by_engine_class={
            EngineClass.WAL_SEGMENT: WALSegmentEnginePauseResumeSubstrate(
                journal_dir=config.repository_root / ".harness" / "engine-recovery-segments",
                state_summary_provider=_default_engine_state_summary,
            ),
            EngineClass.RECONCILER_LOOP: ReconcilerEnginePauseResumeSubstrate(
                journal_dir=config.repository_root / ".harness" / "engine-recovery-reconciler",
                state_summary_provider=_default_engine_state_summary,
            ),
        },
        actor=actor,
    )
    ctx.hitl_tool_loop = hitl_tool_loop
    ctx.engine_recovery_loop = engine_recovery_loop
    return RCXA2ProducerLoopStage(
        hitl_tool_loop=hitl_tool_loop,
        engine_recovery_loop=engine_recovery_loop,
    )


def _actor_identity_from_context(ctx: _MutableHarnessContext) -> ActorIdentity:
    actor = getattr(ctx.ledger_writer, "actor", None)
    actor_id = getattr(actor, "actor_id", "harness-runtime")
    return ActorIdentity(str(actor_id))


def _default_engine_state_summary() -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text="",
        summary_hash="0" * 64,
        idempotency_key=Identifier("r-cxa-2-engine-state"),
        external_references=(),
    )


def _parse_edited_arguments(edited_proposal: str) -> Mapping[str, Any]:
    parsed = json.loads(edited_proposal)
    if not isinstance(parsed, Mapping):
        raise ValueError("HITL EDIT response must be a JSON object of tool arguments")
    return cast(Mapping[str, Any], parsed)
