"""Tests for U-AS-17 — sandbox span hierarchy + event kinds + exclusions (C-AS-15 §15.1,§15.5)."""

from __future__ import annotations

import datetime

from harness_as.sandbox_span_schema import (
    SANDBOX_ENTER_ATTRIBUTES,
    SANDBOX_EXIT_ATTRIBUTES,
    SANDBOX_TIER_ESCALATION_ATTRIBUTES,
    SANDBOX_VIOLATION_ATTRIBUTES,
    SENSITIVE_DATA_EXCLUSIONS,
    SandboxSpanEvent,
    SpanEventKind,
    emit_sandbox_event,
    validate_span_attributes_against_exclusions,
)

_TS = datetime.datetime(2026, 5, 16, tzinfo=datetime.UTC)


def test_span_event_kind_cardinality_five() -> None:
    """Acceptance #1 — SpanEventKind declares 5 kinds."""
    assert len(SpanEventKind) == 5


def test_sandbox_enter_attributes_set_per_spec_15_1() -> None:
    """Acceptance #3 — sandbox.enter carries 10 attributes."""
    assert len(SANDBOX_ENTER_ATTRIBUTES) == 10
    assert "sandbox.tier" in SANDBOX_ENTER_ATTRIBUTES


def test_sandbox_violation_attributes_include_fail_class() -> None:
    """Acceptance #3 — sandbox.violation carries sandbox.fail.class."""
    assert "sandbox.fail.class" in SANDBOX_VIOLATION_ATTRIBUTES


def test_sandbox_violation_attributes_includes_mcp_fail_class() -> None:
    """AS plan v1.4 §2 AC #9 — sandbox.violation carries mcp.fail.class (§15.9)."""
    assert "mcp.fail.class" in SANDBOX_VIOLATION_ATTRIBUTES


def test_sandbox_violation_attributes_cardinality_two() -> None:
    """AS plan v1.4 §2 AC #3 (text-replace) — sandbox.violation carries 2 attribute names.

    Per AS spec v1.6 §15.9 dual-attribute emission. Either MAY be
    omitted-not-null on a given emission per §15.9 5-row matrix; both
    names ride the canonical schema.
    """
    assert len(SANDBOX_VIOLATION_ATTRIBUTES) == 2


def test_sandbox_tier_escalation_attributes_per_spec() -> None:
    """Acceptance #3 — sandbox.tier_escalation carries 3 attributes."""
    assert SANDBOX_TIER_ESCALATION_ATTRIBUTES == frozenset(
        {"from_tier", "to_tier", "escalation_cause"}
    )


def test_sandbox_exit_attributes_per_spec() -> None:
    """Acceptance #3 — sandbox.exit carries 5 attributes."""
    assert len(SANDBOX_EXIT_ATTRIBUTES) == 5


def test_sensitive_data_exclusions_cardinality_four() -> None:
    """Acceptance #4 — SENSITIVE_DATA_EXCLUSIONS contains four entries."""
    assert len(SENSITIVE_DATA_EXCLUSIONS) == 4


def test_validate_rejects_sandbox_resident_filesystem_state() -> None:
    """Acceptance #5 — a span carrying sandbox-resident filesystem state is rejected."""
    result = validate_span_attributes_against_exclusions(
        {"sandbox.tier", "sandbox_resident_filesystem_state"}
    )
    assert result.valid is False
    assert "sandbox_resident_filesystem_state" in result.excluded_present


def test_validate_rejects_screenshot_context() -> None:
    """Acceptance #5 — a span carrying screenshot context is rejected."""
    result = validate_span_attributes_against_exclusions({"sandbox_resident_screenshot_context"})
    assert result.valid is False


def test_validate_rejects_tool_io_raw_content() -> None:
    """Acceptance #6 — a span carrying raw tool I/O content is rejected."""
    result = validate_span_attributes_against_exclusions({"tool_io_raw_content"})
    assert result.valid is False


def test_validate_rejects_secret_value() -> None:
    """Acceptance #6 — a span carrying a secret value is rejected."""
    result = validate_span_attributes_against_exclusions({"secret_value"})
    assert result.valid is False


def test_validate_accepts_structure_only_attributes() -> None:
    """Acceptance #6 — a span carrying only structure attributes is valid."""
    result = validate_span_attributes_against_exclusions(
        {"sandbox.tier", "sandbox.tech", "deployment_surface"}
    )
    assert result.valid is True
    assert result.excluded_present == ()


def test_parent_span_linkage_per_spec_hierarchy() -> None:
    """Acceptance #8 — a sandbox event carries a parent span id; emission honors §15.5."""
    event = SandboxSpanEvent(
        kind=SpanEventKind.SANDBOX_ENTER,
        parent_span_id="subagent.span.0",
        attributes={"sandbox.tier": "tier-1-process"},
        timestamp=_TS,
    )
    assert event.parent_span_id == "subagent.span.0"
    assert emit_sandbox_event(event).emitted is True
    leaky = SandboxSpanEvent(
        kind=SpanEventKind.SANDBOX_VIOLATION,
        parent_span_id="sandbox.enter.0",
        attributes={"secret_value": "leak"},
        timestamp=_TS,
    )
    assert emit_sandbox_event(leaky).emitted is False
