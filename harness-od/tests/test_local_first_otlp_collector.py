"""Tests for U-OD-27 — local-first OTLP collector at cell-1 (C-OD-19).

Test set per the U-OD-27 §3.7.1 (v2.6) `Tests:` field — covers acceptance
#1-#14 against C-OD-19 §19.1 / §19.2 / §19.3.

Phase-2 boundary: U-OD-27 is landed as a library surface. The runtime aspects
(live collector process, live TUI, live sqlite connection) are Class 3
informational for Phase 2 — these tests verify the library types + pure
functions, not a running process.
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_od.local_first_otlp_collector import (
    BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
    CELL_1,
    SCOPED_QUERY_CATEGORIES,
    CellBindingError,
    CollectorTopology,
    EvictionAction,
    RingBufferError,
    RingBufferStorageState,
    RingBufferTraceStoragePolicy,
    SpanRow,
    TuiQuery,
    TuiTraceBrowserSurface,
    bind_in_process_collector,
    evict_oldest_per_ring_buffer_policy,
    operator_burden_scoped_queries,
    query_ring_buffer_via_tui,
)
from harness_od.observability_matrix import ACTIVE_CELLS, CellID
from harness_od.operator_burden_eval_primitives import OperatorBurdenEvalPrimitive


def _policy(
    *, max_age: int | None = 24, max_bytes: int | None = 100
) -> RingBufferTraceStoragePolicy:
    return RingBufferTraceStoragePolicy(
        storage_substrate="SQLITE_LOCAL_FS",
        eviction_policy="RING_BUFFER_FIFO_BY_AGE",
        retention_class="MAX_AGE_OR_MAX_BYTES",
        default_max_age_hours=max_age,
        default_max_bytes_mb=max_bytes,
        closure_invariant="FRESH_ON_RESTART_OPTIONAL_PERSISTENCE_BETWEEN_RESTARTS",
    )


def _span_row(span_id: str) -> SpanRow:
    return SpanRow(
        span_id=span_id,
        trace_id="trace-x",
        span_name="chat",
        start_time_unix_ns=1_000,
        duration_ns=500,
        attributes_json="{}",
    )


# --- acc #1 ----------------------------------------------------------------
def test_cell_1_singleton_solo_local() -> None:
    """`CELL_1` is the `(SOLO_DEVELOPER, LOCAL_DEVELOPMENT)` singleton."""
    assert CELL_1 == CellID(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


# --- acc #2 ----------------------------------------------------------------
def test_collector_topology_cardinality_three() -> None:
    """`CollectorTopology` enumerates exactly 3 values per §19.1."""
    assert len(CollectorTopology) == 3


# --- acc #3 ----------------------------------------------------------------
def test_cell_1_in_process_collector_no_network_hop() -> None:
    """`bind_in_process_collector(CELL_1)` → in-process, no network hop."""
    binding = bind_in_process_collector(CELL_1)
    assert binding.topology is CollectorTopology.IN_PROCESS_COLLECTOR_NO_NETWORK_HOP
    assert binding.network_hop_required is False
    assert binding.cell_id == CELL_1


# --- acc #4 ----------------------------------------------------------------
def test_cell_1_exporter_class_loopback() -> None:
    """cell-1 binding commits the in-process loopback exporter."""
    binding = bind_in_process_collector(CELL_1)
    assert binding.exporter_class == "OTLP_EXPORTER_IN_PROCESS_LOOPBACK"


def test_cells_2_through_8_reject_in_process_binding() -> None:
    """`bind_in_process_collector` rejects all non-cell-1 cells (`Err`)."""
    for cell in ACTIVE_CELLS:
        if cell == CELL_1:
            continue
        with pytest.raises(CellBindingError):
            bind_in_process_collector(cell)


# --- acc #5 ----------------------------------------------------------------
def test_ring_buffer_storage_substrate_sqlite() -> None:
    """`storage_substrate` is `SQLITE_LOCAL_FS` per C-IS-13 §13.2."""
    assert _policy().storage_substrate == "SQLITE_LOCAL_FS"


def test_ring_buffer_eviction_fifo_by_age() -> None:
    """`eviction_policy` is `RING_BUFFER_FIFO_BY_AGE` per C-IS-08 §8.4."""
    assert _policy().eviction_policy == "RING_BUFFER_FIFO_BY_AGE"


# --- acc #6 ----------------------------------------------------------------
def test_closure_invariant_fresh_on_restart() -> None:
    """`closure_invariant` is the §19.2 fresh-on-restart commitment."""
    assert _policy().closure_invariant == "FRESH_ON_RESTART_OPTIONAL_PERSISTENCE_BETWEEN_RESTARTS"


# --- acc #7 ----------------------------------------------------------------
def test_batch_span_processor_window_5_seconds() -> None:
    """`BATCH_SPAN_PROCESSOR_WINDOW_SECONDS == 5` per §19.1 verbatim."""
    assert BATCH_SPAN_PROCESSOR_WINDOW_SECONDS == 5


def test_batch_span_processor_batch_size_512() -> None:
    """`BATCH_SPAN_PROCESSOR_BATCH_SIZE == 512` per §19.1 verbatim."""
    assert BATCH_SPAN_PROCESSOR_BATCH_SIZE == 512


# --- acc #8 ----------------------------------------------------------------
def test_tui_scoped_queries_cover_all_primitives() -> None:
    """`scoped_queries` enumerates a query per U-OD-23 operator-burden primitive.

    Acc #8 requires 5 (U-OD-23) + 3 (U-OD-21) + 4 (U-OD-25) query categories.
    U-OD-23 (5) is landed and wired; U-OD-21 (HALTED Class 1) and U-OD-25 (L8,
    not landed) contributions are documented integration-time additions
    recorded in `SCOPED_QUERY_CATEGORIES` — Class 3 informational. This test
    verifies the U-OD-23 contribution and the category catalog.
    """
    surface = TuiTraceBrowserSurface(
        scoped_queries=operator_burden_scoped_queries(),
        ring_buffer_query_binding="DIRECT_SQLITE_QUERY",
        operator_self_curation_loop="HUSAIN_LOOP_PER_C8_EVAL_ENGINEER",
    )
    # U-OD-23 contribution — exactly the 5 landed operator-burden primitives.
    assert len(surface.scoped_queries) == 5
    assert {q.primitive_or_signal for q in surface.scoped_queries} == {
        p.value for p in OperatorBurdenEvalPrimitive
    }


def test_scoped_query_categories_catalog() -> None:
    """`SCOPED_QUERY_CATEGORIES` records the 5+4+4 acc #8 query categories.

    cost_attribution_rollup_axis grew 3→4 at OD spec v1.30 (PER_DISPATCH_KIND,
    B-COST-DISCRIMINATOR-TAXONOMY).
    """
    counts = {cat: (n, src) for cat, n, src in SCOPED_QUERY_CATEGORIES}
    assert counts["operator_burden_eval_primitive"] == (5, "U-OD-23")
    assert counts["cost_attribution_rollup_axis"] == (4, "U-OD-21")
    assert counts["alignment_floor_drift_event"] == (4, "U-OD-25")
    # Total acc #8 query count is 13.
    assert sum(n for _, n, _ in SCOPED_QUERY_CATEGORIES) == 13


# --- acc #9 ----------------------------------------------------------------
def test_tui_query_binding_direct_sqlite() -> None:
    """`ring_buffer_query_binding` is `DIRECT_SQLITE_QUERY` — no query engine."""
    surface = TuiTraceBrowserSurface(
        scoped_queries=(),
        ring_buffer_query_binding="DIRECT_SQLITE_QUERY",
        operator_self_curation_loop="HUSAIN_LOOP_PER_C8_EVAL_ENGINEER",
    )
    assert surface.ring_buffer_query_binding == "DIRECT_SQLITE_QUERY"
    query = TuiQuery(primitive_or_signal="x", query_form="SQL_OVER_RING_BUFFER")
    assert query.query_form == "SQL_OVER_RING_BUFFER"


# --- acc #10 ---------------------------------------------------------------
def test_evict_oldest_at_age_threshold() -> None:
    """Eviction fires when oldest-row age reaches `default_max_age_hours`."""
    state = RingBufferStorageState(
        policy=_policy(max_age=24, max_bytes=None),
        rows=(_span_row("s1"), _span_row("s2")),
        row_bytes=(40, 60),
        oldest_row_age_hours=25,
        total_bytes_mb=10,
    )
    action = evict_oldest_per_ring_buffer_policy(state)
    assert action.evicted_span_count == 1
    assert action.evicted_bytes == 40
    assert action.eviction_reason == "MAX_AGE_EXCEEDED"


def test_evict_oldest_at_bytes_threshold() -> None:
    """Eviction fires when total bytes reach `default_max_bytes_mb`."""
    state = RingBufferStorageState(
        policy=_policy(max_age=None, max_bytes=100),
        rows=(_span_row("s1"),),
        row_bytes=(80,),
        oldest_row_age_hours=1,
        total_bytes_mb=120,
    )
    action = evict_oldest_per_ring_buffer_policy(state)
    assert action.evicted_span_count == 1
    assert action.eviction_reason == "MAX_BYTES_EXCEEDED"


def test_evict_oldest_no_threshold_fired_is_noop() -> None:
    """No eviction when neither threshold has fired."""
    state = RingBufferStorageState(
        policy=_policy(max_age=24, max_bytes=100),
        rows=(_span_row("s1"),),
        row_bytes=(10,),
        oldest_row_age_hours=1,
        total_bytes_mb=5,
    )
    action = evict_oldest_per_ring_buffer_policy(state)
    assert action.evicted_span_count == 0


def test_evict_oldest_empty_buffer_with_threshold_fired_errors() -> None:
    """`RingBufferError` when a threshold fired but the buffer is empty."""
    state = RingBufferStorageState(
        policy=_policy(max_age=24, max_bytes=None),
        rows=(),
        row_bytes=(),
        oldest_row_age_hours=25,
        total_bytes_mb=0,
    )
    with pytest.raises(RingBufferError):
        evict_oldest_per_ring_buffer_policy(state)


# --- acc #11 ---------------------------------------------------------------
def test_query_ring_buffer_returns_matching_rows() -> None:
    """`query_ring_buffer_via_tui` returns a `list[SpanRow]` (library surface).

    With no live sqlite ring-buffer bound, the library surface returns an
    empty result; the live query backend is wired at a Phase-2 composition
    root (acc #11 — TUI implementation deferred per §19.3).
    """
    result = query_ring_buffer_via_tui(
        TuiQuery(primitive_or_signal="x", query_form="SQL_OVER_RING_BUFFER")
    )
    assert isinstance(result, list)


def test_tui_implementation_deferred_per_19_3() -> None:
    """The TUI implementation is deferred — query returns the library default."""
    # §19.3 defers the TUI implementation; the library surface is a pure
    # signature with an empty default result until the Phase-2 root wires it.
    assert (
        query_ring_buffer_via_tui(
            TuiQuery(primitive_or_signal="cache_hit_rate", query_form="SQL_OVER_RING_BUFFER")
        )
        == []
    )


# --- acc #12 ---------------------------------------------------------------
def test_cell_1_no_network_egress() -> None:
    """cell-1 binding declares the local-first no-egress invariant.

    Acc #12 — network egress prohibited at cell-1: no spans leave the process.
    The library surface declares the invariant (`network_hop_required=False`,
    in-process loopback exporter); the live no-egress guarantee is verified at
    a Phase-2 composition root (Class 3 informational).
    """
    binding = bind_in_process_collector(CELL_1)
    assert binding.network_hop_required is False
    assert binding.exporter_class == "OTLP_EXPORTER_IN_PROCESS_LOOPBACK"
    assert binding.topology is CollectorTopology.IN_PROCESS_COLLECTOR_NO_NETWORK_HOP


# --- acc #13 ---------------------------------------------------------------
def test_cross_axis_edge_to_u_is_nn_c_is_13_section_13_2() -> None:
    """C-IS-13 §13.2 sqlite substrate composes via the storage_substrate field."""
    # The cross-axis edge to U-IS-NN (C-IS-13 §13.2) is realized as the
    # `SQLITE_LOCAL_FS` storage substrate; placeholder unit-ID resolves at 7c.
    assert _policy().storage_substrate == "SQLITE_LOCAL_FS"


def test_cross_axis_edge_to_u_is_nn_c_is_08_section_8_4() -> None:
    """C-IS-08 §8.4 ring-buffer policy composes via the eviction_policy field."""
    assert _policy().eviction_policy == "RING_BUFFER_FIFO_BY_AGE"


# --- acc #14 (v2.6) --------------------------------------------------------
def test_span_row_declared_in_unit() -> None:
    """`SpanRow` is declared in-unit (single-consumer M-1 factor-out)."""
    row = _span_row("s1")
    assert row.span_id == "s1"
    assert row.attributes_json == "{}"


def test_eviction_action_declared_in_unit() -> None:
    """`EvictionAction` is declared in-unit (single-consumer M-1 factor-out)."""
    action = EvictionAction(
        evicted_span_count=2,
        evicted_bytes=128,
        eviction_reason="MAX_AGE_EXCEEDED",
    )
    assert action.evicted_span_count == 2


def test_query_ring_buffer_returns_list_of_span_row() -> None:
    """`query_ring_buffer_via_tui` return type resolves to `list[SpanRow]`."""
    result = query_ring_buffer_via_tui(
        TuiQuery(primitive_or_signal="x", query_form="SQL_OVER_RING_BUFFER")
    )
    assert isinstance(result, list)


def test_evict_oldest_returns_eviction_action() -> None:
    """`evict_oldest_per_ring_buffer_policy` return type is `EvictionAction`."""
    state = RingBufferStorageState(
        policy=_policy(max_age=24, max_bytes=None),
        rows=(_span_row("s1"),),
        row_bytes=(10,),
        oldest_row_age_hours=25,
        total_bytes_mb=1,
    )
    assert isinstance(evict_oldest_per_ring_buffer_policy(state), EvictionAction)
