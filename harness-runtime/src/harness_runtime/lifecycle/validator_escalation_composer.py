"""ValidatorEscalationGateComposer — mid-step re-entrant HITL composer (Reading B).

Implements runtime spec v1.22 §14.15 C-RT-25 contract. Fired at workflow_driver
post-dispatch hook when ``ValidatorEvaluation.next_action == ESCALATE_HITL``
per C-CP-28 §25.2 ValidatorOutcome → ValidatorNextAction mapping. Sibling to
``hitl_gate_composer.py`` (wrap-time pre-action / sub-agent-boundary path).

**Authority anchors (Reading B HIGH-confidence per scoping doc §2 Q1):**
- C-CP-28 §25.3 sync post-dispatch lifecycle
- C-CP-28 §25.4 invariant 2 ("after dispatch, before ledger append")
- C-CP-28 §25.4 + §25.7 invariant 4 ("ESCALATE always emits HITL gate.
  Escalation cannot be silently dropped.")

The composer is a free function (not a stateful instance) — it consumes
``ctx.ask_user_question_surface`` (already bound at stage 5 per §14.8.3) +
``ctx.tracer_provider``. No NEW bootstrap-stage factory; no NEW HarnessContext
field; no NEW RuntimeConfig field — Reading B's runtime-spec-only scope per
scoping doc §1.3.

Spans emitted per §14.15.2 step 2-3 + canonical 4-span shape inheritance:
``validator.escalation`` parent → ``hitl.gate.evaluated`` → ``hitl.invocation.
opened`` → ``hitl.invocation.responded`` OR ``.timed_out``. Parent-context
propagation enforced by-construction via OTel ``start_as_current_span``
nesting per spec §14.15.4 invariant 5.
"""

from __future__ import annotations

from typing import Any

from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.validator_framework_types import HITLEscalationBrief
from opentelemetry.trace import Status, StatusCode

from harness_runtime.lifecycle.ask_user_question_surface import (
    AskUserQuestionResult,
    AskUserQuestionSurface,
    AskUserQuestionTimeoutError,
)
from harness_runtime.lifecycle.effective_palette import compute_effective_palette
from harness_runtime.lifecycle.escalation_prompt import compose_escalation_prompt

__all__ = [
    "ValidatorEscalationGateAuditComposeError",
    "ValidatorEscalationGateRejectedError",
    "ValidatorEscalationGateTimeoutError",
    "compose_validator_escalation_gate",
]


class ValidatorEscalationGateAuditComposeError(Exception):
    """Audit composition substep raised a typed error on APPROVE/EDIT/RESPOND.

    Maps to ``RT-FAIL-VALIDATOR-ESCALATION-GATE-COMPOSE`` per runtime spec
    v1.22 §14.15.5. SUPPRESSED on REJECT path — the rejection brief is the
    primary fault.
    """


class ValidatorEscalationGateRejectedError(Exception):
    """Operator selected REJECT at the validator-escalation HITL gate.

    Mirrors ``HITLGateRejectedError`` from the wrap-time composer; maps to
    the existing ``RT-FAIL-HITL-GATE-REJECTED`` fail class per spec §14.8
    failure-mode taxonomy (re-used at §14.15 per scoping doc §4).
    """


class ValidatorEscalationGateTimeoutError(Exception):
    """``AskUserQuestionSurface.ask`` timed out at validator-escalation gate.

    Mirrors ``HITLGateTimeoutError`` from the wrap-time composer; maps to
    ``RT-FAIL-HITL-GATE-TIMEOUT`` per spec §14.8 failure-mode taxonomy.
    """


# Default timeout for validator-escalation gate invocation (v1.22 MVP).
# Implementation discretion at the C-RT-25 landing arc per spec §14.15.8;
# operator-supplied per-gate timeout is a separate follow-on enhancement.
ESCALATION_TIMEOUT_DEFAULT: float = 300.0
"""Default operator-response deadline in seconds for validator-escalation gates.

5 minutes. Mirrors AskUserQuestion default deadline conventions. Operator-
supplied per-gate override is implementation discretion at follow-on arc.
"""


async def compose_validator_escalation_gate(
    *,
    ask_user_question_surface: AskUserQuestionSurface,
    brief: HITLEscalationBrief,
    step_action_id: str,
    cross_trust_state: CrossTrustBoundaryState,
    gate_level: GateLevel,
    tracer_provider: Any,
) -> HITLResponse:
    """Fire a mid-step re-entrant HITL gate for a validator escalation outcome.

    Invoked by workflow_driver post-dispatch hook when
    ``ValidatorEvaluation.next_action == ValidatorNextAction.ESCALATE_HITL``
    per C-CP-28 §25.2 mapping. Synchronous (async-await) within the step,
    pre-ledger-append per C-CP-28 §25.4 invariant 2.

    Parameters
    ----------
    ask_user_question_surface
        The H_E delivery surface bound at bootstrap stage 5 per §14.8.3.
        Sourced from ``ctx.ask_user_question_surface`` at workflow_driver hook.
    brief
        The ``HITLEscalationBrief`` typed payload from
        ``ValidatorEvaluation.result.escalation_brief``.
    step_action_id
        Parent step ``action_id`` for audit trail. Sourced from current
        ``StepExecutionContext.parent_action_id`` at the hook.
    cross_trust_state
        Current cross-trust-boundary state per C-CP-21 §21.3 4-class enum.
        Derivation source is implementation discretion per spec §14.15.8;
        recommended: closure-over-ctx (read ``ctx.mcp_client_hosts`` current
        server trust state + binding-derived cross-family flag).
    gate_level
        The computed gate level per C-CP-19 §19.1. v1.22 MVP may pass
        ``GateLevel.ASK`` unconditionally at validator-escalation paths
        (the escalation itself implies HITL required); future arc may
        consume per-step computed gate level.
    tracer_provider
        OTel TracerProvider for ``validator.escalation`` + ``hitl.*`` span
        emission. Sourced from ``ctx.tracer_provider`` at the hook.

    Returns
    -------
    HITLResponse
        The operator's selected response (APPROVE / EDIT / REJECT / RESPOND).
        Workflow_driver hook body branches on this per spec §14.15.2 step 8.

    Raises
    ------
    ValidatorEscalationGateRejectedError
        Operator selected REJECT.
    ValidatorEscalationGateTimeoutError
        ``AskUserQuestionSurface.ask`` timed out.
    ValidatorEscalationGateAuditComposeError
        Audit composition substep raised on non-REJECT path.
    """
    tracer = tracer_provider.get_tracer("harness.runtime.validator_escalation")

    # §14.15.2 step 1 — Compute effective palette via UNION-intersection.
    palette = compute_effective_palette(
        gate_level=gate_level,
        cross_trust_state=cross_trust_state,
        validator_escalation_brief=brief,
    )

    # §14.15.2 step 2 — Open validator.escalation span (C-CP-28 §25.5).
    with tracer.start_as_current_span("validator.escalation") as escalation_span:
        escalation_span.set_attribute("step.id", step_action_id)
        escalation_span.set_attribute("validator.outcome", "escalate")
        # Post-CP-v1.18 §25.2.X: HITLEscalationBrief.fail_class is now
        # `ValidatorFailClass | None` (widened to support the pause-trigger
        # composer-body construction site at runtime spec §14.8.8.1 step 1).
        # The validator-escalation path constructs the brief FROM a real
        # validator failure (ValidatorOutcome.ESCALATE) so fail_class should
        # be non-None here; fall back to "unspecified" defensively for the
        # widened type.
        escalation_span.set_attribute(
            "validator.fail.class",
            brief.fail_class.value if brief.fail_class is not None else "unspecified",
        )

        # §14.15.2 step 3 — Open hitl.gate.evaluated span within escalation
        # span parent-context (canonical 4-span shape per §14.8.5 inheritance).
        with tracer.start_as_current_span("hitl.gate.evaluated") as gate_span:
            gate_span.set_attribute("hitl.gate.level", gate_level.value)
            gate_span.set_attribute(
                "hitl.gate.persona_tier",
                "unknown",  # impl-discretion v1.22
            )
            # §14.15.4 invariant 2: escalation by definition requires HITL.
            gate_span.set_attribute("hitl.gate.required", True)

            # §14.15.2 step 4 — Open hitl.invocation.opened span.
            with tracer.start_as_current_span("hitl.invocation.opened") as invocation_span:
                invocation_span.set_attribute("hitl.gate.level", gate_level.value)
                invocation_span.set_attribute("hitl.invocation.placement", "validator_escalation")

                # §14.15.2 step 5 — Invoke gate via AskUserQuestion.
                prompt = compose_escalation_prompt(brief, palette)
                try:
                    result: AskUserQuestionResult = await ask_user_question_surface.ask(
                        prompt=prompt,
                        options=sorted(palette, key=lambda r: r.value),
                        timeout=ESCALATION_TIMEOUT_DEFAULT,
                    )
                except AskUserQuestionTimeoutError as exc:
                    # §14.15.2 step 6 — Timeout path.
                    with tracer.start_as_current_span("hitl.invocation.timed_out") as timeout_span:
                        timeout_span.set_attribute(
                            "hitl.timeout.duration_ms",
                            ESCALATION_TIMEOUT_DEFAULT * 1000,
                        )
                        timeout_span.set_attribute(
                            "hitl.timeout.degradation_mode_applied", "fail-closed"
                        )
                    raise ValidatorEscalationGateTimeoutError(
                        f"Validator-escalation HITL gate timed out at "
                        f"step_action_id={step_action_id!r}"
                    ) from exc

                # §14.15.2 step 6 — Response received.
                with tracer.start_as_current_span("hitl.invocation.responded") as responded_span:
                    responded_span.set_attribute("hitl.response.class", result.response.value)
                    responded_span.set_attribute("hitl.response.latency_ms", result.latency_ms)

                # §14.15.2 step 7 — Audit composition (deferred to follow-on
                # CP composer arc per scoping doc adjacent observation (d);
                # `validator:` action_id prefix discriminator + CXA v2.9 §0.3
                # enumeration refresh. v1.22 MVP: span emission only; full
                # 4-substep audit-write chain is implementation discretion
                # at follow-on arc).

                # §14.15.2 step 8 — Process gate response.
                if result.response == HITLResponse.REJECT:
                    raise ValidatorEscalationGateRejectedError(
                        f"Operator rejected validator-escalation at "
                        f"step_action_id={step_action_id!r}; "
                        f"reason={result.rejection_reason!r}"
                    )

                # APPROVE / EDIT / RESPOND — return to workflow_driver hook
                # per spec §14.15.2 step 8 + §14.15.8 deferred-discretion
                # (per-response branch semantics at hook body).
                escalation_span.set_status(Status(StatusCode.OK))
                return result.response
