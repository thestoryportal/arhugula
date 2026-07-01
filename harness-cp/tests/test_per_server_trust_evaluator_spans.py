"""Tests for U-CP-70 — mcp.trust.evaluate span emission + head sampling
discipline per CP spec v1.10 §27.4.

ACs from CP plan v2.15 §1 U-CP-70 (preserved at v2.17):
  AC #1 mcp.trust.evaluate span emits with 5 attributes per OD spec v1.8
        §C-OD-31.1
  AC #2 Sampling: head=1.0 when audit_required=true; else head=0.1
  AC #3 UNKNOWN_SERVER decisions always emit (audit_required=true forces
        head=1.0)
  AC #4 Span attribute names match OD canonical schema byte-exact
  AC #5 Integration test: 5 evaluations × 6 decision reasons covered

Soft-dep on U-OD-52 per Phase D iteration-1 F1-03 absorption — runtime emits
attribute-name string literals; OD schema module NOT imported at runtime.
Byte-exact alignment verified via string-literal comparison.
"""

from __future__ import annotations

import random

import pytest
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.per_server_trust_evaluator import (
    ATTR_MCP_TRUST_AUDIT_REQUIRED,
    ATTR_MCP_TRUST_DECISION_REASON,
    ATTR_MCP_TRUST_PRIMITIVE_KIND,
    ATTR_MCP_TRUST_SERVER_NAME,
    ATTR_MCP_TRUST_TIER_EVALUATED,
    MCP_TRUST_EVALUATE_SPAN_NAME,
    PerServerTrustEvaluator,
    emit_mcp_trust_evaluate_span,
)
from harness_cp.per_server_trust_types import (
    MCPPrimitive,
    TierDerivationRule,
    TrustDecisionReason,
    TrustEvaluation,
    TrustPolicy,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture
def exporter_and_tracer() -> tuple[InMemorySpanExporter, object]:
    """Per-test isolated TracerProvider + in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("u-cp-70-test")
    return exporter, tracer


def _exported_attrs(exporter: InMemorySpanExporter) -> dict[str, object]:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert isinstance(span, ReadableSpan)
    return dict(span.attributes or {})


def _eval(
    *,
    permitted: bool,
    reason: TrustDecisionReason,
    tier: MCPTrustTier = MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    audit: bool = True,
) -> TrustEvaluation:
    return TrustEvaluation(
        permitted=permitted,
        trust_tier_evaluated=tier,
        decision_reason=reason,
        audit_required=audit,
    )


# ---------------------------------------------------------------------------
# AC #1 + AC #4 — 5-attribute span emission + byte-exact attribute names
# ---------------------------------------------------------------------------


def test_audit_required_emits_5_attributes(
    exporter_and_tracer: tuple[InMemorySpanExporter, object],
) -> None:
    """AC #1 + AC #4 — span carries all 5 mcp.trust.* attributes byte-exact."""
    exporter, tracer = exporter_and_tracer
    eval_ = _eval(
        permitted=True,
        reason=TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS,
        tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        audit=True,
    )
    result = emit_mcp_trust_evaluate_span(tracer, eval_, "srv-a", MCPPrimitive.TOOL)
    assert result is not None
    attrs = _exported_attrs(exporter)
    assert attrs == {
        ATTR_MCP_TRUST_SERVER_NAME: "srv-a",
        ATTR_MCP_TRUST_PRIMITIVE_KIND: "tool",
        ATTR_MCP_TRUST_DECISION_REASON: "unknown_server_tier_floor_pass",
        ATTR_MCP_TRUST_AUDIT_REQUIRED: True,
        ATTR_MCP_TRUST_TIER_EVALUATED: "level-3-allow-with-audit",
    }


def test_attribute_names_byte_exact_per_od_spec_31_1() -> None:
    """AC #4 — module-level constants match OD spec v1.9 §C-OD-31.1 verbatim."""
    assert ATTR_MCP_TRUST_SERVER_NAME == "mcp.trust.server_name"
    assert ATTR_MCP_TRUST_PRIMITIVE_KIND == "mcp.trust.primitive_kind"
    assert ATTR_MCP_TRUST_DECISION_REASON == "mcp.trust.decision_reason"
    assert ATTR_MCP_TRUST_AUDIT_REQUIRED == "mcp.trust.audit_required"
    assert ATTR_MCP_TRUST_TIER_EVALUATED == "mcp.trust.tier_evaluated"


def test_span_name_byte_exact_per_cp_spec_27_4() -> None:
    """AC #1 — span name byte-exact per CP spec v1.10 §27.4."""
    assert MCP_TRUST_EVALUATE_SPAN_NAME == "mcp.trust.evaluate"


# ---------------------------------------------------------------------------
# AC #2 — Sampling: head=1.0 when audit_required; head=0.1 otherwise
# ---------------------------------------------------------------------------


def test_audit_required_always_emits(
    exporter_and_tracer: tuple[InMemorySpanExporter, object],
) -> None:
    """AC #2 — audit_required=True → head=1.0 (deterministic always-emit)."""
    exporter, tracer = exporter_and_tracer
    eval_ = _eval(
        permitted=False,
        reason=TrustDecisionReason.EXPLICIT_DENY,
        audit=True,
    )
    # Use a Random that would sample out non-audit calls.
    rng = random.Random()
    rng.seed(42)  # any seed — the audit branch doesn't consult rng
    result = emit_mcp_trust_evaluate_span(tracer, eval_, "srv", MCPPrimitive.TOOL, rng=rng)
    assert result is not None
    assert len(exporter.get_finished_spans()) == 1


def test_non_audit_emit_when_rng_below_threshold(
    exporter_and_tracer: tuple[InMemorySpanExporter, object],
) -> None:
    """AC #2 — non-audit + rng.random() < 0.1 → emit."""
    exporter, tracer = exporter_and_tracer

    class _FixedRng:
        """rng that returns a value < 0.1 to force emit."""

        def random(self) -> float:
            return 0.05

    eval_ = _eval(
        permitted=True,
        reason=TrustDecisionReason.TIER_FLOOR_PASS,
        audit=False,
    )
    result = emit_mcp_trust_evaluate_span(
        tracer,
        eval_,
        "srv",
        MCPPrimitive.TOOL,
        rng=_FixedRng(),  # type: ignore[arg-type]
    )
    assert result is not None
    assert len(exporter.get_finished_spans()) == 1


def test_non_audit_skip_when_rng_above_threshold(
    exporter_and_tracer: tuple[InMemorySpanExporter, object],
) -> None:
    """AC #2 — non-audit + rng.random() >= 0.1 → skip (sampled out)."""
    exporter, tracer = exporter_and_tracer

    class _FixedRng:
        """rng that returns a value >= 0.1 to force sample-out."""

        def random(self) -> float:
            return 0.5

    eval_ = _eval(
        permitted=True,
        reason=TrustDecisionReason.TIER_FLOOR_PASS,
        audit=False,
    )
    result = emit_mcp_trust_evaluate_span(
        tracer,
        eval_,
        "srv",
        MCPPrimitive.TOOL,
        rng=_FixedRng(),  # type: ignore[arg-type]
    )
    assert result is None
    assert exporter.get_finished_spans() == ()


# ---------------------------------------------------------------------------
# AC #3 — UNKNOWN_SERVER decisions always emit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS,
        TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_VIOLATION,
    ],
)
def test_unknown_server_decisions_always_emit(
    reason: TrustDecisionReason,
    exporter_and_tracer: tuple[InMemorySpanExporter, object],
) -> None:
    """AC #3 — both UNKNOWN_SERVER_* variants always emit (audit_required=True
    forces head=1.0 per §27.6 inv 4)."""
    exporter, tracer = exporter_and_tracer
    eval_ = _eval(
        permitted=(reason == TrustDecisionReason.UNKNOWN_SERVER_TIER_FLOOR_PASS),
        reason=reason,
        audit=True,
    )

    class _AlwaysSampleOutRng:
        def random(self) -> float:
            return 0.99

    result = emit_mcp_trust_evaluate_span(
        tracer,
        eval_,
        "novel-srv",
        MCPPrimitive.TOOL,
        rng=_AlwaysSampleOutRng(),  # type: ignore[arg-type]
    )
    assert result is not None
    assert len(exporter.get_finished_spans()) == 1


# ---------------------------------------------------------------------------
# AC #5 — Integration test: 5 evaluations × 6 decision reasons covered end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_all_six_decision_reasons_emit_via_evaluator(
    exporter_and_tracer: tuple[InMemorySpanExporter, object],
) -> None:
    """AC #5 — drive PerServerTrustEvaluator end-to-end across the 6 decision
    reasons + verify span emission per evaluation."""
    exporter, tracer = exporter_and_tracer

    def _high_tier_resolver(s: str, c: object | None, r: TierDerivationRule) -> MCPTrustTier:
        return MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT

    def _low_tier_resolver(s: str, c: object | None, r: TierDerivationRule) -> MCPTrustTier:
        return MCPTrustTier.LEVEL_0_REFUSE_REMOTE

    class _ForceEmitRng:
        """rng that always emits even on non-audit branches."""

        def random(self) -> float:
            return 0.0

    rng = _ForceEmitRng()

    # 1. EXPLICIT_DENY
    pol_deny = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset({"bad"}),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    eval1 = await PerServerTrustEvaluator().evaluate("bad", MCPPrimitive.TOOL, None, pol_deny)
    emit_mcp_trust_evaluate_span(
        tracer,
        eval1,
        "bad",
        MCPPrimitive.TOOL,
        rng=rng,  # type: ignore[arg-type]
    )

    # 2. EXPLICIT_ALLOW
    pol_allow = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={"good": MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT},
        allow_list=frozenset({"good"}),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    eval2 = await PerServerTrustEvaluator().evaluate("good", MCPPrimitive.RESOURCE, None, pol_allow)
    emit_mcp_trust_evaluate_span(
        tracer,
        eval2,
        "good",
        MCPPrimitive.RESOURCE,
        rng=rng,  # type: ignore[arg-type]
    )

    # 3. TIER_FLOOR_PASS
    pol_pass = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={"mid": MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    eval3 = await PerServerTrustEvaluator().evaluate("mid", MCPPrimitive.PROMPT, None, pol_pass)
    emit_mcp_trust_evaluate_span(
        tracer,
        eval3,
        "mid",
        MCPPrimitive.PROMPT,
        rng=rng,  # type: ignore[arg-type]
    )

    # 4. TIER_FLOOR_VIOLATION
    pol_viol = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        per_server_overrides={"low": MCPTrustTier.LEVEL_0_REFUSE_REMOTE},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )
    eval4 = await PerServerTrustEvaluator().evaluate("low", MCPPrimitive.SAMPLING, None, pol_viol)
    emit_mcp_trust_evaluate_span(
        tracer,
        eval4,
        "low",
        MCPPrimitive.SAMPLING,
        rng=rng,  # type: ignore[arg-type]
    )

    # 5. UNKNOWN_SERVER_TIER_FLOOR_PASS
    pol_unk_pass = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        tier_derivation=TierDerivationRule.OPERATOR_HOOK,
    )
    eval5 = await PerServerTrustEvaluator(tier_resolver=_high_tier_resolver).evaluate(
        "novel-high", MCPPrimitive.TOOL, None, pol_unk_pass
    )
    emit_mcp_trust_evaluate_span(
        tracer,
        eval5,
        "novel-high",
        MCPPrimitive.TOOL,
        rng=rng,  # type: ignore[arg-type]
    )

    # 6. UNKNOWN_SERVER_TIER_FLOOR_VIOLATION
    pol_unk_viol = TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        per_server_overrides={},
        allow_list=frozenset(),
        deny_list=frozenset(),
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        tier_derivation=TierDerivationRule.OPERATOR_HOOK,
    )
    eval6 = await PerServerTrustEvaluator(tier_resolver=_low_tier_resolver).evaluate(
        "novel-low", MCPPrimitive.RESOURCE, None, pol_unk_viol
    )
    emit_mcp_trust_evaluate_span(
        tracer,
        eval6,
        "novel-low",
        MCPPrimitive.RESOURCE,
        rng=rng,  # type: ignore[arg-type]
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 6
    reasons_emitted = {dict(s.attributes or {}).get(ATTR_MCP_TRUST_DECISION_REASON) for s in spans}
    assert reasons_emitted == {
        "explicit_deny",
        "explicit_allow",
        "tier_floor_pass",
        "tier_floor_violation",
        "unknown_server_tier_floor_pass",
        "unknown_server_tier_floor_violation",
    }
