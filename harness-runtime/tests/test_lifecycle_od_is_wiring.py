"""U-RT-36 — `materialize_od_is_wiring_stage` + `RuntimeOdIsWiring` tests.

ACs per Phase 2 Session 7 L7 §12.4 (OD → IS — 2 edges per C-RT-12 §12.4):

Edge 1 (U-OD-30 → U-IS-11): The `RuntimeAuditLedgerWriter` from U-RT-32
is re-cited under the OD → IS stage as the §12.4 edge 1 consumer; the
stage's `audit_writer.append(tenant_id, audit_entry)` round-trips an OD
`AuditLedgerEntry` through the IS chain and `chain_verification` passes.

Edge 2 (U-OD-34 → U-IS-17): `resolve_od_is_manifest_references` walks
the OD `SubstrateSeamExportsManifest` exports and binds every export
whose `cross_axis_edge_targets` includes `"U-IS-17"` to the IS terminal
manifest (`IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST`). The bound manifest IS
the U-RT-33-imported symbol (Pattern P1 identity anchor).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.topology_pattern import TopologyPattern
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_write import WriteResult, read_ledger
from harness_is.substrate_seam_exports import (
    IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
)
from harness_od.audit_ledger_types import (
    AuditLedgerEntry,
    AuditPayload,
    AuditSignatureAttributes,
    SignatureAlgorithm,
    StateLedgerEntryRef,
)
from harness_od.substrate_seam_exports_aggregate_manifest import (
    OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    ConsumerAxis,
    F2_12_CarryForwardInheritance,
    ManifestScope,
    SubstrateSeamExport,
    SubstrateSeamExportsManifest,
)
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.audit_writer import (
    RuntimeAuditLedgerWriter,
    materialize_audit_writer_stage,
)
from harness_runtime.lifecycle.od_is_wiring import (
    OdIsManifestReferenceResolution,
    OdIsManifestReferenceUnresolved,
    OdIsWiringBindError,
    OdIsWiringStage,
    RuntimeOdIsWiring,
    materialize_od_is_wiring_stage,
    resolve_od_is_manifest_references,
)
from harness_runtime.lifecycle.state_ledger import (
    LedgerWriter,
    materialize_state_ledger,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------------


def _resolver_for(tmp_path: Path) -> PathResolver:
    config = PathBindingConfig(
        raw_entries=(
            {
                "path_class": PathClass.STATE_LEDGER,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(tmp_path / "state.jsonl"),
            },
        ),
    )
    return PathResolver(build_path_binding(config))


def _ledger_writer(tmp_path: Path) -> LedgerWriter:
    return materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime"),
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


def _audit_writer(tmp_path: Path) -> RuntimeAuditLedgerWriter:
    stage = materialize_audit_writer_stage(
        _config(tmp_path),
        _ledger_writer(tmp_path),
    )
    return stage.writer


def _stage(
    tmp_path: Path,
    od_manifest: SubstrateSeamExportsManifest | None = None,
) -> OdIsWiringStage:
    return materialize_od_is_wiring_stage(
        _config(tmp_path),
        _audit_writer(tmp_path),
        od_manifest or OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    )


def _audit_entry(entry_hash: str = "a" * 64) -> AuditLedgerEntry:
    return AuditLedgerEntry(
        payload=AuditPayload(
            entry_core=StateLedgerEntryRef(f"entry-ref-{entry_hash[:8]}"),
            audit_namespace_attrs={"audit.actor": "od-emission-site"},
            prior_entry_hash="0" * 64,
        ),
        signature_attrs=AuditSignatureAttributes(
            audit_signature_value=f"sig:{entry_hash[:8]}",
            audit_signature_algorithm=SignatureAlgorithm.ED25519,
            audit_signature_key_id="test-key",
            audit_signature_key_period="2026-Q2",
        ),
        entry_hash=entry_hash,
    )


def _empty_od_manifest_with_export(
    *, cross_axis_targets: tuple[str, ...]
) -> SubstrateSeamExportsManifest:
    """Build a minimal OD manifest with one synthetic export for test injection."""
    return SubstrateSeamExportsManifest(
        exports=(
            SubstrateSeamExport(
                export_name="synthetic-test-export",
                source_unit="U-OD-99",
                contract_anchor="C-OD-XX §X",
                consumer_axis=frozenset({ConsumerAxis.INFORMATION_SUBSTRATE}),
                cross_axis_edge_targets=cross_axis_targets,
            ),
        ),
        cross_axis_edge_count=0,
        cross_axis_edge_breakdown={ConsumerAxis.INFORMATION_SUBSTRATE: 0},
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
    assert isinstance(stage, OdIsWiringStage)
    assert isinstance(stage.wiring, RuntimeOdIsWiring)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    with pytest.raises(AttributeError):
        stage.wiring = stage.wiring  # type: ignore[misc]


def test_wiring_is_frozen(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    with pytest.raises(AttributeError):
        stage.wiring.audit_writer = stage.wiring.audit_writer  # type: ignore[misc]


def test_bind_errors_typed() -> None:
    assert isinstance(OdIsWiringBindError("test"), Exception)
    assert isinstance(OdIsManifestReferenceUnresolved("test"), Exception)


# ---------------------------------------------------------------------------
# Edge 1 — U-OD-30 → U-IS-11 (audit_writer re-cite + round-trip).
# ---------------------------------------------------------------------------


def test_edge_1_audit_writer_is_re_cited(tmp_path: Path) -> None:
    """The stage's `audit_writer` is the same `RuntimeAuditLedgerWriter` instance."""
    writer = _audit_writer(tmp_path)
    stage = materialize_od_is_wiring_stage(
        _config(tmp_path),
        writer,
        OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    )
    assert stage.wiring.audit_writer is writer


def test_edge_1_audit_round_trip_chain_valid(tmp_path: Path) -> None:
    """OD audit entry → IS chain → chain_verification passes."""
    stage = _stage(tmp_path)
    result = stage.wiring.audit_writer.append(
        tenant_id="tenant-x",
        audit_entry=_audit_entry(),
    )
    assert result is WriteResult.APPENDED
    entries = read_ledger(stage.wiring.audit_writer.ledger_writer.handle)
    assert verify_chain(entries).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# Edge 2 — U-OD-34 → U-IS-17 (manifest string reference resolution).
# ---------------------------------------------------------------------------


def test_edge_2_resolves_all_real_od_u_is_17_references(tmp_path: Path) -> None:
    """Every OD export carrying `U-IS-17` resolves; bound manifest is non-empty."""
    stage = _stage(tmp_path)
    refs = stage.wiring.manifest_references
    # OD's real aggregate manifest carries at least one U-IS-17 reference.
    assert len(refs) >= 1
    for ref in refs:
        assert isinstance(ref, OdIsManifestReferenceResolution)
        assert ref.is_manifest_string_id == "U-IS-17"
        assert ref.bound_is_manifest == IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST


def test_edge_2_bound_manifest_is_u_rt_33_anchor_identity(tmp_path: Path) -> None:
    """Pattern P1 anchor — bound manifest IS the IS-module's exported constant."""
    stage = _stage(tmp_path)
    for ref in stage.wiring.manifest_references:
        assert ref.bound_is_manifest is IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST


def test_edge_2_synthetic_manifest_with_one_u_is_17_ref(tmp_path: Path) -> None:
    od_manifest = _empty_od_manifest_with_export(cross_axis_targets=("U-IS-17",))
    refs = resolve_od_is_manifest_references(od_manifest)
    assert len(refs) == 1
    assert refs[0].od_source_unit == "U-OD-99"
    assert refs[0].is_manifest_string_id == "U-IS-17"


def test_edge_2_synthetic_manifest_with_no_u_is_17_ref(tmp_path: Path) -> None:
    """Non-IS targets are skipped; resolver returns empty tuple."""
    od_manifest = _empty_od_manifest_with_export(
        cross_axis_targets=("U-AS-33", "U-CP-54"),
    )
    refs = resolve_od_is_manifest_references(od_manifest)
    assert refs == ()


def test_edge_2_synthetic_manifest_with_multiple_u_is_17_refs(tmp_path: Path) -> None:
    """Multiple exports each carrying U-IS-17 all resolve."""
    od_manifest = SubstrateSeamExportsManifest(
        exports=(
            SubstrateSeamExport(
                export_name="export-A",
                source_unit="U-OD-A",
                contract_anchor="C-OD-1",
                consumer_axis=frozenset({ConsumerAxis.INFORMATION_SUBSTRATE}),
                cross_axis_edge_targets=("U-IS-17",),
            ),
            SubstrateSeamExport(
                export_name="export-B",
                source_unit="U-OD-B",
                contract_anchor="C-OD-2",
                consumer_axis=frozenset({ConsumerAxis.INFORMATION_SUBSTRATE}),
                cross_axis_edge_targets=("U-IS-17", "U-AS-33"),
            ),
        ),
        cross_axis_edge_count=0,
        cross_axis_edge_breakdown={ConsumerAxis.INFORMATION_SUBSTRATE: 0},
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
    refs = resolve_od_is_manifest_references(od_manifest)
    assert len(refs) == 2
    assert {r.od_source_unit for r in refs} == {"U-OD-A", "U-OD-B"}


# ---------------------------------------------------------------------------
# Composer convenience — manifest resolution happens at construction time.
# ---------------------------------------------------------------------------


def test_composer_resolves_references_at_construction(tmp_path: Path) -> None:
    """Resolution runs during composer call — references populated on stage."""
    od_manifest = _empty_od_manifest_with_export(cross_axis_targets=("U-IS-17",))
    stage = _stage(tmp_path, od_manifest=od_manifest)
    assert len(stage.wiring.manifest_references) == 1


def test_audit_writer_independent_of_manifest_resolution(tmp_path: Path) -> None:
    """Edge 1 audit_writer is bound even when the OD manifest carries no IS refs."""
    od_manifest = _empty_od_manifest_with_export(cross_axis_targets=("U-AS-33",))
    stage = _stage(tmp_path, od_manifest=od_manifest)
    assert stage.wiring.manifest_references == ()
    # Edge 1 still wired.
    result = stage.wiring.audit_writer.append(
        tenant_id="tenant-x",
        audit_entry=_audit_entry(entry_hash="b" * 64),
    )
    assert result is WriteResult.APPENDED


def test_two_appends_preserve_chain_integrity(tmp_path: Path) -> None:
    """Audit_writer append x 2 → chain VALID; tenant-scoped reader returns 2 entries."""
    stage = _stage(tmp_path)
    stage.wiring.audit_writer.append(
        tenant_id="tenant-x",
        audit_entry=_audit_entry(entry_hash="1" * 64),
    )
    stage.wiring.audit_writer.append(
        tenant_id="tenant-x",
        audit_entry=_audit_entry(entry_hash="2" * 64),
    )
    entries = read_ledger(stage.wiring.audit_writer.ledger_writer.handle)
    assert len(entries) == 2
    assert verify_chain(entries).status is VerificationStatus.VALID
    tenant_view = stage.wiring.audit_writer.read_for_tenant("tenant-x")
    assert len(tenant_view) == 2


# Silence the "imported but unused" lint on timedelta (kept for symmetry with peers).
_ = timedelta
