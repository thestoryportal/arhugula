"""U-RT-70 — `OperatorBurdenEvaluator` + carriers.

Per `Implementation_Plan_Harness_Runtime_v2_11.md` §1 U-RT-70 (5 ACs).
"""

from __future__ import annotations

import random

import pytest
from harness_core.persona_tier import PersonaTier
from harness_runtime.lifecycle.operator_burden_evaluator import (
    ATTR_HITL_OPERATOR_BURDEN_CUMULATIVE_INVOCATIONS,
    ATTR_HITL_OPERATOR_BURDEN_DEGRADE,
    ATTR_HITL_OPERATOR_BURDEN_PERSONA_TIER,
    ATTR_HITL_OPERATOR_BURDEN_WINDOW_MS,
    DegradationDecision,
    DegradationPolicy,
    OperatorBurdenEvaluator,
    OperatorBurdenScore,
    SpanWindow,
    materialize_operator_burden_evaluator_stage,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------- helpers -------------------------------------------------------


def _make_window(start: int = 1_000_000, end: int = 1_003_600_000) -> SpanWindow:
    return SpanWindow(start=start, end=end)


def _make_policy(threshold: int = 5, mode: str = "auto_reject") -> DegradationPolicy:
    return DegradationPolicy(
        threshold_invocations=threshold,
        degradation_mode=mode,  # type: ignore[arg-type]
    )


# ---------- AC #1 — compute_operator_burden -------------------------------


@pytest.mark.asyncio
async def test_compute_operator_burden_aggregates_via_counter() -> None:
    def counter(window, persona):
        assert window.start == 1_000_000
        assert persona is PersonaTier.SOLO_DEVELOPER
        return 7

    evaluator = OperatorBurdenEvaluator(burden_span_counter=counter)
    score = await evaluator.compute_operator_burden(_make_window(), PersonaTier.SOLO_DEVELOPER)
    assert isinstance(score, OperatorBurdenScore)
    assert score.cumulative_invocations == 7
    assert score.persona_tier is PersonaTier.SOLO_DEVELOPER
    assert score.window_start == 1_000_000


@pytest.mark.asyncio
async def test_default_counter_raises_on_misconfig() -> None:
    evaluator = OperatorBurdenEvaluator()  # default counter
    with pytest.raises(LookupError, match="default BurdenSpanCounter"):
        await evaluator.compute_operator_burden(_make_window(), PersonaTier.TEAM_BINDING)


# ---------- AC #2 — should_degrade ----------------------------------------


@pytest.mark.asyncio
async def test_should_degrade_returns_true_above_threshold() -> None:
    evaluator = OperatorBurdenEvaluator()
    score = OperatorBurdenScore(
        cumulative_invocations=10,
        window_start=0,
        window_end=3_600_000,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    decision = await evaluator.should_degrade(score, _make_policy(threshold=5))
    assert isinstance(decision, DegradationDecision)
    assert decision.degrade is True
    assert decision.degradation_mode == "auto_reject"
    assert "cumulative_invocations=10" in decision.reason
    assert ">= threshold=5" in decision.reason


@pytest.mark.asyncio
async def test_should_degrade_returns_false_below_threshold() -> None:
    evaluator = OperatorBurdenEvaluator()
    score = OperatorBurdenScore(
        cumulative_invocations=3,
        window_start=0,
        window_end=3_600_000,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    decision = await evaluator.should_degrade(score, _make_policy(threshold=5))
    assert decision.degrade is False
    assert decision.degradation_mode is None
    assert "< threshold=5" in decision.reason


# ---------- AC #3 — span emission with sampling -----------------------------


@pytest.mark.asyncio
async def test_should_degrade_emits_span_when_degrade_true() -> None:
    """AC #3 + AC #4: degrade=true → head=1.0 sampling → span ALWAYS emits."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Use a seeded rng that would return < 0.1 — but degrade=true overrides.
    evaluator = OperatorBurdenEvaluator(
        tracer_provider=provider,
        rng=random.Random(42),
    )
    score = OperatorBurdenScore(
        cumulative_invocations=20,
        window_start=0,
        window_end=3_600_000,
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
    )
    await evaluator.should_degrade(score, _make_policy(threshold=5))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "hitl.operator_burden.evaluated"
    attrs = dict(span.attributes or {})
    assert attrs[ATTR_HITL_OPERATOR_BURDEN_CUMULATIVE_INVOCATIONS] == 20
    assert attrs[ATTR_HITL_OPERATOR_BURDEN_WINDOW_MS] == 3_600_000
    assert attrs[ATTR_HITL_OPERATOR_BURDEN_PERSONA_TIER] == "multi-tenant-compliance"
    assert attrs[ATTR_HITL_OPERATOR_BURDEN_DEGRADE] is True


@pytest.mark.asyncio
async def test_should_degrade_skips_span_below_sampling_floor() -> None:
    """AC #4 sampling: degrade=false + rng > 0.1 → span SKIPPED."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class _FixedHighRng:
        def random(self):  # pragma: no cover -- inlined
            return 0.9

    evaluator = OperatorBurdenEvaluator(
        tracer_provider=provider,
        rng=_FixedHighRng(),  # type: ignore[arg-type]
    )
    score = OperatorBurdenScore(
        cumulative_invocations=2,
        window_start=0,
        window_end=3_600_000,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    decision = await evaluator.should_degrade(score, _make_policy(threshold=5))
    assert decision.degrade is False
    spans = exporter.get_finished_spans()
    assert len(spans) == 0  # below 0.1 sampling floor → no emission


@pytest.mark.asyncio
async def test_should_degrade_emits_span_below_threshold_at_low_rng() -> None:
    """AC #4 sampling: degrade=false + rng < 0.1 → span IS emitted."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class _FixedLowRng:
        def random(self):  # pragma: no cover -- inlined
            return 0.05

    evaluator = OperatorBurdenEvaluator(
        tracer_provider=provider,
        rng=_FixedLowRng(),  # type: ignore[arg-type]
    )
    score = OperatorBurdenScore(
        cumulative_invocations=2,
        window_start=0,
        window_end=3_600_000,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    await evaluator.should_degrade(score, _make_policy(threshold=5))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1


# ---------- AC #5 — burden window default + override ------------------------


def test_burden_window_default_is_one_hour() -> None:
    evaluator = OperatorBurdenEvaluator()
    assert evaluator._burden_window_ms == 3_600_000


def test_burden_window_override_via_constructor() -> None:
    evaluator = OperatorBurdenEvaluator(burden_window_ms=600_000)  # 10 min
    assert evaluator._burden_window_ms == 600_000


# ---------- factory --------------------------------------------------------


def test_factory_returns_evaluator() -> None:
    provider = TracerProvider()
    evaluator = materialize_operator_burden_evaluator_stage(tracer_provider=provider)
    assert isinstance(evaluator, OperatorBurdenEvaluator)


# ---------- carriers --------------------------------------------------------


def test_span_window_frozen() -> None:
    w = SpanWindow(start=1, end=2)
    with pytest.raises(Exception):
        w.start = 3  # type: ignore[misc]


def test_operator_burden_score_frozen() -> None:
    s = OperatorBurdenScore(
        cumulative_invocations=1,
        window_start=0,
        window_end=1,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    with pytest.raises(Exception):
        s.cumulative_invocations = 2  # type: ignore[misc]


def test_degradation_decision_frozen() -> None:
    d = DegradationDecision(degrade=True, degradation_mode="auto_approve", reason="x")
    with pytest.raises(Exception):
        d.degrade = False  # type: ignore[misc]
