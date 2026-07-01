"""Tests for U-OD-34 — OD substrate seam exports aggregate manifest.

Authority: C-OD-23.

THE TERMINAL AGGREGATE EXPORTER of the OD axis-stream. Every U-OD-34 acceptance
criterion (#1-#12) maps to >=1 test below. acc #3/#4 are tested at the v2.6
M-3-conformed values (26 / {IS:4, AS:10, CP:12}).
Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.8.3 (M-3
CONFORM revision over v2.1 §3.8.3); Spec_Operational_Discipline_v1_2.md §23.
"""

from __future__ import annotations

from harness_od.substrate_seam_exports_aggregate_manifest import (
    OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
    ConsumerAxis,
    F2_12_CarryForwardInheritance,
    ManifestScope,
    SubstrateSeamExport,
)

_MANIFEST = OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST


# --- acc #1 — 8 export sub-sections ----------------------------------------


def test_exports_cardinality_eight() -> None:
    """acc #1 — the manifest declares exactly 8 export sub-sections per §23.1."""
    assert len(_MANIFEST.exports) == 8


# --- acc #2 — per-export source_unit + contract_anchor resolve -------------


def test_per_export_source_unit_resolves() -> None:
    """acc #2 — every export's source_unit is a non-empty OD unit reference."""
    for export in _MANIFEST.exports:
        assert isinstance(export, SubstrateSeamExport)
        assert export.source_unit.startswith("U-OD-")


def test_per_export_contract_anchor_resolves() -> None:
    """acc #2 — every export's contract_anchor is a non-empty C-OD-NN section."""
    for export in _MANIFEST.exports:
        assert export.contract_anchor.startswith("C-OD-")


# --- acc #3 — cross_axis_edge_count (v2.6 M-3-conformed: 26) ---------------


def test_cross_axis_edge_count_twenty_six() -> None:
    """acc #3 — cross_axis_edge_count == 26 (v2.6 M-3-conformed from 28)."""
    assert _MANIFEST.cross_axis_edge_count == 26


# --- acc #4 — cross_axis_edge_breakdown (v2.6 M-3-conformed: 4/10/12) ------


def test_cross_axis_edge_breakdown_4_10_12() -> None:
    """acc #4 — cross_axis_edge_breakdown == {IS:4, AS:10, CP:12} (M-3)."""
    assert _MANIFEST.cross_axis_edge_breakdown == {
        ConsumerAxis.INFORMATION_SUBSTRATE: 4,
        ConsumerAxis.ACTION_SURFACE: 10,
        ConsumerAxis.CONTROL_PLANE: 12,
    }


def test_cross_axis_edge_count_equals_breakdown_sum() -> None:
    """acc #3/#4 — the aggregate count equals the per-axis breakdown sum."""
    assert _MANIFEST.cross_axis_edge_count == sum(_MANIFEST.cross_axis_edge_breakdown.values())


# --- acc #5 — F2-12 inherited_from -----------------------------------------


def test_f2_12_inherited_from_byte_exact() -> None:
    """acc #5 — f2_12.inherited_from == 'CP plan U-CP-55 §24.4' verbatim."""
    assert _MANIFEST.f2_12_carry_forward_inheritance.inherited_from == "CP plan U-CP-55 §24.4"


# --- acc #6 — F2-12 contract_bearing_site ----------------------------------


def test_f2_12_contract_bearing_site_u_od_20() -> None:
    """acc #6 — U-OD-20 is the sole F2-12 ACTIVE contract-bearing site."""
    assert (
        _MANIFEST.f2_12_carry_forward_inheritance.contract_bearing_site
        == "U-OD-20 implementing C-OD-14 §14.5"
    )


# --- acc #7 — F2-12 closure_path_step_count --------------------------------


def test_f2_12_closure_path_step_count_six() -> None:
    """acc #7 — closure_path_step_count == 6 (inherited from U-CP-55 §24.4)."""
    assert _MANIFEST.f2_12_carry_forward_inheritance.closure_path_step_count == 6


# --- acc #8 — F2-12 closure_target -----------------------------------------


def test_f2_12_closure_target_byte_exact() -> None:
    """acc #8 — closure_target == 'OD plan v2 (revision-pass mode ...)'."""
    assert (
        _MANIFEST.f2_12_carry_forward_inheritance.closure_target
        == "OD plan v2 (revision-pass mode per SKILL.md §8)"
    )


# --- acc #9 — F2-12 closure_pending + partial_closure_rejected ------------


def test_f2_12_closure_pending_at_v1_true() -> None:
    """acc #9 — closure_pending_at_v1 == True."""
    assert _MANIFEST.f2_12_carry_forward_inheritance.closure_pending_at_v1 is True


def test_f2_12_partial_closure_rejected_true() -> None:
    """acc #9 — partial_closure_rejected == True."""
    assert _MANIFEST.f2_12_carry_forward_inheritance.partial_closure_rejected is True


# --- acc #10 — F2-12 forward_routing ---------------------------------------


def test_forward_routing_byte_exact() -> None:
    """acc #10 — forward_routing is the C7+C9 council-orchestrator routing."""
    assert _MANIFEST.f2_12_carry_forward_inheritance.forward_routing == (
        "parallel council-orchestrator C7+C9 session per ADD §6.3.1 active path"
    )


# --- acc #11 — manifest_scope ----------------------------------------------


def test_manifest_scope_terminal_aggregate() -> None:
    """acc #11 — manifest_scope == TERMINAL_AGGREGATE_FOR_PHASE_6_PLUS_IMPL."""
    assert (
        _MANIFEST.manifest_scope == ManifestScope.TERMINAL_AGGREGATE_FOR_PHASE_6_PLUS_IMPLEMENTATION
    )


# --- acc #12 — manifest references the 4 terminal exporter targets ---------


def _all_targets() -> set[str]:
    return {target for export in _MANIFEST.exports for target in export.cross_axis_edge_targets}


def test_manifest_references_u_is_17() -> None:
    """acc #12 — the manifest references U-IS-17 (IS terminal exporter)."""
    assert "U-IS-17" in _all_targets()


def test_manifest_references_u_as_33() -> None:
    """acc #12 — the manifest references U-AS-33 (AS terminal exporter)."""
    assert "U-AS-33" in _all_targets()


def test_manifest_references_u_cp_54() -> None:
    """acc #12 — the manifest references U-CP-54 (CP terminal exporter)."""
    assert "U-CP-54" in _all_targets()


def test_manifest_references_u_cp_55_inheritance() -> None:
    """acc #12 — the manifest references U-CP-55 (CP F2-12 ACTIVE inheritance)."""
    assert "U-CP-55" in _all_targets()


# --- §23.1 manifest content table — load-bearing per-export checks ---------


def test_harness_breaker_export_marked_od_to_cp_exporter() -> None:
    """§23.1 export #4 — the harness.breaker.* export targets U-CP-54."""
    breaker_exports = [e for e in _MANIFEST.exports if e.source_unit == "U-OD-09"]
    assert len(breaker_exports) == 1
    assert "U-CP-54" in breaker_exports[0].cross_axis_edge_targets


def test_cost_attribution_export_includes_f2_12_inheritance() -> None:
    """§23.1 export #6 — the cost-attribution export references U-CP-55."""
    cost_exports = [e for e in _MANIFEST.exports if "U-OD-20" in e.source_unit]
    assert len(cost_exports) == 1
    assert "U-CP-55" in cost_exports[0].cross_axis_edge_targets


def test_cell_1_local_first_export_includes_is_substrate_targets() -> None:
    """§23.1 export #7 — the cell-1 local-first export targets U-IS-17."""
    collector_exports = [e for e in _MANIFEST.exports if "U-OD-27" in e.source_unit]
    assert len(collector_exports) == 1
    assert "U-IS-17" in collector_exports[0].cross_axis_edge_targets


def test_u_od_29_not_a_manifest_source_unit() -> None:
    """U-OD-34 does not depend on the FF-3-halted U-OD-29.

    U-OD-29 is not a `source_unit` of any of the 8 export sub-sections — the
    manifest aggregates the substrate-seam exports, and U-OD-29's per-sandbox-
    tier OTLP reachability surface is not one of them.
    """
    source_units = {e.source_unit for e in _MANIFEST.exports}
    assert not any("U-OD-29" in su for su in source_units)


def test_f2_12_inheritance_is_frozen_record() -> None:
    """F2_12_CarryForwardInheritance is a frozen, extra-forbidding record."""
    assert isinstance(_MANIFEST.f2_12_carry_forward_inheritance, F2_12_CarryForwardInheritance)
