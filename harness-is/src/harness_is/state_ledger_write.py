"""C3-pole append-only write contract — U-IS-11.

Implements C-IS-07 §7.1 (the C3-pole append-only write contract) + §7.3
(composition format — the JSONL storage representation). Declares
`append_ledger_entry`, which appends a hash-chain-preserving state-ledger
entry as one JSON line to the JSONL event ledger.

**§7.3 JSONL line format (this unit owns the storage representation).** Each
ledger line is the JSON object `{action_id, idempotency_key, actor:
{actor_class, actor_id}, response_hash, timestamp, prior_event_hash}` —
`response_hash` / `prior_event_hash` as lowercase hex (a JSON-safe codec for
the 32-byte digests), `timestamp` as ISO-8601. This is the format U-IS-12's
read contract pairs with.

**§7.5 ratified (reading (iii)) — WriteKey ↔ entry shape.** The relationship
between the `(thread_id, step_id, idempotency_key)` keying tuple and the
persisted six-field entry is **reading (iii)**, ratified at IS spec v1.4 §7.5
(this was the C-IS-07 §7.4 / F2-12 deferral, resolved at post-MVP closure
R-CL-P4): the durable idempotency-dedup key is the persisted `idempotency_key`
(the Stripe-style idempotency mechanism — C-IS-05 §5); `thread_id` / `step_id`
are caller-supplied scoping context, validated for consistency but **not
separately persisted** as entry fields (acceptance #10 — WriteKey source not
committed). This matches the landed behavior below; the C-IS-05 §5 six-field
shape is inviolate (IS-AL-3).

Concurrent-writer serialization (acceptance #7) is by a module-level lock
around the read-prior-then-append critical section — an implementation-grade
mechanism per the §6.3 / §7.4 deferral.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.1 U-IS-11
(preserved verbatim from v2.1 §2); Spec_Information_Substrate_v1.md C-IS-07
§7.1 / §7.3; ADR-F2 v1.2 §Consequences (c).
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_is.chain_link_construction import construct_prior_event_hash
from harness_is.entry_hash import compute_response_hash
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    BranchMetadata,
    Identifier,
    StateLedgerEntry,
    Timestamp,
)

#: Clock-skew tolerance for the timestamp-monotonicity check (C-IS-05 §5).
#: Configuration-supplied; defaults to zero (strict non-decreasing order).
_CLOCK_SKEW_TOLERANCE = timedelta(0)

#: Serializes the read-prior-then-append critical section (acceptance #7).
_WRITE_LOCK = threading.Lock()


class EntryPayload(BaseModel):
    """The caller-supplied content of a new ledger entry (C-IS-07 §7.1).

    Omits `response_hash` / `prior_event_hash` — those are computed internally
    (acceptance #8); `extra='forbid'` rejects a caller that supplies them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: Identifier
    idempotency_key: Identifier
    actor: Actor
    timestamp: Timestamp
    # v1.3 NEW D-derivative sidecar (C-IS-05 §5.1; U-IS-11 v2.4 amendment).
    # Optional default preserves backward compat at existing callers; non-None
    # value propagates to StateLedgerEntry + canonicalize + persisted JSONL line.
    procedural_tier_snapshot_ref: Identifier | None = None
    # v1.8 NEW D-derivative sidecar (C-IS-05 §5.4; U-IS-19). Producer-supplied
    # by the CP WorkflowDriver. Optional default preserves backward compat;
    # non-None value propagates to StateLedgerEntry + canonicalize + JSONL line.
    branch_metadata: BranchMetadata | None = None


class WriteKey(BaseModel):
    """The idempotent-write keying tuple (C-IS-07 §7.1, Stripe-style)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: Identifier
    step_id: Identifier
    idempotency_key: Identifier


class WriteResult(StrEnum):
    """The outcome of an `append_ledger_entry` call (C-IS-07 §7.1)."""

    APPENDED = "appended"
    IDEMPOTENT_NOOP = "idempotent_noop"


class WriteKeyMismatchError(ValueError):
    """Raised when `write_key.idempotency_key != entry_payload.idempotency_key`."""


class NonMonotonicTimestampError(ValueError):
    """Raised when an entry's timestamp precedes the prior entry's (C-IS-05 §5)."""


def _serialize_entry(entry: StateLedgerEntry) -> str:
    """Serialize an entry to one §7.3 JSONL line.

    v1.3 sidecar discipline (U-IS-11 v2.4 AC #12): when
    ``procedural_tier_snapshot_ref`` is non-``None``, persisted JSONL line
    includes the field as a 7th key; when ``None``, the key is omitted to
    preserve compact bytes at bootstrap entries.

    v1.8 sidecar discipline (U-IS-19, C-IS-05 §5.4): when ``branch_metadata``
    is non-``None``, the persisted JSONL line includes it as a nested record
    (mirroring the nested ``actor`` shape); when ``None``, the key is omitted.
    """
    payload: dict[str, object] = {
        "action_id": entry.action_id,
        "idempotency_key": entry.idempotency_key,
        "actor": {
            "actor_class": entry.actor.actor_class.value,
            "actor_id": entry.actor.actor_id,
        },
        "response_hash": entry.response_hash.hex(),
        "timestamp": entry.timestamp.isoformat(),
        "prior_event_hash": entry.prior_event_hash.hex(),
    }
    if entry.procedural_tier_snapshot_ref is not None:
        payload["procedural_tier_snapshot_ref"] = entry.procedural_tier_snapshot_ref
    if entry.branch_metadata is not None:
        bm = entry.branch_metadata
        payload["branch_metadata"] = {
            "parent_action_id": bm.parent_action_id,
            "branch_index": bm.branch_index,
            "terminal_status": bm.terminal_status,
        }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _deserialize_entry(line: str) -> StateLedgerEntry:
    """Reconstruct an entry from one §7.3 JSONL line.

    v1.3 sidecar discipline: ``procedural_tier_snapshot_ref`` MAY be absent
    on legacy entries written before v1.3; default ``None`` reproduces the
    schema's optional shape.

    v1.8 sidecar discipline (U-IS-19): ``branch_metadata`` MAY be absent on
    entries written outside a fan-out branch (every pre-v1.8 entry); absent →
    ``None`` reproduces the schema's optional shape.
    """
    raw = json.loads(line)
    snapshot_ref_raw = raw.get("procedural_tier_snapshot_ref")
    branch_metadata_raw = raw.get("branch_metadata")
    return StateLedgerEntry(
        action_id=Identifier(raw["action_id"]),
        idempotency_key=Identifier(raw["idempotency_key"]),
        actor=Actor(
            actor_class=ActorClass(raw["actor"]["actor_class"]),
            actor_id=raw["actor"]["actor_id"],
        ),
        response_hash=bytes.fromhex(raw["response_hash"]),
        timestamp=Timestamp.fromisoformat(raw["timestamp"]),
        prior_event_hash=bytes.fromhex(raw["prior_event_hash"]),
        procedural_tier_snapshot_ref=(
            Identifier(snapshot_ref_raw) if snapshot_ref_raw is not None else None
        ),
        branch_metadata=(
            BranchMetadata(
                parent_action_id=Identifier(branch_metadata_raw["parent_action_id"]),
                branch_index=branch_metadata_raw["branch_index"],
                terminal_status=branch_metadata_raw["terminal_status"],
            )
            if branch_metadata_raw is not None
            else None
        ),
    )


def read_ledger(ledger_handle: JsonlLedgerHandle) -> list[StateLedgerEntry]:
    """Deserialize every entry currently persisted in the JSONL ledger."""
    if not ledger_handle.canonical_path.exists():
        return []
    return [
        _deserialize_entry(line)
        for line in ledger_handle.canonical_path.read_text().splitlines()
        if line.strip()
    ]


def append_ledger_entry(
    ledger_handle: JsonlLedgerHandle,
    entry_payload: EntryPayload,
    write_key: WriteKey,
) -> WriteResult:
    """Append a hash-chain-preserving entry to the JSONL ledger (C-IS-07 §7.1).

    Append-only — existing entries are never modified. A repeat write whose
    `idempotency_key` already appears in the ledger is an `IDEMPOTENT_NOOP`
    (acceptance #4); the first payload is preserved. `response_hash` and
    `prior_event_hash` are computed internally before persisting (acceptance
    #6/#8). A timestamp earlier than the prior entry's (beyond clock-skew
    tolerance) is rejected (acceptance #9).
    """
    if write_key.idempotency_key != entry_payload.idempotency_key:
        raise WriteKeyMismatchError(
            "write_key.idempotency_key must equal entry_payload.idempotency_key"
        )
    with _WRITE_LOCK:
        ledger = read_ledger(ledger_handle)
        if any(e.idempotency_key == entry_payload.idempotency_key for e in ledger):
            return WriteResult.IDEMPOTENT_NOOP
        prior_entry = ledger[-1] if ledger else None
        if (
            prior_entry is not None
            and entry_payload.timestamp < prior_entry.timestamp - _CLOCK_SKEW_TOLERANCE
        ):
            raise NonMonotonicTimestampError(
                "entry timestamp precedes the prior entry's beyond clock-skew tolerance"
            )
        draft = StateLedgerEntry(
            action_id=entry_payload.action_id,
            idempotency_key=entry_payload.idempotency_key,
            actor=entry_payload.actor,
            response_hash=ALL_ZEROS_SENTINEL,  # placeholder — recomputed below
            timestamp=entry_payload.timestamp,
            prior_event_hash=construct_prior_event_hash(prior_entry),
            # v1.3 NEW sidecar (U-IS-11 v2.4 AC #11/#12/#13/#14). Threaded
            # through from EntryPayload; canonicalize() includes it in the
            # hash recipe when non-None per AC #13.
            procedural_tier_snapshot_ref=entry_payload.procedural_tier_snapshot_ref,
            # v1.8 NEW sidecar (U-IS-19, C-IS-05 §5.4). Threaded through from
            # EntryPayload; canonicalize() includes the nested record in the
            # hash recipe when non-None.
            branch_metadata=entry_payload.branch_metadata,
        )
        entry = draft.model_copy(update={"response_hash": compute_response_hash(draft)})
        with ledger_handle.canonical_path.open("a") as fh:
            fh.write(_serialize_entry(entry) + "\n")
    return WriteResult.APPENDED
