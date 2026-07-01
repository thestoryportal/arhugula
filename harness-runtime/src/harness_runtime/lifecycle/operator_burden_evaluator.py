"""U-RT-70 — `OperatorBurdenEvaluator` + carriers.

Per `Spec_Harness_Runtime_v1.md` v1.13 §14.10.1 architectural surfaces +
§14.10.3 spans (`hitl.operator_burden.evaluated`) + §14.10.4 fail class
`RT-FAIL-HITL-OPERATOR-BURDEN-DEGRADATION-CONFLICT`.

Per `Implementation_Plan_Harness_Runtime_v2_11.md` §1 U-RT-70 (5 ACs).

Owns cross-step HITL burden aggregation per persona-tier configuration.
Degradation policy fires when threshold exceeded. Sampling discipline
per spec §14.10.3: head=1.0 only on `degrade=true`; otherwise head=0.1
(tail-keep on degradation per D6 §1.3).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from harness_core.persona_tier import PersonaTier

__all__ = [
    "ATTR_HITL_OPERATOR_BURDEN_CUMULATIVE_INVOCATIONS",
    "ATTR_HITL_OPERATOR_BURDEN_DEGRADE",
    "ATTR_HITL_OPERATOR_BURDEN_PERSONA_TIER",
    "ATTR_HITL_OPERATOR_BURDEN_WINDOW_MS",
    "BurdenSpanCounter",
    "DegradationDecision",
    "DegradationPolicy",
    "OperatorBurdenDegradationConflictError",
    "OperatorBurdenEvaluator",
    "OperatorBurdenScore",
    "SpanWindow",
    "materialize_operator_burden_evaluator_stage",
]


# --- attribute-name constants (spec §14.10.3) -------------------------------

ATTR_HITL_OPERATOR_BURDEN_CUMULATIVE_INVOCATIONS = "hitl.operator_burden.cumulative_invocations"
ATTR_HITL_OPERATOR_BURDEN_WINDOW_MS = "hitl.operator_burden.window_ms"
ATTR_HITL_OPERATOR_BURDEN_PERSONA_TIER = "hitl.operator_burden.persona_tier"
ATTR_HITL_OPERATOR_BURDEN_DEGRADE = "hitl.operator_burden.degrade"


# --- typed errors ----------------------------------------------------------


class OperatorBurdenDegradationConflictError(RuntimeError):
    """`RT-FAIL-HITL-OPERATOR-BURDEN-DEGRADATION-CONFLICT` typed carrier.

    Raised when `DegradationDecision.degrade=true` but no policy match —
    indicates a policy table missing the persona-tier row required to
    pick a `degradation_mode`.
    """


# --- carriers --------------------------------------------------------------


DegradationMode = Literal[
    "auto_approve",
    "auto_reject",
    "pause_workflow",
    "operator_notify",
]


@dataclass(frozen=True)
class SpanWindow:
    """Rolling-window time bounds for burden aggregation (epoch ms)."""

    start: int
    end: int


@dataclass(frozen=True)
class OperatorBurdenScore:
    """Per-window per-persona-tier burden score carrier."""

    cumulative_invocations: int
    window_start: int
    window_end: int
    persona_tier: PersonaTier


@dataclass(frozen=True)
class DegradationPolicy:
    """Per-deployment degradation threshold policy.

    Per spec §14.10.1 the canonical policy type lives at CP plan U-CP-25
    `CP_ROUTING` registry `on_hitl_timeout`. The U-RT-70 contract
    pins the field set this composer reads: per-persona-tier
    `threshold_invocations` + per-persona-tier `degradation_mode`. Operator
    supplies via bootstrap config.
    """

    threshold_invocations: int
    degradation_mode: DegradationMode


@dataclass(frozen=True)
class DegradationDecision:
    """Degradation verdict carrier per spec §14.10.1."""

    degrade: bool
    degradation_mode: DegradationMode | None
    reason: str


BurdenSpanCounter = Callable[[SpanWindow, PersonaTier], int]
"""Operator-supplied resolver mapping (window, persona) → invocation count.

Per spec §14.10.6 deferred-to-discretion: the actual span-aggregation
mechanism (OD span exporter query / counter table / OTel-native API) is
deployment-shaped. The evaluator delegates to the operator-supplied
counter; default raises on production misconfig.
"""


def _default_burden_span_counter(_window: SpanWindow, _persona_tier: PersonaTier) -> int:
    raise LookupError(
        "default BurdenSpanCounter invoked — operator must supply a "
        "burden_span_counter at OperatorBurdenEvaluator.__init__ that "
        "queries the OD span store / counter table for cumulative HITL "
        "invocations within the window per persona-tier"
    )


# --- evaluator -------------------------------------------------------------


class OperatorBurdenEvaluator:
    """Cross-step HITL burden aggregator + degradation arbiter per
    C-RT-20 §14.10.1.

    Per spec §14.10.5 invariants:
    - inv 3: deterministic — same (score, policy) → same DegradationDecision
    - inv 2: window operator-configurable (default 1-hour rolling)
    """

    def __init__(
        self,
        *,
        burden_span_counter: BurdenSpanCounter | None = None,
        tracer_provider: Any = None,
        burden_window_ms: int = 3_600_000,  # 1 hour rolling default
        rng: random.Random | None = None,
    ) -> None:
        """Construct evaluator with operator-supplied burden source + tracer.

        Parameters
        ----------
        burden_span_counter:
            Operator-supplied callable resolving (SpanWindow, PersonaTier) →
            cumulative HITL invocation count. Default raises on production
            misconfig (mirrors the U-CP-68 + U-CP-69 + U-RT-67 default-
            resolver-raises pattern — 4th consecutive cluster).
        tracer_provider:
            OTel `TracerProvider`-shaped object (typed `Any`). Used to
            open the `hitl.operator_burden.evaluated` span. If `None`,
            span emission is skipped.
        burden_window_ms:
            Rolling-window size in epoch-ms. Default 1 hour per spec
            §14.10.5 inv 2. Operator may override via
            `ctx.surface_config.burden_window_overrides` per persona-tier
            in a follow-on arc (deferred per §14.10.6).
        rng:
            Random source for sampling-discipline decisions. Default
            constructs a fresh `random.Random()`. Tests inject a seeded
            instance for determinism.
        """
        self._burden_span_counter: BurdenSpanCounter = (
            burden_span_counter or _default_burden_span_counter
        )
        self._tracer_provider = tracer_provider
        self._burden_window_ms = burden_window_ms
        self._rng = rng or random.Random()

    async def compute_operator_burden(
        self,
        span_window: SpanWindow,
        persona_tier: PersonaTier,
    ) -> OperatorBurdenScore:
        """Per spec §14.10.1 — aggregate HITL spans within window per persona.

        Delegates to `burden_span_counter` for the actual aggregation; this
        method composes the result envelope.
        """
        count = self._burden_span_counter(span_window, persona_tier)
        return OperatorBurdenScore(
            cumulative_invocations=count,
            window_start=span_window.start,
            window_end=span_window.end,
            persona_tier=persona_tier,
        )

    async def should_degrade(
        self,
        score: OperatorBurdenScore,
        degradation_policy: DegradationPolicy,
    ) -> DegradationDecision:
        """Per spec §14.10.1 — deterministic degradation arbitration.

        Emits the `hitl.operator_burden.evaluated` span with sampling
        discipline per spec §14.10.3: head=1.0 on degrade=true; else
        head=0.1.

        Per spec §14.10.4 — if degrade=true but `degradation_policy`
        has no degradation_mode (degenerate config), raise
        `OperatorBurdenDegradationConflictError`.
        """
        degrade = score.cumulative_invocations >= degradation_policy.threshold_invocations
        degradation_mode: DegradationMode | None
        reason: str
        if degrade:
            degradation_mode = degradation_policy.degradation_mode
            reason = (
                f"cumulative_invocations={score.cumulative_invocations} "
                f">= threshold={degradation_policy.threshold_invocations}"
            )
            # Defensive — though dataclass requires the mode, defend
            # against future-extension policies with empty-string mode.
            if not degradation_mode:
                raise OperatorBurdenDegradationConflictError(
                    f"RT-FAIL-HITL-OPERATOR-BURDEN-DEGRADATION-CONFLICT: "
                    f"degrade=true but degradation_policy carries no "
                    f"degradation_mode (persona_tier="
                    f"{score.persona_tier.value})"
                )
        else:
            degradation_mode = None
            reason = (
                f"cumulative_invocations={score.cumulative_invocations} "
                f"< threshold={degradation_policy.threshold_invocations}"
            )

        # --- span emission per §14.10.3 + sampling discipline ---------------
        if self._should_emit_span(degrade):
            tracer = (
                self._tracer_provider.get_tracer("harness.runtime.operator_burden")
                if self._tracer_provider is not None
                else None
            )
            if tracer is not None:
                with tracer.start_as_current_span("hitl.operator_burden.evaluated") as span:
                    span.set_attribute(
                        ATTR_HITL_OPERATOR_BURDEN_CUMULATIVE_INVOCATIONS,
                        score.cumulative_invocations,
                    )
                    span.set_attribute(
                        ATTR_HITL_OPERATOR_BURDEN_WINDOW_MS,
                        self._burden_window_ms,
                    )
                    span.set_attribute(
                        ATTR_HITL_OPERATOR_BURDEN_PERSONA_TIER,
                        score.persona_tier.value,
                    )
                    span.set_attribute(ATTR_HITL_OPERATOR_BURDEN_DEGRADE, degrade)

        return DegradationDecision(
            degrade=degrade,
            degradation_mode=degradation_mode,
            reason=reason,
        )

    def _should_emit_span(self, degrade: bool) -> bool:
        """Per spec §14.10.3 sampling: head=1.0 on degrade=true; else 0.1."""
        if degrade:
            return True
        return self._rng.random() < 0.1


# --- factory ---------------------------------------------------------------


def materialize_operator_burden_evaluator_stage(
    *,
    tracer_provider: Any = None,
) -> OperatorBurdenEvaluator:
    """Stage 5 LOOP_INIT factory for the operator-burden evaluator."""
    return OperatorBurdenEvaluator(tracer_provider=tracer_provider)
