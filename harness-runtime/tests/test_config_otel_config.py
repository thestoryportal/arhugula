"""U-RT-07 — `OTelConfig` + resource-attr derivation tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L1:
- Endpoint URL validates.
- Resource attrs include required namespace tags (ADR-D6 v1.2 §1.2).
"""

from __future__ import annotations

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_od.namespace_map import NAMESPACE_MAP
from harness_od.sampling_mode import PER_DEPLOYMENT_SURFACE_SAMPLING, SamplingMode
from harness_runtime.config.otel_config import (
    build_resource_attributes,
    resolve_sampling_mode,
)
from harness_runtime.types import OTelConfig
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Endpoint URL validation (plan AC).
# ---------------------------------------------------------------------------


def test_endpoint_http_valid() -> None:
    """HTTP OTLP endpoint validates."""
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    assert cfg.otlp_endpoint == "http://localhost:4318"


def test_endpoint_https_valid() -> None:
    """HTTPS OTLP endpoint validates."""
    cfg = OTelConfig(otlp_endpoint="https://collector.example.com:4318")
    assert cfg.otlp_endpoint == "https://collector.example.com:4318"


def test_endpoint_grpc_valid() -> None:
    """gRPC OTLP endpoint validates (`://` scheme present)."""
    cfg = OTelConfig(otlp_endpoint="grpc://localhost:4317")
    assert cfg.otlp_endpoint == "grpc://localhost:4317"


def test_endpoint_without_scheme_rejected() -> None:
    """`localhost:4318` lacks `://` and is rejected."""
    with pytest.raises(ValidationError):
        OTelConfig(otlp_endpoint="localhost:4318")


def test_endpoint_empty_rejected() -> None:
    """Empty endpoint string is rejected."""
    with pytest.raises(ValidationError):
        OTelConfig(otlp_endpoint="")


# ---------------------------------------------------------------------------
# Sampling-mode resolution (per-deployment-surface default + override).
# ---------------------------------------------------------------------------


def test_sampling_mode_defaults_per_deployment_surface() -> None:
    """`sampling_mode=None` → per-deployment-surface default (C-OD-09 §9.1)."""
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    for surface in DeploymentSurface:
        resolved = resolve_sampling_mode(cfg, surface)
        assert resolved is PER_DEPLOYMENT_SURFACE_SAMPLING[surface]


def test_sampling_mode_override_wins() -> None:
    """Explicit `sampling_mode` override beats the surface default."""
    cfg = OTelConfig(
        otlp_endpoint="http://localhost:4318",
        sampling_mode=SamplingMode.TAIL_BASED_PROD,
    )
    # LOCAL_DEVELOPMENT defaults to HEAD_BASED_DEV; override flips it.
    resolved = resolve_sampling_mode(cfg, DeploymentSurface.LOCAL_DEVELOPMENT)
    assert resolved is SamplingMode.TAIL_BASED_PROD


# ---------------------------------------------------------------------------
# Resource attributes (plan AC).
# ---------------------------------------------------------------------------


def test_resource_attrs_include_deployment_surface() -> None:
    """`deployment.surface` resource key carries the surface value."""
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    attrs = build_resource_attributes(cfg, DeploymentSurface.MANAGED_CLOUD)
    assert attrs["deployment.surface"] == "managed-cloud"


def test_resource_attrs_include_all_namespace_declarations() -> None:
    """Every row of the 15-row `NAMESPACE_MAP` gets a `declared=true` attr.

    Plan §2 L1 AC: 'resource attrs include required 12-namespace tags' —
    we attest the full 15-row map (ADR-D6 v1.2 §1.2 is the 12 ingested-at-OD
    namespaces; the OD 15-row map is the full set including the 3 OD-canonical
    + substrate-anchored prefixes).
    """
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    attrs = build_resource_attributes(cfg, DeploymentSurface.LOCAL_DEVELOPMENT)
    for row in NAMESPACE_MAP:
        key = f"namespace.{row.namespace_prefix}declared"
        assert attrs[key] == "true", f"missing namespace attestation: {key}"


def test_resource_attrs_include_operator_additions() -> None:
    """`additional_resource_attrs` entries appear in the output."""
    cfg = OTelConfig(
        otlp_endpoint="http://localhost:4318",
        additional_resource_attrs=(
            ("service.name", "harness-runtime-test"),
            ("service.version", "0.0.0"),
        ),
    )
    attrs = build_resource_attributes(cfg, DeploymentSurface.LOCAL_DEVELOPMENT)
    assert attrs["service.name"] == "harness-runtime-test"
    assert attrs["service.version"] == "0.0.0"


def test_resource_attrs_include_tenant_id_when_present() -> None:
    """`tenant.id` carries the deployment tenant key for multi-tenant cells."""
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    attrs = build_resource_attributes(
        cfg,
        DeploymentSurface.SELF_HOSTED_SERVER,
        tenant_id="tenant-a",
    )
    assert attrs["tenant.id"] == "tenant-a"


def test_resource_attrs_omit_tenant_id_when_absent() -> None:
    """Single-tenant deployments do not emit a synthetic `tenant.id`."""
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    attrs = build_resource_attributes(cfg, DeploymentSurface.SELF_HOSTED_SERVER)
    assert "tenant.id" not in attrs


def test_operator_additions_override_defaults() -> None:
    """Operator-supplied `deployment.surface` overrides the derived default."""
    cfg = OTelConfig(
        otlp_endpoint="http://localhost:4318",
        additional_resource_attrs=(("deployment.surface", "operator-override"),),
    )
    attrs = build_resource_attributes(cfg, DeploymentSurface.LOCAL_DEVELOPMENT)
    assert attrs["deployment.surface"] == "operator-override"


def test_runtime_tenant_id_overrides_operator_tenant_attr() -> None:
    """`RuntimeConfig.tenant_id` is authoritative over extra resource attrs."""
    cfg = OTelConfig(
        otlp_endpoint="http://localhost:4318",
        additional_resource_attrs=(("tenant.id", "operator-override"),),
    )
    attrs = build_resource_attributes(
        cfg,
        DeploymentSurface.SELF_HOSTED_SERVER,
        tenant_id="runtime-tenant",
    )
    assert attrs["tenant.id"] == "runtime-tenant"


def test_resource_attrs_row_count() -> None:
    """Total rows = 1 (deployment.surface) + len(NAMESPACE_MAP) + 0 additional."""
    cfg = OTelConfig(otlp_endpoint="http://localhost:4318")
    attrs = build_resource_attributes(cfg, DeploymentSurface.LOCAL_DEVELOPMENT)
    expected = 1 + len(NAMESPACE_MAP)
    assert len(attrs) == expected, f"got {len(attrs)} attrs, expected {expected}"


# ---------------------------------------------------------------------------
# Config invariants.
# ---------------------------------------------------------------------------


def test_otel_config_is_frozen() -> None:
    """`OTelConfig` is frozen per C-RT-03 invariant."""
    assert OTelConfig.model_config.get("frozen") is True


def test_otel_config_rejects_unknown_keys() -> None:
    """`extra='forbid'` per C-RT-03."""
    with pytest.raises(ValidationError):
        OTelConfig.model_validate(
            {"otlp_endpoint": "http://localhost:4318", "unknown": "x"},
        )
