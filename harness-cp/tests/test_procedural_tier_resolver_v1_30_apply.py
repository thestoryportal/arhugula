"""Tests for CP spec v1.30 §1.2 uniform-resolver-closure apply pass (Reading C).

Coverage:
  v1.30 §1.1 — uniform `procedural_tier_snapshot_resolver` kw-only param across all 6 composers
  v1.30 §1.2 — resolver invoked at composer body; populates `EntryPayload.procedural_tier_snapshot_ref`
  v1.30 §1.5 — composer-site HALT on resolver-raise (uniform across all 6)

Per `.harness/class_1_fork_pr_2_workflow_layer_ctx_access_recipe_underspecified.md`
Reading (C) operator-ratified 2026-05-31.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_as_tool_call_rewriting import (
    HITLSemanticVariant,
    RewrittenToolCall,
    emit_hitl_tool_call_rewriting_state_ledger_entry,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.pause_resume_protocol import (
    PauseEvent,
    PauseReason,
    PauseResumeProtocolEventKind,
    ResumeOutcome,
    ResumeOutcomeKind,
    emit_pause_captured_state_ledger_entry,
    emit_pause_resume_state_ledger_entry,
    emit_resume_attempted_state_ledger_entry,
)
from harness_cp.pause_resume_protocol_types import (
    StateSummary,
)
from harness_cp.per_step_override_evaluator import emit_override_state_ledger_entry
from harness_cp.workload_binding_engine_class_selection import (
    WorkloadBindingSelectionResult,
    emit_workload_class_selection_state_ledger_entry,
)
from harness_is.state_ledger_entry_schema import Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult

_EXPECTED_SNAPSHOT_REF = Identifier("b" * 64)
"""Fixture: the Identifier value the resolver returns at apply-pass tests."""


def _resolver() -> Identifier:
    """v1.30 §1.4 zero-arg resolver closure returning the apply-pass fixture."""
    return _EXPECTED_SNAPSHOT_REF


class _CapturingWriter:
    """Async ledger_writer stub capturing the payload."""

    def __init__(self) -> None:
        self.captured: list[EntryPayload] = []

    async def __call__(self, payload: EntryPayload) -> WriteResult:
        self.captured.append(payload)
        return WriteResult.APPENDED


def _state_summary() -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text="snap-v1",
        summary_hash=hashlib.sha256(b"snap-v1").hexdigest(),
        idempotency_key=Identifier("idem-snap-v1"),
        external_references=(),
    )


def _pause_event() -> PauseEvent:
    return PauseEvent(
        paused_at="2023-11-14T22:13:20+00:00",
        pause_reason=PauseReason.OPERATOR_INITIATED_PAUSE,
        state_summary_snapshot=_state_summary(),
        external_refs_captured=(),
        pause_audit_entry_id=Identifier("pause-1"),
    )


# --- v1.30 §1.1 signature-shape acceptance ---


_ALL_SIX_COMPOSERS = (
    emit_override_state_ledger_entry,
    emit_workload_class_selection_state_ledger_entry,
    emit_pause_resume_state_ledger_entry,
    emit_hitl_tool_call_rewriting_state_ledger_entry,
    emit_pause_captured_state_ledger_entry,
    emit_resume_attempted_state_ledger_entry,
)


def test_all_six_composers_accept_procedural_tier_snapshot_resolver_kwonly() -> None:
    """v1.30 §1.1 — uniform kw-only param across all 6 composers."""
    for composer in _ALL_SIX_COMPOSERS:
        sig = inspect.signature(composer)
        assert "procedural_tier_snapshot_resolver" in sig.parameters, (
            f"{composer.__name__} missing procedural_tier_snapshot_resolver per v1.30 §1.1"
        )
        param = sig.parameters["procedural_tier_snapshot_resolver"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{composer.__name__} resolver param must be kw-only per v1.30 §1.3"
        )
        assert param.default is inspect.Parameter.empty, (
            f"{composer.__name__} resolver param must be REQUIRED (no default) per v1.30 §1.3"
        )


# --- v1.30 §1.2 resolver invocation + sidecar population (6 per-composer cases) ---


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_u_cp_14_override_populates_sidecar_via_resolver() -> None:
    writer = _CapturingWriter()
    _run(
        emit_override_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-2",
            post_override_step_config={"model": "claude-opus"},
            actor=ActorIdentity("cp"),
            ledger_writer=writer,
            procedural_tier_snapshot_resolver=_resolver,
        )
    )
    assert writer.captured[0].procedural_tier_snapshot_ref == _EXPECTED_SNAPSHOT_REF


def test_u_cp_27_workload_class_selection_populates_sidecar() -> None:
    writer = _CapturingWriter()
    selection = WorkloadBindingSelectionResult(
        selected_class=EngineClass.SAVE_POINT_CHECKPOINT,
        candidate_set=frozenset(
            {EngineClass.SAVE_POINT_CHECKPOINT, EngineClass.PURE_PATTERN_NO_ENGINE}
        ),
        selection_rationale="apply-pass fixture",
    )
    _run(
        emit_workload_class_selection_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-2",
            selection_result=selection,
            actor=ActorIdentity("cp"),
            ledger_writer=writer,
            procedural_tier_snapshot_resolver=_resolver,
        )
    )
    assert writer.captured[0].procedural_tier_snapshot_ref == _EXPECTED_SNAPSHOT_REF


def test_u_cp_30_pause_resume_workflow_layer_populates_sidecar() -> None:
    writer = _CapturingWriter()
    _run(
        emit_pause_resume_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-2",
            protocol_event_kind=PauseResumeProtocolEventKind.PAUSE_CAPTURED,
            event_sequence_id=1,
            protocol_state_snapshot={"k": "v"},
            actor=ActorIdentity("cp"),
            ledger_writer=writer,
            procedural_tier_snapshot_resolver=_resolver,
        )
    )
    assert writer.captured[0].procedural_tier_snapshot_ref == _EXPECTED_SNAPSHOT_REF


def test_u_cp_37_hitl_tool_call_rewriting_populates_sidecar() -> None:
    writer = _CapturingWriter()
    rewritten = RewrittenToolCall(
        tool="send_email",
        server="mcp-mail",
        hitl_required=True,
        variant=HITLSemanticVariant.AWAIT_HUMAN_APPROVAL,
        response_palette=frozenset(HITLResponse),
    )
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-2",
            tool_call_id="call-1",
            semantic_variant_binding_id="row-2",
            rewritten_tool_call=rewritten,
            actor=ActorIdentity("cp"),
            ledger_writer=writer,
            procedural_tier_snapshot_resolver=_resolver,
        )
    )
    assert writer.captured[0].procedural_tier_snapshot_ref == _EXPECTED_SNAPSHOT_REF


def test_u_cp_49_pause_captured_engine_layer_populates_sidecar() -> None:
    writer = _CapturingWriter()
    _run(
        emit_pause_captured_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-7",
            pause_event=_pause_event(),
            actor=ActorIdentity("engine"),
            ledger_writer=writer,
            procedural_tier_snapshot_resolver=_resolver,
        )
    )
    assert writer.captured[0].procedural_tier_snapshot_ref == _EXPECTED_SNAPSHOT_REF


def test_u_cp_50_resume_attempted_engine_layer_populates_sidecar() -> None:
    writer = _CapturingWriter()
    outcome = ResumeOutcome(
        outcome_kind=ResumeOutcomeKind.RESUME_CLEAN,
        material_diff=(),
        context_revalidated=False,
        resume_audit_entry_id=None,
    )
    _run(
        emit_resume_attempted_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-7",
            resume_event_id="resume-1",
            resume_attempt_count=1,
            resume_outcome=outcome,
            actor=ActorIdentity("engine"),
            ledger_writer=writer,
            procedural_tier_snapshot_resolver=_resolver,
        )
    )
    assert writer.captured[0].procedural_tier_snapshot_ref == _EXPECTED_SNAPSHOT_REF


# --- v1.30 §1.5 HALT-on-resolver-failure posture (uniform across all 6) ---


class _ResolverFailure(Exception):
    """Stand-in for ProceduralTierResolutionError per v1.30 §1.5."""


def _raising_resolver() -> Identifier:
    raise _ResolverFailure("v1.30 §1.5 HALT posture probe")


def test_u_cp_14_override_halts_on_resolver_raise() -> None:
    """v1.30 §1.5: composer propagates resolver-raise to caller (HALT)."""
    writer = _CapturingWriter()
    with pytest.raises(_ResolverFailure):
        _run(
            emit_override_state_ledger_entry(
                workflow_id="wf-1",
                step_id="step-2",
                post_override_step_config={"model": "claude-opus"},
                actor=ActorIdentity("cp"),
                ledger_writer=writer,
                procedural_tier_snapshot_resolver=_raising_resolver,
            )
        )
    assert writer.captured == [], "resolver-raise must HALT before ledger_writer is awaited"


def test_u_cp_49_pause_captured_halts_on_resolver_raise() -> None:
    """v1.30 §1.5 engine-layer composer mirrors workflow-layer HALT posture."""
    writer = _CapturingWriter()
    with pytest.raises(_ResolverFailure):
        _run(
            emit_pause_captured_state_ledger_entry(
                workflow_id="wf-1",
                step_id="step-7",
                pause_event=_pause_event(),
                actor=ActorIdentity("engine"),
                ledger_writer=writer,
                procedural_tier_snapshot_resolver=_raising_resolver,
            )
        )
    assert writer.captured == []


# --- type-shape probe for the kw-only contract ---


def test_resolver_param_callable_zero_arg_returns_identifier() -> None:
    """v1.30 §1.4 type-shape: Callable[[], Identifier]."""
    _: Callable[[], Identifier] = _resolver
    assert _resolver() == _EXPECTED_SNAPSHOT_REF


def test_writer_signature_unchanged_at_v1_30() -> None:
    """v1.30 preserves §16.5.7 ledger_writer floor verbatim at all 6 composers."""
    expected_writer_type = Callable[[EntryPayload], Awaitable[WriteResult]]
    _ = expected_writer_type  # documentation marker; runtime test below
    for composer in _ALL_SIX_COMPOSERS:
        sig = inspect.signature(composer)
        assert "ledger_writer" in sig.parameters, (
            f"{composer.__name__} must still accept ledger_writer per v1.30 preservation"
        )
        assert sig.parameters["ledger_writer"].kind == inspect.Parameter.KEYWORD_ONLY
