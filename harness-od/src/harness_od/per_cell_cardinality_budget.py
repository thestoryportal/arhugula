"""Per-cell cardinality budget + Pattern P1 discipline anchor — U-OD-13.

Implements C-OD-11 §11.1 (per-cell cardinality budget posture — per-cell rate
limits at the OTLP collector boundary or at backend ingestion) and §11.4
(Pattern P1 cardinality discipline anchor — per-attribute name byte-exact
alignment across all source artifacts).

`PER_CELL_CARDINALITY_BUDGET` carries one `PerCellCardinalityBudget` per ACTIVE
cell of the 9-cell matrix (C-OD-01 §1.3) — 8 entries (the 9th cell,
`multi-tenant-compliance x local-development`, is structurally EXCLUDED).
`PATTERN_P1_DISCIPLINE_ANCHOR` is the declarative invariant against
per-attribute-name drift; it carries the §11.4 anchor text.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.4.3 U-OD-13
(body preserved verbatim through v2.5 / v2.6 per v2.6 §3 pointer table);
effective Depends on: [U-OD-01, U-OD-05] — the v2.1 `U-OD-11` edge is struck
per `.harness/class_1_tension_u_od_13_topological_misplacement.md` RESOLUTION
(Option B — U-OD-13 consumes no U-OD-11 surface; the edge was stale);
Spec_Operational_Discipline_v1_2.md §11 C-OD-11 §11.1 + §11.4 (preserved
verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.3 cardinality-budget-per-cell
paragraph.
"""

from __future__ import annotations

from typing import Literal

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.observability_matrix import ACTIVE_CELLS, CellID

__all__ = [
    "PATTERN_P1_DISCIPLINE_ANCHOR",
    "PER_CELL_CARDINALITY_BUDGET",
    "EnforcementLayer",
    "PerCellCardinalityBudget",
]

#: The `enforcement_layer` admissible value set — C-OD-11 §11.1: per-cell rate
#: limits at the OTLP collector boundary OR at backend ingestion. A single
#: value per cell (each entry of `PER_CELL_CARDINALITY_BUDGET` commits one).
type EnforcementLayer = Literal["COLLECTOR_BOUNDARY", "BACKEND_INGESTION"]


class PerCellCardinalityBudget(BaseModel):
    """The cardinality budget committed for one ACTIVE matrix cell (C-OD-11 §11.1).

    Frozen → `Eq` + `Hash`, stable under serialization. `tenant_rate_limit` is
    `None` at non-multi-tenant cells and a `float` (per-tenant spans/sec) at the
    two multi-tenant cells per C-OD-21 §21.4 per-tenant cardinality isolation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: per-tenant rate limit (spans/sec) — `None` at non-multi-tenant cells.
    tenant_rate_limit: float | None
    #: per-cell global rate limit (spans/sec).
    cell_rate_limit: float
    #: where the per-cell rate limit is enforced.
    enforcement_layer: EnforcementLayer


def _enforcement_layer_for(cell: CellID) -> EnforcementLayer:
    """Resolve the §11.1 enforcement layer for `cell` (acceptance #3).

    Solo-developer cells enforce at `COLLECTOR_BOUNDARY` — the in-process OTLP
    collector against the sqlite ring-buffer (§11.1 solo row, per C-OD-19).
    Team-binding and multi-tenant cells "enforce at either layer per
    cell-committed backend" (plan AC #3) — single-valued per cell.
    Per §11.1, multi-tenant-compliance x self-hosted-server runs per-tenant
    collector instances (C-OD-21), so enforces at `COLLECTOR_BOUNDARY`; all
    other team/multi-tenant cells run real backends (not the in-process sqlite
    ring-buffer), so enforce at `BACKEND_INGESTION`.
    """
    if cell.persona_tier is PersonaTier.SOLO_DEVELOPER:
        return "COLLECTOR_BOUNDARY"
    if (
        cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE
        and cell.deployment_surface is DeploymentSurface.SELF_HOSTED_SERVER
    ):
        return "COLLECTOR_BOUNDARY"
    return "BACKEND_INGESTION"


def _tenant_rate_limit_for(cell: CellID) -> float | None:
    """Resolve the §11.1 / C-OD-21 per-tenant rate limit for `cell` (acceptance #2).

    `Some` (a `float`) only at the two multi-tenant-compliance ACTIVE cells
    (cell-7 self-hosted-server, cell-8 managed-cloud); `None` everywhere else.
    The numeric threshold is operator-tunable per §11.4 ("Deferred to
    implementation discretion"); the carrier commits a non-`None` default.
    """
    if cell.persona_tier is PersonaTier.MULTI_TENANT_COMPLIANCE:
        return 1000.0
    return None


#: The §11.4 Pattern P1 cardinality discipline anchor — declarative invariant
#: against per-attribute-name drift across source artifacts (acceptance #4,
#: byte-exact).
PATTERN_P1_DISCIPLINE_ANCHOR: str = (
    "Per-attribute names MUST be byte-exact across OD spec / AS spec / CP spec "
    "/ IS spec / ADRs / OTel SDK bindings. Pattern P1 was raised at P3c-CK "
    "Iteration 1 as a systemic per-attribute name drift across six or more "
    "source artifacts. Compliance discipline preserved at all 15 "
    "specialization-layer namespace declarations."
)

#: The per-cell cardinality budget — exactly one entry per ACTIVE cell
#: (C-OD-11 §11.1; cardinality 8 per C-OD-01 §1.3 ACTIVE-cell count).
PER_CELL_CARDINALITY_BUDGET: dict[CellID, PerCellCardinalityBudget] = {
    cell: PerCellCardinalityBudget(
        cell_id=cell,
        tenant_rate_limit=_tenant_rate_limit_for(cell),
        cell_rate_limit=10_000.0,
        enforcement_layer=_enforcement_layer_for(cell),
    )
    for cell in ACTIVE_CELLS
}
