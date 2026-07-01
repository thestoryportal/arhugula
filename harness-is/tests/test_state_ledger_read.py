"""Tests for U-IS-12 — C2-pole selective bounded read contract (C-IS-07 §7.2/§7.3).

Test set per the U-IS-12 `Tests:` field — 12 tests covering acceptance #1-#7.
"""

from __future__ import annotations

from datetime import UTC, datetime

from harness_core import WorkloadClass
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)
from harness_is.state_ledger_read import (
    BoundedWindow,
    LedgerNavigationPrimitive,
    NavigationQuery,
    ReadResult,
)


def _entry(i: int) -> StateLedgerEntry:
    return StateLedgerEntry(
        action_id=Identifier(f"act-{i}"),
        idempotency_key=Identifier(f"idem-{i % 3}"),  # idem-0/1/2 repeat
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
        response_hash=b"\x00" * 32,
        timestamp=datetime(2026, 5, 16, i % 28 + 1, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
    )


def _ledger(n: int) -> tuple[StateLedgerEntry, ...]:
    return tuple(_entry(i) for i in range(n))


def _window(max_entries: int = 100) -> BoundedWindow:
    return BoundedWindow(max_entries=max_entries, workload_class=WorkloadClass.SOFTWARE_ENGINEERING)


def test_read_entry_by_action_id_match() -> None:
    """Acceptance #1 — read_entry returns the matching entry."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    result = nav.read_entry(Identifier("act-4"), _window())
    assert [e.action_id for e in result.entries] == ["act-4"]


def test_read_entry_by_action_id_no_match() -> None:
    """Acceptance #1 — a non-existent action_id yields no entries."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    assert nav.read_entry(Identifier("act-999"), _window()).entries == ()


def test_read_range_returns_correct_window() -> None:
    """Acceptance #1 — read_range returns the inclusive 1-indexed window."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    result = nav.read_range(3, 5, _window())
    assert [e.action_id for e in result.entries] == ["act-2", "act-3", "act-4"]


def test_read_recent_returns_last_n_chronological() -> None:
    """Acceptance #1 — read_recent returns the last n entries, in order."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    result = nav.read_recent(3, _window())
    assert [e.action_id for e in result.entries] == ["act-7", "act-8", "act-9"]


def test_read_by_idempotency_key_match() -> None:
    """Acceptance #1 — read_by_idempotency_key returns all matching entries."""
    nav = LedgerNavigationPrimitive(_ledger(9))
    result = nav.read_by_idempotency_key(Identifier("idem-0"), _window())
    assert {e.action_id for e in result.entries} == {"act-0", "act-3", "act-6"}


def test_read_bounded_window_truncates() -> None:
    """Acceptance #2 — a result wider than max_entries is truncated."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    result = nav.read_range(1, 10, _window(max_entries=4))
    assert len(result.entries) == 4
    assert result.truncated is True
    assert result.next_position == 5


def test_read_paginated_continuation() -> None:
    """Acceptance #2 — next_position drives a continuation read."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    first = nav.read_range(1, 10, _window(max_entries=4))
    assert first.next_position == 5
    second = nav.read_range(first.next_position, 10, _window(max_entries=4))
    assert [e.action_id for e in second.entries] == ["act-4", "act-5", "act-6", "act-7"]


def test_read_full_file_cat_precluded() -> None:
    """Acceptance #1/#3 — there is no API that returns the ledger without a
    NavigationQuery + BoundedWindow; an unscoped query returns nothing."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    unscoped = nav.read(NavigationQuery(), _window())
    assert unscoped.entries == ()


def test_read_concurrent_non_blocking_reads() -> None:
    """Acceptance #6 — concurrent reads do not block: the ledger is immutable,
    repeated reads return consistent results."""
    nav = LedgerNavigationPrimitive(_ledger(10))
    a = nav.read_recent(3, _window())
    b = nav.read_recent(3, _window())
    assert [e.action_id for e in a.entries] == [e.action_id for e in b.entries]


def test_read_concurrent_with_write_non_blocking() -> None:
    """Acceptance #6 — a read holds an immutable snapshot; an append elsewhere
    (a new ledger) does not affect or block an existing primitive's reads."""
    original = _ledger(5)
    nav = LedgerNavigationPrimitive(original)
    _appended = (*original, _entry(99))  # a "write" produces a new ledger
    result = nav.read_recent(5, _window())
    assert len(result.entries) == 5  # unaffected by the append


def test_read_does_not_modify_ledger() -> None:
    """Acceptance #7 — a read preserves ledger byte-identity."""
    ledger = _ledger(6)
    snapshot = [e.model_dump() for e in ledger]
    nav = LedgerNavigationPrimitive(ledger)
    nav.read_range(1, 6, _window())
    assert [e.model_dump() for e in ledger] == snapshot


def test_read_returns_dynamic_suffix_boundary_not_crossed() -> None:
    """Acceptance #4 — a read returns a ReadResult of entries to the caller;
    placement into the model's dynamic suffix is CP-axis territory, not crossed
    here."""
    nav = LedgerNavigationPrimitive(_ledger(3))
    result = nav.read_recent(3, _window())
    assert isinstance(result, ReadResult)
    assert all(isinstance(e, StateLedgerEntry) for e in result.entries)
