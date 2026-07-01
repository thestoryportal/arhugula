"""U-OD-45 — typed read-interface tests for the sqlite span store."""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_od.sqlite_span_store import (
    SpanInsertRow,
    initialize_span_store,
    insert_spans,
)
from harness_od.sqlite_span_store_reader import (
    read_span_by_id,
    read_spans_by_trace,
    read_spans_by_workflow,
)


def _row(
    span_id: str,
    *,
    trace_id: str = "t1",
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
    start_time_ns: int = 100,
) -> SpanInsertRow:
    return SpanInsertRow(
        span_id=span_id,
        trace_id=trace_id,
        parent_span_id=None,
        name="workflow.envelope",
        kind=0,
        start_time_ns=start_time_ns,
        end_time_ns=start_time_ns + 1,
        status_code=0,
        status_message=None,
        attributes_json="{}",
        events_json="[]",
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        workflow_idempotency_key=None,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "observability" / "spans.db"


def test_read_spans_by_workflow_returns_matching_rows_ordered_by_start_time(
    db_path: Path,
) -> None:
    conn = initialize_span_store(db_path)
    try:
        insert_spans(
            conn,
            [
                _row("s2", workflow_id="wf-a", workflow_run_id="run-1", start_time_ns=200),
                _row("s1", workflow_id="wf-a", workflow_run_id="run-1", start_time_ns=100),
                _row("s3", workflow_id="wf-a", workflow_run_id="run-2", start_time_ns=300),
                _row("s4", workflow_id="wf-b", workflow_run_id="run-1", start_time_ns=400),
            ],
        )
        result = read_spans_by_workflow(conn, "wf-a", "run-1")
    finally:
        conn.close()
    assert [s.span_id for s in result] == ["s1", "s2"]


def test_read_spans_by_workflow_returns_empty_when_no_match(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        insert_spans(conn, [_row("s1", workflow_id="wf-a", workflow_run_id="run-1")])
        result = read_spans_by_workflow(conn, "wf-missing", "run-1")
    finally:
        conn.close()
    assert result == []


def test_read_spans_by_trace_returns_matching_rows_ordered_by_start_time(
    db_path: Path,
) -> None:
    conn = initialize_span_store(db_path)
    try:
        insert_spans(
            conn,
            [
                _row("b", trace_id="trace-x", start_time_ns=200),
                _row("a", trace_id="trace-x", start_time_ns=100),
                _row("c", trace_id="trace-y", start_time_ns=300),
            ],
        )
        result = read_spans_by_trace(conn, "trace-x")
    finally:
        conn.close()
    assert [s.span_id for s in result] == ["a", "b"]


def test_read_span_by_id_returns_row_when_present(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        insert_spans(conn, [_row("s1", trace_id="t1")])
        result = read_span_by_id(conn, "s1")
    finally:
        conn.close()
    assert result is not None
    assert result.span_id == "s1"
    assert result.trace_id == "t1"


def test_read_span_by_id_returns_none_when_absent(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        result = read_span_by_id(conn, "missing")
    finally:
        conn.close()
    assert result is None


def test_readers_return_typed_span_insert_row_instances(db_path: Path) -> None:
    """Surface contract: reads project back to the same typed carrier the
    writer accepts. Forecloses string-dict drift at consumer-side."""
    conn = initialize_span_store(db_path)
    try:
        insert_spans(conn, [_row("s1", workflow_id="wf-a", workflow_run_id="run-1")])
        wf_result = read_spans_by_workflow(conn, "wf-a", "run-1")
        trace_result = read_spans_by_trace(conn, "t1")
        id_result = read_span_by_id(conn, "s1")
    finally:
        conn.close()
    assert all(isinstance(s, SpanInsertRow) for s in wf_result)
    assert all(isinstance(s, SpanInsertRow) for s in trace_result)
    assert isinstance(id_result, SpanInsertRow)
