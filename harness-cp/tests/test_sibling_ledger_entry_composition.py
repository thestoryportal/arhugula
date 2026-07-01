"""Tests for U-CP-34 — per-sibling F2 ledger entry composition (C-CP-15 §15.1/§15.3).

Acceptance-criterion coverage:
  #1 SiblingLedgerEntry matches F2 shape -> test_sibling_ledger_entry_matches_f2_shape
  #2 idempotency_key Stripe-style        -> test_idempotency_key_construction
  #3 action_id structural concatenation  -> test_action_id_concatenation
  #4 F2-14 rationale 3 entries           -> test_f2_14_rationale_cardinality_three,
                                            test_f2_14_rationale_per_field_match_spec
  #5 append delegates to U-IS-11         -> test_append_delegates_to_u_is_11
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.sibling_ledger_entry_composition import (
    F2_14_READING_1_RATIONALE,
    SiblingLedgerEntry,
    append_sibling_ledger_entry,
    construct_sibling_ledger_entry,
)
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import Identifier, StateLedgerEntry
from harness_is.state_ledger_write import WriteKey, WriteResult


def test_sibling_ledger_entry_matches_f2_shape() -> None:
    """#1 — SiblingLedgerEntry inherits the F2 shape; the D-derivative sidecars are admissible.

    Per IS spec v1.3 §5.1 (LANDED PR #89): `StateLedgerEntry` gains the
    `procedural_tier_snapshot_ref` D-derivative sidecar; per IS spec v1.8 §5.4
    (U-IS-19): it gains the `branch_metadata` D-derivative sidecar — both
    additive to the F-layer 6-field shape. `SiblingLedgerEntry` inherits the
    extended field set verbatim per Pydantic v2 subclass semantics.
    """
    assert issubclass(SiblingLedgerEntry, StateLedgerEntry)
    assert set(SiblingLedgerEntry.model_fields) == {
        "action_id",
        "idempotency_key",
        "actor",
        "response_hash",
        "timestamp",
        "prior_event_hash",
        "procedural_tier_snapshot_ref",
        "branch_metadata",
    }


def test_idempotency_key_construction() -> None:
    """#2 — idempotency_key is the Stripe-style sha256 over the 5-tuple."""
    payload = construct_sibling_ledger_entry(
        parent_action_id="P",
        sibling_thread_id="T",
        step_index=2,
        tool="read",
        canonical_args="{}",
        sibling_agent_identity=ActorIdentity("sib-1"),
        timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
    )
    expected = hashlib.sha256("\x1f".join(("P", "T", "2", "read", "{}")).encode()).hexdigest()
    assert payload.idempotency_key == expected


def test_action_id_concatenation() -> None:
    """#3 — action_id is ParentActionID || sibling_thread_id || step_index."""
    payload = construct_sibling_ledger_entry(
        parent_action_id="P",
        sibling_thread_id="T",
        step_index=2,
        tool="read",
        canonical_args="{}",
        sibling_agent_identity=ActorIdentity("sib-1"),
        timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
    )
    assert payload.action_id == "PT2"
    assert payload.actor.actor_id == "sib-1"


def test_procedural_tier_snapshot_ref_defaults_none() -> None:
    """R-003 — the sidecar defaults to None when the caller omits it
    (outside-workflow / pre-R-003 callers keep the prior behavior)."""
    payload = construct_sibling_ledger_entry(
        parent_action_id="P",
        sibling_thread_id="T",
        step_index=0,
        tool="read",
        canonical_args="{}",
        sibling_agent_identity=ActorIdentity("sib-1"),
        timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
    )
    assert payload.procedural_tier_snapshot_ref is None


def test_procedural_tier_snapshot_ref_populated_when_supplied() -> None:
    """R-003 — when the caller (the runtime cp_is_wiring) supplies a resolved
    Identifier, it lands on the EntryPayload sidecar per IS spec v1.3 §5.1."""
    payload = construct_sibling_ledger_entry(
        parent_action_id="P",
        sibling_thread_id="T",
        step_index=0,
        tool="read",
        canonical_args="{}",
        sibling_agent_identity=ActorIdentity("sib-1"),
        timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
        procedural_tier_snapshot_ref=Identifier("b" * 64),
    )
    assert payload.procedural_tier_snapshot_ref == Identifier("b" * 64)


def test_f2_14_rationale_cardinality_three() -> None:
    """#4 — exactly three F2-14 Reading 1 rationale rows."""
    assert len(F2_14_READING_1_RATIONALE) == 3


def test_f2_14_rationale_per_field_match_spec() -> None:
    """#4 — one rationale per omitted F2 field at parent_fanout_close_entry."""
    omitted = {r.omitted_field for r in F2_14_READING_1_RATIONALE}
    assert omitted == {"idempotency_key", "actor", "response_hash"}
    for r in F2_14_READING_1_RATIONALE:
        assert r.primitive_name == "parent_fanout_close_entry"
        assert r.omission_rationale


def test_append_delegates_to_u_is_11(tmp_path: Path) -> None:
    """#5 — append_sibling_ledger_entry delegates to U-IS-11's append write."""
    payload = construct_sibling_ledger_entry(
        parent_action_id="P",
        sibling_thread_id="T",
        step_index=0,
        tool="read",
        canonical_args="{}",
        sibling_agent_identity=ActorIdentity("sib-1"),
        timestamp=datetime(2026, 5, 16, 1, tzinfo=UTC),
    )
    handle = JsonlLedgerHandle(canonical_path=tmp_path / "state.jsonl", exists=False, entry_count=0)
    write_key = WriteKey(
        thread_id=Identifier("T"),
        step_id=Identifier("0"),
        idempotency_key=payload.idempotency_key,
    )
    result = append_sibling_ledger_entry(handle, payload, write_key)
    assert result is WriteResult.APPENDED
    assert len(handle.canonical_path.read_text().splitlines()) == 1
