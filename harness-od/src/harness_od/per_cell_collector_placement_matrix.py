"""Per-cell OTLP collector placement matrix — U-OD-28.

Implements C-OD-20 §20.1 (per-cell collector placement matrix — the 7-value
`CollectorPlacement` architectural-class enum + the 8-cell `Cell ->
Set<CollectorPlacement>` mapping), §20.2 (BatchSpanProcessor async emission
universality).

`CollectorPlacement` enumerates the seven architectural placement classes per
`Spec_Operational_Discipline_v1_4.md` §20.1 verbatim. `PerCellPlacement` carries,
for one ACTIVE matrix cell, the cell's non-empty `placement_classes`
(`frozenset[CollectorPlacement]`) and the §20.2 BatchSpanProcessor async
emission window. `PER_CELL_COLLECTOR_PLACEMENT` declares one `PerCellPlacement`
per ACTIVE cell — exactly 8 entries. `collector_placement` returns the set;
`assert_async_emission_universality` enforces the §20.2 universality invariant.

v2.9 (FF-2): the v1.4 §20.1 7-value architectural-class enum replaces the v2.1
plan-introduced deployment-topology taxonomy. `PerCellPlacement.placement_classes`
is set-valued — singleton for the six committed cells (1/3/5/6/8) and 2-element
for the three alt-route cells (2/4/7) whose §20.1 prose commits a design-time
disjunction. The operator selects one alternant from a 2-element set at
deployment-binding time.

Disposition note (plan-vs-landed-carrier). The v2.9 §3.7.2 signature block names
the §20.2 emission window `emission_window : Duration` inheriting from a U-OD-27
constant. U-OD-27's landed carrier exports `BATCH_SPAN_PROCESSOR_WINDOW_SECONDS:
int` (seconds) and `BATCH_SPAN_PROCESSOR_BATCH_SIZE: int`; there is no `Duration`
type in the stack and the v1.2 spec §20.2 defines none. Acc #5 binds the fields
to "U-OD-27 constants" (the operative constraint) — this module inherits the
landed U-OD-27 constant names verbatim and types `emission_window` as `int`
(seconds), matching the landed carrier. Not a Class 1 fork — the schematic
`Duration` notation is prose; the acceptance criterion binds to the constants.

Authority: Implementation_Plan_Operational_Discipline_v2_9.md §3.7.2 U-OD-28
(v2.9 FF-2 revision — `CollectorPlacement` conformed to the v1.4 §20.1 7-value
enum; per-cell mapping widened to `Set<CollectorPlacement>`; all other v2.1
surfaces preserved verbatim);
Spec_Operational_Discipline_v1_4.md §20.1 (the FF-2 spec fix — explicit enum
declaration + `Cell -> Set<CollectorPlacement>` mapping) + §20.2 (BatchSpanProcessor
async emission discipline, preserved verbatim from v1.2);
ADR-D6 v1.1 §1.7 (per-cell collector placement table).

Depends on: [U-OD-01] — `CellID` / `ACTIVE_CELLS` from `observability_matrix`;
[U-OD-02] — per-cell binding convention mirrored; [U-OD-27] — the
`BATCH_SPAN_PROCESSOR_WINDOW_SECONDS` / `BATCH_SPAN_PROCESSOR_BATCH_SIZE`
constants from `local_first_otlp_collector`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.local_first_otlp_collector import (
    BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
)
from harness_od.observability_matrix import ACTIVE_CELLS, CellID

__all__ = [
    "PER_CELL_COLLECTOR_PLACEMENT",
    "CollectorPlacement",
    "EmissionModeViolation",
    "PerCellPlacement",
    "assert_async_emission_universality",
    "collector_placement",
]


# --- §20.1 CollectorPlacement enum (v1.4 — canonical declaration) ----------


class CollectorPlacement(StrEnum):
    """The 7 architectural collector-placement classes (C-OD-20 §20.1).

    Exactly 7 values per `Spec_Operational_Discipline_v1_4.md` §20.1 verbatim.
    The §1.2 per-cell-entry-schema Collector-placement field draws from this
    set. v2.9 (FF-2) re-authored from the v2.1 plan-introduced deployment-
    topology taxonomy to this v1.4 §20.1 architectural-class enum.
    """

    IN_PROCESS = "IN_PROCESS"
    """In-process otelcol-contrib collector reached via localhost socket;
    co-resident with the harness process."""

    SELF_HOSTED_BACKEND_COLLECTOR = "SELF_HOSTED_BACKEND_COLLECTOR"
    """The cell-committed self-hosted single-node observability backend's own
    OTLP collector endpoint (Langfuse self-hosted single-node OTLP endpoint,
    Arize Phoenix OSS OTLP endpoint, Helicone HTTP-proxy)."""

    SIDECAR = "SIDECAR"
    """A co-located sidecar-class collector — collector-as-sidecar at non-K8s
    deployments, collector-as-DaemonSet at K8s-resident deployments (DaemonSet
    is a K8s deployment-form of the sidecar architectural class)."""

    VENDOR_PIPELINE = "VENDOR_PIPELINE"
    """A vendor-managed ingestion pipeline reached via vendor SDK or vendor
    agent (Langfuse Cloud SDK, Datadog Agent, Sentry SDK, Arize SaaS SDK,
    collector-as-Lambda)."""

    SIDECAR_WITH_PER_TENANT_ROUTING = "SIDECAR_WITH_PER_TENANT_ROUTING"
    """A sidecar-class collector configured with per-tenant routing —
    per-tenant resource attributes and per-tenant rate limits at the collector
    boundary."""

    PER_TENANT_COLLECTOR_INSTANCE = "PER_TENANT_COLLECTOR_INSTANCE"
    """A distinct collector instance per tenant — full per-tenant
    collector-process isolation."""

    VENDOR_MANAGED_COLLECTOR = "VENDOR_MANAGED_COLLECTOR"
    """A vendor-managed collector at the vendor-managed multi-tenant runtime
    (AWS Bedrock AgentCore, Google Vertex Agent Engine, LangSmith Enterprise
    SDK)."""


# --- §20.2 BatchSpanProcessor universality error arm -----------------------


class EmissionModeViolation(Exception):  # noqa: N818 — name is the U-OD-28 plan signature verbatim
    """Raised when a cell's emission mode deviates from BatchSpanProcessor async.

    The Python materialization of the `Result<(), EmissionModeViolation>` error
    arm in `assert_async_emission_universality` — C-OD-20 §20.2 commits async
    emission universality across all 8 ACTIVE cells. Stack is Pydantic v2 +
    stdlib, no `Result` framework pull (CLAUDE.md §3.2 / I-6).
    """


# --- §20.1 per-cell placement record ---------------------------------------


class PerCellPlacement(BaseModel):
    """The collector placement committed for one ACTIVE matrix cell (§20.1).

    `placement_classes` is a non-empty `frozenset[CollectorPlacement]` —
    cardinality 1 for the six committed cells (1/3/5/6/8) and 2 for the three
    alt-route cells (2/4/7). `emission_mode` is the §20.2 BatchSpanProcessor
    async universality literal; `emission_window` / `emission_batch` inherit
    from the U-OD-27 BatchSpanProcessor constants (see module docstring
    disposition note).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: the §20.1 placement class set — non-empty; |.| in {1, 2}.
    placement_classes: frozenset[CollectorPlacement]
    #: the §20.2 BatchSpanProcessor async universality invariant.
    emission_mode: Literal["BATCH_SPAN_PROCESSOR_ASYNC"]
    #: BatchSpanProcessor flush window (seconds) — = U-OD-27
    #: `BATCH_SPAN_PROCESSOR_WINDOW_SECONDS`.
    emission_window: int
    #: BatchSpanProcessor batch size — = U-OD-27 `BATCH_SPAN_PROCESSOR_BATCH_SIZE`.
    emission_batch: int


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    """Construct a `CellID` — local helper mirroring `per_cell_backend_class`."""
    return CellID(persona_tier=pt, deployment_surface=ds)


_CELL_1 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT)
_CELL_2 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_3 = _cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.MANAGED_CLOUD)
_CELL_4 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT)
_CELL_5 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_6 = _cell(PersonaTier.TEAM_BINDING, DeploymentSurface.MANAGED_CLOUD)
_CELL_7 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER)
_CELL_8 = _cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD)


def _placement(cell: CellID, classes: frozenset[CollectorPlacement]) -> PerCellPlacement:
    """Build a `PerCellPlacement` with the §20.2 uniform async emission window."""
    return PerCellPlacement(
        cell_id=cell,
        placement_classes=classes,
        emission_mode="BATCH_SPAN_PROCESSOR_ASYNC",
        emission_window=BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
        emission_batch=BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    )


#: The per-cell collector placement matrix — exactly 8 entries, one per ACTIVE
#: cell, byte-exact with the v1.4 §20.1 `Cell -> Set<CollectorPlacement>` table.
#: Singleton at cells 1/3/5/6/8; 2-element alt-route disjunction at cells 2/4/7.
PER_CELL_COLLECTOR_PLACEMENT: dict[CellID, PerCellPlacement] = {
    _CELL_1: _placement(_CELL_1, frozenset({CollectorPlacement.IN_PROCESS})),
    _CELL_2: _placement(
        _CELL_2,
        frozenset(
            {
                CollectorPlacement.IN_PROCESS,
                CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
            }
        ),
    ),
    _CELL_3: _placement(_CELL_3, frozenset({CollectorPlacement.VENDOR_PIPELINE})),
    _CELL_4: _placement(
        _CELL_4,
        frozenset(
            {
                CollectorPlacement.IN_PROCESS,
                CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR,
            }
        ),
    ),
    _CELL_5: _placement(_CELL_5, frozenset({CollectorPlacement.SIDECAR})),
    _CELL_6: _placement(_CELL_6, frozenset({CollectorPlacement.VENDOR_PIPELINE})),
    _CELL_7: _placement(
        _CELL_7,
        frozenset(
            {
                CollectorPlacement.SIDECAR_WITH_PER_TENANT_ROUTING,
                CollectorPlacement.PER_TENANT_COLLECTOR_INSTANCE,
            }
        ),
    ),
    _CELL_8: _placement(_CELL_8, frozenset({CollectorPlacement.VENDOR_MANAGED_COLLECTOR})),
}

#: Closure invariant — the matrix covers exactly the 8 ACTIVE cells (acc #2).
assert set(PER_CELL_COLLECTOR_PLACEMENT) == set(ACTIVE_CELLS), (
    "PER_CELL_COLLECTOR_PLACEMENT must cover exactly the 8 ACTIVE cells"
)


def collector_placement(cell_id: CellID) -> frozenset[CollectorPlacement]:
    """Return the collector placement class set for `cell_id` (C-OD-20 §20.1).

    Returns the non-empty `frozenset[CollectorPlacement]` committed for the
    cell (the `Ok` arm). Raises `KeyError` for the structurally EXCLUDED cell —
    no `PerCellPlacement` is declared for `multi-tenant-compliance x
    local-development` (C-OD-01 §1.4); placement is defined only over ACTIVE
    cells.
    """
    return PER_CELL_COLLECTOR_PLACEMENT[cell_id].placement_classes


def assert_async_emission_universality(
    placement: PerCellPlacement,
) -> None:
    """Assert a cell's emission mode is BatchSpanProcessor async (§20.2).

    Returns `None` (the `Ok(())` arm) when `placement.emission_mode` is the
    `BATCH_SPAN_PROCESSOR_ASYNC` universality literal; raises
    `EmissionModeViolation` (the `Err` arm) otherwise. C-OD-20 §20.2 commits
    async emission universality across all 8 ACTIVE cells — span emission MUST
    NOT block the within-turn execution path.
    """
    if placement.emission_mode != "BATCH_SPAN_PROCESSOR_ASYNC":
        raise EmissionModeViolation(
            f"emission-mode universality violated at cell "
            f"{placement.cell_id.persona_tier} x "
            f"{placement.cell_id.deployment_surface}: emission_mode="
            f"{placement.emission_mode!r} deviates from BatchSpanProcessor "
            f"async (C-OD-20 §20.2)"
        )
    return None
