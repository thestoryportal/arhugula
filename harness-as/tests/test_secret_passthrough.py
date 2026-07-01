"""Tests for U-AS-23 — secret-passthrough constraint enforcement (C-AS-06 §6.3)."""

from __future__ import annotations

from harness_as.sandbox_fail_class import SandboxFailClass
from harness_as.secret_passthrough import (
    REDACTION_SENTINEL,
    PassthroughViolationKind,
    detect_mcp_server_token_passthrough,
    redact_secrets_in_input_span_attributes,
    redact_secrets_in_output,
)

_MARKERS = frozenset({"sk-live-deadbeef"})


def test_redact_secrets_in_output_replaces_secret_material() -> None:
    """Acceptance #1 — secret material in tool output is redacted."""
    redacted = redact_secrets_in_output("token=sk-live-deadbeef end", _MARKERS)
    assert "sk-live-deadbeef" not in redacted
    assert REDACTION_SENTINEL in redacted


def test_redact_secrets_in_output_preserves_non_secret_content() -> None:
    """Acceptance #1/#5 — non-secret content is preserved (structure-not-content)."""
    assert redact_secrets_in_output("plain output", _MARKERS) == "plain output"


def test_redact_secrets_in_input_span_attributes_redacts_secret_attribute_values() -> None:
    """Acceptance #2 — secret material in input span-attribute values is redacted."""
    redacted = redact_secrets_in_input_span_attributes(
        {"arg": "sk-live-deadbeef", "kind": "call"}, _MARKERS
    )
    assert "sk-live-deadbeef" not in str(redacted["arg"])
    assert redacted["kind"] == "call"


def test_detect_mcp_server_token_passthrough_detects_forwarded_token() -> None:
    """Acceptance #3/#4 — a forwarded client token upstream is a violation."""
    violation = detect_mcp_server_token_passthrough({"forwarded_client_token": True})
    assert violation is not None
    assert violation.kind is PassthroughViolationKind.MCP_SERVER_FORWARDED_TOKEN_UPSTREAM


def test_detect_mcp_server_token_passthrough_no_violation_on_distinct_upstream_token() -> None:
    """Acceptance #4 — a distinct upstream-issued token is not a violation."""
    assert detect_mcp_server_token_passthrough({"forwarded_client_token": False}) is None


def test_passthrough_violation_emits_egress_denied_class() -> None:
    """Acceptance #7 — a passthrough violation composes with the EGRESS_DENIED fail class."""
    assert SandboxFailClass.EGRESS_DENIED in SandboxFailClass
