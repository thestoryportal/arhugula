"""Tests for U-IS-11 — C3-pole append-only write contract (C-IS-07 §7.1/§7.3).

Test set per the U-IS-11 `Tests:` field — 13 tests covering acceptance #1-#10.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.entry_hash import compute_response_hash
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
)
from harness_is.state_ledger_write import (
    EntryPayload,
    NonMonotonicTimestampError,
    WriteKey,
    WriteKeyMismatchError,
    WriteResult,
    append_ledger_entry,
    read_ledger,
)
from pydantic import ValidationError

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-1")


def _handle(tmp_path: Path) -> JsonlLedgerHandle:
    return JsonlLedgerHandle(canonical_path=tmp_path / "state.jsonl", exists=False, entry_count=0)


def _payload(i: int, hour: int = 1) -> EntryPayload:
    return EntryPayload(
        action_id=Identifier(f"act-{i}"),
        idempotency_key=Identifier(f"idem-{i}"),
        actor=_ACTOR,
        timestamp=datetime(2026, 5, 16, hour, tzinfo=UTC),
    )


def _key(i: int) -> WriteKey:
    return WriteKey(
        thread_id=Identifier("thread-1"),
        step_id=Identifier(f"step-{i}"),
        idempotency_key=Identifier(f"idem-{i}"),
    )


def test_append_appends_to_jsonl_file(tmp_path: Path) -> None:
    """Acceptance #1 — a write appends a line to the JSONL ledger."""
    handle = _handle(tmp_path)
    assert append_ledger_entry(handle, _payload(0), _key(0)) is WriteResult.APPENDED
    assert len(handle.canonical_path.read_text().splitlines()) == 1


def test_append_preserves_order(tmp_path: Path) -> None:
    """Acceptance #1 — appends accumulate at end, in write order."""
    handle = _handle(tmp_path)
    for i in range(3):
        append_ledger_entry(handle, _payload(i), _key(i))
    lines = handle.canonical_path.read_text().splitlines()
    assert [json.loads(line)["action_id"] for line in lines] == ["act-0", "act-1", "act-2"]


def test_append_idempotent_noop_on_duplicate_writekey(tmp_path: Path) -> None:
    """Acceptance #4 — a repeat write with the same key is an IDEMPOTENT_NOOP."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0), _key(0))
    assert append_ledger_entry(handle, _payload(0), _key(0)) is WriteResult.IDEMPOTENT_NOOP
    assert len(handle.canonical_path.read_text().splitlines()) == 1


def test_append_idempotent_preserves_first_payload(tmp_path: Path) -> None:
    """Acceptance #4 — an idempotent no-op does not overwrite the first payload."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0), _key(0))
    second = EntryPayload(
        action_id=Identifier("act-DIFFERENT"),
        idempotency_key=Identifier("idem-0"),
        actor=_ACTOR,
        timestamp=datetime(2026, 5, 16, 2, tzinfo=UTC),
    )
    append_ledger_entry(handle, second, _key(0))
    line = handle.canonical_path.read_text().splitlines()[0]
    assert json.loads(line)["action_id"] == "act-0"


def test_append_rejects_writekey_idempotency_key_mismatch(tmp_path: Path) -> None:
    """Acceptance — write_key.idempotency_key must equal the payload's."""
    handle = _handle(tmp_path)
    with pytest.raises(WriteKeyMismatchError):
        append_ledger_entry(handle, _payload(0), _key(99))


def test_append_computes_response_hash(tmp_path: Path) -> None:
    """Acceptance #6 — the persisted entry's response_hash is computed."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0), _key(0))
    [entry] = read_ledger(handle)
    assert entry.response_hash == compute_response_hash(entry)


def test_append_inception_prior_event_hash(tmp_path: Path) -> None:
    """Acceptance #6 — the first entry's prior_event_hash is the sentinel."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0), _key(0))
    [entry] = read_ledger(handle)
    assert entry.prior_event_hash == ALL_ZEROS_SENTINEL


def test_append_non_inception_prior_event_hash(tmp_path: Path) -> None:
    """Acceptance #6 — entry 2's prior_event_hash links to entry 1."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0), _key(0))
    append_ledger_entry(handle, _payload(1, hour=2), _key(1))
    first, second = read_ledger(handle)
    assert second.prior_event_hash == compute_response_hash(first)


def test_append_one_entry_per_line(tmp_path: Path) -> None:
    """Acceptance #5 — every JSONL line parses as exactly one JSON object."""
    handle = _handle(tmp_path)
    for i in range(3):
        append_ledger_entry(handle, _payload(i), _key(i))
    for line in handle.canonical_path.read_text().splitlines():
        assert isinstance(json.loads(line), dict)


def test_append_rejects_caller_supplied_response_hash() -> None:
    """Acceptance #8 — EntryPayload omits hash fields; supplying one is rejected."""
    with pytest.raises(ValidationError):
        EntryPayload(
            action_id=Identifier("act-0"),
            idempotency_key=Identifier("idem-0"),
            actor=_ACTOR,
            timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
            response_hash=b"\x00" * 32,  # pyright: ignore[reportCallIssue]
        )


def test_append_rejects_non_monotonic_timestamp(tmp_path: Path) -> None:
    """Acceptance #9 — a timestamp earlier than the prior entry's is rejected."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0, hour=12), _key(0))
    with pytest.raises(NonMonotonicTimestampError):
        append_ledger_entry(handle, _payload(1, hour=3), _key(1))


def test_append_chain_verifies_after_writes(tmp_path: Path) -> None:
    """Acceptance #6 — the ledger after a run of writes verifies as a valid chain."""
    handle = _handle(tmp_path)
    for i in range(5):
        append_ledger_entry(handle, _payload(i, hour=i + 1), _key(i))
    assert verify_chain(read_ledger(handle)).status is VerificationStatus.VALID


def test_append_concurrent_writes_serialized(tmp_path: Path) -> None:
    """Acceptance #7 — concurrent appends are serialized: the resulting chain
    is valid and every write landed."""
    handle = _handle(tmp_path)

    def _write(i: int) -> None:
        append_ledger_entry(handle, _payload(i, hour=1), _key(i))

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ledger = read_ledger(handle)
    assert len(ledger) == 6
    assert verify_chain(ledger).status is VerificationStatus.VALID
