"""Secret-passthrough constraint enforcement — U-AS-23.

Implements C-AS-06 §6.3 (output redaction + input redaction + MCP-server
passthrough prohibition). Declares `PassthroughViolationKind`,
`PassthroughViolation`, and the redaction / detection functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-23 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §6.3 C-AS-06; ADR-F5 v1.1 §Decision.

Depends on: U-AS-17 (`AttributeValue`); U-AS-20 (`fetch_secret`-resolved
values); U-AS-22 (allowlist composition).

Detection mechanism (documented discretion): spec §6.3 defers the specific
secret-passthrough detection mechanism (regex / fingerprint / taint-tracking).
The redaction / detection functions take an explicit `secret_markers` set — a
marker-substring detector is the unit-grade placeholder; `_REDACTED_` is the
redaction sentinel.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_span_schema import AttributeValue

#: The redaction sentinel substituted for detected secret material.
REDACTION_SENTINEL = "_REDACTED_"


class PassthroughViolationKind(StrEnum):
    """A secret-passthrough violation kind (C-AS-06 §6.3)."""

    OUTPUT_CONTAINS_SECRET_MATERIAL = "OUTPUT_CONTAINS_SECRET_MATERIAL"
    INPUT_SPAN_ATTRIBUTE_CONTAINS_SECRET_MATERIAL = "INPUT_SPAN_ATTRIBUTE_CONTAINS_SECRET_MATERIAL"
    MCP_SERVER_FORWARDED_TOKEN_UPSTREAM = "MCP_SERVER_FORWARDED_TOKEN_UPSTREAM"


class PassthroughViolation(BaseModel):
    """A detected secret-passthrough violation (C-AS-06 §6.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PassthroughViolationKind
    detection_site: str
    redaction_applied: bool


def redact_secrets_in_output(output: str, secret_markers: frozenset[str] = frozenset()) -> str:
    """Redact secret material from tool output (C-AS-06 §6.3 row 1).

    Each known secret marker substring is replaced with the redaction
    sentinel; non-secret content is preserved (structure-not-content).
    """
    redacted = output
    for marker in secret_markers:
        if marker:
            redacted = redacted.replace(marker, REDACTION_SENTINEL)
    return redacted


def redact_secrets_in_input_span_attributes(
    attributes: Mapping[str, AttributeValue],
    secret_markers: frozenset[str] = frozenset(),
) -> dict[str, AttributeValue]:
    """Redact secret material from input span attributes (C-AS-06 §6.3 row 2).

    String attribute values containing a known secret marker are redacted; the
    resolved secret remains only inside the sandbox per U-AS-20 §5.2.
    """
    return {
        name: (redact_secrets_in_output(value, secret_markers) if isinstance(value, str) else value)
        for name, value in attributes.items()
    }


def detect_mcp_server_token_passthrough(
    mcp_call_record: Mapping[str, object],
) -> PassthroughViolation | None:
    """Detect an MCP-server upstream token passthrough (C-AS-06 §6.3 row 3).

    Per the MCP authorization spec 2025-06-18 directive, an MCP server MUST NOT
    forward the client-issued token to an upstream API. Returns a violation
    when `mcp_call_record` carries `forwarded_client_token=True` (the
    client-issued token reused upstream); a distinct upstream-issued token is
    not a violation.
    """
    if mcp_call_record.get("forwarded_client_token") is True:
        return PassthroughViolation(
            kind=PassthroughViolationKind.MCP_SERVER_FORWARDED_TOKEN_UPSTREAM,
            detection_site="mcp_server_upstream_call",
            redaction_applied=False,
        )
    return None
