"""Stage 5 LOOP_INIT — override evaluator, topology dispatcher, lifecycle
emitter, LLM dispatcher, sub-agent dispatcher, step-kind registry.

Per `Spec_Harness_Runtime_v1.md` v1.2 §2 stage 5 post-conditions +
§14.5 C-RT-15 (LLM-dispatch composer) + v1.6 §14.7 C-RT-17 (sub-agent
dispatch composer + step-kind routing registry per U-RT-59). On success:
``ctx.override_evaluator``, ``ctx.topology_dispatcher``,
``ctx.lifecycle_emitter``, ``ctx.llm_dispatcher``,
``ctx.sub_agent_dispatcher``, and ``ctx.step_dispatchers`` are all
non-None.

Composer order is free across the first three (no intra-stage
dependencies). The LLM dispatcher depends on ``ctx.providers``
(stage 3a) + ``ctx.tracer_provider`` (stage 4 OD); both are populated
before stage 5 begins per the bootstrap traversal order at C-RT-01.

On stage 5 success the orchestrator drains its buffered
`BootstrapStageCompleteEvent` records for stages 0..5 through the
freshly-materialized emitter.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from harness_core.workload_class import WorkloadClass
from harness_cp.hitl_placement import HITLPlacementKind
from harness_cp.workflow_driver_types import StepKind
from harness_od.audit_ledger_types import SignatureAlgorithm

from harness_runtime.bootstrap.factories.managed_agents_dispatcher_factory import (
    materialize_managed_agents_dispatcher_stage,
)
from harness_runtime.bootstrap.factories.memory_tool_registry_factory import (
    materialize_memory_tool_registry_stage,
)
from harness_runtime.bootstrap.factories.pause_resume_protocol_factory import (
    materialize_pause_resume_protocol_stage,
)
from harness_runtime.bootstrap.factories.r_cxa_2_producer_loop_factory import (
    materialize_r_cxa_2_producer_loop_stage,
)
from harness_runtime.bootstrap.factories.runtime_tool_dispatcher_factory import (
    materialize_runtime_tool_dispatcher_stage,
)
from harness_runtime.bootstrap.factories.skill_activation_emitter_factory import (
    materialize_skill_activation_emitter_stage,
)
from harness_runtime.bootstrap.factories.webhook_delivery_composer_factory import (
    materialize_webhook_delivery_composer_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.child_workflow_runner import compose_child_workflow_runner
from harness_runtime.lifecycle.engine_output_store import (
    EngineOutputStore,
    engine_output_dir_for,
)
from harness_runtime.lifecycle.hitl_gate_composer import RuntimeHITLGateComposer
from harness_runtime.lifecycle.inter_step_output_channel import RunScopedInterStepOutputChannel
from harness_runtime.lifecycle.lifecycle_emitter import materialize_lifecycle_emitter_stage
from harness_runtime.lifecycle.llm_dispatch import (
    LLMDispatchBindError,
    materialize_llm_dispatcher_stage,
)
from harness_runtime.lifecycle.mcp_backed_ask_user_question_surface import (
    materialize_mcp_backed_ask_user_question_surface_stage,
)
from harness_runtime.lifecycle.override_evaluator import materialize_override_evaluator_stage
from harness_runtime.lifecycle.post_join_synthesis_dispatch import (
    PostJoinSynthesisStepDispatcher,
)
from harness_runtime.lifecycle.procedural_tier_snapshot import (
    make_procedural_tier_snapshot_resolver,
)
from harness_runtime.lifecycle.resume_context_holder import ResumeContextHolder
from harness_runtime.lifecycle.retry_breaker_fallback import (
    materialize_retry_breaker_fallback_dispatcher_stage,
)
from harness_runtime.lifecycle.step_blast_radius import make_step_blast_radius_resolver
from harness_runtime.lifecycle.step_dispatchers import StepKindDispatcherRegistry
from harness_runtime.lifecycle.step_mcp_trust_tier import make_step_mcp_trust_tier_resolver
from harness_runtime.lifecycle.sub_agent_dispatch import RuntimeSubAgentDispatcher
from harness_runtime.lifecycle.sync_dispatcher_facade import (
    materialize_sync_dispatcher_facade,
)
from harness_runtime.lifecycle.topology_dispatcher import materialize_topology_dispatcher_stage
from harness_runtime.types import HarnessContext, RuntimeConfig

__all__ = ["execute"]


class _NoInferenceDispatcher:
    """Fail-loud null-object bound as the LLM-dispatch core for a non-inference
    (tool-only) workflow per runtime spec v1.47 §2.1.

    A non-inference workflow bootstraps provider-free (`ctx.providers` may be
    empty), so the provider-backed `materialize_llm_dispatcher_stage` — the only
    stage-5 surface that consumes `ctx.providers` — is not constructed. This
    sentinel takes its place as the bare dispatch core, satisfying the
    non-optional C-RT-04 `ctx.llm_dispatcher` carrier (+ `_REQUIRED_FIELDS`)
    without widening the cleared field type. It is **unreachable**: stage 5
    omits the `INFERENCE_STEP` / `SUB_AGENT_DISPATCH` rows from the
    step-dispatcher registry, so the CP driver raises
    `StepKindDispatcherNotBoundError` before ever reaching this object. The
    raise here is a deeper defensive backstop.
    """

    async def dispatch(self, binding: Any, step: Any, *, step_context: Any) -> Mapping[str, Any]:
        _ = (binding, step, step_context)
        raise LLMDispatchBindError(
            "INFERENCE dispatch reached the non-inference sentinel — the "
            "workflow bootstrapped provider-free (runtime spec v1.47 §2.1, "
            "requires_inference=False) yet an INFERENCE_STEP / "
            "SUB_AGENT_DISPATCH was dispatched. Unreachable through the "
            "step-dispatcher registry (those rows are omitted); reaching it "
            "indicates a registry-binding defect."
        )


_NO_INFERENCE_DISPATCHER = _NoInferenceDispatcher()


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate stage 5 LOOP_INIT fields on `ctx`."""

    override = materialize_override_evaluator_stage(config)
    ctx.override_evaluator = override.evaluator

    topology = materialize_topology_dispatcher_stage(config)
    ctx.topology_dispatcher = topology.dispatcher

    emitter = materialize_lifecycle_emitter_stage(config)
    ctx.lifecycle_emitter = emitter.emitter

    # LLM dispatcher (U-RT-52, C-RT-15) — depends on providers (stage 3a)
    # + tracer_provider (stage 4 OD). Both are populated by their declared
    # stages before stage 5 executes per C-RT-01 traversal order. The
    # factory raises ``LLMDispatchBindError`` if providers is empty —
    # surfaces as a stage-5 failure that the orchestrator's reverse-order
    # rollback handles.
    providers = ctx.providers
    if providers is None:
        raise LLMDispatchBindError(
            "ctx.providers is None at stage 5 — stage 3a CP_CLIENTS "
            "did not populate the providers map (bootstrap-orchestrator "
            "defect or stage-3a failure not surfaced before stage 5)"
        )
    tracer_provider = ctx.tracer_provider
    if tracer_provider is None:
        raise LLMDispatchBindError(
            "ctx.tracer_provider is None at stage 5 — stage 4 OD did "
            "not populate the tracer provider"
        )
    # `ctx.tracer_provider` is typed ``object`` per C-RT-04 (the spec
    # defers OTel-SDK type adoption); cast at this site to the
    # composer's structural shape.
    # U-OD-38: cost-attribution substrate is required at composer
    # construction per AC #1. Stage 4 OD populates ctx.cost_chain +
    # ctx.audit_writer; PRICE_TABLE_REF is sourced from RATE_TABLE_V1
    # default per §C-OD-28.3 (operator override via bootstrap config is a
    # future arc — ctx.rate_table extension on HarnessContext).
    from harness_od.rate_table_v1 import RATE_TABLE_V1

    if ctx.cost_chain is None or ctx.audit_writer is None:
        raise LLMDispatchBindError(
            "ctx.cost_chain / ctx.audit_writer is None at stage 5 — stage 4 "
            "OD did not populate the cost-attribution substrate required by "
            "U-OD-38 (C-OD-26.1 + C-OD-26.2 row 'llm_dispatch')."
        )
    # U-RT-80 (C-RT-22 §14.12.3): Memory tool storage-backend registry.
    # Construct BEFORE the LLM dispatcher so U-RT-81's C-RT-15 §14.5.1
    # callback-injection composer-step can be threaded the registry +
    # deployment_surface at construction time. Ordering within stage 5
    # LOOP_INIT is arbitrary per spec §14.12.3.
    await materialize_memory_tool_registry_stage(config, ctx)

    # U-RT-100 — SkillActivationSpanEmitter materialization per runtime spec
    # v1.32 §14.17.3. Operator-opt-in MVP per Reading B Q1=(B): factory returns
    # None when config.skill_activation_hook_config is None (operator opt-out,
    # production-default); returns a bound emitter when opt-in. Materialized
    # BEFORE bare_dispatcher so the per-LLM-dispatch hook-2 binding site (§14.17.2)
    # can be threaded into RuntimeLLMDispatcher construction.
    ctx.skill_activation_emitter = await materialize_skill_activation_emitter_stage(config, ctx)

    retry_breaker = ctx.retry_breaker
    if retry_breaker is None:
        raise LLMDispatchBindError(
            "ctx.retry_breaker is None at stage 5 — stage 3b CP_ROUTING "
            "did not populate the retry/breaker registry (U-RT-58 wrapper "
            "construction requires it per C-RT-16 §14.6 D6)"
        )
    fallback_chain = ctx.fallback_chain
    if fallback_chain is None:
        raise LLMDispatchBindError(
            "ctx.fallback_chain is None at stage 5 — stage 3b CP_ROUTING "
            "did not populate the fallback chain (U-RT-58 wrapper "
            "construction requires it per C-RT-16 §14.6 D6)"
        )
    if ctx.mcp_host is None:
        raise LLMDispatchBindError(
            "ctx.mcp_host is None at stage 5 — stage 2 AS did not populate "
            "the MCP host (U-RT-60 AskUserQuestionSurface construction "
            "requires it per spec §14.8.3 v1.11 binding pin)"
        )

    # R-003 / R-CXA-1 producer-site lift — bind the procedural-tier resolver
    # before TOOL_STEP dispatcher construction so workflow-context secret-fetch
    # AS→IS audit writes carry `procedural_tier_snapshot_ref`. The resolver
    # needs only ctx.skills (stage 2) + ctx.routing_manifest (stage 3b), both
    # available before LOOP_INIT reaches dispatcher composition.
    if ctx.procedural_tier_snapshot_resolver is None:
        ctx.procedural_tier_snapshot_resolver = make_procedural_tier_snapshot_resolver(
            cast(HarnessContext, ctx),
        )
    procedural_tier_snapshot_resolver = ctx.procedural_tier_snapshot_resolver

    # The ask-user surface and TOOL_STEP dispatcher are stage-5 siblings, but
    # R-CXA-2's provider-turn HITL loop needs both before the frozen LLM
    # dispatcher is constructed.
    ask_surface = materialize_mcp_backed_ask_user_question_surface_stage(
        cast(Any, ctx.mcp_host),
        # ctx.mcp_server is the empty `HarnessMCPServer` Protocol; the stage
        # fn wants the concrete — narrow via Any (mirrors mcp_host above).
        harness_mcp_server=cast(Any, ctx.mcp_server),
    )
    ctx.ask_user_question_surface = ask_surface

    # U-RT-68 / R-CXA-2: bind the retry-wrapped TOOL_STEP dispatcher before
    # the LLM dispatcher so provider-turn model tool calls can continue through
    # the same C-RT-19 dispatch surface.
    ctx.tool_dispatcher = await materialize_runtime_tool_dispatcher_stage(
        ctx, config, rate_table=RATE_TABLE_V1
    )
    materialize_r_cxa_2_producer_loop_stage(ctx, config)

    # B-INTERSTEP (runtime spec §14.21 C-RT-34) + B-INTERSTEP-PERRUN-ISOLATION —
    # bind the stable run-scoped channel PROXY when opted-in. The SAME proxy is
    # threaded into the LLM dispatcher (the consumer) below + read by the CP driver
    # (the producer) via `DriverContext.inter_step_output_channel`; both
    # transparently read/write the *current run's* channel resolved from
    # INTER_STEP_CHANNEL_VAR (set fresh per run at the `run_workflow` boundary).
    # Default opt-out (`config.inter_step_data_flow is False`) leaves
    # `ctx.inter_step_output_channel` None → the driver records nothing + the
    # dispatcher injects nothing (byte-identical to pre-v1.59).
    if config.inter_step_data_flow and ctx.inter_step_output_channel is None:
        ctx.inter_step_output_channel = RunScopedInterStepOutputChannel()

    # B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — the durable output-carrying
    # event-history store, co-located under the resolved STATE_LEDGER dir (the
    # `<state_ledger_dir>/engine-output` sibling of the pause-journal). The CP
    # driver records each completed step output here BEFORE the F2 ledger-append
    # (RESERVE-before-COMMIT) + rehydrates the inter-step channel from it on an
    # EVENT_SOURCED_REPLAY resume. `state_ledger_dir` is
    # `ctx.ledger_writer.handle.canonical_path.parent` (the pause-store derivation).
    # Default opt-out leaves `ctx.engine_output_store` None → no recording /
    # rehydration (byte-identical).
    if (
        config.engine_output_replay
        and ctx.engine_output_store is None
        and ctx.ledger_writer is not None
    ):
        _state_ledger_dir = ctx.ledger_writer.handle.canonical_path.parent
        ctx.engine_output_store = EngineOutputStore(
            journal_dir=engine_output_dir_for(_state_ledger_dir)
        )

    # Runtime spec v1.47 §2.1 — inference-conditional LLM-dispatch core. A
    # non-inference (tool-only) workflow bootstraps provider-free (`providers`
    # may be empty), so the provider-backed `materialize_llm_dispatcher_stage`
    # (the ONLY stage-5 surface that consumes `ctx.providers`) is not
    # constructed; a fail-loud sentinel takes its place as the bare core. The
    # HITL/retry wrappers around it + the sub-agent chain below still
    # materialize (provider-free, cheap) but the step-dispatcher registry omits
    # the INFERENCE_STEP / SUB_AGENT_DISPATCH rows, so neither is reachable
    # (StepKindDispatcherNotBoundError backstop). Keeps the non-optional
    # C-RT-04 carrier fields + `_REQUIRED_FIELDS` byte-unchanged.
    bare_dispatcher: Any = _NO_INFERENCE_DISPATCHER
    if ctx.requires_inference:
        bare_dispatcher = materialize_llm_dispatcher_stage(
            providers,
            cast(Any, tracer_provider),
            cost_chain=cast(Any, ctx.cost_chain),
            audit_writer=cast(Any, ctx.audit_writer),
            rate_table=RATE_TABLE_V1,
            # R-FS-1 arc CA + B-INTERSTEP-PERRUN-ISOLATION — thread the run-scoped
            # accumulator PROXY (NOT its `.records` list — that capture-at-bootstrap
            # defeated per-run isolation). Each per-LLM-dispatch SpanCostRecord
            # `append`s through the proxy → the current run's accumulator (resolved
            # from COST_ACCUM_VAR), which `_build_run_result` reads post-join →
            # `RunResult.cost_attribution` (runtime v1.53 §9).
            cost_record_sink=ctx.cost_record_accumulator,
            # B-INTERSTEP (runtime spec §14.21 C-RT-34) — thread the SAME channel
            # instance the CP driver records into; the dispatcher reads
            # `most_recent_output()` and injects it into the dispatched payload.
            # None (opt-out) → no injection (byte-identical to pre-v1.59).
            inter_step_channel=ctx.inter_step_output_channel,
            memory_tool_registry=ctx.memory_tool_registry,
            deployment_surface=config.deployment_surface,
            hitl_tool_loop=ctx.hitl_tool_loop,
            ollama_host=config.ollama_host,
            skill_activation_emitter=ctx.skill_activation_emitter,
            skills=ctx.skills,
            # R-300 — layered routing-selection substrate. routing_manifest from
            # stage 3b CP_ROUTING; workload_class is the run workload; persona_tier
            # from RuntimeConfig. The DECLARATIVE-echo path is behavior-preserving.
            routing_manifest=ctx.routing_manifest,
            workload_class=workload_class,
            persona_tier=config.persona_tier,
            # R-PM-1 cascade PR #1 — resolve the active prompt's inline content from
            # the stage-0-copied `ctx.prompt_manifest` (operator-supplied via
            # `RuntimeConfig.prompt_manifest`). Empty content → None → no injection
            # (byte-identical to pre-R-PM-1 dispatch). This is the load-bearing
            # bootstrap seam: the dispatcher injects the active prompt as a system
            # prompt at translate-time per runtime spec v1.44 §14.5.
            active_system_prompt=ctx.prompt_manifest.active_prompt_version.content or None,
            # R-FS-1 arc B4 (§14.5.3) — the per-role PROMPT injection map resolved
            # at stage 0 (fail-loud there). The dispatcher indexes it per-branch on
            # `step_context.agent_role`; an unbound role falls through to
            # `active_system_prompt`. Empty (no per-role bindings) → byte-identical.
            per_role_system_prompts=ctx.per_role_system_prompts,
            # R-FS-1 arc B4 Slice 3 (CP spec v1.37 §6.1) — per-step PROMPT
            # override support. Project the IS `PromptManifest.versions` store to
            # `{version_sha: content}` so the dispatcher resolves a per-step
            # binding's `prompt_version_sha` → content (precedence per-step >
            # per-role > default; fail-loud `RT-FAIL-PROMPT-SELECTION-UNAUTHORED`
            # if unauthored). Empty store → `{}` → no-override runs byte-identical
            # to pre-Slice-3; a per-step override naming an unauthored sha fails
            # loud. `approved_prompt_version_shas` carries the binding-tier
            # governance parity (OD C-OD-34; inert at the solo-developer tier).
            prompt_versions_by_sha={v.version_sha: v.content for v in ctx.prompt_manifest.versions},
            approved_prompt_version_shas=config.approved_prompt_version_shas,
            # B-L2-EMBEDDING-ACTIVATION (C-CP-02 §2.2 — the routing-activation gate).
            # When True, the DECLARATIVE layer is §2.2-faithful (declines on a
            # manifest-miss → EMBEDDING → L3) and the factory builds the default L2
            # classifier (fail-loud if the `[embedding]` extra is absent). Default
            # False → the #213 always-echo, byte-identical / zero blast radius.
            routing_activation=config.routing_activation,
        )

    # U-RT-58 (C-RT-16 §14.6 D6): rebind ``ctx.llm_dispatcher`` from the
    # bare ``RuntimeLLMDispatcher`` to the ``RetryBreakerFallbackDispatcher``
    # wrapper. The driver call site at `workflow_driver.py:379` is unchanged
    # — the wrapper satisfies the same ``StepDispatcher`` Protocol. The bare
    # dispatcher becomes a private constructor arg of the wrapper.
    # ---------------------------------------------------------------------
    # U-RT-60 (C-RT-18 §14.8) wrap-asymmetry fork APPLIED — row 1 chain:
    #   bare (async C-RT-15)
    #     → HITL gate composer (async; applicable_placements={PRE_ACTION})
    #     → RetryBreakerFallback (async C-RT-16; outer of HITL gate)
    #     → SyncDispatcherFacade (sync; registry binding)
    #
    # AskUserQuestionSurface is bound to the H_T-CP-20-substituted MCP-
    # backed surface (placeholder MCP callback at this arc; FastMCP host
    # wiring deferred per Phase 7d retirement batch 8 H_T-CP-20 RETIRE-READY).
    # Stage 4 OD's `audit_writer` + stage 1 IS's `ledger_writer` are
    # consumed by the HITL composer for the 4-substep audit-write at
    # spec §14.8.2 step 4h (same dep set as U-RT-59 sub-agent dispatch
    # per Q3 ratification — shared `cp_audit_to_od_audit` converter).
    # Defensive guard retained for robustness even though the builder fields
    # are populated by stage 1 / stage 4; pyright proves the audit_writer arm
    # cannot be None here, so its reportUnnecessaryComparison is suppressed.
    if ctx.ledger_writer is None or ctx.audit_writer is None:  # pyright: ignore[reportUnnecessaryComparison]
        raise LLMDispatchBindError(
            "ctx.ledger_writer / ctx.audit_writer is None at stage 5 — stage 1 "
            "IS / stage 4 OD must complete before stage 5 HITL gate composer "
            "construction per the runtime spec v1.11 §14.8.2 step 4h "
            "4-substep audit-write composition contract"
        )
    # pause_requested_flag is bound at stage 0 PREAMBLE (in _REQUIRED_FIELDS),
    # so it is non-None by stage 5; assert narrows it for the two
    # RuntimeHITLGateComposer constructions below (param wants `Event`).
    assert ctx.pause_requested_flag is not None
    # `ctx.mcp_host` is typed against the schema-level Protocol at
    # `harness_runtime.types.MCPHost`; the concrete dataclass at
    # `harness_runtime.lifecycle.mcp_host.MCPHost` is what stage 2 bound
    # (same C-RT-04 Protocol-vs-concrete pattern as `tracer_provider` +
    # `ledger_writer` / `audit_writer`).
    # U-RT-62 AC #4: stage 5 default binding rebinds the surface's
    # callback from `_PlaceholderMCPCallback` to `ServerCtxElicitCallback`
    # when `ctx.mcp_server` is materialized (post-stage-2 per U-RT-62
    # AC #2). The callback reads the in-flight `run_workflow` tool ctx
    # via `ctx.mcp_server.get_current_tool_ctx()` (module-level ContextVar
    # per spec v1.36 §14.18 chapeau per-session ctx isolation) + invokes
    # `await ctx.elicit(...)` outbound per spec v1.12 §14.8.3 topology
    # pin (Reading α CC-initiates). Test substrates that do not exercise
    # the MCP server path may leave `ctx.mcp_server = None` — the
    # surface then falls back to the placeholder for defensive failure.

    # U-RT-94 (v2.25 §7.2 AC #11/#12/#14): materialize pause/webhook/resume
    # bindings BEFORE the HITL composer construction so the composer can
    # capture them at construction time per AC #12 (4 NEW constructor
    # fields). Within-stage-5 sibling ordering is implementer-discretion per
    # spec §14.16.3 + change-note adjacent defect (ii); ordering "pause +
    # webhook + holder BEFORE composer" is observationally equivalent to
    # the reverse because the §14.8.8.1 step 0 OR-form precondition consumes
    # the joint binding at composer dispatch-time.
    ctx.pause_resume_protocol = await materialize_pause_resume_protocol_stage(config, ctx)
    ctx.webhook_delivery_composer = await materialize_webhook_delivery_composer_stage(config, ctx)
    ctx.resume_context_holder = ResumeContextHolder()

    # U-RT-115 (G1-blast): per-step blast-radius resolver closure capturing ctx
    # (the `make_procedural_tier_snapshot_resolver(ctx)` precedent). Shared by
    # both composer instances (PRE_ACTION + SUB_AGENT_BOUNDARY). U-RT-116 (G1-skip):
    # the §3.8 operator policy from config, held as composer instance state.
    blast_radius_resolver = make_step_blast_radius_resolver(cast(HarnessContext, ctx))
    hitl_auto_approve_policy = config.hitl_auto_approve_policy

    hitl_inference = RuntimeHITLGateComposer(
        inner=bare_dispatcher,
        applicable_placements=frozenset({HITLPlacementKind.PRE_ACTION}),
        ask_user_question_surface=cast(Any, ask_surface),
        ledger_writer=cast(Any, ctx.ledger_writer),
        audit_writer=cast(Any, ctx.audit_writer),
        tracer_provider=cast(Any, tracer_provider),
        audit_signing_key_id="harness-runtime-dev",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=procedural_tier_snapshot_resolver,
        pause_resume_protocol=ctx.pause_resume_protocol,
        pause_requested_flag=ctx.pause_requested_flag,
        webhook_delivery_composer=ctx.webhook_delivery_composer,
        resume_context_holder=ctx.resume_context_holder,
        blast_radius_resolver=blast_radius_resolver,
        hitl_auto_approve_policy=hitl_auto_approve_policy,
    )
    ctx.llm_dispatcher = materialize_retry_breaker_fallback_dispatcher_stage(
        inner=cast(Any, hitl_inference),
        retry_breaker=retry_breaker,
        fallback_chain=fallback_chain,
        tracer_provider=cast(Any, tracer_provider),
        # U-RT-114 (§14.5.3): the wrapper reads `step_context.agent_role` to
        # promote a per-role model to the PRIMARY fallback candidate (per-role
        # MODEL specialization composing with C-RT-16 fallback).
        routing_manifest=ctx.routing_manifest,
        # B-L2-FALLBACK-COMPOSITION (§14.6): hand the wrapper a DIRECT handle to
        # the BARE C-RT-15 dispatcher's route-once SELECTION (`resolve_routed_binding`)
        # — NOT through the HITL `inner`, which sits two layers above the
        # routing-capable dispatcher. The wrapper calls it ONCE per step to seed
        # the routed PRIMARY candidate, so layered routing (routing_activation)
        # composes with the fallback chain. Non-inference workflows have no real
        # dispatcher (the `_NO_INFERENCE_DISPATCHER` sentinel) → None (no routing).
        # The resolver itself returns None when `routing_activation` is off, so
        # this is byte-identical for the default-off path.
        routing_resolver=(
            bare_dispatcher.resolve_routed_binding if ctx.requires_inference else None
        ),
        # B-MODEL-RESOLUTION-CONSOLIDATION (§14.6): the run workload — the SAME
        # value handed to the inner dispatcher above (line ~310) — so the wrapper
        # can honour a per-workload `model_binding_override` at the per-workload
        # precedence tier (per-step > per-workload > per-role > routed > default).
        workload_class=workload_class,
    )

    # ---------------------------------------------------------------------
    # U-RT-59 (C-RT-17 §14.7): sub-agent dispatch composer + step-kind
    # routing registry. Per spec §14.7.7 "Integration with C-RT-04": two
    # new HarnessContext fields at v1.6 (`sub_agent_dispatcher`,
    # `step_dispatchers`); both bound here.
    #
    # v1.7 wiring lifts the v1.6 MVP INFERENCE_STEP carve-out per the Path B
    # resolution of the U-RT-59 async/sync StepDispatcher Class 1 fork
    # (`.harness/class_1_tension_u_rt_59_async_sync_step_dispatcher.md`):
    # `ctx.llm_dispatcher` (the U-RT-58 `RetryBreakerFallbackDispatcher`
    # wrapper) is async; the CP driver's `StepDispatcher` Protocol is sync;
    # the registry binds the wrapper through a `SyncDispatcherFacade` that
    # captures this loop and schedules coroutines back via
    # `asyncio.run_coroutine_threadsafe(...).result(timeout=...)` from the
    # worker thread that runs `execute_workflow` per api.py:399. Stage 5 runs
    # in an `async def` awaited from `await run_bootstrap(...)` at
    # api.py:349 — the running loop here IS the loop that hosts the
    # subsequent `asyncio.to_thread`, so `materialize_sync_dispatcher_facade`
    # captures the correct loop. Tool / HITL / validator step kinds remain
    # unbound per spec §14.7 (follow-on composer arcs).
    #
    # Child workflow runner closes over `ctx` (the _MutableHarnessContext);
    # at runtime invocation it reads `ctx.step_dispatchers` (set below) +
    # casts ctx to the CP driver's structural `DriverContext` Protocol.
    # The mutable ctx satisfies the Protocol structurally — same pattern
    # api.py uses on the frozen ctx.
    child_runner = compose_child_workflow_runner(cast(HarnessContext, ctx))

    # v1.7 §14.7.2 step 8 4-substep audit composition extends the
    # dispatcher's dependency set with the IS state-ledger writer (8b F2-
    # write), the OD audit writer (8d IS-anchored append), and signing
    # config + time source for the CP→OD converter at 8c. The audit-writer
    # availability check above (HITL composer construction) covers the
    # same dependency set.
    # Audit-signing config — operator surface deferred per spec §14.7
    # "Deferred to implementation discretion" + ADR-D5 v1.3 §1.4.1 (HSM /
    # KMS / keystore custody). v1.7 MVP binds a deployment-default value;
    # operator-tunable surface is a follow-on RuntimeConfig extension.
    bare_sub_agent_dispatcher = RuntimeSubAgentDispatcher(
        handoff_registry=ctx.handoff_registry,  # type: ignore[arg-type]  # narrowed at stage 3b
        topology_dispatcher=topology.dispatcher,
        tracer_provider=cast(Any, tracer_provider),
        child_workflow_runner=child_runner,
        # `ctx.ledger_writer` / `ctx.audit_writer` are typed as Protocols at
        # `harness_runtime.types` (C-RT-04 schema layer); the dispatcher
        # consumes the concrete dataclass types from `harness_runtime.lifecycle`.
        # The structural shape matches; cast bridges the Protocol → concrete
        # mismatch at the composition site per the C-RT-04 pattern reused at
        # tracer_provider above.
        ledger_writer=cast(Any, ctx.ledger_writer),
        audit_writer=cast(Any, ctx.audit_writer),
        audit_signing_key_id="harness-runtime-dev",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        time_source=lambda: datetime.now(UTC),
        procedural_tier_snapshot_resolver=procedural_tier_snapshot_resolver,
    )

    # U-RT-60 (C-RT-18 §14.8) wrap-asymmetry fork APPLIED — row 2 chain:
    #   bare sub-agent dispatcher (sync C-RT-17)
    #     → HITL gate composer (async; applicable_placements={SUB_AGENT_BOUNDARY})
    #     → SyncDispatcherFacade (sync; registry binding)
    #
    # Inner-call shape: HITL composer body invokes `self.inner.dispatch(...)`
    # via the duck-typed `_dispatch_inner` helper at `hitl_gate_composer.py`;
    # for sync C-RT-17 inner the call returns a Mapping directly (no await
    # needed), per fork §7.2 Q3 ratification.
    hitl_sub_agent = RuntimeHITLGateComposer(
        inner=bare_sub_agent_dispatcher,
        applicable_placements=frozenset({HITLPlacementKind.SUB_AGENT_BOUNDARY}),
        ask_user_question_surface=cast(Any, ask_surface),
        ledger_writer=cast(Any, ctx.ledger_writer),
        audit_writer=cast(Any, ctx.audit_writer),
        tracer_provider=cast(Any, tracer_provider),
        audit_signing_key_id="harness-runtime-dev",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=procedural_tier_snapshot_resolver,
        pause_resume_protocol=ctx.pause_resume_protocol,
        pause_requested_flag=ctx.pause_requested_flag,
        webhook_delivery_composer=ctx.webhook_delivery_composer,
        resume_context_holder=ctx.resume_context_holder,
        blast_radius_resolver=blast_radius_resolver,
        hitl_auto_approve_policy=hitl_auto_approve_policy,
    )
    ctx.sub_agent_dispatcher = hitl_sub_agent

    # Registry binding: both rows wrap through `SyncDispatcherFacade` at the
    # top so the CP `StepDispatcher` Protocol (sync) is satisfied uniformly.
    # `materialize_sync_dispatcher_facade` captures the running event loop
    # (this stage executes on the outer api.py loop per loop-capture-timing
    # invariant). Result-timeout reads `config.step_dispatch_timeout_seconds`
    # per spec v1.31 §3 — per-step worker-thread bound, independent of
    # whole-workflow `config.drain_timeout_seconds`. On expiry, the facade
    # raises `StepDispatchTimeoutError`; CP driver maps to
    # `RT-FAIL-STEP-DISPATCH-TIMEOUT` per spec v1.31 §11.
    inference_step_dispatcher = materialize_sync_dispatcher_facade(
        cast(Any, ctx.llm_dispatcher),
        result_timeout_seconds=config.step_dispatch_timeout_seconds,
    )
    sub_agent_step_dispatcher = materialize_sync_dispatcher_facade(
        cast(Any, hitl_sub_agent),
        result_timeout_seconds=config.step_dispatch_timeout_seconds,
    )

    # ---------------------------------------------------------------------
    # U-RT-68 (REWRITTEN at v2.12): stage-5 TOOL_STEP wire-up via the
    # U-RT-75 `materialize_runtime_tool_dispatcher_stage` factory. Per
    # spec v1.16 §14.9.3 stage-5 prose + §14.11 C-RT-21: the factory
    # composes the 5-step chain (PerServerTrustEvaluator → MCPClientNamespaceEmitter
    # → bare RuntimeToolDispatcher → RetryBreakerToolDispatcher wrap →
    # return wrapper). The bare dispatcher is private to the wrapper per
    # spec §14.9.6 inv 6; ctx.tool_dispatcher binds to the wrapper.
    #
    # Mirrors the existing `ctx.llm_dispatcher` wrap-binding pattern at
    # C-RT-16 §14.6 D6 above. Step-dispatcher registry below extends with
    # TOOL_STEP → tool_step_dispatcher facade.
    # U-OD-39: thread RATE_TABLE_V1 through to the bare dispatcher for
    # cost-attribution at the tool-dispatch site per §C-OD-26.2 row
    # "tool.dispatch". ctx.cost_chain + ctx.audit_writer were already
    # validated non-None above (U-OD-38 enforcement at the LLM-dispatch
    # bind block); the same substrate flows through.
    assert ctx.tool_dispatcher is not None
    # ---------------------------------------------------------------------
    # R-FS-1 `B-TOOL-GATE` (CP spec v1.35 §19.1.2 Producer ¶ + runtime §14.8.2
    # step-4c) — the TOOL_STEP HITL gate site: the third RuntimeHITLGateComposer,
    # wrapping the tool dispatcher with `applicable_placements={PRE_ACTION}` and an
    # `mcp_trust_tier_resolver` that resolves a step's owning MCP host's trust tier
    # (the resolved-owning-host feed the §19.1.2 Producer ¶ describes — making the
    # MCP-trust gate axis non-vacuous AT THE TOOL GATE when it fires: an L0 server's
    # tool floors its gate to DENY, an L3 to AUTO). Same dep set as the inference /
    # sub-agent composers (so the tool gate's HITL flow is identical), plus the resolver.
    #
    # SCOPE: the wrap-time gate is placement-driven — it fires only when
    # `step.hitl_placements` is non-empty. At HEAD no producer binds the WORKFLOW-level
    # `WorkflowManifestEntry.hitl_placements` onto the per-step `WorkflowStep` the driver
    # dispatches, so NO wrap-time gate (inference / sub-agent / tool) fires through the
    # real manifest→driver path. That per-step placement producer is a PRE-EXISTING gap
    # shared by all three gate sites (registered as a forward arc), NOT introduced here;
    # this arc lands the tool-gate-site + MCP-trust feed at parity with the others.
    #
    # **Wrap the registry path, NOT `ctx.tool_dispatcher` itself.** `ctx.tool_dispatcher`
    # is also read by the R-CXA-2 producer loop (`materialize_r_cxa_2_producer_loop_stage`)
    # + the provider-turn tool loop (`hitl_tool_loop`); reassigning it would HITL-gate
    # provider-initiated tool calls too (scope creep + double-gate vs `hitl_tool_loop`).
    # The composer wraps the wrapper as `inner`; HITL is OUTER of the C-RT-16 retry
    # (the gate fires once before dispatch — retries do not re-prompt the operator —
    # a deliberate asymmetry vs the LLM path's HITL-inner-of-retry).
    mcp_trust_tier_resolver = make_step_mcp_trust_tier_resolver(cast(HarnessContext, ctx))
    hitl_tool = RuntimeHITLGateComposer(
        inner=ctx.tool_dispatcher,
        applicable_placements=frozenset({HITLPlacementKind.PRE_ACTION}),
        ask_user_question_surface=cast(Any, ask_surface),
        ledger_writer=cast(Any, ctx.ledger_writer),
        audit_writer=cast(Any, ctx.audit_writer),
        tracer_provider=cast(Any, tracer_provider),
        audit_signing_key_id="harness-runtime-dev",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=procedural_tier_snapshot_resolver,
        pause_resume_protocol=ctx.pause_resume_protocol,
        pause_requested_flag=ctx.pause_requested_flag,
        webhook_delivery_composer=ctx.webhook_delivery_composer,
        resume_context_holder=ctx.resume_context_holder,
        blast_radius_resolver=blast_radius_resolver,
        hitl_auto_approve_policy=hitl_auto_approve_policy,
        mcp_trust_tier_resolver=mcp_trust_tier_resolver,
    )
    tool_step_dispatcher = materialize_sync_dispatcher_facade(
        cast(Any, hitl_tool),
        result_timeout_seconds=config.step_dispatch_timeout_seconds,
    )

    # Runtime spec v1.47 §2.1 — a non-inference (tool-only) workflow OMITS the
    # INFERENCE_STEP / SUB_AGENT_DISPATCH rows (it bootstrapped provider-free,
    # so their dispatch cores are the fail-loud sentinel / built-but-unreachable
    # chain). The CP driver then raises StepKindDispatcherNotBoundError if such a
    # step is ever dispatched — unreachable, because the `requires_inference`
    # predicate reads these same `workflow.steps`.
    dispatchers: dict[StepKind, Any] = {StepKind.TOOL_STEP: tool_step_dispatcher}
    if ctx.requires_inference:
        dispatchers[StepKind.INFERENCE_STEP] = inference_step_dispatcher
        dispatchers[StepKind.SUB_AGENT_DISPATCH] = sub_agent_step_dispatcher
        # R-FS-1 B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3) — the opt-in terminal
        # synthesis step dispatcher wraps the (already-sync) inference facade,
        # composing the concurrent fan-out's branch-index-ordered sibling outputs
        # into the synthesis LLM input. Bound under `requires_inference` (it makes
        # an LLM call); a POST_JOIN_SYNTHESIS step in a provider-free workflow fails
        # closed (StepKindDispatcherNotBoundError), same as INFERENCE_STEP.
        dispatchers[StepKind.POST_JOIN_SYNTHESIS] = PostJoinSynthesisStepDispatcher(
            inner=inference_step_dispatcher
        )

    # R-FS-1 arc M (C-RT-28 §14.20): surface-gated MANAGED_AGENTS binding. The
    # factory returns a dispatcher only when opted-in
    # (`config.managed_agents_config` non-None) AND on `MANAGED_CLOUD` (the
    # H_T-AS-8f local-dev exclusion); else None → the row is omitted → a
    # managed-agents step fails closed (StepKindDispatcherNotBoundError). The
    # async dispatcher binds through `SyncDispatcherFacade` like the others.
    # Independent of `requires_inference` — managed-agents is a separate
    # execution model (the vendor owns the loop; no INFERENCE/topology deps).
    managed_agents_dispatcher = await materialize_managed_agents_dispatcher_stage(config, ctx)
    if managed_agents_dispatcher is not None:
        assert config.managed_agents_config is not None  # narrowed by the factory's non-None return
        ctx.managed_agents_client = config.managed_agents_config.client
        # §14.20.3: the managed-agents facade uses its OWN result-timeout
        # (`managed_agents_config.step_timeout_seconds`, 600s default), NOT the
        # shared 30s `step_dispatch_timeout_seconds`. A vendor session runs
        # minutes; the shared bound would fire RT-FAIL-STEP-DISPATCH-TIMEOUT
        # prematurely while the vendor session keeps running, billable, never
        # cancelled (the abandoned coroutine never reaches its cancel-on-give-up
        # path). The timeout must exceed the per-step poll budget.
        dispatchers[StepKind.MANAGED_AGENTS] = materialize_sync_dispatcher_facade(
            cast(Any, managed_agents_dispatcher),
            result_timeout_seconds=config.managed_agents_config.step_timeout_seconds,
        )
    ctx.step_dispatchers = StepKindDispatcherRegistry(dispatchers=dispatchers)

    # U-RT-88 (C-RT-24 §14.14) + U-RT-97 (C-RT-26 §14.16) + U-RT-94 (v1.25
    # §14.8.8.9): pause_resume_protocol + webhook_delivery_composer +
    # resume_context_holder bindings hoisted ABOVE the HITL composer
    # construction sites at v2.25 §7.2 AC #12 absorption — so the
    # RuntimeHITLGateComposer constructor can capture them at construction
    # time per the 4-NEW-fields amendment (lines 211-233 above). Re-binding
    # here would shadow the values the composer captured + introduce a
    # divergence between composer-local refs and ctx-local refs.
