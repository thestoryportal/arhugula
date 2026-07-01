"""Tests for U-AS-16 — seven sandbox.* attributes + tech-provider join (C-AS-15 §15)."""

from __future__ import annotations

from harness_as.sandbox_attribute_schema import (
    MCP_INVOCATION_ATTRIBUTE_SCHEMA,
    SANDBOX_ATTRIBUTE_SCHEMA,
    SandboxProvider,
    SandboxTechClass,
    provider_belongs_to,
    tech_admits_provider,
)


def test_sandbox_attribute_schema_cardinality_seven() -> None:
    """Acceptance #1 — SANDBOX_ATTRIBUTE_SCHEMA declares exactly 7 entries."""
    assert len(SANDBOX_ATTRIBUTE_SCHEMA) == 7


def test_sandbox_attribute_names_byte_exact_per_spec_15_2() -> None:
    """Acceptance #1/#2 — the seven attribute names are byte-exact per §15.2."""
    assert {a.attribute_name for a in SANDBOX_ATTRIBUTE_SCHEMA} == {
        "sandbox.tier",
        "sandbox.tech",
        "sandbox.fail.class",
        "sandbox.policy.assigned_tier_reason",
        "sandbox.cost.tier_overhead_ms",
        "sandbox.cost.tier_overhead_usd",
        "sandbox.provider",
    }


def test_sandbox_attribute_emitted_on_per_spec() -> None:
    """Acceptance #1 — each attribute's emitted-on span matches §15.2."""
    by_name = {a.attribute_name: a for a in SANDBOX_ATTRIBUTE_SCHEMA}
    assert by_name["sandbox.tier"].emitted_on == "sandbox.enter"
    assert by_name["sandbox.fail.class"].emitted_on == "sandbox.violation"
    assert by_name["sandbox.cost.tier_overhead_ms"].emitted_on == "sandbox.exit"


def test_sandbox_tech_class_cardinality_five() -> None:
    """Acceptance #3 — SandboxTechClass carries exactly 5 values."""
    assert len(SandboxTechClass) == 5


def test_sandbox_provider_cardinality_seventeen_at_v1_1() -> None:
    """Acceptance #4 — SandboxProvider carries exactly 17 values at v1.1."""
    assert len(SandboxProvider) == 17


def test_provider_belongs_to_total_function() -> None:
    """Acceptance #5 — provider_belongs_to is total over SandboxProvider."""
    for provider in SandboxProvider:
        assert provider_belongs_to(provider) in SandboxTechClass


def _members_of(tech: SandboxTechClass) -> set[SandboxProvider]:
    return {p for p in SandboxProvider if provider_belongs_to(p) is tech}


def test_provider_belongs_to_microvm_class_six_members() -> None:
    """Acceptance #5 — the microvm tech class has 6 providers."""
    assert len(_members_of(SandboxTechClass.MICROVM)) == 6


def test_provider_belongs_to_container_class_four_members() -> None:
    """Acceptance #5 — the container tech class has 4 providers."""
    assert len(_members_of(SandboxTechClass.CONTAINER)) == 4


def test_provider_belongs_to_vm_class_zero_members() -> None:
    """Acceptance #5 — the vm tech class is reserved (0 providers at v1.1)."""
    assert len(_members_of(SandboxTechClass.VM)) == 0


def test_provider_belongs_to_language_level_class_two_members() -> None:
    """Acceptance #5 — the language-level tech class has 2 providers."""
    assert len(_members_of(SandboxTechClass.LANGUAGE_LEVEL)) == 2


def test_provider_belongs_to_fs_overlay_class_five_members() -> None:
    """Acceptance #5 — the fs-overlay tech class has 5 providers."""
    assert len(_members_of(SandboxTechClass.FS_OVERLAY)) == 5


def test_tech_admits_provider_functional_join() -> None:
    """Acceptance #5 — tech_admits_provider is the functional belongs-to join."""
    assert tech_admits_provider(SandboxTechClass.MICROVM, SandboxProvider.E2B_FIRECRACKER)
    assert not tech_admits_provider(SandboxTechClass.CONTAINER, SandboxProvider.E2B_FIRECRACKER)


def test_mcp_invocation_attribute_schema_cardinality_one() -> None:
    """AS plan v1.4 §2 AC #10 — MCP_INVOCATION_ATTRIBUTE_SCHEMA declares 1 entry."""
    assert len(MCP_INVOCATION_ATTRIBUTE_SCHEMA) == 1


def test_mcp_fail_class_emitted_on_sandbox_violation_event() -> None:
    """AS plan v1.4 §2 AC #10 — mcp.fail.class emitted_on=sandbox.violation per §15.9."""
    row = MCP_INVOCATION_ATTRIBUTE_SCHEMA[0]
    assert row.attribute_name == "mcp.fail.class"
    assert row.emitted_on == "sandbox.violation"
