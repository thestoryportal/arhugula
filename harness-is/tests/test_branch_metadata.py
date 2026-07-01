"""Tests for U-IS-19 — `branch_metadata` D-derivative sidecar (C-IS-05 §5.4).

Per Implementation_Plan_Information_Substrate_v2_6.md §2.2 U-IS-19 acceptance
criteria + IS spec v1.8 §5.4. The sidecar is the Route-Y branch-causality +
per-branch terminal-disposition carrier the CP non-linear-topology
`WorkflowDriver` composes. Mirrors the §5.1 `procedural_tier_snapshot_ref`
sidecar test discipline (`test_state_ledger_write_sidecar.py`).

The canonicalization goldens are stored byte values (not current-code-vs-
current-code) so a future canonicalization regression on the nested record is
caught. The `branch_metadata=None` golden is the **pre-v1.8** entry digest
(`29016134…`, pinned at `test_entry_hash.py::test_compute_response_hash_golden`)
— asserting it stays unchanged is the ZERO-breaking-change de-risk.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.entry_hash import canonicalize, compute_response_hash
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    BranchMetadata,
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
from pydantic import ValidationError

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-golden")

# The pre-v1.8 golden digest (test_entry_hash.py::test_compute_response_hash_golden).
_PRE_V1_8_GOLDEN = "29016134db6fb137d57fc6a741cea574d49f92c8a510220a056f0be91f3a0f36"
# Golden for branch_metadata with terminal_status=None (causality only).
_CAUSAL_GOLDEN = "bdc509e92a61783c0912d05c42b355aa824d03ccda9dc5ab14f9829f990b3edc"
# Golden for branch_metadata with terminal_status set.
_TERM_GOLDEN = "b9cb8e7ae84195aa14853e2708fb7a35887da7c052c4b07aa58e97a55649f9b1"


def _golden_entry(branch_metadata: BranchMetadata | None = None) -> StateLedgerEntry:
    """The pinned golden entry; `branch_metadata` defaults to None (pre-v1.8)."""
    return StateLedgerEntry(
        action_id=Identifier("act-golden-001"),
        idempotency_key=Identifier("idem-golden-001"),
        actor=_ACTOR,
        response_hash=b"\xab" * 32,
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        prior_event_hash=ALL_ZEROS_SENTINEL,
        branch_metadata=branch_metadata,
    )


# ---------------------------------------------------------------------------
# BranchMetadata record — construction, frozen, constraints (AC #1, #4).
# ---------------------------------------------------------------------------


def test_branch_metadata_constructs_causality_only() -> None:
    """AC #1: causality-only record (terminal_status defaults None) constructs."""
    bm = BranchMetadata(parent_action_id=Identifier("act-parent-7"), branch_index=0)
    assert bm.parent_action_id == "act-parent-7"
    assert bm.branch_index == 0
    assert bm.terminal_status is None


def test_branch_metadata_is_frozen() -> None:
    """AC #1: the record is frozen (immutable)."""
    bm = BranchMetadata(parent_action_id=Identifier("act-parent-7"), branch_index=1)
    with pytest.raises(ValidationError):
        bm.branch_index = 2  # type: ignore[misc]


def test_branch_metadata_rejects_negative_branch_index() -> None:
    """AC: branch_index ≥ 0 — a negative ordinal is rejected at construction."""
    with pytest.raises(ValidationError):
        BranchMetadata(parent_action_id=Identifier("act-parent-7"), branch_index=-1)


@pytest.mark.parametrize("status", ["cancelled", "completed", "timed_out"])
def test_branch_metadata_accepts_each_terminal_status(status: str) -> None:
    """AC #4: each of the three closed-set values is accepted."""
    bm = BranchMetadata(
        parent_action_id=Identifier("act-parent-7"),
        branch_index=0,
        terminal_status=status,  # type: ignore[arg-type]
    )
    assert bm.terminal_status == status


def test_branch_metadata_rejects_out_of_set_terminal_status() -> None:
    """AC #4: a value outside {cancelled, completed, timed_out} | None is rejected
    (Pydantic Literal) — no `failed` member (dispatch-boundary, not step-outcome)."""
    with pytest.raises(ValidationError):
        BranchMetadata(
            parent_action_id=Identifier("act-parent-7"),
            branch_index=0,
            terminal_status="failed",  # type: ignore[arg-type]
        )


def test_branch_metadata_rejects_extra_field() -> None:
    """The record forbids extra fields (extra='forbid')."""
    with pytest.raises(ValidationError):
        BranchMetadata(
            parent_action_id=Identifier("act-parent-7"),
            branch_index=0,
            branch_path="nope",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Canonicalization — byte-identity (None), include-when-set, 3 nested states.
# ---------------------------------------------------------------------------


def test_canonicalize_branch_metadata_none_byte_identical_to_pre_v1_8() -> None:
    """AC #2: an entry with branch_metadata=None canonicalizes byte-identically
    to the pre-v1.8 golden — ZERO breaking change. The field is omitted from
    the canonical payload when None (no "branch_metadata" key)."""
    entry = _golden_entry(branch_metadata=None)
    assert compute_response_hash(entry).hex() == _PRE_V1_8_GOLDEN
    assert b"branch_metadata" not in canonicalize(entry)


def test_canonicalize_branch_metadata_causality_only_golden() -> None:
    """AC #3: non-None branch_metadata with terminal_status=None pins a stable
    golden (include-as-null nested) and includes the nested record."""
    bm = BranchMetadata(parent_action_id=Identifier("act-parent-7"), branch_index=2)
    entry = _golden_entry(branch_metadata=bm)
    assert compute_response_hash(entry).hex() == _CAUSAL_GOLDEN
    parsed = json.loads(canonicalize(entry))
    assert parsed["branch_metadata"] == {
        "parent_action_id": "act-parent-7",
        "branch_index": 2,
        "terminal_status": None,
    }


def test_canonicalize_branch_metadata_terminal_status_golden() -> None:
    """AC #3: non-None branch_metadata with terminal_status set pins a stable
    golden and renders the value."""
    bm = BranchMetadata(
        parent_action_id=Identifier("act-parent-7"),
        branch_index=2,
        terminal_status="cancelled",
    )
    entry = _golden_entry(branch_metadata=bm)
    assert compute_response_hash(entry).hex() == _TERM_GOLDEN
    parsed = json.loads(canonicalize(entry))
    assert parsed["branch_metadata"]["terminal_status"] == "cancelled"


def test_canonicalize_branch_metadata_three_states_distinct() -> None:
    """AC #3: the three states (None / causality-only / terminal) produce three
    distinct hashes — branch_metadata participates in the §6.2 response_hash."""
    none = compute_response_hash(_golden_entry(branch_metadata=None))
    causal = compute_response_hash(
        _golden_entry(
            branch_metadata=BranchMetadata(
                parent_action_id=Identifier("act-parent-7"), branch_index=2
            )
        )
    )
    term = compute_response_hash(
        _golden_entry(
            branch_metadata=BranchMetadata(
                parent_action_id=Identifier("act-parent-7"),
                branch_index=2,
                terminal_status="cancelled",
            )
        )
    )
    assert len({none, causal, term}) == 3


def test_canonicalize_branch_metadata_nfc_normalized() -> None:
    """AC: nested string sub-fields are NFC-normalized (actor precedent) — NFC
    and NFD forms of parent_action_id canonicalize identically."""
    nfc = unicodedata.normalize("NFC", "act-café-ñ")
    nfd = unicodedata.normalize("NFD", "act-café-ñ")
    assert nfc != nfd
    bm_nfc = BranchMetadata(parent_action_id=Identifier(nfc), branch_index=0)
    bm_nfd = BranchMetadata(parent_action_id=Identifier(nfd), branch_index=0)
    assert canonicalize(_golden_entry(branch_metadata=bm_nfc)) == canonicalize(
        _golden_entry(branch_metadata=bm_nfd)
    )


# ---------------------------------------------------------------------------
# Write / read carrier — EntryPayload field, JSONL omit-when-None, round-trip.
# ---------------------------------------------------------------------------


def _handle(tmp_path: Path) -> JsonlLedgerHandle:
    return JsonlLedgerHandle(
        canonical_path=tmp_path / "state.jsonl",
        exists=False,
        entry_count=0,
    )


def _payload(i: int, branch_metadata: BranchMetadata | None = None) -> EntryPayload:
    return EntryPayload(
        action_id=Identifier(f"act-{i}"),
        idempotency_key=Identifier(f"idem-{i}"),
        actor=_ACTOR,
        timestamp=datetime(2026, 6, 13, max(i, 1), tzinfo=UTC),
        branch_metadata=branch_metadata,
    )


def _key(i: int) -> WriteKey:
    return WriteKey(
        thread_id=Identifier(f"thread-{i}"),
        step_id=Identifier(f"step-{i}"),
        idempotency_key=Identifier(f"idem-{i}"),
    )


def test_entry_payload_defaults_branch_metadata_none() -> None:
    """EntryPayload is constructible without branch_metadata; default None."""
    assert _payload(1).branch_metadata is None


def test_append_omits_branch_metadata_key_when_none(tmp_path: Path) -> None:
    """Persisted JSONL line omits the key entirely when None (compact bytes)."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(1, branch_metadata=None), _key(1))
    raw = json.loads(handle.canonical_path.read_text().splitlines()[0])
    assert "branch_metadata" not in raw


def test_append_persists_branch_metadata_when_set(tmp_path: Path) -> None:
    """Persisted JSONL line includes the nested record when set."""
    handle = _handle(tmp_path)
    bm = BranchMetadata(
        parent_action_id=Identifier("act-1"),
        branch_index=3,
        terminal_status="completed",
    )
    append_ledger_entry(handle, _payload(1, branch_metadata=bm), _key(1))
    raw = json.loads(handle.canonical_path.read_text().splitlines()[0])
    assert raw["branch_metadata"] == {
        "parent_action_id": "act-1",
        "branch_index": 3,
        "terminal_status": "completed",
    }


def test_round_trip_preserves_branch_metadata(tmp_path: Path) -> None:
    """write → read round-trip reconstructs the nested record byte-exact, for
    both the causality-only and terminal states."""
    handle = _handle(tmp_path)
    causal = BranchMetadata(parent_action_id=Identifier("act-p"), branch_index=0)
    term = BranchMetadata(
        parent_action_id=Identifier("act-p"),
        branch_index=1,
        terminal_status="timed_out",
    )
    assert append_ledger_entry(handle, _payload(1, causal), _key(1)) == WriteResult.APPENDED
    assert append_ledger_entry(handle, _payload(2, term), _key(2)) == WriteResult.APPENDED
    entries = read_ledger(handle)
    assert entries[0].branch_metadata == causal
    assert entries[1].branch_metadata == term


def test_chain_verify_intact_with_branch_metadata(tmp_path: Path) -> None:
    """§6.3 chain re-verifies with branch_metadata entries — the sidecar travels
    the canonical payload + response_hash + prior_event_hash links cleanly."""
    handle = _handle(tmp_path)
    append_ledger_entry(handle, _payload(0, branch_metadata=None), _key(0))
    append_ledger_entry(
        handle,
        _payload(1, BranchMetadata(parent_action_id=Identifier("act-0"), branch_index=0)),
        _key(1),
    )
    append_ledger_entry(
        handle,
        _payload(
            2,
            BranchMetadata(
                parent_action_id=Identifier("act-0"),
                branch_index=1,
                terminal_status="cancelled",
            ),
        ),
        _key(2),
    )
    result = verify_chain(read_ledger(handle))
    assert result.status == VerificationStatus.VALID
    assert result.entries_verified == 3
