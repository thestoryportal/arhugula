"""Tests for U-CP-30 — `HandoffContext` family schemas (C-CP-13 §13.1/§13.4/§13.5).

Acceptance-criterion coverage:
  #1 HandoffContext 7 fields        -> test_handoff_context_seven_fields
  #2 StateSummary 5 fields          -> test_state_summary_five_plus_external_refs,
                                       test_no_current_state_spelling
  #3 LedgerEntryRef 3 fields        -> test_ledger_entry_ref_three_fields
  #4 idempotency_key join export    -> test_idempotency_key_join_export
  #5 T-perm-2 F2 read/write         -> test_t_perm_2_f2_read_write
  #6 serialization deferred         -> test_serialization_deferred
  #7 ActionKind/ProposedAction/     -> test_action_kind_cardinality_three,
     ActionPayload                     test_proposed_action_three_fields,
                                       test_action_payload_opaque_mapping_no_invented_fields
  #8 FailedAttempt/Alternative/     -> test_failed_attempt_three_fields,
     RetryHistory                      test_alternative_two_fields,
                                       test_retry_history_three_fields
"""

from __future__ import annotations

from harness_core.identity import ActionID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import (
    ActionKind,
    Alternative,
    ExternalReference,
    FailedAttempt,
    HandoffContext,
    LedgerEntryRef,
    ProposedAction,
    ReferenceClass,
    RetryHistory,
    StateSummary,
)
from harness_is.state_ledger_entry_schema import Identifier


def _ledger_ref() -> LedgerEntryRef:
    return LedgerEntryRef(
        action_id=ActionID("a-1"),
        entry_hash="0" * 64,
        actor=ActorIdentity("lead"),
    )


def _state_summary() -> StateSummary:
    return StateSummary(
        relevant_entries=(_ledger_ref(),),
        summary_text="prior turn digest",
        summary_hash="1" * 64,
        idempotency_key=Identifier("idem-1"),
        external_references=(
            ExternalReference(
                reference_class=ReferenceClass.F2_LEDGER_ENTRY,
                reference_id="ref-1",
            ),
        ),
    )


def _handoff() -> HandoffContext:
    return HandoffContext(
        proposed_action=ProposedAction(
            action_kind=ActionKind.INFERENCE_STEP,
            payload={"k": "v"},
        ),
        agent_confidence=0.9,
        failed_attempts=(),
        alternatives_considered=(),
        state_summary=_state_summary(),
        audit_trail_link=_ledger_ref(),
        retry_history=RetryHistory(attempts=(), retry_count=0),
    )


def test_handoff_context_seven_fields() -> None:
    assert set(HandoffContext.model_fields) == {
        "proposed_action",
        "agent_confidence",
        "failed_attempts",
        "alternatives_considered",
        "state_summary",
        "audit_trail_link",
        "retry_history",
    }


def test_state_summary_five_plus_external_refs() -> None:
    assert set(StateSummary.model_fields) == {
        "relevant_entries",
        "summary_text",
        "summary_hash",
        "idempotency_key",
        "external_references",
    }


def test_no_current_state_spelling() -> None:
    # Regression — the unified spelling is `StateSummary`; no `CurrentState`.
    import harness_cp.handoff_context as mod

    assert not hasattr(mod, "CurrentState")
    assert _state_summary().summary_text == "prior turn digest"


def test_ledger_entry_ref_three_fields() -> None:
    assert set(LedgerEntryRef.model_fields) == {"action_id", "entry_hash", "actor"}


def test_idempotency_key_join_export() -> None:
    # idempotency_key is the IS-exported Identifier (IDEMPOTENCY_KEY_JOIN_EXPORT).
    ss = _state_summary()
    assert isinstance(ss.idempotency_key, str)


def test_t_perm_2_f2_read_write() -> None:
    # The handoff crosses the across-turn boundary via F2 LedgerEntryRef pairs;
    # F2-layer resolution stands (the audit_trail_link + relevant_entries are
    # all F2 entry references).
    h = _handoff()
    assert h.audit_trail_link.entry_hash == "0" * 64
    assert h.state_summary.relevant_entries[0].action_id == ActionID("a-1")


def test_serialization_deferred() -> None:
    # Serialization format is implementation discretion; round-trips via Pydantic.
    h = _handoff()
    assert HandoffContext.model_validate(h.model_dump()) == h


def test_action_kind_cardinality_three() -> None:
    assert set(ActionKind) == {
        ActionKind.TOOL_CALL,
        ActionKind.SUB_AGENT_DISPATCH,
        ActionKind.INFERENCE_STEP,
    }
    assert len(ActionKind) == 3


def test_proposed_action_three_fields() -> None:
    assert set(ProposedAction.model_fields) == {"action_kind", "payload", "brief"}


def test_action_payload_opaque_mapping_no_invented_fields() -> None:
    # ActionPayload is the opaque Mapping[str, Any] alias — arbitrary keys.
    pa = ProposedAction(
        action_kind=ActionKind.TOOL_CALL,
        payload={"arbitrary": 1, "shape": ["any"]},
    )
    assert pa.payload["arbitrary"] == 1


def test_failed_attempt_three_fields() -> None:
    assert set(FailedAttempt.model_fields) == {
        "attempt_index",
        "cause",
        "attempted_at",
    }


def test_alternative_two_fields() -> None:
    assert set(Alternative.model_fields) == {"description", "rejected_reason"}


def test_retry_history_three_fields() -> None:
    assert set(RetryHistory.model_fields) == {
        "attempts",
        "retry_count",
        "last_retry_cause",
    }


def test_reference_class_cardinality_four() -> None:
    assert len(ReferenceClass) == 4
