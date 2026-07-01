"""Tests for U-CP-78 emit_pause_captured_state_ledger_entry engine-layer composer.

CP spec v1.26 §16.5 row U-CP-49.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import ExternalReference, ReferenceClass, StateSummary
from harness_cp.pause_resume_protocol import (
    PauseEvent,
    PauseReason,
    emit_pause_captured_state_ledger_entry,
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


def _state_summary(*, version: str = "v1") -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text=version,
        summary_hash=hashlib.sha256(version.encode()).hexdigest(),
        idempotency_key=Identifier("idem-" + version),
        external_references=(
            ExternalReference(
                reference_class=ReferenceClass.FILESYSTEM_STATE,
                reference_id="state-" + version,
                snapshot_capture_at_pause=b"snapshot-" + version.encode("utf-8"),
            ),
        ),
    )


def _pause_event(
    *,
    audit_entry_id: str = "pause-audit-001",
    state_summary_version: str = "v1",
) -> PauseEvent:
    state_summary = _state_summary(version=state_summary_version)
    return PauseEvent(
        paused_at="2023-11-14T22:13:20+00:00",
        pause_reason=PauseReason.OPERATOR_INITIATED_PAUSE,
        state_summary_snapshot=state_summary,
        external_refs_captured=state_summary.external_references,
        pause_audit_entry_id=Identifier(audit_entry_id),
    )


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf-1",
        "step_id": "step-7",
        "pause_event": _pause_event(),
        "actor": ActorIdentity("control-plane"),
        "procedural_tier_snapshot_resolver": _pt_resolver,
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- AC #1 ---


def test_emit_pause_captured_action_id() -> None:
    """action_id is the canonical kebab-case identifier per spec v1.26 §16.5.3 row U-CP-49."""
    writer = _CapturingLedgerWriter()
    _run(emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].action_id == "cp.pause-captured"


# --- AC #2 ---


def test_emit_pause_captured_idempotency_key_per_q_beta_i_1a() -> None:
    """idempotency_key bytes use PauseEvent.pause_audit_entry_id and PauseEvent outcome bytes."""
    pause_event = _pause_event()
    outcome_hash = hashlib.sha256(_canonicalize_outcome_bytes(pause_event)).hexdigest()
    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-7",
                b"pause-audit-001",
                b"pause-audit-001",
                outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(pause_event=pause_event), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == expected


def test_emit_pause_captured_idempotency_key_includes_pause_audit_entry_id() -> None:
    """Different PauseEvent.pause_audit_entry_id at otherwise-identical inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(pause_event=_pause_event(audit_entry_id="pause-audit-A")),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(pause_event=_pause_event(audit_entry_id="pause-audit-B")),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_pause_captured_idempotency_key_includes_outcome_hash_suffix() -> None:
    """Different PauseEvent canonical bytes at SAME audit id → different keys."""
    event_a = _pause_event(audit_entry_id="pause-audit-fixed", state_summary_version="v1")
    event_b = _pause_event(audit_entry_id="pause-audit-fixed", state_summary_version="v2")
    assert event_a.pause_audit_entry_id == event_b.pause_audit_entry_id
    assert _canonicalize_outcome_bytes(event_a) != _canonicalize_outcome_bytes(event_b)

    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(pause_event=event_a), ledger_writer=writer_a
        )
    )
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(pause_event=event_b), ledger_writer=writer_b
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_pause_captured_consumes_real_engine_output() -> None:
    """U-CP-78 Reading A: composer consumes PauseEvent, the engine free-function output."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(pause_event=_pause_event(audit_entry_id="engine-pause-1")),
            ledger_writer=writer,
        )
    )
    assert len(writer.captured) == 1


# --- AC #3 ---


def test_emit_pause_captured_fires_post_capture_pre_return() -> None:
    """AC #3: composer takes the PauseEvent and emits a single payload.

    Documents firing-site discipline per §16.5.7: invoked once AFTER engine-
    layer `capture_pause_snapshot(...)` returns the pause event, BEFORE returning
    to caller. Single-invocation → single payload.
    """
    writer = _CapturingLedgerWriter()
    _run(emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert len(writer.captured) == 1
    assert writer.captured[0].action_id == "cp.pause-captured"


# --- AC #4 (renamed per plan v2.29) ---


def test_emit_pause_captured_response_hash_is_is_computed() -> None:
    """β.i Q-β.i-3(b): composer does NOT supply response_hash; EntryPayload has no such field."""
    assert "response_hash" not in EntryPayload.model_fields
    writer = _CapturingLedgerWriter()
    _run(emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    payload = writer.captured[0]
    assert set(payload.model_fields_set) <= {
        "action_id",
        "idempotency_key",
        "actor",
        "timestamp",
        "procedural_tier_snapshot_ref",
    }


# --- AC #5 ---


def test_emit_pause_captured_zero_cp_audit_emission() -> None:
    """AC #5: greenfield composer emits NO CPAuditLedgerEntry per §16.5.9 invariant 5."""
    writer = _CapturingLedgerWriter()
    result = _run(emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert isinstance(result, WriteResult)
    assert len(writer.captured) == 1


def test_emit_pause_captured_engine_layer_orthogonal_to_workflow_layer_at_u_cp_76() -> None:
    """AC #5: engine-layer action_id distinct from workflow-layer at U-CP-76.

    Per CP spec v1.11 §26 NEW NOTE 2-layer coexistence: engine-layer emits
    `cp.pause-captured` here; workflow-layer at U-CP-76 emits
    `cp.pause-resume-protocol`. Distinct action_id namespaces prevent
    ledger-level collision; downstream consumers discriminate via prefix.
    """
    writer = _CapturingLedgerWriter()
    _run(emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    engine_layer_action_id = writer.captured[0].action_id
    assert engine_layer_action_id == "cp.pause-captured"
    # Workflow-layer action_id at U-CP-76 is distinct.
    assert engine_layer_action_id != "cp.pause-resume-protocol"


# --- composer-await discipline (orthogonal to U-CP-74 AC #9) ---


def test_emit_pause_captured_orthogonal_to_writer_result_variant() -> None:
    """Composer awaits ledger_writer return; does not condition on WriteResult variant."""
    appended_writer = _CapturingLedgerWriter(returns=WriteResult.APPENDED)
    noop_writer = _CapturingLedgerWriter(returns=WriteResult.IDEMPOTENT_NOOP)

    result_a = _run(
        emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=appended_writer)
    )
    result_b = _run(emit_pause_captured_state_ledger_entry(**_kwargs(), ledger_writer=noop_writer))

    assert result_a == WriteResult.APPENDED
    assert result_b == WriteResult.IDEMPOTENT_NOOP
    assert appended_writer.captured[0].idempotency_key == noop_writer.captured[0].idempotency_key


# --- Actor projection ---


def test_emit_pause_captured_actor_projects_to_agent_class() -> None:
    """actor_id is the ActorIdentity stringified; actor_class = AGENT."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_pause_captured_state_ledger_entry(
            **_kwargs(actor=ActorIdentity("engine-layer")),
            ledger_writer=writer,
        )
    )
    actor = writer.captured[0].actor
    assert actor.actor_class == ActorClass.AGENT
    assert actor.actor_id == "engine-layer"
