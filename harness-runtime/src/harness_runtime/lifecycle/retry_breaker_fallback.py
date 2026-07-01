"""Retry / breaker / fallback composer wrapping C-RT-15 — stage 5 LOOP_INIT
(U-RT-58, opens L9-bis).

Per `Spec_Harness_Runtime_v1.md` v1.4 §14.6 C-RT-16 (retry/breaker/fallback
composer). Wraps the bare C-RT-15 ``RuntimeLLMDispatcher`` with the per-step
candidate-iteration loop + per-candidate retry loop + breaker pre-check;
satisfies the same ``harness_cp.workflow_driver.StepDispatcher`` Protocol that
the inner dispatcher satisfies — from the CP driver's perspective the wrapper
IS the dispatcher.

Per-step invocation discipline (the body of
``RetryBreakerFallbackDispatcher.dispatch(binding, step)``):

  1. Look up ``RetryPolicy`` from the registry under the reserved
     ``"llm_dispatch"`` key — operator may not declare a tool by that name
     (enforced at manifest-validation time via ``ReservedToolNameError`` at
     `harness_cp.routing_manifest_residence.validate_routing_manifest`).
  2. Iterate the fallback chain candidates: first ``chain.primary``, then
     advance via ``advance_or_raise`` on per-candidate exhaustion. Cross-
     family transitions surface the C-CP-04 §4.3 attribution flags.
  3. Per candidate: breaker pre-check via ``breaker.should_attempt()``; on
     OPEN-and-cooldown-unexpired emit a ``retry.skipped`` event on the outer
     span and advance to the next candidate.
  4. Per-attempt loop (bounded by ``RetryPolicy.max_attempts``): start an
     inner ``harness.runtime.retry_attempt`` span carrying the
     C-CP-03 §3.5 ``retry.*`` 6-attribute namespace; dispatch via
     ``self.inner.dispatch(rebound_binding, step)``. On success the breaker
     records success and the result returns; on fail-fast (provider-
     unreachable / payload-shape) the breaker records failure and the
     candidate is abandoned; on transient SDK failure the staircase advances
     and either retries (sleeps full-jitter backoff) or escalates.
  5. On ``FallbackChainExhaustedError`` emit ``fallback.exhausted`` on the
     outer span and raise ``RetryBreakerFallbackExhaustedError`` (maps to the
     ``RT-FAIL-FALLBACK-EXHAUSTED`` fail class added at v1.4).

Three nesting levels per composer invocation (canonical OTel retry-wrapper
pattern): outer ``harness.runtime.retry_breaker_fallback`` → per-attempt
``harness.runtime.retry_attempt`` → inner ``gen_ai.{provider}.{operation}``
(from C-RT-15). Head sampler picks outer; tail sampler picks per-attempt;
inner GenAI is always-sampled per OTel GenAI semconv.

**Q2=c Registry key extension.** The reserved ``"llm_dispatch"`` policy key
is injected into the registry's internal ``retry_policies`` map by
``materialize_retry_breaker_stage`` post-validation, NOT carried in the
operator-supplied ``RoutingManifest.retry_policies``. The validator rejects
operator-supplied ``"llm_dispatch"`` keys. See ``RESERVED_LLM_DISPATCH_KEY``
+ ``DEFAULT_LLM_DISPATCH_RETRY_POLICY`` below.

**OTel context-manager note.** The OTel tracer ``start_as_current_span``
context manager is synchronous; spec §14.6 phrasing matches §14.5's pattern.
Inside this async function we use plain ``with`` per OTel API contract.

**Framework-pull discipline.** Hand-rolled per CLAUDE.md §3.2 — NO tenacity /
pybreaker / circuitbreaker. Breaker state machine lives at U-RT-24
``BreakerStateMachine``; backoff via ``compute_full_jitter_delay_seconds``;
candidate iteration via ``advance_or_raise``. The composer is thin
composition over these primitives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
)
from harness_cp.engine_namespace import REPLAY_DISPOSITION_MAPPING
from harness_cp.fall_through_procedure import FallThroughCause
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.provider_capabilities import (
    ProviderCapability,
    provider_supports,
    reflect_provider_capabilities,
)
from harness_cp.routing_manifest_residence import RetryPolicy, RoutingManifest
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass
from harness_cp.validator_fail_transient_staircase import StaircaseStage
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep
from harness_od.harness_breaker_schema import BreakerScope

from harness_runtime.lifecycle.cross_family_cost_tag import provider_family_for_provider
from harness_runtime.lifecycle.fallback_chain import (
    FallbackChainExhaustedError,
    advance_or_raise,
)
from harness_runtime.lifecycle.llm_dispatch import (
    ROUTED_PRIMARY_SPAN_TRACE,
    LLMDispatchPayloadShapeError,
    LLMDispatchProviderUnreachableError,
    RoutedPrimaryResolution,
)
from harness_runtime.lifecycle.retry_breaker import BreakerStateMachine
from harness_runtime.types import LLMDispatcher, RetryBreakerRegistry

__all__ = [
    "DEFAULT_LLM_DISPATCH_RETRY_POLICY",
    "RESERVED_LLM_DISPATCH_KEY",
    "RetryBreakerFallbackDispatcher",
    "RetryBreakerFallbackExhaustedError",
    "materialize_retry_breaker_fallback_dispatcher_stage",
]


RESERVED_LLM_DISPATCH_KEY = "llm_dispatch"
"""Reserved registry key for the LLM-dispatch retry policy (Q2=c clause).

The runtime composer reserves this key for LLM-dispatch retry policy lookup;
tools may not declare a tool named ``"llm_dispatch"`` (enforced at manifest-
validation time via a typed ``ReservedToolNameError`` at
`harness_cp.routing_manifest_residence.validate_routing_manifest`). The
default policy below is injected into the registry's internal mapping by
``materialize_retry_breaker_stage`` when no operator override is supplied
through a future-arc mechanism (per-runtime override is not exposed at MVP
per spec §14.6 "Deferred to implementation discretion")."""


DEFAULT_LLM_DISPATCH_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff="full_jitter",
    jitter="full_jitter",
)
"""Default ``RetryPolicy`` for the reserved ``"llm_dispatch"`` registry key.

Per spec §14.6: ``RetryPolicy(max_attempts=3, backoff="full_jitter",
base_delay_seconds=0.2, delay_cap_seconds=10.0)``. The registry carries
``base_delay_seconds`` / ``delay_cap_seconds`` as registry-level defaults
(not per-policy fields per `RetryPolicy` schema), so those are inherited
from the ``RuntimeRetryBreaker`` instance bound at stage 3b."""


class RetryBreakerFallbackExhaustedError(Exception):
    """Raised when the fallback chain exhausts after per-candidate retry
    exhaustion (every candidate either fails-fast or hits ``max_attempts``).

    Maps to ``RT-FAIL-FALLBACK-EXHAUSTED`` per `Spec_Harness_Runtime_v1.md`
    v1.4 §C-RT-14 failure-mode taxonomy (new row at v1.4). The driver
    ``try/except`` at ``workflow_driver.py:380-389`` catches and maps to
    ``step-failure: RT-FAIL-FALLBACK-EXHAUSTED: ...`` per C-CP-25 §25.3.3.4.

    Carries the last failed candidate for operator-facing attribution.
    """

    def __init__(self, failed: ProviderCandidate) -> None:
        self.failed = failed
        super().__init__(
            f"RT-FAIL-FALLBACK-EXHAUSTED: fallback chain exhausted after "
            f"candidate {failed.provider}:{failed.model} (chain traversal complete)"
        )


def _rebind_to_candidate(
    binding: StepEffectiveBinding, candidate: ProviderCandidate
) -> StepEffectiveBinding:
    """Construct a new ``StepEffectiveBinding`` with ``model_binding`` overridden
    to the current fallback candidate. Other fields carry forward unchanged."""
    return binding.model_copy(
        update={"model_binding": ModelBinding(provider=candidate.provider, model=candidate.model)}
    )


def _candidate_from_binding(mb: ModelBinding) -> ProviderCandidate:
    """Synthesize a ``ProviderCandidate`` from a resolved ``ModelBinding`` for the
    §14.5.3/§14.6 model-resolution precedence (per-step / per-role / per-workload /
    routed all resolve to a ``ModelBinding`` that becomes the augmented PRIMARY)."""
    return ProviderCandidate(
        provider=mb.provider, model=mb.model, family=_provider_family(mb.provider)
    )


#: Mirrors ``llm_dispatch._MVP_DEFAULT_AGENT_ROLE`` (same local-mirror pattern
#: as ``prompt_selection``) — the MVP default branch role. A ``None`` / default
#: ``agent_role`` reads the stage-bound chain unchanged (§14.5.3 non-breaking
#: default); only a NON-default role consults ``per_role_bindings``.
_MVP_DEFAULT_AGENT_ROLE = AgentRole("default")


#: Mirrors ``llm_dispatch._MVP_DEFAULT_WORKLOAD_CLASS`` — the MVP default workload
#: class. The per-workload model-resolution branch (§14.6) reads
#: ``self.workload_class or _MVP_DEFAULT_WORKLOAD_CLASS`` so the wrapper's
#: deterministic per-workload check mirrors the ``resolve_routed_binding`` decline
#: expression EXACTLY (``llm_dispatch.py`` uses the same fallback) — the
#: decline-predicate ⊆ precedence-authority invariant (§14.6.2).
_MVP_DEFAULT_WORKLOAD_CLASS = WorkloadClass.SOFTWARE_ENGINEERING


#: Provider key → ``ProviderFamily`` for synthesizing a per-role
#: ``ProviderCandidate`` (U-RT-114). Operator-authored chain candidates carry
#: ``family`` directly; a per-role ``ModelBinding`` (C-CP-01 §1.3) carries only
#: ``(provider, model)``, so the augmented-chain primary needs the family
#: derived. The canonical provider→family map lives at
#: ``cross_family_cost_tag.provider_family_for_provider`` (one source of truth —
#: shared with the B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION tag map); this
#: module re-binds it under the prior private name to keep the call site stable.
#: The family affects ONLY the C-CP-04 §4.3 cross-family attribution flags
#: (``cross_family_triggered`` / ``cache_state_lost``), never WHICH model is
#: dispatched.
_provider_family = provider_family_for_provider


def _required_capabilities(step: WorkflowStep) -> frozenset[ProviderCapability]:
    """Derive the ``ProviderCapability`` set a step's payload requires — the
    C-CP-03 §3.3 ``capability_required`` derivation.

    §3.3 ``on_capability_shortfall(provider, model, capability_required)`` takes
    ``capability_required`` as a given; this is the impl-discretion mapping
    (``Spec_Control_Plane_v1_2.md`` §2.4 / §3.27 deferral) from the C-CP-01 §1.1
    ``(messages, tools, params)`` payload to the C-CP-01 §1.2
    ``ProviderCapability`` discriminators. The runtime owns this mapping (not CP):
    CP keeps ``params`` an opaque mapping per C-CP-01 §1.4, while the runtime
    already reads the ``params["thinking"]`` convention at
    ``llm_dispatch.py`` ``_extract_request_attrs``.

    - non-empty ``tools`` -> ``TOOLS`` (the call uses tool-use).
    - ``params["thinking"]`` set -> ``THINKING`` (extended-thinking request;
      the capability-preservation axis — a thinking step must not route to a
      non-thinking model).

    Deterministic; a mis-shaped ``params`` value (not a mapping) yields no
    THINKING requirement, leaving payload-shape failures to the inner C-RT-15
    dispatcher's typed ``LLMDispatchPayloadShapeError`` rather than pre-empting
    them here.
    """
    payload = step.step_payload
    required: set[ProviderCapability] = set()
    if payload.get("tools"):
        required.add(ProviderCapability.TOOLS)
    params = payload.get("params")
    if isinstance(params, Mapping) and cast("Mapping[str, Any]", params).get("thinking"):
        required.add(ProviderCapability.THINKING)
    return frozenset(required)


def _classify_provider_exception(exc: BaseException) -> ValidatorRetryExitClass | None:
    """Map a provider-side exception to a ``ValidatorRetryExitClass`` for the
    staircase, or ``None`` for fail-fast / propagate.

    Per spec §14.6 D2: AUTH / payload-shape / shutdown are fail-fast;
    network / rate-limit / 5xx are transient (run the staircase).

    MVP discrimination (conservative — extends naturally to provider-specific
    exception classes at a follow-on arc):

    - ``LLMDispatchProviderUnreachableError`` → ``None`` (fail-fast, abandons
      this candidate; the outer loop advances).
    - ``LLMDispatchPayloadShapeError`` → ``None`` (fail-fast, abandons this
      candidate; the outer loop advances).
    - ``asyncio.CancelledError`` → re-raise (shutdown / cancellation must
      propagate; this is handled by the caller, not classified here).
    - All other ``Exception`` subclasses → ``TRANSIENT_RETRY`` (treat as
      network / rate-limit / 5xx until proven otherwise).
    """
    if isinstance(exc, (LLMDispatchProviderUnreachableError, LLMDispatchPayloadShapeError)):
        return None
    return ValidatorRetryExitClass.TRANSIENT_RETRY


def _is_hitl_terminal_control_flow(exc: BaseException) -> bool:
    """True for HITL gate TERMINAL control-flow exceptions that must PROPAGATE
    through the retry/fallback wrapper untouched — never retried, never
    candidate-advanced.

    Per spec §14.6 D2, HITL gate outcomes are operator/gate control-flow, NOT
    provider results. The wrapper's `inner` is the HITL gate composer (stage 5:
    `retry(HITL(bare_dispatcher))`), so these exceptions surface from
    `self.inner.dispatch`. Classifying them as provider failures is wrong on
    every branch: `TRANSIENT_RETRY` re-fires the gate; a fail-fast candidate-
    advance re-fires it with a fallback model; and for a durable-async RESUME a
    re-fire hits an already-emptied resume holder and RE-PAUSES instead of
    surfacing the operator's terminal decision (Codex out-of-family [P1]). They
    propagate like `asyncio.CancelledError` so the workflow driver maps each to
    its terminal `RT-FAIL-*` (e.g. `RT-FAIL-HITL-GATE-REJECTED`).

    Imported lazily: `hitl_gate_composer` imports this module, so a module-level
    import here would cycle (B-RESUME-RESPONSE-ROUTING; a marker base class is the
    drift-proof follow-on, registered not built).
    """
    from harness_runtime.lifecycle.hitl_gate_composer import (
        HITLCellExcludedError,
        HITLGateAuditComposeError,
        HITLGateEditDecodeError,
        HITLGateRejectedError,
        HITLGateTimeoutError,
    )

    return isinstance(
        exc,
        (
            HITLGateRejectedError,
            HITLGateEditDecodeError,
            HITLCellExcludedError,
            HITLGateTimeoutError,
            HITLGateAuditComposeError,
        ),
    )


class RoutedBindingResolver(Protocol):
    """The route-once SELECTION handle the stage-5 factory hands the C-RT-16
    wrapper (B-L2-FALLBACK-COMPOSITION, §14.6).

    Satisfied by ``RuntimeLLMDispatcher.resolve_routed_binding`` — a DIRECT
    handle to the bare C-RT-15 dispatcher's routing surface, NOT reached through
    the wrapper's ``inner`` (which is the HITL composer, two layers above the
    routing-capable dispatcher). Resolves the layered routing decision ONCE and
    returns a ``RoutedPrimaryResolution`` (the routed ``ModelBinding`` the
    wrapper seeds as its PRIMARY fallback candidate + the resolving
    ``RoutingDecisionTrace`` for span attribution per
    B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION), or ``None`` when routing_activation is
    off / a deterministic binding governs / nothing is routable."""

    async def __call__(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> RoutedPrimaryResolution | None: ...


@dataclass(slots=True)
class RetryBreakerFallbackDispatcher:
    """Per-step retry / breaker / fallback composer (C-RT-16).

    Wraps the bare C-RT-15 ``RuntimeLLMDispatcher`` (or any ``LLMDispatcher``
    Protocol-satisfying inner) with the candidate-iteration + retry-loop +
    breaker-coordination orchestration. Satisfies the
    ``harness_cp.workflow_driver.StepDispatcher`` Protocol via the same
    ``runtime_checkable`` introspection that the inner dispatcher satisfies.

    Attributes
    ----------
    inner :
        The inner ``LLMDispatcher`` (typically ``RuntimeLLMDispatcher``).
        Invoked exactly once per per-attempt iteration with a rebound
        ``StepEffectiveBinding`` whose ``model_binding`` is the current
        candidate's ``(provider, model)``.
    retry_breaker :
        The U-RT-24 registry. Used for policy lookup (``get_policy``),
        per-candidate breaker access (``get_breaker``), staircase
        advancement (``advance_staircase``), and ``harness.breaker.*``
        transition emission (``emit_breaker_transition_event``).
    fallback_chain :
        The stage 3b-bound ``FallbackChain``. The composer iterates its
        candidates in the §4.2 traversal order
        (primary → same-family → cross-family → terminal).
    tracer_provider :
        The stage 4 ``TracerProvider`` for outer + inner span emission.
        Typed ``Any`` for the same C-RT-04 reason ``RuntimeLLMDispatcher``
        uses (avoids pulling the OTel SDK type into the schema at L0).
    sleep_fn :
        Awaitable sleep function for full-jitter backoff between retry
        attempts. Defaults to ``asyncio.sleep``; tests inject a recording
        no-op to keep async tests fast and deterministic.
    routing_manifest :
        The stage-3a-bound ``RoutingManifest`` (``ctx.routing_manifest``).
        Read at dispatch for the U-RT-114 (§14.5.3) per-role MODEL binding:
        a non-default branch ``agent_role`` with a ``per_role_bindings`` entry
        promotes its ``preferred_model_binding`` to the PRIMARY fallback
        candidate (so per-role model specialization composes with C-RT-16
        fallback). ``None`` / default role / empty catalog ⟹ the stage-bound
        ``fallback_chain`` is used unchanged (the §14.5.3 non-breaking default).
    routing_resolver :
        The route-once SELECTION handle (B-L2-FALLBACK-COMPOSITION §14.6) — the
        bare C-RT-15 dispatcher's ``resolve_routed_binding`` method, handed by the
        stage-5 factory. When set AND it returns a routed ``ModelBinding`` (i.e.
        ``routing_activation`` is on and no deterministic binding governs), that
        model is promoted to the wrapper's PRIMARY fallback candidate ONCE per
        step — so the layered routing decision composes with the fallback chain
        instead of re-routing per attempt. ``None`` ⟹ no routing augmentation
        (the §14.5.3 per-role / stage-chain path governs, byte-identical).
    workload_class :
        The run ``WorkloadClass`` (stage-5-bound — the SAME value the inner
        ``RuntimeLLMDispatcher`` receives). Read at dispatch for the
        B-MODEL-RESOLUTION-CONSOLIDATION per-workload MODEL branch (§14.6): a
        ``per_workload_overrides[workload].model_binding_override`` promotes its
        ``ModelBinding`` to the PRIMARY fallback candidate when no higher-
        precedence (per-step / per-role) source governs. ``None`` ⟹ the MVP
        default workload class (``_MVP_DEFAULT_WORKLOAD_CLASS``), mirroring the
        ``resolve_routed_binding`` decline expression so the precedence authority
        (``_effective_chain``) and the routing decline never disagree (§14.6.2).
    """

    inner: LLMDispatcher
    retry_breaker: RetryBreakerRegistry
    fallback_chain: FallbackChain
    tracer_provider: Any
    sleep_fn: Callable[[float], Awaitable[None]] = field(default_factory=lambda: asyncio.sleep)
    routing_manifest: RoutingManifest | None = None
    routing_resolver: RoutedBindingResolver | None = None
    workload_class: WorkloadClass | None = None

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Invoke the step body under the effective binding with retry /
        breaker / fallback orchestration; return step output.

        ``step_context`` accepted at v1.6 Path A per amended
        ``StepDispatcher`` Protocol (C-RT-17 resolution). C-RT-16 wrapper
        does NOT consume ``step_context`` at v1.6 directly but passes it
        through to the inner C-RT-15 dispatcher per the Protocol
        conformance discipline. Inner dispatcher likewise does not consume
        at v1.6; reserved for v1.7+ surfaces.

        Raises
        ------
        RetryBreakerFallbackExhaustedError
            The fallback chain exhausted after per-candidate retry
            exhaustion. Maps to ``RT-FAIL-FALLBACK-EXHAUSTED``.
        asyncio.CancelledError
            Re-raised verbatim (shutdown / cancellation propagates).
        """
        policy = self.retry_breaker.get_policy(RESERVED_LLM_DISPATCH_KEY)
        tracer = self.tracer_provider.get_tracer("harness.runtime.retry_breaker_fallback")

        with tracer.start_as_current_span("harness.runtime.retry_breaker_fallback") as outer_span:
            # B-L2-FALLBACK-COMPOSITION (§14.6): resolve the layered-routing
            # PRIMARY candidate ONCE (route-once-then-fallback-the-chain). The
            # `routing_resolver` (the bare C-RT-15 dispatcher's
            # `resolve_routed_binding`) returns the routed model when
            # `routing_activation` is on AND no deterministic binding governs;
            # `None` ⟹ no augmentation (the §14.5.3 per-role / stage-chain path
            # governs, byte-identical). Resolved ONCE here — NOT inside the
            # per-attempt loop — so routing composes with fallback instead of
            # re-routing per attempt (the §14.5.3 "no two-authority-at-dispatch"
            # invariant the inner-routing re-evaluation otherwise broke).
            routed_resolution = (
                await self.routing_resolver(binding, step, step_context=step_context)
                if self.routing_resolver is not None
                else None
            )
            # The chain composes off the routed MODEL only; the resolving trace
            # (B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION) rides `ROUTED_PRIMARY_SPAN_TRACE`
            # to the inner span for the routed-PRIMARY dispatch below.
            routed = routed_resolution.model_binding if routed_resolution is not None else None
            # U-RT-114 (§14.5.3): resolve the per-dispatch chain for the branch
            # role. A routed primary (above) takes the chain PRIMARY; else a
            # non-default role with a `per_role_bindings` entry ⟹ an augmented
            # chain whose PRIMARY is the per-role model; else the stage-bound
            # `self.fallback_chain` unchanged. One source of truth for model
            # selection — the chain — so the inner C-RT-15 dispatcher faithfully
            # dispatches each rebound candidate.
            chain = self._effective_chain(binding, step_context.agent_role, routed=routed)
            candidate: ProviderCandidate = chain.primary
            last_failure_class: str | None = None
            chain_length = _chain_length(chain)
            outer_span.set_attribute("fallback.chain_length", chain_length)
            # C-CP-03 §3.3 ``capability_required`` — constant across the chain
            # traversal (it is a property of the call-site payload, not the
            # candidate). Empty for the common case (no tools, no thinking) ->
            # the §3.3 pre-check below is a no-op.
            required_caps = _required_capabilities(step)

            while True:
                # --- C-CP-03 §3.3 capability-shortfall pre-check ----------
                # If the current candidate cannot serve a capability the call
                # requires, emit ``fallback.triggered`` (cause =
                # capability_shortfall) and advance to the next provider BEFORE
                # the error path (the breaker pre-check + provider call). All
                # candidates short -> the chain exhausts ->
                # ``RetryBreakerFallbackExhaustedError`` (§3.2 step 3 fail-closed:
                # a thinking step with no thinking-capable provider fails rather
                # than silently receiving a non-thinking response — the
                # capability-preservation guarantee).
                missing_caps = frozenset(
                    cap
                    for cap in required_caps
                    if not provider_supports(
                        cap, reflect_provider_capabilities(candidate.provider, candidate.model)
                    )
                )
                if missing_caps:
                    outer_span.add_event(
                        "fallback.triggered",
                        attributes={
                            "fallback.from_provider": candidate.provider,
                            "fallback.from_model": candidate.model,
                            "fallback.cause": FallThroughCause.CAPABILITY_SHORTFALL.value,
                            "fallback.required_capability": ",".join(
                                sorted(cap.value for cap in missing_caps)
                            ),
                        },
                    )
                    last_failure_class = "capability-shortfall"
                    candidate = self._advance_or_exhaust(
                        candidate,
                        outer_span,
                        last_failure_class,
                        chain_length,
                        chain=chain,
                        exhaustion_cause="capability-shortfall",
                    )
                    continue

                # --- Step 3: breaker pre-check ----------------------------
                breaker_identifier = f"{candidate.provider}:{candidate.model}"
                breaker_obj = self.retry_breaker.get_breaker(
                    BreakerScope.PER_MODEL, breaker_identifier
                )
                # Protocol returns ``object`` to avoid a `types`↔`lifecycle`
                # import cycle; the concrete type is the L8 `BreakerStateMachine`.
                # Narrow via isinstance per the Protocol docstring's guidance.
                assert isinstance(breaker_obj, BreakerStateMachine), (
                    f"retry_breaker.get_breaker returned non-BreakerStateMachine "
                    f"object: {type(breaker_obj).__name__}"
                )
                breaker = breaker_obj
                if not breaker.should_attempt():
                    outer_span.add_event(
                        "retry.skipped",
                        attributes={
                            "retry.skipped.reason": "breaker-open",
                            "retry.skipped.candidate": breaker_identifier,
                        },
                    )
                    last_failure_class = "breaker-open"
                    candidate = self._advance_or_exhaust(
                        candidate, outer_span, last_failure_class, chain_length, chain=chain
                    )
                    continue

                # --- Step 4: per-attempt loop -----------------------------
                # B-L2-ROUTING-SPAN-LAYER-ATTRIBUTION (§14.6.1 scope-boundary
                # closure): publish the real resolving trace to the inner span
                # ONLY for the routed PRIMARY candidate. `chain.primary` is the
                # `_augment_primary`-seeded routed model when `routed_resolution`
                # is non-None; any later (advanced) candidate is a fallback the
                # CHAIN selected, not routing — so it keeps faithful echo
                # reporting (the ContextVar is cleared to None for it).
                _on_routed_primary = (
                    routed_resolution is not None
                    and candidate.provider == chain.primary.provider
                    and candidate.model == chain.primary.model
                )
                _span_trace_token = ROUTED_PRIMARY_SPAN_TRACE.set(
                    (routed_resolution.routing_trace, routed_resolution.binding_rationale)
                    if _on_routed_primary and routed_resolution is not None
                    else None
                )
                try:
                    attempt_terminal = await self._run_per_candidate_attempts(
                        binding=binding,
                        step=step,
                        candidate=candidate,
                        policy=policy,
                        breaker=breaker,
                        tracer=tracer,
                        outer_span=outer_span,
                        step_context=step_context,
                    )
                finally:
                    ROUTED_PRIMARY_SPAN_TRACE.reset(_span_trace_token)

                if attempt_terminal.result is not None:
                    # Success.
                    return attempt_terminal.result

                # Candidate abandoned; advance to next.
                last_failure_class = attempt_terminal.last_failure_class
                candidate = self._advance_or_exhaust(
                    candidate, outer_span, last_failure_class, chain_length, chain=chain
                )

    def _effective_chain(
        self,
        binding: StepEffectiveBinding,
        agent_role: AgentRole | None,
        *,
        routed: ModelBinding | None = None,
    ) -> FallbackChain:
        """Resolve the per-dispatch fallback chain — the SOLE model-resolution
        precedence authority (B-MODEL-RESOLUTION-CONSOLIDATION §14.6), placed at
        the dispatch-composition surface so model-candidate selection composes
        with C-RT-16 fallback (ONE source of truth — the chain — so the inner
        C-RT-15 dispatcher dispatches each rebound candidate faithfully).

        Precedence (operator-ratified): **per-step > per-workload > per-role >
        routed > default**. The first non-``None`` source becomes the AUGMENTED
        chain PRIMARY (``_augment_primary``); the stage chain is the deduped tail.
        per-workload beats per-role to MATCH the cleared cross-subsystem
        convention — the PROMPT subsystem resolves ``(role, workload)`` as
        ``per_workload_overrides > per_role_bindings`` (``resolve_active_prompt_version_sha``,
        "mirrors RoutingManifest workload-override-on-top-of-role"), so MODEL
        follows the same workload-over-role ordering (operator-confirmed at
        build-open 2026-06-22).

        - **per-step** ``binding.model_binding_override`` (C-CP-06 §6.2 `None`-or-
          override SIGNAL — distinct from ``binding.model_binding``, always
          concrete) ⟹ AUGMENTED chain.
        - **per-workload** ``per_workload_overrides[workload].model_binding_override``
          (W-2; the run workload, ``self.workload_class or
          _MVP_DEFAULT_WORKLOAD_CLASS``) ⟹ AUGMENTED chain. Applies routing-on AND
          routing-off (the present-tense gap closed: per-workload ENGINE+PROMPT
          were already consumed routing-off; MODEL now joins them).
        - **per-role**: a NON-default branch role with a ``per_role_bindings``
          entry ⟹ AUGMENTED chain whose PRIMARY is ``preferred_model_binding``
          (U-RT-114 §14.5.3; per-role MODEL specialization).
        - **routed** (B-L2-FALLBACK-COMPOSITION): a routing_activation routed
          model ⟹ AUGMENTED chain. ``routed`` is non-``None`` ONLY when no
          higher-precedence deterministic binding governs (``resolve_routed_binding``
          declines otherwise) — so it fills only the no-override gap.
        - **default**: none of the above ⟹ the stage-bound ``self.fallback_chain``
          UNCHANGED (the §14.5.3 non-breaking-default + linear-path-untouched
          invariants; empty catalog + routing off ⟹ zero behaviour change).

        §14.6.2 invariant — *the decline predicate ⊆ this authority*: each
        deterministic branch here (per-step / per-workload / per-role) has a
        MATCHING conjunct in ``resolve_routed_binding``'s decline expression (the
        decline is an order-independent disjunction, so the per-workload/per-role
        swap does not affect it), so the decline is never STRICTER than this
        resolution. Were it stricter, routing would decline (``routed`` ``None``)
        while this method also skipped the source, silently dropping to ``default``.
        """
        # 1. per-step MODEL override (highest precedence).
        if binding.model_binding_override is not None:
            return self._augment_primary(_candidate_from_binding(binding.model_binding_override))
        # 2. per-workload MODEL override (beats per-role per the cleared
        #    workload-over-role convention; mirrors the decline's
        #    `self.workload_class or _MVP_DEFAULT_WORKLOAD_CLASS` EXACTLY).
        if self.routing_manifest is not None:
            workload = self.workload_class or _MVP_DEFAULT_WORKLOAD_CLASS
            workload_override = self.routing_manifest.per_workload_overrides.get(workload)
            if (
                workload_override is not None
                and workload_override.model_binding_override is not None
            ):
                return self._augment_primary(
                    _candidate_from_binding(workload_override.model_binding_override)
                )
        # 3. per-role MODEL binding (NON-default role; mirrors the decline's
        #    `_role != _MVP_DEFAULT_AGENT_ROLE and _role in per_role_bindings`).
        role = agent_role or _MVP_DEFAULT_AGENT_ROLE
        if role != _MVP_DEFAULT_AGENT_ROLE and self.routing_manifest is not None:
            role_binding = self.routing_manifest.per_role_bindings.get(role)
            if role_binding is not None:
                return self._augment_primary(
                    _candidate_from_binding(role_binding.preferred_model_binding)
                )
        # 4. routed (fills only the no-override gap; non-None ⟹ no higher source).
        if routed is not None:
            return self._augment_primary(_candidate_from_binding(routed))
        # 5. default — the stage-bound chain unchanged (§14.5.3 non-breaking).
        return self.fallback_chain

    def _augment_primary(self, new_primary: ProviderCandidate) -> FallbackChain:
        """Promote ``new_primary`` to the chain PRIMARY with the stage-bound
        chain's full §4.2 traversal as the deduped tail.

        The U-RT-114 §14.5.3 chain-augmentation, SHARED by the per-role MODEL
        binding and the B-L2-FALLBACK-COMPOSITION routed primary: the selected
        model is tried first; the operator-configured chain applies on its
        failure — so model specialization is non-hollow AND fallback is preserved.

        Dedup by ``(provider, model)``: if ``new_primary`` already appears in the
        chain, it is not re-listed as its own fallback (avoid a same-model
        double-dispatch on advance). When the stage chain IS that single model,
        the chain is returned UNCHANGED.
        """
        base = self.fallback_chain
        original_ordered = (
            base.primary,
            *base.same_family,
            *base.cross_family,
            *((base.terminal,) if base.terminal is not None else ()),
        )
        tail = tuple(
            c
            for c in original_ordered
            if (c.provider, c.model) != (new_primary.provider, new_primary.model)
        )
        if not tail:
            return base
        return FallbackChain(
            primary=new_primary,
            same_family=(),
            cross_family=tail,
            terminal=None,
        )

    def _advance_or_exhaust(
        self,
        failed: ProviderCandidate,
        outer_span: Any,
        last_failure_class: str | None,
        chain_length: int,
        *,
        chain: FallbackChain,
        exhaustion_cause: str = "per-candidate-retry-exhaustion",
    ) -> ProviderCandidate:
        """Advance to the next candidate or raise on exhaustion (Step 5).

        Emits ``fallback.exhausted`` on the outer span before raising the typed
        ``RetryBreakerFallbackExhaustedError``. ``exhaustion_cause`` attributes
        the terminal event: the default reflects per-candidate retry exhaustion;
        the C-CP-03 §3.3 capability-shortfall pre-check passes
        ``"capability-shortfall"`` so an all-candidates-incapable chain is not
        mis-classified as retry exhaustion (no provider attempt ran in that case).

        ``chain`` is the per-dispatch effective chain (U-RT-114 §14.5.3) — the
        stage-bound ``self.fallback_chain`` for the default path, or the
        role-augmented chain for a per-role branch — so ``advance_or_raise``
        traverses the SAME chain the dispatch started from (``failed`` is a
        member of it; using ``self.fallback_chain`` here would raise spuriously
        on the augmented primary).
        """
        try:
            next_candidate, _result = advance_or_raise(chain, failed)
        except FallbackChainExhaustedError as exc:
            outer_span.add_event(
                "fallback.exhausted",
                attributes={
                    "fallback.chain_length": chain_length,
                    "fallback.last_failure_class": last_failure_class or "unknown",
                    "fallback.exhaustion_cause": exhaustion_cause,
                },
            )
            raise RetryBreakerFallbackExhaustedError(failed) from exc
        return next_candidate

    async def _run_per_candidate_attempts(
        self,
        *,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        candidate: ProviderCandidate,
        policy: RetryPolicy,
        breaker: Any,
        tracer: Any,
        outer_span: Any,
        step_context: StepExecutionContext,
    ) -> _PerCandidateTerminal:
        """Run the per-attempt loop for a single candidate (Step 4).

        Returns a ``_PerCandidateTerminal`` carrying either the successful
        result (return path) or the last-failure attribution (abandon path).

        The C-CP-21 §21.2 staircase is consulted as a **cause-class classifier**,
        not a per-attempt counter: `advance_staircase` is stage-keyed lookup
        and `attempt` is bookkeeping per the function's own docstring. The
        wrapper passes ``STAGE_1_REFLEXION`` as `current` on every call;
        transient classes (`TRANSIENT_RETRY` / `REFLEXION_RECOVERABLE`) route
        to ``STAGE_2_RETRY_WITH_BACKOFF`` (retry); skip-classes
        (`PERMANENT_FAIL_EXIT` / `TERMINAL_FAIL_EXIT` / `HITL_RECOVERABLE`)
        route directly to ``STAGE_5_HITL_ESCALATION`` (escalate). The
        per-attempt loop is capped by ``RetryPolicy.max_attempts``; both the
        staircase-escalate branch and the max-attempts-exhaustion branch are
        reachable under this reading.
        """
        rebound = _rebind_to_candidate(binding, candidate)
        last_failure_class: str | None = None

        # Derive the canonical attribute values that don't depend on per-attempt state.
        original_span_id_hex = _format_span_id_hex(outer_span)
        replay_disposition = REPLAY_DISPOSITION_MAPPING[binding.engine_class]

        for attempt in range(policy.max_attempts):
            with tracer.start_as_current_span("harness.runtime.retry_attempt") as inner_span:
                # CP-canonical retry.* 6-attribute namespace per Spec_Control_Plane_v1_3.md
                # §3.5 + ADR-D1 v1.2 §1.1.1 + landed carrier at
                # `harness_cp.retry_fallback_namespace.RETRY_ATTEMPT_CHILD_SPAN_SCHEMA`.
                # `retry.attempt_number` is 1-indexed (canonical); `retry.original_span_id`
                # carries the outer wrapper span's 16-hex W3C trace-context span_id as
                # the original-operation reference per spec §14.6 v1.5 amendment.
                inner_span.set_attribute("retry.attempt_number", attempt + 1)
                inner_span.set_attribute("retry.original_span_id", original_span_id_hex)
                inner_span.set_attribute("engine.replay_disposition", replay_disposition.value)

                try:
                    result = await self.inner.dispatch(rebound, step, step_context=step_context)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if _is_hitl_terminal_control_flow(exc):
                        # Terminal HITL gate decision (REJECT / EDIT-decode /
                        # cell-excluded / timeout / placement-foreclosed / audit-
                        # compose) — NOT a transient provider failure. Propagate
                        # untouched so it is neither retried nor candidate-advanced
                        # (both re-fire the gate; a durable-async resume re-fire
                        # re-PAUSES). The driver maps it to its terminal RT-FAIL-*.
                        raise
                    cause = _classify_provider_exception(exc)
                    if cause is None:
                        # Fail-fast: AUTH / payload-shape. Per CP §3.5 canonical
                        # 5-class taxonomy, fail-fast maps to PERMANENT_FAIL_EXIT.
                        # `retry.cause_attribution` carries the broader
                        # open-set string (Python exception class name MVP).
                        inner_span.set_attribute("retry.delay_ms", 0)
                        inner_span.set_attribute("retry.cause_attribution", type(exc).__name__)
                        inner_span.set_attribute(
                            "retry.fail_class",
                            ValidatorRetryExitClass.PERMANENT_FAIL_EXIT.value,
                        )
                        transition = breaker.record_failure()
                        if transition is not None:
                            self._emit_breaker_transition(transition, outer_span)
                        last_failure_class = type(exc).__name__
                        return _PerCandidateTerminal(
                            result=None, last_failure_class=last_failure_class
                        )

                    # Transient: classify via staircase. STAGE_1 is passed as
                    # `current` every time — the staircase is a cause-class
                    # classifier, not stateful across attempts (per the spec
                    # narrative + `advance_staircase` docstring).
                    staircase_transition = self.retry_breaker.advance_staircase(
                        StaircaseStage.STAGE_1_REFLEXION, cause, attempt
                    )
                    next_stage = staircase_transition.to_stage
                    is_last_attempt = attempt == policy.max_attempts - 1
                    if (
                        next_stage is StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF
                        and not is_last_attempt
                    ):
                        backoff_seconds = self.retry_breaker.compute_delay_seconds(attempt)
                        inner_span.set_attribute("retry.delay_ms", int(backoff_seconds * 1000))
                        inner_span.set_attribute("retry.cause_attribution", cause.value)
                        inner_span.set_attribute("retry.fail_class", cause.value)
                        last_failure_class = cause.value
                        # Sleep outside the span CM is fine; OTel ends the span
                        # at the with-exit. We sleep after recording the
                        # attempt terminal so the inner span carries the
                        # actually-elapsed backoff hint.
                    elif (
                        next_stage is StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF and is_last_attempt
                    ):
                        # Staircase would retry, but max_attempts exhausted.
                        # Per CP §3.5: `retry.fail_class = terminal-fail-exit`
                        # for the last-attempt exhaustion (canonical 5-class entry).
                        inner_span.set_attribute("retry.delay_ms", 0)
                        inner_span.set_attribute("retry.cause_attribution", cause.value)
                        inner_span.set_attribute(
                            "retry.fail_class",
                            ValidatorRetryExitClass.TERMINAL_FAIL_EXIT.value,
                        )
                        transition = breaker.record_failure()
                        if transition is not None:
                            self._emit_breaker_transition(transition, outer_span)
                        last_failure_class = "max-attempts"
                        return _PerCandidateTerminal(
                            result=None, last_failure_class=last_failure_class
                        )
                    else:
                        # Escalation: cross-family-fallback / local-terminal /
                        # HITL-escalation — abandon this candidate. The staircase
                        # returned STAGE_5_HITL_ESCALATION for a skip-class cause;
                        # canonical `retry.fail_class` for this path is the cause
                        # class itself (HITL_RECOVERABLE / PERMANENT_FAIL_EXIT /
                        # TERMINAL_FAIL_EXIT per the staircase skip-class set).
                        inner_span.set_attribute("retry.delay_ms", 0)
                        inner_span.set_attribute("retry.cause_attribution", cause.value)
                        inner_span.set_attribute("retry.fail_class", cause.value)
                        transition = breaker.record_failure()
                        if transition is not None:
                            self._emit_breaker_transition(transition, outer_span)
                        last_failure_class = cause.value
                        return _PerCandidateTerminal(
                            result=None, last_failure_class=last_failure_class
                        )
                else:
                    # Success. `retry.fail_class` is canonically only set on
                    # failure paths; on success it's omitted (a span tail-keep
                    # sampler reads `retry.fail_class` presence as a fail signal
                    # per C-CP-03 §3.5 sampling discipline). `retry.delay_ms = 0`
                    # makes the success span numerically distinct from retries.
                    inner_span.set_attribute("retry.delay_ms", 0)
                    transition = breaker.record_success()
                    if transition is not None:
                        self._emit_breaker_transition(transition, outer_span)
                    return _PerCandidateTerminal(result=result, last_failure_class=None)

            # Sleep between retries (outside the inner span CM).
            await self.sleep_fn(self.retry_breaker.compute_delay_seconds(attempt))

        # Unreachable: the last attempt's `is_last_attempt` branch above
        # always returns. Kept as a defensive fallback if the iteration
        # invariant ever drifts.
        return _PerCandidateTerminal(
            result=None,
            last_failure_class=last_failure_class or "max-attempts",
        )

    def _emit_breaker_transition(self, transition: Any, parent_span: Any) -> None:
        """Delegate breaker-transition span emission to the registry per
        spec §14.6 D5. Wrapper code is thin against breaker concerns."""
        self.retry_breaker.emit_breaker_transition_event(
            transition,
            parent_span,
        )


@dataclass(frozen=True, slots=True)
class _PerCandidateTerminal:
    """Per-candidate attempt-loop terminal carrier.

    ``result`` non-None iff a successful dispatch occurred (return path).
    Otherwise ``last_failure_class`` carries the last attribution token for
    the outer ``fallback.exhausted`` event.
    """

    result: Mapping[str, Any] | None
    last_failure_class: str | None


def _format_span_id_hex(span: Any) -> str:
    """Format an OTel span's ``span_id`` as 16-hex W3C trace-context (canonical
    `retry.original_span_id` format per CP §3.5 v1.3).

    OTel SDK exposes ``span.get_span_context().span_id`` as a 64-bit integer;
    the W3C Trace Context spec formats it as 16 lowercase hex characters,
    zero-padded. Returns the empty string if the span isn't recording (no
    span context available).
    """
    span_context = span.get_span_context() if hasattr(span, "get_span_context") else None
    if span_context is None or not getattr(span_context, "is_valid", True):
        return ""
    span_id_int = span_context.span_id
    return format(span_id_int, "016x")


def _chain_length(chain: FallbackChain) -> int:
    """Total candidate count across the §4.1 four-field structure."""
    return (
        1
        + len(chain.same_family)
        + len(chain.cross_family)
        + (1 if chain.terminal is not None else 0)
    )


def materialize_retry_breaker_fallback_dispatcher_stage(
    *,
    inner: LLMDispatcher,
    retry_breaker: RetryBreakerRegistry,
    fallback_chain: FallbackChain,
    tracer_provider: Any,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    routing_manifest: RoutingManifest | None = None,
    routing_resolver: RoutedBindingResolver | None = None,
    workload_class: WorkloadClass | None = None,
) -> RetryBreakerFallbackDispatcher:
    """Stage 5 LOOP_INIT factory for the retry/breaker/fallback wrapper
    (U-RT-58, C-RT-16).

    Constructs the wrapper around the inner C-RT-15 dispatcher. Bootstrap
    stage 5 invokes this AFTER the bare ``RuntimeLLMDispatcher`` is built
    (per the existing ``materialize_llm_dispatcher_stage`` site) and
    rebinds ``ctx.llm_dispatcher`` to the wrapper.

    The wrapper consumes ``ctx.retry_breaker`` (U-RT-24) +
    ``ctx.fallback_chain`` (stage 3b) + ``ctx.tracer_provider`` (C-RT-06)
    + the inner dispatcher (private). ``StepDispatcher`` Protocol shape
    preserved per spec §14.6. ``routing_manifest`` (``ctx.routing_manifest``,
    stage 3a) is passed for the U-RT-114 §14.5.3 per-role MODEL dispatch-read;
    ``None`` ⟹ no per-role routing (the §14.5.3 non-breaking default).

    ``routing_resolver`` (B-L2-FALLBACK-COMPOSITION §14.6) is the bare C-RT-15
    dispatcher's ``resolve_routed_binding`` handle — passed DIRECTLY (NOT via
    the HITL ``inner``) so the wrapper resolves the layered-routing PRIMARY
    candidate ONCE per step. ``None`` ⟹ no routing augmentation (byte-identical
    to the pre-B-L2 default).

    ``workload_class`` (B-MODEL-RESOLUTION-CONSOLIDATION §14.6) is the run
    workload — the SAME value handed to the inner ``RuntimeLLMDispatcher`` — so
    the wrapper can honour a ``per_workload_overrides[workload].model_binding_override``
    at the per-workload precedence tier. ``None`` ⟹ the MVP default workload class.
    """
    return RetryBreakerFallbackDispatcher(
        inner=inner,
        retry_breaker=retry_breaker,
        fallback_chain=fallback_chain,
        tracer_provider=tracer_provider,
        sleep_fn=sleep_fn,
        routing_manifest=routing_manifest,
        routing_resolver=routing_resolver,
        workload_class=workload_class,
    )
