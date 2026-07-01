"""Ring-buffer + sqlite rotation wiring — stage 4 OD (U-RT-30, PARTIAL-LAND).

Per `Spec_Harness_Runtime_v1.md` v1.1 §7 (C-RT-07 collector daemon lifecycle —
F-P2-5 absorption) + OD C-OD-19 §19.2 (sqlite ring-buffer trace storage), and
the Phase 2 Session 3 Track A atomic decomposition §L6 (U-RT-30). The runtime
wires the OD ring-buffer trace-storage policy + eviction discipline against
the U-RT-29 collector-daemon supervisor's in-memory `SpanRow` buffer.

**PARTIAL-LAND posture.** Per the `halt-route-split-AC-pattern` workspace
memory and the Class 1 tension at
`.harness/class_1_tension_u_rt_30_trace_storage_pathclass_gap.md`:

- AC #1 (rotation under load tested) — LANDED here. Verifiable against the
  OD `evict_oldest_per_ring_buffer_policy` pure function + the in-memory
  `SpanRow` buffer from U-RT-29.
- AC #2 (sqlite path resolves via IS registry) — STRUCK. The IS
  `PATH_CLASS_REGISTRY` carries no class for sqlite trace storage
  (IS-AL-1 names the 4 classes — SKILLS / PROMPTS / ROUTING_MANIFEST /
  STATE_LEDGER — as distinct, not aliases). Adding a 5th class is an
  X-AL-3 architectural extension requiring operator ratification. Routed
  to the Class 1 tension record. Re-lands at the follow-on unit.
- AC #3 (backpressure observable) — LANDED here. The ring buffer exposes
  a `RingBufferSnapshot` with `under_pressure` + `evicted_total_count` +
  `evicted_total_bytes` counters; `rotate()` returns the per-call
  `EvictionAction` from the OD policy.

**Sqlite storage at U-RT-30 (deferred).** The OD library's
`RingBufferTraceStoragePolicy.storage_substrate` is `"SQLITE_LOCAL_FS"`,
but the actual sqlite write path is gated on AC #2 (the IS-registry path
resolution). Until the Class 1 record clears, the runtime ring buffer is
**in-memory-only**, mirroring the U-RT-29 supervisor's placeholder buffer.
The OD `RingBufferTraceStoragePolicy.closure_invariant` is
`"FRESH_ON_RESTART_OPTIONAL_PERSISTENCE_BETWEEN_RESTARTS"` — fresh-on-restart
is the spec-committed floor; persistence between restarts is optional. The
in-memory floor satisfies the spec floor.

**Per-row bytes.** The OD `RingBufferStorageState.row_bytes` is the
per-row byte cost. The runtime computes this as the byte length of the
canonical-JSON serialization of each `SpanRow` (Pydantic `model_dump_json`
on the frozen schema, UTF-8 encoded). Deterministic + reproducible across
runs of the same row set.

**Row age computation.** The OD state's `oldest_row_age_hours` is the age
of the oldest row in hours, computed from `now_unix_ns - row.start_time_unix_ns`.
Tests inject a deterministic `now_unix_ns` via the `rotate(now_unix_ns)`
parameter; production passes `time.time_ns()`.

Per-component landing posture:

- `RingBufferBindError` — bootstrap-time bind failure (RT-FAIL-BOOTSTRAP)
  for malformed policy parameters.
- `RuntimeRingBuffer` — mutable wrapper around the U-RT-29 supervisor's
  in-memory buffer; holds the OD policy, computes state snapshots, applies
  rotation via the OD pure eviction function.
- `RingBufferSnapshot` — frozen observability snapshot (current rows +
  policy + under_pressure flag + cumulative eviction counters).
- `RingBufferStage` — frozen materialization stage.
- `materialize_ring_buffer_stage(config, daemon)` — sync composer.

Scope discipline (U-RT-30 boundary held): NO sqlite write path (gated on
Class 1); NO cost-attribution chain (U-RT-31); NO audit-ledger writer
(U-RT-32). This unit lands the in-memory ring-buffer + rotation wiring +
backpressure observability surface only.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Final

from harness_od.local_first_otlp_collector import (
    EvictionAction,
    RingBufferStorageState,
    RingBufferTraceStoragePolicy,
    SpanRow,
    evict_oldest_per_ring_buffer_policy,
)
from harness_od.sqlite_span_store import (
    SpanInsertRow,
    insert_spans,
    retention_cleanup_lazy,
)

from harness_runtime.lifecycle.collector_daemon import CollectorDaemonSupervisor
from harness_runtime.types import RuntimeConfig

__all__ = [
    "RingBufferBindError",
    "RingBufferSnapshot",
    "RingBufferStage",
    "RuntimeRingBuffer",
    "materialize_ring_buffer_stage",
]


#: Nanoseconds per hour — used to convert `start_time_unix_ns` deltas to
#: the OD `RingBufferStorageState.oldest_row_age_hours` field.
_NS_PER_HOUR: Final[int] = 3_600 * 1_000_000_000


def _project_span_row(row: SpanRow) -> SpanInsertRow:
    """Project U-RT-29 placeholder `SpanRow` (6 fields) → `SpanInsertRow` (14).

    Defaults match OTel-canonical UNSPECIFIED/UNSET conventions. Surface
    isolated for U-OD-43 schema-gap discipline; widening at the ingest layer
    (richer `SpanRow` or pre-projection at the OTLP receiver) replaces this
    function without touching `RuntimeRingBuffer.flush_to_sqlite`.
    """
    return SpanInsertRow(
        span_id=row.span_id,
        trace_id=row.trace_id,
        parent_span_id=None,
        name=row.span_name,
        kind=0,
        start_time_ns=row.start_time_unix_ns,
        end_time_ns=row.start_time_unix_ns + row.duration_ns,
        status_code=0,
        status_message=None,
        attributes_json=row.attributes_json,
        events_json="[]",
        workflow_id=None,
        workflow_run_id=None,
        workflow_idempotency_key=None,
    )


#: Bytes per megabyte — used to convert per-row byte counts to the OD
#: `RingBufferStorageState.total_bytes_mb` field.
_BYTES_PER_MB: Final[int] = 1_000_000


class RingBufferBindError(Exception):
    """Bootstrap-time ring-buffer bind failure (RT-FAIL-BOOTSTRAP).

    Raised when the policy parameters from `CollectorConfig` cannot be
    composed into a valid `RingBufferTraceStoragePolicy`. Surfaces at
    `materialize_ring_buffer_stage`, never at runtime."""


@dataclass(frozen=True, slots=True)
class RingBufferSnapshot:
    """Frozen observability snapshot of the ring buffer (AC #3 surface).

    `under_pressure` is True iff the policy would fire eviction at this
    snapshot (oldest-row-age OR total-bytes thresholds exceeded).
    `evicted_total_count` + `evicted_total_bytes` are cumulative counters
    across the buffer's lifetime — the backpressure-observable signal.
    `row_count` + `total_bytes_mb` are the current buffer occupancy.
    """

    policy: RingBufferTraceStoragePolicy
    row_count: int
    total_bytes_mb: int
    oldest_row_age_hours: int
    under_pressure: bool
    evicted_total_count: int
    evicted_total_bytes: int


class RuntimeRingBuffer:
    """Mutable ring-buffer wrapper around the U-RT-29 supervisor's buffer.

    The U-RT-29 supervisor's `ingested_rows` list is the live buffer; this
    class wraps it with the OD `RingBufferTraceStoragePolicy` + drives
    `evict_oldest_per_ring_buffer_policy` for rotation. Mutable by design:
    `rotate()` advances the buffer; the cumulative eviction counters
    accumulate across calls.

    Operates against the supervisor's `_ingested_rows` list directly (via
    a stable reference captured at construction). Eviction removes the
    oldest row from the supervisor's buffer in-place per FIFO-by-age.
    """

    def __init__(
        self,
        *,
        policy: RingBufferTraceStoragePolicy,
        daemon: CollectorDaemonSupervisor,
        retention_days: int = 7,
    ) -> None:
        self._policy: RingBufferTraceStoragePolicy = policy
        self._daemon: CollectorDaemonSupervisor = daemon
        self._retention_days: int = retention_days
        self._evicted_total_count: int = 0
        self._evicted_total_bytes: int = 0

    @property
    def retention_days(self) -> int:
        """Operator-configured retention horizon in days (U-OD-44)."""
        return self._retention_days

    @property
    def policy(self) -> RingBufferTraceStoragePolicy:
        """The bound OD ring-buffer trace-storage policy."""
        return self._policy

    def _rows(self) -> list[SpanRow]:
        """Return the live reference to the supervisor's buffer.

        Internal — the runtime ring buffer mutates this list in place when
        rotation evicts the oldest row. Marked private to surface that
        downstream consumers should read via `snapshot()` rather than the
        live list.
        """
        return self._daemon._ingested_rows  # pyright: ignore[reportPrivateUsage]

    def _row_bytes(self, row: SpanRow) -> int:
        """Per-row byte cost — canonical-JSON serialization length."""
        return len(row.model_dump_json().encode("utf-8"))

    def compute_state(self, now_unix_ns: int) -> RingBufferStorageState:
        """Build the OD `RingBufferStorageState` snapshot.

        Composes the live buffer + per-row bytes + oldest-row age + total
        bytes into the OD state record. Passes through to
        `evict_oldest_per_ring_buffer_policy` at rotation time.
        """
        rows = tuple(self._rows())
        row_bytes = tuple(self._row_bytes(row) for row in rows)
        if rows:
            oldest_age_ns = max(0, now_unix_ns - rows[0].start_time_unix_ns)
            oldest_age_hours = oldest_age_ns // _NS_PER_HOUR
        else:
            oldest_age_hours = 0
        total_bytes = sum(row_bytes)
        total_bytes_mb = total_bytes // _BYTES_PER_MB
        return RingBufferStorageState(
            policy=self._policy,
            rows=rows,
            row_bytes=row_bytes,
            oldest_row_age_hours=int(oldest_age_hours),
            total_bytes_mb=total_bytes_mb,
        )

    def rotate(self, now_unix_ns: int | None = None) -> EvictionAction:
        """Apply one rotation step per the OD eviction policy.

        Calls `evict_oldest_per_ring_buffer_policy(state)` against the
        current buffer state. When the policy fires (age or bytes
        threshold exceeded), evicts the oldest row in place + accumulates
        the eviction counters. Returns the OD `EvictionAction` for the
        caller to observe (per-call eviction count + bytes + reason).

        `now_unix_ns` defaults to `time.time_ns()` for production callers;
        tests pass a deterministic clock value.

        AC #1 surface (rotation under load tested) + AC #3 surface
        (backpressure observable via the per-call EvictionAction +
        cumulative counters on the snapshot).
        """
        clock_ns = now_unix_ns if now_unix_ns is not None else time.time_ns()
        state = self.compute_state(clock_ns)
        action = evict_oldest_per_ring_buffer_policy(state)
        if action.evicted_span_count > 0:
            # FIFO-by-age — pop the oldest row from the supervisor's buffer.
            self._rows().pop(0)
            self._evicted_total_count += action.evicted_span_count
            self._evicted_total_bytes += action.evicted_bytes
        return action

    def rotate_until_within_policy(
        self, now_unix_ns: int | None = None, max_iterations: int = 10_000
    ) -> int:
        """Apply rotation repeatedly until the policy no longer fires.

        Bounded by `max_iterations` to avoid pathological loops. Returns
        the total rows evicted across the call. AC #1 surface — drives
        rotation under load until the buffer is within policy thresholds.
        """
        evicted = 0
        for _ in range(max_iterations):
            action = self.rotate(now_unix_ns)
            if action.evicted_span_count == 0:
                return evicted
            evicted += action.evicted_span_count
        return evicted

    def under_pressure(self, now_unix_ns: int | None = None) -> bool:
        """Return True iff the policy would fire eviction at this state.

        AC #3 surface (backpressure observable as a binary flag). Computes
        the current state + checks the age + bytes thresholds; no rotation
        applied."""
        clock_ns = now_unix_ns if now_unix_ns is not None else time.time_ns()
        state = self.compute_state(clock_ns)
        policy = state.policy
        age_exceeded = (
            policy.default_max_age_hours is not None
            and state.oldest_row_age_hours >= policy.default_max_age_hours
        )
        bytes_exceeded = (
            policy.default_max_bytes_mb is not None
            and state.total_bytes_mb >= policy.default_max_bytes_mb
        )
        return age_exceeded or bytes_exceeded

    async def flush_to_sqlite(self, conn: sqlite3.Connection, *, now_ns: int | None = None) -> int:
        """Flush the current buffer to the sqlite span store via INSERT OR IGNORE
        and apply U-OD-44 lazy-on-write retention cleanup.

        Snapshots the live buffer (non-draining), projects each `SpanRow` to a
        14-column `SpanInsertRow` per OD spec v1.8 §C-OD-27.1, and dispatches
        to `harness_od.sqlite_span_store.insert_spans`. After insert, applies
        `retention_cleanup_lazy(conn, self._retention_days, now_ns)` per spec
        §27.5 row 2 lazy-on-write default. Returns the count of rows actually
        inserted (excludes primary-key-collision skips per spec §27.4
        invariant 3); retention cleanup is observable via row-count delta on
        the spans table, not via this return value.

        Schema gap projection (placeholder `SpanRow` 6 fields → `SpanInsertRow`
        14 fields) fills OTel-canonical defaults at the runtime axis boundary:
        `kind=0` (UNSPECIFIED), `status_code=0` (UNSET), `events_json="[]"`,
        and `parent_span_id` / `status_message` / `workflow_*` to `None`.

        Sqlite calls are blocking; we dispatch to `asyncio.to_thread` to keep
        the runtime event loop free per AC #5 latency target (100-span batch
        flush < 100ms).
        """
        clock_ns = now_ns if now_ns is not None else time.time_ns()
        rows_snapshot = tuple(self._rows())
        insert_rows = tuple(_project_span_row(r) for r in rows_snapshot)
        inserted = await asyncio.to_thread(insert_spans, conn, insert_rows)
        await asyncio.to_thread(retention_cleanup_lazy, conn, self._retention_days, clock_ns)
        return inserted

    def snapshot(self, now_unix_ns: int | None = None) -> RingBufferSnapshot:
        """Return a frozen observability snapshot (AC #3 surface).

        Composes the current state + cumulative eviction counters + the
        under_pressure flag into a single frozen record."""
        clock_ns = now_unix_ns if now_unix_ns is not None else time.time_ns()
        state = self.compute_state(clock_ns)
        return RingBufferSnapshot(
            policy=self._policy,
            row_count=len(state.rows),
            total_bytes_mb=state.total_bytes_mb,
            oldest_row_age_hours=state.oldest_row_age_hours,
            under_pressure=self.under_pressure(clock_ns),
            evicted_total_count=self._evicted_total_count,
            evicted_total_bytes=self._evicted_total_bytes,
        )


@dataclass(frozen=True, slots=True)
class RingBufferStage:
    """Frozen result of stage 4 OD ring-buffer materialization."""

    ring_buffer: RuntimeRingBuffer


def materialize_ring_buffer_stage(
    config: RuntimeConfig,
    daemon: CollectorDaemonSupervisor,
) -> RingBufferStage:
    """Build the stage 4 OD ring-buffer wiring per C-OD-19 §19.2.

    Stage 4 composer. Reads `CollectorConfig.sqlite_rotation_max_rows` +
    `sqlite_rotation_max_bytes` to derive the OD ring-buffer policy's
    `default_max_age_hours` and `default_max_bytes_mb` thresholds:

    - `default_max_bytes_mb = sqlite_rotation_max_bytes / 1_000_000`.
    - `default_max_age_hours = None` (no age threshold default at HEAD;
      operator override via a future `CollectorConfig` extension when
      needed — the spec defers age-threshold defaults to
      deployment-binding-time per OD C-OD-19 §19.2 acc #10).

    The composer does NOT start rotation; the orchestrator (U-RT-43) is
    responsible for invoking `rotate_until_within_policy(...)` on the
    daemon's ingest cadence.

    Parameters
    ----------
    config :
        Frozen `RuntimeConfig`. Drives `CollectorConfig.sqlite_rotation_*`.
    daemon :
        The U-RT-29 supervisor. The ring buffer wraps `daemon._ingested_rows`
        as the live buffer; eviction mutates this list in place.

    Raises
    ------
    RingBufferBindError
        Wrap-and-re-raise for malformed policy parameter composition.
    """
    try:
        max_bytes_mb = config.collector.sqlite_rotation_max_bytes // _BYTES_PER_MB
        policy = RingBufferTraceStoragePolicy(
            storage_substrate="SQLITE_LOCAL_FS",
            eviction_policy="RING_BUFFER_FIFO_BY_AGE",
            retention_class="MAX_AGE_OR_MAX_BYTES",
            default_max_age_hours=None,
            default_max_bytes_mb=max_bytes_mb,
            closure_invariant="FRESH_ON_RESTART_OPTIONAL_PERSISTENCE_BETWEEN_RESTARTS",
        )
    except Exception as exc:
        raise RingBufferBindError(
            f"ring-buffer policy bind failed from CollectorConfig: {exc}"
        ) from exc
    ring_buffer = RuntimeRingBuffer(
        policy=policy,
        daemon=daemon,
        retention_days=config.collector.sqlite_retention_days,
    )
    return RingBufferStage(ring_buffer=ring_buffer)
