"""Tests for U-CP-49 — pause/resume protocol + snapshot capture (C-CP-22 §22.1).

Acceptance-criterion coverage:
  #1 PauseEvent 5 fields        -> test_pause_event_five_fields
  #2 PauseReason 4 values       -> test_pause_reason_cardinality_four
  #4 pause audit surface        -> test_capture_pause_snapshot_surface
  #5 resume surface             -> test_attempt_resume_surface
  #6 ResumeOutcomeKind 4 values -> test_resume_outcome_cardinality_four
  #7 clean / revalidated resume -> test_clean_resume_no_diff
                                   test_revalidation_resume_with_diff
                                   test_abort_on_revalidation_fail
  #10 deterministic resume      -> test_resume_classification_deterministic
"""

from __future__ import annotations

import pytest
from harness_core import EntryID, WorkflowID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.handoff_context import (
    ExternalReference,
    ReferenceClass,
    StateSummary,
)
from harness_cp.material_diff_detection import MaterialDiff
from harness_cp.pause_resume_protocol import (
    DeterministicEnginePauseResumeSubstrate,
    EnginePauseResumeSubstrateNotBoundError,
    PauseEvent,
    PauseReason,
    ResumeAttempt,
    ResumeOutcomeKind,
    attempt_resume,
    bind_engine_pause_resume_substrate,
    capture_pause_snapshot,
    classify_resume,
)
from harness_is.state_ledger_entry_schema import Identifier


def _ref() -> ExternalReference:
    return ExternalReference(
        reference_class=ReferenceClass.FILESYSTEM_STATE,
        reference_id="f0",
        snapshot_capture_at_pause=b"snap",
    )


def _state_summary() -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text="x",
        summary_hash="0" * 64,
        idempotency_key=Identifier("k0"),
        external_references=(_ref(),),
    )


def _material(is_material: bool) -> MaterialDiff:
    return MaterialDiff(
        reference=_ref(),
        prior_snapshot=b"a",
        current_value=b"b",
        is_material=is_material,
    )


def test_pause_event_five_fields() -> None:
    """#1 — PauseEvent declares exactly five fields per §22.1."""
    assert set(PauseEvent.model_fields) == {
        "paused_at",
        "pause_reason",
        "state_summary_snapshot",
        "external_refs_captured",
        "pause_audit_entry_id",
    }
    event = PauseEvent(
        paused_at="2026-05-16T00:00:00Z",
        pause_reason=PauseReason.HITL_INVOCATION_PENDING,
        state_summary_snapshot=_state_summary(),
        external_refs_captured=(_ref(),),
        pause_audit_entry_id=EntryID("e0"),
    )
    assert event.pause_reason is PauseReason.HITL_INVOCATION_PENDING


def test_pause_reason_cardinality_four() -> None:
    """#2 — PauseReason declares exactly four values per §22.1."""
    assert len(PauseReason) == 4


def test_capture_pause_snapshot_surface() -> None:
    """#4 — capture_pause_snapshot fails closed until a substrate is bound."""
    with pytest.raises(EnginePauseResumeSubstrateNotBoundError):
        capture_pause_snapshot(WorkflowID("w0"), PauseReason.OPERATOR_INITIATED_PAUSE)


def test_attempt_resume_surface() -> None:
    """#5 — attempt_resume fails closed until a substrate is bound."""
    attempt = ResumeAttempt(
        paused_workflow_id=WorkflowID("w0"),
        resume_at="2026-05-16T01:00:00Z",
        resume_request_actor=ActorIdentity("operator"),
    )
    with pytest.raises(EnginePauseResumeSubstrateNotBoundError):
        attempt_resume(attempt)


def test_engine_substrate_capture_pause_snapshot_returns_event() -> None:
    """#4 — a bound engine substrate materializes the free-function surface."""
    substrate = DeterministicEnginePauseResumeSubstrate(
        state_summary_provider=_state_summary,
        pause_audit_entry_id_provider=lambda _workflow_id, _pause_reason: EntryID("pause-entry-1"),
    )
    with bind_engine_pause_resume_substrate(substrate):
        event = capture_pause_snapshot(WorkflowID("w0"), PauseReason.OPERATOR_INITIATED_PAUSE)

    assert event.pause_reason is PauseReason.OPERATOR_INITIATED_PAUSE
    assert event.state_summary_snapshot == _state_summary()
    assert event.external_refs_captured == (_ref(),)
    assert event.pause_audit_entry_id == "pause-entry-1"


def test_engine_substrate_attempt_resume_clean_after_capture() -> None:
    """#5/#7 — bound substrate reads the captured pause event and classifies clean resume."""
    substrate = DeterministicEnginePauseResumeSubstrate(
        state_summary_provider=_state_summary,
    )
    attempt = ResumeAttempt(
        paused_workflow_id=WorkflowID("w0"),
        resume_at="2026-05-16T01:00:00Z",
        resume_request_actor=ActorIdentity("operator"),
    )

    with bind_engine_pause_resume_substrate(substrate):
        capture_pause_snapshot(WorkflowID("w0"), PauseReason.OPERATOR_INITIATED_PAUSE)
        outcome = attempt_resume(attempt)

    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_CLEAN
    assert outcome.material_diff == ()
    assert outcome.context_revalidated is False


def test_engine_substrate_attempt_resume_after_revalidation_with_diff() -> None:
    """#7 — material diff with successful revalidation resumes after revalidation."""
    substrate = DeterministicEnginePauseResumeSubstrate(
        state_summary_provider=_state_summary,
        diff_provider=lambda _event, _attempt: (_material(True),),
        revalidation_succeeded=lambda _attempt, _diff: True,
    )
    attempt = ResumeAttempt(
        paused_workflow_id=WorkflowID("w0"),
        resume_at="2026-05-16T01:00:00Z",
        resume_request_actor=ActorIdentity("operator"),
    )

    with bind_engine_pause_resume_substrate(substrate):
        capture_pause_snapshot(WorkflowID("w0"), PauseReason.OPERATOR_INITIATED_PAUSE)
        outcome = attempt_resume(attempt)

    assert outcome.outcome_kind is ResumeOutcomeKind.RESUME_AFTER_REVALIDATION
    assert len(outcome.material_diff) == 1
    assert outcome.context_revalidated is True


def test_engine_substrate_attempt_resume_aborts_without_captured_snapshot() -> None:
    """#5 — bounded-read miss is surfaced as snapshot-corruption abort."""
    substrate = DeterministicEnginePauseResumeSubstrate(
        state_summary_provider=_state_summary,
    )
    attempt = ResumeAttempt(
        paused_workflow_id=WorkflowID("missing"),
        resume_at="2026-05-16T01:00:00Z",
        resume_request_actor=ActorIdentity("operator"),
    )

    with bind_engine_pause_resume_substrate(substrate):
        outcome = attempt_resume(attempt)

    assert outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED
    assert outcome.material_diff == ()
    assert outcome.context_revalidated is False


def test_resume_outcome_cardinality_four() -> None:
    """#6 — ResumeOutcomeKind declares exactly four values per §22.1."""
    assert len(ResumeOutcomeKind) == 4


def test_clean_resume_no_diff() -> None:
    """#7 — an empty / immaterial diff-set classifies as RESUME_CLEAN."""
    assert classify_resume((), revalidation_succeeded=True) is (ResumeOutcomeKind.RESUME_CLEAN)
    assert (
        classify_resume((_material(False),), revalidation_succeeded=True)
        is ResumeOutcomeKind.RESUME_CLEAN
    )


def test_revalidation_resume_with_diff() -> None:
    """#7 — a material diff with successful revalidation resumes."""
    assert (
        classify_resume((_material(True),), revalidation_succeeded=True)
        is ResumeOutcomeKind.RESUME_AFTER_REVALIDATION
    )


def test_abort_on_revalidation_fail() -> None:
    """#7 — a material diff with failed revalidation aborts."""
    assert (
        classify_resume((_material(True),), revalidation_succeeded=False)
        is ResumeOutcomeKind.ABORT_REVALIDATION_FAILED
    )


def test_resume_classification_deterministic() -> None:
    """#10 — classify_resume is deterministic given its inputs."""
    diff = (_material(True),)
    a = classify_resume(diff, revalidation_succeeded=True)
    b = classify_resume(diff, revalidation_succeeded=True)
    assert a is b
