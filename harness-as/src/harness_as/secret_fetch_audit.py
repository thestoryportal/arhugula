"""Secret-fetch audit-ledger entry composition — U-AS-26.

Implements C-AS-08 §8.2 (audit-ledger entry shape against C-IS-05), §8.3
(hash-chain integrity composition). Declares `SecretFetchEvent`,
`compose_secret_fetch_audit_entry`, and `verify_secret_fetch_entry_in_chain`.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-26 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §8.2-§8.3 C-AS-08; ADR-F5 v1.1.

Depends on: U-AS-20 (`SecretScope`); U-AS-25 (`compute_outputs_hash`); and the
cross-axis IS surface — U-IS-07 (`StateLedgerEntry`, `Actor`, `Bytes32`,
`Identifier`, `Timestamp`, `ALL_ZEROS_SENTINEL`), U-IS-09
(`construct_prior_event_hash`), U-IS-10 (`verify_chain`,
`ChainVerificationResult`) — consumed read-only.

Idempotency-key construction (documented discretion): AC6 delegates the §7.1
idempotency-key construction to the IS axis, but the IS surface exports no
reusable idempotency-key constructor at this grade, and the §7.1 formula's
inputs (`conversation_id`, `step_index`, `tool`, `canonical_args`) are not all
carried by a `SecretFetchEvent`. U-AS-26 constructs a deterministic Stripe-style
key by SHA-256 over the event's identity fields (`thread_id`, `step_id`,
`secret_name`, `secret_scope.name`) — the secret-fetch event's stable identity.
"""

from __future__ import annotations

import hashlib
import uuid

from harness_is.chain_link_construction import construct_prior_event_hash
from harness_is.chain_verification import ChainVerificationResult, verify_chain
from harness_is.state_ledger_entry_schema import (
    Actor,
    Identifier,
    StateLedgerEntry,
    Timestamp,
)
from pydantic import BaseModel, ConfigDict

from harness_as.secret_fetch import SecretScope
from harness_as.secret_outputs_hash import compute_outputs_hash


class SecretFetchEvent(BaseModel):
    """A secret-fetch event to be composed into an audit-ledger entry (§8.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    secret_name: str
    secret_scope: SecretScope
    secret_last_rotated_at: str
    """ISO-8601 timestamp string — a version attribute (structure, not value)."""

    actor: Actor
    timestamp: Timestamp
    thread_id: Identifier
    step_id: Identifier


def _idempotency_key(event: SecretFetchEvent) -> Identifier:
    """Construct a deterministic Stripe-style idempotency key for the event."""
    digest = hashlib.sha256(
        "\x00".join(
            (
                event.thread_id,
                event.step_id,
                event.secret_name,
                event.secret_scope.name,
            )
        ).encode("utf-8")
    ).hexdigest()
    return Identifier(digest)


def compose_secret_fetch_audit_entry(
    event: SecretFetchEvent,
    prior_entry: StateLedgerEntry | None,
) -> StateLedgerEntry:
    """Compose a secret-fetch audit-ledger entry (C-AS-08 §8.2).

    Returns a `StateLedgerEntry` conforming to the U-IS-07 six-field shape — no
    AS-side field additions (acceptance #1 / #7). Per-field population per the
    §8.2 table: `response_hash` is the structure-not-content `outputs_hash`
    (U-AS-25); `prior_event_hash` is `construct_prior_event_hash` (U-IS-09) —
    chain inception (`prior_entry is None`) yields the all-zeros sentinel
    (acceptance #8). The entry carries no secret value (acceptance #9 — the
    six-field shape has no value field).
    """
    return StateLedgerEntry(
        action_id=Identifier(str(uuid.uuid4())),
        idempotency_key=_idempotency_key(event),
        actor=event.actor,
        response_hash=compute_outputs_hash(
            event.secret_name, event.secret_scope, event.secret_last_rotated_at
        ),
        timestamp=event.timestamp,
        prior_event_hash=construct_prior_event_hash(prior_entry),
    )


def verify_secret_fetch_entry_in_chain(
    entry: StateLedgerEntry,
    ledger_position: int,
    full_ledger: list[StateLedgerEntry],
) -> ChainVerificationResult:
    """Verify a secret-fetch entry's hash-chain integrity (C-AS-08 §8.3).

    Delegates to U-IS-10's `verify_chain` (acceptance #5) — secret-fetch audit
    entries participate in the F2 state-ledger hash-chain and are verified by
    the same whole-ledger tamper-evidence pass (C-IS-06 §6.4-§6.5). `entry` and
    `ledger_position` are advisory; `verify_chain` verifies the full chain.
    """
    return verify_chain(full_ledger)
