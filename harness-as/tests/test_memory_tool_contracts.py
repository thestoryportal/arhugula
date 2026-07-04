"""Tests for U-MEM-13 provider-neutral memory tool contracts."""

from __future__ import annotations

from harness_as.memory_tool_contracts import (
    MEMORY_TOOL_CONTRACTS,
    MemoryToolDurableOperationKind,
    MemoryToolName,
    MemoryToolPolicyRequirement,
    memory_tool_contract,
    memory_tool_contracts_by_name,
)
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier


def test_memory_tool_contract_vocabulary_matches_c_mem_14() -> None:
    assert {entry.tool.value for entry in MEMORY_TOOL_CONTRACTS} == {
        "memory.search",
        "memory.read",
        "memory.write_note",
        "memory.propose_promotion",
        "memory.request_redaction",
    }
    assert {entry.contract.name for entry in MEMORY_TOOL_CONTRACTS} == {
        tool.value for tool in MemoryToolName
    }


def test_memory_tool_contract_lookup_is_total_and_stable() -> None:
    by_name = memory_tool_contracts_by_name()

    assert tuple(by_name) == tuple(tool.value for tool in MemoryToolName)
    for tool in MemoryToolName:
        assert memory_tool_contract(tool).tool is tool
        assert memory_tool_contract(tool.value).tool is tool
        assert by_name[tool.value] == memory_tool_contract(tool)


def test_memory_tools_are_provider_neutral_tool_contracts() -> None:
    provider_tokens = ("anthropic", "openai", "claude", "gpt", "gemini")

    for entry in MEMORY_TOOL_CONTRACTS:
        payload = str(entry.contract.model_dump(mode="json")).lower()
        assert all(token not in payload for token in provider_tokens)
        assert entry.contract.required_secrets == ()
        assert entry.contract.forces_computer_use is False
        assert entry.contract.forces_code_execution is False


def test_read_like_tools_are_read_only_idempotent_contracts() -> None:
    for tool in (MemoryToolName.SEARCH, MemoryToolName.READ):
        entry = memory_tool_contract(tool)

        assert entry.contract.minimum_tier is SandboxTier.TIER_1_PROCESS
        assert entry.contract.blast_radius_tier is BlastRadiusTier.READ_ONLY
        assert entry.contract.idempotent is True
        assert entry.requires_durable_memory_operation is False
        assert entry.durable_operation_kind is None
        assert MemoryToolPolicyRequirement.RETRIEVAL_ALLOWED in entry.policy_requirements


def test_write_like_tools_require_durable_memory_operations() -> None:
    expected = {
        MemoryToolName.WRITE_NOTE: MemoryToolDurableOperationKind.CAPTURE,
        MemoryToolName.PROPOSE_PROMOTION: MemoryToolDurableOperationKind.PROPOSE_PROMOTION,
        MemoryToolName.REQUEST_REDACTION: MemoryToolDurableOperationKind.REDACT,
    }

    for tool, operation_kind in expected.items():
        entry = memory_tool_contract(tool)
        assert entry.contract.minimum_tier is SandboxTier.TIER_1_PROCESS
        assert entry.contract.blast_radius_tier is BlastRadiusTier.LOCAL_MUTATION
        assert entry.contract.idempotent is False
        assert entry.requires_durable_memory_operation is True
        assert entry.durable_operation_kind is operation_kind
        assert MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION in entry.policy_requirements


def test_policy_requirements_match_each_memory_tool() -> None:
    assert memory_tool_contract(MemoryToolName.SEARCH).policy_requirements == (
        MemoryToolPolicyRequirement.RETRIEVAL_ALLOWED,
    )
    assert memory_tool_contract(MemoryToolName.READ).policy_requirements == (
        MemoryToolPolicyRequirement.RETRIEVAL_ALLOWED,
    )
    assert memory_tool_contract(MemoryToolName.WRITE_NOTE).policy_requirements == (
        MemoryToolPolicyRequirement.CAPTURE_ALLOWED,
        MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION,
    )
    assert memory_tool_contract(MemoryToolName.PROPOSE_PROMOTION).policy_requirements == (
        MemoryToolPolicyRequirement.PROMOTION_ALLOWED,
        MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION,
    )
    assert memory_tool_contract(MemoryToolName.REQUEST_REDACTION).policy_requirements == (
        MemoryToolPolicyRequirement.REDACTION_ALLOWED,
        MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION,
    )


def test_output_schemas_carry_stable_refs_and_retrieval_text_only_where_needed() -> None:
    for entry in MEMORY_TOOL_CONTRACTS:
        output_schema = entry.contract.output_schema
        field_names = _schema_field_names(output_schema)
        assert any(
            field_name.endswith("_ref") or field_name == "memory_ref" for field_name in field_names
        )
        assert "policy_ref" in field_names
        if entry.tool in {MemoryToolName.SEARCH, MemoryToolName.READ}:
            assert "text" in field_names
        else:
            assert "text" not in field_names


def _schema_field_names(schema: object) -> set[str]:
    if isinstance(schema, dict):
        field_names: set[str] = set()
        properties = schema.get("properties")
        if isinstance(properties, dict):
            field_names.update(str(key) for key in properties)
        for value in schema.values():
            field_names.update(_schema_field_names(value))
        return field_names
    if isinstance(schema, list):
        field_names: set[str] = set()
        for value in schema:
            field_names.update(_schema_field_names(value))
        return field_names
    return set()
