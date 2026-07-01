"""Tests for U-CP-63 — C-CP-26 PauseResumeProtocol.capture_pause_snapshot().

ACs from CP plan v2.17 §2 U-CP-63 (v2.17 amendment — AC #4 line-121 closure
STRUCK; AC #5 renumbered to AC #4 per CP spec v1.11 §26 NEW NOTE coexistence):
  AC #1 Snapshot computes snapshot_hash via canonical serialization of
        (workflow_id + run_id + step_index + state_summary)
  AC #2 Snapshot immutable after capture (frozen Pydantic model)
  AC #3 State-ledger anchor populated with current entry_hash from
        ctx.state_ledger_writer (via pause_context_reader FACTOR-OUT)
  AC #4 (v2.17 renumber, was AC #5 at v2.15) Unit test: capture + verify
        hash + verify immutability
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import PauseResumeProtocol
from harness_cp.pause_resume_protocol_types import (
    PauseSnapshot,
    WorkflowPauseReason,
)
from harness_is.state_ledger_entry_schema import Identifier
from pydantic import ValidationError


def _build_state_summary(summary_text: str = "test-state") -> StateSummary:
    """Construct a minimal StateSummary for U-CP-63 capture tests."""
    return StateSummary(
        relevant_entries=(),
        summary_text=summary_text,
        summary_hash="0" * 64,
        idempotency_key=Identifier("idem-1"),
        external_references=(),
    )


def _build_protocol(
    *, state_summary: StateSummary | None = None, anchor: str = "a" * 64
) -> PauseResumeProtocol:
    """Construct a PauseResumeProtocol with stub state-ledger refs + reader."""
    summary = state_summary if state_summary is not None else _build_state_summary()
    return PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, anchor),
    )


# --- AC #1 — snapshot_hash composition -------------------------------------


def test_capture_pause_snapshot_returns_pause_snapshot() -> None:
    """Sanity — capture returns a PauseSnapshot instance."""
    protocol = _build_protocol()
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    assert isinstance(snapshot, PauseSnapshot)


def test_capture_pause_snapshot_hash_is_sha256_hex_64() -> None:
    """AC #1 — snapshot_hash is a 64-char sha256 hex value."""
    protocol = _build_protocol()
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    assert len(snapshot.snapshot_hash) == 64
    # All chars are hex
    int(snapshot.snapshot_hash, 16)


def test_capture_pause_snapshot_hash_matches_canonical_serialization() -> None:
    """AC #1 — snapshot_hash = sha256(canonical_json(workflow_id, run_id, step_index, state_summary))."""
    state_summary = _build_state_summary(summary_text="deterministic")
    protocol = _build_protocol(state_summary=state_summary)
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=3,
            pause_reason=WorkflowPauseReason.HITL_PENDING,
        )
    )
    expected_payload = json.dumps(
        {
            "workflow_id": "wf-1",
            "run_id": "run-1",
            "step_index": 3,
            "state_summary": state_summary.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    expected_hash = hashlib.sha256(expected_payload).hexdigest()
    assert snapshot.snapshot_hash == expected_hash


def test_capture_pause_snapshot_hash_deterministic() -> None:
    """AC #1 — equal inputs yield equal hashes (deterministic canonical serialization)."""
    state_summary = _build_state_summary()
    protocol_a = _build_protocol(state_summary=state_summary)
    protocol_b = _build_protocol(state_summary=state_summary)

    snapshot_a = asyncio.run(
        protocol_a.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    snapshot_b = asyncio.run(
        protocol_b.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    assert snapshot_a.snapshot_hash == snapshot_b.snapshot_hash


def test_capture_pause_snapshot_hash_changes_with_step_index() -> None:
    """AC #1 — different step_index yields different snapshot_hash."""
    state_summary = _build_state_summary()
    protocol = _build_protocol(state_summary=state_summary)

    snap_0 = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    snap_1 = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=1,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    assert snap_0.snapshot_hash != snap_1.snapshot_hash


# --- AC #2 — immutability --------------------------------------------------


def test_pause_snapshot_immutable_after_capture() -> None:
    """AC #2 — snapshot frozen after capture; mutation raises (§26.6 invariant 1)."""
    protocol = _build_protocol()
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    with pytest.raises(ValidationError):
        snapshot.workflow_id = "wf-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.snapshot_hash = "x" * 64  # type: ignore[misc]


# --- AC #3 — state_ledger_anchor populated ---------------------------------


def test_capture_populates_state_ledger_anchor_from_reader() -> None:
    """AC #3 — state_ledger_anchor field populated from pause_context_reader."""
    expected_anchor = "f" * 64
    protocol = _build_protocol(anchor=expected_anchor)
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    assert snapshot.state_ledger_anchor == expected_anchor


def test_capture_invokes_pause_context_reader_each_call() -> None:
    """AC #3 — reader invoked per capture call (not memoized across calls).

    Verifies that ledger advancement between captures surfaces in the
    state_ledger_anchor field. Material-diff detection at U-CP-64 relies on
    captures reading the current anchor at capture time, not a stale value.
    """
    invocations: list[int] = []
    anchors = ["aa" * 32, "bb" * 32]
    state_summary = _build_state_summary()

    def reader() -> tuple[StateSummary, str]:
        i = len(invocations)
        invocations.append(i)
        return state_summary, anchors[i]

    protocol = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=reader,
    )

    snap_0 = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.HITL_PENDING,
        )
    )
    snap_1 = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=1,
            pause_reason=WorkflowPauseReason.HITL_PENDING,
        )
    )
    assert snap_0.state_ledger_anchor == anchors[0]
    assert snap_1.state_ledger_anchor == anchors[1]
    assert len(invocations) == 2


# --- AC #4 (v2.17 renumber) — capture + verify hash + verify immutability --


def test_ac4_capture_then_verify_hash_then_verify_immutability() -> None:
    """AC #4 (v2.17 renumber; was AC #5 at v2.15) — composite test capturing
    the U-CP-63 happy path: capture a snapshot, verify the hash matches the
    canonical-serialization recipe, then verify the snapshot is immutable.
    """
    state_summary = _build_state_summary(summary_text="ac4-composite")
    protocol = _build_protocol(state_summary=state_summary, anchor="c" * 64)

    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-ac4",
            run_id="run-ac4",
            step_index=7,
            pause_reason=WorkflowPauseReason.VALIDATOR_ESCALATION,
        )
    )

    # Verify hash recipe
    expected_payload = json.dumps(
        {
            "workflow_id": "wf-ac4",
            "run_id": "run-ac4",
            "step_index": 7,
            "state_summary": state_summary.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert snapshot.snapshot_hash == hashlib.sha256(expected_payload).hexdigest()

    # Verify all 8 fields populated correctly
    assert snapshot.workflow_id == "wf-ac4"
    assert snapshot.run_id == "run-ac4"
    assert snapshot.step_index == 7
    assert snapshot.pause_reason == WorkflowPauseReason.VALIDATOR_ESCALATION
    assert snapshot.state_summary is state_summary
    assert snapshot.state_ledger_anchor == "c" * 64
    assert isinstance(snapshot.created_at, int)
    assert snapshot.created_at > 0

    # Verify immutability
    with pytest.raises(ValidationError):
        snapshot.pause_reason = WorkflowPauseReason.EXPLICIT_OPERATOR  # type: ignore[misc]


# --- Coexistence regression: U-CP-49 free-function surface preserved -------


def test_u_cp_49_free_function_capture_pause_snapshot_requires_engine_substrate() -> None:
    """Coexistence regression: CP spec v1.11 §26 NEW NOTE — the OLD
    U-CP-49 free-function `capture_pause_snapshot` remains distinct from
    the C-CP-26 workflow-layer class and fails closed when no engine substrate
    is bound. Per the v2.17 plan AC #4 strike: line 121 is NOT closed by
    U-CP-63.
    """
    from harness_core import WorkflowID
    from harness_cp.pause_resume_protocol import (
        PauseReason as EngineLayerPauseReason,
    )
    from harness_cp.pause_resume_protocol import (
        capture_pause_snapshot as engine_layer_capture,
    )

    with pytest.raises(NotImplementedError):
        engine_layer_capture(
            WorkflowID("wf-old"),
            EngineLayerPauseReason.OPERATOR_INITIATED_PAUSE,
        )


def test_workflow_layer_protocol_distinct_from_engine_layer_free_functions() -> None:
    """Coexistence regression: PauseResumeProtocol class is distinct from
    the OLD free-function surface — different scope, different signatures."""
    from harness_cp.pause_resume_protocol import (
        capture_pause_snapshot as engine_layer_capture,
    )

    # Engine-layer is a free function; workflow-layer is a class
    assert callable(engine_layer_capture)
    assert not isinstance(engine_layer_capture, type)
    assert isinstance(PauseResumeProtocol, type)
