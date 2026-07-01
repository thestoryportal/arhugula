"""Tests for U-AS-04 — `MCPTransport` enum + cross-cutting discriminator imports.

Test set per the U-AS-04 `Tests:` field (R3-revised, v1.1 §5.3). The
`test_deployment_surface_*` / `test_persona_tier_*` tests are deleted — those
enums moved to U-CORE-01 and are tested there. Retained tests are scoped to
the AS-owned `MCPTransport`; `test_u_as_04_does_not_redeclare_core_enums` is
the declaration-site-conversion guard (acceptance #4).
"""

from __future__ import annotations

from harness_as.discriminators import DeploymentSurface, MCPTransport, PersonaTier

_SPEC_MCP_TRANSPORTS = {
    "stdio",
    "streamable_http_l0",
    "streamable_http_l1",
    "streamable_http_l2",
    "streamable_http_l3",
}


def test_mcp_transport_cardinality_five() -> None:
    """Acceptance #1 — MCPTransport carries exactly 5 values, spec-canonical."""
    assert len(MCPTransport) == 5
    assert {m.value for m in MCPTransport} == _SPEC_MCP_TRANSPORTS


def test_enum_identifier_strings_byte_exact() -> None:
    """Acceptance #3 — MCPTransport identifier strings byte-exact spec-canonical."""
    assert MCPTransport.STDIO.value == "stdio"
    assert MCPTransport.STREAMABLE_HTTP_L0_REFUSE.value == "streamable_http_l0"
    assert MCPTransport.STREAMABLE_HTTP_L3_AUDIT.value == "streamable_http_l3"


def test_u_as_04_does_not_redeclare_core_enums() -> None:
    """Acceptance #4 — `DeploymentSurface` / `PersonaTier` are imported from
    `harness-core` (U-CORE-01); U-AS-04 does not redeclare them. A local
    redeclaration would put `__module__` at `harness_as.discriminators`."""
    assert DeploymentSurface.__module__ == "harness_core.deployment_surface"
    assert PersonaTier.__module__ == "harness_core.persona_tier"
