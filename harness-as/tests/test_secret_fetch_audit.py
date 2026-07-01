"""Tests for U-AS-26 — secret-fetch audit-ledger entry composition (C-AS-08 §8.2-§8.3)."""

from __future__ import annotations

import datetime

from harness_as.secret_fetch import SecretScope
from harness_as.secret_fetch_audit import (
    SecretFetchEvent,
    compose_secret_fetch_audit_entry,
    verify_secret_fetch_entry_in_chain,
)
from harness_as.secret_outputs_hash import compute_outputs_hash
from harness_is.chain_link_construction import construct_prior_event_hash
from harness_is.chain_verification import VerificationStatus
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-1")
_TS = datetime.datetime(2026, 5, 16, 0, 0, 0, tzinfo=datetime.UTC)


def _event(*, name: str = "ANTHROPIC_API_KEY") -> SecretFetchEvent:
    return SecretFetchEvent(
        secret_name=name,
        secret_scope=SecretScope(name="prod"),
        secret_last_rotated_at="2026-05-16T00:00:00Z",
        actor=_ACTOR,
        timestamp=_TS,
        thread_id=Identifier("thread-1"),
        step_id=Identifier("step-1"),
    )


def test_compose_audit_entry_six_field_shape() -> None:
    """Acceptance #1 — composed entry inherits F2 shape; the D-derivative sidecars are admissible.

    Per IS spec v1.3 §5.1 (LANDED PR #89): `StateLedgerEntry` gains the
    `procedural_tier_snapshot_ref` D-derivative sidecar; per IS spec v1.8 §5.4
    (U-IS-19): it gains the `branch_metadata` D-derivative sidecar — both
    additive to the F-layer 6-field shape.
    """
    entry = compose_secret_fetch_audit_entry(_event(), None)
    assert isinstance(entry, StateLedgerEntry)
    assert set(StateLedgerEntry.model_fields) == {
        "action_id",
        "idempotency_key",
        "actor",
        "response_hash",
        "timestamp",
        "prior_event_hash",
        "procedural_tier_snapshot_ref",
        "branch_metadata",
    }


def test_compose_audit_entry_response_hash_from_u_as_25() -> None:
    """Acceptance #2 — response_hash is the U-AS-25 outputs_hash."""
    event = _event()
    entry = compose_secret_fetch_audit_entry(event, None)
    assert entry.response_hash == compute_outputs_hash(
        event.secret_name, event.secret_scope, event.secret_last_rotated_at
    )


def test_compose_audit_entry_prior_event_hash_from_u_is_09() -> None:
    """Acceptance #2 — prior_event_hash is construct_prior_event_hash (U-IS-09)."""
    first = compose_secret_fetch_audit_entry(_event(), None)
    second = compose_secret_fetch_audit_entry(_event(name="OTHER_KEY"), first)
    assert second.prior_event_hash == construct_prior_event_hash(first)


def test_compose_audit_entry_inception_sentinel() -> None:
    """Acceptance #8 — prior_entry=None yields the all-zeros inception sentinel."""
    entry = compose_secret_fetch_audit_entry(_event(), None)
    assert entry.prior_event_hash == ALL_ZEROS_SENTINEL


def test_compose_audit_entry_actor_preservation() -> None:
    """Acceptance #2 — the event actor is preserved on the entry."""
    entry = compose_secret_fetch_audit_entry(_event(), None)
    assert entry.actor == _ACTOR


def test_compose_audit_entry_idempotency_key_uses_thread_step() -> None:
    """Acceptance #2/#6 — the idempotency key is deterministic over the event identity."""
    a = compose_secret_fetch_audit_entry(_event(), None)
    b = compose_secret_fetch_audit_entry(_event(), None)
    assert a.idempotency_key == b.idempotency_key
    # A different secret identity yields a different key.
    c = compose_secret_fetch_audit_entry(_event(name="OTHER_KEY"), None)
    assert c.idempotency_key != a.idempotency_key


def test_verify_secret_fetch_entry_in_chain_delegates_to_u_is_10() -> None:
    """Acceptance #5 — chain verification delegates to U-IS-10's verify_chain."""
    first = compose_secret_fetch_audit_entry(_event(), None)
    second = compose_secret_fetch_audit_entry(_event(name="OTHER_KEY"), first)
    result = verify_secret_fetch_entry_in_chain(second, 2, [first, second])
    assert result.status is VerificationStatus.VALID
    assert result.entries_verified == 2


def test_compose_audit_entry_schema_uniform_with_non_secret_entries() -> None:
    """Acceptance #7 — the secret-fetch entry shares the StateLedgerEntry schema."""
    entry = compose_secret_fetch_audit_entry(_event(), None)
    assert type(entry) is StateLedgerEntry


def test_compose_audit_entry_negative_no_secret_value_field() -> None:
    """Acceptance #9 — the entry carries no secret-value field."""
    entry = compose_secret_fetch_audit_entry(_event(), None)
    for forbidden in ("value", "secret", "secret_value"):
        assert forbidden not in StateLedgerEntry.model_fields
        assert not hasattr(entry, forbidden)
