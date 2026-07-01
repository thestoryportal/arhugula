"""Tests for U-AS-28 — eleven-primitive adoption-depth matrix (C-AS-13 §13.1-§13.2)."""

from __future__ import annotations

from harness_as.anthropic_primitive_adoption import (
    ADOPTION_DEPTH_MATRIX,
    ANTHROPIC_PRIMITIVE_ANCHORS,
    AdoptionDepth,
    AdoptionDepthBinding,
    AnchorCitation,
    AnthropicPrimitive,
    ConfidenceTag,
    adoption_depth,
    skills_loads_from_filesystem_path,
)
from harness_as.discriminators import DeploymentSurface
from harness_core import WorkloadClass
from harness_is import PathClass, PathClassMetadata

# Expected reference-surface depth per §13.2 cell, ordered SE / CC / PA / RE.
_EXPECTED_ROWS: dict[AnthropicPrimitive, tuple[AdoptionDepth, ...]] = {
    AnthropicPrimitive.SKILLS_SYSTEM: (
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.REQUIRED,
        AdoptionDepth.RECOMMENDED,
    ),
    AnthropicPrimitive.MCP_AS_CODE: (
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.REQUIRED,
        AdoptionDepth.RECOMMENDED,
    ),
    AnthropicPrimitive.MANAGED_AGENTS: (
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
    ),
    AnthropicPrimitive.PER_ROLE_MODEL_BINDING: (
        AdoptionDepth.REQUIRED,
        AdoptionDepth.REQUIRED,
        AdoptionDepth.REQUIRED,
        AdoptionDepth.REQUIRED,
    ),
    AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT: (
        AdoptionDepth.REQUIRED,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.REQUIRED,
        AdoptionDepth.REQUIRED,
    ),
    AnthropicPrimitive.EXTENDED_THINKING_BUDGET: (
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.RECOMMENDED,
    ),
    AnthropicPrimitive.BATCH_API: (
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
    ),
    AnthropicPrimitive.CLAUDE_CODE_HOOKS: (
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.OPTIONAL,
    ),
    AnthropicPrimitive.CLAUDE_MD_AGENTS_MD_CONVENTION: (
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
    ),
    AnthropicPrimitive.FILES_API: (
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.RECOMMENDED,
        AdoptionDepth.REQUIRED,
        AdoptionDepth.REQUIRED,
    ),
    AnthropicPrimitive.MEMORY_TOOL: (
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.OPTIONAL,
        AdoptionDepth.OPTIONAL,
    ),
}

_WORKLOAD_ORDER = (
    WorkloadClass.SOFTWARE_ENGINEERING,
    WorkloadClass.CONTENT_CREATION,
    WorkloadClass.PIPELINE_AUTOMATION,
    WorkloadClass.RESEARCH,
)


def test_anthropic_primitive_cardinality_eleven() -> None:
    """Acceptance #1 — AnthropicPrimitive declares exactly 11 members."""
    assert len(AnthropicPrimitive) == 11


def test_anthropic_primitive_cardinality_eleven_one_per_spec_13_1_concept() -> None:
    """Acceptance #1 (Pattern A3) — 11 members, one per §13.1 prose concept."""
    assert len(set(AnthropicPrimitive)) == 11
    # Concept coverage — the §13.1 name-table concepts each have a member.
    assert AnthropicPrimitive.SKILLS_SYSTEM in AnthropicPrimitive
    assert AnthropicPrimitive.MEMORY_TOOL in AnthropicPrimitive


def test_workload_class_cardinality_four() -> None:
    """Acceptance #2 — WorkloadClass declares exactly 4 values."""
    assert len(WorkloadClass) == 4


def test_adoption_depth_cardinality_four() -> None:
    """Acceptance #3 — AdoptionDepth declares exactly 4 values (R/r/o/X)."""
    assert len(AdoptionDepth) == 4


def test_anthropic_primitive_anchors_complete() -> None:
    """Acceptance #4 — 11 anchor-citation entries, one per primitive.

    Each entry is a primary-source citation carrying a §13.1 confidence tag.
    The §13.1 anchor column [HIGH]-tags primary Anthropic / specification
    sources; the one community-witness anchor (Claude Code hooks) is MODERATE.
    AC4 does not require all-HIGH — `ConfidenceTag` is 3-valued by design.
    """
    assert len(ANTHROPIC_PRIMITIVE_ANCHORS) == 11
    for primitive in AnthropicPrimitive:
        anchor = ANTHROPIC_PRIMITIVE_ANCHORS[primitive]
        assert isinstance(anchor, AnchorCitation)
        assert anchor.source_identifier
        assert anchor.confidence_tag in ConfidenceTag
    # The §13.1 [HIGH]-marked primitives carry the HIGH confidence tag.
    high_marked = {
        AnthropicPrimitive.SKILLS_SYSTEM,
        AnthropicPrimitive.MCP_AS_CODE,
        AnthropicPrimitive.MANAGED_AGENTS,
        AnthropicPrimitive.PER_ROLE_MODEL_BINDING,
        AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT,
        AnthropicPrimitive.EXTENDED_THINKING_BUDGET,
        AnthropicPrimitive.BATCH_API,
        AnthropicPrimitive.FILES_API,
        AnthropicPrimitive.MEMORY_TOOL,
    }
    for primitive in high_marked:
        assert ANTHROPIC_PRIMITIVE_ANCHORS[primitive].confidence_tag is ConfidenceTag.HIGH
    # The community-witness anchor (Claude Code hooks) is MODERATE, not HIGH.
    assert (
        ANTHROPIC_PRIMITIVE_ANCHORS[AnthropicPrimitive.CLAUDE_CODE_HOOKS].confidence_tag
        is ConfidenceTag.MODERATE
    )


def test_adoption_depth_matrix_cardinality_44() -> None:
    """Acceptance #5 — ADOPTION_DEPTH_MATRIX declares exactly 44 cells."""
    assert len(ADOPTION_DEPTH_MATRIX) == 44
    assert len(AnthropicPrimitive) * len(WorkloadClass) == 44


def test_adoption_depth_matrix_row_by_row_per_spec_13_2() -> None:
    """Acceptance #6 — per-cell depth + verbatim notes match §13.2 row-by-row."""
    for primitive, expected in _EXPECTED_ROWS.items():
        for workload, expected_depth in zip(_WORKLOAD_ORDER, expected, strict=True):
            binding = ADOPTION_DEPTH_MATRIX[(primitive, workload)]
            assert binding.depth is expected_depth
    # Surface-conditioned rows carry verbatim §13.2 cell text in `notes`.
    assert (
        ADOPTION_DEPTH_MATRIX[
            (AnthropicPrimitive.MANAGED_AGENTS, WorkloadClass.PIPELINE_AUTOMATION)
        ].notes
        == "r at managed-cloud; o at hybrid; X at local-development"
    )
    assert (
        ADOPTION_DEPTH_MATRIX[
            (AnthropicPrimitive.FILES_API, WorkloadClass.SOFTWARE_ENGINEERING)
        ].notes
        == "r at managed-cloud / hybrid; o at local-development"
    )


def test_managed_agents_excluded_at_local_development() -> None:
    """Acceptance #6 — Managed Agents is surface-conditioned X at local-development."""
    for workload in WorkloadClass:
        binding = ADOPTION_DEPTH_MATRIX[(AnthropicPrimitive.MANAGED_AGENTS, workload)]
        assert binding.surface_qualifier is DeploymentSurface.LOCAL_DEVELOPMENT
        assert binding.notes is not None
        assert "X at local-development" in binding.notes


def test_per_role_model_binding_required_all_workloads() -> None:
    """Acceptance #6 — Per-role model binding is REQUIRED across all workload classes."""
    for workload in WorkloadClass:
        binding = ADOPTION_DEPTH_MATRIX[(AnthropicPrimitive.PER_ROLE_MODEL_BINDING, workload)]
        assert binding.depth is AdoptionDepth.REQUIRED


def test_skills_loads_from_filesystem_via_u_is_01_and_u_is_02() -> None:
    """Acceptance #7 — skills_loads_from_filesystem_path returns the IS SKILLS contract."""
    contract = skills_loads_from_filesystem_path()
    assert isinstance(contract, PathClassMetadata)
    assert contract.path_class is PathClass.SKILLS


def test_adoption_depth_total_function() -> None:
    """Acceptance #8 — adoption_depth is total over (AnthropicPrimitive, WorkloadClass)."""
    for primitive in AnthropicPrimitive:
        for workload in WorkloadClass:
            binding = adoption_depth(primitive, workload)
            assert isinstance(binding, AdoptionDepthBinding)
            assert binding.primitive is primitive
            assert binding.workload_class is workload


def test_anchor_citation_declared() -> None:
    """v1.1 AC — AnchorCitation is declared as the ANTHROPIC_PRIMITIVE_ANCHORS value type."""
    assert set(AnchorCitation.model_fields) == {"source_identifier", "confidence_tag"}


def test_workload_class_consumed_from_harness_core() -> None:
    """v1.1 AC — WorkloadClass is consumed from harness-core; no local redeclaration."""
    assert WorkloadClass.__module__ == "harness_core.workload_class"
