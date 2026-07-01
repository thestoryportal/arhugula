"""Tests for U-AS-18 — sandbox-event sampling discipline (C-AS-15 §15.4)."""

from __future__ import annotations

from harness_as.sandbox_event_sampling import (
    SAMPLING_POLICY,
    SamplingPosture,
    audit_floor_violated,
    is_operator_tunable_at_base_rate,
    sampling_posture,
)
from harness_as.sandbox_span_schema import SpanEventKind


def test_sampling_policy_cardinality_four() -> None:
    """Acceptance #1 — SAMPLING_POLICY declares the 4 §15.4 entries."""
    assert len(SAMPLING_POLICY) == 4


def test_sandbox_enter_base_rate_matches_parent() -> None:
    """Acceptance #1 — sandbox.enter is base-rate-matches-parent."""
    assert sampling_posture(SpanEventKind.SANDBOX_ENTER) is SamplingPosture.BASE_RATE_MATCHES_PARENT


def test_sandbox_exit_base_rate_matches_parent() -> None:
    """Acceptance #1 — sandbox.exit is base-rate-matches-parent."""
    assert sampling_posture(SpanEventKind.SANDBOX_EXIT) is SamplingPosture.BASE_RATE_MATCHES_PARENT


def test_sandbox_violation_always_sampled_with_tail_keep() -> None:
    """Acceptance #1/#6 — sandbox.violation is always-sampled-with-tail-keep."""
    assert (
        sampling_posture(SpanEventKind.SANDBOX_VIOLATION)
        is SamplingPosture.ALWAYS_SAMPLED_WITH_TAIL_KEEP
    )


def test_sandbox_tier_escalation_always_sampled_head_1_0() -> None:
    """Acceptance #1 — sandbox.tier_escalation is always-sampled-head-1.0."""
    assert (
        sampling_posture(SpanEventKind.SANDBOX_TIER_ESCALATION)
        is SamplingPosture.ALWAYS_SAMPLED_HEAD_1_0
    )


def test_is_operator_tunable_at_base_rate_returns_true_for_enter_exit() -> None:
    """Acceptance #2 — enter/exit are operator-tunable at base rate."""
    assert is_operator_tunable_at_base_rate(SpanEventKind.SANDBOX_ENTER)
    assert is_operator_tunable_at_base_rate(SpanEventKind.SANDBOX_EXIT)


def test_is_operator_tunable_at_base_rate_returns_false_for_violation_escalation() -> None:
    """Acceptance #2 — violation/tier_escalation are an untunable hard floor."""
    assert not is_operator_tunable_at_base_rate(SpanEventKind.SANDBOX_VIOLATION)
    assert not is_operator_tunable_at_base_rate(SpanEventKind.SANDBOX_TIER_ESCALATION)


def test_audit_floor_violated_detects_downgrade_attempt() -> None:
    """Acceptance #3 — downgrading violation to base-rate violates the audit floor."""
    proposed = {
        SpanEventKind.SANDBOX_VIOLATION: SamplingPosture.BASE_RATE_MATCHES_PARENT,
    }
    assert audit_floor_violated(proposed) is True


def test_audit_floor_violated_returns_false_for_compliant_policy() -> None:
    """Acceptance #3 — the canonical §15.4 policy does not violate the audit floor."""
    assert audit_floor_violated(SAMPLING_POLICY) is False
