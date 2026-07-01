"""C-OD-27 sqlite span-store schema + WAL-mode initialization.

U-OD-42 — `4-OD-B SqliteWritePath` cluster L0 anchor. Lands the canonical
14-column `spans` table per OD spec v1.8 §C-OD-27.1, 4 indexes
(`idx_workflow` composite / `idx_idempotency` / `idx_trace` /
`idx_time_range` composite), and WAL-mode + foreign-keys-off pragmas per
§27.2 row 2. Idempotent re-initialization via `CREATE TABLE IF NOT EXISTS`
+ `CREATE INDEX IF NOT EXISTS` (§27.4 invariant via declarative DDL).

Subsequent units in the cluster (U-OD-43 batched flush, U-OD-44 lazy-on-write
retention, U-OD-45 typed read interface) extend this module without altering
the schema.

Closes `H_T-OD-6 PARTIAL` sqlite-write-path gap per workspace CLAUDE.md §4.1
OD substitution row. Plan AC #1 cites "12 columns" — implementation follows
OD spec v1.8 §C-OD-27.1 (14 columns) per checkpoint-ratified spec-canonical
discipline; plan canonical-reading amendment owed at OD plan v2.24.

Parent-directory creation at `db_path.parent` is implementer-discretion per
§27.5 silence; default is `mkdir(parents=True, exist_ok=True)` for true
idempotent init.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = [
    "INDEX_DDL",
    "SPANS_DDL",
    "SpanInsertRow",
    "initialize_span_store",
    "insert_spans",
    "retention_cleanup_lazy",
]

_NS_PER_DAY: int = 86_400 * 1_000_000_000


SPANS_DDL = """\
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT NULL,
    name TEXT NOT NULL,
    kind INTEGER NOT NULL,
    start_time_ns INTEGER NOT NULL,
    end_time_ns INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    status_message TEXT NULL,
    attributes_json TEXT NOT NULL,
    events_json TEXT NOT NULL,
    workflow_id TEXT NULL,
    workflow_run_id TEXT NULL,
    workflow_idempotency_key TEXT NULL
)
"""

INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_workflow ON spans(workflow_id, workflow_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_idempotency ON spans(workflow_idempotency_key)",
    "CREATE INDEX IF NOT EXISTS idx_trace ON spans(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_time_range ON spans(start_time_ns, end_time_ns)",
)


def initialize_span_store(db_path: Path) -> sqlite3.Connection:
    """Open the span-store sqlite db, apply pragmas, and ensure schema.

    Caller owns the returned connection and is responsible for `close()`.
    Re-invocation against an existing db is a no-op (schema + indexes use
    `IF NOT EXISTS`; pragmas are idempotent).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # `check_same_thread=False` permits the runtime to dispatch sqlite calls
    # via `asyncio.to_thread` (different worker threads across calls). Caller
    # must serialize writes, which `RuntimeRingBuffer.flush_to_sqlite` does by
    # virtue of single-writer invocation through the asyncio event loop.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(SPANS_DDL)
    for stmt in INDEX_DDL:
        conn.execute(stmt)
    conn.commit()
    return conn


class SpanInsertRow(BaseModel):
    """Typed 14-column row matching `spans` table per OD spec v1.8 §C-OD-27.1.

    Frozen Pydantic v2 model preserving column-set + nullability discipline at
    the OD-axis schema boundary. Caller (e.g. `harness-runtime` ring-buffer
    flush) projects its in-memory span shape into this carrier before invoking
    `insert_spans`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_time_ns: int
    end_time_ns: int
    status_code: int
    status_message: str | None
    attributes_json: str
    events_json: str
    workflow_id: str | None
    workflow_run_id: str | None
    workflow_idempotency_key: str | None


_INSERT_SQL = """\
INSERT OR IGNORE INTO spans (
    span_id, trace_id, parent_span_id, name, kind,
    start_time_ns, end_time_ns, status_code, status_message,
    attributes_json, events_json,
    workflow_id, workflow_run_id, workflow_idempotency_key
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def insert_spans(conn: sqlite3.Connection, rows: Iterable[SpanInsertRow]) -> int:
    """Batch-insert spans with INSERT OR IGNORE per spec §C-OD-27.4 invariant 3.

    Returns the number of rows actually inserted (excludes rows skipped by the
    primary-key IGNORE clause). Transaction commit is the caller's responsibility
    when batching across multiple `insert_spans` calls; we commit once per call
    to make idempotency at-call-site observable.
    """
    tuples = [
        (
            row.span_id,
            row.trace_id,
            row.parent_span_id,
            row.name,
            row.kind,
            row.start_time_ns,
            row.end_time_ns,
            row.status_code,
            row.status_message,
            row.attributes_json,
            row.events_json,
            row.workflow_id,
            row.workflow_run_id,
            row.workflow_idempotency_key,
        )
        for row in rows
    ]
    if not tuples:
        return 0
    cur = conn.executemany(_INSERT_SQL, tuples)
    conn.commit()
    return cur.rowcount


def retention_cleanup_lazy(conn: sqlite3.Connection, retention_days: int, now_ns: int) -> int:
    """Delete rows with `end_time_ns < (now_ns - retention_days * 86_400 * 1e9)`.

    Implements OD spec v1.8 §C-OD-27.2 row 3 retention policy under the
    §27.5 row 2 lazy-on-write default discipline. Caller invokes once per
    flush (or per any sqlite-write moment) — no background task per
    deliberate avoidance of additional async machinery.

    Returns the count of rows deleted. `retention_days` is validated > 0 at
    `CollectorConfig` boundary; we trust the contract here.
    """
    cutoff_ns = now_ns - retention_days * _NS_PER_DAY
    cur = conn.execute("DELETE FROM spans WHERE end_time_ns < ?", (cutoff_ns,))
    conn.commit()
    return cur.rowcount
