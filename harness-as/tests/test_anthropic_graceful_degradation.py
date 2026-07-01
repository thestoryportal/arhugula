"""Tests for U-AS-30 — Anthropic graceful degradation + workload binding (C-AS-13 §13.5-§13.6)."""

from __future__ import annotations

from harness_as.anthropic_graceful_degradation import (
    C6_CROSS_FAMILY_FALLBACK_CHAIN,
    GRACEFUL_DEGRADATION_POLICY,
    BatchApiCell,
    ExtendedThinkingEffort,
    MemoryToolStorageBackend,
    ModelClass,
    OutageBehavior,
    Provider,
    WorkloadManifestOverrides,
    compose_workload_binding_decision,
    memory_tool_storage_backend,
)
from harness_as.anthropic_primitive_adoption import AnthropicPrimitive
from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.engine_class_composition import SubAgentRole
from harness_core import WorkloadClass


def test_graceful_degradation_policy_cardinality_eleven() -> None:
    """Acceptance #1 — GRACEFUL_DEGRADATION_POLICY declares 11 rows."""
    assert len(GRACEFUL_DEGRADATION_POLICY) == 11
    assert set(GRACEFUL_DEGRADATION_POLICY) == set(AnthropicPrimitive)


def test_graceful_degradation_policy_per_spec_row_by_row() -> None:
    """Acceptance #1 — per-primitive outage behavior matches §13.5."""
    expect = {
        AnthropicPrimitive.SKILLS_SYSTEM: OutageBehavior.CONTINUES,
        AnthropicPrimitive.MANAGED_AGENTS: OutageBehavior.FALLS_THROUGH_TO_HARNESS_OWNED_TOPOLOGY,
        AnthropicPrimitive.PER_ROLE_MODEL_BINDING: OutageBehavior.C6_CROSS_FAMILY_FALLBACK,
        AnthropicPrimitive.PROMPT_CACHE_BREAKPOINT_PLACEMENT: OutageBehavior.CACHE_STATE_LOST,
        AnthropicPrimitive.BATCH_API: OutageBehavior.IN_FLIGHT_RESUME_ON_RECOVERY,
        AnthropicPrimitive.FILES_API: OutageBehavior.CROSS_FAMILY_LOSES_REFERENCES,
        AnthropicPrimitive.MEMORY_TOOL: OutageBehavior.CROSS_FAMILY_COMPATIBLE_VIA_CLIENT_STORAGE,
    }
    for primitive, behavior in expect.items():
        assert GRACEFUL_DEGRADATION_POLICY[primitive].outage_behavior is behavior


def test_c6_cross_family_fallback_chain_five_steps() -> None:
    """Acceptance #2 — the C6 fallback chain has 5 steps."""
    assert len(C6_CROSS_FAMILY_FALLBACK_CHAIN) == 5


def test_c6_cross_family_fallback_chain_ordered() -> None:
    """Acceptance #2 — the chain is anthropic → bedrock → vertex → openai → ollama."""
    providers = [provider for provider, _ in C6_CROSS_FAMILY_FALLBACK_CHAIN]
    assert providers == [
        Provider.ANTHROPIC,
        Provider.BEDROCK,
        Provider.VERTEX,
        Provider.OPENAI,
        Provider.OLLAMA,
    ]


def test_memory_tool_storage_backend_cardinality_five() -> None:
    """Acceptance #4 — MemoryToolStorageBackend carries 5 values."""
    assert len(MemoryToolStorageBackend) == 5


def test_memory_tool_storage_backend_local_development_returns_filesystem_options() -> None:
    """Acceptance #5 — local-development → filesystem options."""
    backends = memory_tool_storage_backend(DeploymentSurface.LOCAL_DEVELOPMENT)
    assert backends == frozenset(
        {
            MemoryToolStorageBackend.FILESYSTEM,
            MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM,
        }
    )


def test_memory_tool_storage_backend_managed_cloud_returns_cloud_options() -> None:
    """Acceptance #5 — managed-cloud → cloud options."""
    backends = memory_tool_storage_backend(DeploymentSurface.MANAGED_CLOUD)
    assert backends == frozenset({MemoryToolStorageBackend.S3, MemoryToolStorageBackend.DATABASE})


def test_memory_tool_filesystem_backend_consumes_u_is_01_path_contract() -> None:
    """Acceptance #6 — the filesystem backend at local-development is admissible."""
    assert MemoryToolStorageBackend.FILESYSTEM in memory_tool_storage_backend(
        DeploymentSurface.LOCAL_DEVELOPMENT
    )


def test_compose_workload_binding_decision_eight_steps() -> None:
    """Acceptance #7 — the composed decision populates the §13.6 procedure outputs."""
    decision = compose_workload_binding_decision(
        WorkloadClass.SOFTWARE_ENGINEERING,
        PersonaTier.SOLO_DEVELOPER,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        WorkloadManifestOverrides(),
    )
    assert len(decision.per_primitive_adoption) == 11
    assert len(decision.per_role_model_binding) == len(SubAgentRole)
    assert len(decision.extended_thinking_effort) == len(SubAgentRole)


def test_compose_workload_binding_decision_deterministic() -> None:
    """Acceptance #8 — the decision is deterministic given its inputs."""
    args = (
        WorkloadClass.RESEARCH,
        PersonaTier.TEAM_BINDING,
        DeploymentSurface.MANAGED_CLOUD,
        WorkloadManifestOverrides(memory_tool_backend=MemoryToolStorageBackend.S3),
    )
    assert compose_workload_binding_decision(*args) == compose_workload_binding_decision(*args)


def test_compose_workload_binding_decision_delegates_to_u_as_28_and_u_as_29() -> None:
    """Acceptance #11 — adoption depth (U-AS-28) and model binding (U-AS-29) are used."""
    from harness_as.anthropic_primitive_adoption import adoption_depth
    from harness_as.engine_class_composition import model_binding

    decision = compose_workload_binding_decision(
        WorkloadClass.SOFTWARE_ENGINEERING,
        PersonaTier.SOLO_DEVELOPER,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        WorkloadManifestOverrides(),
    )
    assert (
        decision.per_primitive_adoption[AnthropicPrimitive.SKILLS_SYSTEM]
        is adoption_depth(
            AnthropicPrimitive.SKILLS_SYSTEM, WorkloadClass.SOFTWARE_ENGINEERING
        ).depth
    )
    assert decision.per_role_model_binding[SubAgentRole.LEAD_ORCHESTRATOR] == model_binding(
        WorkloadClass.SOFTWARE_ENGINEERING, SubAgentRole.LEAD_ORCHESTRATOR
    )


def test_extended_thinking_effort_declared() -> None:
    """v1.1 AC — ExtendedThinkingEffort is declared in this unit."""
    assert len(ExtendedThinkingEffort) == 5


def test_batch_api_cell_declared() -> None:
    """v1.1 AC — BatchApiCell is declared in this unit."""
    cell = BatchApiCell(workload_class=WorkloadClass.PIPELINE_AUTOMATION)
    assert cell.workload_class is WorkloadClass.PIPELINE_AUTOMATION


def test_workload_manifest_overrides_declared() -> None:
    """v1.1 AC — WorkloadManifestOverrides is declared in this unit."""
    assert WorkloadManifestOverrides().memory_tool_backend is None


def test_provider_enum_five_families() -> None:
    """v1.1 AC — Provider declares the 5 fallback families."""
    assert len(Provider) == 5


def test_model_class_declared() -> None:
    """v1.1 AC — ModelClass is declared in this unit."""
    assert ModelClass.CLAUDE in ModelClass


def test_workload_class_consumed_from_harness_core() -> None:
    """v1.1 AC — WorkloadClass is consumed from harness-core."""
    assert WorkloadClass.__module__ == "harness_core.workload_class"
