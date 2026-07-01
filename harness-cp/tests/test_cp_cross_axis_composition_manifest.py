"""Tests for U-CP-55 — CP cross-axis composition manifest (C-CP-24 §24.2-§24.4).

The CP-axis terminal aggregate exporter. Acceptance-criterion coverage:
  #1 manifest cardinality 9      -> test_cross_axis_composition_manifest_cardinality_nine
  #1 per-composition source units -> test_per_composition_source_units_match_spec
  #1 session targets             -> test_session_targets_match_spec
  #11 surface-kind 6 values      -> test_surface_kind_discriminator_six_values
  #3 F2-12 active engagement     -> test_f2_12_active_engagement_at_u_cp_20
  #3 F2-12 closure 6 steps       -> test_f2_12_closure_path_six_steps
  #3 F2-12 inheritance sessions  -> test_f2_12_inheritance_at_session_4_and_5
  #3 F2-12 active_at_v1          -> test_f2_12_active_at_v1
  #8 Session 5 load-bearing      -> test_session_5_ingests_four_load_bearing_exports
  #9 Session 4 load-bearing      -> test_session_4_ingests_five_load_bearing_exports
"""

from __future__ import annotations

from harness_cp.cp_cross_axis_composition_manifest import (
    CP_CROSS_AXIS_COMPOSITION_MANIFEST,
    F2_12_CARRY_FORWARD,
    SessionTarget,
    SurfaceKind,
)


def _by_name(name: str):
    return next(e for e in CP_CROSS_AXIS_COMPOSITION_MANIFEST if e.composition_name == name)


def test_cross_axis_composition_manifest_cardinality_nine() -> None:
    """#1 — CP_CROSS_AXIS_COMPOSITION_MANIFEST declares exactly 9 entries."""
    assert len(CP_CROSS_AXIS_COMPOSITION_MANIFEST) == 9


def test_per_composition_source_units_match_spec() -> None:
    """#1 — per-composition CP source units match C-CP-24 §24.2 verbatim."""
    assert _by_name("CP_namespace_exports").composition_surfaces[0].cp_source_units == ("U-CP-54",)
    assert set(
        _by_name("five_axis_gate_level_composition").composition_surfaces[0].cp_source_units
    ) == {"U-CP-43", "U-CP-45"}
    assert set(
        _by_name("per_persona_tier_audit_cryptographic_shape")
        .composition_surfaces[0]
        .cp_source_units
    ) == {"U-CP-42", "U-CP-44", "U-CP-45"}


def test_session_targets_match_spec() -> None:
    """#1 — session targets match the §24.2 export table."""
    assert _by_name("CP_namespace_exports").exported_to_session == (
        SessionTarget.OD_PLAN_SESSION_4,
    )
    assert _by_name("T_perm_3_three_layer_composition").exported_to_session == (
        SessionTarget.CROSS_AXIS_COMPOSITION_SESSION_5,
    )
    # deterministic_outer_harness_boundary exports to both sessions.
    both = _by_name("deterministic_outer_harness_boundary").exported_to_session
    assert set(both) == {
        SessionTarget.OD_PLAN_SESSION_4,
        SessionTarget.CROSS_AXIS_COMPOSITION_SESSION_5,
    }


def test_surface_kind_discriminator_six_values() -> None:
    """#11 — SurfaceKind declares exactly six discriminator values."""
    assert len(SurfaceKind) == 6


def test_f2_12_active_engagement_at_u_cp_20() -> None:
    """#3 — the F2-12 active-engagement unit is U-CP-20."""
    assert F2_12_CARRY_FORWARD.active_engagement_unit == "U-CP-20"


def test_f2_12_closure_path_six_steps() -> None:
    """#3 — the F2-12 closure path is the canonical 6-step revision chain."""
    steps = F2_12_CARRY_FORWARD.closure_path
    assert len(steps) == 6
    assert [s.step_index for s in steps] == [1, 2, 3, 4, 5, 6]
    assert "ADR-D1 v1.1 -> v1.2" in steps[0].revision_target
    assert "CP spec v1.2 -> v1.3" in steps[4].revision_target
    assert "CP plan v1 -> v2" in steps[5].revision_target


def test_f2_12_inheritance_at_session_4_and_5() -> None:
    """#3 — F2-12 inheritance sessions are OD Session 4 + Composition Session 5."""
    assert set(F2_12_CARRY_FORWARD.inheritance_sessions) == {
        SessionTarget.OD_PLAN_SESSION_4,
        SessionTarget.CROSS_AXIS_COMPOSITION_SESSION_5,
    }


def test_f2_12_active_at_v1() -> None:
    """#3 — the F2-12 carry-forward is declared active_at_v1."""
    assert F2_12_CARRY_FORWARD.active_at_v1 is True


def test_session_5_ingests_four_load_bearing_exports() -> None:
    """#8 — Composition Session 5 ingests 4 cross-axis-load-bearing exports."""
    s5 = [
        e
        for e in CP_CROSS_AXIS_COMPOSITION_MANIFEST
        if SessionTarget.CROSS_AXIS_COMPOSITION_SESSION_5 in e.exported_to_session
    ]
    assert len(s5) == 4
    assert {e.composition_name for e in s5} == {
        "T_perm_3_three_layer_composition",
        "five_axis_gate_level_composition",
        "sub_agent_gate_descent",
        "deterministic_outer_harness_boundary",
    }


def test_session_4_ingests_five_load_bearing_exports() -> None:
    """#9 — OD plan Session 4 ingests 5 OD-load-bearing exports."""
    s4 = [
        e
        for e in CP_CROSS_AXIS_COMPOSITION_MANIFEST
        if SessionTarget.OD_PLAN_SESSION_4 in e.exported_to_session
    ]
    # 5 OD-only + deterministic_outer_harness_boundary (exports to both) = 6.
    assert len(s4) == 6
    od_only = {
        e.composition_name
        for e in s4
        if e.composition_name != "deterministic_outer_harness_boundary"
    }
    assert od_only == {
        "CP_namespace_exports",
        "multi_agent_span_hierarchy",
        "F2_substrate_join_at_engine_boundary",
        "per_persona_tier_audit_cryptographic_shape",
        "operator_burden_eval_primitive",
    }
