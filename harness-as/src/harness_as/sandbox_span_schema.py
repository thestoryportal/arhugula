"""Sandbox span hierarchy + event kinds + sensitive-data discipline — U-AS-17.

Implements C-AS-15 §15.1 (span hierarchy + five span event kinds), §15.5
(sensitive-data discipline). Declares `SpanEventKind`, `SandboxSpanEvent`, the
per-event-kind attribute-set constants, `SENSITIVE_DATA_EXCLUSIONS`, and the
`emit_sandbox_event` / `validate_span_attributes_against_exclusions` functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-17 (R3-revised body
canonical per Implementation_Plan_Action_Surface_v1_1.md §5.3);
Spec_Action_Surface_v1.md §15.1 + §15.5 C-AS-15; ADR-D2 v1.2 §1.7.

Depends on: U-AS-01, U-AS-03, U-AS-08, U-AS-16. GUARDRAIL unit
(Plan_Executability_Audit_v1.md §3.2) — the actual OTel `SpanProcessor` wiring
that drops `SENSITIVE_DATA_EXCLUSIONS` attributes is project-authored against
`opentelemetry.sdk.trace.SpanProcessor` and is downstream of this unit;
`emit_sandbox_event` here validates the event and returns a structured result.

Referenced-but-unenumerated types resolved minimally: `SpanId` → `str`;
`AttributeValue` → `str | int | float | bool`; `MonotonicTimestamp` →
`datetime`; `EmissionResult` / `ValidationResult` → small frozen results.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

type AttributeValue = str | int | float | bool


class SpanEventKind(StrEnum):
    """The 5 sandbox span event kinds (C-AS-15 §15.1)."""

    SANDBOX_ENTER = "sandbox_enter"
    TOOL_CALL = "tool_call"
    SANDBOX_VIOLATION = "sandbox_violation"
    SANDBOX_TIER_ESCALATION = "sandbox_tier_escalation"
    SANDBOX_EXIT = "sandbox_exit"


class SandboxSpanEvent(BaseModel):
    """A sandbox-bounded span event (C-AS-15 §15.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SpanEventKind
    parent_span_id: str
    attributes: Mapping[str, AttributeValue]
    timestamp: datetime.datetime


class EmissionResult(BaseModel):
    """The result of an `emit_sandbox_event` call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    emitted: bool
    rejected_attributes: tuple[str, ...]


class SpanValidationResult(BaseModel):
    """The result of validating span attributes against the exclusion set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    excluded_present: tuple[str, ...]


#: §15.1 — per-event-kind attribute sets.
SANDBOX_ENTER_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "sandbox.tier",
        "sandbox.tech",
        "sandbox.provider",
        "sandbox.policy.assigned_tier_reason",
        "deployment_surface",
        "blast_radius_tier",
        "mcp_transport",
        "cold_start_ms",
        "pool_acquired",
        "persona_tier",
    }
)
SANDBOX_VIOLATION_ATTRIBUTES: frozenset[str] = frozenset({"sandbox.fail.class", "mcp.fail.class"})
"""Per AS spec v1.6 §15.9 dual-attribute emission. `sandbox.violation`
carries BOTH `sandbox.fail.class` (F4 process-shape per §4.1) AND
`mcp.fail.class` (MCP-shape per §15.8). Either MAY be omitted-not-null
on a given emission per §15.9 5-row scenario matrix; both names ride
the canonical schema."""
SANDBOX_TIER_ESCALATION_ATTRIBUTES: frozenset[str] = frozenset(
    {"from_tier", "to_tier", "escalation_cause"}
)
SANDBOX_EXIT_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "sandbox.tier",
        "sandbox.tech",
        "sandbox.cost.tier_overhead_ms",
        "sandbox.cost.tier_overhead_usd",
        "pool_returned",
    }
)

#: §15.5 — span attributes structurally excluded (sensitive-data discipline).
SENSITIVE_DATA_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "sandbox_resident_filesystem_state",
        "sandbox_resident_screenshot_context",
        "tool_io_raw_content",
        "secret_value",
    }
)


def validate_span_attributes_against_exclusions(
    attrs: frozenset[str] | set[str] | Mapping[str, AttributeValue],
) -> SpanValidationResult:
    """Validate span attribute names against `SENSITIVE_DATA_EXCLUSIONS` (§15.5).

    A span carrying any exclusion-set attribute is invalid — structure-not-content
    discipline (§15.5 rows 1-2): span attributes never carry raw tool I/O,
    sandbox-resident filesystem state, screenshot context, or secret values.
    """
    present = tuple(sorted(set(attrs) & SENSITIVE_DATA_EXCLUSIONS))
    return SpanValidationResult(valid=not present, excluded_present=present)


def emit_sandbox_event(event: SandboxSpanEvent) -> EmissionResult:
    """Emit a sandbox span event after the §15.5 sensitive-data check.

    An event carrying an exclusion-set attribute is **not** emitted — its
    excluded attributes are reported on the result. The actual OTel
    `SpanProcessor` delivery is the project-authored GUARDRAIL downstream of
    this unit.
    """
    validation = validate_span_attributes_against_exclusions(event.attributes)
    return EmissionResult(
        emitted=validation.valid,
        rejected_attributes=validation.excluded_present,
    )
