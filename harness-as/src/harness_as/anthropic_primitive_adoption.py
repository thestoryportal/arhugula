"""Eleven-primitive Anthropic-adoption-depth matrix — U-AS-28.

Implements C-AS-13 §13.1 (closed eleven-primitive enumeration) + §13.2
(per-primitive by workload-class adoption-depth matrix). Declares the
`AnthropicPrimitive` enum, the `AdoptionDepth` enum, the `ConfidenceTag` /
`AnchorCitation` carriers, the `AdoptionDepthBinding` record, the populated
`ANTHROPIC_PRIMITIVE_ANCHORS` map, the 44-cell `ADOPTION_DEPTH_MATRIX`, and the
`adoption_depth` / `skills_loads_from_filesystem_path` functions.

Authority: Implementation_Plan_Action_Surface_v1_2.md §5.1 U-AS-28 (body
canonical at v1.1 §5.3 — Pattern A3 conformance + Pattern B `AnchorCitation`
carrier + Q-R3-5 `WorkloadClass` re-home; v1 base body at
Implementation_Plan_Action_Surface_v1.md §2 U-AS-28); Spec_Action_Surface_v1.md
§13 C-AS-13; ADR-D3 v1.2 §1.1 + §1.2.

Pattern A3 (v1.1): §13.1 is a prose name table with no machine-identifier
column; the `AnthropicPrimitive` member identifiers are a Python-stack
naming-convention rendering (SCREAMING_SNAKE, one per §13.1 concept), NOT a
byte-exact transcription of a spec string set.

Cross-axis (Q-R3-5): `WorkloadClass` is consumed from `harness-core` (U-CP-00);
a local redeclaration would be a defect. The IS filesystem-path contract is
consumed read-only from `harness-is` (U-IS-01 / U-IS-02) per the
FILESYSTEM_PATH_CONTRACT_EXPORT seam (U-IS-17 manifest, C-IS-10 §10.4).

Surface-conditioned cells (materialization discretion): §13.2 has eight
surface-conditioned cells (the Managed Agents and Files API rows) whose
adoption depth varies by deployment surface. `AdoptionDepthBinding` carries one
`depth`; per the binding's `surface_qualifier` + `notes` fields, the `depth`
records the **managed-cloud reference-surface** adoption depth, `notes` carries
the verbatim §13.2 cell text, and `surface_qualifier` marks the deployment
surface at which the depth diverges (`local-development`). The full §13.2
content is preserved in `notes`; `depth` is the cell-level reference value.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from harness_core import WorkloadClass
from harness_is import PathClass, PathClassMetadata
from harness_is.path_class_registry import PATH_CLASS_REGISTRY
from pydantic import BaseModel, ConfigDict

from harness_as.discriminators import DeploymentSurface


class AnthropicPrimitive(StrEnum):
    """The 11 Anthropic-platform primitives (C-AS-13 §13.1).

    Closed enumeration — one member per §13.1 prose name-table concept.
    Adding a 12th requires a Workflow §4.1.2 Class-2 ADR-D3 revision. The
    member identifiers are a Python naming-convention rendering of the §13.1
    concept names (Pattern A3) — §13.1 has no machine-identifier column.
    """

    SKILLS_SYSTEM = "skills_system"
    MCP_AS_CODE = "mcp_as_code"
    MANAGED_AGENTS = "managed_agents"
    PER_ROLE_MODEL_BINDING = "per_role_model_binding"
    PROMPT_CACHE_BREAKPOINT_PLACEMENT = "prompt_cache_breakpoint_placement"
    EXTENDED_THINKING_BUDGET = "extended_thinking_budget"
    BATCH_API = "batch_api"
    CLAUDE_CODE_HOOKS = "claude_code_hooks"
    CLAUDE_MD_AGENTS_MD_CONVENTION = "claude_md_agents_md_convention"
    FILES_API = "files_api"
    MEMORY_TOOL = "memory_tool"


class AdoptionDepth(StrEnum):
    """Per-cell adoption-depth value (C-AS-13 §13.2).

    Closed at cardinality 4 — the §13.2 R / r / o / X legend.
    """

    REQUIRED = "REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    OPTIONAL = "OPTIONAL"
    EXCLUDED = "EXCLUDED"


class ConfidenceTag(StrEnum):
    """Anchor-citation confidence tag (C-AS-13 §13.1 anchor column)."""

    HIGH = "HIGH"
    MODERATE = "MODERATE"
    SPECULATIVE = "SPECULATIVE"


class AnchorCitation(BaseModel):
    """Anchor citation for an Anthropic primitive (C-AS-13 §13.1).

    Pattern B carrier declared in this unit as the `ANTHROPIC_PRIMITIVE_ANCHORS`
    map value type.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_identifier: str
    confidence_tag: ConfidenceTag


class AdoptionDepthBinding(BaseModel):
    """One cell of the per-primitive by workload-class matrix (C-AS-13 §13.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primitive: AnthropicPrimitive
    workload_class: WorkloadClass
    depth: AdoptionDepth
    """The reference-surface (managed-cloud) adoption depth for the cell."""

    surface_qualifier: DeploymentSurface | None = None
    """Deployment surface at which `depth` diverges; None if surface-uniform."""

    notes: str | None = None
    """Verbatim §13.2 cell text for surface-conditioned / annotated cells."""


# --- §13.1 anchor citations ------------------------------------------------

#: Per-primitive anchor citation per C-AS-13 §13.1. Confidence tag reflects the
#: §13.1 anchor column: primary Anthropic / specification sources are HIGH;
#: community-witness anchors (Claude Code hooks) are MODERATE.
ANTHROPIC_PRIMITIVE_ANCHORS: Mapping[AnthropicPrimitive, AnchorCitation] = MappingProxyType(
    {
        AnthropicPrimitive.SKILLS_SYSTEM: AnchorCitation(
            source_identifier=(
                "platform.claude.com/docs/en/agents-and-tools/agent-skills/overview"
            ),
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.MCP_AS_CODE: AnchorCitation(
            source_identifier=(
                "modelcontextprotocol.io/specification/2025-06-18; "
                "anthropic.com/engineering/code-execution-with-mcp"
            ),
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.MANAGED_AGENTS: AnchorCitation(
            source_identifier="platform.claude.com/docs/en/managed-agents/overview",
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.PER_ROLE_MODEL_BINDING: AnchorCitation(
            source_identifier=(
                "Cluster 1 §[HIGH] Anthropic research system; "
                "Cluster 5 V2 §[HIGH] Q2 2026 rate card"
            ),
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT: AnchorCitation(
            source_identifier=(
                "Cluster 2 V2 §1.1 [HIGH]; "
                "platform.claude.com/docs/en/build-with-claude/prompt-caching"
            ),
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.EXTENDED_THINKING_BUDGET: AnchorCitation(
            source_identifier=(
                "Cluster 5 V2 §[HIGH]; "
                "platform.claude.com/docs/en/build-with-claude/extended-thinking"
            ),
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.BATCH_API: AnchorCitation(
            source_identifier=(
                "Cluster 5 V2 §[HIGH]; "
                "platform.claude.com/docs/en/build-with-claude/batch-processing"
            ),
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.CLAUDE_CODE_HOOKS: AnchorCitation(
            source_identifier=(
                "disler/body-of-work hooks-mastery; mindfold-ai/Trellis bracketed-hooks witness"
            ),
            confidence_tag=ConfidenceTag.MODERATE,
        ),
        AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION: AnchorCitation(
            source_identifier="Anthropic Claude Code convention",
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.FILES_API: AnchorCitation(
            source_identifier="platform.claude.com/docs/en/build-with-claude/files",
            confidence_tag=ConfidenceTag.HIGH,
        ),
        AnthropicPrimitive.MEMORY_TOOL: AnchorCitation(
            source_identifier=("docs.claude.com/en/docs/agents-and-tools/tool-use/memory-tool"),
            confidence_tag=ConfidenceTag.HIGH,
        ),
    }
)


# --- §13.2 adoption-depth matrix -------------------------------------------

_SE = WorkloadClass.SOFTWARE_ENGINEERING
_CC = WorkloadClass.CONTENT_CREATION
_PA = WorkloadClass.PIPELINE_AUTOMATION
_RE = WorkloadClass.RESEARCH

_R = AdoptionDepth.REQUIRED
_r = AdoptionDepth.RECOMMENDED
_o = AdoptionDepth.OPTIONAL
_LOCAL = DeploymentSurface.LOCAL_DEVELOPMENT

# Per-cell spec: (depth, surface_qualifier, notes) keyed by (primitive, workload).
# `depth` is the managed-cloud reference-surface value; `notes` carries the
# verbatim §13.2 cell text for surface-conditioned / annotated cells.
_MATRIX_SPEC: dict[
    tuple[AnthropicPrimitive, WorkloadClass],
    tuple[AdoptionDepth, DeploymentSurface | None, str | None],
] = {
    # Row 1 — Skills system: r / r / R / r
    (AnthropicPrimitive.SKILLS_SYSTEM, _SE): (_r, None, None),
    (AnthropicPrimitive.SKILLS_SYSTEM, _CC): (_r, None, None),
    (AnthropicPrimitive.SKILLS_SYSTEM, _PA): (_R, None, None),
    (AnthropicPrimitive.SKILLS_SYSTEM, _RE): (_r, None, None),
    # Row 2 — MCP-as-code: r / o / R / r
    (AnthropicPrimitive.MCP_AS_CODE, _SE): (_r, None, None),
    (AnthropicPrimitive.MCP_AS_CODE, _CC): (_o, None, None),
    (AnthropicPrimitive.MCP_AS_CODE, _PA): (_R, None, None),
    (AnthropicPrimitive.MCP_AS_CODE, _RE): (_r, None, None),
    # Row 3 — Managed Agents: surface-conditioned, X at local-development
    (AnthropicPrimitive.MANAGED_AGENTS, _SE): (
        _o,
        _LOCAL,
        "o at managed-cloud / hybrid; X at local-development",
    ),
    (AnthropicPrimitive.MANAGED_AGENTS, _CC): (
        _o,
        _LOCAL,
        "o at managed-cloud / hybrid; X at local-development",
    ),
    (AnthropicPrimitive.MANAGED_AGENTS, _PA): (
        _r,
        _LOCAL,
        "r at managed-cloud; o at hybrid; X at local-development",
    ),
    (AnthropicPrimitive.MANAGED_AGENTS, _RE): (
        _r,
        _LOCAL,
        "r at managed-cloud; o at hybrid; X at local-development",
    ),
    # Row 4 — Per-role model binding: R / R / R / R
    (AnthropicPrimitive.PER_ROLE_MODEL_BINDING, _SE): (_R, None, None),
    (AnthropicPrimitive.PER_ROLE_MODEL_BINDING, _CC): (_R, None, None),
    (AnthropicPrimitive.PER_ROLE_MODEL_BINDING, _PA): (_R, None, None),
    (AnthropicPrimitive.PER_ROLE_MODEL_BINDING, _RE): (_R, None, None),
    # Row 5 — Prompt-cache breakpoint placement: R / r / R / R
    (AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT, _SE): (_R, None, None),
    (AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT, _CC): (_r, None, None),
    (AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT, _PA): (_R, None, None),
    (AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT, _RE): (_R, None, None),
    # Row 6 — Extended-thinking budget: r / o / o / r with budget annotations
    (AnthropicPrimitive.EXTENDED_THINKING_BUDGET, _SE): (
        _r,
        None,
        "r (xhigh recommended)",
    ),
    (AnthropicPrimitive.EXTENDED_THINKING_BUDGET, _CC): (
        _o,
        None,
        "o (low if adopted)",
    ),
    (AnthropicPrimitive.EXTENDED_THINKING_BUDGET, _PA): (
        _o,
        None,
        "o (low to medium)",
    ),
    (AnthropicPrimitive.EXTENDED_THINKING_BUDGET, _RE): (
        _r,
        None,
        "r (high at orchestrator; low at Haiku siblings)",
    ),
    # Row 7 — Batch API: o / o / r / r
    (AnthropicPrimitive.BATCH_API, _SE): (_o, None, None),
    (AnthropicPrimitive.BATCH_API, _CC): (_o, None, None),
    (AnthropicPrimitive.BATCH_API, _PA): (_r, None, None),
    (AnthropicPrimitive.BATCH_API, _RE): (_r, None, None),
    # Row 8 — Claude Code hooks: r / o / r / o
    (AnthropicPrimitive.CLAUDE_CODE_HOOKS, _SE): (_r, None, None),
    (AnthropicPrimitive.CLAUDE_CODE_HOOKS, _CC): (_o, None, None),
    (AnthropicPrimitive.CLAUDE_CODE_HOOKS, _PA): (_r, None, None),
    (AnthropicPrimitive.CLAUDE_CODE_HOOKS, _RE): (_o, None, None),
    # Row 9 — claude.md / agents.md convention: r / r / r / r
    (AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION, _SE): (_r, None, None),
    (AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION, _CC): (_r, None, None),
    (AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION, _PA): (_r, None, None),
    (AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION, _RE): (_r, None, None),
    # Row 10 — Files API: surface-conditioned, o at local-development
    (AnthropicPrimitive.FILES_API, _SE): (
        _r,
        _LOCAL,
        "r at managed-cloud / hybrid; o at local-development",
    ),
    (AnthropicPrimitive.FILES_API, _CC): (
        _r,
        _LOCAL,
        "r at managed-cloud / hybrid; o at local-development",
    ),
    (AnthropicPrimitive.FILES_API, _PA): (
        _R,
        _LOCAL,
        "R at managed-cloud / hybrid; o at local-development",
    ),
    (AnthropicPrimitive.FILES_API, _RE): (
        _R,
        _LOCAL,
        "R at managed-cloud / hybrid; o at local-development",
    ),
    # Row 11 — Memory tool: per-workload selection, all surfaces
    (AnthropicPrimitive.MEMORY_TOOL, _SE): (
        _o,
        None,
        "per-workload selection; structurally available all surfaces (storage backend per §13.6)",
    ),
    (AnthropicPrimitive.MEMORY_TOOL, _CC): (
        _o,
        None,
        "per-workload selection; structurally available all surfaces (storage backend per §13.6)",
    ),
    (AnthropicPrimitive.MEMORY_TOOL, _PA): (
        _o,
        None,
        "per-workload selection; structurally available all surfaces (storage backend per §13.6)",
    ),
    (AnthropicPrimitive.MEMORY_TOOL, _RE): (
        _o,
        None,
        "per-workload selection; structurally available all surfaces (storage backend per §13.6)",
    ),
}

#: The 44-cell per-primitive by workload-class adoption-depth matrix (§13.2).
ADOPTION_DEPTH_MATRIX: Mapping[tuple[AnthropicPrimitive, WorkloadClass], AdoptionDepthBinding] = (
    MappingProxyType(
        {
            (primitive, workload): AdoptionDepthBinding(
                primitive=primitive,
                workload_class=workload,
                depth=depth,
                surface_qualifier=surface_qualifier,
                notes=notes,
            )
            for (primitive, workload), (
                depth,
                surface_qualifier,
                notes,
            ) in _MATRIX_SPEC.items()
        }
    )
)


def adoption_depth(
    primitive: AnthropicPrimitive, workload_class: WorkloadClass
) -> AdoptionDepthBinding:
    """Return the adoption-depth binding for a (primitive, workload-class) cell.

    Total over `(AnthropicPrimitive, WorkloadClass)` — the matrix has all 44
    cells populated (C-AS-13 §13.2).
    """
    return ADOPTION_DEPTH_MATRIX[(primitive, workload_class)]


def skills_loads_from_filesystem_path() -> PathClassMetadata:
    """Return the canonical filesystem path contract for the Skills primitive.

    Cross-axis read-only consumption (acceptance #7): the Skills system loads
    `SKILL.md` from the filesystem per the IS `SKILLS` path class (C-IS-01),
    carried by U-IS-01 / U-IS-02 and exported via the IS substrate seam
    FILESYSTEM_PATH_CONTRACT_EXPORT (U-IS-17 manifest, C-IS-10 §10.4). Skills
    artifacts reside in the `procedural` artifact tier per C-IS-02.
    """
    return PATH_CLASS_REGISTRY[PathClass.SKILLS]
