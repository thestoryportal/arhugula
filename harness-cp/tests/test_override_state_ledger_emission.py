"""Tests for U-CP-74 emit_override_state_ledger_entry composer (CP spec v1.26 §16.5)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.per_step_override_evaluator import (
    CPAuditLedgerEntry,
    CPSignedAuditLedgerEntry,
    emit_override_audit_entry,
    emit_override_state_ledger_entry,
)
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from harness_cp.workflow_manifest_entry import StepOverride
from harness_is.state_ledger_entry_schema import ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult

_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)
"""CP spec v1.30 §1.2 fixture: stub Identifier returned by the test resolver."""


def _snapshot_resolver() -> Identifier:
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


def _outcome() -> dict[str, Any]:
    return {"model": "claude-opus", "max_tokens": 4096}


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf-1",
        "step_id": "step-2",
        "post_override_step_config": _outcome(),
        "actor": ActorIdentity("control-plane"),
        "procedural_tier_snapshot_resolver": _snapshot_resolver,
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- AC #1 ---


def test_emit_override_state_ledger_action_id() -> None:
    """action_id is the canonical kebab-case identifier per spec v1.26 §16.5.3."""
    writer = _CapturingLedgerWriter()
    _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].action_id == "cp.per-step-override-application"


# --- AC #2 ---


def test_emit_override_state_ledger_idempotency_key_per_reading_a() -> None:
    """idempotency_key bytes follow CP spec v1.27 §16.5.4 row U-CP-14 3-tuple per Reading A.

    v1.25 + v1.26 `override_id` + `policy_id` placeholder segments dropped per
    Q1=A operator ratification 2026-05-29; `(workflow_id, step_id)` discriminator
    carries per-step-override uniqueness per `per_step_overrides: dict[StepID,
    StepOverride]` at `workflow_manifest_entry.py:109`.
    """
    outcome_hash = hashlib.sha256(_canonicalize_outcome_bytes(_outcome())).hexdigest()
    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-2",
                outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].idempotency_key == expected


def test_emit_override_state_ledger_idempotency_key_includes_outcome_hash_suffix() -> None:
    """Q-β.i-1(a): different outcomes at same disambiguator inputs produce different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_override_state_ledger_entry(
            **_kwargs(post_override_step_config={"model": "claude-opus"}),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_override_state_ledger_entry(
            **_kwargs(post_override_step_config={"model": "claude-haiku"}),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


# --- AC #3 ---


def test_emit_override_state_ledger_response_hash_is_is_computed_not_composer_controlled() -> None:
    """β.i Q-β.i-3(b): composer does NOT supply response_hash; EntryPayload has no such field."""
    assert "response_hash" not in EntryPayload.model_fields
    writer = _CapturingLedgerWriter()
    _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    payload = writer.captured[0]
    assert set(payload.model_fields_set) <= {
        "action_id",
        "idempotency_key",
        "actor",
        "timestamp",
        "procedural_tier_snapshot_ref",
    }


# --- AC #4 ---


def test_emit_override_state_ledger_actor_direct_reuse() -> None:
    """actor_id is the ActorIdentity stringified; actor_class = AGENT."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_override_state_ledger_entry(
            **_kwargs(actor=ActorIdentity("control-plane")),
            ledger_writer=writer,
        )
    )
    actor = writer.captured[0].actor
    assert actor.actor_class == ActorClass.AGENT
    assert actor.actor_id == "control-plane"


# --- AC #5 — sibling composer ADDITIVE ---


def test_emit_override_audit_entry_signature_preserved_post_v1_28() -> None:
    """Composer signature + return type preserved at v1.28 per CP spec
    v1.28 §16.5.6.X ZERO breaking change discipline. `timestamp` field
    transitions from `""` placeholder to composer-site ISO-8601 clock per
    the universal-fix scope. `prior_event_hash="0"*64` sentinel preserved
    at solo-developer tier per ADR-D5 §1.4 row 1."""
    out = emit_override_audit_entry(
        workflow_id="wf-1",
        step_id="step-2",
        override=StepOverride(step_id="step-2"),
        actor=ActorIdentity("control-plane"),
    )
    assert isinstance(out, CPAuditLedgerEntry)
    assert out.action_id == "wf-1||step-2"
    assert out.response == "approve"
    assert out.timestamp != ""  # v1.28: composer-site clock
    assert out.prior_event_hash == "0" * 64  # solo-dev sentinel per ADR-D5 §1.4 row 1


# --- AC #6 — dual-emission order-independent ---


def test_emit_override_state_ledger_dual_emission_order_independent() -> None:
    """Sibling composer return value does not depend on audit composer execution order."""
    writer = _CapturingLedgerWriter()
    override = StepOverride(step_id="step-2")
    actor = ActorIdentity("a")

    # Audit-first, then state-ledger
    _audit_a = emit_override_audit_entry(
        workflow_id="wf-1", step_id="step-2", override=override, actor=actor
    )
    state_a = _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=writer))

    # State-ledger-first, then audit
    state_b = _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    _audit_b = emit_override_audit_entry(
        workflow_id="wf-1", step_id="step-2", override=override, actor=actor
    )

    # Both state-ledger emissions identical regardless of audit order (idempotent inputs)
    assert state_a == state_b
    # Both calls captured identical payloads
    assert writer.captured[0].idempotency_key == writer.captured[1].idempotency_key


def test_emit_override_state_ledger_orthogonal_to_audit_emission() -> None:
    """AC #9: composer awaits ledger_writer return; does not condition on WriteResult variant."""
    appended_writer = _CapturingLedgerWriter(returns=WriteResult.APPENDED)
    noop_writer = _CapturingLedgerWriter(returns=WriteResult.IDEMPOTENT_NOOP)

    result_a = _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=appended_writer))
    result_b = _run(emit_override_state_ledger_entry(**_kwargs(), ledger_writer=noop_writer))

    assert result_a == WriteResult.APPENDED
    assert result_b == WriteResult.IDEMPOTENT_NOOP
    # Both invocations produced identical payloads regardless of writer return
    assert appended_writer.captured[0].idempotency_key == noop_writer.captured[0].idempotency_key


# --- AC #7 — ZERO breaking change at §16.2 + §20.4 ---


def test_cp_audit_ledger_entry_shape_unchanged_at_v1_26() -> None:
    """CPAuditLedgerEntry §16.2 8-field shape unchanged at spec v1.26."""
    expected_fields = {
        "action_id",
        "gate_level",
        "response",
        "edited_proposal_hash",
        "rejection_reason_hash",
        "response_text_hash",
        "timestamp",
        "prior_event_hash",
    }
    assert set(CPAuditLedgerEntry.model_fields) == expected_fields


def test_cp_signed_audit_ledger_entry_signing_contract_unchanged() -> None:
    """CPSignedAuditLedgerEntry §20.4 signing contract 6 fields unchanged at v1.26."""
    expected_fields = {
        "entry",
        "audit_signature_sha256",
        "audit_signature_value",
        "audit_signature_algorithm",
        "audit_signature_key_id",
        "audit_signature_key_period",
    }
    assert set(CPSignedAuditLedgerEntry.model_fields) == expected_fields
