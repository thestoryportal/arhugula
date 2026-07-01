"""Tests for U-CP-24 — per-engine-class topology overlay (C-CP-11 §11.2).

Acceptance-criterion coverage:
  #1 5 entries per §11.2 verbatim -> test_per_engine_overlays_cardinality_five,
                                     test_per_engine_overlay_match_spec_verbatim
  #2 T-perm-3 reading 1 per class -> test_t_perm_3_reading_one_per_engine
  #3 cascade enforcement per read -> test_cascade_enforcement_consistent_with_reading
"""

from __future__ import annotations

from harness_cp.engine_class import EngineClass
from harness_cp.per_engine_class_topology_overlay import (
    PER_ENGINE_CLASS_OVERLAYS,
    CascadeEnforcementMechanism,
    TopologyFaultHandling,
    WriterSerializationMechanism,
    overlay_for,
)


def test_per_engine_overlays_cardinality_five() -> None:
    """#1 — exactly five entries, one per EngineClass."""
    assert len(PER_ENGINE_CLASS_OVERLAYS) == 5
    assert {o.engine_class for o in PER_ENGINE_CLASS_OVERLAYS} == set(EngineClass)


def test_per_engine_overlay_match_spec_verbatim() -> None:
    """#1 — each overlay matches the §11.2 5-row table verbatim."""
    expected = {
        EngineClass.EVENT_SOURCED_REPLAY: (
            TopologyFaultHandling.BELOW_ENGINE,
            CascadeEnforcementMechanism.ENGINE_NATIVE_CANCELLATION,
            WriterSerializationMechanism.ENGINE_NATIVE_WRITER_SERIAL,
        ),
        EngineClass.SAVE_POINT_CHECKPOINT: (
            TopologyFaultHandling.ABOVE_ENGINE,
            CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
            WriterSerializationMechanism.HARNESS_LEASE_ACQUISITION,
        ),
        EngineClass.PURE_PATTERN_NO_ENGINE: (
            TopologyFaultHandling.ABOVE_ENGINE,
            CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
            WriterSerializationMechanism.HARNESS_LEASE_ACQUISITION,
        ),
        EngineClass.RECONCILER_LOOP: (
            TopologyFaultHandling.RECONCILER,
            CascadeEnforcementMechanism.CRD_RECONCILER_DRIVEN,
            WriterSerializationMechanism.CRD_RESOURCE_VERSION,
        ),
        EngineClass.WAL_SEGMENT: (
            TopologyFaultHandling.ABOVE_ENGINE,
            CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
            WriterSerializationMechanism.HARNESS_LEASE_ACQUISITION,
        ),
    }
    for ec, (reading, cascade, writer) in expected.items():
        o = overlay_for(ec)
        assert o.t_perm_3_reading is reading
        assert o.cascade_enforcement_mechanism is cascade
        assert o.writer_serialization_mechanism is writer


def test_t_perm_3_reading_one_per_engine() -> None:
    """#2 — each engine class maps to exactly one T-perm-3 reading."""
    for ec in EngineClass:
        readings = {o.t_perm_3_reading for o in PER_ENGINE_CLASS_OVERLAYS if o.engine_class is ec}
        assert len(readings) == 1


def test_cascade_enforcement_consistent_with_reading() -> None:
    """#3 — cascade enforcement delegates per the §11.2 reading."""
    for o in PER_ENGINE_CLASS_OVERLAYS:
        if o.t_perm_3_reading is TopologyFaultHandling.BELOW_ENGINE:
            assert (
                o.cascade_enforcement_mechanism
                is CascadeEnforcementMechanism.ENGINE_NATIVE_CANCELLATION
            )
        elif o.t_perm_3_reading is TopologyFaultHandling.ABOVE_ENGINE:
            assert (
                o.cascade_enforcement_mechanism
                is CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION
            )
        else:
            assert (
                o.cascade_enforcement_mechanism is CascadeEnforcementMechanism.CRD_RECONCILER_DRIVEN
            )
