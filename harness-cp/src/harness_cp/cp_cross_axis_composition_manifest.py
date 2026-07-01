"""CP-axis cross-axis composition manifest + F2-12 carry-forward — U-CP-55.

Implements C-CP-24 §24.2/§24.3/§24.4 (the CP-axis cross-axis composition
manifest + the F2-12 carry-forward declaration). This is the **terminal unit**
of the CP plan dependency graph (L8) — the CP axis-stream terminal aggregate
exporter; no within-axis CP unit depends on it.

Declares the `CrossAxisCompositionExport` / `CompositionSurface` records, the
`SessionTarget` / `AxisName` / `SurfaceKind` enums, the
`CP_CROSS_AXIS_COMPOSITION_MANIFEST` constant (9 cross-axis composition
exports), the `F2_12_CarryForward` / `RevisionStep` records, and the
`F2_12_CARRY_FORWARD` declaration.

The manifest catalogs the CP-axis composition surfaces consumed cross-axis: 5
OD-load-bearing exports (OD plan Session 4) + 4 cross-axis-load-bearing exports
(Composition Session 5). The F2-12 carry-forward declares the canonical
6-step closure-path chain (D1→D6→ADD→PRD→spec→plan revision pass).

The manifest is **descriptive** — composition surfaces live at the source
units; this exports references only. U-CP-55 matches the U-IS-17 / U-AS-33
terminal-exporter shape.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.9 U-CP-55 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §24 C-CP-24 §24.2/§24.3/§24.4.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import UnitId
from pydantic import BaseModel, ConfigDict


class SessionTarget(StrEnum):
    """The downstream session a CP composition export is consumed at (§24.2)."""

    OD_PLAN_SESSION_4 = "od-plan-session-4"
    CROSS_AXIS_COMPOSITION_SESSION_5 = "cross-axis-composition-session-5"


class AxisName(StrEnum):
    """A cross-axis consumer of a CP composition surface (C-CP-24 §24.2)."""

    OD = "od"
    COMPOSITION_SESSION_5 = "composition-session-5"


class SurfaceKind(StrEnum):
    """The surface-kind discriminator of a CP composition export (§24.2)."""

    NAMESPACE_EXPORT = "namespace-export"
    TUNABLE_COMPOSITION = "tunable-composition"
    GATE_LEVEL_RULE = "gate-level-rule"
    T_PERM_3_READING = "t-perm-3-reading"
    DETERMINISTIC_BOUNDARY = "deterministic-boundary"
    AUDIT_LEDGER_INVARIANT = "audit-ledger-invariant"


class CompositionSurface(BaseModel):
    """One cross-axis composition surface (C-CP-24 §24.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cp_source_units: tuple[UnitId, ...]
    cross_axis_consumer: AxisName
    surface_kind: SurfaceKind


class CrossAxisCompositionExport(BaseModel):
    """One CP-axis cross-axis composition export (C-CP-24 §24.2/§24.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    composition_name: str
    exported_to_session: tuple[SessionTarget, ...]
    composition_surfaces: tuple[CompositionSurface, ...]
    exported_invariants: tuple[str, ...]


def _u(*ids: str) -> tuple[UnitId, ...]:
    return tuple(UnitId(i) for i in ids)


_S4 = SessionTarget.OD_PLAN_SESSION_4
_S5 = SessionTarget.CROSS_AXIS_COMPOSITION_SESSION_5


def _export(
    name: str,
    sessions: tuple[SessionTarget, ...],
    units: tuple[UnitId, ...],
    consumer: AxisName,
    kind: SurfaceKind,
    invariants: tuple[str, ...],
) -> CrossAxisCompositionExport:
    return CrossAxisCompositionExport(
        composition_name=name,
        exported_to_session=sessions,
        composition_surfaces=(
            CompositionSurface(
                cp_source_units=units,
                cross_axis_consumer=consumer,
                surface_kind=kind,
            ),
        ),
        exported_invariants=invariants,
    )


CP_CROSS_AXIS_COMPOSITION_MANIFEST: tuple[CrossAxisCompositionExport, ...] = (
    _export(
        "CP_namespace_exports",
        (_S4,),
        _u("U-CP-54"),
        AxisName.OD,
        SurfaceKind.NAMESPACE_EXPORT,
        ("11 CP namespaces; 63 attributes exported to OD plan Session 4 D6",),
    ),
    _export(
        "T_perm_3_three_layer_composition",
        (_S5,),
        _u("U-CP-53"),
        AxisName.COMPOSITION_SESSION_5,
        SurfaceKind.T_PERM_3_READING,
        ("T-perm-3 F1/D1/D4 three-layer composition reading",),
    ),
    _export(
        "five_axis_gate_level_composition",
        (_S5,),
        _u("U-CP-43", "U-CP-45"),
        AxisName.COMPOSITION_SESSION_5,
        SurfaceKind.GATE_LEVEL_RULE,
        ("5-axis gate-level composition; composes with U-AS-14 cross-axis",),
    ),
    _export(
        "sub_agent_gate_descent",
        (_S5,),
        _u("U-CP-27"),
        AxisName.COMPOSITION_SESSION_5,
        SurfaceKind.GATE_LEVEL_RULE,
        ("sub-agent gate-level monotonic descent invariant",),
    ),
    _export(
        "multi_agent_span_hierarchy",
        (_S4,),
        _u("U-CP-32"),
        AxisName.OD,
        SurfaceKind.NAMESPACE_EXPORT,
        ("multi-agent span hierarchy + per-span sampling discipline",),
    ),
    _export(
        "F2_substrate_join_at_engine_boundary",
        (_S4,),
        _u("U-CP-20"),
        AxisName.OD,
        SurfaceKind.AUDIT_LEDGER_INVARIANT,
        ("F2 substrate join at the engine boundary (R-CP-07)",),
    ),
    _export(
        "deterministic_outer_harness_boundary",
        (_S4, _S5),
        _u("U-CP-53"),
        AxisName.COMPOSITION_SESSION_5,
        SurfaceKind.DETERMINISTIC_BOUNDARY,
        ("deterministic outer harness around the probabilistic `infer` core",),
    ),
    _export(
        "per_persona_tier_audit_cryptographic_shape",
        (_S4,),
        _u("U-CP-42", "U-CP-44", "U-CP-45"),
        AxisName.OD,
        SurfaceKind.AUDIT_LEDGER_INVARIANT,
        ("per-persona-tier audit-ledger cryptographic shape",),
    ),
    _export(
        "operator_burden_eval_primitive",
        (_S4,),
        _u("U-CP-51"),
        AxisName.OD,
        SurfaceKind.TUNABLE_COMPOSITION,
        ("operator-burden eval primitive + HITL-span tail-keep rules",),
    ),
)
"""The 9 CP-axis cross-axis composition exports, C-CP-24 §24.2/§24.3 verbatim."""


class RevisionStep(BaseModel):
    """One step of the F2-12 closure-path revision-pass chain (C-CP-24 §24.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_index: int
    revision_target: str
    """Artifact + version transition."""

    rationale: str


class F2_12_CarryForward(BaseModel):  # noqa: N801 — class name encodes the F2-12 design-substrate citation ID
    """The F2-12 carry-forward declaration (C-CP-24 §24.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_engagement_unit: UnitId
    """U-CP-20 — the R-CP-07-satisfying F2 substrate-join contract."""

    closure_path: tuple[RevisionStep, ...]
    inheritance_sessions: tuple[SessionTarget, ...]
    active_at_v1: bool
    """`true` — declared (not closed) at the CP plan v1 version."""


F2_12_CARRY_FORWARD: F2_12_CarryForward = F2_12_CarryForward(
    active_engagement_unit=UnitId("U-CP-20"),
    closure_path=(
        RevisionStep(
            step_index=1,
            revision_target="ADR-D1 v1.1 -> v1.2",
            rationale="resolve resumption-observable-behavior body-citation drift",
        ),
        RevisionStep(
            step_index=2,
            revision_target="ADR-D6 v1.1 -> v1.2",
            rationale="consolidate downstream observability ingestion of D1 v1.2",
        ),
        RevisionStep(
            step_index=3,
            revision_target="ADD v1.2 -> v1.3",
            rationale=("reconsolidate engine-class + observability cross-section per revised ADRs"),
        ),
        RevisionStep(
            step_index=4,
            revision_target="PRD v1.0.1 -> v1.1",
            rationale="cite revised ADD + ADRs at R-CP-04 + R-CP-07",
        ),
        RevisionStep(
            step_index=5,
            revision_target="CP spec v1.2 -> v1.3",
            rationale="revise C-CP-08 + C-CP-09 + §24.4 to close the carry-forward",
        ),
        RevisionStep(
            step_index=6,
            revision_target="CP plan v1 -> v2",
            rationale="revision-pass mode per implementation-planner SKILL.md §8",
        ),
    ),
    inheritance_sessions=(_S4, _S5),
    active_at_v1=True,
)
"""The F2-12 carry-forward declaration — the canonical 6-step closure path
(D1 -> D6 -> ADD -> PRD -> CP spec -> CP plan revision-pass chain), C-CP-24
§24.4 verbatim."""
