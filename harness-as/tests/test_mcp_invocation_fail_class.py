"""Tests for MCPInvocationFailClass + projection — AS plan v1.4 §1.

Authority: AS spec v1.6 §15.8 (MCPInvocationFailClass 4-value enum) +
§15.10 (best-effort projection table MCP-shape → F4-shape). Sibling to
F4 SandboxFailClass at C-AS-04 §4.1.

Carrier-extension landed at U-AS-03 per AS plan v1.4 §1 canonical-reading
amendment (sandbox_fail_class.py module).
"""

from __future__ import annotations

from harness_as.sandbox_fail_class import (
    MCPInvocationFailClass,
    SandboxFailClass,
    project_mcp_to_sandbox_fail_class,
)

# 4 MCP-fail-class identifiers byte-exact per spec §15.8 row 1.
_SPEC_MCP_FAIL_CLASSES = {
    "transport",
    "protocol_error",
    "schema_violation",
    "timeout",
}


def test_mcp_invocation_fail_class_enum_cardinality_four() -> None:
    """AS plan v1.4 §1 AC #N+1 — bounded cardinality 4."""
    assert len(list(MCPInvocationFailClass)) == 4


def test_mcp_invocation_fail_class_identifier_strings_snake_case_byte_exact() -> None:
    """AS plan v1.4 §1 AC #N+1 — 4 enum values match §15.8 row 1 byte-exact."""
    actual = {m.value for m in MCPInvocationFailClass}
    assert actual == _SPEC_MCP_FAIL_CLASSES


def test_mcp_invocation_fail_class_is_str_enum() -> None:
    """MCPInvocationFailClass values are usable as strings (StrEnum)."""
    assert MCPInvocationFailClass.TRANSPORT == "transport"
    assert MCPInvocationFailClass.PROTOCOL_ERROR == "protocol_error"
    assert MCPInvocationFailClass.SCHEMA_VIOLATION == "schema_violation"
    assert MCPInvocationFailClass.TIMEOUT == "timeout"


def test_project_mcp_to_sandbox_transport_returns_exit_nonzero() -> None:
    """§15.10 row 1 — transport → exit_nonzero (MODERATE semantic stretch)."""
    assert (
        project_mcp_to_sandbox_fail_class(MCPInvocationFailClass.TRANSPORT)
        == SandboxFailClass.EXIT_NONZERO
    )


def test_project_mcp_to_sandbox_protocol_error_returns_exit_nonzero() -> None:
    """§15.10 row 2 — protocol_error → exit_nonzero (MODERATE semantic stretch)."""
    assert (
        project_mcp_to_sandbox_fail_class(MCPInvocationFailClass.PROTOCOL_ERROR)
        == SandboxFailClass.EXIT_NONZERO
    )


def test_project_mcp_to_sandbox_schema_violation_returns_policy_override() -> None:
    """§15.10 row 3 — schema_violation → policy_override (HIGH semantic stretch).

    Flagged HIGH at §15.10 row 3 itself. Future ADR-D2 / F4 enum
    revision MAY add a `contract_violation` value to absorb cleanly.
    """
    assert (
        project_mcp_to_sandbox_fail_class(MCPInvocationFailClass.SCHEMA_VIOLATION)
        == SandboxFailClass.POLICY_OVERRIDE
    )


def test_project_mcp_to_sandbox_timeout_returns_timeout() -> None:
    """§15.10 row 4 — timeout → timeout (clean at value name; layer-stretch only)."""
    assert (
        project_mcp_to_sandbox_fail_class(MCPInvocationFailClass.TIMEOUT)
        == SandboxFailClass.TIMEOUT
    )


def test_project_mcp_to_sandbox_total_function_over_enum_domain() -> None:
    """§15.10 projection is total over the 4-value MCPInvocationFailClass domain."""
    for member in MCPInvocationFailClass:
        result = project_mcp_to_sandbox_fail_class(member)
        assert isinstance(result, SandboxFailClass)


def test_project_targets_subset_of_sandbox_fail_class_enum() -> None:
    """Projection codomain is a subset of the 7-value F4 enum (§4.1 PRESERVED VERBATIM)."""
    projected = {project_mcp_to_sandbox_fail_class(m) for m in MCPInvocationFailClass}
    # 3 distinct F4 values reached: exit_nonzero (×2), policy_override, timeout.
    assert projected == {
        SandboxFailClass.EXIT_NONZERO,
        SandboxFailClass.POLICY_OVERRIDE,
        SandboxFailClass.TIMEOUT,
    }


def test_mcp_invocation_fail_class_exposed_at_harness_as_root() -> None:
    """AS plan v1.4 §1 AC #N+3 — re-exported from harness_as package root."""
    from harness_as import MCPInvocationFailClass as Reexport

    assert Reexport is MCPInvocationFailClass


def test_project_function_exposed_at_harness_as_root() -> None:
    """AS plan v1.4 §1 AC #N+3 — re-exported from harness_as package root."""
    from harness_as import project_mcp_to_sandbox_fail_class as reexport

    assert reexport is project_mcp_to_sandbox_fail_class
