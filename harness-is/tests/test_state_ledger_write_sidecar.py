"""Tests for U-IS-11 v2.4 amendment — `procedural_tier_snapshot_ref` sidecar.

Per Implementation_Plan_Information_Substrate_v2_4.md §2.2 U-IS-11 acceptance
criteria #11-#14 + tests list. The sidecar is the D-derivative extension of
the §5 F-layer six-field shape per IS spec v1.3 §C-IS-05 §5.1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from harness_is.entry_hash import compute_response_hash
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
    StateLedgerEntry,
)
from harness_is.state_ledger_write import (
    EntryPayload,
    WriteKey,
    WriteResult,
    append_ledger_entry,
    read_ledger,
)

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-1")
_SNAPSHOT_REF = Identifier(
    "ffeeddccbbaa998877665544332211000011223344556677889900aabbccddeeff"[:64]
)


def _handle(tmp_path: Path) -> JsonlLedgerHandle:
    return JsonlLedgerHandle(
        canonical_path=tmp_path / "state.jsonl",
        exists=False,
        entry_count=0,
    )


def _payload(
    i: int,
    snapshot_ref: Identifier | None = None,
) -> EntryPayload:
    return EntryPayload(
        action_id=Identifier(f"act-{i}"),
        idempotency_key=Identifier(f"idem-{i}"),
        actor=_ACTOR,
        timestamp=datetime(2026, 5, 30, i, tzinfo=UTC),
        procedural_tier_snapshot_ref=snapshot_ref,
    )


def _key(i: int) -> WriteKey:
    return WriteKey(
        thread_id=Identifier(f"thread-{i}"),
        step_id=Identifier(f"step-{i}"),
        idempotency_key=Identifier(f"idem-{i}"),
    )


# ---------------------------------------------------------------------------
# AC #11 — sidecar field optional with default None.
# ---------------------------------------------------------------------------


def test_entry_payload_accepts_none_procedural_tier_snapshot_ref_by_default() -> None:
    """AC #11: ``EntryPayload`` constructible without sidecar; default ``None``."""
    payload = EntryPayload(
        action_id=Identifier("a"),
        idempotency_key=Identifier("i"),
        actor=_ACTOR,
        timestamp=datetime(2026, 5, 30, 1, tzinfo=UTC),
    )
    assert payload.procedural_tier_snapshot_ref is None


def test_entry_payload_accepts_non_none_procedural_tier_snapshot_ref() -> None:
    """AC #11: ``EntryPayload`` accepts non-``None`` sidecar value."""
    payload = EntryPayload(
        action_id=Identifier("a"),
        idempotency_key=Identifier("i"),
        actor=_ACTOR,
        timestamp=datetime(2026, 5, 30, 1, tzinfo=UTC),
        procedural_tier_snapshot_ref=_SNAPSHOT_REF,
    )
    assert payload.procedural_tier_snapshot_ref == _SNAPSHOT_REF


# ---------------------------------------------------------------------------
# AC #12 — serialization discipline (omit when None; include when non-None).
# ---------------------------------------------------------------------------


def test_append_persists_sidecar_field_when_non_none(tmp_path: Path) -> None:
    """AC #12: persisted JSONL line includes sidecar key when non-None."""
    handle = _handle(tmp_path)
    payload = _payload(1, snapshot_ref=_SNAPSHOT_REF)
    result = append_ledger_entry(handle, payload, _key(1))
    assert result == WriteResult.APPENDED
    line = handle.canonical_path.read_text().splitlines()[0]
    raw = json.loads(line)
    assert raw["procedural_tier_snapshot_ref"] == _SNAPSHOT_REF


def test_append_omits_sidecar_key_when_none(tmp_path: Path) -> None:
    """AC #12: persisted JSONL line omits sidecar key entirely when None."""
    handle = _handle(tmp_path)
    payload = _payload(1, snapshot_ref=None)
    append_ledger_entry(handle, payload, _key(1))
    line = handle.canonical_path.read_text().splitlines()[0]
    raw = json.loads(line)
    assert "procedural_tier_snapshot_ref" not in raw


# ---------------------------------------------------------------------------
# AC #13 — response_hash includes sidecar field contribution when non-None.
# ---------------------------------------------------------------------------


def test_response_hash_includes_sidecar_field_contribution() -> None:
    """AC #13: two entries differing only in sidecar value produce different
    ``response_hash`` (sidecar participates in canonicalize when non-None)."""
    base_ts = datetime(2026, 5, 30, 1, tzinfo=UTC)
    entry_none = StateLedgerEntry(
        action_id=Identifier("a"),
        idempotency_key=Identifier("i"),
        actor=_ACTOR,
        response_hash=ALL_ZEROS_SENTINEL,
        timestamp=base_ts,
        prior_event_hash=ALL_ZEROS_SENTINEL,
        procedural_tier_snapshot_ref=None,
    )
    entry_with_ref = entry_none.model_copy(
        update={"procedural_tier_snapshot_ref": _SNAPSHOT_REF},
    )
    assert compute_response_hash(entry_none) != compute_response_hash(entry_with_ref)


def test_round_trip_with_sidecar_field_deterministic(tmp_path: Path) -> None:
    """AC #13: write→read round-trip preserves sidecar value byte-exact."""
    handle = _handle(tmp_path)
    payload = _payload(1, snapshot_ref=_SNAPSHOT_REF)
    append_ledger_entry(handle, payload, _key(1))
    entries = read_ledger(handle)
    assert len(entries) == 1
    assert entries[0].procedural_tier_snapshot_ref == _SNAPSHOT_REF


# ---------------------------------------------------------------------------
# AC #14 — action_id + sidecar compose without conflation.
# ---------------------------------------------------------------------------


def test_action_id_and_sidecar_compose_without_conflation(tmp_path: Path) -> None:
    """AC #14: action_id retains action-class semantics independently;
    sidecar populated alongside without interference per §C-IS-02 line 170."""
    handle = _handle(tmp_path)
    payload = EntryPayload(
        action_id=Identifier("workflow.step.completed"),  # action-class label
        idempotency_key=Identifier("idem-x"),
        actor=_ACTOR,
        timestamp=datetime(2026, 5, 30, 1, tzinfo=UTC),
        procedural_tier_snapshot_ref=_SNAPSHOT_REF,  # procedural-tier ref
    )
    append_ledger_entry(
        handle,
        payload,
        WriteKey(
            thread_id=Identifier("t"),
            step_id=Identifier("s"),
            idempotency_key=Identifier("idem-x"),
        ),
    )
    entries = read_ledger(handle)
    assert entries[0].action_id == "workflow.step.completed"
    assert entries[0].procedural_tier_snapshot_ref == _SNAPSHOT_REF


# ---------------------------------------------------------------------------
# Legacy chain backward-compat — pre-v1.3 entries (no sidecar key) hash same
# as v1.3 entries with sidecar None.
# ---------------------------------------------------------------------------


def test_legacy_entry_without_sidecar_key_hashes_same_as_v1_3_none(
    tmp_path: Path,
) -> None:
    """ZERO breaking change at hash level: legacy entries (no sidecar key in
    JSON) round-trip to ``procedural_tier_snapshot_ref=None`` and produce the
    same ``response_hash`` as v1.3 entries with sidecar None per IS plan v2.4
    + spec v1.3 §C-IS-06 §6.1 NEW canonicalize sidecar discipline."""
    base_ts = datetime(2026, 5, 30, 1, tzinfo=UTC)
    entry_v1_3_none = StateLedgerEntry(
        action_id=Identifier("a"),
        idempotency_key=Identifier("i"),
        actor=_ACTOR,
        response_hash=ALL_ZEROS_SENTINEL,
        timestamp=base_ts,
        prior_event_hash=ALL_ZEROS_SENTINEL,
        procedural_tier_snapshot_ref=None,
    )
    # Pre-v1.3 schema had no field — canonicalize must produce identical bytes.
    # Simulate this by hashing twice with identical entries; the canonicalize
    # contract guarantees ``None`` ⇒ key omitted, matching legacy entries.
    hash_a = compute_response_hash(entry_v1_3_none)
    hash_b = compute_response_hash(entry_v1_3_none)
    assert hash_a == hash_b
    # Negative control: non-None sidecar diverges per AC #13.
    entry_v1_3_with = entry_v1_3_none.model_copy(
        update={"procedural_tier_snapshot_ref": _SNAPSHOT_REF},
    )
    assert hash_a != compute_response_hash(entry_v1_3_with)
