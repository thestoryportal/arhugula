"""Tests for U-CP-64 — C-CP-26 PauseResumeProtocol.attempt_resume() +
material-diff detection.

ACs from CP plan v2.17 §3 U-CP-64 (preserved verbatim from v2.15):
  AC #1 Snapshot hash validated on resume; corruption →
        CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION
  AC #2 Material diff detected when state_ledger_anchor no longer reachable
        from current entry chain (MVP: anchor equality check)
  AC #3 STRICT policy: diff → CP-FAIL-RESUME-MATERIAL-DIFF-DETECTED
  AC #4 OPERATOR_ARBITRATE policy: diff → CP-FAIL-RESUME-OPERATOR-ARBITRATION-OWED
        + HITL escalation (fail-class marker emitted; gate-open is downstream)
  AC #5 Coexist with U-CP-56 prefix-replay-based resumption (Path A-modified
        preserved)
"""

from __future__ import annotations

import asyncio

from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import (
    CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION,
    CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED,
    CP_FAIL_RESUME_OPERATOR_ARBITRATION_OWED,
    PauseResumeProtocol,
)
from harness_cp.pause_resume_protocol_types import (
    MaterialDiffPolicy,
    PauseSnapshot,
    ResumeContext,
    ResumeResult,
    WorkflowPauseReason,
)
from harness_is.state_ledger_entry_schema import Identifier


def _build_state_summary(summary_text: str = "test-state") -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text=summary_text,
        summary_hash="0" * 64,
        idempotency_key=Identifier("idem-1"),
        external_references=(),
    )


def _capture_then_protocol(
    *,
    capture_anchor: str = "a" * 64,
    resume_anchor: str | None = None,
    state_summary: StateSummary | None = None,
) -> tuple[PauseSnapshot, PauseResumeProtocol]:
    """Capture a snapshot at one anchor, then build a protocol seeing a
    potentially-different anchor at resume time. Returns (snapshot, protocol_at_resume).
    """
    summary = state_summary if state_summary is not None else _build_state_summary()
    capture_protocol = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, capture_anchor),
    )
    snapshot = asyncio.run(
        capture_protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    resume_protocol = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (
            summary,
            resume_anchor if resume_anchor is not None else capture_anchor,
        ),
    )
    return snapshot, resume_protocol


# --- AC #1 — snapshot_hash validation --------------------------------------


def test_attempt_resume_returns_resume_result() -> None:
    """Sanity — attempt_resume returns a ResumeResult instance."""
    snapshot, protocol = _capture_then_protocol()
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert isinstance(result, ResumeResult)


def test_corrupt_snapshot_yields_corruption_fail_class() -> None:
    """AC #1 — snapshot_hash mismatch → CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION."""
    snapshot, protocol = _capture_then_protocol()
    # Corrupt the snapshot by constructing a tampered copy with a bad hash
    corrupted = snapshot.model_copy(update={"snapshot_hash": "f" * 64})
    result = asyncio.run(
        protocol.attempt_resume(corrupted, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result.resumed is False
    assert result.diff_detected is False
    assert result.fail_class == CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION
    assert result.diff_summary_hash is None


def test_intact_snapshot_passes_hash_validation() -> None:
    """AC #1 — intact snapshot validates and proceeds past hash check."""
    snapshot, protocol = _capture_then_protocol()
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    # Hash valid → not corruption fail class
    assert result.fail_class != CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION


# --- AC #2 — material diff via anchor divergence ---------------------------


def test_no_diff_when_anchor_unchanged_yields_clean_resume() -> None:
    """AC #2 — same anchor at capture + resume → no diff → clean resume."""
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="a" * 64)
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result.resumed is True
    assert result.diff_detected is False
    assert result.fail_class is None
    assert result.diff_summary_hash is None


def test_diff_detected_when_anchor_changed() -> None:
    """AC #2 — anchor divergence → diff_detected=True at any policy."""
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result.diff_detected is True


# --- AC #3 — STRICT policy aborts on diff ----------------------------------


def test_strict_policy_diff_yields_material_diff_fail_class() -> None:
    """AC #3 — STRICT + diff → CP-FAIL-RESUME-MATERIAL-DIFF-DETECTED."""
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result.resumed is False
    assert result.diff_detected is True
    assert result.fail_class == CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED
    assert result.diff_summary_hash is not None
    assert len(result.diff_summary_hash) == 64


# --- AC #4 — OPERATOR_ARBITRATE escalates ----------------------------------


def test_operator_arbitrate_policy_diff_yields_arbitration_fail_class() -> None:
    """AC #4 — OPERATOR_ARBITRATE + diff → CP-FAIL-RESUME-OPERATOR-ARBITRATION-OWED."""
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    result = asyncio.run(
        protocol.attempt_resume(
            snapshot,
            material_diff_policy=MaterialDiffPolicy.OPERATOR_ARBITRATE,
        )
    )
    assert result.resumed is False
    assert result.diff_detected is True
    assert result.fail_class == CP_FAIL_RESUME_OPERATOR_ARBITRATION_OWED
    assert result.diff_summary_hash is not None


# --- LENIENT policy — diff permitted, resumption proceeds ------------------


def test_lenient_policy_diff_permits_resumption() -> None:
    """LENIENT policy permits resumption despite diff (§26.2 'only behavior-
    changing diff abort'; MVP treats all diffs as non-behavior-changing under
    LENIENT). diff_detected=True marker carried for caller awareness; no
    fail-class set."""
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.LENIENT)
    )
    assert result.resumed is True
    assert result.diff_detected is True
    assert result.fail_class is None
    assert result.diff_summary_hash is not None


# --- AC #5 — coexist with U-CP-56 prefix-replay-based resumption ----------


def test_attempt_resume_does_not_invoke_engine_layer_classify_resume() -> None:
    """AC #5 — explicit-pause resumption (C-CP-26) is non-overlapping with
    prefix-replay resumption (U-CP-56) and the engine-layer C-CP-22
    classify_resume free function. The two paths are distinct; verify
    attempt_resume does not delegate to classify_resume.
    """
    # Sanity: classify_resume still importable + functional (preserved verbatim)
    from harness_cp.pause_resume_protocol import (
        ResumeOutcomeKind,
        classify_resume,
    )

    assert classify_resume((), revalidation_succeeded=True) is ResumeOutcomeKind.RESUME_CLEAN

    # attempt_resume (workflow-layer) returns ResumeResult, not ResumeOutcomeKind
    snapshot, protocol = _capture_then_protocol()
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert isinstance(result, ResumeResult)
    assert not isinstance(result, ResumeOutcomeKind)


# --- diff_summary_hash determinism + content shape -------------------------


def test_diff_summary_hash_deterministic() -> None:
    """diff_summary_hash deterministic across equal (snapshot_anchor, current_anchor) pairs."""
    snapshot_a, protocol_a = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    snapshot_b, protocol_b = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    result_a = asyncio.run(
        protocol_a.attempt_resume(snapshot_a, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    result_b = asyncio.run(
        protocol_b.attempt_resume(snapshot_b, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result_a.diff_summary_hash == result_b.diff_summary_hash


def test_diff_summary_hash_changes_with_current_anchor() -> None:
    """diff_summary_hash distinguishes (snapshot_anchor, current_anchor) pairs."""
    snapshot_a, protocol_a = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="b" * 64)
    snapshot_c, protocol_c = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="c" * 64)
    result_a = asyncio.run(
        protocol_a.attempt_resume(snapshot_a, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    result_c = asyncio.run(
        protocol_c.attempt_resume(snapshot_c, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result_a.diff_summary_hash != result_c.diff_summary_hash


# --- ResumeResult frozen guard ---------------------------------------------


def test_resume_result_is_frozen() -> None:
    """ResumeResult model_config = frozen=True."""
    import pytest
    from pydantic import ValidationError

    snapshot, protocol = _capture_then_protocol()
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    with pytest.raises(ValidationError):
        result.resumed = False  # type: ignore[misc]


# --- AC #6 (v2.21) — attempt_resume accepts ResumeContext keyword-only param


def test_attempt_resume_accepts_resume_context_none_default() -> None:
    """AC #6 — `attempt_resume` accepts no resume_context arg (None default).

    Backward-compat per CP spec v1.16 §26.8.5: callers passing no
    `resume_context` receive None default → identical control flow to
    pre-v1.16 baseline. Verified against the same clean-resume path covered
    at `test_no_diff_when_anchor_unchanged_yields_clean_resume`.
    """
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="a" * 64)
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    assert result.resumed is True
    assert result.diff_detected is False


def test_attempt_resume_accepts_explicit_resume_context_none() -> None:
    """AC #6 — `attempt_resume(..., resume_context=None)` is a no-op vs default."""
    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="a" * 64)
    result = asyncio.run(
        protocol.attempt_resume(
            snapshot,
            material_diff_policy=MaterialDiffPolicy.STRICT,
            resume_context=None,
        )
    )
    assert result.resumed is True


def test_attempt_resume_ingests_populated_resume_context_without_consuming() -> None:
    """AC #6 — `attempt_resume` ingests `ResumeContext(hitl_response=...)` but
    does NOT consume it at v1.16 per CP spec v1.16 §26.8.5 method-body-posture
    framing. Behavior identical to None default: the CP-side method carries
    the parameter through; runtime-side is the propagation site.
    """
    from harness_core.identity import EntryID
    from harness_cp.hitl_placement import HITLResult
    from harness_cp.hitl_response_palette import HITLResponse

    snapshot, protocol = _capture_then_protocol(capture_anchor="a" * 64, resume_anchor="a" * 64)
    hitl = HITLResult(
        response=HITLResponse.APPROVE,
        timestamp="2026-05-24T00:00:00Z",
        audit_ledger_entry_id=EntryID("e-rc-1"),
        response_summary_hash="0" * 64,
    )
    rc = ResumeContext(hitl_response=hitl)
    result_with = asyncio.run(
        protocol.attempt_resume(
            snapshot,
            material_diff_policy=MaterialDiffPolicy.STRICT,
            resume_context=rc,
        )
    )
    result_without = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    # Identical control flow at v1.16 — CP-side ingests but does not consume.
    assert result_with.resumed == result_without.resumed
    assert result_with.diff_detected == result_without.diff_detected
    assert result_with.fail_class == result_without.fail_class


def test_attempt_resume_resume_context_is_keyword_only() -> None:
    """AC #6 — `resume_context` is keyword-only per CP spec v1.16 §26.8.5
    canonical signature reading.
    """
    import pytest

    snapshot, protocol = _capture_then_protocol()
    # Positional invocation must fail (kw-only after `*`).
    with pytest.raises(TypeError):
        asyncio.run(
            protocol.attempt_resume(
                snapshot,
                MaterialDiffPolicy.STRICT,  # type: ignore[misc]
                None,
            )
        )
