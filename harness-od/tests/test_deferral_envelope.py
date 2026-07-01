"""Tests for U-OD-03 — deferral envelope (committed-at-D6 vs deferred).

Test set per the U-OD-03 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_1.md §3.1.3). Every acceptance criterion maps to >=1 test.

Acceptance criteria (C-OD-03 §3.1 / §3.2 / §3.3):
  #1 — SurfaceCommitmentClass enumerates exactly 2 values.
  #2 — COMMITTED_AT_D6_SURFACES declares exactly 6 entries (verbatim §3.1).
  #3 — DEFERRED_SURFACES aggregates the spec deferred blocks (11+).
  #4 — boundary invariant: a surface is in exactly one class.
  #5 — every "Deferred to implementation discretion" block enumerated.
  #6 — closure target is one of two values.
  #7 — committed contract anchors resolve to OD spec sections.
"""

from __future__ import annotations

from harness_od.deferral_envelope import (
    CLOSURE_TARGETS,
    COMMITTED_AT_D6_SURFACES,
    DEFERRED_SURFACES,
    CommittedSurface,
    DeferredSurface,
    SurfaceCommitmentClass,
)


def test_surface_commitment_class_cardinality_two() -> None:
    """Acceptance #1 — SurfaceCommitmentClass enumerates exactly 2 values."""
    assert len(SurfaceCommitmentClass) == 2
    assert set(SurfaceCommitmentClass) == {
        SurfaceCommitmentClass.COMMITTED_AT_D6,
        SurfaceCommitmentClass.DEFERRED_TO_DEPLOYMENT_BINDING_TIME,
    }


def test_committed_at_d6_surfaces_cardinality_six() -> None:
    """Acceptance #2 — COMMITTED_AT_D6_SURFACES declares exactly 6 entries."""
    assert len(COMMITTED_AT_D6_SURFACES) == 6
    for surface in COMMITTED_AT_D6_SURFACES:
        assert isinstance(surface, CommittedSurface)


def test_committed_at_d6_surfaces_verbatim_six_set() -> None:
    """Acceptance #2 — the 6 committed surfaces match the plan acc #2 set."""
    names = {s.surface_name for s in COMMITTED_AT_D6_SURFACES}
    assert names == {
        "per-cell backend class",
        "sampling discipline",
        "redaction class",
        "trace storage tier",
        "collector placement",
        "retention class",
    }


def test_deferred_surfaces_aggregates_eleven_or_more() -> None:
    """Acceptance #3 — DEFERRED_SURFACES aggregates at least 11 blocks.

    The spec carries one "Deferred to implementation discretion" block per
    contract §1-§23; the strict-coverage reading enumerates all 23.
    """
    assert len(DEFERRED_SURFACES) >= 11
    for surface in DEFERRED_SURFACES:
        assert isinstance(surface, DeferredSurface)


def test_deferral_envelope_boundary_no_overlap() -> None:
    """Acceptance #4 — boundary invariant: committed and deferred disjoint."""
    committed = {s.surface_name for s in COMMITTED_AT_D6_SURFACES}
    deferred = {s.surface_name for s in DEFERRED_SURFACES}
    assert committed.isdisjoint(deferred)


def test_every_deferred_implementation_discretion_block_enumerated() -> None:
    """Acceptance #5 — every spec contract §1-§23 deferred block enumerated.

    23 contracts, each with exactly one "Deferred to implementation
    discretion" block → 23 distinct contract anchors in DEFERRED_SURFACES.
    """
    anchors = {s.contract_anchor for s in DEFERRED_SURFACES}
    assert len(anchors) == len(DEFERRED_SURFACES)
    for n in range(1, 24):
        prefix = f"C-OD-{n:02d} "
        assert any(a.startswith(prefix) for a in anchors), (
            f"missing deferred-surface entry for C-OD-{n:02d}"
        )


def test_closure_target_one_of_two_values() -> None:
    """Acceptance #6 — closure target is one of two values."""
    assert CLOSURE_TARGETS == {"deployment_binding_time", "phase_6_implementation"}
    for surface in DEFERRED_SURFACES:
        assert surface.closure_target in CLOSURE_TARGETS


def test_committed_surface_contract_anchors_resolve_to_od_spec_sections() -> None:
    """Acceptance #7 — committed contract anchors cite OD spec C-OD-NN sections."""
    for surface in COMMITTED_AT_D6_SURFACES:
        assert "C-OD-" in surface.contract_anchor


def test_committed_surface_frozen_and_hashable() -> None:
    """The CommittedSurface record is frozen → Eq + Hash."""
    surface = COMMITTED_AT_D6_SURFACES[0]
    assert hash(surface) == hash(surface)
    duplicate = CommittedSurface(
        surface_name=surface.surface_name,
        contract_anchor=surface.contract_anchor,
    )
    assert surface == duplicate


def test_deferred_surface_frozen_and_hashable() -> None:
    """The DeferredSurface record is frozen → Eq + Hash."""
    surface = DEFERRED_SURFACES[0]
    assert hash(surface) == hash(surface)
