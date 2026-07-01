"""State-ledger entry canonicalization + per-entry SHA-256 hash — U-IS-08.

Implements C-IS-06 §6.1 (per-entry canonicalization) + §6.2 (per-entry hash
computation). Declares `canonicalize` and `compute_response_hash` — the
write-time primitives the hash-chain (U-IS-09) and verification (U-IS-10) build
on.

**Canonicalization scope.** `canonicalize` produces the deterministic byte
representation of the entry's *signable payload* — the five fields other than
`response_hash`. `response_hash` is excluded because it is the field being
computed: `compute_response_hash = SHA-256(canonicalize(entry))` would be
circular otherwise. This is the operative state-ledger recipe already in use
(the `.harness/state.jsonl` chain): an entry's `response_hash` is the SHA-256
of its response-hash-excluded canonical form, and the next entry's
`prior_event_hash` is the prior entry's `response_hash` (C-IS-06 §6.3).

**Canonicalization scheme — implementation discretion (C-IS-06 §6.1).** §6.1
marks the RFC 8785 JCS library binding `[MODERATE — to be confirmed at the
D-ADR on canonicalization library]`; that D-ADR has not landed, and
`CLAUDE.md` §3.2 framework-pull discipline (I-6) precludes pulling a JCS
framework where the existing stack suffices. The scheme here is hand-rolled on
the stdlib: NFC Unicode normalization of every string value + `json.dumps`
with sorted keys and no whitespace. For the `StateLedgerEntry` data shape
(strings, a `StrEnum`, a `datetime`, byte digests, and — since v1.8 §5.4 — one
**integer**, `branch_metadata.branch_index`; **no float fields**) this is
RFC 8785 JCS-conformant: the one RFC 8785 property the stdlib `json` does not
guarantee is ECMAScript number serialization, which is a **float**-specific
ambiguity (`1.0` vs `1`). Integers serialize deterministically under stdlib
`json`, and the entry shape carries no float field — so the property holds. The
scheme is encapsulated behind the single `canonicalize` boundary (acceptance
#5 — one swappable binding site).

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.1 U-IS-08
(preserved verbatim from v2.1 §2); Spec_Information_Substrate_v1.md C-IS-06
§6.1 / §6.2; ADR-F2 v1.2 §Rationale (a.1).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

from harness_is.state_ledger_entry_schema import Bytes32, StateLedgerEntry


def _nfc(value: str) -> str:
    """NFC-normalize a string (RFC 8785 JCS Unicode normalization)."""
    return unicodedata.normalize("NFC", value)


def canonicalize(entry: StateLedgerEntry) -> bytes:
    """Canonicalize a state-ledger entry to deterministic bytes (C-IS-06 §6.1).

    Operates on the entry's signable payload — every field except
    `response_hash`. Deterministic: byte-identical output for logically-equal
    entries across runs / machines (acceptance #1); field-order-insensitive
    (sorted keys), Unicode-normalized (NFC), number-representation-canonical
    (no float fields — vacuous) (acceptance #2).

    v1.3 NEW D-derivative sidecar contribution (U-IS-11 v2.4 AC #13):
    ``procedural_tier_snapshot_ref`` is included in the canonical payload
    when non-``None``; omitted when ``None``. Legacy entries with no sidecar
    field (pre-v1.3) hash identically to v1.3 entries with sidecar ``None`` —
    ZERO breaking change at the hash level for the existing chain.

    v1.8 NEW D-derivative sidecar contribution (U-IS-19, C-IS-05 §5.4):
    ``branch_metadata`` is included as a nested record when non-``None``;
    omitted when ``None`` (the same omit-when-``None`` discipline). The nested
    record follows the ``actor`` nested-record precedent (§5.4) — built by hand
    with NFC normalization on every string sub-field; ``branch_index`` is an
    int; ``terminal_status`` renders its value (NFC) when set and JSON ``null``
    when ``None`` (include-as-null — the record's fields are rendered whenever
    the record is present, mirroring ``actor``). Every pre-v1.8 entry carries
    ``branch_metadata = None`` ⟹ byte-identical canonicalization.
    """
    payload: dict[str, object] = {
        "action_id": _nfc(entry.action_id),
        "idempotency_key": _nfc(entry.idempotency_key),
        "actor": {
            "actor_class": _nfc(entry.actor.actor_class.value),
            "actor_id": _nfc(entry.actor.actor_id),
        },
        "timestamp": entry.timestamp.isoformat(),
        "prior_event_hash": entry.prior_event_hash.hex(),
    }
    if entry.procedural_tier_snapshot_ref is not None:
        payload["procedural_tier_snapshot_ref"] = _nfc(
            entry.procedural_tier_snapshot_ref,
        )
    if entry.branch_metadata is not None:
        bm = entry.branch_metadata
        payload["branch_metadata"] = {
            "parent_action_id": _nfc(bm.parent_action_id),
            "branch_index": bm.branch_index,
            "terminal_status": (
                _nfc(bm.terminal_status) if bm.terminal_status is not None else None
            ),
        }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_response_hash(entry: StateLedgerEntry) -> Bytes32:
    """Compute the entry's `response_hash` — `SHA-256(canonicalize(entry))`
    (C-IS-06 §6.2). Output is exactly 32 bytes (acceptance #3/#4)."""
    return hashlib.sha256(canonicalize(entry)).digest()
