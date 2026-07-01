"""Tests for U-CP-76 emit_pause_resume_state_ledger_entry workflow-layer composer.

CP spec v1.26 §16.5 row U-CP-30.
"""

from __future__ import annotations

import asyncio
import hashlib
from enum import StrEnum
from typing import Any

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.pause_resume_protocol import (
    PauseResumeProtocolEventKind,
    emit_pause_resume_state_ledger_entry,
)
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from harness_is.state_ledger_entry_schema import ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult

_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)


def _pt_resolver() -> Identifier:
    """CP spec v1.30 §1.4: zero-arg resolver closure returning the fixture."""
    return _PROCEDURAL_TIER_SNAPSHOT_FIXTURE


class _CapturingLedgerWriter:
    """Async ledger_writer stub capturing payloads for verification."""

    def __init__(self, returns: WriteResult = WriteResult.APPENDED) -> None:
        self.captured: list[EntryPayload] = []
        self._returns = returns

    async def __call__(self, payload: EntryPayload) -> WriteResult:
        self.captured.append(payload)
        return self._returns


def _snapshot() -> dict[str, Any]:
    return {"step_index": 7, "snapshot_hash": "deadbeef" * 8}


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf-1",
        "step_id": "step-2",
        "protocol_event_kind": PauseResumeProtocolEventKind.PAUSE_CAPTURED,
        "event_sequence_id": 42,
        "protocol_state_snapshot": _snapshot(),
        "actor": ActorIdentity("control-plane"),
        "procedural_tier_snapshot_resolver": _pt_resolver,
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- AC #1 ---


def test_emit_pause_resume_workflow_layer_action_id() -> None:
    """action_id is the canonical kebab-case identifier per spec v1.26 §16.5.3 row U-CP-30."""
    writer = _CapturingLedgerWriter()
    _run(emit_pause_resume_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].action_id == "cp.pause-resume-protocol"


# --- AC #2 ---


def test_emit_pause_resume_idempotency_key_per_q_beta_i_1a() -> None:
    """idempotency_key bytes follow §16.5.4 row U-CP-30 5-tuple (v1.26 with outcome-hash suffix)."""
    snapshot = _snapshot()
    outcome_hash = hashlib.sha256(_canonicalize_outcome_bytes(snapshot)).hexdigest()
    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-2",
                PauseResumeProtocolEventKind.PAUSE_CAPTURED.value.encode("utf-8"),
                b"42",
                outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(
        emit_pause_resume_state_ledger_entry(
            **_kwargs(protocol_state_snapshot=snapshot), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == expected


def test_emit_pause_resume_idempotency_key_includes_event_kind() -> None:
    """Different protocol_event_kind at otherwise-identical inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_pause_resume_state_ledger_entry(
            **_kwargs(protocol_event_kind=PauseResumeProtocolEventKind.PAUSE_CAPTURED),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_pause_resume_state_ledger_entry(
            **_kwargs(protocol_event_kind=PauseResumeProtocolEventKind.RESUME_ATTEMPTED),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_pause_resume_idempotency_key_includes_event_sequence_id() -> None:
    """Different event_sequence_id at otherwise-identical inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_pause_resume_state_ledger_entry(**_kwargs(event_sequence_id=1), ledger_writer=writer_a)
    )
    _run(
        emit_pause_resume_state_ledger_entry(**_kwargs(event_sequence_id=2), ledger_writer=writer_b)
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_pause_resume_idempotency_key_includes_outcome_hash_suffix() -> None:
    """Q-β.i-1(a): different snapshot at same disambiguator inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_pause_resume_state_ledger_entry(
            **_kwargs(protocol_state_snapshot={"step_index": 1}),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_pause_resume_state_ledger_entry(
            **_kwargs(protocol_state_snapshot={"step_index": 2}),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


# --- AC #3 ---


def test_emit_pause_resume_fires_at_workflow_layer_protocol_transitions() -> None:
    """AC #3: composer accepts both workflow-layer transitions; emits per protocol_event_kind."""
    writer = _CapturingLedgerWriter()
    for kind in PauseResumeProtocolEventKind:
        _run(
            emit_pause_resume_state_ledger_entry(
                **_kwargs(protocol_event_kind=kind), ledger_writer=writer
            )
        )
    # Both PAUSE_CAPTURED and RESUME_ATTEMPTED emit successfully under the same action_id.
    assert len(writer.captured) == len(PauseResumeProtocolEventKind)
    for payload in writer.captured:
        assert payload.action_id == "cp.pause-resume-protocol"


# --- AC #4 (renamed) ---


def test_emit_pause_resume_response_hash_is_is_computed() -> None:
    """β.i Q-β.i-3(b): composer does NOT supply response_hash; EntryPayload has no such field."""
    assert "response_hash" not in EntryPayload.model_fields
    writer = _CapturingLedgerWriter()
    _run(emit_pause_resume_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    payload = writer.captured[0]
    assert set(payload.model_fields_set) <= {
        "action_id",
        "idempotency_key",
        "actor",
        "timestamp",
        "procedural_tier_snapshot_ref",
    }


# --- AC #5 ---


def test_emit_pause_resume_zero_cp_audit_emission() -> None:
    """AC #5: greenfield composer emits NO CPAuditLedgerEntry per §16.5.9 invariant 5."""
    writer = _CapturingLedgerWriter()
    result = _run(emit_pause_resume_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert isinstance(result, WriteResult)
    assert len(writer.captured) == 1


def test_emit_pause_resume_orthogonal_to_engine_layer_at_u_cp_78_u_cp_79() -> None:
    """AC #5: workflow-layer action_id distinct from engine-layer at U-CP-78/U-CP-79.

    Per CP spec v1.11 §26 NEW NOTE 2-layer coexistence: workflow-layer emits
    `cp.pause-resume-protocol`; engine-layer at U-CP-78/U-CP-79 emits
    `cp.pause-captured` / `cp.resume-attempted`. Distinct action_id namespaces
    prevent ledger-level collision; downstream consumers discriminate via prefix.
    """
    writer = _CapturingLedgerWriter()
    _run(emit_pause_resume_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    workflow_layer_action_id = writer.captured[0].action_id
    assert workflow_layer_action_id == "cp.pause-resume-protocol"
    # Engine-layer action_ids (per U-CP-78/U-CP-79 spec rows) are distinct.
    assert workflow_layer_action_id != "cp.pause-captured"
    assert workflow_layer_action_id != "cp.resume-attempted"


# --- AC #6 (enum exhaustiveness) ---


def test_pause_resume_protocol_event_kind_enum_exhaustive() -> None:
    """AC #6: PauseResumeProtocolEventKind is a StrEnum with PauseResumeProtocol-method members."""
    assert issubclass(PauseResumeProtocolEventKind, StrEnum)
    members = {m.value for m in PauseResumeProtocolEventKind}
    # Members discriminate the two PauseResumeProtocol class methods at v1.16:
    # capture_pause_snapshot + attempt_resume.
    assert members == {"pause-captured", "resume-attempted"}


# --- composer-await discipline (orthogonal to U-CP-74 AC #9) ---


def test_emit_pause_resume_orthogonal_to_writer_result_variant() -> None:
    """Composer awaits ledger_writer return; does not condition on WriteResult variant."""
    appended_writer = _CapturingLedgerWriter(returns=WriteResult.APPENDED)
    noop_writer = _CapturingLedgerWriter(returns=WriteResult.IDEMPOTENT_NOOP)

    result_a = _run(
        emit_pause_resume_state_ledger_entry(**_kwargs(), ledger_writer=appended_writer)
    )
    result_b = _run(emit_pause_resume_state_ledger_entry(**_kwargs(), ledger_writer=noop_writer))

    assert result_a == WriteResult.APPENDED
    assert result_b == WriteResult.IDEMPOTENT_NOOP
    assert appended_writer.captured[0].idempotency_key == noop_writer.captured[0].idempotency_key


# --- Actor projection ---


def test_emit_pause_resume_actor_projects_to_agent_class() -> None:
    """actor_id is the ActorIdentity stringified; actor_class = AGENT."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_pause_resume_state_ledger_entry(
            **_kwargs(actor=ActorIdentity("control-plane")),
            ledger_writer=writer,
        )
    )
    actor = writer.captured[0].actor
    assert actor.actor_class == ActorClass.AGENT
    assert actor.actor_id == "control-plane"
