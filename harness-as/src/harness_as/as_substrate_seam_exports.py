"""AS-axis substrate seam exports manifest — U-AS-33.

Implements C-AS-16 §16.1-§16.7 (the AS-axis substrate seam exports surface).
Declares the terminal aggregate exporter manifest — seven `ASSubstrateSeamExport`
records the CP / OD axes consume via stable `C-AS-16 §16.X` citations.

Declarative only — no executable behavior (acceptance #5). U-AS-33 is the
AS-axis-stream terminal unit.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-33 (verbatim CONFORM
unit per Implementation_Plan_Action_Surface_v1_1.md §0.5);
Spec_Action_Surface_v1.md §16 C-AS-16. Per OD-S2-3.A — consumer-axis dependency
declarations are authored at the CP / OD plan sessions, not here.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import UnitId
from pydantic import BaseModel, ConfigDict


class ASSeamId(StrEnum):
    """The 7 AS substrate seam exports (C-AS-16 §16.1-§16.7)."""

    SANDBOX_BOUNDED_SPAN_SCHEMA_EXPORT = "sandbox_bounded_span_schema_export"
    FIVE_AXIS_MULTIPLICATIVE_TUNABLE_EXPORT = "five_axis_multiplicative_tunable_export"
    SECRET_FETCH_AUDIT_EXPORT = "secret_fetch_audit_export"
    SIX_ANTHROPIC_PRIMITIVE_ATTRIBUTE_NAMESPACE_EXPORT = (
        "six_anthropic_primitive_attribute_namespace_export"
    )
    PER_TOOL_REQUIRED_SECRETS_EXPORT = "per_tool_required_secrets_export"
    ELEVEN_PRIMITIVE_ADOPTION_DEPTH_MATRIX_EXPORT = "eleven_primitive_adoption_depth_matrix_export"
    FORCING_CONDITION_EXPORT = "forcing_condition_export"


class ASConsumingAxis(StrEnum):
    """A downstream axis that consumes an AS substrate seam export."""

    CONTROL_PLANE = "control_plane"
    OPERATIONAL_DISCIPLINE = "operational_discipline"
    CROSS_AXIS = "cross_axis"


class ASSubstrateSeamExport(BaseModel):
    """One AS substrate seam export declaration (C-AS-16 §16.X)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seam_id: ASSeamId
    spec_citation: str
    export_surface: str
    carrier_units: tuple[UnitId, ...]
    consuming_axes: tuple[ASConsumingAxis, ...]
    composition_references: tuple[str, ...]
    cross_spec_citation_target: tuple[str, ...]


_CP = ASConsumingAxis.CONTROL_PLANE
_OD = ASConsumingAxis.OPERATIONAL_DISCIPLINE
_XA = ASConsumingAxis.CROSS_AXIS


def _units(*ids: str) -> tuple[UnitId, ...]:
    return tuple(UnitId(i) for i in ids)


#: The AS-axis substrate seam exports manifest (C-AS-16 §16.1-§16.7) — the
#: terminal aggregate exporter, consumed by CP / OD via stable citation.
AS_SUBSTRATE_SEAM_EXPORTS: tuple[ASSubstrateSeamExport, ...] = (
    ASSubstrateSeamExport(
        seam_id=ASSeamId.SANDBOX_BOUNDED_SPAN_SCHEMA_EXPORT,
        spec_citation="C-AS-16 §16.1",
        export_surface="Sandbox-bounded span schema (`sandbox.*` namespace) per C-AS-15",
        carrier_units=_units("U-AS-16", "U-AS-17", "U-AS-18"),
        consuming_axes=(_CP, _OD),
        composition_references=(
            "OD D6 v1.1 §1.2 ingests `sandbox.*` from C-AS-15 §15.2 verbatim; "
            "CP D4 v1.1 §1.9 composes sub-agent dispatch with sandbox.enter/exit; "
            "CP D5 v1.3 §1.10 pre-HITL routes `sandbox.fail.class`.",
        ),
        cross_spec_citation_target=("D6 v1.1 §1.2", "D4 v1.1 §1.9", "D5 v1.3 §1.10"),
    ),
    ASSubstrateSeamExport(
        seam_id=ASSeamId.FIVE_AXIS_MULTIPLICATIVE_TUNABLE_EXPORT,
        spec_citation="C-AS-16 §16.2",
        export_surface="5-axis multiplicative gate-level tunable per C-AS-12 §12.1",
        carrier_units=_units("U-AS-09", "U-AS-14", "U-AS-15"),
        consuming_axes=(_CP, _XA),
        composition_references=(
            "CP D5 v1.3 §1.5 multiplicative gate-level rule specialized by "
            "C-AS-12 §12.1; CP D4 v1.1 §1.5 sub-agent privilege inheritance "
            "composes with C-AS-11 monotonic-ascension; ADD §5.2.1 T-perm-1 "
            "closure at C-AS-12 §12.5.",
        ),
        cross_spec_citation_target=("D5 v1.3 §1.5", "D4 v1.1 §1.5", "ADD §5.2.1"),
    ),
    ASSubstrateSeamExport(
        seam_id=ASSeamId.SECRET_FETCH_AUDIT_EXPORT,
        spec_citation="C-AS-16 §16.3",
        export_surface="Secret-fetch structure-not-content audit composition per C-AS-08",
        carrier_units=_units("U-AS-25", "U-AS-26", "U-AS-27"),
        consuming_axes=(_OD,),
        composition_references=(
            "OD D5 v1.3 §1.4 per-persona-tier audit-ledger cryptographic shape "
            "composes at Session 4; IS C-IS-10 §10.1 export pattern.",
        ),
        cross_spec_citation_target=("D5 v1.3 §1.4", "C-IS-10 §10.1"),
    ),
    ASSubstrateSeamExport(
        seam_id=ASSeamId.SIX_ANTHROPIC_PRIMITIVE_ATTRIBUTE_NAMESPACE_EXPORT,
        spec_citation="C-AS-16 §16.4",
        export_surface="Six Anthropic-primitive attribute namespaces per C-AS-14",
        carrier_units=_units("U-AS-31", "U-AS-32"),
        consuming_axes=(_OD,),
        composition_references=(
            "OD D6 v1.1 §1.2 ingests the C-AS-14 §§14.2-14.7 namespace rows "
            "verbatim under Pattern P1 mechanical-alignment at Session 4.",
        ),
        cross_spec_citation_target=("D6 v1.1 §1.2", "D6 v1.1 §1.3"),
    ),
    ASSubstrateSeamExport(
        seam_id=ASSeamId.PER_TOOL_REQUIRED_SECRETS_EXPORT,
        spec_citation="C-AS-16 §16.5",
        export_surface="Per-tool `required_secrets` allowlist per C-AS-06",
        carrier_units=_units("U-AS-22"),
        consuming_axes=(_CP,),
        composition_references=(
            "CP D5 v1.3 §1.5 multiplicative gate-level rule; `required_secrets` "
            "orthogonal per ADR-F5 v1.1 T-perm-1 — NOT a fifth max() floor.",
        ),
        cross_spec_citation_target=("D5 v1.3 §1.5", "ADR-F5 v1.1"),
    ),
    ASSubstrateSeamExport(
        seam_id=ASSeamId.ELEVEN_PRIMITIVE_ADOPTION_DEPTH_MATRIX_EXPORT,
        spec_citation="C-AS-16 §16.6",
        export_surface="Eleven-primitive Anthropic-adoption-depth matrix per C-AS-13",
        carrier_units=_units("U-AS-28", "U-AS-29", "U-AS-30"),
        consuming_axes=(_CP, _XA),
        composition_references=(
            "CP D4 v1.1 §1.2 per-workload-class topology inherits C-AS-13 §13.4; "
            "CP D1 v1.1 §1.1 engine-class taxonomy specialized by C-AS-13 §13.3; "
            "ADD §5.2.3 T-perm-3 D3-layer adjacency.",
        ),
        cross_spec_citation_target=("D4 v1.1 §1.2", "D1 v1.1 §1.1", "ADD §5.2.3"),
    ),
    ASSubstrateSeamExport(
        seam_id=ASSeamId.FORCING_CONDITION_EXPORT,
        spec_citation="C-AS-16 §16.7",
        export_surface="Forcing-condition cell resolution per C-AS-01 §1.3 / C-AS-09 §9.3",
        carrier_units=_units("U-AS-02", "U-AS-10"),
        consuming_axes=(_CP,),
        composition_references=(
            "CP D5 v1.3 §1.10 pre-HITL escalation skips the C-AS-04 §4.2 "
            "staircase for `escape_attempt` / `egress_denied` / `signal`.",
        ),
        cross_spec_citation_target=("D5 v1.3 §1.10",),
    ),
)

_BY_SEAM: dict[ASSeamId, ASSubstrateSeamExport] = {
    export.seam_id: export for export in AS_SUBSTRATE_SEAM_EXPORTS
}


def as_seam_carrier_units(seam: ASSeamId) -> tuple[UnitId, ...]:
    """Return the carrier units of an AS substrate seam (C-AS-16 §16.X)."""
    return _BY_SEAM[seam].carrier_units


def as_seam_consuming_axes(seam: ASSeamId) -> tuple[ASConsumingAxis, ...]:
    """Return the consuming axes of an AS substrate seam (C-AS-16 §16.X)."""
    return _BY_SEAM[seam].consuming_axes


def as_seam_export_surface(seam: ASSeamId) -> str:
    """Return the export-surface description of an AS substrate seam (§16.X)."""
    return _BY_SEAM[seam].export_surface
