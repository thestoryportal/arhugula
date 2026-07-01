"""U-RT-37 — `materialize_od_as_wiring_stage` + `RuntimeOdAsWiring` tests.

ACs per Phase 2 Session 7 L7 §12.5 (OD → AS — 1 edge per C-RT-12 §12.5):

1. Edge resolution: `resolve_od_as_manifest_references` walks the OD
   `SubstrateSeamExportsManifest` exports and binds every export whose
   `cross_axis_edge_targets` includes `"U-AS-33"` to the AS terminal
   manifest (`AS_SUBSTRATE_SEAM_EXPORTS`); bound manifest IS the
   U-RT-33-imported symbol (Pattern P1 identity anchor).
2. AS namespace verification: `verify_as_namespace_coverage` confirms the
   `ASSeamId` enum coverage matches the AS-manifest-declared seam IDs;
   mismatch surfaces typed via `AsNamespaceVerificationMismatch`.
3. Composer wiring: both surfaces run at construction; failures surface
   typed (fail-fast at bootstrap stage 6).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as.as_substrate_seam_exports import (
    AS_SUBSTRATE_SEAM_EXPORTS,
    ASSeamId,
)
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_od.substrate_seam_exports_aggregate_manifest import (
    OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    ConsumerAxis,
    F2_12_CarryForwardInheritance,
    ManifestScope,
    SubstrateSeamExport,
    SubstrateSeamExportsManifest,
)
from harness_runtime.lifecycle.od_as_wiring import (
    AsNamespaceVerificationMismatch,
    AsNamespaceVerificationResult,
    OdAsManifestReferenceResolution,
    OdAsManifestReferenceUnresolved,
    OdAsWiringBindError,
    OdAsWiringStage,
    RuntimeOdAsWiring,
    materialize_od_as_wiring_stage,
    resolve_od_as_manifest_references,
    verify_as_namespace_coverage,
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
) -> OdAsWiringStage:
    return materialize_od_as_wiring_stage(
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
                consumer_axis=frozenset({ConsumerAxis.ACTION_SURFACE}),
                cross_axis_edge_targets=cross_axis_targets,
            ),
        ),
        cross_axis_edge_count=0,
        cross_axis_edge_breakdown={ConsumerAxis.ACTION_SURFACE: 0},
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
    assert isinstance(stage, OdAsWiringStage)
    assert isinstance(stage.wiring, RuntimeOdAsWiring)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    with pytest.raises(AttributeError):
        stage.wiring = stage.wiring  # type: ignore[misc]


def test_bind_errors_typed() -> None:
    assert isinstance(OdAsWiringBindError("test"), Exception)
    assert isinstance(OdAsManifestReferenceUnresolved("test"), Exception)
    assert isinstance(AsNamespaceVerificationMismatch("test"), Exception)


# ---------------------------------------------------------------------------
# AC #1 — Edge resolution (OD "U-AS-33" → AS_SUBSTRATE_SEAM_EXPORTS).
# ---------------------------------------------------------------------------


def test_resolves_all_real_od_u_as_33_references(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    refs = stage.wiring.manifest_references
    # OD's real aggregate manifest carries at least one U-AS-33 reference.
    assert len(refs) >= 1
    for ref in refs:
        assert isinstance(ref, OdAsManifestReferenceResolution)
        assert ref.as_manifest_string_id == "U-AS-33"
        assert ref.bound_as_manifest == AS_SUBSTRATE_SEAM_EXPORTS


def test_bound_manifest_is_u_rt_33_anchor_identity(tmp_path: Path) -> None:
    """Pattern P1 anchor — bound manifest IS the AS-module's exported constant."""
    stage = _stage(tmp_path)
    for ref in stage.wiring.manifest_references:
        assert ref.bound_as_manifest is AS_SUBSTRATE_SEAM_EXPORTS


def test_synthetic_manifest_with_one_u_as_33_ref() -> None:
    od_manifest = _od_manifest_with_one_export(cross_axis_targets=("U-AS-33",))
    refs = resolve_od_as_manifest_references(od_manifest)
    assert len(refs) == 1
    assert refs[0].od_source_unit == "U-OD-99"
    assert refs[0].as_manifest_string_id == "U-AS-33"


def test_synthetic_manifest_with_no_u_as_33_ref() -> None:
    od_manifest = _od_manifest_with_one_export(
        cross_axis_targets=("U-IS-17", "U-CP-54"),
    )
    refs = resolve_od_as_manifest_references(od_manifest)
    assert refs == ()


def test_synthetic_manifest_with_multiple_u_as_33_refs() -> None:
    od_manifest = SubstrateSeamExportsManifest(
        exports=(
            SubstrateSeamExport(
                export_name="export-A",
                source_unit="U-OD-A",
                contract_anchor="C-OD-1",
                consumer_axis=frozenset({ConsumerAxis.ACTION_SURFACE}),
                cross_axis_edge_targets=("U-AS-33",),
            ),
            SubstrateSeamExport(
                export_name="export-B",
                source_unit="U-OD-B",
                contract_anchor="C-OD-2",
                consumer_axis=frozenset({ConsumerAxis.ACTION_SURFACE}),
                cross_axis_edge_targets=("U-AS-33", "U-IS-17"),
            ),
        ),
        cross_axis_edge_count=0,
        cross_axis_edge_breakdown={ConsumerAxis.ACTION_SURFACE: 0},
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
    refs = resolve_od_as_manifest_references(od_manifest)
    assert len(refs) == 2
    assert {r.od_source_unit for r in refs} == {"U-OD-A", "U-OD-B"}


# ---------------------------------------------------------------------------
# AC #2 — AS namespace verification.
# ---------------------------------------------------------------------------


def test_as_namespace_verification_at_head_passes() -> None:
    """At HEAD the ASSeamId enum + AS manifest seam IDs are equal sets."""
    result = verify_as_namespace_coverage()
    assert isinstance(result, AsNamespaceVerificationResult)
    assert result.coverage_match is True
    assert result.declared_seam_ids == frozenset(ASSeamId)
    assert result.enum_seam_ids == frozenset(ASSeamId)


def test_as_namespace_verification_covers_seven_seams() -> None:
    """C-AS-16 §16.1-§16.7 — exactly 7 ASSeamId values."""
    result = verify_as_namespace_coverage()
    assert len(result.enum_seam_ids) == 7
    assert len(result.declared_seam_ids) == 7


def test_composer_runs_namespace_verification(tmp_path: Path) -> None:
    """Composer materializes a namespace_verification record."""
    stage = _stage(tmp_path)
    assert stage.wiring.namespace_verification.coverage_match is True


# ---------------------------------------------------------------------------
# AC #3 — Composer composition: manifest references + verification together.
# ---------------------------------------------------------------------------


def test_composer_resolves_references_at_construction(tmp_path: Path) -> None:
    od_manifest = _od_manifest_with_one_export(cross_axis_targets=("U-AS-33",))
    stage = _stage(tmp_path, od_manifest=od_manifest)
    assert len(stage.wiring.manifest_references) == 1
    assert stage.wiring.namespace_verification.coverage_match is True


def test_composer_handles_zero_as_references(tmp_path: Path) -> None:
    """OD manifest with no U-AS-33 refs → empty references; verification still runs."""
    od_manifest = _od_manifest_with_one_export(cross_axis_targets=("U-IS-17",))
    stage = _stage(tmp_path, od_manifest=od_manifest)
    assert stage.wiring.manifest_references == ()
    assert stage.wiring.namespace_verification.coverage_match is True
