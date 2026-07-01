"""U-RT-29 — in-process OTLP collector daemon supervisor tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L6 U-RT-29:
  #1 daemon starts and answers health.
     -> test_daemon_starts_in_healthy_state
     -> test_daemon_health_before_start_is_stopped
     -> test_double_start_raises
  #2 controlled stop flushes.
     -> test_stop_transitions_to_stopped
     -> test_stop_preserves_ingested_rows_in_snapshot
     -> test_stop_is_idempotent
  #3 crash-restart bounded.
     -> test_single_crash_transitions_to_degraded
     -> test_three_crashes_remain_degraded
     -> test_four_crashes_in_window_transitions_to_failed
     -> test_crashes_outside_window_expire
     -> test_failed_state_recovers_to_healthy_after_window_expires

Plus binding + composer plumbing tests:
  -> test_in_process_placement_cell_1_binds_in_process_collector
  -> test_non_local_dev_in_process_placement_raises_bind_error
  -> test_non_in_process_placement_yields_no_binding
  -> test_materialize_returns_stage_with_daemon_not_started
  -> test_collector_daemon_stage_is_frozen
  -> test_ingest_span_row_appends_to_snapshot

Test discipline: every test that exercises the asyncio task uses a fresh
event loop via pytest-asyncio. Crash-window tests inject a monotonic clock
(`time_source` argument) to make the 60-second sliding window deterministic.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_od.local_first_otlp_collector import (
    CELL_1,
    CollectorTopology,
    InProcessCollectorBinding,
    SpanRow,
)
from harness_od.per_cell_collector_placement_matrix import CollectorPlacement
from harness_runtime.lifecycle.collector_daemon import (
    CollectorDaemonBindError,
    CollectorDaemonHealth,
    CollectorDaemonStage,
    CollectorDaemonSupervisor,
    materialize_collector_daemon_stage,
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


class _FakeClock:
    """Deterministic monotonic clock for crash-window tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _config(
    tmp_path: Path,
    *,
    placement: CollectorPlacement = CollectorPlacement.IN_PROCESS,
    deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(placement=placement),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _span_row(span_id: str = "span-0") -> SpanRow:
    return SpanRow(
        span_id=span_id,
        trace_id="trace-0",
        span_name="test",
        start_time_unix_ns=0,
        duration_ns=1,
        attributes_json="{}",
    )


# ---------------------------------------------------------------------------
# AC #1 — daemon starts and answers health.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_starts_in_healthy_state(tmp_path: Path) -> None:
    """`start()` transitions the daemon to `HEALTHY` per C-RT-07."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    assert await stage.daemon.health() is CollectorDaemonHealth.STOPPED
    await stage.daemon.start()
    assert await stage.daemon.health() is CollectorDaemonHealth.HEALTHY
    await stage.daemon.stop()


@pytest.mark.asyncio
async def test_daemon_health_before_start_is_stopped(tmp_path: Path) -> None:
    """Pre-start health is `STOPPED` — no daemon task running."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    assert await stage.daemon.health() is CollectorDaemonHealth.STOPPED


@pytest.mark.asyncio
async def test_double_start_raises(tmp_path: Path) -> None:
    """C-RT-07 lifecycle invariant: exactly one start per bootstrap."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    await stage.daemon.start()
    with pytest.raises(RuntimeError, match=r"already started"):
        await stage.daemon.start()
    await stage.daemon.stop()


# ---------------------------------------------------------------------------
# AC #2 — controlled stop flushes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_transitions_to_stopped(tmp_path: Path) -> None:
    """`stop()` transitions health to `STOPPED` (post-stop terminal state)."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    await stage.daemon.start()
    await stage.daemon.stop()
    assert await stage.daemon.health() is CollectorDaemonHealth.STOPPED


@pytest.mark.asyncio
async def test_stop_preserves_ingested_rows_in_snapshot(tmp_path: Path) -> None:
    """Buffered rows are preserved across the stop transition — the
    'controlled stop flushes' AC is verified by the snapshot reflecting
    every row ingested before stop()."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    await stage.daemon.start()
    await stage.daemon.ingest_span_row(_span_row("a"))
    await stage.daemon.ingest_span_row(_span_row("b"))
    await stage.daemon.stop()
    snap = stage.daemon.snapshot()
    assert len(snap.ingested_rows) == 2
    assert snap.ingested_rows[0].span_id == "a"
    assert snap.ingested_rows[1].span_id == "b"


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    """A second `stop()` after the daemon is already stopped is a no-op."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    await stage.daemon.start()
    await stage.daemon.stop()
    await stage.daemon.stop()  # no raise
    assert await stage.daemon.health() is CollectorDaemonHealth.STOPPED


# ---------------------------------------------------------------------------
# AC #3 — crash-restart bounded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_crash_transitions_to_degraded(tmp_path: Path) -> None:
    """A single in-window crash transitions HEALTHY → DEGRADED."""
    clock = _FakeClock()
    daemon = CollectorDaemonSupervisor(binding=None, time_source=clock)
    await daemon.start()
    daemon.inject_crash_for_tests()
    assert await daemon.health() is CollectorDaemonHealth.DEGRADED
    await daemon.stop()


@pytest.mark.asyncio
async def test_three_crashes_remain_degraded(tmp_path: Path) -> None:
    """Three crashes in 60s stay at DEGRADED (cap is ≤3 tolerated)."""
    clock = _FakeClock()
    daemon = CollectorDaemonSupervisor(binding=None, time_source=clock)
    await daemon.start()
    for _ in range(3):
        daemon.inject_crash_for_tests()
        clock.advance(5.0)  # 3 crashes within 15s — well within 60s window.
    assert await daemon.health() is CollectorDaemonHealth.DEGRADED
    await daemon.stop()


@pytest.mark.asyncio
async def test_four_crashes_in_window_transitions_to_failed(tmp_path: Path) -> None:
    """A fourth crash within the 60s window flips to FAILED
    (RT-FAIL-HARNESS-DEGRADED ongoing state)."""
    clock = _FakeClock()
    daemon = CollectorDaemonSupervisor(binding=None, time_source=clock)
    await daemon.start()
    for _ in range(4):
        daemon.inject_crash_for_tests()
        clock.advance(5.0)
    assert await daemon.health() is CollectorDaemonHealth.FAILED
    await daemon.stop()


@pytest.mark.asyncio
async def test_crashes_outside_window_expire(tmp_path: Path) -> None:
    """Crashes older than 60s are pruned from the sliding window."""
    clock = _FakeClock()
    daemon = CollectorDaemonSupervisor(binding=None, time_source=clock)
    await daemon.start()
    daemon.inject_crash_for_tests()
    daemon.inject_crash_for_tests()
    assert await daemon.health() is CollectorDaemonHealth.DEGRADED
    # Advance clock past the 60s window; both crashes expire.
    clock.advance(61.0)
    assert await daemon.health() is CollectorDaemonHealth.HEALTHY
    await daemon.stop()


@pytest.mark.asyncio
async def test_failed_state_recovers_to_healthy_after_window_expires(
    tmp_path: Path,
) -> None:
    """After bounded-restart exhaustion (FAILED), the daemon recovers to
    HEALTHY once the 60s window of crashes expires."""
    clock = _FakeClock()
    daemon = CollectorDaemonSupervisor(binding=None, time_source=clock)
    await daemon.start()
    for _ in range(4):
        daemon.inject_crash_for_tests()
        clock.advance(5.0)
    assert await daemon.health() is CollectorDaemonHealth.FAILED
    # Advance past 60s from the LAST crash; all crashes expire.
    clock.advance(61.0)
    assert await daemon.health() is CollectorDaemonHealth.HEALTHY
    await daemon.stop()


# ---------------------------------------------------------------------------
# Binding + composer plumbing.
# ---------------------------------------------------------------------------


def test_in_process_placement_cell_1_binds_in_process_collector(
    tmp_path: Path,
) -> None:
    """`IN_PROCESS` placement at cell-1 binds the OD in-process collector."""
    stage = materialize_collector_daemon_stage(
        _config(tmp_path, placement=CollectorPlacement.IN_PROCESS)
    )
    binding = stage.daemon.binding
    assert isinstance(binding, InProcessCollectorBinding)
    assert binding.cell_id == CELL_1
    assert binding.topology is CollectorTopology.IN_PROCESS_COLLECTOR_NO_NETWORK_HOP
    assert binding.network_hop_required is False


def test_non_local_dev_in_process_placement_raises_bind_error(
    tmp_path: Path,
) -> None:
    """`IN_PROCESS` placement at a non-cell-1 deployment surface raises."""
    with pytest.raises(CollectorDaemonBindError, match=r"is not cell-1"):
        materialize_collector_daemon_stage(
            _config(
                tmp_path,
                placement=CollectorPlacement.IN_PROCESS,
                deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
            )
        )


def test_non_in_process_placement_yields_no_binding(tmp_path: Path) -> None:
    """Non-`IN_PROCESS` placement (e.g. `SIDECAR`) yields a no-op supervisor
    with `binding=None`. The supervisor still exists for uniform lifecycle."""
    stage = materialize_collector_daemon_stage(
        _config(tmp_path, placement=CollectorPlacement.SIDECAR)
    )
    assert stage.daemon.binding is None


def test_materialize_returns_stage_with_daemon_not_started(tmp_path: Path) -> None:
    """The composer constructs the daemon but does NOT start it — the
    bootstrap orchestrator drives start() at stage 4 entry."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    assert isinstance(stage, CollectorDaemonStage)
    assert isinstance(stage.daemon, CollectorDaemonSupervisor)
    # Pre-start health is STOPPED — verifiable without awaiting health() since
    # snapshot() reads the cached state without re-evaluation.
    assert stage.daemon.snapshot().health is CollectorDaemonHealth.STOPPED


def test_collector_daemon_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    with pytest.raises(FrozenInstanceError):
        stage.daemon = CollectorDaemonSupervisor(binding=None)  # type: ignore[misc]


@pytest.mark.asyncio
async def test_ingest_span_row_appends_to_snapshot(tmp_path: Path) -> None:
    """`ingest_span_row` appends to the in-memory buffer; snapshot reflects."""
    stage = materialize_collector_daemon_stage(_config(tmp_path))
    await stage.daemon.start()
    await stage.daemon.ingest_span_row(_span_row("alpha"))
    snap = stage.daemon.snapshot()
    assert len(snap.ingested_rows) == 1
    assert snap.ingested_rows[0].span_id == "alpha"
    await stage.daemon.stop()
