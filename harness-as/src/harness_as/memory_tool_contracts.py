"""Provider-neutral memory tool contracts - U-MEM-13."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.tool_contract import ToolContract


class MemoryToolName(StrEnum):
    """C-MEM-14 provider-neutral memory tool names."""

    SEARCH = "memory.search"
    READ = "memory.read"
    WRITE_NOTE = "memory.write_note"
    PROPOSE_PROMOTION = "memory.propose_promotion"
    REQUEST_REDACTION = "memory.request_redaction"


class MemoryToolPolicyRequirement(StrEnum):
    """Policy gates a memory tool runtime must satisfy before execution."""

    RETRIEVAL_ALLOWED = "retrieval_allowed"
    CAPTURE_ALLOWED = "capture_allowed"
    PROMOTION_ALLOWED = "promotion_allowed"
    REDACTION_ALLOWED = "redaction_allowed"
    DURABLE_MEMORY_OPERATION = "durable_memory_operation"


class MemoryToolDurableOperationKind(StrEnum):
    """Durable memory operation kinds required by write-like C-MEM-14 tools."""

    CAPTURE = "capture"
    PROPOSE_PROMOTION = "propose_promotion"
    REDACT = "redact"


class MemoryToolContract(BaseModel):
    """A provider-neutral memory tool contract plus policy metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: MemoryToolName
    contract: ToolContract
    policy_requirements: tuple[MemoryToolPolicyRequirement, ...]
    requires_durable_memory_operation: bool
    durable_operation_kind: MemoryToolDurableOperationKind | None = None

    @model_validator(mode="after")
    def _durable_operation_matches_requirement(self) -> Self:
        has_durable_requirement = (
            MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION in self.policy_requirements
        )
        if self.requires_durable_memory_operation != has_durable_requirement:
            raise ValueError("durable requirement must match durable operation flag")
        if self.requires_durable_memory_operation and self.durable_operation_kind is None:
            raise ValueError("write-like memory tools must declare a durable operation kind")
        if not self.requires_durable_memory_operation and self.durable_operation_kind is not None:
            raise ValueError("read-like memory tools cannot declare a durable operation kind")
        if self.contract.name != self.tool.value:
            raise ValueError("tool contract name must match memory tool name")
        return self


def memory_tool_contracts_by_name() -> MappingProxyType[str, MemoryToolContract]:
    """Return memory tool contracts keyed by their provider-neutral tool name."""

    return MappingProxyType({entry.tool.value: entry for entry in MEMORY_TOOL_CONTRACTS})


def memory_tool_contract(tool: MemoryToolName | str) -> MemoryToolContract:
    """Return one provider-neutral memory tool contract."""

    tool_name = tool if isinstance(tool, MemoryToolName) else MemoryToolName(tool)
    return memory_tool_contracts_by_name()[tool_name.value]


def _tool_contract(
    *,
    name: MemoryToolName,
    description: str,
    input_schema: dict[str, object],
    output_schema: dict[str, object],
    blast_radius_tier: BlastRadiusTier,
    idempotent: bool,
) -> ToolContract:
    return ToolContract(
        name=name.value,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=blast_radius_tier,
        required_secrets=(),
        idempotent=idempotent,
    )


def _object_schema(
    *,
    properties: dict[str, object],
    required: tuple[str, ...],
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": properties,
    }


def _array_schema(items: dict[str, object]) -> dict[str, object]:
    return {"type": "array", "items": items}


_REF: dict[str, object] = {"type": "string", "minLength": 1}
_HASH: dict[str, object] = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
_KIND: dict[str, object] = {"type": "string", "minLength": 1}
_SCORE: dict[str, object] = {"type": "integer", "minimum": 0}
_BOOL: dict[str, object] = {"type": "boolean"}
_QUERY: dict[str, object] = {"type": "string", "minLength": 1}
_LIMIT: dict[str, object] = {"type": "integer", "minimum": 1}
_NOTE: dict[str, object] = {"type": "string", "minLength": 1}
_TEXT: dict[str, object] = {"type": "string"}


MEMORY_TOOL_CONTRACTS: tuple[MemoryToolContract, ...] = (
    MemoryToolContract(
        tool=MemoryToolName.SEARCH,
        contract=_tool_contract(
            name=MemoryToolName.SEARCH,
            description="Search eligible memory records and return source-linked refs plus text.",
            input_schema=_object_schema(
                required=("query", "scope_ref", "policy_ref"),
                properties={
                    "query": _QUERY,
                    "scope_ref": _REF,
                    "policy_ref": _REF,
                    "limit": _LIMIT,
                    "allowed_kinds": _array_schema(_KIND),
                },
            ),
            output_schema=_object_schema(
                required=("results", "policy_ref"),
                properties={
                    "results": _array_schema(
                        _object_schema(
                            required=(
                                "memory_ref",
                                "record_kind",
                                "packet_section_ref",
                                "packet_hash",
                                "score",
                                "text",
                            ),
                            properties={
                                "memory_ref": _REF,
                                "record_kind": _KIND,
                                "packet_section_ref": _REF,
                                "packet_hash": _HASH,
                                "score": _SCORE,
                                "text": _TEXT,
                                "ranking_trace_ref": _REF,
                            },
                        )
                    ),
                    "policy_ref": _REF,
                },
            ),
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
            idempotent=True,
        ),
        policy_requirements=(MemoryToolPolicyRequirement.RETRIEVAL_ALLOWED,),
        requires_durable_memory_operation=False,
    ),
    MemoryToolContract(
        tool=MemoryToolName.READ,
        contract=_tool_contract(
            name=MemoryToolName.READ,
            description="Read one allowed memory record or packet section by stable ref.",
            input_schema=_object_schema(
                required=("memory_ref", "policy_ref"),
                properties={
                    "memory_ref": _REF,
                    "packet_section_ref": _REF,
                    "policy_ref": _REF,
                },
            ),
            output_schema=_object_schema(
                required=(
                    "memory_ref",
                    "record_kind",
                    "packet_section_ref",
                    "content_hash",
                    "text",
                    "policy_ref",
                ),
                properties={
                    "memory_ref": _REF,
                    "record_kind": _KIND,
                    "packet_section_ref": _REF,
                    "content_hash": _HASH,
                    "text": _TEXT,
                    "policy_ref": _REF,
                },
            ),
            blast_radius_tier=BlastRadiusTier.READ_ONLY,
            idempotent=True,
        ),
        policy_requirements=(MemoryToolPolicyRequirement.RETRIEVAL_ALLOWED,),
        requires_durable_memory_operation=False,
    ),
    MemoryToolContract(
        tool=MemoryToolName.WRITE_NOTE,
        contract=_tool_contract(
            name=MemoryToolName.WRITE_NOTE,
            description="Write an episodic memory note under policy.",
            input_schema=_object_schema(
                required=("note", "scope_ref", "policy_ref"),
                properties={
                    "note": _NOTE,
                    "scope_ref": _REF,
                    "policy_ref": _REF,
                    "idempotency_key": _REF,
                },
            ),
            output_schema=_object_schema(
                required=("memory_ref", "operation_ref", "policy_ref"),
                properties={
                    "memory_ref": _REF,
                    "operation_ref": _REF,
                    "policy_ref": _REF,
                },
            ),
            blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
            idempotent=False,
        ),
        policy_requirements=(
            MemoryToolPolicyRequirement.CAPTURE_ALLOWED,
            MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION,
        ),
        requires_durable_memory_operation=True,
        durable_operation_kind=MemoryToolDurableOperationKind.CAPTURE,
    ),
    MemoryToolContract(
        tool=MemoryToolName.PROPOSE_PROMOTION,
        contract=_tool_contract(
            name=MemoryToolName.PROPOSE_PROMOTION,
            description="Submit a memory promotion candidate for policy review.",
            input_schema=_object_schema(
                required=("memory_ref", "target_kind", "policy_ref"),
                properties={
                    "memory_ref": _REF,
                    "target_kind": _KIND,
                    "evidence_ref": _REF,
                    "policy_ref": _REF,
                    "idempotency_key": _REF,
                },
            ),
            output_schema=_object_schema(
                required=("promotion_ref", "operation_ref", "policy_ref", "review_required"),
                properties={
                    "promotion_ref": _REF,
                    "operation_ref": _REF,
                    "policy_ref": _REF,
                    "review_required": _BOOL,
                },
            ),
            blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
            idempotent=False,
        ),
        policy_requirements=(
            MemoryToolPolicyRequirement.PROMOTION_ALLOWED,
            MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION,
        ),
        requires_durable_memory_operation=True,
        durable_operation_kind=MemoryToolDurableOperationKind.PROPOSE_PROMOTION,
    ),
    MemoryToolContract(
        tool=MemoryToolName.REQUEST_REDACTION,
        contract=_tool_contract(
            name=MemoryToolName.REQUEST_REDACTION,
            description="Submit a durable redaction request for a memory ref.",
            input_schema=_object_schema(
                required=("memory_ref", "reason", "policy_ref"),
                properties={
                    "memory_ref": _REF,
                    "reason": _NOTE,
                    "policy_ref": _REF,
                    "idempotency_key": _REF,
                },
            ),
            output_schema=_object_schema(
                required=("redaction_request_ref", "operation_ref", "policy_ref"),
                properties={
                    "redaction_request_ref": _REF,
                    "operation_ref": _REF,
                    "policy_ref": _REF,
                },
            ),
            blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
            idempotent=False,
        ),
        policy_requirements=(
            MemoryToolPolicyRequirement.REDACTION_ALLOWED,
            MemoryToolPolicyRequirement.DURABLE_MEMORY_OPERATION,
        ),
        requires_durable_memory_operation=True,
        durable_operation_kind=MemoryToolDurableOperationKind.REDACT,
    ),
)


__all__ = [
    "MEMORY_TOOL_CONTRACTS",
    "MemoryToolContract",
    "MemoryToolDurableOperationKind",
    "MemoryToolName",
    "MemoryToolPolicyRequirement",
    "memory_tool_contract",
    "memory_tool_contracts_by_name",
]
