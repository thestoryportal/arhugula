"""Tests for U-OD-51 v2.18 helpers — pause/resume audit-write production helpers.

OD spec v1.11 §C-OD-30.4 + OD plan v2.18 U-OD-51 ACs #6 + #7 + #8 + #10.

Two module-level helpers at `harness_od.pause_resume_namespace`:
- `_project_pause_event_to_audit_payload(event, *, ...) -> PauseResumeAuditPayload`
- `_project_resume_outcome_to_audit_payload(attempt, outcome, *, ...) -> PauseResumeAuditPayload`

Helpers land as part of the pause/resume back-flow arc (narrow scope per operator
AskUserQuestion 2026-05-24). Helpers are DEAD CODE at landing — no production
callsite exists; the CP composer authoring arc (gates H_T-CP-22 PARTIAL →
RETIRE-READY per harness-cp/CLAUDE.md §4.1) is the future consumer.

Acceptance-criterion coverage (OD plan v2.18 U-OD-51 ACs #6-#10):
  #6  pause helper signature + audit_cp_action_id pattern + audit_cp_response "paused"
        + path-disjoint nullification of resume-path fields
        -> test_project_pause_event_action_id_pattern
           test_project_pause_event_response_constant
           test_project_pause_event_nulls_resume_path_fields
           test_project_pause_event_round_trip_fields

  #7  resume helper signature + 4-outcome response selection +
        path-disjoint nullification of pause-path fields
        -> test_project_resume_outcome_action_id_pattern
           test_project_resume_outcome_response_resume_clean
           test_project_resume_outcome_response_resume_after_revalidation
           test_project_resume_outcome_response_abort_revalidation_failed
           test_project_resume_outcome_response_abort_snapshot_corrupted
           test_project_resume_outcome_nulls_pause_path_fields
           test_project_resume_outcome_round_trip_fields

  #8  path-disjoint field nullification enforced at helper bodies
        -> test_project_pause_event_nulls_resume_path_fields
           test_project_resume_outcome_nulls_pause_path_fields

  #10 importable; sentinel values flow through; frozen-model invariant
        -> test_helpers_importable
           test_project_pause_event_timestamp_mvp_sentinel
           test_project_pause_event_prior_event_hash_zero_sentinel
           test_project_resume_outcome_diff_summary_hash_optional
           test_helpers_produce_frozen_payloads
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
    PauseEvent,
    PauseReason,
    ResumeAttempt,
    ResumeOutcome,
    ResumeOutcomeKind,
)
from harness_is.state_ledger_entry_schema import Identifier

# Test-fixture direct-construction of helpers per OD spec v1.11 §C-OD-30.4.1
# step 1 explicit carve-out: "Direct construction at TEST fixtures and at the
# converter's own isinstance branch is permitted". Underscore-prefixed names
# mirror cost-axis sibling precedent at `cost_record_audit_writer.py`.
from harness_od.pause_resume_namespace import (  # pyright: ignore[reportPrivateUsage]
    PauseResumeAuditPayload,
    _project_pause_event_to_audit_payload,
    _project_resume_outcome_to_audit_payload,
)
from pydantic import ValidationError

# ----------------------------------------------------------------------------
# Test fixtures (mirror harness-cp/tests/test_pause_resume_protocol.py shapes)
# ----------------------------------------------------------------------------


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


def _pause_event(
    pause_reason: PauseReason = PauseReason.HITL_INVOCATION_PENDING,
) -> PauseEvent:
    return PauseEvent(
        paused_at="2026-05-24T00:00:00Z",
        pause_reason=pause_reason,
        state_summary_snapshot=_state_summary(),
        external_refs_captured=(_ref(),),
        pause_audit_entry_id=EntryID("e0"),
    )


def _resume_attempt() -> ResumeAttempt:
    return ResumeAttempt(
        paused_workflow_id=WorkflowID("w-test-0"),
        resume_at="2026-05-24T01:00:00Z",
        resume_request_actor=ActorIdentity("operator"),
    )


def _resume_outcome(
    outcome_kind: ResumeOutcomeKind = ResumeOutcomeKind.RESUME_CLEAN,
    diff: tuple[MaterialDiff, ...] = (),
) -> ResumeOutcome:
    return ResumeOutcome(
        outcome_kind=outcome_kind,
        material_diff=diff,
        context_revalidated=outcome_kind == ResumeOutcomeKind.RESUME_AFTER_REVALIDATION,
        resume_audit_entry_id=EntryID("e1") if diff == () else None,
    )


# ----------------------------------------------------------------------------
# AC #10 — helpers importable
# ----------------------------------------------------------------------------


def test_helpers_importable() -> None:
    """AC #10 — both module-level helpers resolve from harness_od.pause_resume_namespace."""
    assert callable(_project_pause_event_to_audit_payload)
    assert callable(_project_resume_outcome_to_audit_payload)


# ----------------------------------------------------------------------------
# AC #6 — _project_pause_event_to_audit_payload
# ----------------------------------------------------------------------------


def test_project_pause_event_action_id_pattern() -> None:
    """AC #6 + §C-OD-30.4.1 step 2 — action_id is `pause:<workflow_id>:<step_index>`."""
    payload = _project_pause_event_to_audit_payload(
        _pause_event(),
        workflow_id="w-test-0",
        step_index=7,
        snapshot_hash="a" * 64,
        state_ledger_anchor="b" * 64,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_action_id == "pause:w-test-0:7"
    assert payload.audit_cp_action_id.startswith("pause:")


def test_project_pause_event_response_constant() -> None:
    """AC #6 + §C-OD-30.4.1 step 3 — audit_cp_response hard-coded to "paused"."""
    payload = _project_pause_event_to_audit_payload(
        _pause_event(),
        workflow_id="w-test-0",
        step_index=0,
        snapshot_hash="a" * 64,
        state_ledger_anchor="b" * 64,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_response == "paused"


def test_project_pause_event_nulls_resume_path_fields() -> None:
    """AC #6 + AC #8 + §C-OD-30.4.1 step 8 — resume-path fields are None on pause."""
    payload = _project_pause_event_to_audit_payload(
        _pause_event(),
        workflow_id="w-test-0",
        step_index=0,
        snapshot_hash="a" * 64,
        state_ledger_anchor="b" * 64,
        prior_event_hash="0" * 64,
    )
    assert payload.diff_detected is None
    assert payload.diff_policy is None
    assert payload.diff_summary_hash is None
    assert payload.resume_outcome is None


def test_project_pause_event_round_trip_fields() -> None:
    """AC #6 — all populated fields land byte-exact from inputs.

    `pause_reason` serializes from the StrEnum value (not the enum class) per
    §C-OD-30.2 `pause_reason: str | None` field typing.
    """
    payload = _project_pause_event_to_audit_payload(
        _pause_event(PauseReason.OPERATOR_INITIATED_PAUSE),
        workflow_id="w-round-trip",
        step_index=42,
        snapshot_hash="f" * 64,
        state_ledger_anchor="9" * 64,
        prior_event_hash="3" * 64,
        timestamp="2026-05-24T00:00:00Z",
    )
    assert payload.audit_cp_action_id == "pause:w-round-trip:42"
    assert payload.audit_cp_timestamp == "2026-05-24T00:00:00Z"
    assert payload.audit_cp_prior_event_hash == "3" * 64
    assert payload.snapshot_hash == "f" * 64
    assert payload.step_index == 42
    assert payload.pause_reason == "operator-initiated-pause"
    assert payload.state_ledger_anchor == "9" * 64


# ----------------------------------------------------------------------------
# AC #7 — _project_resume_outcome_to_audit_payload (4 outcome-kind cases)
# ----------------------------------------------------------------------------


def test_project_resume_outcome_action_id_pattern() -> None:
    """AC #7 + §C-OD-30.4.1 step 2 — action_id is `resume:<workflow_id>:<step_index>`.

    workflow_id extracted from `attempt.paused_workflow_id` per §C-OD-30.4 helper-
    signature rationale.
    """
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(),
        step_index=11,
        snapshot_hash="a" * 64,
        diff_summary_hash=None,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_action_id == "resume:w-test-0:11"
    assert payload.audit_cp_action_id.startswith("resume:")


def test_project_resume_outcome_response_resume_clean() -> None:
    """AC #7 + §C-OD-30.4.1 step 3 — RESUME_CLEAN → "resumed"."""
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(ResumeOutcomeKind.RESUME_CLEAN),
        step_index=0,
        snapshot_hash="a" * 64,
        diff_summary_hash=None,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_response == "resumed"
    assert payload.diff_detected is False
    assert payload.diff_policy is None
    assert payload.resume_outcome == "resume-clean"


def test_project_resume_outcome_response_resume_after_revalidation() -> None:
    """AC #7 — RESUME_AFTER_REVALIDATION → "resumed" (revalidation succeeded)."""
    diff = (
        MaterialDiff(
            reference=_ref(),
            prior_snapshot=b"a",
            current_value=b"b",
            is_material=True,
        ),
    )
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(ResumeOutcomeKind.RESUME_AFTER_REVALIDATION, diff),
        step_index=3,
        snapshot_hash="a" * 64,
        diff_summary_hash="c" * 64,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_response == "resumed"
    assert payload.diff_detected is True
    assert payload.diff_policy == "resume-after-revalidation"
    assert payload.diff_summary_hash == "c" * 64
    assert payload.resume_outcome == "resume-after-revalidation"


def test_project_resume_outcome_response_abort_revalidation_failed() -> None:
    """AC #7 — ABORT_REVALIDATION_FAILED → "diff_detected"."""
    diff = (
        MaterialDiff(
            reference=_ref(),
            prior_snapshot=b"a",
            current_value=b"b",
            is_material=True,
        ),
    )
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(ResumeOutcomeKind.ABORT_REVALIDATION_FAILED, diff),
        step_index=5,
        snapshot_hash="a" * 64,
        diff_summary_hash="d" * 64,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_response == "diff_detected"
    assert payload.diff_detected is True
    assert payload.diff_policy == "abort-revalidation-failed"
    assert payload.resume_outcome == "abort-revalidation-failed"


def test_project_resume_outcome_response_abort_snapshot_corrupted() -> None:
    """AC #7 — ABORT_SNAPSHOT_CORRUPTED → "diff_detected" (integrity-failure)."""
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED),
        step_index=8,
        snapshot_hash="a" * 64,
        diff_summary_hash=None,
        prior_event_hash="0" * 64,
    )
    assert payload.audit_cp_response == "diff_detected"
    assert payload.diff_detected is True
    assert payload.diff_policy == "abort-snapshot-corrupted"
    assert payload.resume_outcome == "abort-snapshot-corrupted"


def test_project_resume_outcome_nulls_pause_path_fields() -> None:
    """AC #7 + AC #8 + §C-OD-30.4.1 step 8 — pause-path fields are None on resume."""
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(),
        step_index=0,
        snapshot_hash="a" * 64,
        diff_summary_hash=None,
        prior_event_hash="0" * 64,
    )
    assert payload.pause_reason is None
    assert payload.state_ledger_anchor is None


def test_project_resume_outcome_round_trip_fields() -> None:
    """AC #7 — all populated fields land byte-exact from inputs."""
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(ResumeOutcomeKind.RESUME_CLEAN),
        step_index=99,
        snapshot_hash="e" * 64,
        diff_summary_hash=None,
        prior_event_hash="2" * 64,
        timestamp="2026-05-24T01:00:00Z",
    )
    assert payload.audit_cp_action_id == "resume:w-test-0:99"
    assert payload.audit_cp_timestamp == "2026-05-24T01:00:00Z"
    assert payload.audit_cp_prior_event_hash == "2" * 64
    assert payload.snapshot_hash == "e" * 64
    assert payload.step_index == 99


# ----------------------------------------------------------------------------
# AC #10 — sentinel values + invariants
# ----------------------------------------------------------------------------


def test_project_pause_event_timestamp_mvp_sentinel() -> None:
    """AC #10 — empty-string timestamp MVP sentinel flows through unchanged."""
    payload = _project_pause_event_to_audit_payload(
        _pause_event(),
        workflow_id="w-0",
        step_index=0,
        snapshot_hash="a" * 64,
        state_ledger_anchor="b" * 64,
        prior_event_hash="0" * 64,
        # timestamp omitted → default ""
    )
    assert payload.audit_cp_timestamp == ""


def test_project_pause_event_prior_event_hash_zero_sentinel() -> None:
    """AC #10 — zero-hash sentinel "0"*64 flows through unchanged."""
    zero_hash = "0" * 64
    payload = _project_pause_event_to_audit_payload(
        _pause_event(),
        workflow_id="w-0",
        step_index=0,
        snapshot_hash="a" * 64,
        state_ledger_anchor="b" * 64,
        prior_event_hash=zero_hash,
    )
    assert payload.audit_cp_prior_event_hash == zero_hash


def test_project_resume_outcome_diff_summary_hash_optional() -> None:
    """AC #10 — diff_summary_hash kwarg of None flows through to None field."""
    payload = _project_resume_outcome_to_audit_payload(
        _resume_attempt(),
        _resume_outcome(),
        step_index=0,
        snapshot_hash="a" * 64,
        diff_summary_hash=None,
        prior_event_hash="0" * 64,
    )
    assert payload.diff_summary_hash is None


def test_helpers_produce_frozen_payloads() -> None:
    """AC #10 — output PauseResumeAuditPayload is frozen per Sub-arc A
    `ConfigDict(extra="forbid", frozen=True)`; field-assign raises."""
    payload = _project_pause_event_to_audit_payload(
        _pause_event(),
        workflow_id="w-0",
        step_index=0,
        snapshot_hash="a" * 64,
        state_ledger_anchor="b" * 64,
        prior_event_hash="0" * 64,
    )
    assert isinstance(payload, PauseResumeAuditPayload)
    with pytest.raises(ValidationError):
        payload.audit_cp_response = "tampered"  # type: ignore[misc]
