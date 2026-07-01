"""U-RT-33 — `materialize_cxa_terminal_imports_stage` tests.

ACs per Phase 2 Session 7 L7 (CXA wiring entry — U-RT-33 opens L7):

1. All 5 terminal aggregate exporter manifests per spec §12.1 import
   cleanly via this module (side-effect realization at module load).
2. Each manifest exposes its expected top-level constant; constant is
   non-None and is the same object the manifest module itself exposes
   (Pattern P1 identity-equality anchor for U-RT-51 verification).
3. Composer returns a frozen stage with the 5 modules in spec §12.1
   table order; `CxaTerminalImportError` is typed.

Verification of Pattern P1 identity-equality across all 22 typed seams
is U-RT-51; this unit's verification scope is per spec §12.1 — import
realization + per-manifest-constant reachability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as import as_substrate_seam_exports as as_seam_exports
from harness_core.deployment_surface import DeploymentSurface
from harness_cp import cp_cross_axis_composition_manifest as cp_xa_manifest
from harness_cp import cp_namespace_export_manifest as cp_ns_manifest
from harness_cp.topology_pattern import TopologyPattern
from harness_is import substrate_seam_exports as is_seam_exports
from harness_od import substrate_seam_exports_aggregate_manifest as od_seam_exports
from harness_runtime.lifecycle.cxa_terminal_imports import (
    TERMINAL_MANIFEST_CONSTANTS,
    TERMINAL_MANIFEST_MODULES,
    CxaTerminalImportError,
    CxaTerminalImportsStage,
    materialize_cxa_terminal_imports_stage,
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


# ---------------------------------------------------------------------------
# Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert isinstance(stage, CxaTerminalImportsStage)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    with pytest.raises(AttributeError):
        stage.imported_manifests = stage.imported_manifests  # type: ignore[misc]


def test_bind_error_typed() -> None:
    err = CxaTerminalImportError("test")
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# AC #1 — All 5 terminal manifests import cleanly.
# ---------------------------------------------------------------------------


def test_five_manifests_imported(tmp_path: Path) -> None:
    """Spec §12.1 table — exactly 5 terminal manifest modules realized."""
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert len(stage.imported_manifests) == 5


def test_imported_manifests_match_spec_table_order(tmp_path: Path) -> None:
    """Spec §12.1 table row order: IS, AS, CP-namespace, CP-cross-axis, OD."""
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert stage.imported_manifests == (
        is_seam_exports,
        as_seam_exports,
        cp_ns_manifest,
        cp_xa_manifest,
        od_seam_exports,
    )


def test_module_level_constant_is_the_same_tuple(tmp_path: Path) -> None:
    """Module-level `TERMINAL_MANIFEST_MODULES` matches the stage materialization."""
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert stage.imported_manifests is TERMINAL_MANIFEST_MODULES


# ---------------------------------------------------------------------------
# AC #2 — Each manifest's top-level constant is reachable and identity-equal.
# ---------------------------------------------------------------------------


def test_is_manifest_constant_identity(tmp_path: Path) -> None:
    """Pattern P1 anchor — IS manifest constant IS the producer-side constant."""
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert (
        stage.manifest_constants["harness_is.substrate_seam_exports"]
        is is_seam_exports.IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST
    )


def test_as_manifest_constant_identity(tmp_path: Path) -> None:
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert (
        stage.manifest_constants["harness_as.as_substrate_seam_exports"]
        is as_seam_exports.AS_SUBSTRATE_SEAM_EXPORTS
    )


def test_cp_namespace_manifest_constant_identity(tmp_path: Path) -> None:
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert (
        stage.manifest_constants["harness_cp.cp_namespace_export_manifest"]
        is cp_ns_manifest.CP_NAMESPACE_EXPORT_MANIFEST
    )


def test_cp_cross_axis_manifest_constant_identity(tmp_path: Path) -> None:
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert (
        stage.manifest_constants["harness_cp.cp_cross_axis_composition_manifest"]
        is cp_xa_manifest.CP_CROSS_AXIS_COMPOSITION_MANIFEST
    )


def test_od_manifest_constant_identity(tmp_path: Path) -> None:
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert (
        stage.manifest_constants["harness_od.substrate_seam_exports_aggregate_manifest"]
        is od_seam_exports.OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST
    )


def test_all_manifest_constants_non_none(tmp_path: Path) -> None:
    """Per-manifest constants reachable; non-None precondition of composer."""
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert len(stage.manifest_constants) == 5
    for module_name, constant in stage.manifest_constants.items():
        assert constant is not None, f"{module_name} exposes None constant"


# ---------------------------------------------------------------------------
# AC #3 — module-level constants vs stage instance independence.
# ---------------------------------------------------------------------------


def test_stage_manifest_constants_is_a_copy(tmp_path: Path) -> None:
    """The stage holds a copy of the module-level dict — mutating it is local."""
    stage = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert stage.manifest_constants == TERMINAL_MANIFEST_CONSTANTS
    assert stage.manifest_constants is not TERMINAL_MANIFEST_CONSTANTS


def test_repeat_composer_calls_yield_same_module_identities(tmp_path: Path) -> None:
    """Idempotent re-construction — module references stable across calls."""
    a = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    b = materialize_cxa_terminal_imports_stage(_config(tmp_path))
    assert a.imported_manifests == b.imported_manifests
    for key in a.manifest_constants:
        assert a.manifest_constants[key] is b.manifest_constants[key]
