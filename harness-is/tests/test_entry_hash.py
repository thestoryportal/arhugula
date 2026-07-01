"""Tests for U-IS-08 — canonicalization + per-entry SHA-256 hash (C-IS-06 §6.1/§6.2).

Test set per the U-IS-08 `Tests:` field — covers acceptance #1-#6.

`test_canonicalize_library_binding_flex` is realized as a scheme-stability
check: the §6.1 RFC 8785 library binding is spec-deferred to a D-ADR that has
not landed (no external JCS library is pulled — framework-pull discipline), so
"two distinct bindings agree" is exercised as "the `canonicalize` scheme agrees
with an independent recomputation of the same scheme."
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime

from harness_is.entry_hash import canonicalize, compute_response_hash
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)


def _entry(
    action_id: str = "act-1",
    idempotency_key: str = "idem-1",
    actor_id: str = "agent-1",
) -> StateLedgerEntry:
    return StateLedgerEntry(
        action_id=Identifier(action_id),
        idempotency_key=Identifier(idempotency_key),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id=actor_id),
        response_hash=b"\xab" * 32,
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
    )


def test_canonicalize_deterministic_same_invocation() -> None:
    """Acceptance #1 — two canonicalizations of one entry are byte-equal."""
    entry = _entry()
    assert canonicalize(entry) == canonicalize(entry)


def test_canonicalize_field_order_insensitive() -> None:
    """Acceptance #2 — entries built with kwargs in different order canonicalize
    identically (sorted keys)."""
    a = StateLedgerEntry(
        action_id=Identifier("act-1"),
        idempotency_key=Identifier("idem-1"),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="agent-1"),
        response_hash=b"\xab" * 32,
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
    )
    b = StateLedgerEntry(
        prior_event_hash=ALL_ZEROS_SENTINEL,
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        response_hash=b"\xab" * 32,
        actor=Actor(actor_id="agent-1", actor_class=ActorClass.AGENT),
        idempotency_key=Identifier("idem-1"),
        action_id=Identifier("act-1"),
    )
    assert canonicalize(a) == canonicalize(b)


def test_canonicalize_unicode_normalization() -> None:
    """Acceptance #2 — NFC and NFD forms of one string canonicalize identically."""
    nfc = unicodedata.normalize("NFC", "café-ñ")
    nfd = unicodedata.normalize("NFD", "café-ñ")
    assert nfc != nfd  # distinct byte sequences in memory
    assert canonicalize(_entry(actor_id=nfc)) == canonicalize(_entry(actor_id=nfd))


def test_canonicalize_number_representation() -> None:
    """Acceptance #2 — the RFC 8785 number-canonicalization concern is
    float-specific (`1.0` vs `1`); `StateLedgerEntry` carries no float field, so
    no such divergence is reachable through `canonicalize`. The six F-layer
    fields all serialize as JSON strings; the v1.8 §5.4 `branch_metadata`
    sidecar introduces one integer (`branch_index`), which serializes
    deterministically — not a float."""
    parsed = json.loads(canonicalize(_entry()))
    for key, value in parsed.items():
        if key == "actor":
            assert all(isinstance(v, str) for v in value.values())
        else:
            assert isinstance(value, str)

    # With a §5.4 branch_metadata sidecar, the only non-string scalar in the
    # canonical payload is the integer branch_index; assert no float is
    # reachable anywhere (the RFC 8785 ambiguity is float-only).
    from harness_is.state_ledger_entry_schema import BranchMetadata

    entry_bm = _entry().model_copy(
        update={
            "branch_metadata": BranchMetadata(
                parent_action_id=Identifier("act-parent"),
                branch_index=3,
                terminal_status="completed",
            )
        }
    )
    bm = json.loads(canonicalize(entry_bm))["branch_metadata"]
    assert isinstance(bm["branch_index"], int) and not isinstance(bm["branch_index"], bool)

    def _assert_no_float(obj: object) -> None:
        assert not isinstance(obj, float)
        if isinstance(obj, dict):
            for v in obj.values():
                _assert_no_float(v)

    _assert_no_float(json.loads(canonicalize(entry_bm)))


def test_compute_response_hash_length() -> None:
    """Acceptance #4 — compute_response_hash output is exactly 32 bytes."""
    assert len(compute_response_hash(_entry())) == 32


def test_compute_response_hash_golden() -> None:
    """Acceptance #3 — a fixture entry produces its pinned SHA-256 digest."""
    golden = _entry(
        action_id="act-golden-001",
        idempotency_key="idem-golden-001",
        actor_id="agent-golden",
    )
    assert (
        compute_response_hash(golden).hex()
        == "29016134db6fb137d57fc6a741cea574d49f92c8a510220a056f0be91f3a0f36"
    )


def test_compute_response_hash_collision_smoke() -> None:
    """Acceptance #3 — 1000 distinct entries produce 1000 distinct hashes."""
    hashes = {compute_response_hash(_entry(action_id=f"act-{i}")) for i in range(1000)}
    assert len(hashes) == 1000


def test_canonicalize_library_binding_flex() -> None:
    """Acceptance #1/#5 — the canonicalization scheme is binding-stable: an
    independent recomputation of the same scheme is byte-equal to `canonicalize`
    (the §6.1 external-JCS-library binding is D-ADR-deferred / not pulled)."""
    entry = _entry()
    independent = json.dumps(
        {
            "action_id": unicodedata.normalize("NFC", entry.action_id),
            "idempotency_key": unicodedata.normalize("NFC", entry.idempotency_key),
            "actor": {
                "actor_class": unicodedata.normalize("NFC", entry.actor.actor_class.value),
                "actor_id": unicodedata.normalize("NFC", entry.actor.actor_id),
            },
            "timestamp": entry.timestamp.isoformat(),
            "prior_event_hash": entry.prior_event_hash.hex(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert canonicalize(entry) == independent
    assert compute_response_hash(entry) == hashlib.sha256(independent).digest()
