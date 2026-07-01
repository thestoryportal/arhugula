"""Six Anthropic-primitive attribute namespaces — U-AS-31.

Implements C-AS-14 §14.1-§14.7 (the six Anthropic-primitive attribute namespace
declarations — 40 attributes across 6 namespaces). Declares `AttributeNamespace`,
`AttributeSchema`, the six per-namespace schema tables, `namespace_schema`, and
the skill-version-sha semantic-distinction enforcement.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-31 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §14 C-AS-14; ADR-D3 v1.2 §1.8.1.

Depends on: U-AS-04 (the AS discriminator surface); U-AS-28 (the §13 primitive
surface). `AttributeValueType` / `Cardinality` are consumed from `harness-core`
per the U-AS-31 Class 1 fork resolution
(`.harness/class_1_tension_u_as_31_attribute_schema_enums.md`) — they were
re-homed from `harness-cp` to `harness-core` (cross-axis shared types).

`value_type` mapping: the §14 "Type" column maps to the 5-value
`AttributeValueType` — `int`→INT, `string` / `string (hex)`→STRING,
`enum string`→ENUM_REF, `bool`→BOOL, `float`→FLOAT.

`cardinality` mapping (materialization discretion, Class 3): the §14
"Cardinality" column uses ~12 descriptive phrases; the 4-value `Cardinality`
enum buckets them — `low` / `binary` / `bounded (N)`→LOW; `medium`→MEDIUM;
`high (per-session)` and the per-entity phrases (`per-primitive`,
`per-Skill-version`, `per-artifact`, `per-workspace`, `per-memory-file`)→HIGH;
`unbounded (metric)`→PER_REQUEST (a metric whose value varies every request).
The verbatim §14 phrase is preserved in the `semantic` field's source row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class AttributeNamespace(StrEnum):
    """The 6 Anthropic-primitive attribute namespaces (C-AS-14 §14.1)."""

    ANTHROPIC = "anthropic"
    MCP = "mcp"
    SKILL = "skill"
    MANAGED_AGENTS = "managed_agents"
    FILES = "files"
    MEMORY = "memory"


class AttributeSchema(BaseModel):
    """One per-attribute schema row (C-AS-14 §14.2-§14.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    semantic: str
    cardinality: Cardinality
    parent_span: str
    required: bool


class SkillVersionValidationResult(BaseModel):
    """Result of the §14.4 skill-version dual-field enforcement.

    The plan signature names this `ValidationResult`; named
    `SkillVersionValidationResult` here for an unambiguous public name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    missing: tuple[str, ...]


_STR = AttributeValueType.STRING
_INT = AttributeValueType.INT
_FLOAT = AttributeValueType.FLOAT
_BOOL = AttributeValueType.BOOL
_ENUM = AttributeValueType.ENUM_REF

_LOW = Cardinality.LOW
_MED = Cardinality.MEDIUM
_HIGH = Cardinality.HIGH
_PR = Cardinality.PER_REQUEST


def _row(
    name: str,
    value_type: AttributeValueType,
    semantic: str,
    cardinality: Cardinality,
    parent_span: str,
    *,
    required: bool = True,
) -> AttributeSchema:
    return AttributeSchema(
        attribute_name=name,
        value_type=value_type,
        semantic=semantic,
        cardinality=cardinality,
        parent_span=parent_span,
        required=required,
    )


# §14.2 — anthropic.* (10 attributes on the LLM inference span per AS spec v1.7
# §14.1 alias-term convention). `_ANTHROPIC_SPAN` is the alias-term per AS spec
# v1.7 §14.1: it decouples the parent-anchor cite from the runtime span-name,
# which is owned by OD spec v1.12 §C-OD-04 §4.1.
_ANTHROPIC_SPAN = "the LLM inference span"
ANTHROPIC_NAMESPACE_SCHEMA: tuple[AttributeSchema, ...] = (
    _row("anthropic.cache_creation_input_tokens", _INT, "Cache write count", _PR, _ANTHROPIC_SPAN),
    _row(
        "anthropic.cache_read_input_tokens",
        _INT,
        "Cache read count (0.10x cost)",
        _PR,
        _ANTHROPIC_SPAN,
    ),
    _row(
        "anthropic.cache_breakpoint_id", _STR, "Which of <=4 breakpoints hit", _LOW, _ANTHROPIC_SPAN
    ),
    _row("anthropic.cache_ttl_seconds", _INT, "300 (5min) or 3600 (1hr)", _LOW, _ANTHROPIC_SPAN),
    _row("anthropic.thinking_mode", _ENUM, "adaptive / enabled / disabled", _LOW, _ANTHROPIC_SPAN),
    _row(
        "anthropic.thinking_budget_tokens",
        _INT,
        "Adaptive: actual; enabled: budget",
        _PR,
        _ANTHROPIC_SPAN,
    ),
    _row(
        "anthropic.thinking_effort",
        _ENUM,
        "low / medium / high / xhigh / max",
        _LOW,
        _ANTHROPIC_SPAN,
    ),
    _row(
        "anthropic.batch_id",
        _STR,
        "Batch API submission marker",
        _PR,
        _ANTHROPIC_SPAN,
        required=False,
    ),
    _row("anthropic.tokenizer_version", _STR, "v1 (default); v2 (Opus 4.7)", _LOW, _ANTHROPIC_SPAN),
    _row(
        "anthropic.inference_geo",
        _ENUM,
        "us if data-residency premium",
        _LOW,
        _ANTHROPIC_SPAN,
        required=False,
    ),
)

# §14.3 — mcp.* (7 attributes on the mcp.tool.call span)
_MCP_SPAN = "mcp.tool.call"
MCP_NAMESPACE_SCHEMA: tuple[AttributeSchema, ...] = (
    _row("mcp.server.name", _STR, "Per-deployment server registry identifier", _MED, _MCP_SPAN),
    _row("mcp.server.trust_tier", _ENUM, "Four-tier per Cluster 4 §2.3.3", _LOW, _MCP_SPAN),
    _row("mcp.protocol_version", _STR, "2025-06-18", _LOW, _MCP_SPAN),
    _row("mcp.transport", _ENUM, "stdio / streamable_http", _LOW, _MCP_SPAN),
    _row("mcp.auth_present", _BOOL, "Always false on STDIO", _LOW, _MCP_SPAN),
    _row("mcp.primitive.kind", _ENUM, "tool / resource / prompt / sampling", _LOW, _MCP_SPAN),
    _row(
        "mcp.primitive.signature.sha256",
        _STR,
        "Per-primitive content-addressable hash",
        _HIGH,
        _MCP_SPAN,
    ),
)

# §14.4 — skill.* (6 attributes on the skill.activation span)
_SKILL_SPAN = "skill.activation"
SKILL_NAMESPACE_SCHEMA: tuple[AttributeSchema, ...] = (
    _row("skill.id", _STR, "Canonical Skill identifier", _MED, _SKILL_SPAN),
    _row("skill.name", _STR, "SKILL.md frontmatter name", _MED, _SKILL_SPAN),
    _row(
        "skill.version_sha",
        _STR,
        "Git content hash (replay-determinism anchor)",
        _HIGH,
        _SKILL_SPAN,
    ),
    _row(
        "skill.frontmatter.version",
        _STR,
        "SKILL.md frontmatter version (migration-tracking)",
        _HIGH,
        _SKILL_SPAN,
    ),
    _row(
        "skill.body_tokens",
        _INT,
        "Cost attribution (Skills coverage holdout per C8)",
        _PR,
        _SKILL_SPAN,
    ),
    _row(
        "skill.activation_mode",
        _ENUM,
        "frontmatter_only / tool_search / filesystem_read",
        _LOW,
        _SKILL_SPAN,
    ),
)

# §14.5 — managed_agents.* (3 attributes on the managed_agents.runtime span)
_MA_SPAN = "managed_agents.runtime"
MANAGED_AGENTS_NAMESPACE_SCHEMA: tuple[AttributeSchema, ...] = (
    _row("managed_agents.runtime_ms", _INT, "Runtime in milliseconds", _PR, _MA_SPAN),
    _row("managed_agents.session_id", _STR, "Per-session identifier", _HIGH, _MA_SPAN),
    _row("managed_agents.billable_seconds", _FLOAT, "x $0.08/3600 = cost", _PR, _MA_SPAN),
)

# §14.6 — files.* (8 attributes on the files.operation span)
_FILES_SPAN = "files.operation"
FILES_NAMESPACE_SCHEMA: tuple[AttributeSchema, ...] = (
    _row(
        "files.operation.kind",
        _ENUM,
        "upload / list / metadata / delete / reference",
        _LOW,
        _FILES_SPAN,
    ),
    _row("files.file_id", _STR, "Workspace-scoped file identifier", _HIGH, _FILES_SPAN),
    _row("files.filename", _STR, "Original filename (structure-not-content)", _HIGH, _FILES_SPAN),
    _row("files.mime_type", _STR, "MIME type discriminator", _MED, _FILES_SPAN),
    _row("files.size_bytes", _INT, "Uploaded size for cost attribution", _PR, _FILES_SPAN),
    _row(
        "files.workspace_id",
        _STR,
        "Workspace scope (Files API is workspace-scoped)",
        _HIGH,
        _FILES_SPAN,
    ),
    _row(
        "files.batch_composition",
        _BOOL,
        "True if file referenced in a Batch API submission",
        _LOW,
        _FILES_SPAN,
        required=False,
    ),
    _row(
        "files.code_execution_composition",
        _BOOL,
        "True if file passed to code execution via file_ids",
        _LOW,
        _FILES_SPAN,
        required=False,
    ),
)

# §14.7 — memory.* (6 attributes on the memory.operation span)
_MEMORY_SPAN = "memory.operation"
MEMORY_NAMESPACE_SCHEMA: tuple[AttributeSchema, ...] = (
    _row(
        "memory.operation.kind", _ENUM, "read / write / update / delete / list", _LOW, _MEMORY_SPAN
    ),
    _row("memory.path", _STR, "Path within /memories (structure-not-content)", _HIGH, _MEMORY_SPAN),
    _row(
        "memory.backend",
        _ENUM,
        "filesystem / s3 / database / encrypted_filesystem / operator_defined",
        _LOW,
        _MEMORY_SPAN,
    ),
    _row(
        "memory.bytes_read",
        _INT,
        "Read operations; cost attribution",
        _PR,
        _MEMORY_SPAN,
        required=False,
    ),
    _row(
        "memory.bytes_written",
        _INT,
        "Write operations; cost attribution",
        _PR,
        _MEMORY_SPAN,
        required=False,
    ),
    _row(
        "memory.context_editing_active",
        _BOOL,
        "True if parent (the LLM inference span) uses clear_tool_uses_20250919",
        _LOW,
        _MEMORY_SPAN,
    ),
)

#: The six per-namespace attribute-schema tables (C-AS-14 §14.2-§14.7).
_NAMESPACE_SCHEMAS: Mapping[AttributeNamespace, tuple[AttributeSchema, ...]] = MappingProxyType(
    {
        AttributeNamespace.ANTHROPIC: ANTHROPIC_NAMESPACE_SCHEMA,
        AttributeNamespace.MCP: MCP_NAMESPACE_SCHEMA,
        AttributeNamespace.SKILL: SKILL_NAMESPACE_SCHEMA,
        AttributeNamespace.MANAGED_AGENTS: MANAGED_AGENTS_NAMESPACE_SCHEMA,
        AttributeNamespace.FILES: FILES_NAMESPACE_SCHEMA,
        AttributeNamespace.MEMORY: MEMORY_NAMESPACE_SCHEMA,
    }
)

#: The two skill-version fields both required at every skill.activation span
#: (C-AS-14 §14.4 / ADR-D3 v1.2 §1.8.1 — load-bearing semantic distinction).
_SKILL_VERSION_FIELDS: tuple[str, ...] = ("skill.version_sha", "skill.frontmatter.version")


def namespace_schema(ns: AttributeNamespace) -> tuple[AttributeSchema, ...]:
    """Return the attribute-schema table for an attribute namespace (§14.2-§14.7)."""
    return _NAMESPACE_SCHEMAS[ns]


def validate_skill_attributes_carry_both_version_fields(
    skill_span_attrs: Iterable[str],
) -> SkillVersionValidationResult:
    """Enforce the §14.4 skill-version dual-field invariant.

    `skill.version_sha` (git content hash; replay-determinism anchor) AND
    `skill.frontmatter.version` (operator-declared; migration-tracking) are
    BOTH required at every `skill.activation` span — a span carrying only one
    is rejected (acceptance #6 / ADR-D3 v1.2 §1.8.1).
    """
    present = set(skill_span_attrs)
    missing = tuple(f for f in _SKILL_VERSION_FIELDS if f not in present)
    return SkillVersionValidationResult(valid=not missing, missing=missing)
