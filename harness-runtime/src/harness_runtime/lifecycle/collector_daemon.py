"""In-process OTLP collector daemon supervisor — stage 4 OD (U-RT-29).

Per `Spec_Harness_Runtime_v1.md` v1.1 §7 (C-RT-07 collector daemon lifecycle —
F-P2-5 absorption) and the Phase 2 Session 3 Track A atomic decomposition §L6
(U-RT-29). The runtime owns the *daemon* that runs the OD
`local_first_otlp_collector` library as an in-process supervised component:

- The OD library provides policy + storage primitives:
  `bind_in_process_collector(cell_id)` (cell-1-exclusive binding),
  `RingBufferTraceStoragePolicy` schema,
  `evict_oldest_per_ring_buffer_policy(state)` pure eviction decision.
- The runtime composes a supervisor around the library: start at stage 4,
  health-checked, bounded restart-on-fail, structured stop on shutdown.

**Risk-gate result (no Class 1 fork).** Spec C-RT-07 explicitly names the
supervisor obligations as "the runtime piece" — they are runtime-axis
additions, not OD-spec gaps. The OD library invariants (no-network-egress,
FIFO-by-age eviction, fresh-on-restart) are preserved; the supervisor does
NOT weaken them.

**Implementation mode (spec-deferred).** Per §7 implementation-discretion:
the supervisor may run the daemon as a subprocess, asyncio task, or thread.
This module uses an **asyncio task** because the rest of the runtime is
async-first (C-RT-08 `run()` is async-only). A future unit may swap in a
subprocess-mode supervisor for stronger crash-isolation.

**Cell-1 exclusivity.** `bind_in_process_collector(...)` rejects every cell
except `(SOLO_DEVELOPER, LOCAL_DEVELOPMENT)`. For non-cell-1 deployments the
daemon supervisor lands as a **no-op** (the daemon is started in `STOPPED`
state with no binding; placement is external per `CollectorConfig.placement`).
The supervisor surface still exists at the bootstrap site so consumers can
call `start()` / `stop()` / `health()` uniformly across cells.

**Live OTLP-gRPC receiver (deferred).** The OD `local_first_otlp_collector`
library lands the *policy* (eviction, binding, storage state) as pure
functions; it does NOT land a real OTLP-gRPC server. At HEAD the supervisor
holds an in-memory `SpanRow` buffer and exposes `ingest_span_row(row)` as
the placeholder ingest surface. The real OTLP-gRPC receiver wiring (so the
U-RT-28 BSP → OTLPSpanExporter pipeline actually reaches this daemon) is
deferred to a future Phase-2 runtime sub-unit. The U-RT-29 AC ("daemon
starts and answers health; controlled stop flushes; crash-restart bounded")
is verifiable against the supervisor scaffold without a live gRPC receiver.

Per-component landing posture:

- `CollectorDaemonHealth` — typed `HEALTHY | DEGRADED | FAILED | STOPPED`
  per C-RT-07 health-check contract; `STOPPED` added for the pre-start /
  post-stop terminal state.
- `CollectorDaemonState` — frozen snapshot of supervisor state for tests +
  observability.
- `CollectorDaemonSupervisor` — concrete `CollectorDaemonHandle` Protocol
  implementation. Mutable supervisor; exposes async `start()` / `stop()` /
  `health()` plus `ingest_span_row(row)` and the test-injection
  `inject_crash_for_tests()` method.
- `CollectorDaemonStage` — frozen materialization stage carrying the
  supervisor handle.
- `materialize_collector_daemon_stage(config)` — sync composer; returns the
  stage with a daemon NOT YET STARTED. Bootstrap orchestrator (U-RT-43)
  calls `await stage.daemon.start()` at the stage 4 entry; shutdown calls
  `await stage.daemon.stop()`.

Restart-bound discipline (C-RT-07): ≤3 crashes in a 60-second sliding window
is tolerated as `DEGRADED`; >3 in 60s flips to `FAILED` (harness-degraded
ongoing state per C-RT-07 failure-mode taxonomy `RT-FAIL-HARNESS-DEGRADED`).
The supervisor records crash timestamps via the `time_source` callable —
`time.monotonic` by default; tests inject deterministic clocks.

Scope discipline (U-RT-29 boundary held): NO ring-buffer + sqlite rotation
wiring (U-RT-30 wires `evict_oldest_per_ring_buffer_policy` to live storage),
NO cost-attribution chain (U-RT-31), NO audit-ledger writer (U-RT-32), NO
live OTLP-gRPC server (deferred). This unit lands the asyncio-task-based
supervisor + health + bounded restart only.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from harness_core.persona_tier import PersonaTier
from harness_od.local_first_otlp_collector import (
    CELL_1,
    CellBindingError,
    InProcessCollectorBinding,
    SpanRow,
    bind_in_process_collector,
)
from harness_od.observability_matrix import CellID
from harness_od.per_cell_collector_placement_matrix import CollectorPlacement

from harness_runtime.types import RuntimeConfig

__all__ = [
    "CollectorDaemonBindError",
    "CollectorDaemonHealth",
    "CollectorDaemonStage",
    "CollectorDaemonState",
    "CollectorDaemonSupervisor",
    "materialize_collector_daemon_stage",
]


#: C-RT-07 bounded-restart window in seconds.
_RESTART_WINDOW_SECONDS: Final[float] = 60.0

#: C-RT-07 bounded-restart cap (≤3 in `_RESTART_WINDOW_SECONDS`).
_RESTART_CAP: Final[int] = 3

#: C-RT-07 health-check cadence default (10s; operator-tunable in a future unit).
_DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS: Final[float] = 10.0


class CollectorDaemonHealth(StrEnum):
    """Typed daemon health-check states (C-RT-07).

    Spec §7: "Expose a health check (typed: `healthy | degraded | failed`)".
    `STOPPED` is a runtime-internal state for the pre-start + post-stop
    terminal phase — the supervisor exists but no daemon task is running.
    Distinct from `FAILED` (which is the bounded-restart-exhausted ongoing
    degradation state per C-RT-07 `RT-FAIL-HARNESS-DEGRADED`).
    """

    HEALTHY = "healthy"
    """Daemon task is running; binding is present (cell-1) or skipped
    (non-cell-1 no-op); no recent crash-restart events."""

    DEGRADED = "degraded"
    """Daemon experienced crash-restart events ≤ `_RESTART_CAP` within
    `_RESTART_WINDOW_SECONDS`; still running and serving."""

    FAILED = "failed"
    """Bounded restart-budget exhausted (> `_RESTART_CAP` crashes within
    `_RESTART_WINDOW_SECONDS`); ongoing harness-degraded state per C-RT-07."""

    STOPPED = "stopped"
    """Pre-start or post-stop terminal state — no daemon task running."""


class CollectorDaemonBindError(Exception):
    """Bootstrap-time collector daemon bind failure (RT-FAIL-BOOTSTRAP).

    Wraps the OD-canonical `CellBindingError` when the supervisor expected
    cell-1 placement (`CollectorPlacement.IN_PROCESS`) but the configured
    `(deployment_surface, persona_tier)` did not land at `CELL_1`. Surfaces
    at `materialize_collector_daemon_stage`, never at runtime.
    """


@dataclass(frozen=True, slots=True)
class CollectorDaemonState:
    """Frozen snapshot of supervisor state for tests + observability.

    `binding` is `None` when the daemon supervisor is a no-op (non-cell-1
    placements). `ingested_rows` is a tuple snapshot of the in-memory
    buffer — the U-RT-30 wiring will replace this with live sqlite-backed
    storage. `crash_timestamps` is the sliding-window history of crash
    events used for `_RESTART_CAP`-bound enforcement.
    """

    health: CollectorDaemonHealth
    binding: InProcessCollectorBinding | None
    ingested_rows: tuple[SpanRow, ...]
    crash_timestamps: tuple[float, ...]


class CollectorDaemonSupervisor:
    """Asyncio-task-based in-process collector daemon supervisor (U-RT-29).

    Concrete `CollectorDaemonHandle` Protocol implementation. Holds the
    OD cell binding (when cell-1) + an in-memory `SpanRow` buffer +
    crash-window tracking + the running asyncio task handle. Async methods
    (`start` / `stop` / `health` / `ingest_span_row`) compose under the
    runtime's async-first discipline (C-RT-08).

    Mutable by design: `start()` flips state from `STOPPED → HEALTHY`;
    `stop()` flips back to `STOPPED`; crashes increment the crash window
    and transition `HEALTHY → DEGRADED → FAILED`. The C-RT-07 invariant
    "daemon lifecycle is strictly contained within harness process
    lifecycle" is enforced by tying daemon start/stop to the bootstrap
    orchestrator's stage 4 entry + C-RT-10 shutdown.
    """

    def __init__(
        self,
        *,
        binding: InProcessCollectorBinding | None,
        time_source: Callable[[], float] = time.monotonic,
        health_check_interval_seconds: float = _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._binding: InProcessCollectorBinding | None = binding
        self._time_source: Callable[[], float] = time_source
        self._health_check_interval_seconds: float = health_check_interval_seconds
        self._health: CollectorDaemonHealth = CollectorDaemonHealth.STOPPED
        self._ingested_rows: list[SpanRow] = []
        self._crash_timestamps: deque[float] = deque()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def binding(self) -> InProcessCollectorBinding | None:
        """The cell-1 binding (None for non-cell-1 no-op supervisors)."""
        return self._binding

    async def start(self) -> None:
        """Start the daemon (idempotent — re-start raises).

        Spec §7: "Start the daemon at stage 4, after TracerProvider
        registration so that spans flow through BSP → OTLP exporter →
        daemon → ring-buffer + sqlite." Spawns the asyncio task carrying
        the daemon's health-check loop; flips health to HEALTHY.
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError(
                "collector daemon already started; C-RT-07 lifecycle "
                "invariant — exactly one start per bootstrap"
            )
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())
        self._health = CollectorDaemonHealth.HEALTHY

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        """Structured stop — flush buffers, terminate task, await termination.

        Spec §7 obligation: "On structured stop (during C-RT-10 shutdown):
        flush daemon buffers to sqlite, close sqlite cleanly, terminate
        daemon process/thread, await termination with timeout." At HEAD the
        flush is a no-op (in-memory buffer is implicitly preserved across
        the stop transition for inspection by the test suite); the U-RT-30
        ring-buffer + sqlite wiring will replace this with a live drain.
        """
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                # Bounded timeout: cancel + swallow per C-RT-07 "await
                # termination with timeout" obligation.
                self._task.cancel()
            self._task = None
        self._stop_event = None
        self._health = CollectorDaemonHealth.STOPPED

    async def health(self) -> CollectorDaemonHealth:
        """Return the current typed health state (C-RT-07).

        Re-evaluates the crash-window cap before returning: any restart
        events outside the `_RESTART_WINDOW_SECONDS` sliding window are
        expired; the health state is then derived from the remaining
        in-window count vs `_RESTART_CAP`.
        """
        self._prune_crash_window()
        # When STOPPED, never auto-flip away from STOPPED on health() —
        # only start() / stop() drive the STOPPED ↔ running transitions.
        if self._task is None or self._task.done():
            return CollectorDaemonHealth.STOPPED
        in_window = len(self._crash_timestamps)
        if in_window > _RESTART_CAP:
            self._health = CollectorDaemonHealth.FAILED
        elif in_window > 0:
            self._health = CollectorDaemonHealth.DEGRADED
        else:
            self._health = CollectorDaemonHealth.HEALTHY
        return self._health

    async def ingest_span_row(self, row: SpanRow) -> None:
        """Append a span row to the in-memory buffer (placeholder).

        The live ingest path (OTLP-gRPC receiver → ring-buffer → sqlite) is
        deferred to a future Phase-2 runtime sub-unit; at HEAD this method
        is the placeholder buffer write so the U-RT-29 supervisor surface is
        end-to-end testable (start → ingest → stop → assert buffered).
        """
        self._ingested_rows.append(row)

    def inject_crash_for_tests(self) -> None:
        """Test-only crash injection. Records a crash timestamp + transitions
        health per the C-RT-07 bounded-restart rule.

        NOT exported in `__all__`; tests invoke this to simulate a
        crash-restart event. Production crash detection is wired by the
        daemon's asyncio task itself (future Phase-2 sub-unit; the actual
        receiver implementation will catch + record crashes).
        """
        self._crash_timestamps.append(self._time_source())
        self._prune_crash_window()

    def snapshot(self) -> CollectorDaemonState:
        """Return a frozen snapshot for tests + observability."""
        return CollectorDaemonState(
            health=self._health,
            binding=self._binding,
            ingested_rows=tuple(self._ingested_rows),
            crash_timestamps=tuple(self._crash_timestamps),
        )

    async def _run_loop(self) -> None:
        """The daemon's asyncio task body — health-check cadence + stop.

        Sleeps `_health_check_interval_seconds` between iterations; exits
        cleanly on `_stop_event`. The actual receiver work (OTLP-gRPC
        ingest, ring-buffer rotation, sqlite eviction) is deferred to a
        future Phase-2 sub-unit; this loop is the supervisor scaffold.
        """
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._health_check_interval_seconds,
                )
            except TimeoutError:
                # Health-check cadence tick; no work to do at HEAD.
                continue

    def _prune_crash_window(self) -> None:
        """Drop crash timestamps older than `_RESTART_WINDOW_SECONDS`."""
        now = self._time_source()
        cutoff = now - _RESTART_WINDOW_SECONDS
        while self._crash_timestamps and self._crash_timestamps[0] < cutoff:
            self._crash_timestamps.popleft()


@dataclass(frozen=True, slots=True)
class CollectorDaemonStage:
    """Frozen result of stage 4 OD collector-daemon materialization.

    The bootstrap orchestrator (U-RT-43) calls `await stage.daemon.start()`
    at the stage 4 entry; the C-RT-10 shutdown calls
    `await stage.daemon.stop()`. The stage itself is frozen — the daemon's
    mutable state lives behind the `CollectorDaemonSupervisor` handle.
    """

    daemon: CollectorDaemonSupervisor


def materialize_collector_daemon_stage(
    config: RuntimeConfig,
    *,
    time_source: Callable[[], float] = time.monotonic,
    health_check_interval_seconds: float = _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
) -> CollectorDaemonStage:
    """Build the stage 4 OD `CollectorDaemonStage` per C-RT-07.

    Stage 4 composer. The daemon is NOT started here — the bootstrap
    orchestrator calls `await stage.daemon.start()` after the U-RT-28 BSP
    + exporter wiring lands (so the ingest path is ready when the daemon
    comes up).

    Cell-1 binding:
    - When `config.deployment_surface == LOCAL_DEVELOPMENT` AND
      `config.collector.placement == IN_PROCESS`, the composer calls
      `bind_in_process_collector(CELL_1)` and the daemon supervises the
      in-process collector.
    - When `config.collector.placement != IN_PROCESS`, the daemon
      supervisor is a no-op: it starts/stops/answers health cleanly but
      holds no binding (the actual collector lives elsewhere per the §20.1
      placement matrix).

    `time_source` + `health_check_interval_seconds` are testable injection
    points; production threads `time.monotonic` and the default 10s interval
    per C-RT-07.

    Raises
    ------
    CollectorDaemonBindError
        When the configured `(deployment_surface, persona_tier)` lands at
        a non-cell-1 cell but `placement == IN_PROCESS` was selected.
    """
    binding: InProcessCollectorBinding | None = None
    if config.collector.placement is CollectorPlacement.IN_PROCESS:
        # In-process placement requires cell-1 per OD `bind_in_process_collector`.
        # The runtime defaults persona_tier to SOLO_DEVELOPER at this stage —
        # the persona-tier surface on RuntimeConfig is a future-unit extension
        # (the operator-facing run() API resolves it; here we bind cell-1
        # against LOCAL_DEVELOPMENT and let bind_in_process_collector raise
        # for non-LOCAL_DEVELOPMENT surfaces).
        cell_id = CellID(
            persona_tier=PersonaTier.SOLO_DEVELOPER,
            deployment_surface=config.deployment_surface,
        )
        try:
            binding = bind_in_process_collector(cell_id)
        except CellBindingError as exc:
            raise CollectorDaemonBindError(
                f"in-process collector daemon binding failed: "
                f"deployment_surface={config.deployment_surface.value} is not "
                f"cell-1 ({CELL_1.persona_tier.value} x "
                f"{CELL_1.deployment_surface.value}); CollectorConfig.placement "
                f"is IN_PROCESS but the cell does not support it. "
                f"OD library said: {exc}"
            ) from exc

    daemon = CollectorDaemonSupervisor(
        binding=binding,
        time_source=time_source,
        health_check_interval_seconds=health_check_interval_seconds,
    )
    return CollectorDaemonStage(daemon=daemon)
