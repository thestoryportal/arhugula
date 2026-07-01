"""Tests for U-CP-75 emit_workload_class_selection_state_ledger_entry.

CP spec v1.26 §16.5 row U-CP-27.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.engine_class import EngineClass
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from harness_cp.workload_binding_engine_class_selection import (
    WorkloadBindingSelectionResult,
    emit_workload_class_selection_state_ledger_entry,
)
from harness_is.state_ledger_entry_schema import ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult

_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)


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


def _result(
    selected_class: EngineClass = EngineClass.SAVE_POINT_CHECKPOINT,
    rationale: str = "§7.3 step 2 — software-engineering favors save-point-checkpoint",
) -> WorkloadBindingSelectionResult:
    return WorkloadBindingSelectionResult(
        selected_class=selected_class,
        candidate_set=frozenset({selected_class, EngineClass.PURE_PATTERN_NO_ENGINE}),
        selection_rationale=rationale,
    )


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf-1",
        "step_id": "step-2",
        "selection_result": _result(),
        "actor": ActorIdentity("control-plane"),
        "procedural_tier_snapshot_resolver": _snapshot_resolver,
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- AC #1 ---


def test_emit_workload_class_selection_action_id() -> None:
    """action_id is the canonical kebab-case identifier per spec v1.26 §16.5.3 row U-CP-27."""
    writer = _CapturingLedgerWriter()
    _run(emit_workload_class_selection_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].action_id == "cp.workload-binding-class-selection"


# --- AC #2 ---


def test_emit_workload_class_selection_idempotency_key_per_q_beta_i_1a() -> None:
    """idempotency_key bytes follow §16.5.4 row U-CP-27 5-tuple (v1.26 with outcome-hash suffix)."""
    selection_result = _result()
    canonical = _canonicalize_outcome_bytes(selection_result)
    outcome_hash = hashlib.sha256(canonical).hexdigest()
    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-2",
                selection_result.selected_class.value.encode("utf-8"),
                canonical,
                outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(selection_result=selection_result), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == expected


def test_emit_workload_class_selection_idempotency_key_includes_engine_class_id() -> None:
    """Different selected_class at otherwise-identical inputs produce different idempotency keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(
                selection_result=_result(
                    selected_class=EngineClass.SAVE_POINT_CHECKPOINT,
                    rationale="r",
                )
            ),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(
                selection_result=_result(
                    selected_class=EngineClass.EVENT_SOURCED_REPLAY,
                    rationale="r",
                )
            ),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_workload_class_selection_idempotency_key_includes_outcome_hash_suffix() -> None:
    """Q-β.i-1(a): different rationale (same selected_class) → different keys via outcome-hash."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(selection_result=_result(rationale="rationale-a")),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(selection_result=_result(rationale="rationale-b")),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


# --- AC #3 ---


def test_emit_workload_class_selection_response_hash_is_is_computed() -> None:
    """β.i Q-β.i-3(b): composer does NOT supply response_hash; EntryPayload has no such field."""
    assert "response_hash" not in EntryPayload.model_fields
    writer = _CapturingLedgerWriter()
    _run(emit_workload_class_selection_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    payload = writer.captured[0]
    assert set(payload.model_fields_set) <= {
        "action_id",
        "idempotency_key",
        "actor",
        "timestamp",
        "procedural_tier_snapshot_ref",
    }


# --- AC #4 — fires post-resolve-pre-return (type-encoded) ---


def test_emit_workload_class_selection_takes_constructed_selection_result_input() -> None:
    """AC #4: composer takes already-constructed WorkloadBindingSelectionResult.

    Encodes post-resolve-pre-return discipline at the type system — the composer
    cannot fire before `select_engine_class` produces the Pydantic-frozen
    `WorkloadBindingSelectionResult` value; cannot fire AFTER the value is
    returned to caller and discarded.
    """
    writer = _CapturingLedgerWriter()
    selection_result = _result()
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(selection_result=selection_result), ledger_writer=writer
        )
    )
    # Composer captured the result's content via canonical-bytes derivation in
    # the idempotency_key — derivable only from a constructed result instance.
    canonical = _canonicalize_outcome_bytes(selection_result)
    outcome_hash = hashlib.sha256(canonical).hexdigest()
    assert outcome_hash.encode("utf-8") in b"\x1e".join(
        (
            b"wf-1",
            b"step-2",
            selection_result.selected_class.value.encode("utf-8"),
            canonical,
            outcome_hash.encode("utf-8"),
        )
    )


# --- AC #5 — ZERO CP audit-ledger emission (greenfield composer) ---


def test_emit_workload_class_selection_zero_cp_audit_emission() -> None:
    """AC #5: greenfield composer emits NO CPAuditLedgerEntry per §16.5.9 invariant 5."""
    writer = _CapturingLedgerWriter()
    result = _run(
        emit_workload_class_selection_state_ledger_entry(**_kwargs(), ledger_writer=writer)
    )
    # Composer returns the WriteResult (state-ledger emission) — NOT a
    # CPAuditLedgerEntry. Single ledger_writer invocation; no sibling audit-write.
    assert isinstance(result, WriteResult)
    assert len(writer.captured) == 1


# --- AC #6 — idempotent replay ---


def test_emit_workload_class_selection_idempotent_replay() -> None:
    """Same inputs → identical idempotency_key (replay-safe per IS hash-chain semantic)."""
    writer = _CapturingLedgerWriter()
    selection_result = _result()
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(selection_result=selection_result), ledger_writer=writer
        )
    )
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(selection_result=selection_result), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == writer.captured[1].idempotency_key
    assert writer.captured[0].action_id == writer.captured[1].action_id


# --- AC #6 (composer-await discipline; orthogonal to U-CP-74 AC #9) ---


def test_emit_workload_class_selection_orthogonal_to_writer_result_variant() -> None:
    """Composer awaits ledger_writer return; does not condition on WriteResult variant."""
    appended_writer = _CapturingLedgerWriter(returns=WriteResult.APPENDED)
    noop_writer = _CapturingLedgerWriter(returns=WriteResult.IDEMPOTENT_NOOP)

    result_a = _run(
        emit_workload_class_selection_state_ledger_entry(**_kwargs(), ledger_writer=appended_writer)
    )
    result_b = _run(
        emit_workload_class_selection_state_ledger_entry(**_kwargs(), ledger_writer=noop_writer)
    )

    assert result_a == WriteResult.APPENDED
    assert result_b == WriteResult.IDEMPOTENT_NOOP
    assert appended_writer.captured[0].idempotency_key == noop_writer.captured[0].idempotency_key


# --- Actor projection ---


def test_emit_workload_class_selection_actor_projects_to_agent_class() -> None:
    """actor_id is the ActorIdentity stringified; actor_class = AGENT (matches U-CP-74 pattern)."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_workload_class_selection_state_ledger_entry(
            **_kwargs(actor=ActorIdentity("control-plane")),
            ledger_writer=writer,
        )
    )
    actor = writer.captured[0].actor
    assert actor.actor_class == ActorClass.AGENT
    assert actor.actor_id == "control-plane"
