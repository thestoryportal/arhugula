"""Per-engine-class topology overlay + T-perm-3 reading binding — U-CP-24.

Implements C-CP-11 §11.2 (per-engine-class topology overlay). Declares the
`TopologyFaultHandling` 3-value enum (the T-perm-3 reading), the
`CascadeEnforcementMechanism` 3-value enum, the `WriterSerializationMechanism`
3-value enum, the `PerEngineClassTopologyOverlay` record, and the 5-entry
`PER_ENGINE_CLASS_OVERLAYS` table — one entry per `EngineClass`.

The T-perm-3 reading is a **per-engine-class** binding: no engine class maps to
multiple readings (§11.2 non-collapsing invariant). Cascade enforcement
delegates to engine-native at `BELOW_ENGINE`, harness-driven at `ABOVE_ENGINE`,
CRD-driven at `RECONCILER`.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.4 U-CP-24 (preserved
verbatim into v2.4); Spec_Control_Plane_v1_2.md §11 C-CP-11 §11.2 (preserved
verbatim into v1.3); ADR-D4 v1.1.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass


class TopologyFaultHandling(StrEnum):
    """The T-perm-3 reading — per-engine-class topology fault handling (§11.2)."""

    ABOVE_ENGINE = "above_engine"
    """Harness composes lease + dedup + resumption."""

    BELOW_ENGINE = "below_engine"
    """Engine owns lifecycle; harness becomes topology-author."""

    RECONCILER = "reconciler"
    """Control-loop owns reconvergence."""


class CascadeEnforcementMechanism(StrEnum):
    """The cascade-enforcement mechanism per engine class (§11.2)."""

    HARNESS_CANCELLATION_PROPAGATION = "harness_cancellation_propagation"
    ENGINE_NATIVE_CANCELLATION = "engine_native_cancellation"
    CRD_RECONCILER_DRIVEN = "crd_reconciler_driven"


class WriterSerializationMechanism(StrEnum):
    """The writer-serialization mechanism per engine class (§11.2)."""

    HARNESS_LEASE_ACQUISITION = "harness_lease_acquisition"
    ENGINE_NATIVE_WRITER_SERIAL = "engine_native_writer_serial"
    CRD_RESOURCE_VERSION = "crd_resource_version"


class PerEngineClassTopologyOverlay(BaseModel):
    """The topology overlay for one engine class (C-CP-11 §11.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: EngineClass
    t_perm_3_reading: TopologyFaultHandling
    cascade_enforcement_mechanism: CascadeEnforcementMechanism
    writer_serialization_mechanism: WriterSerializationMechanism


# --- Registry population (C-CP-11 §11.2 5-row table) ------------------------

PER_ENGINE_CLASS_OVERLAYS: tuple[PerEngineClassTopologyOverlay, ...] = (
    PerEngineClassTopologyOverlay(
        engine_class=EngineClass.EVENT_SOURCED_REPLAY,
        t_perm_3_reading=TopologyFaultHandling.BELOW_ENGINE,
        cascade_enforcement_mechanism=CascadeEnforcementMechanism.ENGINE_NATIVE_CANCELLATION,
        writer_serialization_mechanism=WriterSerializationMechanism.ENGINE_NATIVE_WRITER_SERIAL,
    ),
    PerEngineClassTopologyOverlay(
        engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
        t_perm_3_reading=TopologyFaultHandling.ABOVE_ENGINE,
        cascade_enforcement_mechanism=CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
        writer_serialization_mechanism=WriterSerializationMechanism.HARNESS_LEASE_ACQUISITION,
    ),
    PerEngineClassTopologyOverlay(
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        t_perm_3_reading=TopologyFaultHandling.ABOVE_ENGINE,
        cascade_enforcement_mechanism=CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
        writer_serialization_mechanism=WriterSerializationMechanism.HARNESS_LEASE_ACQUISITION,
    ),
    PerEngineClassTopologyOverlay(
        engine_class=EngineClass.RECONCILER_LOOP,
        t_perm_3_reading=TopologyFaultHandling.RECONCILER,
        cascade_enforcement_mechanism=CascadeEnforcementMechanism.CRD_RECONCILER_DRIVEN,
        writer_serialization_mechanism=WriterSerializationMechanism.CRD_RESOURCE_VERSION,
    ),
    PerEngineClassTopologyOverlay(
        engine_class=EngineClass.WAL_SEGMENT,
        t_perm_3_reading=TopologyFaultHandling.ABOVE_ENGINE,
        cascade_enforcement_mechanism=CascadeEnforcementMechanism.HARNESS_CANCELLATION_PROPAGATION,
        writer_serialization_mechanism=WriterSerializationMechanism.HARNESS_LEASE_ACQUISITION,
    ),
)
"""The 5 per-engine-class topology overlays per C-CP-11 §11.2 verbatim."""

_OVERLAY_BY_CLASS: dict[EngineClass, PerEngineClassTopologyOverlay] = {
    o.engine_class: o for o in PER_ENGINE_CLASS_OVERLAYS
}


def overlay_for(engine_class: EngineClass) -> PerEngineClassTopologyOverlay:
    """Return the topology overlay for an engine class. Total over `EngineClass`."""
    return _OVERLAY_BY_CLASS[engine_class]
