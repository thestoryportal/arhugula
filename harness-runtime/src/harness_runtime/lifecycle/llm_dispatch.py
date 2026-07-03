"""LLM-dispatch composer — stage 5 LOOP_INIT (U-RT-52, opens L9).

Per `Spec_Harness_Runtime_v1.md` v1.2 §C-RT-15 (LLM-dispatch composer).
Satisfies the `harness_cp.workflow_driver.StepDispatcher` Protocol
(declared at `harness-cp/src/harness_cp/workflow_driver.py:151`,
`runtime_checkable`). Per-step async composer that:

  1. Resolves the per-provider adapter from `ctx.providers` via
     ``binding.model_binding.provider`` (CP `ModelBinding.provider: str`
     per C-CP-01 §1.4).
  2. Opens a GenAI-semconv 1.41.0 span via
     ``ctx.tracer_provider.get_tracer("harness.runtime.llm_dispatch")``.
  3. Dispatches to the provider's SDK message-construction method
     (anthropic / openai / ollama) using the unpacked payload.
  4. Populates GenAI semconv attributes per OD C-OD-04..08 + (for the
     anthropic provider) ``anthropic.*`` cache attributes per AS spec
     C-AS-14 §14.2.
  5. Returns ``Mapping[str, Any]`` per `StepDispatcher` Protocol contract.

**Payload-shape convention (Class 3 fork resolution 2026-05-20,
`.harness/fork_u_rt_52_step_payload_shape.md`).** ``step.step_payload``
is consumed as a `harness_cp.cp_shared_types.ProviderAgnosticPayload`
mapping (``messages`` / ``tools`` / ``params``) per ADR-F1 v1.2 +
C-CP-01 §1.1 the provider-neutral 3-tuple. Spec §14.5 was silent on
``step_payload`` shape at v1.2; this module pins the convention at
v1.3 (`Spec_Harness_Runtime_v1.md` §14.5 amendment 2026-05-20).

**Q2a scope discipline (per `.harness/fork_llm_dispatch_composer_scope.md`
operator ratification 2026-05-20).** Composer is the smallest-scope
surface: per-step dispatch only. Fallback / retry / breaker wrappers
are explicitly out of scope — provider-side exceptions propagate
unmodified to `workflow_driver.py:380-389` `try/except`. CP-3 (retry.*)
+ CP-4 (fallback chain) retirements deferred to follow-on units.

**Q3a in-arc GenAI semconv binding.** Composer attaches the GenAI
1.41.0 attribute set per OTel semconv. Enables H_T-OD-2 PARTIAL →
RETIRE-READY upgrade in the same arc (OTel SDK substrate operative +
GenAI binding present + spans flow).

**OTel context-manager note.** Spec §14.5 invariants phrase ``async
with tracer.start_as_current_span(...)``; OpenTelemetry's tracer
context manager is synchronous (returns a regular ``ContextManager``,
not an ``AsyncContextManager``). Inside this async function we use
plain ``with`` per OTel API contract; spec phrasing is imprecise but
the semantic — exactly one span per call, lifecycle bound by the
``with`` block — is preserved.

**Module convention.** One module per unit. Bound at bootstrap stage 5
alongside override evaluator / topology dispatcher / lifecycle emitter.
Typed `LLMDispatchBindError` for bootstrap-time failures. Mirrors the
L5..L8 stage shape established at U-RT-21..U-RT-41.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from harness_as.memory_tool_contracts import MemoryToolName
from harness_core import PersonaTier, WorkloadClass
from harness_cp.cp_shared_types import (
    ActorIdentity,
    AgentRole,
    ModelBinding,
    ProviderAgnosticPayload,
    RoutingDecisionTrace,
    TraceContext,
)
from harness_cp.layer_budget import DEFAULT_LAYER_BUDGETS, LayerBudget
from harness_cp.layered_routing_strategy import LayerDecisionFn
from harness_cp.memory_access_mode import MemoryAccessMode
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.routing_core_surface import (
    InferenceRequest,
    ProviderDispatchResult,
    RouterResolutionFn,
    infer,
    resolve_routing_trace,
)
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_od.otel_genai_base import HIERARCHY_CORRELATION_KEY, GenAiOperation

from harness_runtime.lifecycle.cost_record_sink import SupportsCostRecordAppend
from harness_runtime.lifecycle.hitl_tool_loop import (
    HITLToolLoopContext,
    ModelToolCall,
    RuntimeHITLToolLoop,
)
from harness_runtime.lifecycle.memory_tool_dispatch import (
    derive_context_editing_active,
    execute_with_memory_callbacks,
    step_has_memory_tool,
)
from harness_runtime.memory_context import (
    RuntimeMemoryContext,
    compose_system_prompt_with_memory_packet,
)
from harness_runtime.memory_tool_executor import (
    MemoryToolExecutionContext,
    MemoryToolExecutionInputError,
    MemoryToolExecutionRequest,
)


@dataclass(frozen=True, slots=True)
class RoutedPrimaryResolution:
    """The B-L2-FALLBACK-COMPOSITION routed-primary resolution (§14.6.1).

    `resolve_routed_binding` returns this when the layered routing
    (DECLARATIVE-decline → EMBEDDING → L3) picks a model: the routed
    ``model_binding`` the C-RT-16 wrapper seeds as its chain PRIMARY, PLUS the
    ``routing_trace`` (the real EMBEDDING/L3 layer + candidate) and
    ``binding_rationale`` that B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION threads to the
    inner `gen_ai` span for the routed-primary dispatch (§14.6.1 scope-boundary
    closure). The wrapper reads ``model_binding`` for chain composition; the
    trace + rationale ride `ROUTED_PRIMARY_SPAN_TRACE` to the span emitter.
    """

    model_binding: ModelBinding
    routing_trace: RoutingDecisionTrace
    binding_rationale: str | None


# B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION (§14.6.1 scope-boundary closure). Under
# `routing_activation` the C-RT-16 wrapper resolves the layered-routing decision
# ONCE (route-once-then-fallback) and reverts the inner C-RT-15 dispatcher to a
# FAITHFUL DECLARATIVE echo — so the inner's `gen_ai` span would carry the echo
# `routing.layer`, not the real EMBEDDING/L3 layer the wrapper used. The wrapper
# publishes the real `(routing_trace, binding_rationale)` here for the
# routed-PRIMARY dispatch ONLY (cleared for fallback candidates, which ARE
# faithful); `_invoke_provider` reads it for `routing.layer`/`binding_rationale`.
# A ContextVar (not an instance attribute) because the C-RT-15 dispatcher is
# shared across concurrent fan-out branches — the per-task isolation mirrors the
# B-INTERSTEP-PERRUN-ISOLATION (v1.64) precedent. Default/absent (routing off,
# non-routed, or any direct non-wrapper dispatch) → the local echo trace, so the
# pre-arc span attribution is byte-identical.
ROUTED_PRIMARY_SPAN_TRACE: ContextVar[tuple[RoutingDecisionTrace, str | None] | None] = ContextVar(
    "_routed_primary_span_trace", default=None
)


class LLMDispatchBindError(Exception):
    """Raised when LLM-dispatch composer stage materialization fails."""


class LLMDispatchProviderUnreachableError(Exception):
    """Raised when ``binding.model_binding.provider`` resolves to a
    provider absent from ``ctx.providers`` (e.g., Ollama-degraded path
    skipped registration).

    Maps to ``RT-FAIL-PROVIDER-UNREACHABLE`` per
    `Spec_Harness_Runtime_v1.md` v1.2 §C-RT-14 failure-mode taxonomy.
    Carries the offending provider name for operator-facing attribution.
    """

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        super().__init__(
            f"RT-FAIL-PROVIDER-UNREACHABLE: provider {provider_name!r} not in ctx.providers"
        )


class LLMDispatchPayloadShapeError(Exception):
    """Raised when ``step.step_payload`` cannot be coerced to the
    provider-neutral ``ProviderAgnosticPayload`` shape.

    Per the Class 3 fork resolution (2026-05-20), ``step.step_payload``
    is the `ProviderAgnosticPayload(messages, tools, params)` 3-tuple
    per C-CP-01 §1.1. Mis-shaped payloads (e.g., missing ``messages``)
    surface as this typed error rather than a generic ``KeyError`` /
    ``ValidationError`` so the driver's ``except`` boundary can
    attribute the failure to the dispatch site.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"RT-FAIL-PAYLOAD-SHAPE: {reason}")


class PromptInjectionConflictError(Exception):
    """Raised when a configured active prompt collides with a payload-carried
    system source at translate-time (R-PM-1 cascade PR #1).

    The active prompt (``HarnessContext.prompt_manifest.active_prompt_version
    .content``, bound on the dispatcher as ``active_system_prompt``) is the
    harness-owned base system prompt. If an active prompt is configured AND the
    payload already carries a competing system source — an Anthropic
    ``params["system"]`` (the opaque escape hatch) or an OpenAI/Ollama leading
    ``{"role":"system"}`` message — the two-source ambiguity is **fail-loud /
    detect-then-refuse**, never silently merged or dropped (consistent with the
    arc-#1 ``RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE`` posture).

    Maps to ``RT-FAIL-PROMPT-INJECTION-CONFLICT`` per
    `Spec_Harness_Runtime_v1.md` §C-RT-15 (v1.44 amendment). Step-level (per
    dispatch), like ``RT-FAIL-PROVIDER-UNREACHABLE``: propagates through the
    driver ``except`` boundary at `workflow_driver.py` as a step-failure; does
    NOT abort bootstrap.

    Known operational consequence: for OpenAI/Ollama a leading
    ``{"role":"system"}`` message is the idiomatic way a workflow step supplies
    its own system prompt, so configuring an active prompt will hard-error any
    workflow that already carries its own system message — this is the intended
    v1 contract (surface the collision, do not silently pick). The escape valve
    is a future explicit merge/replace policy (runtime spec §14.5 OQ-5).
    """

    def __init__(self, provider: str, source: str) -> None:
        self.provider = provider
        self.source = source
        super().__init__(
            "RT-FAIL-PROMPT-INJECTION-CONFLICT: active prompt configured but "
            f"{provider!r} payload already carries a system source ({source}); "
            "fail-loud rather than silently merge/replace"
        )


@runtime_checkable
class _ProvidersLike(Protocol):
    """Minimal ``ctx.providers`` substrate the composer consumes.

    Structurally satisfied by `harness_runtime.lifecycle.providers.
    ProviderClientsStage.providers` (a ``dict[str, ProviderClient]``
    mapping per C-RT-05). Position-only ``key`` parameters match
    `dict.__getitem__` / `dict.__contains__` shape so Protocol
    conformance carries through to the concrete dict at the stage-5
    factory call site.
    """

    def __contains__(self, key: object, /) -> bool: ...
    def __getitem__(self, key: str, /) -> Any: ...
    def __len__(self) -> int: ...


@runtime_checkable
class _ExternalCLIProviderLike(Protocol):
    """Provider shape for text-only local CLI adapters."""

    async def dispatch_text(self, *, model: str, prompt: str) -> Any: ...


@runtime_checkable
class _TracerProviderLike(Protocol):
    """Minimal ``ctx.tracer_provider`` substrate the composer consumes.

    Structurally satisfied by `opentelemetry.sdk.trace.TracerProvider`
    materialized at C-RT-06 stage 4 OD.
    """

    def get_tracer(self, instrumenting_module_name: str, /) -> Any: ...


def _coerce_payload(payload: Mapping[str, Any]) -> ProviderAgnosticPayload:
    """Coerce ``step.step_payload`` to `ProviderAgnosticPayload`.

    Pydantic v2 ``model_validate`` accepts a mapping in the canonical
    shape. Mis-shaped mappings raise `LLMDispatchPayloadShapeError`
    wrapping the underlying `ValidationError` so the driver's
    ``except`` block sees a typed failure attributable to the dispatch
    site.
    """
    if isinstance(payload, ProviderAgnosticPayload):
        return payload
    try:
        return ProviderAgnosticPayload.model_validate(payload)
    except Exception as exc:
        raise LLMDispatchPayloadShapeError(
            f"step.step_payload not coercible to ProviderAgnosticPayload: {exc}"
        ) from exc


_UPSTREAM_CONTEXT_PREFIX = "Upstream step output:\n"
"""Label prefixing the B-INTERSTEP upstream-output context message (runtime spec
§14.21 C-RT-34). Lets a model (and a test) distinguish the injected prior-step
output from the step's own messages."""


def _inject_upstream_context_message(
    kwargs: dict[str, Any], upstream: Mapping[str, Any] | None
) -> None:
    """Insert the immediately-prior step's output as an upstream-context ``user``
    message into the FINAL provider ``kwargs["messages"]``, AFTER any leading
    ``role:"system"`` message.

    B-INTERSTEP (runtime spec §14.21 C-RT-34). Operating on the final kwargs (NOT
    ``payload.messages``) is load-bearing, and mirrors the R-PM-1 system-prompt
    post-`params` injection: it runs AFTER ``kwargs.update(payload.params)`` so a
    ``params["messages"]`` escape-hatch override cannot silently drop it, and AFTER
    ``_inject_leading_system_message`` so the upstream ``user`` message does not
    push a conflicting payload-leading ``system`` message off index 0 and mask the
    competing-system-source conflict check (Codex review). The dispatcher does NOT
    introspect the step body — it carries the driver-recorded output opaquely
    (`workflow_driver` §25.3.3.4 preserved); ``default=str`` keeps a non-JSON-native
    value serializable + ``sort_keys`` keeps the injected content deterministic.
    ``None`` (opt-out / empty channel) → no-op (byte-identical to pre-v1.59).
    """
    if upstream is None:
        return
    messages: list[Mapping[str, Any]] = list(kwargs.get("messages", ()))
    context_message: dict[str, Any] = {
        "role": "user",
        "content": _UPSTREAM_CONTEXT_PREFIX
        + json.dumps(dict(upstream), sort_keys=True, default=str),
    }
    insert_at = 1 if messages and messages[0].get("role") == "system" else 0
    kwargs["messages"] = [*messages[:insert_at], context_message, *messages[insert_at:]]


def _set_if_present(span: Any, key: str, value: Any) -> None:
    """Set a span attribute only when the value is not ``None``.

    OTel allows ``None`` as a value but the GenAI semconv discourages
    emitting attributes whose value is unknown. This helper keeps the
    per-provider attribute extraction code uncluttered.
    """
    if value is not None:
        span.set_attribute(key, value)


def _extract_anthropic_cache_request_attrs(
    payload: ProviderAgnosticPayload,
) -> tuple[str | None, int | None]:
    """Extract ``anthropic.cache_breakpoint_id`` + ``anthropic.cache_ttl_seconds``
    from the request payload's ``cache_control`` directives, if present.

    Per Anthropic prompt-caching docs, ``cache_control`` lives on
    individual content blocks within ``messages`` (and optionally on
    ``system`` / ``tools``). The breakpoint_id is the ordinal of the
    first cache_control-bearing block (≤4 per Anthropic limit); the
    ttl is the ``cache_control.ttl`` field translated to seconds
    (``"5m"`` → 300; ``"1h"`` → 3600). Returns ``(None, None)`` when
    no cache_control directive is present.

    The extraction is best-effort: payloads that don't follow the
    cache-control convention return ``(None, None)`` rather than
    raising. Per C-AS-14 §14.2 these attributes have low cardinality
    (≤4 breakpoints; binary ttl).
    """
    for index, message in enumerate(payload.messages):
        content = cast(Any, message.get("content"))
        if not isinstance(content, list):
            continue
        blocks = cast(list[Any], content)
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            block_mapping = cast(Mapping[str, Any], block)
            cache_control = cast(Any, block_mapping.get("cache_control"))
            if not isinstance(cache_control, Mapping):
                continue
            cc_mapping = cast(Mapping[str, Any], cache_control)
            ttl_label = cast(Any, cc_mapping.get("ttl"))
            ttl_seconds: int | None
            if ttl_label == "1h":
                ttl_seconds = 3600
            elif ttl_label == "5m":
                ttl_seconds = 300
            elif ttl_label is None:
                ttl_seconds = 300  # Anthropic default
            else:
                ttl_seconds = None
            return (f"msg-{index}", ttl_seconds)
    return (None, None)


# R-300 MVP routing-envelope placeholders. The DECLARATIVE-echo layer decision
# (see `RuntimeLLMDispatcher.dispatch`) echoes the resolved `binding.model_binding`
# and ignores the InferenceRequest discriminators, so these are carried-but-not-
# selection-driving at MVP. `AgentRole` is an open-string newtype and no per-step
# role is threaded through the execution path at v1.6 (WorkflowStep carries only
# step_id/step_kind/step_payload) and no conventional default-role key exists in
# `RoutingManifest.per_role_bindings`; they become load-bearing at
# R-300-second-provider when `route()` performs real per-discriminator selection.
_MVP_DEFAULT_AGENT_ROLE = AgentRole("default")
_MVP_DEFAULT_WORKLOAD_CLASS = WorkloadClass.SOFTWARE_ENGINEERING
_MVP_PLACEHOLDER_TRACE_CONTEXT = TraceContext(
    trace_id="0" * 32, span_id="0" * 16, trace_flags=0, trace_state=None
)
# Used when `self.routing_manifest` is unset (unit-test ergonomics). The echo
# decision ignores the manifest, so an empty manifest is behavior-neutral.
_EMPTY_ROUTING_MANIFEST = RoutingManifest(
    manifest_version=1,
    per_role_bindings={},
    per_workload_overrides={},
    fallback_chains=(),
    retry_policies={},
)


def _declarative_decline(
    _payload: ProviderAgnosticPayload, _manifest: RoutingManifest
) -> str | None:
    """A DECLARATIVE layer-decision that always DECLINES (returns ``None``).

    Used by `RuntimeLLMDispatcher.resolve_routed_binding`
    (B-L2-FALLBACK-COMPOSITION) AFTER its deterministic-binding check has
    established the request has NO deterministic binding — so DECLARATIVE must
    fall through to the EMBEDDING classifier (then the L3 router) per C-CP-02
    §2.2. A typed module-level function (not a lambda) keeps it pyright-clean
    against the ``LayerDecisionFn`` signature."""
    return None


@dataclass(frozen=True, slots=True)
class RuntimeLLMDispatcher:
    """Per-step LLM-dispatch composer satisfying `harness_cp.workflow_driver.
    StepDispatcher` Protocol (C-RT-15 + C-CP-25 §25.3.3.4).

    Constructed at bootstrap stage 5 (LOOP_INIT) with frozen references
    to the provider clients map + tracer-provider materialized at
    earlier stages. The composer is stateless across calls — each
    `dispatch` invocation is driven entirely by its arguments + the
    frozen provider/tracer substrate.

    U-OD-38 extension (2026-05-21): cost-attribution substrate added per
    C-OD-26.1 + C-OD-26.2. Post-provider-call, every dispatch invokes
    `attribute_llm_dispatch_cost(...)` to compute + persist the per-attempt
    cost-record + audit-ledger entry. Substrate (cost_chain / audit_writer /
    rate_table) is REQUIRED — cost-attribution is not optional per AC #1.

    Attributes
    ----------
    providers :
        Frozen reference to the ``ctx.providers`` map (provider_name →
        ProviderClient adapter) materialized at stage 3a per C-RT-05.
    tracer_provider :
        Frozen reference to the ``ctx.tracer_provider`` materialized at
        stage 4 OD per C-RT-06.
    cost_chain :
        Cost-attribution chain (`ctx.cost_chain`) materialized at stage 4 OD
        per C-RT-31. Consumed at every dispatch for the §C-OD-26.1
        per-attempt cost computation. U-OD-38 — required.
    audit_writer :
        Audit-ledger writer (`ctx.audit_writer`) materialized at stage 4 OD
        per C-RT-32. Receives the cost-attribution audit entry per
        §C-OD-26.3. U-OD-38 — required.
    rate_table :
        Resolved PRICE_TABLE_REF (`ctx.rate_table`) per §C-OD-28.2 —
        immutable for the workflow's lifetime. Resolves to ProviderRates per
        (provider, model) for compute_per_attempt_cost. U-OD-38 — required.
    """

    providers: _ProvidersLike
    tracer_provider: _TracerProviderLike
    # U-OD-38 cost-attribution substrate. Required at production
    # construction (enforced at bootstrap stage 5); defaulted to None to
    # preserve construction ergonomics for unit-tests that only exercise
    # the LLM-dispatch surface and don't need cost-attribution. When any
    # is None, _attribute_cost_best_effort early-returns (no-op).
    cost_chain: Any = None
    audit_writer: Any = None
    rate_table: Any = None
    # R-FS-1 arc CA — run-scoped cost-record sink (the SAME list as
    # `ctx.cost_record_accumulator`, threaded by `materialize_llm_dispatcher_stage`
    # from the mutable bootstrap ctx). `_attribute_cost_best_effort` appends each
    # dispatch's returned `SpanCostRecord` so `_build_run_result` can roll up
    # `RunResult.cost_attribution` (runtime spec v1.53 §9 C-RT-09). None at unit-test
    # construction → the wrapper skips the append (no-op).
    cost_record_sink: SupportsCostRecordAppend | None = None
    # B-INTERSTEP (runtime spec §14.21 C-RT-34) — run-scoped inter-step output
    # channel (the SAME `InterStepOutputChannel` instance the CP driver records
    # into, threaded by `materialize_llm_dispatcher_stage` from the mutable
    # bootstrap ctx). When bound + non-empty, `dispatch` injects the
    # immediately-prior step's output (`most_recent_output()`) into the dispatched
    # payload as a prepended upstream-context message. None (opt-out / unit-test
    # construction) → no injection (byte-identical to pre-v1.59). Typed `Any` so
    # the dataclass field carries no import dependency on the concrete holder.
    inter_step_channel: Any = None
    # B-LAYER-BUDGET-OVERRIDE (CP spec v1.43 §2.5.3 / §3.1) — the per-layer
    # time budgets threaded to `infer()`. The L3 router timeout honors the §3.1
    # per-workload-class / per-persona-tier override carried on these budgets
    # (resolved against the request's `workload_class` + `persona_tier`). This
    # is the DORMANT threading seam: production stage-5 leaves it at the
    # `DEFAULT_LAYER_BUDGETS` default (no override surface is wired today, and
    # production never reaches L3 — `router=None` + DECLARATIVE always resolves),
    # so the override path is capability-built + production-dormant behind the
    # (UNOWNED) routing-activation gate. A test / future operator-config binding
    # supplies an override-bearing tuple. Default is the module-global immutable
    # tuple → byte-identical to the prior hardcoded `infer(budgets=...)`.
    budgets: tuple[LayerBudget, ...] = DEFAULT_LAYER_BUDGETS
    # U-RT-81 (C-RT-15 §14.5.1) — Memory tool storage-backend registry +
    # deployment_surface for `resolve_backend` call. When `step.step_payload.
    # tools` contains the Anthropic Memory tool definition + both fields are
    # bound, the dispatcher routes the anthropic branch through the harness-
    # authored inner loop at `memory_tool_dispatch.execute_with_memory_callbacks`
    # per spec v1.17 §14.5.1 mechanism β. Both defaulted to None for unit-test
    # ergonomics; production stage-5 binding sets both per `materialize_
    # llm_dispatcher_stage` kwargs.
    memory_tool_registry: Any = None
    deployment_surface: Any = None
    # R-CXA-2 — provider-turn model-emitted tool calls can run through the
    # bound RuntimeHITLToolLoop before Anthropic continuation. None preserves
    # the historical one-shot provider dispatch path for unit tests and
    # bootstrap states where the R-CXA-2 producer loop is unavailable.
    hitl_tool_loop: RuntimeHITLToolLoop | None = None
    # OD spec v1.20 §C-OD-04 §4.3 `server.address` / `server.port` resolution
    # for the ollama provider (operator-configurable endpoint). Threaded from
    # `RuntimeConfig.ollama_host` at stage 5 per `[[fork-od-spec-declared-but-
    # not-emitted-attributes]]` Path A. Defaults to None: when unset, ollama
    # spans emit neither `server.address` nor `server.port` per OTel
    # Conditionally Required "If `server.address` is set" gating.
    ollama_host: str | None = None
    # U-RT-101 (C-RT-27 §14.17.2 hook-2 per-LLM-dispatch) — Skill activation
    # emitter + loaded skills carrier. Both default None for unit-test
    # ergonomics; production stage-5 binding sets both when the operator opts
    # in via `RuntimeConfig.skill_activation_hook_config`. When either is None
    # OR the emitter's bound hook is None, the per-LLM-dispatch activation
    # hook silent-skips per §14.17.5 invariant 3.
    skill_activation_emitter: Any = None
    skills: Any = None
    # R-300 — layered routing-selection substrate. Threaded at bootstrap stage 5
    # from `ctx.routing_manifest` (stage 3b) + the run `workload_class` +
    # `config.persona_tier`. All default None for unit-test ergonomics; the
    # DECLARATIVE-echo path tolerates a None manifest (the echo decision ignores
    # it; `_EMPTY_ROUTING_MANIFEST` substitutes) and resolves the envelope
    # discriminators to MVP placeholders when unset.
    routing_manifest: RoutingManifest | None = None
    workload_class: WorkloadClass | None = None
    persona_tier: PersonaTier | None = None
    # R-PM-1 cascade PR #1 — the active prompt's resolved system-prompt content
    # (`ctx.prompt_manifest.active_prompt_version.content or None`), bound at
    # bootstrap stage 5. None / "" → no injection (byte-identical to pre-R-PM-1
    # dispatch; the local-first default). Per-provider injection happens at the
    # translate fns (`system=` kwarg for anthropic; leading `role:"system"`
    # message for openai/ollama); `ProviderAgnosticPayload` stays frozen.
    active_system_prompt: str | None = None
    # U-MEM prompt-extension memory fallback context. When C-MEM selects
    # PROMPT_EXTENSION_PACKET, the dispatcher renders the packet as read-only
    # system content and composes it into the existing provider prompt seam.
    memory_context: RuntimeMemoryContext | None = None
    # U-MEM provider-neutral standard memory tool executor. When C-MEM selects
    # STANDARD_MEMORY_TOOLS for OpenAI, tool-call continuation dispatches
    # memory.* calls through this executor under policy.
    standard_memory_tool_executor: Any = None
    # Automatic local memory substrate. When bound, each dispatch composes a
    # fresh memory context from the current model binding and step context.
    memory_runtime: Any = None
    fallback_chain: Any = None
    # R-FS-1 arc B4 (§14.5.3) — per-role PROMPT map: branch `AgentRole` → its
    # resolved system-prompt content, pre-resolved at bootstrap stage 0 (where the
    # fail-loud store-membership + binding-tier governance checks fire — before
    # this dispatcher is constructed) and bound here. The DEFAULT role is
    # deliberately EXCLUDED (it IS `active_system_prompt`), so an empty/no-per-role
    # catalog leaves this `{}` and every dispatch falls through to
    # `active_system_prompt` (byte-identical to pre-B4). Indexed per-call at
    # `dispatch()` via the branch role (NOT stashed on self — the dispatcher is
    # shared across concurrent fan-out branches).
    per_role_system_prompts: Mapping[AgentRole, str] = field(
        default_factory=lambda: dict[AgentRole, str](),
    )
    # R-FS-1 arc B4 Slice 3 (CP spec v1.37 §6.1) — per-step PROMPT override
    # support. The IS `PromptManifest.versions` content-addressed store projected
    # to `{version_sha: content}`, built at bootstrap stage 5 from
    # `ctx.prompt_manifest.versions`. A per-step binding's `prompt_version_sha`
    # (`StepEffectiveBinding`, set by CP `resolve_step_binding`) is resolved
    # against this map at dispatch with precedence **per-step > per-role >
    # run-level default**; fail-loud (`RT-FAIL-PROMPT-SELECTION-UNAUTHORED`) if
    # the sha is not an authored store member. Empty (`{}`) when no store /
    # no per-step overrides — every dispatch falls through unchanged (pre-Slice-3
    # behavior verbatim). Keyed by content-addressed sha (not stashed per-call;
    # the per-step value flows via the binding param, not on self).
    prompt_versions_by_sha: Mapping[str, str] = field(
        default_factory=lambda: dict[str, str](),
    )
    # Binding-tier prompt governance parity (OD spec C-OD-34): a per-step prompt
    # activated at a binding tier (team-binding / multi-tenant) must be
    # operator-approved, else fail-loud (`RT-FAIL-PROMPT-VERSION-UNAPPROVED`) —
    # closing the gap where a per-step override could otherwise bypass the
    # approval the per-role/default paths enforce at bootstrap. Threaded from
    # `RuntimeConfig.approved_prompt_version_shas` at stage 5; the gate is inert
    # at the solo-developer tier (no approval required) per `resolve_prompt_governance`.
    approved_prompt_version_shas: frozenset[str] = frozenset()
    # C-CP-02 §2.5 (R-impl-1) / U-RT-133 — the injected Layer-3
    # router-resolution callable.
    # Production binds None: the Layer-3 surface stays inert (DECLARATIVE
    # resolves all production traffic; no router is injected until R-impl-2
    # binds a real router model). A test fixture injects a mock RouterResolutionFn
    # (+ `layer_decisions_override` below to force the L3 sentinel) to exercise
    # the spec-§2.5.5 runtime fall-through->router e2e.
    router: RouterResolutionFn | None = None
    # Test-only seam (C-CP-02 §2.5.5 runtime e2e): override the layer-decision
    # map threaded into `infer`. None -> the production
    # `{RoutingLayer.DECLARATIVE: _declarative_echo}` (byte-identical). A test
    # injects e.g. `{RoutingLayer.DECLARATIVE: lambda *_: None}` to force the
    # `route()` L3 sentinel so the injected `router` is reached. NOT a production
    # override; production never sets it (the factory leaves it None).
    layer_decisions_override: Mapping[RoutingLayer, LayerDecisionFn] | None = None
    # C-CP-02 §2.1/§2.4 (R-FS-1 L2 — Option B) — the injected Layer-2 EMBEDDING
    # decision fn (a sync k-NN embedding classifier; built by
    # `embedding_resolution.make_embedding_classifier` over an injected light
    # in-process `EmbeddingFn` + a trained corpus). When set, it is bound at the
    # EMBEDDING layer of the production `layer_decisions` map below. Production
    # leaves it None: like the Layer-3 `router`, the Layer-2 surface stays
    # production-inert until R-300-second-provider makes DECLARATIVE conditional
    # (DECLARATIVE always echoes today, so `route()` short-circuits before
    # EMBEDDING). A test reaches it via `layer_decisions_override` (binding the
    # classifier at EMBEDDING + omitting DECLARATIVE).
    embedding_classifier: LayerDecisionFn | None = None
    # B-L2-EMBEDDING-ACTIVATION (C-CP-02 §2.2 — the routing-activation gate) — when
    # True, the DECLARATIVE layer is §2.2-FAITHFUL: it resolves the effective binding
    # ONLY when the routing manifest binds the request's tuple (`agent_role ∈
    # per_role_bindings` OR `workload_class ∈ per_workload_overrides`); otherwise it
    # DECLINES (returns None) so `route()` falls through to the EMBEDDING classifier
    # (then LLM_AS_ROUTER) — the cheapest-deterministic-first §2.2 contract. False
    # (default) → the #213 MVP behavior-preserving echo (DECLARATIVE always resolves)
    # → byte-identical, ZERO blast radius. Threaded from `RuntimeConfig.routing_
    # activation` at stage 5. HIGHEST-blast-radius opt-in (changes which model serves
    # a workload); flag-on assumes a bound `embedding_classifier` as the fall-through
    # target — the production factory (`materialize_llm_dispatcher_stage`) builds one
    # (or fail-louds on a missing `[embedding]` extra), so production is safe; a
    # DIRECT construction with flag-on but no classifier would let a manifest-miss
    # fall through EMBEDDING (empty) → L3 (`router=None`) → a dispatch raise.
    routing_activation: bool = False

    def _resolve_per_step_system_prompt(self, prompt_version_sha: str) -> str:
        """Resolve a per-step ``prompt_version_sha`` → injected content (B4 Slice 3).

        The per-step override (CP ``StepOverride.prompt_version_sha``, applied by
        ``resolve_step_binding``) names a content-addressed member of the IS
        ``PromptManifest.versions`` store. This resolves it to content via the
        stage-5 ``prompt_versions_by_sha`` map, running the SAME fail-loud guards
        the per-role bootstrap path runs — but at this per-dispatch site (the
        per-step binding is only known here, not at bootstrap):

        * unauthored sha → :class:`PromptSelectionUnauthoredError`
          (``RT-FAIL-PROMPT-SELECTION-UNAUTHORED``) — a selected version must be
          an authored store member;
        * binding-tier governance parity → :class:`PromptVersionUnapprovedError`
          (``RT-FAIL-PROMPT-VERSION-UNAPPROVED``) — at a tier requiring approval
          (OD C-OD-34), the version's sha must be a member of
          ``approved_prompt_version_shas``.

        Governance keys on the **deployment** persona tier (``self.persona_tier``,
        threaded from ``config.persona_tier`` at stage 5) — NOT the per-workflow
        ``binding.persona_tier`` — exactly as the default/per-role approval gate
        does at bootstrap. Keying on the per-workflow tier would let a workflow
        whose manifest declares ``SOLO_DEVELOPER`` bypass the approval a
        binding-tier *deployment* requires (Codex P1). ``self.persona_tier`` is
        ``None`` only in the unit-test-ergonomics path (production stage 5 always
        binds ``config.persona_tier``); a ``None`` deployment tier is treated as
        no-governance-context (inert), never a binding tier.

        Both surface as a STEP failure here (per-dispatch — propagates to the CP
        driver try/except), unlike the per-role path's BootstrapFailure. Reuses the
        canonical fail-class exceptions so the audit fail-class catalog stays
        single-sourced. Lazy imports mirror the existing ``SkillActivationMode``
        lazy-import pattern in ``dispatch`` (keeps the module-load edge minimal).
        """
        from harness_od.prompt_governance_gradient import resolve_prompt_governance

        from harness_runtime.lifecycle.prompt_selection import (
            PromptSelectionUnauthoredError,
            PromptVersionUnapprovedError,
        )

        content = self.prompt_versions_by_sha.get(prompt_version_sha)
        if content is None:
            raise PromptSelectionUnauthoredError(prompt_version_sha)
        deployment_tier = self.persona_tier
        if (
            deployment_tier is not None
            and resolve_prompt_governance(deployment_tier).approval_required
            and prompt_version_sha not in self.approved_prompt_version_shas
        ):
            raise PromptVersionUnapprovedError(
                persona_tier=deployment_tier, version_sha=prompt_version_sha
            )
        return content

    async def resolve_routed_binding(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> RoutedPrimaryResolution | None:
        """Resolve the layered-routing PRIMARY candidate ONCE for the C-RT-16
        wrapper (B-L2-FALLBACK-COMPOSITION, `Spec_Harness_Runtime_v1.md` §14.6).

        The SELECTION half of `dispatch` — runs the layered routing strategy
        (DECLARATIVE-decline → EMBEDDING classifier → L3 router per C-CP-02 §2.2)
        and returns a ``RoutedPrimaryResolution`` (the routed ``ModelBinding`` +
        the resolving ``RoutingDecisionTrace`` + binding rationale) WITHOUT
        dispatching — the trace rides to the inner span per
        B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION (§14.6.1). The C-RT-16
        ``RetryBreakerFallbackDispatcher`` calls this ONCE per step (via a direct
        handle the stage-5 factory hands it — NOT through its HITL/inner chain)
        and SEEDS the routed model as its PRIMARY fallback candidate, so the
        layered routing decision composes with the fallback chain. The inner
        ``dispatch`` stays FAITHFUL — it dispatches the wrapper's rebound
        candidate exactly once per attempt (§14.6). This is the U-RT-114 §14.5.3
        pattern applied to layered routing: model-candidate selection lives at
        the wrapper as the chain PRIMARY, never re-run per fallback attempt (the
        §14.5.3 "no two-authority-at-dispatch" invariant the inner-routing
        re-evaluation otherwise breaks).

        Returns ``None`` — "no routing augmentation; the wrapper's existing
        per-role / stage chain governs" — when:

        - ``routing_activation`` is off (the default → byte-identical to the
          pre-B-L2 always-echo dispatch); OR
        - the request has a DETERMINISTIC binding (a per-step override
          ``override_applied``, a per-role ``per_role_bindings`` entry, or a
          MODEL-BEARING per-workload override). DECLARATIVE would RESOLVE (echo
          ``binding.model_binding``), which IS the wrapper's existing chain
          primary — so no augmentation is needed, and a per-role binding's own
          U-RT-114 primary augmentation takes PRECEDENCE (routing fills only the
          no-deterministic-binding gap — the mutual-exclusivity §2.2's
          ``_has_deterministic_binding`` check encodes); OR
        - no EMBEDDING classifier AND no L3 router is configured (nothing to
          route to → faithful; defensively, the factory builds the default
          EMBEDDING classifier when ``routing_activation`` is on).

        Raises
        ------
        RoutingCandidateUnresolvedError
            DECLARATIVE declined and EMBEDDING + L3 produced no well-formed
            candidate (§2.5.2/§2.5.3). The wrapper's ``dispatch`` surfaces it as
            a step failure, exactly as the inner dispatch surfaces it on the
            non-routing path.
        """
        if not self.routing_activation:
            return None
        _role = step_context.agent_role or _MVP_DEFAULT_AGENT_ROLE
        manifest = self.routing_manifest or _EMPTY_ROUTING_MANIFEST
        _workload = self.workload_class or _MVP_DEFAULT_WORKLOAD_CLASS
        _workload_override = manifest.per_workload_overrides.get(_workload)
        # B-MODEL-RESOLUTION-CONSOLIDATION §14.6.2 — the decline predicate MUST
        # mirror the wrapper's `_effective_chain` deterministic branches EXACTLY
        # (per-step MODEL / per-workload MODEL / per-role). It is an order-
        # independent disjunction, so the per-workload/per-role precedence swap does
        # not affect it. The decline may be more LENIENT than the authority (a wasted
        # routing call `_effective_chain` ignores) but NEVER stricter: a stricter
        # decline returns None while the authority also skips the source ⟹ the model
        # silently drops to default. Each conjunct pairs with a branch:
        #   - `model_binding_override is not None`  ⟷ per-step branch
        #     (was `binding.override_applied` — over-declined on a NON-model
        #     per-step override [hitl/engine-only], suppressing routing spuriously)
        #   - the per-workload conjunct ⟷ per-workload branch (same
        #     `self.workload_class or _MVP_DEFAULT_WORKLOAD_CLASS`)
        #   - `_role != _MVP_DEFAULT_AGENT_ROLE and _role in per_role_bindings`
        #     ⟷ per-role branch (the default-role exclusion mirrors the authority)
        _has_deterministic_binding = (
            binding.model_binding_override is not None
            or (
                _workload_override is not None
                and _workload_override.model_binding_override is not None
            )
            or (_role != _MVP_DEFAULT_AGENT_ROLE and _role in manifest.per_role_bindings)
        )
        if _has_deterministic_binding:
            # A higher-than-routed MODEL source governs (per-step / per-workload /
            # per-role) — the wrapper's `_effective_chain` owns the PRIMARY for it;
            # routing declines so it fills only the no-override gap.
            return None
        if self.embedding_classifier is None and self.router is None:
            # Nothing to route to (no L2 classifier + no L3 router) → faithful.
            # Defensive: `materialize_llm_dispatcher_stage` builds the default
            # EMBEDDING classifier when `routing_activation` is on.
            return None
        payload = _coerce_payload(step.step_payload)
        envelope = InferenceRequest(
            agent_role=_role,
            workload_class=_workload,
            persona_tier=self.persona_tier or PersonaTier.SOLO_DEVELOPER,
            context_tokens=len(payload.messages),
            request_payload=payload,
            trace_context=_MVP_PLACEHOLDER_TRACE_CONTEXT,
        )
        # Past the deterministic check → DECLARATIVE DECLINES so `route()` falls
        # through to the injected EMBEDDING classifier (then the L3 router). ONE
        # source of truth for the routing layers — the same EMBEDDING/L3 surface
        # the inner held, now consulted ONCE here for the wrapper's primary.
        routing_layer_decisions: dict[RoutingLayer, LayerDecisionFn] = {
            RoutingLayer.DECLARATIVE: _declarative_decline
        }
        if self.embedding_classifier is not None:
            routing_layer_decisions[RoutingLayer.EMBEDDING] = self.embedding_classifier
        trace, _binding_rationale = await resolve_routing_trace(
            envelope,
            manifest=manifest,
            layer_decisions=routing_layer_decisions,
            budgets=self.budgets,
            router=self.router,
        )
        provider, _sep, model = trace.candidate.partition(":")
        return RoutedPrimaryResolution(
            model_binding=ModelBinding(provider=provider, model=model),
            routing_trace=trace,
            binding_rationale=_binding_rationale,
        )

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Invoke the step body under the effective binding; return step output.

        ``step_context`` parameter accepted at v1.6 Path A per amended
        ``StepDispatcher`` Protocol (C-RT-17 resolution). C-RT-15 does NOT
        consume ``step_context`` at v1.6; reserved for v1.7+ surfaces that
        may bind parent context to the LLM inference span attributes per
        future C-RT-NN amendments.

        Per C-RT-15 §Specification content steps 1-5. Provider-specific
        dispatch branches are exhaustive over the three providers
        constructed at C-RT-05 stage 3a (anthropic / openai / ollama).

        R-300 (2026-06-01): provider/model SELECTION now flows through the
        layered routing strategy via `infer`
        (`harness_cp.routing_core_surface`) rather than reading
        `binding.model_binding` directly. `infer` composes `route` (U-CP-05)
        with `_invoke_provider` (this dispatcher's provider-SDK boundary) as
        its injected dispatch callable. At MVP the DECLARATIVE layer echoes the
        resolved `binding.model_binding` (== the manifest role binding with
        per-step overrides applied), so selection is behavior-preserving; the
        new behavior is the `routing.*` span attribution (C-CP-01 §1.4) on the
        `llm.inference` span. `infer` runs inside each RetryBreakerFallback
        attempt; at the echo it is idempotent and cross-family fallback remains
        the wrapper's responsibility (R-300-second-provider).

        Raises
        ------
        LLMDispatchProviderUnreachableError
            ``binding.model_binding.provider`` not in ``self.providers``.
            Maps to ``RT-FAIL-PROVIDER-UNREACHABLE`` per C-RT-14.
        LLMDispatchPayloadShapeError
            ``step.step_payload`` not coercible to `ProviderAgnosticPayload`.
        Exception
            Any provider-side SDK exception propagates unmodified per
            Q2a scope discipline. The CP driver's ``except`` boundary
            at `workflow_driver.py:380-389` fails the step with
            ``step-failure: {type}: {exc}``.
        """
        # --- C-RT-27 §14.17.2 hook-2 (per-LLM-dispatch activation hook) ---
        # Pre-condition: emitter bound + hook bound + skills available. When
        # any is missing/None, silent-skip per §14.17.5 invariant 3 (operator
        # opt-out path preserves pre-v1.32 production behavior). Emit one
        # `skill.activation` span per skill returned by the operator-supplied
        # `SkillActivationHook.select_for_llm_dispatch(...)` policy, with
        # `activation_mode = TOOL_SEARCH` per Q2=(d) hybrid hook-to-enum
        # mapping. Fires BEFORE provider resolution + LLM call per §14.17.2
        # hook-2 step 4 ordering.
        if (
            self.skill_activation_emitter is not None
            and self.skill_activation_emitter.hook is not None
            and self.skills is not None
        ):
            from harness_runtime.lifecycle.skill_activation import SkillActivationMode

            selected_ids = list(
                self.skill_activation_emitter.hook.select_for_llm_dispatch(
                    loaded_skills=self.skills.keys(),
                    workflow_id=step_context.workflow_id,
                    step_index=step_context.step_index,
                )
            )
            for skill_id in selected_ids:
                if skill_id in self.skills:
                    self.skill_activation_emitter.emit(
                        skill_id=skill_id,
                        mode=SkillActivationMode.TOOL_SEARCH,
                        workflow_id=step_context.workflow_id,
                        skill=self.skills[skill_id],
                    )

        payload = _coerce_payload(step.step_payload)

        # --- B-POSTJOIN effect-free boundary guard (out-of-family Codex round 8 [P1]) ---
        # The LOAD-BEARING enforcement of the synthesis effect-free invariant (CP spec
        # v1.54 §3). This is the single convergence point for ALL payload sources: the
        # original compose, the `params` route, AND a post-HITL-EDIT replacement — the
        # `RuntimeHITLGateComposer` wraps this dispatcher, so a PRE_ACTION EDIT that
        # re-introduces tools lands in `step.step_payload` BEFORE this `_coerce_payload`.
        # The compose-time guard ran upstream and CANNOT see the edited payload; this
        # boundary is the real floor (`[[enforce-floor-no-bypass-seam]]`). A
        # POST_JOIN_SYNTHESIS step is read-only / effect-free — reject any tool binding
        # before the provider call (the synthesis except in `_maybe_post_join_synthesis`
        # maps this to a FAILED RunResult). Scoped to the SAFETY half only: an EDIT that
        # drops the sibling context is operator intent shown at the gate (CP spec v1.54 §3),
        # not a silent break.
        if step.step_kind is StepKind.POST_JOIN_SYNTHESIS:
            from harness_runtime.lifecycle.post_join_synthesis_dispatch import (
                post_join_tool_binding_violations,
            )

            _synth_violations = post_join_tool_binding_violations(payload.tools, payload.params)
            if _synth_violations:
                raise LLMDispatchPayloadShapeError(
                    "post-join-synthesis step is read-only / effect-free but the dispatched "
                    f"payload binds tools ({', '.join(_synth_violations)}); rejected at the LLM "
                    "dispatch boundary (this catches a post-HITL-EDIT re-introduction the "
                    "compose-time guard cannot see). CP spec v1.54 §3."
                )

        # --- B-INTERSTEP (runtime spec §14.21 C-RT-34): upstream-output read ---
        # The immediately-prior step's output (the opt-in inter-step channel's
        # most-recent record). It is injected into the FINAL provider kwargs at the
        # translator layer (`_inject_upstream_context_message`), NOT into
        # `payload.messages` here, so (1) a `params["messages"]` escape-hatch
        # override cannot silently drop it and (2) it runs AFTER the system-prompt
        # injection so it cannot mask the competing-system-source conflict check
        # (Codex review; mirrors the R-PM-1 post-`params` system-prompt injection).
        # None (opt-out) / empty channel → no injection (byte-identical to pre-v1.59).
        _upstream_output: Mapping[str, Any] | None = (
            self.inter_step_channel.most_recent_output()
            if self.inter_step_channel is not None
            else None
        )

        # --- U-RT-114 (C-RT-15 §14.5.3): branch AgentRole carry ----------
        # The branch role (the CP-composed child StepExecutionContext, CP plan
        # v2.32 U-CP-81) is carried for the `InferenceRequest` envelope's
        # `agent_role` attribution below. Per-role MODEL selection is resolved
        # ONE LAYER OUT, at the C-RT-16 `RetryBreakerFallbackDispatcher` (the
        # dispatch-composition surface that owns candidate selection): the
        # per-role model is that wrapper's PRIMARY fallback candidate, so per-role
        # specialization composes with fallback — ONE source of truth for model
        # selection (the wrapper's chain). Here the inner faithfully dispatches
        # `binding.model_binding` (the wrapper's rebound candidate). §14.5.3
        # mechanism + all 3 invariants preserved; indexing the per-role model at
        # the inner too would create TWO authorities (wrapper candidate vs inner
        # override) and silently defeat fallback for role-routed branches (the
        # C-RT-16 composition gap). MODEL BINDING ONLY — the per-role PROMPT is
        # resolved once at stage 0 with the default role (deferred to B4, §14.5.3).
        _role = step_context.agent_role or _MVP_DEFAULT_AGENT_ROLE
        _effective_model_binding = binding.model_binding

        # --- B4 (C-RT-15 §14.5.3): per-role PROMPT dispatch-read --------------
        # Index the branch role into the pre-resolved per-role system-prompt map
        # (built at bootstrap stage 0). EXPLICIT membership check (not `or`): a
        # bound role uses its resolved content even when "" (→ no injection per
        # §14.5.2), keeping "bound-but-empty" distinct from "unbound". An unbound
        # role — including the default/linear path, never in the map — falls
        # through to the stage-0 default-role `active_system_prompt` (the
        # §14.5.3 suggested fall-through-to-default lookup-miss policy). Per-call
        # LOCAL (never on self): the dispatcher is shared across concurrent
        # fan-out branches, so a per-role value MUST flow as a dispatch parameter.
        # --- B4 Slice 3 (CP spec v1.37 §6.1): per-step PROMPT override ---------
        # A per-step `prompt_version_sha` on the resolved binding (CP
        # `resolve_step_binding` applied a `StepOverride.prompt_version_sha`)
        # overrides BOTH the per-role and run-level-default prompts for THIS step.
        # Precedence: per-step > per-role > run-level default. The per-step sha is
        # resolved to content + governed HERE (the per-dispatch site) — not at
        # bootstrap like per-role — because the per-step binding is only known at
        # dispatch (it is a per-workflow `WorkflowManifestEntry.per_step_overrides`
        # entry, resolved CP-driver-side, not run-level config). Fail-loud on an
        # unauthored sha / unapproved binding-tier version surfaces as a STEP
        # failure (propagates to the driver try/except, like
        # RT-FAIL-PROMPT-INJECTION-CONFLICT), not a BootstrapFailure.
        if binding.prompt_version_sha is not None:
            _effective_system_prompt = self._resolve_per_step_system_prompt(
                binding.prompt_version_sha
            )
        elif _role in self.per_role_system_prompts:
            _effective_system_prompt = self.per_role_system_prompts[_role]
        else:
            _effective_system_prompt = self.active_system_prompt
        _memory_context = self.memory_context
        _standard_memory_tool_executor = self.standard_memory_tool_executor
        if self.memory_runtime is not None:
            if self.fallback_chain is None:
                raise LLMDispatchBindError(
                    "automatic memory runtime requires a fallback_chain binding"
                )
            _memory_context = self.memory_runtime.compose_for_dispatch(
                binding=binding,
                fallback_chain=self.fallback_chain,
                step=step,
                step_context=step_context,
            )
            if _standard_memory_tool_executor is None:
                _standard_memory_tool_executor = getattr(
                    self.memory_runtime,
                    "standard_memory_tool_executor",
                    None,
                )
        _effective_system_prompt = compose_system_prompt_with_memory_packet(
            _effective_system_prompt,
            _memory_context,
        )

        # --- R-300 / B-L2-FALLBACK-COMPOSITION: faithful dispatch-read ----------
        # The DECLARATIVE layer decision ECHOES the effective binding
        # (`binding.model_binding` — the C-RT-16 wrapper's rebound candidate: the
        # stage-chain primary, the U-RT-114 per-role model, or the routing_activation
        # routed primary). The inner C-RT-15 dispatch is FAITHFUL — it dispatches
        # whatever the wrapper rebinds, exactly once per attempt (§14.6 "the wrapper
        # invokes the inner dispatcher exactly once per attempt with a rebound
        # binding"). Layered-routing ACTIVATION (routing_activation → DECLARATIVE-
        # decline → EMBEDDING/L3 picking a DIFFERENT model) is NOT performed here: at
        # the inner it re-runs per fallback attempt and silently defeats the wrapper's
        # candidate chain (the §14.5.3 "no two-authority-at-dispatch" anti-pattern).
        # The routing decision is made ONCE, ONE LAYER OUT, at the C-RT-16 wrapper via
        # `resolve_routed_binding` (below), which seeds the routed model as the
        # wrapper's PRIMARY fallback candidate — so layered routing composes with the
        # fallback chain (B-L2-FALLBACK-COMPOSITION, runtime §14.6). `route()`'s
        # `manifest` arg + the envelope discriminators are carried for `routing.*`
        # span attribution.
        def _declarative_echo(
            _payload: ProviderAgnosticPayload, _manifest: RoutingManifest
        ) -> str | None:
            return f"{_effective_model_binding.provider}:{_effective_model_binding.model}"

        # `infer()` requires an InferenceRequest envelope. Its discriminator
        # fields are carried for the C-CP-01 §1.1 API surface but DISCARDED at
        # this boundary: the live routing decision is surfaced as `routing.*`
        # span attrs inside `_invoke_provider`, and the step output is the raw
        # provider Mapping returned below — NOT the InferenceResponse (which is
        # likewise discarded). They become load-bearing at R-300-second-provider.
        envelope = InferenceRequest(
            agent_role=_role,
            workload_class=self.workload_class or _MVP_DEFAULT_WORKLOAD_CLASS,
            persona_tier=self.persona_tier or PersonaTier.SOLO_DEVELOPER,
            context_tokens=len(payload.messages),
            request_payload=payload,
            trace_context=_MVP_PLACEHOLDER_TRACE_CONTEXT,
        )

        # The raw provider response Mapping (the step output) is surfaced out of
        # the `infer()` composition via this holder — `infer()` returns an
        # InferenceResponse, but the step-output contract is the raw Mapping.
        raw_response: dict[str, Mapping[str, Any]] = {}

        async def _provider_dispatch(
            provider: str,
            model: str,
            inner_payload: ProviderAgnosticPayload,
            routing_trace: RoutingDecisionTrace,
            *,
            binding_rationale: str | None = None,
        ) -> ProviderDispatchResult:
            # C-CP-02 §2.5.4 — `binding_rationale` is the optional carrier
            # `infer()` threads ONLY on the Layer-3 router path (None on the
            # non-router path, so the span emitter keeps deriving
            # `f"{layer}:{candidate}"`). Matches the §2.5.4 Protocol-ized
            # `ProviderDispatchFn` seam (the defaulted kwarg keeps this closure
            # assignable + every non-router call byte-identical).
            response = await self._invoke_provider(
                provider,
                model,
                inner_payload,
                step_context,
                step_id=str(step.step_id),
                routing_trace=routing_trace,
                binding_rationale=binding_rationale,
                effective_system_prompt=_effective_system_prompt,
                upstream_output=_upstream_output,
                memory_context=_memory_context,
                standard_memory_tool_executor=_standard_memory_tool_executor,
            )
            raw_response["value"] = response
            # ProviderDispatchResult is structurally required by `infer()` but
            # DISCARDED at this boundary: the raw `response` Mapping is the step
            # output, and gen_ai.usage.* + cost-attribution are already emitted +
            # persisted inside `_invoke_provider`. Minimal placeholder.
            return ProviderDispatchResult(
                response_payload=ProviderAgnosticPayload(messages=(), tools=None, params={}),
                tokens_in=0,
                tokens_out=0,
                cached_tokens_in=0,
            )

        # C-CP-02 §2.1 production layer-decision map for the FAITHFUL inner
        # dispatch: DECLARATIVE-echo only. The DECLARATIVE echo always resolves
        # `binding.model_binding` (the wrapper's rebound candidate), so `route()`
        # short-circuits at DECLARATIVE and the inner faithfully dispatches it
        # (§14.6). Layered-routing ACTIVATION (the EMBEDDING classifier + the L3
        # router) is consulted ONCE at the C-RT-16 wrapper via
        # `resolve_routed_binding`, NOT here — so routing composes with fallback
        # instead of re-routing per attempt (B-L2-FALLBACK-COMPOSITION, §14.6).
        production_layer_decisions: dict[RoutingLayer, LayerDecisionFn] = {
            RoutingLayer.DECLARATIVE: _declarative_echo
        }

        await infer(
            envelope,
            dispatch=_provider_dispatch,
            manifest=self.routing_manifest or _EMPTY_ROUTING_MANIFEST,
            # `layer_decisions_override` is the test-only seam (C-CP-02 §2.5.5):
            # None -> the production map (DECLARATIVE-echo [+ EMBEDDING when a
            # classifier is injected]). A test injects a sentinel-forcing map to
            # reach the injected `router`, or an EMBEDDING-only map to reach L2.
            layer_decisions=(
                self.layer_decisions_override
                if self.layer_decisions_override is not None
                else production_layer_decisions
            ),
            # B-LAYER-BUDGET-OVERRIDE — the threaded budgets (DORMANT seam:
            # `DEFAULT_LAYER_BUDGETS` at production stage-5; an override-bearing
            # tuple resolves the §3.1 L3 per-persona/per-workload override).
            budgets=self.budgets,
            # C-CP-02 §2.5 — production binds None (Layer-3 inert); a test
            # fixture injects a mock RouterResolutionFn.
            router=self.router,
        )
        return raw_response["value"]

    async def _invoke_provider(
        self,
        provider_name: str,
        model: str,
        payload: ProviderAgnosticPayload,
        step_context: StepExecutionContext,
        *,
        step_id: str,
        routing_trace: RoutingDecisionTrace,
        binding_rationale: str | None = None,
        effective_system_prompt: str | None = None,
        upstream_output: Mapping[str, Any] | None = None,
        memory_context: RuntimeMemoryContext | None = None,
        standard_memory_tool_executor: Any = None,
    ) -> Mapping[str, Any]:
        """Provider-SDK dispatch boundary — the injected dispatch callable for
        `infer()` (R-300). Opens the `llm.inference` span (gen_ai.* + routing.*
        attribution), dispatches to the routed provider, runs cost-attribution,
        and returns the raw provider response Mapping (the step output).

        ``provider_name`` + ``model`` are the routing-selected candidate (per
        `route()` per U-CP-05); ``routing_trace`` carries the routing decision
        for `routing.*` span attribution per C-CP-01 §1.4.

        ``effective_system_prompt`` is the per-call system prompt the §14.5.2
        translate seam injects — the per-role prompt for a role-bound fan-out
        branch (B4 §14.5.3), else `self.active_system_prompt` (the stage-0
        default-role prompt). Passed as a parameter (not read from `self`)
        because the dispatcher is shared across concurrent branches.

        Raises
        ------
        LLMDispatchProviderUnreachableError
            ``provider_name`` not in ``self.providers``.
            Maps to ``RT-FAIL-PROVIDER-UNREACHABLE`` per C-RT-14.
        """
        # --- Step 1: provider resolution --------------------------------
        if provider_name not in self.providers:
            raise LLMDispatchProviderUnreachableError(provider_name)

        adapter = self.providers[provider_name]

        # --- Step 2: open GenAI-semconv span ----------------------------
        # Span name per OD spec v1.12 §C-OD-04 §4.1 (2-token space-separated):
        # `{gen_ai.operation.name} {gen_ai.request.model}`. Byte-exact to OTel
        # GenAI semantic conventions 1.41.0 archived text per
        # `.harness/class_1_fork_genai_span_name_four_way_drift.md` §7.4.2 (R1).
        # Required (Stable) attribute set per §4.3: `gen_ai.operation.name` +
        # `gen_ai.provider.name` + `gen_ai.request.model` — all 3 emitted at
        # step 2 below per fork §"Adjacent observations" (f) RESOLVED arc.
        # Finding (g) RESOLVED at `_PROVIDER_OPERATIONS` binding above —
        # the operation-token in the span name + the `gen_ai.operation.name`
        # attribute value both source from `GenAiOperation.CHAT.value`
        # ("chat") per §4.2 enum.
        tracer = self.tracer_provider.get_tracer("harness.runtime.llm_dispatch")
        operation = _PROVIDER_OPERATIONS.get(provider_name)
        if operation is None and isinstance(adapter, _ExternalCLIProviderLike):
            operation = GenAiOperation.CHAT
        if operation is None:
            # Defensive — every key in self.providers is one of the
            # three constructed at stage 3a per C-RT-05. Surfacing any
            # other key as UNREACHABLE preserves the C-RT-14 taxonomy.
            raise LLMDispatchProviderUnreachableError(provider_name)
        span_name = f"{operation.value} {model}"

        # OTel tracer CM is synchronous (returns ``ContextManager``, not
        # ``AsyncContextManager``); spec §14.5 phrasing is imprecise.
        with tracer.start_as_current_span(span_name) as span:
            # §4.3 Required (Stable) tier — all 2 attributes always emitted
            # (per v1.19 §1.1 redistribution: `gen_ai.operation.name` +
            # `gen_ai.provider.name`).
            span.set_attribute("gen_ai.operation.name", operation.value)
            span.set_attribute("gen_ai.provider.name", provider_name)
            # §4.3 Conditionally Required tier — harness emits the model
            # unconditionally (always known at dispatch) + the conversation
            # id from `step_context.workflow_id` (always known per CP spec
            # v1.12 §25.2.1 9th-field). `server.port` emission below is
            # gated on `server.address` per the OTel canonical condition.
            # Closes `[[fork-od-spec-declared-but-not-emitted-attributes]]`
            # finding (g) Path A 2026-05-27.
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute(HIERARCHY_CORRELATION_KEY, step_context.workflow_id)
            # §4.3 `server.address` Recommended (Development) + `server.port`
            # Conditionally Required ("If `server.address` is set"). Hosted
            # providers resolve from static maps; ollama resolves from the
            # dispatcher-bound `ollama_host` (None → no emission per OTel
            # Conditionally Required gating). Closes Path A finding (f).
            if provider_name == "ollama":
                server_address, server_port = _parse_ollama_host(self.ollama_host)
            else:
                server_address = _PROVIDER_SERVER_ADDRESS.get(provider_name)
                server_port = _PROVIDER_SERVER_PORT.get(provider_name)
            if server_address is not None:
                span.set_attribute("server.address", server_address)
                if server_port is not None:
                    span.set_attribute("server.port", server_port)

            # --- routing.* attribution (C-CP-01 §1.4; R-300) ------------
            # The layered routing decision (`route()` per U-CP-05, composed by
            # `infer()`) attaches to the `llm.inference` span per §1.4. The full
            # §1.4 set is emitted so routing visibility is complete on the span
            # (the canonical routing-visibility surface) — `infer()`'s
            # InferenceResponse is discarded at the dispatch boundary, so the
            # span is where routing visibility lives. `routing.binding_rationale`
            # is the §1.4 optional token. C-CP-02 §2.5.4: when `infer()` resolves
            # via a Layer-3 router it threads the router's free-text rationale
            # through the dispatch seam (`binding_rationale`); the emitter uses
            # it WHEN PRESENT. On every other path `binding_rationale is None`
            # and the emitter keeps deriving the layer+candidate token
            # (byte-identical to pre-§2.5 — the non-router path is unchanged).
            # B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION (§14.6.1 scope-boundary
            # closure): when the C-RT-16 wrapper resolved a routed PRIMARY it
            # publishes the REAL `(routing_trace, binding_rationale)` for THIS
            # dispatch — use it so `routing.layer` reports the EMBEDDING/L3 layer
            # that PICKED the model, not the inner's faithful DECLARATIVE echo.
            # Absent (routing off / a fallback candidate / any direct non-wrapper
            # dispatch) → the local echo trace (pre-arc behavior, byte-identical).
            # `routing.provider`/`routing.model` always reflect the actually
            # dispatched candidate (correct on every path), so only the layer +
            # rationale are sourced from the published trace.
            _routed_span = ROUTED_PRIMARY_SPAN_TRACE.get()
            _eff_trace = _routed_span[0] if _routed_span is not None else routing_trace
            _eff_rationale = _routed_span[1] if _routed_span is not None else binding_rationale
            span.set_attribute("routing.provider", provider_name)
            span.set_attribute("routing.model", model)
            span.set_attribute("routing.layer", _eff_trace.layer)
            span.set_attribute(
                "routing.binding_rationale",
                _eff_rationale
                if _eff_rationale is not None
                else f"{_eff_trace.layer}:{_eff_trace.candidate}",
            )

            # --- Step 3: per-provider dispatch --------------------------
            cache_attrs: _AnthropicCacheAttrs | None
            request_attrs: _AnthropicRequestAttrs | None
            external_cli_attrs: _ExternalCLIAttrs | None = None
            if provider_name == "anthropic":
                # U-RT-81 (C-RT-15 §14.5.1) — Memory tool callback-injection
                # composer-step. If `step.step_payload.tools` contains the
                # Anthropic Memory tool definition AND the registry + surface
                # are bound, route through the harness-authored inner loop
                # (mechanism β). Otherwise the existing §14.5 step 4 path
                # preserves verbatim. The branch detection is intentionally
                # cheap (single `step_has_memory_tool` predicate over the
                # `payload.tools` sequence) so non-memory dispatches see
                # zero overhead.
                if (
                    self.memory_tool_registry is not None
                    and self.deployment_surface is not None
                    and step_has_memory_tool(payload.tools)
                ):
                    (
                        response,
                        usage_attrs,
                        cache_attrs,
                        request_attrs,
                    ) = await _dispatch_anthropic_with_memory(
                        adapter,
                        model,
                        payload,
                        registry=self.memory_tool_registry,
                        deployment_surface=self.deployment_surface,
                        tracer=tracer,
                        system=effective_system_prompt,
                        upstream=upstream_output,
                    )
                elif (
                    self.hitl_tool_loop is not None
                    and payload.tools is not None
                    and not step_has_memory_tool(payload.tools)
                ):
                    (
                        response,
                        usage_attrs,
                        cache_attrs,
                        request_attrs,
                    ) = await _dispatch_anthropic_with_hitl_tool_loop(
                        adapter,
                        model,
                        payload,
                        hitl_tool_loop=self.hitl_tool_loop,
                        step_context=step_context,
                        step_id=step_id,
                        persona_tier=self.persona_tier or PersonaTier.SOLO_DEVELOPER,
                        system=effective_system_prompt,
                        upstream=upstream_output,
                    )
                else:
                    (
                        response,
                        usage_attrs,
                        cache_attrs,
                        request_attrs,
                    ) = await _dispatch_anthropic(
                        adapter,
                        model,
                        payload,
                        system=effective_system_prompt,
                        upstream=upstream_output,
                    )
            elif provider_name == "openai":
                if (
                    standard_memory_tool_executor is not None
                    and memory_context is not None
                    and memory_context.access_mode is MemoryAccessMode.STANDARD_MEMORY_TOOLS
                ):
                    response, usage_attrs = await _dispatch_openai_with_standard_memory_tools(
                        adapter,
                        model,
                        payload,
                        memory_context=memory_context,
                        standard_memory_tool_executor=standard_memory_tool_executor,
                        step_context=step_context,
                        step_id=step_id,
                        system=effective_system_prompt,
                        upstream=upstream_output,
                    )
                else:
                    response, usage_attrs = await _dispatch_openai(
                        adapter,
                        model,
                        payload,
                        system=effective_system_prompt,
                        upstream=upstream_output,
                    )
                cache_attrs = None
                request_attrs = None
            elif provider_name == "ollama":
                response, usage_attrs = await _dispatch_ollama(
                    adapter,
                    model,
                    payload,
                    system=effective_system_prompt,
                    upstream=upstream_output,
                )
                cache_attrs = None
                request_attrs = None
            elif isinstance(adapter, _ExternalCLIProviderLike):
                response, usage_attrs, external_cli_attrs = await _dispatch_external_cli(
                    adapter,
                    provider_name,
                    model,
                    payload,
                    system=effective_system_prompt,
                    upstream=upstream_output,
                )
                cache_attrs = None
                request_attrs = None
            else:
                raise LLMDispatchProviderUnreachableError(provider_name)

            # --- Step 4: populate response-side attributes --------------
            _set_if_present(span, "gen_ai.usage.input_tokens", usage_attrs.input_tokens)
            _set_if_present(span, "gen_ai.usage.output_tokens", usage_attrs.output_tokens)
            _set_if_present(span, "gen_ai.response.id", usage_attrs.response_id)
            if external_cli_attrs is not None:
                span.set_attribute("external_cli.exit_code", external_cli_attrs.exit_code)
                span.set_attribute("external_cli.provider.kind", external_cli_attrs.kind)

            # anthropic.* per C-AS-14 §14.2 — emitted ONLY when
            # provider == "anthropic" per AS-AL-3 cross-axis scope.
            if cache_attrs is not None:
                _set_if_present(
                    span,
                    "anthropic.cache_creation_input_tokens",
                    cache_attrs.cache_creation_input_tokens,
                )
                _set_if_present(
                    span,
                    "anthropic.cache_read_input_tokens",
                    cache_attrs.cache_read_input_tokens,
                )
                _set_if_present(
                    span,
                    "anthropic.cache_breakpoint_id",
                    cache_attrs.cache_breakpoint_id,
                )
                _set_if_present(
                    span,
                    "anthropic.cache_ttl_seconds",
                    cache_attrs.cache_ttl_seconds,
                )
            # anthropic.* rows 5-10 per C-AS-14 §14.2 — request-side +
            # model-derived attrs. `tokenizer_version` always emits;
            # the optional 5 emit only when present per spec optional
            # discipline (`_set_if_present` short-circuits on None).
            if request_attrs is not None:
                _set_if_present(span, "anthropic.thinking_mode", request_attrs.thinking_mode)
                _set_if_present(
                    span,
                    "anthropic.thinking_budget_tokens",
                    request_attrs.thinking_budget_tokens,
                )
                _set_if_present(
                    span,
                    "anthropic.thinking_effort",
                    request_attrs.thinking_effort,
                )
                _set_if_present(span, "anthropic.batch_id", request_attrs.batch_id)
                span.set_attribute("anthropic.tokenizer_version", request_attrs.tokenizer_version)
                _set_if_present(span, "anthropic.inference_geo", request_attrs.inference_geo)

            # --- Step 4.5: cost-attribution (U-OD-38) -------------------
            # Per §C-OD-26.1 + §C-OD-26.2 row "llm_dispatch": every LLM
            # dispatch invokes the 5-substep cost-attribution chain post-
            # provider-call. Persists the cost-record + audit-ledger entry
            # + emits cost.attributed_decimal OTel attribute via U-OD-49
            # string-form preserving Decimal precision at the OTel boundary.
            # Wrapped in best-effort try/except: cost-attribution failure
            # MUST NOT fail the dispatch (cost is observability not contract).
            _attribute_cost_best_effort(
                span=span,
                cost_chain=self.cost_chain,
                audit_writer=self.audit_writer,
                rate_table=self.rate_table,
                cost_record_sink=self.cost_record_sink,
                provider_name=provider_name,
                model=model,
                parent_idempotency_key=step_context.parent_idempotency_key,
                workflow_id=step_context.workflow_id,
                parent_action_id=step_context.parent_action_id,
                input_tokens=usage_attrs.input_tokens,
                output_tokens=usage_attrs.output_tokens,
                cache_creation=(
                    cache_attrs.cache_creation_input_tokens if cache_attrs is not None else None
                ),
                cache_read=(
                    cache_attrs.cache_read_input_tokens if cache_attrs is not None else None
                ),
                tenant_id=step_context.tenant_id,
            )

            # --- Step 5: return step output mapping ---------------------
            return response


# ---------------------------------------------------------------------------
# Per-provider dispatch helpers.
# ---------------------------------------------------------------------------


#: Per-provider §4.2 operation enum value used for both the span name
#: operation-token (per OD spec v1.12 §C-OD-04 §4.1) and the
#: `gen_ai.operation.name` Required (Stable) attribute (§4.3). All 3
#: providers dispatch chat-style completions:
#:   - `anthropic` → `client.messages.create` (Anthropic Messages API)
#:   - `openai`    → `client.chat.completions.create`
#:   - `ollama`    → `client.chat`
#: All 3 map to `GenAiOperation.CHAT` per OTel GenAI semconv 1.41.0 §4.2.
#: Finding (g) RESOLVED at this binding (was: API method names not in §4.2
#: enum — `messages.create` / `chat.completions` / `chat`).
_PROVIDER_OPERATIONS: dict[str, GenAiOperation] = {
    "anthropic": GenAiOperation.CHAT,
    "openai": GenAiOperation.CHAT,
    "ollama": GenAiOperation.CHAT,
}


#: Per-hosted-provider `server.address` per OD spec v1.20 §C-OD-04 §4.3
#: Recommended (Development) tier. Hosted providers (anthropic / openai) have
#: stable canonical endpoints. Ollama is operator-configurable and is resolved
#: from `RuntimeLLMDispatcher.ollama_host` per the OTel Conditionally Required
#: "If `server.address` is set" rule (no emission when the value is unknown).
_PROVIDER_SERVER_ADDRESS: dict[str, str] = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
}

#: Per-hosted-provider `server.port` per OD spec v1.20 §C-OD-04 §4.3
#: Conditionally Required tier. Both hosted providers are HTTPS-only.
_PROVIDER_SERVER_PORT: dict[str, int] = {
    "anthropic": 443,
    "openai": 443,
}


def _parse_ollama_host(host: str | None) -> tuple[str | None, int | None]:
    """Parse `RuntimeConfig.ollama_host` into `(address, port)`.

    Accepts a full URL (e.g., ``http://localhost:11434``) per spec §5 line
    354 `AsyncClient(host=...)` convention. Returns ``(None, None)`` when
    the input is ``None``, satisfying the OTel Conditionally Required "If
    `server.address` is set" gating — when the harness does not know the
    user's configured ollama endpoint, the harness emits neither attribute
    (per advisor 2026-05-27 correction to fork doc §3 Path A static-map
    framing; static `localhost` would be a factual lie in telemetry when
    the operator binds a remote daemon).

    When the input is set but port is omitted, defaults to the ollama SDK
    default port 11434 (matching `AsyncOllamaClient` fallback at
    `lifecycle/providers.py:504-505`).
    """
    if host is None:
        return (None, None)
    # Strip scheme.
    remainder = host
    for scheme in ("http://", "https://"):
        if remainder.startswith(scheme):
            remainder = remainder[len(scheme) :]
            break
    # Strip path.
    if "/" in remainder:
        remainder = remainder.split("/", 1)[0]
    # Split host:port.
    if ":" in remainder:
        address, _, port_str = remainder.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            return (address or None, 11434)
        return (address or None, port)
    return (remainder or None, 11434)


@dataclass(frozen=True, slots=True)
class _UsageAttrs:
    """Provider-neutral usage-attribute carrier."""

    input_tokens: int | None
    output_tokens: int | None
    response_id: str | None


@dataclass(frozen=True, slots=True)
class _AnthropicCacheAttrs:
    """Anthropic-specific cache-attribute carrier (per C-AS-14 §14.2)."""

    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    cache_breakpoint_id: str | None
    cache_ttl_seconds: int | None


@dataclass(frozen=True, slots=True)
class _AnthropicRequestAttrs:
    """Anthropic request-side + model-derived attribute carrier per C-AS-14
    §14.2 rows 5-10 (the 6 non-cache attrs on the `llm.inference` span).

    Sources per Anthropic Python SDK `MessageCreateParams` shape:
    - `thinking_mode` / `thinking_budget_tokens` — `payload.params["thinking"]`
    - `thinking_effort` — beta `payload.params["output_config"]["effort"]`
    - `inference_geo` — `payload.params["inference_geo"]`
    - `batch_id` — operator-supplied marker at `payload.params["batch_id"]`
      (not in synchronous messages.create; out-of-band Batch API submission marker)
    - `tokenizer_version` — model-derived: `"v2"` for Opus 4.7+; else `"v1"` per
      spec §14.2 row "v1 (default); v2 (Opus 4.7)". Always emitted.
    """

    thinking_mode: str | None
    thinking_budget_tokens: int | None
    thinking_effort: str | None
    batch_id: str | None
    tokenizer_version: str  # always emitted; bounded enum {"v1", "v2"}
    inference_geo: str | None


@dataclass(frozen=True, slots=True)
class _ExternalCLIAttrs:
    exit_code: int
    kind: str


def _derive_tokenizer_version(model: str) -> str:
    """Per spec §14.2 row 9 — strict reading: `v2` for Opus 4.7+; else `v1`.

    Future model families MAY warrant additional `v*` values; that surface
    extension routes through design-phase back-flow per X-AL-3.
    """
    return "v2" if model.startswith("claude-opus-4-7") else "v1"


def _extract_anthropic_request_attrs(
    payload: ProviderAgnosticPayload, model: str
) -> _AnthropicRequestAttrs:
    """Extract the 6 non-cache anthropic.* attrs from request payload + model.

    Per C-AS-14 §14.2 rows 5-10. Best-effort: payloads that don't follow the
    expected shape return `None` per-field rather than raising. Total over
    `_AnthropicRequestAttrs` field domain.
    """
    params = payload.params
    thinking_cfg = params.get("thinking")
    thinking_mode: str | None = None
    thinking_budget: int | None = None
    if isinstance(thinking_cfg, Mapping):
        cfg = cast(Mapping[str, Any], thinking_cfg)
        type_raw = cfg.get("type")
        budget_raw = cfg.get("budget_tokens")
        if isinstance(type_raw, str):
            thinking_mode = type_raw
        if isinstance(budget_raw, int) and not isinstance(budget_raw, bool):
            thinking_budget = budget_raw

    output_cfg = params.get("output_config")
    thinking_effort: str | None = None
    if isinstance(output_cfg, Mapping):
        cfg = cast(Mapping[str, Any], output_cfg)
        effort_raw = cfg.get("effort")
        if isinstance(effort_raw, str):
            thinking_effort = effort_raw

    batch_id_raw = params.get("batch_id")
    batch_id = batch_id_raw if isinstance(batch_id_raw, str) else None

    inference_geo_raw = params.get("inference_geo")
    inference_geo = inference_geo_raw if isinstance(inference_geo_raw, str) else None

    return _AnthropicRequestAttrs(
        thinking_mode=thinking_mode,
        thinking_budget_tokens=thinking_budget,
        thinking_effort=thinking_effort,
        batch_id=batch_id,
        tokenizer_version=_derive_tokenizer_version(model),
        inference_geo=inference_geo,
    )


def _payload_to_anthropic_kwargs(
    payload: ProviderAgnosticPayload,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate `ProviderAgnosticPayload` → ``messages.create`` kwargs.

    Anthropic's ``messages.create`` requires ``max_tokens``; the
    provider-neutral payload carries it in ``params``. Tools are passed
    through when present; ``params`` keys merge into the call kwargs.

    R-PM-1 PR #1 — when ``system`` (the active prompt content) is supplied,
    inject it as Anthropic's **top-level ``system=`` kwarg** (the base-system-
    prompt route per the `claude-api` reference; a ``role:"system"`` message
    entry is NOT honored as a base prompt by Anthropic). ``ProviderAgnosticPayload``
    stays frozen — the system content rides the dispatcher, never the payload —
    so cost-attribution + ``_extract_anthropic_request_attrs`` are untouched
    (ADR-F1-faithful: per-provider feature use at the call site, no provider-
    specific field lifted into the neutral record). Fail-loud if ``params``
    already carries a competing ``system`` (the opaque escape hatch).
    """
    kwargs: dict[str, Any] = {"messages": list(payload.messages)}
    if payload.tools is not None:
        kwargs["tools"] = list(payload.tools)
    kwargs.update(payload.params)
    if system:
        if "system" in kwargs:
            raise PromptInjectionConflictError("anthropic", 'params["system"]')
        kwargs["system"] = system
    # B-INTERSTEP — Anthropic carries `system` as a top-level kwarg (not a message),
    # so the upstream context goes at messages index 0 (post-`params`-merge).
    _inject_upstream_context_message(kwargs, upstream)
    return kwargs


def _inject_leading_system_message(
    kwargs: dict[str, Any], system: str | None, provider: str
) -> None:
    """Prepend a ``{"role":"system"}`` entry to the OpenAI/Ollama messages
    (the base-system-prompt route for these providers) when ``system`` is
    supplied — R-PM-1 PR #1. Operates on the **post-`params`-merge** ``kwargs``
    so the injection cannot be clobbered by, and any competing system source
    cannot be hidden in, a ``params["messages"]`` escape-hatch override (Codex
    review). Fail-loud (`detect-then-refuse`) if the effective messages already
    lead with a ``role:"system"`` entry (the idiomatic per-step system prompt).
    """
    if not system:
        return
    messages: list[Mapping[str, Any]] = list(kwargs.get("messages", ()))
    if messages and messages[0].get("role") == "system":
        raise PromptInjectionConflictError(provider, 'messages[0] role:"system"')
    kwargs["messages"] = [{"role": "system", "content": system}, *messages]


def _payload_to_openai_kwargs(
    payload: ProviderAgnosticPayload,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate `ProviderAgnosticPayload` → ``chat.completions.create`` kwargs.

    R-PM-1 PR #1 — ``system`` (active prompt content) injects as a leading
    ``{"role":"system","content":...}`` message (the OpenAI base-prompt route),
    **after** the ``params`` merge so a ``params["messages"]`` override cannot
    silently drop it. B-INTERSTEP — the upstream-context ``user`` message injects
    AFTER the system injection (so it lands after any leading system message and
    does not mask its conflict check).
    """
    kwargs: dict[str, Any] = {"messages": list(payload.messages)}
    if payload.tools is not None:
        kwargs["tools"] = list(payload.tools)
    kwargs.update(payload.params)
    _inject_leading_system_message(kwargs, system, "openai")
    _inject_upstream_context_message(kwargs, upstream)
    return kwargs


def _payload_to_ollama_kwargs(
    payload: ProviderAgnosticPayload,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate `ProviderAgnosticPayload` → ``ollama.chat`` kwargs.

    R-PM-1 PR #1 — ``system`` (active prompt content) injects as a leading
    ``{"role":"system","content":...}`` message (the Ollama base-prompt route),
    **after** the ``params`` merge so a ``params["messages"]`` override cannot
    silently drop it. B-INTERSTEP — the upstream-context ``user`` message injects
    AFTER the system injection (so it lands after any leading system message and
    does not mask its conflict check).
    """
    kwargs: dict[str, Any] = {"messages": list(payload.messages)}
    if payload.tools is not None:
        kwargs["tools"] = list(payload.tools)
    kwargs.update(payload.params)
    _inject_leading_system_message(kwargs, system, "ollama")
    _inject_upstream_context_message(kwargs, upstream)
    return kwargs


def _payload_to_external_cli_prompt(
    payload: ProviderAgnosticPayload,
    *,
    provider: str,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> str:
    """Translate provider-neutral messages to one text prompt for local CLIs.

    R-CLI-1 v1 is text-only. Non-empty tools are rejected before the subprocess
    boundary so a local CLI cannot execute or negotiate tools accidentally.
    """
    if payload.tools:
        raise LLMDispatchPayloadShapeError(
            f"external CLI provider {provider!r} is text-only; tools are not supported"
        )

    messages = list(payload.messages)
    if system and messages and messages[0].get("role") == "system":
        raise PromptInjectionConflictError(provider, 'messages[0] role:"system"')

    parts: list[str] = []
    if system:
        parts.append(f"system:\n{system}")
    if upstream is not None:
        rendered_upstream = json.dumps(dict(upstream), sort_keys=True, default=str)
        parts.append(f"user:\n{_UPSTREAM_CONTEXT_PREFIX}{rendered_upstream}")
    for message in messages:
        role = message.get("role", "user")
        if not isinstance(role, str) or not role:
            role = "user"
        content = message.get("content", "")
        if isinstance(content, str):
            rendered_content = content
        else:
            rendered_content = json.dumps(content, sort_keys=True, default=str)
        parts.append(f"{role}:\n{rendered_content}")
    return "\n\n".join(parts)


_ANTHROPIC_HITL_MAX_TOOL_TURNS = 16


def _anthropic_attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value).get(name)
    return getattr(value, name, None)


def _anthropic_content_blocks(response: Any) -> tuple[Any, ...]:
    content = _anthropic_attr(response, "content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return tuple(cast(Sequence[Any], content))
    return ()


def _anthropic_stop_reason(response: Any) -> str | None:
    value = _anthropic_attr(response, "stop_reason")
    return value if isinstance(value, str) else None


def _anthropic_block_mapping(block: Any) -> Mapping[str, Any]:
    if isinstance(block, Mapping):
        return dict(cast(Mapping[str, Any], block))
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dict(cast(Mapping[str, Any], dumped))
    projected: dict[str, Any] = {}
    for name in ("type", "id", "name", "input", "text"):
        value = getattr(block, name, None)
        if value is not None:
            projected[name] = value
    return projected


def _anthropic_tool_use_blocks(response: Any) -> tuple[Any, ...]:
    if _anthropic_stop_reason(response) != "tool_use":
        return ()
    return tuple(
        block
        for block in _anthropic_content_blocks(response)
        if _anthropic_attr(block, "type") == "tool_use"
    )


def _anthropic_tool_server_for_name(
    payload: ProviderAgnosticPayload,
    tool_name: str,
) -> str:
    for tool in payload.tools or ():
        tool_mapping = _anthropic_block_mapping(tool)
        if tool_mapping.get("name") != tool_name:
            continue
        for key in ("server", "mcp_server", "server_name", "mcp_server_name"):
            value = tool_mapping.get(key)
            if isinstance(value, str) and value:
                return value
    return "anthropic"


def _model_tool_call_from_anthropic_block(
    block: Any,
    *,
    payload: ProviderAgnosticPayload,
    provider: str,
    model: str,
) -> ModelToolCall:
    tool_call_id = _anthropic_attr(block, "id")
    tool_name = _anthropic_attr(block, "name")
    arguments = _anthropic_attr(block, "input")
    if not isinstance(tool_call_id, str) or not isinstance(tool_name, str):
        raise LLMDispatchPayloadShapeError(
            "Anthropic tool_use block missing string id/name for R-CXA-2 HITL loop"
        )
    if not isinstance(arguments, Mapping):
        arguments = {}
    return ModelToolCall(
        tool_call_id=tool_call_id,
        tool=tool_name,
        server=_anthropic_tool_server_for_name(payload, tool_name),
        arguments=cast(Mapping[str, Any], arguments),
        provider=provider,
        model=model,
    )


def _hitl_loop_context_from_step(
    step_context: StepExecutionContext,
    *,
    step_id: str,
    persona_tier: PersonaTier,
) -> HITLToolLoopContext:
    actor_id = getattr(step_context.parent_actor, "actor_id", "harness-runtime")
    return HITLToolLoopContext(
        workflow_id=step_context.workflow_id,
        step_id=step_id,
        persona_tier=persona_tier,
        cell_synchrony_class=SynchronyClass.SYNC_BLOCKING,
        cross_trust_boundary_state=CrossTrustBoundaryState.NONE,
        actor=ActorIdentity(str(actor_id)),
    )


def _anthropic_tool_result_content(result: Any) -> str:
    if result is None:
        return "HITL tool loop did not return a result for this tool call."
    dispatch_result = getattr(result, "dispatch_result", None)
    if not isinstance(dispatch_result, Mapping):
        return "HITL rejected or skipped this tool call."
    dispatch_mapping = cast(Mapping[str, Any], dispatch_result)
    response_text = dispatch_mapping.get("response_text")
    if isinstance(response_text, str):
        return response_text
    return json.dumps(dict(dispatch_mapping), sort_keys=True, default=str)


def _anthropic_tool_result_block(tool_use_block: Any, result: Any) -> dict[str, Any]:
    tool_call_id = _anthropic_attr(tool_use_block, "id")
    if not isinstance(tool_call_id, str):
        raise LLMDispatchPayloadShapeError(
            "Anthropic tool_use block missing string id for tool_result continuation"
        )
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": _anthropic_tool_result_content(result),
    }
    if result is None or getattr(result, "dispatch_result", None) is None:
        block["is_error"] = True
    return block


def _anthropic_response_bundle(
    response: Any,
    payload: ProviderAgnosticPayload,
    model: str,
) -> tuple[Mapping[str, Any], _UsageAttrs, _AnthropicCacheAttrs, _AnthropicRequestAttrs]:
    usage = getattr(response, "usage", None)
    usage_attrs = _UsageAttrs(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        response_id=getattr(response, "id", None),
    )
    cache_breakpoint_id, cache_ttl_seconds = _extract_anthropic_cache_request_attrs(payload)
    cache_attrs = _AnthropicCacheAttrs(
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
        cache_breakpoint_id=cache_breakpoint_id,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    request_attrs = _extract_anthropic_request_attrs(payload, model)
    return (_response_to_mapping(response), usage_attrs, cache_attrs, request_attrs)


async def _dispatch_anthropic_with_hitl_tool_loop(
    adapter: Any,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    hitl_tool_loop: RuntimeHITLToolLoop,
    step_context: StepExecutionContext,
    step_id: str,
    persona_tier: PersonaTier,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], _UsageAttrs, _AnthropicCacheAttrs, _AnthropicRequestAttrs]:
    """Anthropic provider branch with generic R-CXA-2 HITL tool continuation.

    Non-memory Anthropic ``tool_use`` blocks are adapted into provider-neutral
    ``ModelToolCall`` values, processed by the bound ``RuntimeHITLToolLoop``,
    and then returned to Anthropic as ``tool_result`` blocks for continuation.

    R-PM-1 PR #1 — ``system`` (active prompt content) injects as the Anthropic
    ``system=`` top-level kwarg; it persists across continuation turns (the
    mutable ``messages`` list is rebuilt per turn, but ``system`` is a separate
    kwarg).
    """
    kwargs = _payload_to_anthropic_kwargs(payload, system, upstream)
    # Seed the per-turn mutable loop list from the TRANSLATED `kwargs["messages"]`
    # (NOT `payload.messages`) so it carries the `params["messages"]` merge result
    # AND the B-INTERSTEP upstream-context injection — otherwise the tool-loop's
    # model calls silently lose the inter-step context while the non-HITL Anthropic
    # path keeps it (Codex review). The loop then appends tool_result turns to it.
    messages = list(kwargs["messages"])
    kwargs["messages"] = messages
    context = _hitl_loop_context_from_step(
        step_context,
        step_id=step_id,
        persona_tier=persona_tier,
    )

    for _turn_index in range(_ANTHROPIC_HITL_MAX_TOOL_TURNS):
        response = await adapter.client.messages.create(model=model, **kwargs)
        tool_use_blocks = _anthropic_tool_use_blocks(response)
        if not tool_use_blocks:
            return _anthropic_response_bundle(response, payload, model)

        calls = tuple(
            _model_tool_call_from_anthropic_block(
                block,
                payload=payload,
                provider="anthropic",
                model=model,
            )
            for block in tool_use_blocks
        )
        results = await hitl_tool_loop.run_tool_calls(calls, context)
        result_by_id = {result.tool_call_id: result for result in results}

        messages.append(
            {
                "role": "assistant",
                "content": [
                    dict(_anthropic_block_mapping(block))
                    for block in _anthropic_content_blocks(response)
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    _anthropic_tool_result_block(
                        block,
                        result_by_id.get(cast(str, _anthropic_attr(block, "id"))),
                    )
                    for block in tool_use_blocks
                ],
            }
        )

    raise RuntimeError(
        "Anthropic R-CXA-2 HITL tool loop exceeded "
        f"{_ANTHROPIC_HITL_MAX_TOOL_TURNS} continuation turns"
    )


async def _dispatch_anthropic_with_memory(
    adapter: Any,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    registry: Any,
    deployment_surface: Any,
    tracer: Any,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], _UsageAttrs, _AnthropicCacheAttrs, _AnthropicRequestAttrs]:
    """Anthropic provider branch with Memory tool inner loop (U-RT-81).

    Per spec v1.17 §14.5.1 mechanism β + AS spec v1.5 §14.7 memory.*
    namespace emission. Resolves the storage backend via the registry,
    derives `memory.context_editing_active`, then defers to
    `execute_with_memory_callbacks` for the inner tool-use loop.

    `MemoryPathViolationError` / `MemoryCallbackIOError` propagate
    VERBATIM through this helper to the C-RT-15 dispatcher boundary →
    driver `try/except` at `workflow_driver.py:618-635` per spec §14.5.1
    step 5.
    """
    backend = registry.resolve_backend(deployment_surface)
    configured_backend = registry.configured_backend
    context_editing_active = derive_context_editing_active(payload.params)
    kwargs = _payload_to_anthropic_kwargs(payload, system, upstream)

    response = await execute_with_memory_callbacks(
        adapter=adapter,
        model=model,
        messages_create_kwargs=kwargs,
        backend=backend,
        backend_enum=configured_backend,
        tracer=tracer,
        context_editing_active=context_editing_active,
    )

    return _anthropic_response_bundle(response, payload, model)


async def _dispatch_anthropic(
    adapter: Any,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], _UsageAttrs, _AnthropicCacheAttrs, _AnthropicRequestAttrs]:
    """Anthropic provider branch — ``client.messages.create(...)``."""
    kwargs = _payload_to_anthropic_kwargs(payload, system, upstream)
    response = await adapter.client.messages.create(model=model, **kwargs)

    return _anthropic_response_bundle(response, payload, model)


async def _dispatch_openai(
    adapter: Any,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], _UsageAttrs]:
    """OpenAI provider branch — ``client.chat.completions.create(...)``."""
    kwargs = _payload_to_openai_kwargs(payload, system, upstream)
    response = await adapter.client.chat.completions.create(model=model, **kwargs)
    return _openai_response_bundle(response)


async def _dispatch_openai_with_standard_memory_tools(
    adapter: Any,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    memory_context: RuntimeMemoryContext,
    standard_memory_tool_executor: Any,
    step_context: StepExecutionContext,
    step_id: str,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
    max_iterations: int = 16,
) -> tuple[Mapping[str, Any], _UsageAttrs]:
    """OpenAI provider branch with standard memory tool continuation."""
    kwargs = _payload_to_openai_kwargs(payload, system, upstream)
    messages = list(kwargs["messages"])
    kwargs["messages"] = messages

    for _ in range(max_iterations):
        response = await adapter.client.chat.completions.create(model=model, **kwargs)
        response_mapping = _response_to_mapping(response)
        tool_calls = _openai_tool_calls(response_mapping)
        if not tool_calls:
            return _openai_response_bundle(response)

        assistant_message = _openai_assistant_message(response_mapping)
        messages.append(assistant_message)
        messages.extend(
            _openai_memory_tool_result_message(
                call,
                standard_memory_tool_executor=standard_memory_tool_executor,
                memory_context=memory_context,
                step_context=step_context,
                step_id=step_id,
                provider="openai",
                model=model,
            )
            for call in tool_calls
        )

    raise RuntimeError(
        f"OpenAI standard memory tool loop exceeded {max_iterations} continuation turns"
    )


def _openai_response_bundle(response: Any) -> tuple[Mapping[str, Any], _UsageAttrs]:
    usage = getattr(response, "usage", None)
    usage_attrs = _UsageAttrs(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        response_id=getattr(response, "id", None),
    )
    return (_response_to_mapping(response), usage_attrs)


def _openai_assistant_message(response: Mapping[str, Any]) -> dict[str, Any]:
    message = _openai_first_message(response)
    return dict(message)


def _openai_tool_calls(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    message = _openai_first_message(response)
    tool_calls = message.get("tool_calls")
    if tool_calls is None:
        return ()
    if isinstance(tool_calls, str | bytes) or not isinstance(tool_calls, Sequence):
        raise LLMDispatchPayloadShapeError("OpenAI tool_calls must be a sequence")
    calls: list[Mapping[str, Any]] = []
    for item in cast(Sequence[object], tool_calls):
        if not isinstance(item, Mapping):
            raise LLMDispatchPayloadShapeError("OpenAI tool_call entries must be mappings")
        calls.append(cast(Mapping[str, Any], item))
    return tuple(calls)


def _openai_first_message(response: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = response.get("choices")
    if isinstance(choices, str | bytes) or not isinstance(choices, Sequence) or not choices:
        raise LLMDispatchPayloadShapeError("OpenAI response choices missing")
    first = cast(Sequence[object], choices)[0]
    if not isinstance(first, Mapping):
        raise LLMDispatchPayloadShapeError("OpenAI response choice must be a mapping")
    first_mapping = cast(Mapping[str, object], first)
    message = first_mapping.get("message")
    if not isinstance(message, Mapping):
        raise LLMDispatchPayloadShapeError("OpenAI response choice.message missing")
    return cast(Mapping[str, Any], message)


def _openai_memory_tool_result_message(
    call: Mapping[str, Any],
    *,
    standard_memory_tool_executor: Any,
    memory_context: RuntimeMemoryContext,
    step_context: StepExecutionContext,
    step_id: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    tool_call_id = call.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise LLMDispatchPayloadShapeError("OpenAI memory tool_call missing string id")
    tool_name, arguments = _openai_memory_tool_name_and_arguments(call)
    request = MemoryToolExecutionRequest(
        tool_name=tool_name,
        arguments=arguments,
        context=_standard_memory_tool_context(
            arguments,
            memory_context=memory_context,
            step_context=step_context,
            step_id=step_id,
            provider=provider,
            model=model,
        ),
    )
    result = standard_memory_tool_executor.execute(request)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name.value,
        "content": json.dumps(result, sort_keys=True, separators=(",", ":"), default=str),
    }


def _openai_memory_tool_name_and_arguments(
    call: Mapping[str, Any],
) -> tuple[MemoryToolName, dict[str, object]]:
    function = call.get("function")
    if not isinstance(function, Mapping):
        raise LLMDispatchPayloadShapeError("OpenAI memory tool_call.function missing")
    function_mapping = cast(Mapping[str, object], function)
    name = function_mapping.get("name")
    if not isinstance(name, str) or not name:
        raise LLMDispatchPayloadShapeError("OpenAI memory tool_call.function.name missing")
    try:
        tool_name = MemoryToolName(name)
    except ValueError as exc:
        raise LLMDispatchPayloadShapeError(
            f"OpenAI standard memory loop received non-memory tool {name!r}"
        ) from exc
    raw_arguments = function_mapping.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise LLMDispatchPayloadShapeError(
                f"OpenAI memory tool {name!r} arguments are not valid JSON"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise LLMDispatchPayloadShapeError(
                f"OpenAI memory tool {name!r} arguments must decode to an object"
            )
        return (tool_name, dict(cast(Mapping[str, object], parsed)))
    if isinstance(raw_arguments, Mapping):
        return (tool_name, dict(cast(Mapping[str, object], raw_arguments)))
    raise LLMDispatchPayloadShapeError(f"OpenAI memory tool {name!r} arguments missing")


def _standard_memory_tool_context(
    arguments: Mapping[str, object],
    *,
    memory_context: RuntimeMemoryContext,
    step_context: StepExecutionContext,
    step_id: str,
    provider: str,
    model: str,
) -> MemoryToolExecutionContext:
    if memory_context.record_scope is None:
        raise MemoryToolExecutionInputError(
            "standard memory tool dispatch requires RuntimeMemoryContext.record_scope"
        )
    scope_ref = arguments.get("scope_ref")
    if not isinstance(scope_ref, str) or not scope_ref:
        raise MemoryToolExecutionInputError("standard memory tool call requires scope_ref")
    token_budget = memory_context.packet.token_budget if memory_context.packet is not None else 0
    return MemoryToolExecutionContext(
        run_id=memory_context.run_id,
        workflow_id=step_context.workflow_id,
        workload_class=None,
        step_id=step_id,
        provider=provider,
        model=model,
        cli_profile=memory_context.selection.cli_profile_ref,
        scope=memory_context.record_scope,
        scope_ref=scope_ref,
        policy_ref=memory_context.policy_ref,
        token_budget=token_budget,
        timestamp=datetime.now(UTC),
        actor=step_context.parent_actor,
    )


async def _dispatch_ollama(
    adapter: Any,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], _UsageAttrs]:
    """Ollama provider branch — ``client.chat(...)``.

    Ollama's ``ChatResponse`` exposes ``prompt_eval_count`` / ``eval_count``
    instead of a nested ``usage`` object; no ``response_id``.
    """
    kwargs = _payload_to_ollama_kwargs(payload, system, upstream)
    response = await adapter.client.chat(model=model, **kwargs)

    usage_attrs = _UsageAttrs(
        input_tokens=getattr(response, "prompt_eval_count", None),
        output_tokens=getattr(response, "eval_count", None),
        response_id=None,
    )
    return (_response_to_mapping(response), usage_attrs)


async def _dispatch_external_cli(
    adapter: _ExternalCLIProviderLike,
    provider: str,
    model: str,
    payload: ProviderAgnosticPayload,
    *,
    system: str | None = None,
    upstream: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], _UsageAttrs, _ExternalCLIAttrs]:
    """External CLI provider branch — one text prompt, one text response."""
    prompt = _payload_to_external_cli_prompt(
        payload,
        provider=provider,
        system=system,
        upstream=upstream,
    )
    result = await adapter.dispatch_text(model=model, prompt=prompt)
    text = getattr(result, "text", None)
    if not isinstance(text, str):
        raise LLMDispatchPayloadShapeError("external CLI provider result missing string text field")
    exit_code = getattr(result, "exit_code", 0)
    if not isinstance(exit_code, int):
        exit_code = 0
    kind = getattr(adapter, "kind", "external-cli")
    if not isinstance(kind, str):
        kind = "external-cli"
    response: Mapping[str, Any] = {
        "provider": provider,
        "model": model,
        "content": [{"type": "text", "text": text}],
    }
    return (
        response,
        _UsageAttrs(input_tokens=None, output_tokens=None, response_id=None),
        _ExternalCLIAttrs(exit_code=exit_code, kind=kind),
    )


def _response_to_mapping(response: Any) -> Mapping[str, Any]:
    """Coerce a provider response to ``Mapping[str, Any]``.

    All three provider SDKs return pydantic v2 models from their
    chat/messages methods, so ``model_dump()`` is uniformly available.
    Falls back to passing through Mapping instances for hand-rolled
    test stubs.
    """
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        result = cast(Any, dump())
        if isinstance(result, Mapping):
            return cast(Mapping[str, Any], result)
    if isinstance(response, Mapping):
        return cast(Mapping[str, Any], response)
    raise LLMDispatchPayloadShapeError(
        f"provider response not coercible to Mapping[str, Any]: {type(response)!r}"
    )


def _attribute_cost_best_effort(
    *,
    span: Any,
    cost_chain: Any,
    audit_writer: Any,
    rate_table: Any,
    cost_record_sink: SupportsCostRecordAppend | None = None,
    provider_name: str,
    model: str,
    parent_idempotency_key: str,
    workflow_id: str,
    parent_action_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_creation: int | None,
    cache_read: int | None,
    tenant_id: str | None,
) -> None:
    """Best-effort cost-attribution invocation per §C-OD-26.1 (U-OD-38).

    Calls `attribute_llm_dispatch_cost`. On success, emits the
    cost.attributed_decimal OTel attribute on the current dispatch span via
    U-OD-49 string-form. On failure (rate-table missing OR upstream cost
    chain failure), swallows the exception — cost-attribution is observability,
    not contract, and MUST NOT fail the dispatch (AC #1: invoked on every
    dispatch, success + failure paths). Future hardening: per-provider
    "raise" config flag per §C-OD-28.2 fail-closed default; not at v1 MVP
    scope for the dispatch-side wrapper.
    """
    # Defer imports to module-load time at call-site to keep llm_dispatch.py's
    # cold import surface narrow (cost-attribution path pulls OD + CXA types
    # transitively).
    from decimal import Decimal

    from harness_od.cost_record_otel_serializer import (
        COST_ATTRIBUTED_DECIMAL_ATTR,
        serialize_decimal_for_otel,
    )

    from harness_runtime.lifecycle.cost_attribution_llm_dispatch import (
        attribute_llm_dispatch_cost,
    )
    from harness_runtime.lifecycle.cross_family_cost_tag import (
        cross_family_tag_for_provider,
    )

    if cost_chain is None or audit_writer is None or rate_table is None:
        # Cost-attribution substrate not bound — unit-test path (production
        # bootstrap stage 5 enforces all 3 substrates are present).
        return
    if input_tokens is None or output_tokens is None:
        # No usage attrs from provider — cost is undefined. Skip silently.
        return

    span_context = span.get_span_context()
    span_id_hex = format(span_context.span_id, "016x")

    try:
        attached = attribute_llm_dispatch_cost(
            rate_table=rate_table,
            cost_chain=cost_chain,
            audit_writer=audit_writer,
            provider_name=provider_name,
            model=model,
            span_id=span_id_hex,
            parent_idempotency_key=parent_idempotency_key,
            workflow_id=workflow_id,
            parent_action_id=parent_action_id,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cache_creation=int(cache_creation) if cache_creation is not None else 0,
            cache_read=int(cache_read) if cache_read is not None else 0,
            tenant_id=tenant_id,
            # R-FS-1 B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION — populate the
            # §15.3 cross-family family tag from the dispatched provider so the
            # PER_PROVIDER_DISCRIMINATOR rollup is non-vacuous in production.
            # An LLM dispatch always has a provider ⟹ always a family tag.
            provider_discriminator=cross_family_tag_for_provider(provider_name),
        )
    except Exception:
        # Cost-attribution is observability, not contract. Swallow.
        return

    # Emit cost.attributed_decimal OTel attribute via U-OD-49 string-form
    # preserving the float→Decimal serialization at the OTel boundary.
    span.set_attribute(
        COST_ATTRIBUTED_DECIMAL_ATTR,
        serialize_decimal_for_otel(Decimal(str(attached.total_cost))),
    )

    # R-FS-1 arc CA — record the per-dispatch SpanCostRecord into the run-scoped
    # accumulator so `_build_run_result` can roll up `RunResult.cost_attribution`
    # (runtime spec v1.53 §9 C-RT-09). `list.append` is atomic under the GIL.
    if cost_record_sink is not None:
        cost_record_sink.append(attached)


def materialize_llm_dispatcher_stage(
    providers: _ProvidersLike,
    tracer_provider: _TracerProviderLike,
    *,
    cost_chain: Any = None,
    audit_writer: Any = None,
    rate_table: Any = None,
    cost_record_sink: SupportsCostRecordAppend | None = None,
    inter_step_channel: Any = None,
    memory_tool_registry: Any = None,
    deployment_surface: Any = None,
    hitl_tool_loop: RuntimeHITLToolLoop | None = None,
    ollama_host: str | None = None,
    skill_activation_emitter: Any = None,
    skills: Any = None,
    routing_manifest: RoutingManifest | None = None,
    workload_class: WorkloadClass | None = None,
    persona_tier: PersonaTier | None = None,
    active_system_prompt: str | None = None,
    memory_context: RuntimeMemoryContext | None = None,
    standard_memory_tool_executor: Any = None,
    memory_runtime: Any = None,
    fallback_chain: Any = None,
    per_role_system_prompts: Mapping[AgentRole, str] | None = None,
    prompt_versions_by_sha: Mapping[str, str] | None = None,
    approved_prompt_version_shas: frozenset[str] = frozenset(),
    routing_activation: bool = False,
    embedding_classifier: LayerDecisionFn | None = None,
) -> RuntimeLLMDispatcher:
    """Stage 5 LOOP_INIT composer factory for the LLM dispatcher (U-RT-52).

    U-OD-38 extension: cost-attribution substrate (cost_chain + audit_writer +
    rate_table) is required at composer construction. The stage caller
    (bootstrap stage 5) sources from `ctx.cost_chain` (stage 4 OD) +
    `ctx.audit_writer` (stage 4 OD) + `ctx.rate_table` (stage 4 OD or
    bootstrap config).

    Raises
    ------
    LLMDispatchBindError
        If the providers map is empty (no providers registered at
        stage 3a — would indicate either Ollama-degraded + non-optional
        anthropic/openai failure path OR a bootstrap-orchestrator bug).
    """
    if len(providers) == 0:
        raise LLMDispatchBindError(
            "No providers registered at stage 3a — cannot bind LLM dispatcher at stage 5"
        )

    # B-L2-FALLBACK-COMPOSITION (runtime §14.6) — routing_activation NOW composes
    # with the C-RT-16 fallback chain: the routing decision is made ONCE at the
    # C-RT-16 wrapper (via `RuntimeLLMDispatcher.resolve_routed_binding`, which the
    # stage-5 factory hands the wrapper as a direct handle) and SEEDS the routed
    # model as the wrapper's PRIMARY fallback candidate; the inner `dispatch` stays
    # FAITHFUL. The prior B-L2-EMBEDDING-ACTIVATION detect-then-refuse guard
    # (routing_activation + non-empty fallback_chains → LLMDispatchBindError) is
    # RETIRED — the silent-fallback-defeat it prevented is now architecturally
    # closed (the U-RT-114 §14.5.3 "wrapper owns model-candidate selection",
    # route-once-then-fallback-the-chain pattern applied to layered routing).

    # B-L2-EMBEDDING-ACTIVATION (C-CP-02 §2.2): when routing_activation is on and no
    # classifier is injected, build the default L2 EMBEDDING classifier (the light
    # in-process fastembed realization over the default per-workload corpus) so the
    # §2.2-conditional DECLARATIVE decline has a real fall-through target. Local
    # imports keep the module-load light + touch fastembed ONLY when flag-on;
    # `make_fastembed_embedding` fail-louds with the install hint when the optional
    # `[embedding]` extra is absent (the dep stays optional — promoting it to a
    # required dependency is the deferred deployment step). Default-off ⇒ this block
    # is skipped, fastembed is never imported, byte-identical.
    if routing_activation and embedding_classifier is None:
        from harness_cp.embedding_routing import make_embedding_classifier

        from harness_runtime.lifecycle.embedding_resolution import (
            default_routing_corpus,
            make_fastembed_embedding,
        )

        embedding_classifier = make_embedding_classifier(
            embed=make_fastembed_embedding(),
            corpus=default_routing_corpus(),
        )

    return RuntimeLLMDispatcher(
        providers=providers,
        tracer_provider=tracer_provider,
        cost_chain=cost_chain,
        audit_writer=audit_writer,
        rate_table=rate_table,
        cost_record_sink=cost_record_sink,
        inter_step_channel=inter_step_channel,
        memory_tool_registry=memory_tool_registry,
        deployment_surface=deployment_surface,
        hitl_tool_loop=hitl_tool_loop,
        ollama_host=ollama_host,
        skill_activation_emitter=skill_activation_emitter,
        skills=skills,
        routing_manifest=routing_manifest,
        workload_class=workload_class,
        persona_tier=persona_tier,
        active_system_prompt=active_system_prompt,
        memory_context=memory_context,
        standard_memory_tool_executor=standard_memory_tool_executor,
        memory_runtime=memory_runtime,
        fallback_chain=fallback_chain,
        per_role_system_prompts=per_role_system_prompts or {},
        prompt_versions_by_sha=prompt_versions_by_sha or {},
        approved_prompt_version_shas=approved_prompt_version_shas,
        routing_activation=routing_activation,
        embedding_classifier=embedding_classifier,
    )


__all__ = [
    "LLMDispatchBindError",
    "LLMDispatchPayloadShapeError",
    "LLMDispatchProviderUnreachableError",
    "PromptInjectionConflictError",
    "RuntimeLLMDispatcher",
    "materialize_llm_dispatcher_stage",
]
