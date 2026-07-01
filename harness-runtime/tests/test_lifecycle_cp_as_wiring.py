"""R-CXA-3 — CP -> AS runtime composer tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as.as_substrate_seam_exports import (
    AS_SUBSTRATE_SEAM_EXPORTS,
    ASConsumingAxis,
    ASSeamId,
)
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.cp_as_wiring import (
    AsCpConsumerCoverageMismatch,
    CpAsWiringBindError,
    CpAsWiringStage,
    CpConsumedAsSeamResolution,
    CpConsumedAsSeamsCoverage,
    CpConsumedAsSeamUnresolved,
    RuntimeCpAsWiring,
    materialize_cp_as_wiring_stage,
    resolve_cp_consumed_as_seams,
    verify_cp_consumed_as_seam_coverage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _stage(tmp_path: Path) -> CpAsWiringStage:
    return materialize_cp_as_wiring_stage(_config(tmp_path))


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    assert isinstance(stage, CpAsWiringStage)
    assert isinstance(stage.wiring, RuntimeCpAsWiring)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    with pytest.raises(AttributeError):
        stage.wiring = stage.wiring  # type: ignore[misc]


def test_bind_errors_typed() -> None:
    assert isinstance(CpAsWiringBindError("test"), Exception)
    assert isinstance(CpConsumedAsSeamUnresolved("test"), Exception)
    assert isinstance(AsCpConsumerCoverageMismatch("test"), Exception)


def test_resolves_cp_consumed_as_seam_exports() -> None:
    resolutions = resolve_cp_consumed_as_seams()
    assert len(resolutions) == 5
    assert {r.seam_id for r in resolutions} == {
        ASSeamId.SANDBOX_BOUNDED_SPAN_SCHEMA_EXPORT,
        ASSeamId.FIVE_AXIS_MULTIPLICATIVE_TUNABLE_EXPORT,
        ASSeamId.PER_TOOL_REQUIRED_SECRETS_EXPORT,
        ASSeamId.ELEVEN_PRIMITIVE_ADOPTION_DEPTH_MATRIX_EXPORT,
        ASSeamId.FORCING_CONDITION_EXPORT,
    }
    for resolution in resolutions:
        assert isinstance(resolution, CpConsumedAsSeamResolution)
        assert ASConsumingAxis.CONTROL_PLANE in resolution.as_export.consuming_axes
        assert resolution.bound_as_export is resolution.as_export


def test_bound_exports_are_as_manifest_identity_anchors(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    for resolution in stage.wiring.cp_consumed_as_seams:
        assert resolution.bound_as_export in AS_SUBSTRATE_SEAM_EXPORTS


def test_cp_consumed_as_seam_coverage_at_head_passes() -> None:
    coverage = verify_cp_consumed_as_seam_coverage()
    assert isinstance(coverage, CpConsumedAsSeamsCoverage)
    assert coverage.coverage_match is True
    assert coverage.declared_cp_consumed_seams == coverage.expected_cp_consumed_seams
