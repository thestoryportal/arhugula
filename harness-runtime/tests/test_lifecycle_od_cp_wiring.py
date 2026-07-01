"""U-RT-38 — `materialize_od_cp_wiring_stage` + `RuntimeOdCpWiring` tests.

ACs per Phase 2 Session 7 L7 §12.6 (OD → CP — 3 edges per C-RT-12 §12.6):

Edge 1 (U-OD-09 → U-CP-54): `verify_harness_breaker_namespace_inversion`
confirms CP's `harness.breaker.*` export advertises 7 attributes matching
OD's canonical `HARNESS_BREAKER_ATTRIBUTES` tuple (C-OD-07 §7.1). F-CP-01
Stage 3b inversion contract.

Edges 2 + 3 (U-OD-34 → U-CP-54 / U-CP-55): `resolve_od_cp_manifest_references`
walks the OD `SubstrateSeamExportsManifest` exports and binds every export
whose `cross_axis_edge_targets` contains `"U-CP-54"` or `"U-CP-55"` to the
respective CP terminal manifest tuple. Pattern P1 identity anchors.

Closes L7 stage 6 CXA_WIRING.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cp_cross_axis_composition_manifest import (
    CP_CROSS_AXIS_COMPOSITION_MANIFEST,
)
from harness_cp.cp_namespace_export_manifest import (
    CP_NAMESPACE_EXPORT_MANIFEST,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_od.harness_breaker_schema import HARNESS_BREAKER_ATTRIBUTES
from harness_od.substrate_seam_exports_aggregate_manifest import (
    OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    ConsumerAxis,
    F2_12_CarryForwardInheritance,
    ManifestScope,
    SubstrateSeamExport,
    SubstrateSeamExportsManifest,
)
from harness_runtime.lifecycle.od_cp_wiring import (
    HarnessBreakerNamespaceInversion,
    HarnessBreakerNamespaceInversionMismatch,
    OdCpManifestReferenceUnresolved,
    OdCpWiringBindError,
    OdCpWiringStage,
    RuntimeOdCpWiring,
    materialize_od_cp_wiring_stage,
    resolve_od_cp_manifest_references,
    verify_harness_breaker_namespace_inversion,
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


def _stage(
    tmp_path: Path,
    od_manifest: SubstrateSeamExportsManifest | None = None,
) -> OdCpWiringStage:
    return materialize_od_cp_wiring_stage(
        _config(tmp_path),
        od_manifest or OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    )


def _od_manifest_with_one_export(
    *, cross_axis_targets: tuple[str, ...]
) -> SubstrateSeamExportsManifest:
    return SubstrateSeamExportsManifest(
        exports=(
            SubstrateSeamExport(
                export_name="synthetic-test-export",
                source_unit="U-OD-99",
                contract_anchor="C-OD-XX §X",
                consumer_axis=frozenset({ConsumerAxis.CONTROL_PLANE}),
                cross_axis_edge_targets=cross_axis_targets,
            ),
        ),
        cross_axis_edge_count=0,
        cross_axis_edge_breakdown={ConsumerAxis.CONTROL_PLANE: 0},
        f2_12_carry_forward_inheritance=F2_12_CarryForwardInheritance(
            inherited_from="CP plan U-CP-55 §24.4",
            contract_bearing_site="U-OD-20 implementing C-OD-14 §14.5",
            closure_path_step_count=6,
            closure_target="OD plan v2 (revision-pass mode per SKILL.md §8)",
            closure_pending_at_v1=False,
            partial_closure_rejected=True,
            forward_routing=(
                "parallel council-orchestrator C7+C9 session per ADD §6.3.1 active path"
            ),
        ),
        manifest_scope=ManifestScope.TERMINAL_AGGREGATE_FOR_PHASE_6_PLUS_IMPLEMENTATION,
    )


# ---------------------------------------------------------------------------
# Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    assert isinstance(stage, OdCpWiringStage)
    assert isinstance(stage.wiring, RuntimeOdCpWiring)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    with pytest.raises(AttributeError):
        stage.wiring = stage.wiring  # type: ignore[misc]


def test_bind_errors_typed() -> None:
    assert isinstance(OdCpWiringBindError("test"), Exception)
    assert isinstance(OdCpManifestReferenceUnresolved("test"), Exception)
    assert isinstance(HarnessBreakerNamespaceInversionMismatch("test"), Exception)


# ---------------------------------------------------------------------------
# Edge 1 — harness.breaker.* F-CP-01 Stage 3b inversion verification.
# ---------------------------------------------------------------------------


def test_harness_breaker_inversion_at_head_matches() -> None:
    """OD's 7-attribute canonical schema matches CP's namespace export count."""
    result = verify_harness_breaker_namespace_inversion()
    assert isinstance(result, HarnessBreakerNamespaceInversion)
    assert result.match is True
    assert result.od_canonical_attribute_count == 7
    assert result.cp_declared_attribute_count == 7


def test_harness_breaker_attribute_count_equals_od_tuple_len() -> None:
    """OD canonical attribute count IS the length of HARNESS_BREAKER_ATTRIBUTES."""
    result = verify_harness_breaker_namespace_inversion()
    assert result.od_canonical_attribute_count == len(HARNESS_BREAKER_ATTRIBUTES)


def test_composer_runs_inversion_verification(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    assert stage.wiring.harness_breaker_inversion.match is True


# ---------------------------------------------------------------------------
# Edges 2 + 3 — manifest string reference resolution.
# ---------------------------------------------------------------------------


def test_resolves_real_od_manifest_cp_references(tmp_path: Path) -> None:
    """OD's real aggregate manifest carries U-CP-54 + U-CP-55 references."""
    stage = _stage(tmp_path)
    refs = stage.wiring.manifest_references
    target_ids = {r.cp_target_string_id for r in refs}
    assert "U-CP-54" in target_ids
    assert "U-CP-55" in target_ids


def test_bound_cp_manifests_are_u_rt_33_anchor_identities(tmp_path: Path) -> None:
    """Pattern P1 anchors — bound manifests ARE the CP-module's exported constants."""
    stage = _stage(tmp_path)
    for ref in stage.wiring.manifest_references:
        if ref.cp_target_string_id == "U-CP-54":
            assert ref.bound_cp_manifest is CP_NAMESPACE_EXPORT_MANIFEST
        elif ref.cp_target_string_id == "U-CP-55":
            assert ref.bound_cp_manifest is CP_CROSS_AXIS_COMPOSITION_MANIFEST


def test_synthetic_manifest_with_u_cp_54_ref() -> None:
    od_manifest = _od_manifest_with_one_export(cross_axis_targets=("U-CP-54",))
    refs = resolve_od_cp_manifest_references(od_manifest)
    assert len(refs) == 1
    assert refs[0].cp_target_string_id == "U-CP-54"
    assert refs[0].bound_cp_manifest is CP_NAMESPACE_EXPORT_MANIFEST


def test_synthetic_manifest_with_u_cp_55_ref() -> None:
    od_manifest = _od_manifest_with_one_export(cross_axis_targets=("U-CP-55",))
    refs = resolve_od_cp_manifest_references(od_manifest)
    assert len(refs) == 1
    assert refs[0].cp_target_string_id == "U-CP-55"
    assert refs[0].bound_cp_manifest is CP_CROSS_AXIS_COMPOSITION_MANIFEST


def test_synthetic_manifest_with_both_cp_refs() -> None:
    od_manifest = _od_manifest_with_one_export(
        cross_axis_targets=("U-CP-54", "U-CP-55"),
    )
    refs = resolve_od_cp_manifest_references(od_manifest)
    assert len(refs) == 2
    assert {r.cp_target_string_id for r in refs} == {"U-CP-54", "U-CP-55"}


def test_synthetic_manifest_with_no_cp_refs() -> None:
    od_manifest = _od_manifest_with_one_export(
        cross_axis_targets=("U-IS-17", "U-AS-33"),
    )
    refs = resolve_od_cp_manifest_references(od_manifest)
    assert refs == ()


# ---------------------------------------------------------------------------
# Composer composition — both surfaces run together.
# ---------------------------------------------------------------------------


def test_composer_resolves_references_at_construction(tmp_path: Path) -> None:
    od_manifest = _od_manifest_with_one_export(
        cross_axis_targets=("U-CP-54", "U-CP-55"),
    )
    stage = _stage(tmp_path, od_manifest=od_manifest)
    assert len(stage.wiring.manifest_references) == 2
    assert stage.wiring.harness_breaker_inversion.match is True


def test_composer_handles_zero_cp_references(tmp_path: Path) -> None:
    """Zero CP refs → empty references; inversion still runs."""
    od_manifest = _od_manifest_with_one_export(cross_axis_targets=("U-IS-17",))
    stage = _stage(tmp_path, od_manifest=od_manifest)
    assert stage.wiring.manifest_references == ()
    assert stage.wiring.harness_breaker_inversion.match is True
