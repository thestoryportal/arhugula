"""Tests for U-CP-79 emit_resume_attempted_state_ledger_entry engine-layer composer.

CP spec v1.26 §16.5 row U-CP-50.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.pause_resume_protocol import (
    ResumeOutcome,
    ResumeOutcomeKind,
    emit_resume_attempted_state_ledger_entry,
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


def _outcome(
    *,
    outcome_kind: ResumeOutcomeKind = ResumeOutcomeKind.RESUME_CLEAN,
    context_revalidated: bool = False,
) -> ResumeOutcome:
    return ResumeOutcome(
        outcome_kind=outcome_kind,
        material_diff=(),
        context_revalidated=context_revalidated,
        resume_audit_entry_id=None,
    )


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf-1",
        "step_id": "step-7",
        "resume_event_id": "resume-evt-001",
        "resume_attempt_count": 1,
        "resume_outcome": _outcome(),
        "actor": ActorIdentity("engine-layer"),
        "procedural_tier_snapshot_resolver": _pt_resolver,
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- AC #1 ---


def test_emit_resume_attempted_action_id() -> None:
    """action_id is the canonical kebab-case identifier per spec v1.26 §16.5.3 row U-CP-50."""
    writer = _CapturingLedgerWriter()
    _run(emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].action_id == "cp.resume-attempted"


# --- AC #2 ---


def test_emit_resume_attempted_idempotency_key_per_q_beta_i_1a() -> None:
    """idempotency_key bytes follow §16.5.4 row U-CP-50 5-tuple (v1.26 with outcome-hash suffix)."""
    outcome = _outcome()
    outcome_hash = hashlib.sha256(_canonicalize_outcome_bytes(outcome)).hexdigest()
    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-7",
                b"resume-evt-001",
                b"1",
                outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_outcome=outcome), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == expected


def test_emit_resume_attempted_idempotency_key_includes_attempt_count() -> None:
    """Different resume_attempt_count at otherwise-identical inputs → different keys.

    Discriminates retry attempts at the same `resume_event_id` per AC #2.
    """
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_attempt_count=1), ledger_writer=writer_a
        )
    )
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_attempt_count=2), ledger_writer=writer_b
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_resume_attempted_idempotency_key_includes_resume_event_id() -> None:
    """Different resume_event_id at otherwise-identical inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_event_id="evt-A"), ledger_writer=writer_a
        )
    )
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_event_id="evt-B"), ledger_writer=writer_b
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_resume_attempted_idempotency_key_includes_outcome_hash_suffix() -> None:
    """Q-β.i-1(a): different ResumeOutcome at same disambiguators → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_outcome=_outcome(outcome_kind=ResumeOutcomeKind.RESUME_CLEAN)),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(
                resume_outcome=_outcome(
                    outcome_kind=ResumeOutcomeKind.RESUME_AFTER_REVALIDATION,
                    context_revalidated=True,
                )
            ),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


# --- AC #3 (fires post-resolve pre-return) ---


def test_emit_resume_attempted_fires_post_resolve_pre_return() -> None:
    """AC #3: composer takes the ResumeOutcome and emits a single payload.

    Documents firing-site discipline per §16.5.7: invoked once AFTER engine-
    layer `attempt_resume(...)` resolves the outcome, BEFORE returning. Single-
    invocation → single payload.
    """
    writer = _CapturingLedgerWriter()
    _run(emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert len(writer.captured) == 1
    assert writer.captured[0].action_id == "cp.resume-attempted"


# --- AC #4 (renamed per plan v2.29) ---


def test_emit_resume_attempted_response_hash_is_is_computed() -> None:
    """β.i Q-β.i-3(b): composer does NOT supply response_hash; EntryPayload has no such field."""
    assert "response_hash" not in EntryPayload.model_fields
    writer = _CapturingLedgerWriter()
    _run(emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    payload = writer.captured[0]
    assert set(payload.model_fields_set) <= {
        "action_id",
        "idempotency_key",
        "actor",
        "timestamp",
        "procedural_tier_snapshot_ref",
    }


# --- AC #5 (ZERO CP-audit + engine-layer-vs-workflow-layer orthogonality) ---


def test_emit_resume_attempted_zero_cp_audit_emission() -> None:
    """AC #5: greenfield composer emits NO CPAuditLedgerEntry per §16.5.9 invariant 5."""
    writer = _CapturingLedgerWriter()
    result = _run(emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert isinstance(result, WriteResult)
    assert len(writer.captured) == 1


def test_emit_resume_attempted_engine_layer_orthogonal_to_workflow_layer_at_u_cp_76() -> None:
    """AC #5: engine-layer action_id distinct from workflow-layer at U-CP-76.

    Per CP spec v1.11 §26 NEW NOTE 2-layer coexistence: engine-layer emits
    `cp.resume-attempted` here; workflow-layer at U-CP-76 emits
    `cp.pause-resume-protocol`. Engine-layer is also distinct from sibling
    `cp.pause-captured` at U-CP-78. Distinct action_id namespaces prevent
    ledger-level collision; downstream consumers discriminate via prefix.
    """
    writer = _CapturingLedgerWriter()
    _run(emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    engine_layer_action_id = writer.captured[0].action_id
    assert engine_layer_action_id == "cp.resume-attempted"
    assert engine_layer_action_id != "cp.pause-resume-protocol"
    assert engine_layer_action_id != "cp.pause-captured"


# --- AC #6 (fires at BOTH success and failure outcomes) ---


def test_emit_resume_attempted_fires_on_success_outcomes() -> None:
    """AC #6: fires on both RESUME_CLEAN and RESUME_AFTER_REVALIDATION (success kinds)."""
    success_kinds = (
        ResumeOutcomeKind.RESUME_CLEAN,
        ResumeOutcomeKind.RESUME_AFTER_REVALIDATION,
    )
    writer = _CapturingLedgerWriter()
    for kind in success_kinds:
        _run(
            emit_resume_attempted_state_ledger_entry(
                **_kwargs(resume_outcome=_outcome(outcome_kind=kind)),
                ledger_writer=writer,
            )
        )
    assert len(writer.captured) == len(success_kinds)
    for payload in writer.captured:
        assert payload.action_id == "cp.resume-attempted"


def test_emit_resume_attempted_fires_on_failure_outcomes() -> None:
    """AC #6: fires on both ABORT_REVALIDATION_FAILED and ABORT_SNAPSHOT_CORRUPTED.

    Failure is a recorded outcome via ResumeOutcome.outcome_kind = ABORT_*,
    NOT a swallowed exception. Composer treats failure-outcome as a normal
    invocation and emits the ledger entry.
    """
    failure_kinds = (
        ResumeOutcomeKind.ABORT_REVALIDATION_FAILED,
        ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED,
    )
    writer = _CapturingLedgerWriter()
    for kind in failure_kinds:
        _run(
            emit_resume_attempted_state_ledger_entry(
                **_kwargs(resume_outcome=_outcome(outcome_kind=kind)),
                ledger_writer=writer,
            )
        )
    assert len(writer.captured) == len(failure_kinds)
    for payload in writer.captured:
        assert payload.action_id == "cp.resume-attempted"


def test_emit_resume_attempted_success_and_failure_yield_different_keys() -> None:
    """AC #6: success-outcome and failure-outcome at same disambiguators → different keys.

    Failure outcomes are recorded via different `outcome_kind` enum values,
    which alters `ResumeOutcome` canonical bytes → different outcome-hash
    suffix → different idempotency_key.
    """
    writer_success = _CapturingLedgerWriter()
    writer_failure = _CapturingLedgerWriter()
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(resume_outcome=_outcome(outcome_kind=ResumeOutcomeKind.RESUME_CLEAN)),
            ledger_writer=writer_success,
        )
    )
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(
                resume_outcome=_outcome(outcome_kind=ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED)
            ),
            ledger_writer=writer_failure,
        )
    )
    assert writer_success.captured[0].idempotency_key != writer_failure.captured[0].idempotency_key


# --- composer-await discipline (orthogonal to U-CP-74 AC #9) ---


def test_emit_resume_attempted_orthogonal_to_writer_result_variant() -> None:
    """Composer awaits ledger_writer return; does not condition on WriteResult variant."""
    appended_writer = _CapturingLedgerWriter(returns=WriteResult.APPENDED)
    noop_writer = _CapturingLedgerWriter(returns=WriteResult.IDEMPOTENT_NOOP)

    result_a = _run(
        emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=appended_writer)
    )
    result_b = _run(
        emit_resume_attempted_state_ledger_entry(**_kwargs(), ledger_writer=noop_writer)
    )

    assert result_a == WriteResult.APPENDED
    assert result_b == WriteResult.IDEMPOTENT_NOOP
    assert appended_writer.captured[0].idempotency_key == noop_writer.captured[0].idempotency_key


# --- Actor projection ---


def test_emit_resume_attempted_actor_projects_to_agent_class() -> None:
    """actor_id is the ActorIdentity stringified; actor_class = AGENT."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_resume_attempted_state_ledger_entry(
            **_kwargs(actor=ActorIdentity("engine-layer")),
            ledger_writer=writer,
        )
    )
    actor = writer.captured[0].actor
    assert actor.actor_class == ActorClass.AGENT
    assert actor.actor_id == "engine-layer"
