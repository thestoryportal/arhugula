"""Anthropic-API graceful-degradation + workload-binding selection — U-AS-30.

Implements C-AS-13 §13.5 (per-primitive graceful degradation under Anthropic-API
outage), §13.6 (workload-binding-time selection contract). Declares the
`OutageBehavior` enum, `GRACEFUL_DEGRADATION_POLICY`, the C6 cross-family
fallback chain, `MemoryToolStorageBackend`, `memory_tool_storage_backend`,
`WorkloadBindingDecision`, and `compose_workload_binding_decision`.

Authority: Implementation_Plan_Action_Surface_v1_1.md §5.3 U-AS-30 (R3-revised
body — Pattern B carriers declared in-unit; `[U-CP-00]` edge for `WorkloadClass`;
v1 base body at Implementation_Plan_Action_Surface_v1.md §2 U-AS-30);
Spec_Action_Surface_v1.md §13.5-§13.6 C-AS-13; ADR-D3 v1.2 §1.7 + §1.9.

Depends on: U-AS-04 (`DeploymentSurface`); U-AS-28 (`AnthropicPrimitive`,
`AdoptionDepth`, `adoption_depth`); U-AS-29 (`SubAgentRole`, `ModelBinding`,
`model_binding`); U-CP-00 (cross-axis: core — `WorkloadClass`).

Five Pattern B carriers are declared in-unit (v1.1 §5.3) — `ExtendedThinkingEffort`,
`BatchApiCell`, `WorkloadManifestOverrides`, `Provider`, `ModelClass`. The spec
§13.5/§13.6 name them but give no field schema for `BatchApiCell` /
`WorkloadManifestOverrides`; each is resolved minimally (no invented structure,
X-AL-3). `memory_tool_storage_backend` for `self-hosted-server` is filled with
the composable subset — AC5 specifies only `local-development` and
`managed-cloud` (Class 3 materialization discretion).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_as.anthropic_primitive_adoption import (
    AdoptionDepth,
    AnthropicPrimitive,
    adoption_depth,
)
from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.engine_class_composition import ModelBinding, SubAgentRole, model_binding


class OutageBehavior(StrEnum):
    """Per-primitive behavior under an Anthropic-API outage (C-AS-13 §13.5)."""

    CONTINUES = "CONTINUES"
    FALLS_THROUGH_TO_HARNESS_OWNED_TOPOLOGY = "FALLS_THROUGH_TO_HARNESS_OWNED_TOPOLOGY"
    C6_CROSS_FAMILY_FALLBACK = "C6_CROSS_FAMILY_FALLBACK"
    CACHE_STATE_LOST = "CACHE_STATE_LOST"
    RUNS_WITHOUT_PRIMITIVE = "RUNS_WITHOUT_PRIMITIVE"
    IN_FLIGHT_RESUME_ON_RECOVERY = "IN_FLIGHT_RESUME_ON_RECOVERY"
    HARNESS_OWNED_HOOK_LIFECYCLE = "HARNESS_OWNED_HOOK_LIFECYCLE"
    CROSS_FAMILY_LOSES_REFERENCES = "CROSS_FAMILY_LOSES_REFERENCES"
    CROSS_FAMILY_COMPATIBLE_VIA_CLIENT_STORAGE = "CROSS_FAMILY_COMPATIBLE_VIA_CLIENT_STORAGE"


class Provider(StrEnum):
    """A multi-LLM provider family — the §13.5 row-4 fallback families."""

    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    VERTEX = "vertex"
    OPENAI = "openai"
    OLLAMA = "ollama"


class ModelClass(StrEnum):
    """A fallback-pair model-class identifier (C-AS-13 §13.5 row 4)."""

    CLAUDE = "claude"
    CLAUDE_EQUIVALENT = "claude_equivalent"
    GPT_CLASS = "gpt_class"
    OLLAMA = "ollama"


class ExtendedThinkingEffort(StrEnum):
    """Extended-thinking effort level (C-AS-13 §13.6 step 6 / spec §14.2)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class MemoryToolStorageBackend(StrEnum):
    """Memory-tool storage backend (C-AS-13 §13.6 step 8)."""

    FILESYSTEM = "filesystem"
    S3 = "s3"
    DATABASE = "database"
    ENCRYPTED_FILESYSTEM = "encrypted_filesystem"
    OPERATOR_DEFINED = "operator_defined"


class GracefulDegradationPolicy(BaseModel):
    """Per-primitive Anthropic-API-outage degradation policy (C-AS-13 §13.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primitive: AnthropicPrimitive
    outage_behavior: OutageBehavior
    fallback_detail: str


class BatchApiCell(BaseModel):
    """A Batch API submission-cell binding (C-AS-13 §13.6 step 7).

    Minimal resolution — the workload-class cell at which Batch API is bound.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_class: WorkloadClass


class WorkloadManifestOverrides(BaseModel):
    """Operator overrides to the workload manifest (C-AS-13 §13.6).

    Minimal resolution — the one operator-override surface `compose_workload_
    binding_decision` consumes (the Memory-tool backend); further override
    fields are workload-binding-time discretion per §13.6.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_tool_backend: MemoryToolStorageBackend | None = None


class WorkloadBindingDecision(BaseModel):
    """A composed workload-binding-time decision (C-AS-13 §13.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload_class: WorkloadClass
    persona_tier: PersonaTier
    deployment_surface: DeploymentSurface
    per_primitive_adoption: Mapping[AnthropicPrimitive, AdoptionDepth]
    per_role_model_binding: Mapping[SubAgentRole, ModelBinding | None]
    extended_thinking_effort: Mapping[SubAgentRole, ExtendedThinkingEffort]
    batch_api_cells: tuple[BatchApiCell, ...]
    """The §13.6 step-7 `Set<BatchApiCell>` — materialized as a tuple of
    distinct cells (the codebase collection idiom for frozen-model sets)."""

    memory_tool_backend: MemoryToolStorageBackend | None


# §13.5 — per-primitive outage-behavior policy (11 rows).
_DEGRADATION_SPEC: dict[AnthropicPrimitive, tuple[OutageBehavior, str]] = {
    AnthropicPrimitive.SKILLS_SYSTEM: (
        OutageBehavior.CONTINUES,
        "SKILL.md filesystem residence is provider-independent",
    ),
    AnthropicPrimitive.MCP_AS_CODE: (
        OutageBehavior.CONTINUES,
        "MCP servers are provider-independent",
    ),
    AnthropicPrimitive.MANAGED_AGENTS: (
        OutageBehavior.FALLS_THROUGH_TO_HARNESS_OWNED_TOPOLOGY,
        "D4 patterns at the cell's workload-class row",
    ),
    AnthropicPrimitive.PER_ROLE_MODEL_BINDING: (
        OutageBehavior.C6_CROSS_FAMILY_FALLBACK,
        "C6 cross-family fallback per F1",
    ),
    AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT: (
        OutageBehavior.CACHE_STATE_LOST,
        "cache state lost on cross-family fallback; warm-up restarts",
    ),
    AnthropicPrimitive.EXTENDED_THINKING_BUDGET: (
        OutageBehavior.RUNS_WITHOUT_PRIMITIVE,
        "Anthropic-only; cross-family runs without extended thinking",
    ),
    AnthropicPrimitive.BATCH_API: (
        OutageBehavior.IN_FLIGHT_RESUME_ON_RECOVERY,
        "Anthropic-only; in-flight resume on Anthropic recovery",
    ),
    AnthropicPrimitive.CLAUDE_CODE_HOOKS: (
        OutageBehavior.HARNESS_OWNED_HOOK_LIFECYCLE,
        "harness-owned hook lifecycle if implemented independently",
    ),
    AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION: (
        OutageBehavior.CONTINUES,
        "static-prefix content is provider-independent",
    ),
    AnthropicPrimitive.FILES_API: (
        OutageBehavior.CROSS_FAMILY_LOSES_REFERENCES,
        "cross-family loses file_id references; harness MAY pre-cache locally",
    ),
    AnthropicPrimitive.MEMORY_TOOL: (
        OutageBehavior.CROSS_FAMILY_COMPATIBLE_VIA_CLIENT_STORAGE,
        "cross-family-compatible via client-side storage",
    ),
}

#: Per-primitive Anthropic-API-outage degradation policy (C-AS-13 §13.5).
GRACEFUL_DEGRADATION_POLICY: Mapping[AnthropicPrimitive, GracefulDegradationPolicy] = (
    MappingProxyType(
        {
            primitive: GracefulDegradationPolicy(
                primitive=primitive,
                outage_behavior=behavior,
                fallback_detail=detail,
            )
            for primitive, (behavior, detail) in _DEGRADATION_SPEC.items()
        }
    )
)

#: The C6 cross-family fallback chain (C-AS-13 §13.5 row 4) — 5 ordered steps.
C6_CROSS_FAMILY_FALLBACK_CHAIN: tuple[tuple[Provider, ModelClass], ...] = (
    (Provider.ANTHROPIC, ModelClass.CLAUDE),
    (Provider.BEDROCK, ModelClass.CLAUDE_EQUIVALENT),
    (Provider.VERTEX, ModelClass.CLAUDE_EQUIVALENT),
    (Provider.OPENAI, ModelClass.GPT_CLASS),
    (Provider.OLLAMA, ModelClass.OLLAMA),
)

# §13.6 step 8 — per-deployment-surface Memory-tool storage backends.
_MEMORY_BACKENDS: dict[DeploymentSurface, frozenset[MemoryToolStorageBackend]] = {
    DeploymentSurface.LOCAL_DEVELOPMENT: frozenset(
        {MemoryToolStorageBackend.FILESYSTEM, MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM}
    ),
    DeploymentSurface.MANAGED_CLOUD: frozenset(
        {MemoryToolStorageBackend.S3, MemoryToolStorageBackend.DATABASE}
    ),
    # self-hosted-server: AC5 unspecified — the composable subset (Class 3).
    DeploymentSurface.SELF_HOSTED_SERVER: frozenset(
        {
            MemoryToolStorageBackend.FILESYSTEM,
            MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM,
            MemoryToolStorageBackend.DATABASE,
        }
    ),
}

# §13.6 step 6 — per-workload-class recommended extended-thinking effort (§13.2 row 6).
_WORKLOAD_EFFORT: dict[WorkloadClass, ExtendedThinkingEffort] = {
    WorkloadClass.SOFTWARE_ENGINEERING: ExtendedThinkingEffort.XHIGH,
    WorkloadClass.CONTENT_CREATION: ExtendedThinkingEffort.LOW,
    WorkloadClass.PIPELINE_AUTOMATION: ExtendedThinkingEffort.MEDIUM,
    WorkloadClass.RESEARCH: ExtendedThinkingEffort.HIGH,
}


def memory_tool_storage_backend(
    deployment_surface: DeploymentSurface,
) -> frozenset[MemoryToolStorageBackend]:
    """Return the admissible Memory-tool storage backends for a surface (§13.6).

    `local-development` → filesystem options; `managed-cloud` → cloud options
    (acceptance #5). Total over `DeploymentSurface`.
    """
    return _MEMORY_BACKENDS[deployment_surface]


def compose_workload_binding_decision(
    workload_class: WorkloadClass,
    persona_tier: PersonaTier,
    deployment_surface: DeploymentSurface,
    operator_overrides: WorkloadManifestOverrides,
) -> WorkloadBindingDecision:
    """Compose a workload-binding-time decision (C-AS-13 §13.6 8-step procedure).

    Materializes the computable steps of the §13.6 procedure: step 2 — per-
    primitive adoption depth via `adoption_depth` (U-AS-28); step 4 — per-role
    model binding via `model_binding` (U-AS-29); step 6 — per-role extended-
    thinking effort (the workload's §13.2-row-6 recommended effort); step 7 —
    Batch API cell binding; step 8 — Memory-tool backend (operator override, or
    `None` if unbound). Deterministic given inputs (acceptance #8).
    """
    per_primitive_adoption = {
        primitive: adoption_depth(primitive, workload_class).depth
        for primitive in AnthropicPrimitive
    }
    per_role_model_binding = {role: model_binding(workload_class, role) for role in SubAgentRole}
    effort = _WORKLOAD_EFFORT[workload_class]
    extended_thinking_effort = {role: effort for role in SubAgentRole}
    batch_cells: tuple[BatchApiCell, ...] = (
        (BatchApiCell(workload_class=workload_class),)
        if per_primitive_adoption[AnthropicPrimitive.BATCH_API]
        in (AdoptionDepth.REQUIRED, AdoptionDepth.RECOMMENDED)
        else ()
    )
    return WorkloadBindingDecision(
        workload_class=workload_class,
        persona_tier=persona_tier,
        deployment_surface=deployment_surface,
        per_primitive_adoption=MappingProxyType(per_primitive_adoption),
        per_role_model_binding=MappingProxyType(per_role_model_binding),
        extended_thinking_effort=MappingProxyType(extended_thinking_effort),
        batch_api_cells=batch_cells,
        memory_tool_backend=operator_overrides.memory_tool_backend,
    )
