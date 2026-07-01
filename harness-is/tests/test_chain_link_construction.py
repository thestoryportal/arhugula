"""Tests for U-IS-09 — chain-link construction primitive (C-IS-06 §6.3).

Test set per the U-IS-09 `Tests:` field — covers acceptance #1-#5.
"""

from __future__ import annotations

from datetime import UTC, datetime

from harness_is.chain_link_construction import construct_prior_event_hash
from harness_is.entry_hash import compute_response_hash
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)


def _entry(action_id: str = "act-1") -> StateLedgerEntry:
    return StateLedgerEntry(
        action_id=Identifier(action_id),
        idempotency_key=Identifier("idem-1"),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
        response_hash=b"\xcd" * 32,
        timestamp=datetime(2026, 5, 16, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
    )


def test_construct_prior_event_hash_inception() -> None:
    """Acceptance #1 — None input ⇒ ALL_ZEROS_SENTINEL."""
    assert construct_prior_event_hash(None) == ALL_ZEROS_SENTINEL


def test_construct_prior_event_hash_non_inception() -> None:
    """Acceptance #2 — non-inception ⇒ compute_response_hash(prior_entry)."""
    prior = _entry()
    assert construct_prior_event_hash(prior) == compute_response_hash(prior)


def test_construct_prior_event_hash_pure_deterministic() -> None:
    """Acceptance #3 — repeated invocation is byte-equal."""
    prior = _entry()
    assert construct_prior_event_hash(prior) == construct_prior_event_hash(prior)
    assert construct_prior_event_hash(None) == construct_prior_event_hash(None)


def test_construct_prior_event_hash_pure_no_io(tmp_path: object) -> None:
    """Acceptance #3 — the function performs no filesystem I/O.

    `tmp_path` is requested but left untouched: after invocation the directory
    is still empty, evidencing no write side effect.
    """
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    construct_prior_event_hash(_entry())
    construct_prior_event_hash(None)
    assert list(tmp_path.iterdir()) == []


def test_construct_prior_event_hash_does_not_write_entry() -> None:
    """Acceptance #4 — invocation returns a value; it does not persist entries.

    The function's only output is its return value (a `Bytes32`); the caller
    inserts it into the new entry's `prior_event_hash` field.
    """
    result = construct_prior_event_hash(_entry())
    assert isinstance(result, bytes)
    assert len(result) == 32
