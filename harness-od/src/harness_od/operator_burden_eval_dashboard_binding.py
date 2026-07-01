"""Per-cell operator-burden eval dashboard binding scaling — U-OD-24.

Implements C-OD-17 §17.3 (per-cell dashboard binding scaling — the three
cell-class binding forms for the operator-burden eval primitives).

`EvalDashboardForm` / `AlignmentFloorAlertingPosture` / `HusainLoopBinding`
each enumerate the 3 per-cell-class scaling forms per §17.3 (one row per cell
class: solo-developer / team-binding / multi-tenant-compliance).
`CellEvalDashboardBinding` records one ACTIVE cell's binding;
`PER_CELL_EVAL_DASHBOARD_BINDINGS` declares one binding per ACTIVE cell
(exactly 8 entries — the 3x3 persona-tier x deployment-surface matrix minus the
structurally-excluded `multi-tenant-compliance x local-development` cell).
`HusainLoopState` records the state of one Husain manual-review -> categorize
-> automate -> align loop iteration at a cell; `run_husain_loop_at_cell`
returns it.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.6.2 U-OD-24
(v2.6 M-1 revision — `HusainLoopState` declared in-unit, single-consumer; all
v2.1 surfaces preserved verbatim from v2.1 §3.6.2);
Spec_Operational_Discipline_v1_2.md §17 C-OD-17 §17.3 (preserved verbatim into
v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.6.

Depends on: [U-OD-01, U-OD-22, U-OD-23, U-OD-27]. `CellID` / `ACTIVE_CELLS` /
`reject_excluded_cell` from U-OD-01 (`observability_matrix`);
`OperatorBurdenEvalPrimitive` from U-OD-23
(`operator_burden_eval_primitives`). U-OD-22 (cost dashboard) is the §16.3
consolidation reference; U-OD-27 (C-OD-19 TUI trace browser) is the solo-cell
ring-buffer surface — both are structural-composition references, no type
import is induced. `HusainLoopState` is declared in-unit (single OD consumer,
v2.6 M-1) — no carrier unit, no carrier edge.

The v2.1 acceptance criteria enumerate cells by ordinal index ("solo cells
1,2,3" / "team cells 4,5,6" / "multi-tenant cells 7,8"); that enumeration
predates the formal `CellID(persona_tier x deployment_surface)` model. The
canonical cell key is `CellID`; binding is keyed off `persona_tier` exactly as
the U-OD-22 cost-dashboard binding does — the cell-class groups are the three
`PersonaTier` values, not numeric indices.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    CellID,
    reject_excluded_cell,
)
from harness_od.operator_burden_eval_primitives import OperatorBurdenEvalPrimitive

__all__ = [
    "PER_CELL_EVAL_DASHBOARD_BINDINGS",
    "AlignmentFloorAlertingPosture",
    "CellEvalDashboardBinding",
    "EvalDashboardForm",
    "HusainLoopBinding",
    "HusainLoopState",
    "run_husain_loop_at_cell",
]


class EvalDashboardForm(StrEnum):
    """The per-cell-class operator-burden eval dashboard form (C-OD-17 §17.3).

    Exactly 3 values, one per §17.3 cell-class row.

    `TUI_RING_BUFFER_SCOPED_QUERIES` — solo-developer cells: the five
    primitives surface as scoped queries against the sqlite ring-buffer (no
    separate dashboard layer; operator inspects the ring-buffer directly via
    the C-OD-19 TUI trace browser). `NAMED_DASHBOARD_QUERIES_WITH_ALIGNMENT_
    FLOOR_ALERTING` — team-binding cells: the five primitives bind as named
    dashboard queries on the cell-committed backend with alignment-floor
    alerting. `PER_TENANT_SEPARATION_WITH_COMPLIANCE_ATTESTATION_ALERTING` —
    multi-tenant-compliance cells: per-tenant dashboard separation with
    compliance-attestation alerting thresholds.
    """

    TUI_RING_BUFFER_SCOPED_QUERIES = "TUI_RING_BUFFER_SCOPED_QUERIES"
    NAMED_DASHBOARD_QUERIES_WITH_ALIGNMENT_FLOOR_ALERTING = (
        "NAMED_DASHBOARD_QUERIES_WITH_ALIGNMENT_FLOOR_ALERTING"
    )
    PER_TENANT_SEPARATION_WITH_COMPLIANCE_ATTESTATION_ALERTING = (
        "PER_TENANT_SEPARATION_WITH_COMPLIANCE_ATTESTATION_ALERTING"
    )


class AlignmentFloorAlertingPosture(StrEnum):
    """The per-cell-class alignment-floor alerting posture (C-OD-17 §17.3).

    Exactly 3 values, one per §17.3 cell-class row.

    `OPERATOR_SELF_CURATION_VIA_TUI` — solo-developer cells: no backend
    alerting; the operator self-curates via the TUI ring-buffer.
    `ALIGNMENT_FLOOR_BOUND_TO_BACKEND_ALERTING` — team-binding cells: the
    alignment-floor ratios bind to the cell-committed backend's alerting (the
    U-OD-25 drift-detection composition surface). `PER_TENANT_ALIGNMENT_FLOOR_
    NO_CROSS_TENANT` — multi-tenant-compliance cells: per-tenant alignment-floor
    binding; cross-tenant aggregation forbidden per C-OD-21.
    """

    OPERATOR_SELF_CURATION_VIA_TUI = "OPERATOR_SELF_CURATION_VIA_TUI"
    ALIGNMENT_FLOOR_BOUND_TO_BACKEND_ALERTING = "ALIGNMENT_FLOOR_BOUND_TO_BACKEND_ALERTING"
    PER_TENANT_ALIGNMENT_FLOOR_NO_CROSS_TENANT = "PER_TENANT_ALIGNMENT_FLOOR_NO_CROSS_TENANT"


class HusainLoopBinding(StrEnum):
    """The per-cell-class Husain-loop tooling binding (C-OD-17 §17.3 row 1).

    Exactly 3 values, one per §17.3 cell-class row.

    `RING_BUFFER_OPERATOR_SELF_CURATION` — solo-developer cells: the Husain
    manual-review -> categorize -> automate -> align loop runs against the
    sqlite ring-buffer with operator self-curation. `BACKEND_HOSTED` —
    team-binding cells: the loop runs against the cell-committed backend.
    `PER_TENANT_BACKEND_HOSTED` — multi-tenant-compliance cells: the loop runs
    against the cell-committed backend, per-tenant.
    """

    RING_BUFFER_OPERATOR_SELF_CURATION = "RING_BUFFER_OPERATOR_SELF_CURATION"
    BACKEND_HOSTED = "BACKEND_HOSTED"
    PER_TENANT_BACKEND_HOSTED = "PER_TENANT_BACKEND_HOSTED"


class CellEvalDashboardBinding(BaseModel):
    """One ACTIVE cell's operator-burden eval dashboard binding (C-OD-17 §17.3).

    Frozen → `Eq` + `Hash`, stable under serialization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: the §17.3 dashboard form for the cell class.
    dashboard_form: EvalDashboardForm
    #: the §17.3 alignment-floor alerting posture for the cell class.
    alignment_floor_alerting: AlignmentFloorAlertingPosture
    #: the §17.3 row-1 Husain-loop tooling binding for the cell class.
    husain_loop_binding: HusainLoopBinding
    #: §16.3 — the cell MAY consolidate the operator-burden eval dashboard with
    #: the U-OD-22 cost-attribution dashboard (`True` permissible per §16.3).
    consolidates_with_cost: bool


# --- v2.6 addition: in-unit `HusainLoopState` declaration (M-1, single-consumer)
# `HusainLoopState` has exactly one OD consumer (U-OD-24 — the
# `run_husain_loop_at_cell` return). Single-consumer → declared in-unit; no
# carrier unit, no carrier edge. Faithful factor-out of the C-OD-17 §17.3
# Husain-loop binding concept, not a design extension.


class HusainLoopState(BaseModel):
    """The state of one Husain manual-review -> categorize -> automate -> align
    loop iteration at a cell (C-OD-17 §17.3 Husain-loop binding).

    Faithful factor-out of the §17.3 Husain-loop binding concept. Frozen →
    `Eq`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key the loop iteration runs at (U-OD-01).
    cell_id: CellID
    #: the cell-class Husain-loop tooling binding (§17.3 row 1).
    loop_binding: HusainLoopBinding
    #: the operator-burden eval primitive under review (from U-OD-23).
    primitive: OperatorBurdenEvalPrimitive
    #: the loop phase — one of "manual-review" / "categorize" / "automate" /
    #: "align" (§17.3 Husain manual-review -> categorize -> automate -> align).
    iteration_phase: str


def _binding_for(cell: CellID) -> CellEvalDashboardBinding:
    """Construct the §17.3 binding for `cell` from its persona-tier class.

    solo-developer cells → TUI ring-buffer scoped queries; team-binding cells →
    named dashboard queries with alignment-floor alerting; multi-tenant-
    compliance cells → per-tenant separation with compliance-attestation
    alerting (C-OD-17 §17.3 — acc #3 / #4 / #5).
    """
    if cell.persona_tier is PersonaTier.SOLO_DEVELOPER:
        form = EvalDashboardForm.TUI_RING_BUFFER_SCOPED_QUERIES
        alerting = AlignmentFloorAlertingPosture.OPERATOR_SELF_CURATION_VIA_TUI
        loop = HusainLoopBinding.RING_BUFFER_OPERATOR_SELF_CURATION
    elif cell.persona_tier is PersonaTier.TEAM_BINDING:
        form = EvalDashboardForm.NAMED_DASHBOARD_QUERIES_WITH_ALIGNMENT_FLOOR_ALERTING
        alerting = AlignmentFloorAlertingPosture.ALIGNMENT_FLOOR_BOUND_TO_BACKEND_ALERTING
        loop = HusainLoopBinding.BACKEND_HOSTED
    else:  # MULTI_TENANT_COMPLIANCE
        form = EvalDashboardForm.PER_TENANT_SEPARATION_WITH_COMPLIANCE_ATTESTATION_ALERTING
        alerting = AlignmentFloorAlertingPosture.PER_TENANT_ALIGNMENT_FLOOR_NO_CROSS_TENANT
        loop = HusainLoopBinding.PER_TENANT_BACKEND_HOSTED
    return CellEvalDashboardBinding(
        cell_id=cell,
        dashboard_form=form,
        alignment_floor_alerting=alerting,
        husain_loop_binding=loop,
        consolidates_with_cost=True,
    )


#: The per-cell operator-burden eval dashboard bindings — exactly 8 entries,
#: one per ACTIVE cell (C-OD-17 §17.3; acc #2). solo cells → TUI ring-buffer
#: scoped queries; team cells → named dashboard queries with alignment-floor
#: alerting; multi-tenant cells → per-tenant separation with compliance-
#: attestation alerting.
PER_CELL_EVAL_DASHBOARD_BINDINGS: dict[CellID, CellEvalDashboardBinding] = {
    cell: _binding_for(cell) for cell in ACTIVE_CELLS
}


def run_husain_loop_at_cell(
    cell_id: CellID,
    primitive: OperatorBurdenEvalPrimitive,
) -> HusainLoopState:
    """Initiate the Husain manual-review -> categorize -> automate -> align loop
    for `primitive` at `cell_id` (C-OD-17 §17.3 row 1).

    Returns the `HusainLoopState` for the loop iteration's opening phase
    ("manual-review"). The loop's tooling binding is the cell-class binding
    from `PER_CELL_EVAL_DASHBOARD_BINDINGS` — solo cells run the loop against
    the sqlite ring-buffer with operator self-curation; team and multi-tenant
    cells run it against the cell-committed backend (§17.3 row 1).

    The structurally-excluded cell (`multi-tenant-compliance x local-
    development`, C-OD-01 §1.4) is rejected — no eval dashboard binding exists
    for it.
    """
    reject_excluded_cell(cell_id)
    binding = PER_CELL_EVAL_DASHBOARD_BINDINGS[cell_id]
    return HusainLoopState(
        cell_id=cell_id,
        loop_binding=binding.husain_loop_binding,
        primitive=primitive,
        iteration_phase="manual-review",
    )
