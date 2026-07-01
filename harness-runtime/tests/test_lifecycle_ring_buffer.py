"""U-RT-30 — ring-buffer + sqlite rotation wiring tests (PARTIAL-LAND).

ACs per Phase 2 Session 3 Track A atomic decomposition §L6 U-RT-30:
  #1 rotation under load tested. LANDED.
     -> test_rotate_evicts_oldest_row_when_bytes_threshold_exceeded
     -> test_rotate_no_op_when_under_threshold
     -> test_rotate_until_within_policy_drains_to_threshold
     -> test_rotation_preserves_fifo_by_age_order
  #2 sqlite path resolves via IS registry. STRUCK — Class 1 routed.
     See `.harness/class_1_tension_u_rt_30_trace_storage_pathclass_gap.md`.
  #3 backpressure observable. LANDED.
     -> test_under_pressure_flag_flips_when_bytes_exceeded
     -> test_snapshot_carries_cumulative_eviction_counters
     -> test_snapshot_under_pressure_matches_predicate

Plus composer plumbing + invariants:
  -> test_materialize_returns_stage_with_ring_buffer
  -> test_policy_carries_sqlite_local_fs_storage_substrate
  -> test_policy_max_bytes_derived_from_collector_config
  -> test_ring_buffer_stage_is_frozen
  -> test_compute_state_oldest_age_hours_from_unix_ns
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_od.local_first_otlp_collector import (
    SpanRow,
)
from harness_runtime.lifecycle.collector_daemon import (
    CollectorDaemonSupervisor,
    materialize_collector_daemon_stage,
)
from harness_runtime.lifecycle.ring_buffer import (
    RingBufferStage,
    RuntimeRingBuffer,
    materialize_ring_buffer_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


_HOUR_NS = 3_600 * 1_000_000_000


def _config(
    tmp_path: Path,
    *,
    sqlite_rotation_max_bytes: int = 100_000_000,
) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(
            sqlite_rotation_max_bytes=sqlite_rotation_max_bytes,
        ),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _daemon(tmp_path: Path) -> CollectorDaemonSupervisor:
    return materialize_collector_daemon_stage(_config(tmp_path)).daemon


def _span_row(span_id: str, *, start_time_unix_ns: int = 0, attrs: str = "{}") -> SpanRow:
    return SpanRow(
        span_id=span_id,
        trace_id="trace-0",
        span_name="test-span",
        start_time_unix_ns=start_time_unix_ns,
        duration_ns=1,
        attributes_json=attrs,
    )


def _seed(daemon: CollectorDaemonSupervisor, rows: list[SpanRow]) -> None:
    """Seed the daemon's buffer directly (test-internal; ingest_span_row is
    async but the daemon's buffer is a plain list we can populate)."""
    daemon._ingested_rows.extend(rows)  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# AC #1 — rotation under load tested.
# ---------------------------------------------------------------------------


def test_rotate_evicts_oldest_row_when_bytes_threshold_exceeded(
    tmp_path: Path,
) -> None:
    """When `total_bytes_mb >= default_max_bytes_mb`, `rotate()` evicts the
    oldest row in place per FIFO-by-age."""
    daemon = _daemon(tmp_path)
    # Construct a ring buffer with a tiny bytes threshold so any row exceeds.
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=1_000_000),  # 1 MB threshold
        daemon,
    ).ring_buffer
    # Pad each row with a 1 MB attributes_json blob → 3 rows ≈ 3 MB total.
    large_attrs = "x" * 1_000_000
    rows = [
        _span_row("oldest", start_time_unix_ns=0, attrs=large_attrs),
        _span_row("middle", start_time_unix_ns=1 * _HOUR_NS, attrs=large_attrs),
        _span_row("newest", start_time_unix_ns=2 * _HOUR_NS, attrs=large_attrs),
    ]
    _seed(daemon, rows)
    action = ring.rotate(now_unix_ns=3 * _HOUR_NS)
    assert action.evicted_span_count == 1
    # Oldest row was popped from the buffer.
    remaining_ids = [r.span_id for r in daemon._ingested_rows]  # pyright: ignore[reportPrivateUsage]
    assert remaining_ids == ["middle", "newest"]


def test_rotate_no_op_when_under_threshold(tmp_path: Path) -> None:
    """When the buffer is within policy thresholds, `rotate()` is a no-op."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=100_000_000),
        daemon,
    ).ring_buffer
    _seed(daemon, [_span_row("a"), _span_row("b")])
    action = ring.rotate(now_unix_ns=_HOUR_NS)
    assert action.evicted_span_count == 0
    assert len(daemon._ingested_rows) == 2  # pyright: ignore[reportPrivateUsage]


def test_rotate_until_within_policy_drains_to_threshold(tmp_path: Path) -> None:
    """`rotate_until_within_policy` drives rotation until the buffer is back
    within policy. Inject N rows that exceed → eviction reduces total bytes
    until under threshold."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=1_000_000),  # 1 MB
        daemon,
    ).ring_buffer
    large_attrs = "x" * 600_000  # each row ≈ 0.6 MB
    rows = [
        _span_row(f"row-{i}", start_time_unix_ns=i * _HOUR_NS, attrs=large_attrs) for i in range(5)
    ]
    _seed(daemon, rows)
    evicted = ring.rotate_until_within_policy(now_unix_ns=5 * _HOUR_NS)
    assert evicted > 0
    # Buffer should now be within policy.
    assert ring.under_pressure(now_unix_ns=5 * _HOUR_NS) is False


def test_rotation_preserves_fifo_by_age_order(tmp_path: Path) -> None:
    """FIFO-by-age: evicting `evict_span_count` rows removes the OLDEST first.
    Remaining rows are in original insertion order (newest at the end)."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=1_000_000),
        daemon,
    ).ring_buffer
    big = "x" * 1_000_000
    _seed(
        daemon,
        [
            _span_row("oldest", start_time_unix_ns=0, attrs=big),
            _span_row("middle", start_time_unix_ns=_HOUR_NS, attrs=big),
            _span_row("newest", start_time_unix_ns=2 * _HOUR_NS, attrs=big),
        ],
    )
    ring.rotate(now_unix_ns=3 * _HOUR_NS)
    ids = [r.span_id for r in daemon._ingested_rows]  # pyright: ignore[reportPrivateUsage]
    # 'oldest' evicted; 'middle' + 'newest' remain in age order.
    assert ids == ["middle", "newest"]


# ---------------------------------------------------------------------------
# AC #3 — backpressure observable.
# ---------------------------------------------------------------------------


def test_under_pressure_flag_flips_when_bytes_exceeded(tmp_path: Path) -> None:
    """`under_pressure()` returns True iff the policy would fire eviction."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=1_000_000),
        daemon,
    ).ring_buffer
    assert ring.under_pressure(now_unix_ns=0) is False
    _seed(daemon, [_span_row("big", attrs="x" * 1_500_000)])
    assert ring.under_pressure(now_unix_ns=0) is True


def test_snapshot_carries_cumulative_eviction_counters(tmp_path: Path) -> None:
    """`snapshot()` exposes `evicted_total_count` + `evicted_total_bytes` as
    cumulative counters across rotation calls (AC #3 backpressure observable)."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=1_000_000),
        daemon,
    ).ring_buffer
    snap0 = ring.snapshot(now_unix_ns=0)
    assert snap0.evicted_total_count == 0
    assert snap0.evicted_total_bytes == 0
    large_attrs = "x" * 1_500_000
    _seed(
        daemon,
        [
            _span_row("a", start_time_unix_ns=0, attrs=large_attrs),
            _span_row("b", start_time_unix_ns=_HOUR_NS, attrs=large_attrs),
        ],
    )
    ring.rotate(now_unix_ns=2 * _HOUR_NS)
    snap1 = ring.snapshot(now_unix_ns=2 * _HOUR_NS)
    assert snap1.evicted_total_count == 1
    assert snap1.evicted_total_bytes > 0


def test_snapshot_under_pressure_matches_predicate(tmp_path: Path) -> None:
    """`snapshot().under_pressure` matches the standalone `under_pressure()` call."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=1_000_000),
        daemon,
    ).ring_buffer
    _seed(daemon, [_span_row("big", attrs="x" * 1_500_000)])
    snap = ring.snapshot(now_unix_ns=0)
    assert snap.under_pressure is ring.under_pressure(now_unix_ns=0)
    assert snap.under_pressure is True


# ---------------------------------------------------------------------------
# Composer plumbing + invariants.
# ---------------------------------------------------------------------------


def test_materialize_returns_stage_with_ring_buffer(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    stage = materialize_ring_buffer_stage(_config(tmp_path), daemon)
    assert isinstance(stage, RingBufferStage)
    assert isinstance(stage.ring_buffer, RuntimeRingBuffer)


def test_policy_carries_sqlite_local_fs_storage_substrate(tmp_path: Path) -> None:
    """OD C-OD-19 §19.2 `storage_substrate` is `SQLITE_LOCAL_FS` verbatim."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    assert ring.policy.storage_substrate == "SQLITE_LOCAL_FS"
    assert ring.policy.eviction_policy == "RING_BUFFER_FIFO_BY_AGE"
    assert ring.policy.retention_class == "MAX_AGE_OR_MAX_BYTES"
    assert ring.policy.closure_invariant == (
        "FRESH_ON_RESTART_OPTIONAL_PERSISTENCE_BETWEEN_RESTARTS"
    )


def test_policy_max_bytes_derived_from_collector_config(tmp_path: Path) -> None:
    """`default_max_bytes_mb` = `CollectorConfig.sqlite_rotation_max_bytes / 1MB`."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(
        _config(tmp_path, sqlite_rotation_max_bytes=50_000_000),
        daemon,
    ).ring_buffer
    assert ring.policy.default_max_bytes_mb == 50  # 50 MB


def test_ring_buffer_stage_is_frozen(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    stage = materialize_ring_buffer_stage(_config(tmp_path), daemon)
    with pytest.raises(FrozenInstanceError):
        stage.ring_buffer = RuntimeRingBuffer(  # type: ignore[misc]
            policy=stage.ring_buffer.policy, daemon=daemon
        )


def test_compute_state_oldest_age_hours_from_unix_ns(tmp_path: Path) -> None:
    """`compute_state(now)` computes `oldest_row_age_hours` from the oldest
    row's `start_time_unix_ns`."""
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    _seed(daemon, [_span_row("old", start_time_unix_ns=0)])
    state = ring.compute_state(now_unix_ns=5 * _HOUR_NS)
    assert state.oldest_row_age_hours == 5


# ---------------------------------------------------------------------------
# U-OD-43 — flush_to_sqlite + SpanRow → SpanInsertRow projection.
# ---------------------------------------------------------------------------


import time

from harness_od.sqlite_span_store import SpanInsertRow, initialize_span_store
from harness_runtime.lifecycle.ring_buffer import _project_span_row


def test_project_span_row_fills_otel_defaults_for_missing_fields() -> None:
    row = _span_row("s1", start_time_unix_ns=100, attrs='{"k":"v"}')
    insert_row = _project_span_row(row)
    assert isinstance(insert_row, SpanInsertRow)
    assert insert_row.span_id == "s1"
    assert insert_row.name == "test-span"
    assert insert_row.start_time_ns == 100
    assert insert_row.end_time_ns == 101  # start + duration_ns=1
    assert insert_row.kind == 0
    assert insert_row.status_code == 0
    assert insert_row.events_json == "[]"
    assert insert_row.attributes_json == '{"k":"v"}'
    assert insert_row.parent_span_id is None
    assert insert_row.status_message is None
    assert insert_row.workflow_id is None
    assert insert_row.workflow_run_id is None
    assert insert_row.workflow_idempotency_key is None


async def test_flush_to_sqlite_empty_buffer_returns_zero(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    conn = initialize_span_store(tmp_path / "spans.db")
    try:
        inserted = await ring.flush_to_sqlite(conn)
    finally:
        conn.close()
    assert inserted == 0


async def test_flush_to_sqlite_writes_buffered_rows(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    _seed(daemon, [_span_row(f"s{i}") for i in range(5)])
    conn = initialize_span_store(tmp_path / "spans.db")
    try:
        # now_ns=0 keeps placeholder-aged rows inside the retention horizon.
        inserted = await ring.flush_to_sqlite(conn, now_ns=0)
        count = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    finally:
        conn.close()
    assert inserted == 5
    assert count == 5


async def test_flush_to_sqlite_is_non_draining(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    _seed(daemon, [_span_row("s1"), _span_row("s2")])
    conn = initialize_span_store(tmp_path / "spans.db")
    try:
        await ring.flush_to_sqlite(conn)
        # Buffer still contains rows; flush does not drain.
        assert len(daemon._ingested_rows) == 2  # pyright: ignore[reportPrivateUsage]
    finally:
        conn.close()


async def test_re_flush_is_no_op_per_spec_27_4_inv_3(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    _seed(daemon, [_span_row(f"s{i}") for i in range(3)])
    conn = initialize_span_store(tmp_path / "spans.db")
    try:
        first = await ring.flush_to_sqlite(conn, now_ns=0)
        second = await ring.flush_to_sqlite(conn, now_ns=0)
        count = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    finally:
        conn.close()
    assert first == 3
    assert second == 0
    assert count == 3


async def test_flush_to_sqlite_applies_retention_cleanup_per_u_od_44(
    tmp_path: Path,
) -> None:
    """100 spans across 14 days + flush with 7-day retention → ~50 remain
    (AC #5: U-OD-44 ledger). Lazy-on-write cleanup fires during flush."""
    from harness_od.sqlite_span_store import SpanInsertRow, insert_spans

    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    conn = initialize_span_store(tmp_path / "spans.db")
    _NS_PER_DAY = 86_400 * 1_000_000_000
    now_ns = 14 * _NS_PER_DAY
    # Pre-load spans directly (not via ring buffer) at varied ages.
    seed_rows = [
        SpanInsertRow(
            span_id=f"s{i}",
            trace_id="t1",
            parent_span_id=None,
            name="seed",
            kind=0,
            start_time_ns=i * _NS_PER_DAY,
            end_time_ns=i * _NS_PER_DAY + 1,
            status_code=0,
            status_message=None,
            attributes_json="{}",
            events_json="[]",
            workflow_id=None,
            workflow_run_id=None,
            workflow_idempotency_key=None,
        )
        for i in range(14)
    ]
    insert_spans(conn, seed_rows)
    try:
        # Flush with empty buffer + retention 7d at now=day-14 → rows with
        # end_time_ns < day-7 (i.e. spans days 0..6 since end=day*ns+1 falls
        # into the strict-less-than-cutoff bucket for i ≤ 6) are deleted.
        await ring.flush_to_sqlite(conn, now_ns=now_ns)
        remaining = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        ids = {r[0] for r in conn.execute("SELECT span_id FROM spans")}
    finally:
        conn.close()
    assert remaining == 7
    assert ids == {f"s{i}" for i in range(7, 14)}


def test_ring_buffer_carries_retention_days_from_config(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(config, daemon).ring_buffer
    assert ring.retention_days == 7  # CollectorConfig default


async def test_flush_to_sqlite_100_span_batch_under_100ms_per_ac_5(
    tmp_path: Path,
) -> None:
    daemon = _daemon(tmp_path)
    ring = materialize_ring_buffer_stage(_config(tmp_path), daemon).ring_buffer
    _seed(daemon, [_span_row(f"s{i}") for i in range(100)])
    conn = initialize_span_store(tmp_path / "spans.db")
    try:
        start_ns = time.perf_counter_ns()
        inserted = await ring.flush_to_sqlite(conn)
        elapsed_ns = time.perf_counter_ns() - start_ns
    finally:
        conn.close()
    assert inserted == 100
    assert elapsed_ns < 100_000_000, f"flush took {elapsed_ns}ns; AC #5 budget 100ms"
