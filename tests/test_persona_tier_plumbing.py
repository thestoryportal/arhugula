"""Tests for OD-3 + OD-4 persona_tier plumbing per fork doc
`class_1_fork_od_3_od_4_retire_ready_persona_tier_plumbing.md` Reading (α)
operator-ratified 2026-05-28 (Q1=A + Q2=A + Q3=a + Q4=i + Q5=α).

Covers:
- `RuntimeConfig.persona_tier` field landing + default + 3-source resolution
- `materialize_tracer_provider_stage` reads `config.persona_tier` and resolves
  base_rate per OD spec §10.3 via `PER_CELL_BASE_RATE_ENVELOPE`
- `RedactionSpanProcessor` consumes `persona_tier` ctor param + refuses
  multi-tenant empty-redacted-attributes override per §13.1 row 3
- Excluded cell (multi-tenant × local-development) raises typed error at
  bootstrap rather than silent default
"""

from __future__ import annotations

import pytest
from harness_core import PersonaTier
from harness_core.deployment_surface import DeploymentSurface
from harness_od.observability_matrix import CellBindingViolation
from harness_od.redaction_span_processor import (
    MultiTenantOverrideRefusedError,
    RedactionSpanProcessor,
)


class TestRuntimeConfigPersonaTierField:
    """The `persona_tier` field landing at RuntimeConfig."""

    def test_default_is_solo_developer(self) -> None:
        """Default preserves MVP backward-compat (base_rate=1.0 at solo)."""
        # Construct minimal RuntimeConfig — defaults at all non-required fields
        from harness_runtime.config.otel_config import OTelConfig
        from harness_runtime.types import RuntimeConfig

        config = RuntimeConfig(
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            repository_root=__import__("pathlib").Path("/tmp"),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            default_topology=__import__(
                "harness_cp.topology_pattern", fromlist=["TopologyPattern"]
            ).TopologyPattern.SINGLE_THREADED_LINEAR,
        )
        assert config.persona_tier == PersonaTier.SOLO_DEVELOPER

    def test_explicit_team_binding_persisted(self) -> None:
        from harness_runtime.config.otel_config import OTelConfig
        from harness_runtime.types import RuntimeConfig

        config = RuntimeConfig(
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            repository_root=__import__("pathlib").Path("/tmp"),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            default_topology=__import__(
                "harness_cp.topology_pattern", fromlist=["TopologyPattern"]
            ).TopologyPattern.SINGLE_THREADED_LINEAR,
            persona_tier=PersonaTier.TEAM_BINDING,
        )
        assert config.persona_tier == PersonaTier.TEAM_BINDING

    def test_explicit_multi_tenant_compliance_persisted(self) -> None:
        from harness_runtime.config.otel_config import OTelConfig
        from harness_runtime.types import RuntimeConfig

        config = RuntimeConfig(
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
            repository_root=__import__("pathlib").Path("/tmp"),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            default_topology=__import__(
                "harness_cp.topology_pattern", fromlist=["TopologyPattern"]
            ).TopologyPattern.SINGLE_THREADED_LINEAR,
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        )
        assert config.persona_tier == PersonaTier.MULTI_TENANT_COMPLIANCE


class TestRedactionSpanProcessorPersonaTier:
    """RedactionSpanProcessor consumes persona_tier per §13.1 row gating."""

    def test_default_persona_tier_is_solo_developer(self) -> None:
        """Backward-compat default for tests + existing call sites."""
        processor = RedactionSpanProcessor()
        assert processor.persona_tier == PersonaTier.SOLO_DEVELOPER

    def test_explicit_team_binding_persisted(self) -> None:
        processor = RedactionSpanProcessor(persona_tier=PersonaTier.TEAM_BINDING)
        assert processor.persona_tier == PersonaTier.TEAM_BINDING

    def test_explicit_multi_tenant_compliance_persisted(self) -> None:
        processor = RedactionSpanProcessor(persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE)
        assert processor.persona_tier == PersonaTier.MULTI_TENANT_COMPLIANCE

    def test_solo_developer_permits_empty_redacted_attributes(self) -> None:
        """Solo-developer toggleable per §13.1 row 1 — operator override OK."""
        processor = RedactionSpanProcessor(
            persona_tier=PersonaTier.SOLO_DEVELOPER,
            redacted_attributes=frozenset(),
        )
        assert processor.redacted_attributes == frozenset()

    def test_team_binding_permits_empty_redacted_attributes(self) -> None:
        """Team-binding allows ctor override (per-session audit deferred)."""
        processor = RedactionSpanProcessor(
            persona_tier=PersonaTier.TEAM_BINDING,
            redacted_attributes=frozenset(),
        )
        assert processor.redacted_attributes == frozenset()

    def test_multi_tenant_refuses_empty_redacted_attributes(self) -> None:
        """§13.1 row 3 — operator cannot disable redaction at multi-tenant."""
        with pytest.raises(MultiTenantOverrideRefusedError) as exc_info:
            RedactionSpanProcessor(
                persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
                redacted_attributes=frozenset(),
            )
        assert "multi-tenant-compliance" in str(exc_info.value)
        assert "non-toggleable" in str(exc_info.value)

    def test_multi_tenant_accepts_default_redacted_attributes(self) -> None:
        """Multi-tenant + spec-canonical default is valid (strip-by-default)."""
        processor = RedactionSpanProcessor(persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE)
        assert len(processor.redacted_attributes) > 0
        assert processor.persona_tier == PersonaTier.MULTI_TENANT_COMPLIANCE

    def test_multi_tenant_accepts_non_empty_operator_set(self) -> None:
        """Multi-tenant accepts a non-empty operator-tuned set (e.g., wider)."""
        custom = frozenset({"gen_ai.input.messages"})
        processor = RedactionSpanProcessor(
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
            redacted_attributes=custom,
        )
        assert processor.redacted_attributes == custom


class TestTracerProviderPersonaTierBaseRate:
    """`materialize_tracer_provider_stage` reads persona_tier → §10.3 base_rate."""

    def _make_config(
        self,
        persona_tier: PersonaTier,
        deployment_surface: DeploymentSurface,
    ):
        from harness_runtime.config.otel_config import OTelConfig
        from harness_runtime.types import RuntimeConfig

        return RuntimeConfig(
            deployment_surface=deployment_surface,
            repository_root=__import__("pathlib").Path("/tmp"),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            default_topology=__import__(
                "harness_cp.topology_pattern", fromlist=["TopologyPattern"]
            ).TopologyPattern.SINGLE_THREADED_LINEAR,
            persona_tier=persona_tier,
        )

    def test_solo_developer_local_yields_base_rate_1_0(self) -> None:
        """§10.3 row 1: solo × local-dev = 1.0."""
        from harness_od.composite_sampler import HarnessCompositeSampler
        from harness_runtime.lifecycle.tracer_provider import (
            materialize_tracer_provider_stage,
            reset_runtime_registration_for_tests,
        )

        reset_runtime_registration_for_tests()
        config = self._make_config(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
        stage = materialize_tracer_provider_stage(config, register_globally=False)
        # ParentBased wraps HarnessCompositeSampler; reach inside via _root
        sampler = stage.provider.sampler
        # Unwrap ParentBased to get root sampler
        root = sampler._root  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]
        assert isinstance(root, HarnessCompositeSampler)
        assert root.base_rate == 1.0

    def test_team_binding_local_yields_base_rate_0_5(self) -> None:
        """§10.3 row 4: team × local-dev = 0.5."""
        from harness_od.composite_sampler import HarnessCompositeSampler
        from harness_runtime.lifecycle.tracer_provider import (
            materialize_tracer_provider_stage,
            reset_runtime_registration_for_tests,
        )

        reset_runtime_registration_for_tests()
        config = self._make_config(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT)
        stage = materialize_tracer_provider_stage(config, register_globally=False)
        root = stage.provider.sampler._root  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]
        assert isinstance(root, HarnessCompositeSampler)
        assert root.base_rate == 0.5

    def test_team_binding_self_hosted_yields_base_rate_0_1(self) -> None:
        """§10.3 row 5: team × self-hosted = 0.1."""
        from harness_od.composite_sampler import HarnessCompositeSampler
        from harness_runtime.lifecycle.tracer_provider import (
            materialize_tracer_provider_stage,
            reset_runtime_registration_for_tests,
        )

        reset_runtime_registration_for_tests()
        config = self._make_config(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
        stage = materialize_tracer_provider_stage(config, register_globally=False)
        root = stage.provider.sampler._root  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]
        assert isinstance(root, HarnessCompositeSampler)
        assert root.base_rate == 0.1

    def test_multi_tenant_managed_cloud_yields_base_rate_0_2(self) -> None:
        """§10.3 row 8: multi-tenant × managed-cloud = 0.2."""
        from harness_od.composite_sampler import HarnessCompositeSampler
        from harness_runtime.lifecycle.tracer_provider import (
            materialize_tracer_provider_stage,
            reset_runtime_registration_for_tests,
        )

        reset_runtime_registration_for_tests()
        config = self._make_config(
            PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD
        )
        stage = materialize_tracer_provider_stage(config, register_globally=False)
        root = stage.provider.sampler._root  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]
        assert isinstance(root, HarnessCompositeSampler)
        assert root.base_rate == 0.2

    def test_excluded_cell_multi_tenant_local_dev_raises(self) -> None:
        """§10.3 has NO row for multi-tenant × local-dev → typed error."""
        from harness_runtime.lifecycle.tracer_provider import (
            TracerProviderBindError,
            materialize_tracer_provider_stage,
            reset_runtime_registration_for_tests,
        )

        reset_runtime_registration_for_tests()
        config = self._make_config(
            PersonaTier.MULTI_TENANT_COMPLIANCE,
            DeploymentSurface.LOCAL_DEVELOPMENT,
        )
        # `reject_excluded_cell` raises CellBindingViolation; the composer
        # wraps unexpected exceptions in TracerProviderBindError per its
        # try/except contract
        with pytest.raises(TracerProviderBindError) as exc_info:
            materialize_tracer_provider_stage(config, register_globally=False)
        # Verify root cause was the excluded-cell rejection
        assert isinstance(exc_info.value.__cause__, CellBindingViolation)
