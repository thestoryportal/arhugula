"""Tests for U-AS-31 — six Anthropic-primitive attribute namespaces (C-AS-14 §14)."""

from __future__ import annotations

from harness_as.anthropic_attribute_namespaces import (
    AttributeNamespace,
    AttributeSchema,
    namespace_schema,
    validate_skill_attributes_carry_both_version_fields,
)
from harness_core import AttributeValueType, Cardinality

_EXPECTED_COUNTS = {
    AttributeNamespace.ANTHROPIC: 10,
    AttributeNamespace.MCP: 7,
    AttributeNamespace.SKILL: 6,
    AttributeNamespace.MANAGED_AGENTS: 3,
    AttributeNamespace.FILES: 8,
    AttributeNamespace.MEMORY: 6,
}

_OPTIONAL_ATTRIBUTES = {
    "anthropic.batch_id",
    "anthropic.inference_geo",
    "files.batch_composition",
    "files.code_execution_composition",
    "memory.bytes_read",
    "memory.bytes_written",
}


def test_attribute_namespace_cardinality_six() -> None:
    """Acceptance #1 — AttributeNamespace declares 6 values."""
    assert len(AttributeNamespace) == 6


def test_anthropic_namespace_ten_attributes_per_spec_14_2() -> None:
    """Acceptance #2/#3 — the anthropic.* namespace has 10 attributes."""
    schema = namespace_schema(AttributeNamespace.ANTHROPIC)
    assert len(schema) == 10
    assert {a.attribute_name for a in schema} == {
        "anthropic.cache_creation_input_tokens",
        "anthropic.cache_read_input_tokens",
        "anthropic.cache_breakpoint_id",
        "anthropic.cache_ttl_seconds",
        "anthropic.thinking_mode",
        "anthropic.thinking_budget_tokens",
        "anthropic.thinking_effort",
        "anthropic.batch_id",
        "anthropic.tokenizer_version",
        "anthropic.inference_geo",
    }


def test_mcp_namespace_seven_attributes_per_spec_14_3() -> None:
    """Acceptance #2/#4 — the mcp.* namespace has 7 attributes."""
    assert len(namespace_schema(AttributeNamespace.MCP)) == 7


def test_skill_namespace_six_attributes_per_spec_14_4() -> None:
    """Acceptance #2/#5 — the skill.* namespace has 6 attributes."""
    assert len(namespace_schema(AttributeNamespace.SKILL)) == 6


def test_managed_agents_namespace_three_attributes_per_spec_14_5() -> None:
    """Acceptance #2/#7 — the managed_agents.* namespace has 3 attributes."""
    assert len(namespace_schema(AttributeNamespace.MANAGED_AGENTS)) == 3


def test_files_namespace_eight_attributes_per_spec_14_6() -> None:
    """Acceptance #2/#8 — the files.* namespace has 8 attributes."""
    assert len(namespace_schema(AttributeNamespace.FILES)) == 8


def test_memory_namespace_six_attributes_per_spec_14_7() -> None:
    """Acceptance #2/#9 — the memory.* namespace has 6 attributes."""
    assert len(namespace_schema(AttributeNamespace.MEMORY)) == 6


def test_aggregate_attribute_count_40() -> None:
    """Acceptance #2 — aggregate attribute count across all 6 namespaces is 40."""
    total = sum(len(namespace_schema(ns)) for ns in AttributeNamespace)
    assert total == 40
    for ns, count in _EXPECTED_COUNTS.items():
        assert len(namespace_schema(ns)) == count


def test_skill_span_requires_both_version_fields() -> None:
    """Acceptance #6 — a skill span with only version_sha is rejected."""
    result = validate_skill_attributes_carry_both_version_fields({"skill.version_sha"})
    assert result.valid is False
    assert "skill.frontmatter.version" in result.missing


def test_skill_span_requires_both_version_fields_reverse() -> None:
    """Acceptance #6 — a skill span with only frontmatter.version is rejected."""
    result = validate_skill_attributes_carry_both_version_fields({"skill.frontmatter.version"})
    assert result.valid is False
    assert "skill.version_sha" in result.missing


def test_skill_span_accepts_both_version_fields() -> None:
    """Acceptance #6 — a skill span carrying both version fields is accepted."""
    result = validate_skill_attributes_carry_both_version_fields(
        {"skill.version_sha", "skill.frontmatter.version", "skill.id"}
    )
    assert result.valid is True
    assert result.missing == ()


def test_attribute_names_byte_exact_per_spec() -> None:
    """Acceptance #10 — every attribute name is namespace-prefixed and an AttributeSchema."""
    for ns in AttributeNamespace:
        for attr in namespace_schema(ns):
            assert isinstance(attr, AttributeSchema)
            assert attr.attribute_name.startswith(f"{ns.value}.")
            assert attr.value_type in AttributeValueType
            assert attr.cardinality in Cardinality


def test_optional_attributes_per_spec() -> None:
    """Acceptance #12 — exactly the six §14 optional attributes have required=False."""
    optional_found: set[str] = set()
    for ns in AttributeNamespace:
        for attr in namespace_schema(ns):
            if not attr.required:
                optional_found.add(attr.attribute_name)
    assert optional_found == _OPTIONAL_ATTRIBUTES
