"""U-RT-27 — TracerProvider construction + global registration tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L6 U-RT-27:
  #1 `get_tracer_provider()` returns the registered provider in-process.
     -> test_materialize_with_register_globally_makes_provider_globally_visible
  #2 resource carries deployment-surface attrs.
     -> test_provider_resource_carries_deployment_surface_attr
     -> test_provider_resource_carries_namespace_declared_attrs
     -> test_provider_resource_honors_additional_resource_attrs

Plus C-RT-06 invariant + composer plumbing tests:
  -> test_materialize_returns_stage_with_provider
  -> test_materialize_without_register_globally_does_not_set_global
  -> test_double_runtime_registration_raises_concurrent_error
  -> test_tracer_provider_stage_is_frozen
  -> test_provider_uses_supplied_sampler_when_passed
  -> test_provider_constructed_for_all_deployment_surfaces

Test isolation: every test calls `reset_runtime_registration_for_tests`
before any materialize call so the per-process runtime-registration flag
starts fresh. The OTel SDK's own global is NOT reset between tests (it's
one-shot per process); we sidestep this by using `register_globally=False`
for tests that don't need to verify the global registration AC.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.tracer_provider import (
    TracerProviderConcurrentRegistrationError,
    TracerProviderStage,
    materialize_tracer_provider_stage,
    reset_runtime_registration_for_tests,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from opentelemetry import trace as ot_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_runtime_registration() -> None:
    """Reset the per-process runtime-registration flag for each test."""
    reset_runtime_registration_for_tests()


def _config(
    tmp_path: Path,
    *,
    deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
    persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER,
    tenant_id: str | None = None,
    additional_attrs: tuple[tuple[str, str], ...] = (),
) -> RuntimeConfig:
    """Build a minimal `RuntimeConfig` for materialize tests."""
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        tenant_id=tenant_id,
        persona_tier=persona_tier,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(
            otlp_endpoint="http://localhost:4317",
            additional_resource_attrs=additional_attrs,
        ),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


# ---------------------------------------------------------------------------
# AC #1 — get_tracer_provider() returns the registered provider in-process.
# ---------------------------------------------------------------------------


def test_materialize_with_register_globally_makes_provider_globally_visible(
    tmp_path: Path,
) -> None:
    """After `materialize_tracer_provider_stage(config, register_globally=True)`,
    `opentelemetry.trace.get_tracer_provider()` returns the constructed
    provider (the global registration step landed)."""
    stage = materialize_tracer_provider_stage(_config(tmp_path))
    assert stage.registered_globally is True
    # OTel global registration is one-shot — this assertion only holds when
    # the test session has not already registered a different provider.
    assert ot_trace.get_tracer_provider() is stage.provider


# ---------------------------------------------------------------------------
# AC #2 — resource carries deployment-surface attrs.
# ---------------------------------------------------------------------------


def test_provider_resource_carries_deployment_surface_attr(tmp_path: Path) -> None:
    """The provider's `Resource` carries a `deployment.surface` attribute
    whose value matches `config.deployment_surface.value`."""
    stage = materialize_tracer_provider_stage(
        _config(tmp_path, deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER),
        register_globally=False,
    )
    attrs = stage.provider.resource.attributes
    assert attrs.get("deployment.surface") == DeploymentSurface.SELF_HOSTED_SERVER.value


def test_provider_resource_carries_namespace_declared_attrs(tmp_path: Path) -> None:
    """The 15-row OD namespace map surfaces as `namespace.*` declared
    attributes on the resource (C-OD-05 §5.1 declaration-coverage attestation).

    Note: `build_resource_attributes` (U-RT-07) emits `namespace.<prefix>declared`
    — one entry per `NAMESPACE_MAP` row. Most prefixes carry a trailing `.`
    (e.g. `anthropic.`), producing `namespace.anthropic.declared`. One row
    (`provider_discriminator`) has no trailing `.`, producing
    `namespace.provider_discriminatordeclared`. Total is 15 either way.
    """
    stage = materialize_tracer_provider_stage(_config(tmp_path), register_globally=False)
    attrs = stage.provider.resource.attributes
    declared_keys = [k for k in attrs if k.startswith("namespace.")]
    # The 15-row OD namespace map produces 15 namespace.* attributes.
    assert len(declared_keys) == 15
    for value in (attrs[k] for k in declared_keys):
        assert value == "true"


def test_provider_resource_honors_additional_resource_attrs(tmp_path: Path) -> None:
    """`additional_resource_attrs` operator-supplied entries override + extend
    the default attribute set (kwargs > defaults per `build_resource_attributes`)."""
    extra: tuple[tuple[str, str], ...] = (
        ("service.name", "test-harness"),
        ("service.version", "0.1.0-test"),
    )
    stage = materialize_tracer_provider_stage(
        _config(tmp_path, additional_attrs=extra), register_globally=False
    )
    attrs = stage.provider.resource.attributes
    assert attrs.get("service.name") == "test-harness"
    assert attrs.get("service.version") == "0.1.0-test"


def test_provider_resource_carries_runtime_tenant_id(tmp_path: Path) -> None:
    """A configured tenant is emitted as the C-OD-21 `tenant.id` resource attr."""
    stage = materialize_tracer_provider_stage(
        _config(
            tmp_path,
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
            tenant_id="tenant-a",
        ),
        register_globally=False,
    )
    attrs = stage.provider.resource.attributes
    assert attrs.get("tenant.id") == "tenant-a"


def test_provider_uses_multi_tenant_self_hosted_base_rate(tmp_path: Path) -> None:
    """MTC x SELF_HOSTED materialization binds the §10.3 default base_rate."""
    stage = materialize_tracer_provider_stage(
        _config(
            tmp_path,
            deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
            tenant_id="tenant-a",
        ),
        register_globally=False,
    )
    assert "base_rate=0.2" in stage.provider.sampler.get_description()


# ---------------------------------------------------------------------------
# C-RT-06 invariant — set_tracer_provider exactly once per process.
# ---------------------------------------------------------------------------


def test_double_runtime_registration_raises_concurrent_error(
    tmp_path: Path,
) -> None:
    """A second runtime-driven `set_tracer_provider(...)` call raises
    `TracerProviderConcurrentRegistrationError` (C-RT-14 + C-RT-06 invariant)."""
    materialize_tracer_provider_stage(_config(tmp_path))
    with pytest.raises(TracerProviderConcurrentRegistrationError):
        materialize_tracer_provider_stage(_config(tmp_path))


# ---------------------------------------------------------------------------
# Composer plumbing + invariants.
# ---------------------------------------------------------------------------


def test_materialize_returns_stage_with_provider(tmp_path: Path) -> None:
    stage = materialize_tracer_provider_stage(_config(tmp_path), register_globally=False)
    assert isinstance(stage, TracerProviderStage)
    assert isinstance(stage.provider, TracerProvider)


def test_materialize_without_register_globally_does_not_set_global(
    tmp_path: Path,
) -> None:
    """`register_globally=False` builds the provider but skips the global
    registration step (`registered_globally=False`)."""
    stage = materialize_tracer_provider_stage(_config(tmp_path), register_globally=False)
    assert stage.registered_globally is False


def test_tracer_provider_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_tracer_provider_stage(_config(tmp_path), register_globally=False)
    with pytest.raises(FrozenInstanceError):
        stage.registered_globally = True  # type: ignore[misc]


def test_provider_uses_supplied_sampler_when_passed(tmp_path: Path) -> None:
    """The composer's `sampler` keyword threads through to the constructed
    provider — verified by passing `ALWAYS_OFF` and asserting on the
    provider's sampler attribute."""
    stage = materialize_tracer_provider_stage(
        _config(tmp_path), register_globally=False, sampler=ALWAYS_OFF
    )
    assert stage.provider.sampler is ALWAYS_OFF


def test_provider_constructed_for_all_deployment_surfaces(tmp_path: Path) -> None:
    """Each `DeploymentSurface` produces a valid provider; the resource's
    `deployment.surface` attribute matches the surface."""
    for surface in DeploymentSurface:
        stage = materialize_tracer_provider_stage(
            _config(tmp_path, deployment_surface=surface),
            register_globally=False,
        )
        assert stage.provider.resource.attributes.get("deployment.surface") == surface.value
