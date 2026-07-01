"""Tests for U-IS-10 — chain verification + tamper-evidence (C-IS-06 §6.4/§6.5).

Test set per the U-IS-10 `Tests:` field — core verification (acceptance #1-#6)
+ the 5 §6.5 tamper-evidence scenarios (acceptance #7).
"""

from __future__ import annotations

from datetime import UTC, datetime

from harness_is.chain_verification import (
    FailureType,
    VerificationStatus,
    verify_chain,
)
from harness_is.entry_hash import compute_response_hash
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)


def _entry(action_id: str, prior_event_hash: bytes) -> StateLedgerEntry:
    return StateLedgerEntry(
        action_id=Identifier(action_id),
        idempotency_key=Identifier(f"idem-{action_id}"),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
        response_hash=b"\x00" * 32,
        timestamp=datetime(2026, 5, 16, tzinfo=UTC),
        prior_event_hash=prior_event_hash,
    )


def _valid_chain(n: int) -> list[StateLedgerEntry]:
    """Build an n-entry chain with correct `prior_event_hash` links."""
    chain: list[StateLedgerEntry] = []
    prior = ALL_ZEROS_SENTINEL
    for i in range(n):
        entry = _entry(f"act-{i}", prior)
        chain.append(entry)
        prior = compute_response_hash(entry)
    return chain


# --- core verification (acceptance #1-#6) -----------------------------------


def test_verify_chain_empty_ledger() -> None:
    """Acceptance #1 — empty ledger ⇒ VALID, entries_verified=0."""
    result = verify_chain([])
    assert result.status is VerificationStatus.VALID
    assert result.entries_verified == 0


def test_verify_chain_inception_valid() -> None:
    """Acceptance #2 — single entry with sentinel prior ⇒ VALID."""
    assert verify_chain(_valid_chain(1)).status is VerificationStatus.VALID


def test_verify_chain_inception_invalid() -> None:
    """Acceptance #3 — single entry with non-sentinel prior ⇒ INVALID at 1."""
    result = verify_chain([_entry("act-0", b"\x07" * 32)])
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 1
    assert result.failure_type is FailureType.INCEPTION_SENTINEL_MISMATCH


def test_verify_chain_valid_multi_entry() -> None:
    """Acceptance #4 — N-entry valid chain ⇒ VALID, entries_verified=N."""
    result = verify_chain(_valid_chain(5))
    assert result.status is VerificationStatus.VALID
    assert result.entries_verified == 5


def test_verify_chain_invalid_at_position_K() -> None:  # noqa: N802 — U-IS-10 plan `Tests:` name verbatim
    """Acceptance #5 — a broken link at K ⇒ INVALID, failure_position=K."""
    chain = _valid_chain(5)
    chain[3] = _entry("act-3", b"\x09" * 32)  # break the link into position 4
    result = verify_chain(chain)
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 4
    assert result.failure_type is FailureType.CHAIN_LINK_MISMATCH


def test_verify_chain_read_only() -> None:
    """Acceptance #6 — verification does not modify the ledger."""
    chain = _valid_chain(4)
    snapshot = [e.model_dump() for e in chain]
    verify_chain(chain)
    assert [e.model_dump() for e in chain] == snapshot


# --- tamper-evidence per §6.5 (acceptance #7) -------------------------------


def test_tamper_entry_content_modification() -> None:
    """§6.5 row 1 — content modification at K ⇒ failure at K+1."""
    chain = _valid_chain(5)
    tampered = chain[2].model_copy(
        update={"actor": Actor(actor_class=ActorClass.OPERATOR, actor_id="evil")}
    )
    chain[2] = tampered
    result = verify_chain(chain)
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 4  # 1-indexed K+1 for 0-indexed K=2
    assert result.failure_type is FailureType.CHAIN_LINK_MISMATCH


def test_tamper_prior_event_hash_modification() -> None:
    """§6.5 row 2 — prior_event_hash modification at K ⇒ failure at K."""
    chain = _valid_chain(5)
    chain[3] = chain[3].model_copy(update={"prior_event_hash": b"\x55" * 32})
    result = verify_chain(chain)
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 4  # 1-indexed K for 0-indexed K=3


def test_tamper_entry_deletion_mid_chain() -> None:
    """§6.5 row 3 — deletion mid-chain ⇒ failure at the new position K."""
    chain = _valid_chain(5)
    del chain[2]  # the entry now at 0-indexed 2 has a stale prior
    result = verify_chain(chain)
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 3


def test_tamper_entry_insertion_mid_chain() -> None:
    """§6.5 row 4 — a forged entry inserted mid-chain ⇒ failure at K+1."""
    chain = _valid_chain(5)
    # Forge an entry whose prior links correctly to chain[1] — it passes, but
    # the original chain[2] downstream still references the original chain[1].
    forged = _entry("forged", compute_response_hash(chain[1]))
    chain.insert(2, forged)
    result = verify_chain(chain)
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 4  # the displaced original entry


def test_tamper_inception_modification() -> None:
    """§6.5 row 5 — entry[1].prior_event_hash made non-sentinel ⇒ failure at 1."""
    chain = _valid_chain(3)
    chain[0] = chain[0].model_copy(update={"prior_event_hash": b"\x01" * 32})
    result = verify_chain(chain)
    assert result.status is VerificationStatus.INVALID
    assert result.failure_position == 1
    assert result.failure_type is FailureType.INCEPTION_SENTINEL_MISMATCH
