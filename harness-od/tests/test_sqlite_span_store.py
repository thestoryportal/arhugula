"""U-OD-42 — sqlite_span_store schema + WAL-mode init verification.

Tests verify spec §C-OD-27.1 schema verbatim via `PRAGMA table_info`
introspection (sqlite normalizes DDL on store, so column-level introspection
is the auditable form). §C-OD-27.2 row 2 pragmas (`journal_mode=WAL`,
`foreign_keys=OFF`) verified via `PRAGMA` query.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from harness_od.sqlite_span_store import (
    SpanInsertRow,
    initialize_span_store,
    insert_spans,
    retention_cleanup_lazy,
)

_NS_PER_DAY = 86_400 * 1_000_000_000


def _make_row(
    span_id: str = "s1",
    *,
    parent_span_id: str | None = None,
    end_time_ns: int = 200,
) -> SpanInsertRow:
    return SpanInsertRow(
        span_id=span_id,
        trace_id="t1",
        parent_span_id=parent_span_id,
        name="workflow.envelope",
        kind=0,
        start_time_ns=100,
        end_time_ns=end_time_ns,
        status_code=0,
        status_message=None,
        attributes_json="{}",
        events_json="[]",
        workflow_id=None,
        workflow_run_id=None,
        workflow_idempotency_key=None,
    )


# Spec §C-OD-27.1 canonical 14-column schema. Column order matches the
# CREATE TABLE statement; (name, type, notnull, pk).
_EXPECTED_COLUMNS: tuple[tuple[str, str, int, int], ...] = (
    ("span_id", "TEXT", 0, 1),
    ("trace_id", "TEXT", 1, 0),
    ("parent_span_id", "TEXT", 0, 0),
    ("name", "TEXT", 1, 0),
    ("kind", "INTEGER", 1, 0),
    ("start_time_ns", "INTEGER", 1, 0),
    ("end_time_ns", "INTEGER", 1, 0),
    ("status_code", "INTEGER", 1, 0),
    ("status_message", "TEXT", 0, 0),
    ("attributes_json", "TEXT", 1, 0),
    ("events_json", "TEXT", 1, 0),
    ("workflow_id", "TEXT", 0, 0),
    ("workflow_run_id", "TEXT", 0, 0),
    ("workflow_idempotency_key", "TEXT", 0, 0),
)

_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {"idx_workflow", "idx_idempotency", "idx_trace", "idx_time_range"}
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "observability" / "spans.db"


def test_initialize_creates_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "observability" / "spans.db"
    assert not db_path.parent.exists()
    conn = initialize_span_store(db_path)
    try:
        assert db_path.parent.is_dir()
        assert db_path.exists()
    finally:
        conn.close()


def test_spans_table_has_14_columns_per_spec_c_od_27_1(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(spans)").fetchall()
    finally:
        conn.close()
    assert len(rows) == 14, f"expected 14 columns per §C-OD-27.1; got {len(rows)}"
    actual = tuple((r[1], r[2], r[3], r[5]) for r in rows)
    assert actual == _EXPECTED_COLUMNS


def test_span_id_is_primary_key(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(spans)").fetchall()
    finally:
        conn.close()
    pks = [r[1] for r in rows if r[5] == 1]
    assert pks == ["span_id"]


def test_four_indexes_created_per_spec_c_od_27_1(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='spans'"
        ).fetchall()
    finally:
        conn.close()
    names = {r[0] for r in rows if not r[0].startswith("sqlite_")}
    assert names == _EXPECTED_INDEXES


def test_idx_workflow_is_composite_over_workflow_id_and_run_id(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        rows = conn.execute("PRAGMA index_info(idx_workflow)").fetchall()
    finally:
        conn.close()
    cols = [r[2] for r in rows]
    assert cols == ["workflow_id", "workflow_run_id"]


def test_idx_time_range_is_composite_over_start_and_end_time_ns(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        rows = conn.execute("PRAGMA index_info(idx_time_range)").fetchall()
    finally:
        conn.close()
    cols = [r[2] for r in rows]
    assert cols == ["start_time_ns", "end_time_ns"]


def test_wal_mode_enabled_per_spec_27_2_row_2(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_foreign_keys_off_per_spec_27_2_row_2(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()
    assert fk == 0


def test_re_initialization_is_idempotent_per_ac_4(db_path: Path) -> None:
    conn1 = initialize_span_store(db_path)
    conn1.execute(
        "INSERT INTO spans (span_id, trace_id, name, kind, start_time_ns, "
        "end_time_ns, status_code, attributes_json, events_json) VALUES "
        "('s1', 't1', 'workflow.envelope', 1, 100, 200, 0, '{}', '[]')"
    )
    conn1.commit()
    conn1.close()

    # Re-initialize against the existing db — must not raise + must preserve data.
    conn2 = initialize_span_store(db_path)
    try:
        rows = conn2.execute("SELECT span_id FROM spans").fetchall()
    finally:
        conn2.close()
    assert rows == [("s1",)]


def test_insert_or_ignore_preserves_existing_row_per_spec_27_4_inv_3(
    db_path: Path,
) -> None:
    conn = initialize_span_store(db_path)
    try:
        conn.execute(
            "INSERT INTO spans (span_id, trace_id, name, kind, start_time_ns, "
            "end_time_ns, status_code, attributes_json, events_json) VALUES "
            "('s1', 't1', 'original', 1, 100, 200, 0, '{}', '[]')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO spans (span_id, trace_id, name, kind, "
            "start_time_ns, end_time_ns, status_code, attributes_json, "
            "events_json) VALUES "
            "('s1', 't1', 'duplicate', 1, 300, 400, 0, '{}', '[]')"
        )
        conn.commit()
        row = conn.execute("SELECT name FROM spans WHERE span_id = 's1'").fetchone()
    finally:
        conn.close()
    assert row == ("original",)


def test_insert_spans_returns_zero_for_empty_iterable(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        assert insert_spans(conn, []) == 0
    finally:
        conn.close()


def test_insert_spans_writes_batch_and_returns_count(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    rows = [_make_row(f"s{i}") for i in range(10)]
    try:
        inserted = insert_spans(conn, rows)
        count = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    finally:
        conn.close()
    assert inserted == 10
    assert count == 10


def test_insert_spans_idempotent_on_re_flush_per_spec_27_4_inv_3(
    db_path: Path,
) -> None:
    conn = initialize_span_store(db_path)
    rows = [_make_row(f"s{i}") for i in range(5)]
    try:
        first = insert_spans(conn, rows)
        second = insert_spans(conn, rows)
        count = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    finally:
        conn.close()
    assert first == 5
    assert second == 0
    assert count == 5


def test_insert_spans_partial_overlap_inserts_only_new(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        insert_spans(conn, [_make_row("s1"), _make_row("s2")])
        added = insert_spans(conn, [_make_row("s2"), _make_row("s3"), _make_row("s4")])
        ids = sorted(r[0] for r in conn.execute("SELECT span_id FROM spans"))
    finally:
        conn.close()
    assert added == 2
    assert ids == ["s1", "s2", "s3", "s4"]


def test_insert_spans_preserves_parent_span_id_when_set(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        insert_spans(conn, [_make_row("s1", parent_span_id="p1")])
        row = conn.execute("SELECT parent_span_id FROM spans WHERE span_id='s1'").fetchone()
    finally:
        conn.close()
    assert row == ("p1",)


def test_span_insert_row_is_frozen(db_path: Path) -> None:
    row = _make_row("s1")
    with pytest.raises(Exception):  # pydantic raises ValidationError on frozen
        row.span_id = "s2"  # type: ignore[misc]


def test_retention_cleanup_deletes_rows_older_than_horizon(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    now_ns = 100 * _NS_PER_DAY
    old = [_make_row(f"old{i}", end_time_ns=10 * _NS_PER_DAY) for i in range(5)]
    fresh = [_make_row(f"new{i}", end_time_ns=99 * _NS_PER_DAY) for i in range(3)]
    try:
        insert_spans(conn, old + fresh)
        deleted = retention_cleanup_lazy(conn, retention_days=7, now_ns=now_ns)
        remaining = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    finally:
        conn.close()
    assert deleted == 5
    assert remaining == 3


def test_retention_cleanup_returns_zero_when_nothing_expired(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    now_ns = 10 * _NS_PER_DAY
    rows = [_make_row(f"s{i}", end_time_ns=9 * _NS_PER_DAY) for i in range(4)]
    try:
        insert_spans(conn, rows)
        deleted = retention_cleanup_lazy(conn, retention_days=7, now_ns=now_ns)
    finally:
        conn.close()
    assert deleted == 0


def test_retention_horizon_boundary_inclusive_at_cutoff(db_path: Path) -> None:
    """Spec §27.2 row 3: DELETE WHERE end_time_ns < cutoff (strict less-than).
    Row at exactly cutoff_ns is preserved."""
    conn = initialize_span_store(db_path)
    now_ns = 100 * _NS_PER_DAY
    cutoff_ns = now_ns - 7 * _NS_PER_DAY
    boundary = _make_row("boundary", end_time_ns=cutoff_ns)
    just_before = _make_row("expired", end_time_ns=cutoff_ns - 1)
    try:
        insert_spans(conn, [boundary, just_before])
        deleted = retention_cleanup_lazy(conn, retention_days=7, now_ns=now_ns)
        remaining = sorted(r[0] for r in conn.execute("SELECT span_id FROM spans"))
    finally:
        conn.close()
    assert deleted == 1
    assert remaining == ["boundary"]


def test_returns_open_sqlite3_connection(db_path: Path) -> None:
    conn = initialize_span_store(db_path)
    try:
        assert isinstance(conn, sqlite3.Connection)
        # Connection is usable: smoke query.
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()
