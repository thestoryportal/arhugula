"""Anthropic-primitive sampling discipline + audit-floor commitments — U-AS-32.

Implements C-AS-14 §14.8 (per-namespace sampling discipline + audit-floor
commitments), §14.9 (D6 sampling-discipline forward-reference). Declares
`AnthropicPrimitiveSamplingPosture`, `ANTHROPIC_PRIMITIVE_SAMPLING_POLICY`,
`AuditFloorScope`, `AUDIT_FLOOR_COMMITMENTS`, and the audit-floor /
D6-alignment functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-32 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §14.8-§14.9 C-AS-14; ADR-D3 v1.2 §1.8.

Depends on: U-AS-18 (the sibling sandbox-event sampling — orthogonal namespace);
U-AS-31 (the six Anthropic-primitive namespaces).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict


class AnthropicPrimitiveSamplingPosture(StrEnum):
    """A per-namespace Anthropic-primitive sampling posture (C-AS-14 §14.8)."""

    HEAD_BASED_DEV_TAIL_BASED_PROD = "HEAD_BASED_DEV_TAIL_BASED_PROD"
    HEAD_1_0_DESIGN_TIME_BASE_RATE_PROD = "HEAD_1_0_DESIGN_TIME_BASE_RATE_PROD"
    HEAD_1_0_WITH_TAIL_KEEP_ON_VIOLATIONS = "HEAD_1_0_WITH_TAIL_KEEP_ON_VIOLATIONS"
    HEAD_1_0_ALWAYS = "HEAD_1_0_ALWAYS"
    HEAD_1_0_AT_MUTATION_BASE_RATE_AT_READ = "HEAD_1_0_AT_MUTATION_BASE_RATE_AT_READ"


#: Per-span-name Anthropic-primitive sampling policy (C-AS-14 §14.8, 6 rows).
ANTHROPIC_PRIMITIVE_SAMPLING_POLICY: Mapping[str, AnthropicPrimitiveSamplingPosture] = (
    MappingProxyType(
        {
            # Alias-term key per AS spec v1.7 §14.1 + §14.8 row 1 — the LLM
            # inference span is owned at OD spec v1.12 §C-OD-04 §4.1 (actual
            # runtime span-name `{operation} {model}`); spec-side cite uses
            # the alias term, not the literal runtime span name.
            "the LLM inference span": (
                AnthropicPrimitiveSamplingPosture.HEAD_BASED_DEV_TAIL_BASED_PROD
            ),
            "skill.activation": (
                AnthropicPrimitiveSamplingPosture.HEAD_1_0_DESIGN_TIME_BASE_RATE_PROD
            ),
            "mcp.tool.call": (
                AnthropicPrimitiveSamplingPosture.HEAD_1_0_WITH_TAIL_KEEP_ON_VIOLATIONS
            ),
            "managed_agents.runtime": AnthropicPrimitiveSamplingPosture.HEAD_1_0_ALWAYS,
            "files.operation": (
                AnthropicPrimitiveSamplingPosture.HEAD_1_0_AT_MUTATION_BASE_RATE_AT_READ
            ),
            "memory.operation": (
                AnthropicPrimitiveSamplingPosture.HEAD_1_0_AT_MUTATION_BASE_RATE_AT_READ
            ),
        }
    )
)


class AuditFloorScope(StrEnum):
    """An audit-floor commitment scope (C-AS-14 §14.8)."""

    MCP_TOOL_CALL_ALWAYS_SAMPLED = "MCP_TOOL_CALL_ALWAYS_SAMPLED"
    FILES_OPERATION_MUTATION_ALWAYS_SAMPLED = "FILES_OPERATION_MUTATION_ALWAYS_SAMPLED"
    MEMORY_OPERATION_MUTATION_ALWAYS_SAMPLED = "MEMORY_OPERATION_MUTATION_ALWAYS_SAMPLED"
    MANAGED_AGENTS_RUNTIME_ALWAYS_SAMPLED = "MANAGED_AGENTS_RUNTIME_ALWAYS_SAMPLED"
    SKILL_ACTIVATION_DESIGN_TIME_ALWAYS_SAMPLED = "SKILL_ACTIVATION_DESIGN_TIME_ALWAYS_SAMPLED"


#: The audit-floor commitments — hard floors at the deployment-binding layer.
AUDIT_FLOOR_COMMITMENTS: frozenset[AuditFloorScope] = frozenset(AuditFloorScope)

# Audit-floor scope → the span name + non-base-rate posture it commits.
_FLOOR_SPAN: dict[AuditFloorScope, str] = {
    AuditFloorScope.MCP_TOOL_CALL_ALWAYS_SAMPLED: "mcp.tool.call",
    AuditFloorScope.FILES_OPERATION_MUTATION_ALWAYS_SAMPLED: "files.operation",
    AuditFloorScope.MEMORY_OPERATION_MUTATION_ALWAYS_SAMPLED: "memory.operation",
    AuditFloorScope.MANAGED_AGENTS_RUNTIME_ALWAYS_SAMPLED: "managed_agents.runtime",
    AuditFloorScope.SKILL_ACTIVATION_DESIGN_TIME_ALWAYS_SAMPLED: "skill.activation",
}
_BASE_RATE_POSTURES: frozenset[AnthropicPrimitiveSamplingPosture] = frozenset(
    {AnthropicPrimitiveSamplingPosture.HEAD_BASED_DEV_TAIL_BASED_PROD}
)


class AlignmentResult(BaseModel):
    """The result of a D6 sampling-discipline alignment check (C-AS-14 §14.9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aligned: bool
    detail: str


def audit_floor_commitment_violated(
    proposed_policy: Mapping[str, AnthropicPrimitiveSamplingPosture],
) -> frozenset[AuditFloorScope]:
    """Return the audit-floor scopes a proposed policy violates (C-AS-14 §14.8).

    A scope is violated when the proposed policy downgrades its committed span
    to a base-rate posture — the audit floors are not operator-tunable at base
    rate (acceptance #2 / #3).
    """
    return frozenset(
        scope
        for scope, span in _FLOOR_SPAN.items()
        if proposed_policy.get(span) in _BASE_RATE_POSTURES
    )


def d6_sampling_discipline_alignment_check(
    d6_proposed_policy: Mapping[str, AnthropicPrimitiveSamplingPosture],
) -> AlignmentResult:
    """Check a D6-proposed policy for §14.9 sampling-discipline alignment.

    The D6 policy must distinguish `mcp.tool.call` (always-sampled) from a
    non-MCP `tool.call` (base-rate) — `mcp.tool.call` must NOT carry a
    base-rate posture (acceptance #6).
    """
    mcp = d6_proposed_policy.get("mcp.tool.call")
    if mcp in _BASE_RATE_POSTURES:
        return AlignmentResult(
            aligned=False,
            detail="mcp.tool.call downgraded to base-rate; not distinguished "
            "from non-MCP tool.call",
        )
    return AlignmentResult(
        aligned=True, detail="mcp.tool.call distinguished from non-MCP tool.call"
    )
