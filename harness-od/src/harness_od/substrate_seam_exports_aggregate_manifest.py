"""OD substrate seam exports aggregate manifest — U-OD-34.

THE TERMINAL AGGREGATE EXPORTER of the OD axis-stream. Implements C-OD-23 §23.1
(substrate seam exports aggregate manifest — 8 export sub-sections), §23.2
(F2-12 carry-forward inheritance from C-OD-14 §14.5), §23.3 (cross-axis edge
aggregate per OD-S4-3.A), §23.4 (manifest scope — terminal aggregate for Phase
6+ implementation).

`SubstrateSeamExport` is one OD-axis substrate-seam export — a named surface the
OD axis exposes to consuming axes. `SubstrateSeamExportsManifest` aggregates the
8 export sub-sections + the cross-axis edge aggregate + the F2-12 carry-forward
inheritance declaration. `OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST` is the const
terminal manifest — the OD axis's aggregate hand-off to sub-phase 7c cross-axis
composition.

OD is the consumer-most-downstream axis (CXA v2.1 §2.1) — 0 outbound cross-axis
edges; the cross-axis edges this manifest aggregates are OD's 26 inbound
consumer edges. The manifest is OD's terminal export surface.

Cross-axis terminal-exporter references (resolve at sub-phase 7c — NOT imported
here). The manifest references the 4 terminal-aggregate exporter / inheritance
targets in the 3 prior axis plans: U-IS-17 (IS terminal aggregate exporter),
U-AS-33 (AS terminal aggregate exporter), U-CP-54 (CP terminal aggregate
exporter — namespace map), U-CP-55 (CP F2-12 ACTIVE inheritance). These are
string-typed target identifiers; the edge wiring lands at 7c per CXA v2.1.

v2.6 M-3 conformance. The `cross_axis_edge_count` and `cross_axis_edge_breakdown`
scalar fields are conformed to the v2.4 §4.5.1-canonical **26 total / {IS: 4,
AS: 10, CP: 12}** — a determinate propagation of the operator-ratified C3-15
Path (i-refined) IS-consuming-edge delete/remap (the v2.1 body's stale `28` /
`{IS: 6, ...}` predated that delta). The 8-export manifest content table and the
per-export `cross_axis_edge_targets` lists are unchanged — the M-3 fix corrects
only the aggregate scalar fields.

Dependency note. U-OD-34's `Depends on` list is the 19 within-OD edges
[U-OD-04/05/06/07/08/09/10/11/17/18/19/20/21/23/27/28/30/32/33] + the 4
cross-axis terminal-exporter references. It does NOT include U-OD-29 — U-OD-29
(per-sandbox-tier OTLP reachability) is not a U-OD-34 dependency; U-OD-34
aggregates the substrate-seam exports, and U-OD-29's surface is not one of the 8
exports. (U-OD-29 is separately halted on the FF-3 Class 1 fork; it is a leaf.)
This manifest carries the dependency unit identifiers as `source_unit` string
references — it does not import the dependency modules' types.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.8.3 U-OD-34
(v2.6 M-3 CONFORM revision — `cross_axis_edge_count` 28->26,
`cross_axis_edge_breakdown` {IS:6,...}->{IS:4,...}; all other surfaces preserved
verbatim from v2.1 §3.8.3); v2.1 §3.8.3 (base unit body);
Spec_Operational_Discipline_v1_2.md §23 C-OD-23 (preserved verbatim into v1.4
per v1.4 §0).

Depends on: [U-OD-04, U-OD-05, U-OD-06, U-OD-07, U-OD-08, U-OD-09, U-OD-10,
U-OD-11, U-OD-17, U-OD-18, U-OD-19, U-OD-20, U-OD-21, U-OD-23, U-OD-27, U-OD-28,
U-OD-30, U-OD-32, U-OD-33] (19 within-OD edges) + [U-IS-17, U-AS-33, U-CP-54,
U-CP-55] (4 cross-axis terminal-exporter references — resolve at 7c).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST",
    "ConsumerAxis",
    "F2_12_CarryForwardInheritance",
    "ManifestScope",
    "SubstrateSeamExport",
    "SubstrateSeamExportsManifest",
]


# --- §23.1 export record types ---------------------------------------------


class ConsumerAxis(StrEnum):
    """The axes that consume an OD substrate-seam export (C-OD-23 §23.1)."""

    INFORMATION_SUBSTRATE = "INFORMATION_SUBSTRATE"
    ACTION_SURFACE = "ACTION_SURFACE"
    CONTROL_PLANE = "CONTROL_PLANE"
    PHASE_6_IMPLEMENTATION = "PHASE_6_IMPLEMENTATION"


class SubstrateSeamExport(BaseModel):
    """One OD-axis substrate-seam export sub-section (C-OD-23 §23.1).

    A named surface the OD axis exposes to consuming axes. `source_unit` is the
    declaring OD unit identifier (e.g. `"U-OD-04"`); `contract_anchor` is the
    OD spec section (e.g. `"C-OD-04 §4.1"`); `cross_axis_edge_targets` lists the
    IS / AS / CP resolution targets, resolved at sub-phase 7c.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    export_name: str
    source_unit: str
    contract_anchor: str
    consumer_axis: frozenset[ConsumerAxis]
    cross_axis_edge_targets: tuple[str, ...]


class ManifestScope(StrEnum):
    """The scope of the substrate seam exports manifest (C-OD-23 §23.4)."""

    TERMINAL_AGGREGATE_FOR_PHASE_6_PLUS_IMPLEMENTATION = (
        "TERMINAL_AGGREGATE_FOR_PHASE_6_PLUS_IMPLEMENTATION"
    )
    CROSS_AXIS_COMPOSITION_VERIFICATION_AT_SESSION_5 = (
        "CROSS_AXIS_COMPOSITION_VERIFICATION_AT_SESSION_5"
    )


class F2_12_CarryForwardInheritance(BaseModel):  # noqa: N801 — name is the U-OD-34 plan signature verbatim
    """The F2-12 carry-forward inheritance declaration (C-OD-23 §23.2).

    Declares U-OD-20 as the sole F2-12 ACTIVE contract-bearing site in the OD
    plan; the closure path inherits its 6-step structure from CP plan U-CP-55
    §24.4. `closure_pending_at_v1` + `partial_closure_rejected` encode the
    all-6-steps-in-canonical-order closure discipline.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    inherited_from: Literal["CP plan U-CP-55 §24.4"]
    contract_bearing_site: Literal["U-OD-20 implementing C-OD-14 §14.5"]
    closure_path_step_count: int
    closure_target: Literal["OD plan v2 (revision-pass mode per SKILL.md §8)"]
    closure_pending_at_v1: bool
    partial_closure_rejected: bool
    forward_routing: Literal[
        "parallel council-orchestrator C7+C9 session per ADD §6.3.1 active path"
    ]


class SubstrateSeamExportsManifest(BaseModel):
    """The OD substrate seam exports aggregate manifest (C-OD-23 §23.1-§23.4).

    The OD axis's terminal aggregate export surface. `exports` is the 8-export
    sub-section list per §23.1; `cross_axis_edge_count` /
    `cross_axis_edge_breakdown` are the §23.3 cross-axis edge aggregate
    (v2.6 M-3-conformed to the v2.4 §4.5.1-canonical 26 / {IS:4, AS:10, CP:12});
    `f2_12_carry_forward_inheritance` is the §23.2 declaration; `manifest_scope`
    is the §23.4 terminal-aggregate scope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    exports: tuple[SubstrateSeamExport, ...]
    cross_axis_edge_count: int
    cross_axis_edge_breakdown: dict[ConsumerAxis, int]
    f2_12_carry_forward_inheritance: F2_12_CarryForwardInheritance
    manifest_scope: ManifestScope


# --- §23.1 the 8 export sub-sections ---------------------------------------

_EXPORTS: tuple[SubstrateSeamExport, ...] = (
    SubstrateSeamExport(
        export_name="OTel GenAI semconv 1.41.0 base-layer attribute set",
        source_unit="U-OD-04",
        contract_anchor="C-OD-04 §4.1-§4.5",
        consumer_axis=frozenset(
            {
                ConsumerAxis.INFORMATION_SUBSTRATE,
                ConsumerAxis.ACTION_SURFACE,
                ConsumerAxis.CONTROL_PLANE,
                ConsumerAxis.PHASE_6_IMPLEMENTATION,
            }
        ),
        cross_axis_edge_targets=(),
    ),
    SubstrateSeamExport(
        export_name="15-row namespace ingestion map",
        source_unit="U-OD-05",
        contract_anchor="C-OD-05 §5.1",
        consumer_axis=frozenset(
            {
                ConsumerAxis.INFORMATION_SUBSTRATE,
                ConsumerAxis.ACTION_SURFACE,
                ConsumerAxis.CONTROL_PLANE,
                ConsumerAxis.PHASE_6_IMPLEMENTATION,
            }
        ),
        cross_axis_edge_targets=("U-AS-33", "U-CP-54"),
    ),
    SubstrateSeamExport(
        export_name="F3 lifecycle event-to-span-event mapping (8 events)",
        source_unit="U-OD-08",
        contract_anchor="C-OD-06 §6.1",
        consumer_axis=frozenset({ConsumerAxis.CONTROL_PLANE, ConsumerAxis.PHASE_6_IMPLEMENTATION}),
        cross_axis_edge_targets=("U-CP-54",),
    ),
    SubstrateSeamExport(
        export_name=("harness.breaker.* 7-attribute substrate-anchored canonical schema"),
        source_unit="U-OD-09",
        contract_anchor="C-OD-07 §7.1",
        consumer_axis=frozenset({ConsumerAxis.CONTROL_PLANE, ConsumerAxis.PHASE_6_IMPLEMENTATION}),
        cross_axis_edge_targets=("U-CP-54",),
    ),
    SubstrateSeamExport(
        export_name=("18-entry always-sampled set + 13-entry base-rate set + per-cell envelope"),
        source_unit="U-OD-11 + U-OD-12",
        contract_anchor="C-OD-09 §9.2 + C-OD-10 §10.1",
        consumer_axis=frozenset({ConsumerAxis.PHASE_6_IMPLEMENTATION}),
        cross_axis_edge_targets=(),
    ),
    SubstrateSeamExport(
        export_name=("Per-span cost formula + idempotency-key join + cross-family rollup"),
        source_unit="U-OD-18 + U-OD-20 + U-OD-21",
        contract_anchor="C-OD-14 §14.1, §14.4, §14.5 + C-OD-15 §15.1",
        consumer_axis=frozenset(
            {
                ConsumerAxis.INFORMATION_SUBSTRATE,
                ConsumerAxis.ACTION_SURFACE,
                ConsumerAxis.CONTROL_PLANE,
                ConsumerAxis.PHASE_6_IMPLEMENTATION,
            }
        ),
        cross_axis_edge_targets=("U-IS-17", "U-AS-33", "U-CP-54", "U-CP-55"),
    ),
    SubstrateSeamExport(
        export_name=("Local-first OTLP collector at cell-1 + per-cell collector placement matrix"),
        source_unit="U-OD-27 + U-OD-28",
        contract_anchor="C-OD-19 §19.1-§19.3 + C-OD-20 §20.1",
        consumer_axis=frozenset(
            {
                ConsumerAxis.INFORMATION_SUBSTRATE,
                ConsumerAxis.PHASE_6_IMPLEMENTATION,
            }
        ),
        cross_axis_edge_targets=("U-IS-17",),
    ),
    SubstrateSeamExport(
        export_name=(
            "Multi-tenant per-tenant separation + audit ledger + bridging-arc "
            "8-transition table + preservation invariants"
        ),
        source_unit="U-OD-30 + U-OD-32 + U-OD-33",
        contract_anchor="C-OD-21 §21.1 + C-OD-22 §22.1-§22.4",
        consumer_axis=frozenset(
            {
                ConsumerAxis.INFORMATION_SUBSTRATE,
                ConsumerAxis.ACTION_SURFACE,
                ConsumerAxis.CONTROL_PLANE,
                ConsumerAxis.PHASE_6_IMPLEMENTATION,
            }
        ),
        cross_axis_edge_targets=("U-IS-17", "U-AS-33", "U-CP-54"),
    ),
)


# --- §23.2 F2-12 carry-forward inheritance ---------------------------------

_F2_12_INHERITANCE: F2_12_CarryForwardInheritance = F2_12_CarryForwardInheritance(
    inherited_from="CP plan U-CP-55 §24.4",
    contract_bearing_site="U-OD-20 implementing C-OD-14 §14.5",
    closure_path_step_count=6,
    closure_target="OD plan v2 (revision-pass mode per SKILL.md §8)",
    closure_pending_at_v1=True,
    partial_closure_rejected=True,
    forward_routing=("parallel council-orchestrator C7+C9 session per ADD §6.3.1 active path"),
)


# --- §23.3 cross-axis edge aggregate (v2.6 M-3-conformed) ------------------

#: The OD cross-axis edge aggregate — v2.6 M-3-conformed to the v2.4 §4.5.1
#: canonical 26 total / {IS:4, AS:10, CP:12} (the v2.1 body's stale 28 /
#: {IS:6, ...} predated the operator-ratified C3-15 Path (i-refined) delta).
_CROSS_AXIS_EDGE_BREAKDOWN: dict[ConsumerAxis, int] = {
    ConsumerAxis.INFORMATION_SUBSTRATE: 4,
    ConsumerAxis.ACTION_SURFACE: 10,
    ConsumerAxis.CONTROL_PLANE: 12,
}
_CROSS_AXIS_EDGE_COUNT: int = 26


# --- the terminal aggregate manifest const ---------------------------------

#: THE TERMINAL AGGREGATE EXPORTER — the OD axis's substrate seam exports
#: aggregate manifest. Closes the OD axis-stream; consumed at sub-phase 7c
#: cross-axis composition.
OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST: SubstrateSeamExportsManifest = SubstrateSeamExportsManifest(
    exports=_EXPORTS,
    cross_axis_edge_count=_CROSS_AXIS_EDGE_COUNT,
    cross_axis_edge_breakdown=_CROSS_AXIS_EDGE_BREAKDOWN,
    f2_12_carry_forward_inheritance=_F2_12_INHERITANCE,
    manifest_scope=(ManifestScope.TERMINAL_AGGREGATE_FOR_PHASE_6_PLUS_IMPLEMENTATION),
)

#: Closure invariant — the manifest declares exactly 8 export sub-sections
#: (C-OD-23 §23.1, acc #1) and the M-3-conformed cross-axis edge aggregate
#: (acc #3/#4).
assert len(OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST.exports) == 8, (
    "OD substrate seam exports manifest must declare exactly 8 export sub-sections (C-OD-23 §23.1)"
)
assert OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST.cross_axis_edge_count == sum(
    _CROSS_AXIS_EDGE_BREAKDOWN.values()
), "cross_axis_edge_count must equal the sum of the per-axis breakdown"
