"""Local-first OTLP collector at solo-developer x local-development — U-OD-27.

Implements C-OD-19 §19.1 (in-process collector commitment), §19.2 (sqlite
ring-buffer trace storage), §19.3 (TUI trace browser primitive).

`CollectorTopology` enumerates the 3 collector placements; `bind_in_process_
collector` returns the in-process binding for cell-1 and rejects all other
cells. `RingBufferTraceStoragePolicy` declares the sqlite ring-buffer policy;
`evict_oldest_per_ring_buffer_policy` computes a FIFO-by-age eviction over a
storage-state record. `TuiTraceBrowserSurface` + `TuiQuery` declare the TUI
trace-browser surface; `query_ring_buffer_via_tui` is the ring-buffer query
function.

Authority: Implementation_Plan_Operational_Discipline_v2_6.md §3.7.1 U-OD-27
(v2.6 M-1 revision — in-unit `SpanRow` + `EvictionAction` declarations added;
all v2.1 surfaces preserved verbatim from v2.1 §3.7.1);
Spec_Operational_Discipline_v1_2.md §19 C-OD-19 §19.1 + §19.2 + §19.3
(preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.7.

Phase 1/2 boundary (operator standing directive — `.harness/od_axis_worklist.md`
§"Phase 1 / Phase 2 boundary watch"). U-OD-27 is an observability *primitive*
that borders the Phase-2 runtime/DevEx plane. This module lands the unit's
**library surface only** — records, enums, policy types, pure functions. It
does NOT stand up a running in-process collector, a live TUI, a sqlite
connection, or a daemon. The runtime aspects are marked Class 3 informational
for Phase 2:
  - acc #11: TUI trace-browser *implementation* (terminal toolkit binding, live
    query loop) — spec §19.3 itself defers this to implementation discretion.
  - acc #12: network-egress prohibition is a *runtime* invariant — enforced by
    the cell-1 binding committing `network_hop_required=false` and
    `exporter_class="OTLP_EXPORTER_IN_PROCESS_LOOPBACK"`; the live no-egress
    guarantee is verified at a Phase-2 composition root that wires the actual
    exporter. The library surface declares the invariant; the running process
    enforces it.
  - acc #10: `evict_oldest_per_ring_buffer_policy` is landed as a pure function
    over a storage-state record; the live sqlite ring-buffer rotation is Phase-2.

Acc #8 cross-unit dependency (Class 3 informational). `TuiTraceBrowserSurface.
scoped_queries` enumerates a query per: (a) the 5 operator-burden eval
primitives from U-OD-23 (landed — wired below); (b) the 3 cost-attribution
rollup axes from U-OD-21; (c) the 4 alignment-floor drift-event primitives from
U-OD-25. U-OD-21 is HALTED Class 1
(`.harness/class_1_tension_u_od_21_span_cost_record_missing_rollup_keys.md`) and
U-OD-25 (L8) is not yet landed; neither appears in U-OD-27's declared
`Depends on` cone. The (b) + (c) contributions are recorded as documented
integration-time additions in `SCOPED_QUERY_CATEGORIES` (count + source-unit
citation) and wired at sub-phase 7c / when U-OD-21 + U-OD-25 land. U-OD-27
imports no surface from either halted/unlanded unit.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.observability_matrix import CellID
from harness_od.operator_burden_eval_primitives import OperatorBurdenEvalPrimitive

__all__ = [
    "BATCH_SPAN_PROCESSOR_BATCH_SIZE",
    "BATCH_SPAN_PROCESSOR_WINDOW_SECONDS",
    "CELL_1",
    "SCOPED_QUERY_CATEGORIES",
    "CellBindingError",
    "CollectorTopology",
    "EvictionAction",
    "InProcessCollectorBinding",
    "RingBufferError",
    "RingBufferStorageState",
    "RingBufferTraceStoragePolicy",
    "SpanRow",
    "TuiQuery",
    "TuiTraceBrowserSurface",
    "bind_in_process_collector",
    "evict_oldest_per_ring_buffer_policy",
    "operator_burden_scoped_queries",
    "query_ring_buffer_via_tui",
]


# --- error types (in-unit; landed-unit Result-arm convention) --------------


class CellBindingError(Exception):
    """Raised when `bind_in_process_collector` is called for a non-cell-1 cell.

    The Python materialization of the `Result<_, CellBindingError>` error arm —
    the in-process collector contract is cell-1-exclusive (C-OD-19 §19.1, acc
    #4). Stack is Pydantic v2 + stdlib, no `Result` framework pull.
    """


class RingBufferError(Exception):
    """Raised when a ring-buffer eviction cannot proceed (C-OD-19 §19.2).

    The `Result<EvictionAction, RingBufferError>` error arm of
    `evict_oldest_per_ring_buffer_policy` — inline per OD plan §0.8.
    """


# --- §19.1 in-process collector commitment ---------------------------------

#: The cell-1 singleton — `(SOLO_DEVELOPER, LOCAL_DEVELOPMENT)` (C-OD-19 §19.1
#: / C-OD-01 §1.3 cell-1, acc #1). This unit's contracts apply exclusively at
#: this cell.
CELL_1: CellID = CellID(
    persona_tier=PersonaTier.SOLO_DEVELOPER,
    deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
)


class CollectorTopology(StrEnum):
    """Collector placement (C-OD-19 §19.1) — exactly 3 values (acc #2)."""

    IN_PROCESS_COLLECTOR_NO_NETWORK_HOP = "IN_PROCESS_COLLECTOR_NO_NETWORK_HOP"
    """cell-1 — in-process collector, no network hop."""

    EXTERNAL_OTLP_COLLECTOR = "EXTERNAL_OTLP_COLLECTOR"
    """cells 2-8 baseline — external OTLP collector."""

    EXTERNAL_PER_TENANT_OTLP_COLLECTOR = "EXTERNAL_PER_TENANT_OTLP_COLLECTOR"
    """multi-tenant cells 7,8 — external per-tenant OTLP collector."""


class InProcessCollectorBinding(BaseModel):
    """The cell-1 in-process collector binding (C-OD-19 §19.1).

    `network_hop_required` is `False` (acc #3) — the local-first invariant:
    spans do not leave the harness process. `exporter_class` is the
    in-process loopback exporter (acc #4 — `OTLP_EXPORTER_IN_PROCESS_LOOPBACK`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_id: CellID
    topology: CollectorTopology
    exporter_class: Literal["OTLP_EXPORTER_IN_PROCESS_LOOPBACK"]
    network_hop_required: bool


#: §19.1 verbatim — the `BatchSpanProcessor` default batching window (5 seconds)
#: and batch size (512 spans), whichever fires first (acc #7).
BATCH_SPAN_PROCESSOR_WINDOW_SECONDS: int = 5
BATCH_SPAN_PROCESSOR_BATCH_SIZE: int = 512


def bind_in_process_collector(cell_id: CellID) -> InProcessCollectorBinding:
    """Bind the in-process collector for a cell (C-OD-19 §19.1, acc #3 / #4).

    Returns an `InProcessCollectorBinding` with `topology=
    IN_PROCESS_COLLECTOR_NO_NETWORK_HOP` and `network_hop_required=False` for
    cell-1 (the `Ok` arm). Raises `CellBindingError` (the `Err` arm) for any
    other cell — the in-process collector contract is cell-1-exclusive.
    """
    if cell_id != CELL_1:
        raise CellBindingError(
            f"in-process collector binding rejected: {cell_id.persona_tier} x "
            f"{cell_id.deployment_surface} is not cell-1 "
            f"(SOLO_DEVELOPER x LOCAL_DEVELOPMENT); C-OD-19 §19.1 is "
            f"cell-1-exclusive"
        )
    return InProcessCollectorBinding(
        cell_id=CELL_1,
        topology=CollectorTopology.IN_PROCESS_COLLECTOR_NO_NETWORK_HOP,
        exporter_class="OTLP_EXPORTER_IN_PROCESS_LOOPBACK",
        network_hop_required=False,
    )


# --- §19.2 sqlite ring-buffer trace storage --------------------------------


class SpanRow(BaseModel):
    """One row of the sqlite ring-buffer trace store (C-OD-19 §19.2).

    A span persisted to the C-IS-13 §13.2 sqlite substrate. Declared in-unit
    per OD plan v2.6 §3.7.1 M-1 — single OD consumer (U-OD-27); faithful
    factor-out of the §19.2 ring-buffer trace-storage concept (acc #14).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    trace_id: str
    span_name: str
    start_time_unix_ns: int
    duration_ns: int
    attributes_json: str
    """serialized `SpanAttributes` for sqlite storage."""


class EvictionAction(BaseModel):
    """The outcome of one ring-buffer eviction (C-OD-19 §19.2).

    The §19.2 FIFO-by-age eviction outcome. Declared in-unit per OD plan v2.6
    §3.7.1 M-1 — single OD consumer (U-OD-27); faithful factor-out of the
    §19.2 eviction concept (acc #14).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evicted_span_count: int
    evicted_bytes: int
    eviction_reason: Literal["MAX_AGE_EXCEEDED", "MAX_BYTES_EXCEEDED"]


class RingBufferTraceStoragePolicy(BaseModel):
    """The sqlite ring-buffer trace-storage policy (C-OD-19 §19.2).

    `storage_substrate` composes with C-IS-13 §13.2 sqlite substrate;
    `eviction_policy` composes with C-IS-08 §8.4 ring-buffer FIFO-by-age policy
    (acc #5). `closure_invariant` is the §19.2 fresh-on-restart commitment
    (acc #6). `default_max_age_hours` / `default_max_bytes_mb` are `None` when
    deferred to the deployment-binding default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    storage_substrate: Literal["SQLITE_LOCAL_FS"]
    eviction_policy: Literal["RING_BUFFER_FIFO_BY_AGE"]
    retention_class: Literal["MAX_AGE_OR_MAX_BYTES"]
    default_max_age_hours: int | None
    default_max_bytes_mb: int | None
    closure_invariant: Literal["FRESH_ON_RESTART_OPTIONAL_PERSISTENCE_BETWEEN_RESTARTS"]


class RingBufferStorageState(BaseModel):
    """A snapshot of ring-buffer storage state — eviction-function input.

    The library-surface input to `evict_oldest_per_ring_buffer_policy`. Phase-2
    boundary: the live sqlite ring-buffer rotation is a Phase-2 runtime concern;
    U-OD-27 lands the eviction *policy* as a pure function over this state
    snapshot (acc #10).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: RingBufferTraceStoragePolicy
    #: spans currently in the ring buffer, ordered oldest-first.
    rows: tuple[SpanRow, ...]
    #: per-row byte cost, index-aligned with `rows`.
    row_bytes: tuple[int, ...]
    #: age (hours) of the oldest row.
    oldest_row_age_hours: int
    #: total bytes currently occupied.
    total_bytes_mb: int


def evict_oldest_per_ring_buffer_policy(
    storage_state: RingBufferStorageState,
) -> EvictionAction:
    """Evict the oldest span row(s) per the FIFO-by-age policy (C-OD-19 §19.2).

    Evicts the oldest row when storage approaches `default_max_age_hours` OR
    `default_max_bytes_mb` (whichever fires first); deployment-binding-time
    configurable (acc #10). Returns an `EvictionAction` (the `Ok` arm); raises
    `RingBufferError` (the `Err` arm) when there is nothing to evict but a
    threshold has fired.

    Pure function over a `RingBufferStorageState` snapshot — the live sqlite
    ring-buffer rotation is a Phase-2 runtime concern (see module docstring).
    """
    policy = storage_state.policy
    age_exceeded = (
        policy.default_max_age_hours is not None
        and storage_state.oldest_row_age_hours >= policy.default_max_age_hours
    )
    bytes_exceeded = (
        policy.default_max_bytes_mb is not None
        and storage_state.total_bytes_mb >= policy.default_max_bytes_mb
    )
    if not (age_exceeded or bytes_exceeded):
        return EvictionAction(
            evicted_span_count=0,
            evicted_bytes=0,
            # No threshold fired — no-op eviction; reason records the
            # dominant threshold class (age is the FIFO-by-age primary).
            eviction_reason="MAX_AGE_EXCEEDED",
        )
    if not storage_state.rows:
        raise RingBufferError(
            "ring-buffer eviction threshold fired but the buffer is empty (C-OD-19 §19.2)"
        )
    # FIFO-by-age — evict the single oldest row (`rows` is oldest-first).
    evicted_bytes = storage_state.row_bytes[0] if storage_state.row_bytes else 0
    reason: Literal["MAX_AGE_EXCEEDED", "MAX_BYTES_EXCEEDED"] = (
        "MAX_AGE_EXCEEDED" if age_exceeded else "MAX_BYTES_EXCEEDED"
    )
    return EvictionAction(
        evicted_span_count=1,
        evicted_bytes=evicted_bytes,
        eviction_reason=reason,
    )


# --- §19.3 TUI trace browser primitive -------------------------------------


class TuiQuery(BaseModel):
    """One scoped query of the TUI trace browser (C-OD-19 §19.3).

    `query_form` is `SQL_OVER_RING_BUFFER` — TUI queries run as SQL against the
    sqlite ring-buffer directly (acc #9).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    primitive_or_signal: str
    query_form: Literal["SQL_OVER_RING_BUFFER"]


class TuiTraceBrowserSurface(BaseModel):
    """The TUI trace-browser surface (C-OD-19 §19.3).

    `ring_buffer_query_binding` is `DIRECT_SQLITE_QUERY` — no intermediate query
    engine (acc #9). `operator_self_curation_loop` is the Husain loop per the
    c8-eval-engineer discipline.

    Phase-2 boundary: this record declares the TUI surface *type*; the running
    TUI implementation (terminal toolkit binding, live query loop) is deferred
    per §19.3 and is a Phase-2 concern (acc #11).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scoped_queries: tuple[TuiQuery, ...]
    ring_buffer_query_binding: Literal["DIRECT_SQLITE_QUERY"]
    operator_self_curation_loop: Literal["HUSAIN_LOOP_PER_C8_EVAL_ENGINEER"]


#: Acc #8 scoped-query categories — `(category, query_count, source_unit)`.
#: U-OD-23 (5 operator-burden eval primitives) is landed and wired below. The
#: U-OD-21 (4 cost-attribution rollup axes — PER_DISPATCH_KIND added at OD spec
#: v1.30, B-COST-DISCRIMINATOR-TAXONOMY) and U-OD-25 (4 alignment-floor
#: drift-event primitives) contributions are recorded here as documented
#: integration-time additions — U-OD-25 (L8) is not yet landed; not in U-OD-27's
#: `Depends on` cone. The (b) + (c) query rows are populated at sub-phase 7c /
#: when those units land. See module docstring (Class 3 informational).
SCOPED_QUERY_CATEGORIES: tuple[tuple[str, int, str], ...] = (
    ("operator_burden_eval_primitive", 5, "U-OD-23"),
    ("cost_attribution_rollup_axis", 4, "U-OD-21"),
    ("alignment_floor_drift_event", 4, "U-OD-25"),
)


def operator_burden_scoped_queries() -> tuple[TuiQuery, ...]:
    """The 5 scoped queries for the U-OD-23 operator-burden eval primitives.

    The U-OD-23 contribution to `TuiTraceBrowserSurface.scoped_queries` (acc
    #8 — the only one of the three `SCOPED_QUERY_CATEGORIES` whose source unit
    is landed). The U-OD-21 + U-OD-25 contributions are wired at 7c / when
    those units land (see module docstring — Class 3 informational).
    """
    return tuple(
        TuiQuery(
            primitive_or_signal=primitive.value,
            query_form="SQL_OVER_RING_BUFFER",
        )
        for primitive in OperatorBurdenEvalPrimitive
    )


def query_ring_buffer_via_tui(query: TuiQuery) -> list[SpanRow]:
    """Query the sqlite ring-buffer via the TUI trace browser (C-OD-19 §19.3).

    Returns matching span rows from the sqlite substrate (acc #11). The TUI
    *implementation* (terminal toolkit binding; live query execution against a
    running sqlite connection) is deferred per §19.3 "Deferred to
    implementation discretion" and is a Phase-2 runtime concern.

    Library surface: with no live sqlite ring-buffer bound, this returns an
    empty result — the Phase-2 composition root wires the actual ring-buffer
    query backend. The function signature + return type are the U-OD-27
    contract; the live query is Phase 2.
    """
    del query  # live ring-buffer query backend is wired at a Phase-2 root
    return []
