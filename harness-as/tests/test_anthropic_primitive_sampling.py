"""Tests for U-AS-32 — Anthropic-primitive sampling discipline (C-AS-14 §14.8-§14.9)."""

from __future__ import annotations

from harness_as.anthropic_primitive_sampling import (
    ANTHROPIC_PRIMITIVE_SAMPLING_POLICY,
    AUDIT_FLOOR_COMMITMENTS,
    AnthropicPrimitiveSamplingPosture,
    AuditFloorScope,
    audit_floor_commitment_violated,
    d6_sampling_discipline_alignment_check,
)


def test_anthropic_primitive_sampling_policy_six_rows_per_spec_14_8() -> None:
    """Acceptance #1 — the sampling policy declares 6 rows."""
    assert len(ANTHROPIC_PRIMITIVE_SAMPLING_POLICY) == 6
    assert (
        ANTHROPIC_PRIMITIVE_SAMPLING_POLICY["managed_agents.runtime"]
        is AnthropicPrimitiveSamplingPosture.HEAD_1_0_ALWAYS
    )


def test_audit_floor_commitments_cardinality_five() -> None:
    """Acceptance #2 — AUDIT_FLOOR_COMMITMENTS declares 5 scopes."""
    assert len(AUDIT_FLOOR_COMMITMENTS) == 5


def test_audit_floor_commitment_violated_detects_mcp_tool_call_downgrade() -> None:
    """Acceptance #3 — downgrading mcp.tool.call to base-rate is a violation."""
    proposed = {
        "mcp.tool.call": AnthropicPrimitiveSamplingPosture.HEAD_BASED_DEV_TAIL_BASED_PROD,
    }
    violated = audit_floor_commitment_violated(proposed)
    assert AuditFloorScope.MCP_TOOL_CALL_ALWAYS_SAMPLED in violated


def test_audit_floor_commitment_violated_returns_empty_for_compliant_policy() -> None:
    """Acceptance #3 — the canonical §14.8 policy violates no audit floor."""
    assert audit_floor_commitment_violated(ANTHROPIC_PRIMITIVE_SAMPLING_POLICY) == frozenset()


def test_files_operation_sampling_distinguishes_mutation_from_read() -> None:
    """Acceptance #4 — files.operation sampling is mutation/read-distinguishing."""
    assert (
        ANTHROPIC_PRIMITIVE_SAMPLING_POLICY["files.operation"]
        is AnthropicPrimitiveSamplingPosture.HEAD_1_0_AT_MUTATION_BASE_RATE_AT_READ
    )


def test_memory_operation_sampling_distinguishes_mutation_from_read() -> None:
    """Acceptance #4 — memory.operation sampling is mutation/read-distinguishing."""
    assert (
        ANTHROPIC_PRIMITIVE_SAMPLING_POLICY["memory.operation"]
        is AnthropicPrimitiveSamplingPosture.HEAD_1_0_AT_MUTATION_BASE_RATE_AT_READ
    )


def test_mcp_tool_call_tail_keep_on_trust_tier_violation() -> None:
    """Acceptance #5 — mcp.tool.call is head=1.0 with tail-keep on violations."""
    assert (
        ANTHROPIC_PRIMITIVE_SAMPLING_POLICY["mcp.tool.call"]
        is AnthropicPrimitiveSamplingPosture.HEAD_1_0_WITH_TAIL_KEEP_ON_VIOLATIONS
    )


def test_d6_sampling_alignment_check_distinguishes_mcp_from_non_mcp_tool_call() -> None:
    """Acceptance #6 — the alignment check rejects an mcp.tool.call base-rate downgrade."""
    proposed = {
        "mcp.tool.call": AnthropicPrimitiveSamplingPosture.HEAD_BASED_DEV_TAIL_BASED_PROD,
    }
    assert d6_sampling_discipline_alignment_check(proposed).aligned is False


def test_d6_sampling_alignment_check_passes_compliant_d6_policy() -> None:
    """Acceptance #6 — the canonical policy passes the D6 alignment check."""
    assert (
        d6_sampling_discipline_alignment_check(ANTHROPIC_PRIMITIVE_SAMPLING_POLICY).aligned is True
    )
