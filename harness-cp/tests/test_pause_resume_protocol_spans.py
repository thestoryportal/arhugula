"""Tests for U-CP-65 — pause.captured + resume.attempted span emission.

ACs from CP plan v2.17 §4 (= v2.15 U-CP-65 preserved verbatim; only soft-dep
note re-anchored to v2.17 at spec citations):
  AC #1 `pause.captured` span emits with 4 attributes per OD spec v1.9 §C-OD-30.1
  AC #2 `resume.attempted` span emits with 4 attributes per §C-OD-30.1
  AC #3 Both spans head=1.0 (always-sampled per §26.4)
  AC #4 Span attribute names match OD canonical schema byte-exact (Pattern-P1
        alignment)
  AC #5 Integration test: pause + resume + verify span emission via OTel test
        collector

Soft-dep on U-OD-51 per CP plan v2.17 §4 (preserved from v2.15) — runtime
emits attribute-name string literals; OD schema module NOT imported at
runtime. This test verifies byte-exact alignment via string-literal
comparison.
"""

from __future__ import annotations

import asyncio

import pytest
from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import (
    PauseResumeProtocol,
    emit_pause_captured_span,
    emit_resume_attempted_span,
)
from harness_cp.pause_resume_protocol_types import (
    MaterialDiffPolicy,
    WorkflowPauseReason,
)
from harness_is.state_ledger_entry_schema import Identifier
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture
def exporter_and_provider() -> tuple[InMemorySpanExporter, TracerProvider]:
    """Per-test isolated TracerProvider + in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def _build_state_summary() -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text="span-test",
        summary_hash="0" * 64,
        idempotency_key=Identifier("idem-1"),
        external_references=(),
    )


def _build_protocol(anchor: str = "a" * 64) -> PauseResumeProtocol:
    summary = _build_state_summary()
    return PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, anchor),
    )


def _find_span(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    """Locate a span by name; assert exactly one match."""
    matches = [s for s in spans if s.name == name]
    assert len(matches) == 1, f"Expected exactly one {name!r} span; got {len(matches)}"
    return matches[0]


# --- OD canonical schema string literals (Pattern-P1 alignment ground truth) -

PAUSE_CAPTURED_ATTRS: frozenset[str] = frozenset(
    {
        "pause.reason",
        "pause.snapshot_hash",
        "pause.step_index",
        "pause.state_ledger_anchor",
    }
)

RESUME_ATTEMPTED_ATTRS: frozenset[str] = frozenset(
    {
        "resume.snapshot_hash",
        "resume.diff_detected",
        "resume.diff_policy",
        "resume.outcome",
    }
)


# --- AC #1 — pause.captured span ------------------------------------------


def test_pause_captured_span_emits(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #1 — pause.captured span emits with 4 attributes per §C-OD-30.1."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    protocol = _build_protocol(anchor="f" * 64)
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=3,
            pause_reason=WorkflowPauseReason.HITL_PENDING,
        )
    )
    emit_pause_captured_span(snapshot, tracer=tracer)

    spans = exporter.get_finished_spans()
    span = _find_span(list(spans), "pause.captured")
    assert span.attributes is not None
    assert set(span.attributes.keys()) == PAUSE_CAPTURED_ATTRS
    assert span.attributes["pause.reason"] == "hitl_pending"
    assert span.attributes["pause.snapshot_hash"] == snapshot.snapshot_hash
    assert span.attributes["pause.step_index"] == 3
    assert span.attributes["pause.state_ledger_anchor"] == "f" * 64


def test_pause_captured_attribute_names_byte_exact(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #4 — Pattern-P1 byte-exact alignment with OD canonical schema."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    protocol = _build_protocol()
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    emit_pause_captured_span(snapshot, tracer=tracer)

    spans = list(exporter.get_finished_spans())
    span = _find_span(spans, "pause.captured")
    # Exact frozenset equality — no extras, no missing
    assert span.attributes is not None
    assert set(span.attributes.keys()) == PAUSE_CAPTURED_ATTRS


# --- AC #2 — resume.attempted span ----------------------------------------


def test_resume_attempted_span_clean_resume(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #2 — resume.attempted span emits with 4 attributes; clean-resume
    outcome = "resumed"."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    protocol = _build_protocol(anchor="a" * 64)
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    emit_resume_attempted_span(
        snapshot, result, tracer=tracer, diff_policy=MaterialDiffPolicy.STRICT
    )

    spans = list(exporter.get_finished_spans())
    span = _find_span(spans, "resume.attempted")
    assert span.attributes is not None
    assert set(span.attributes.keys()) == RESUME_ATTEMPTED_ATTRS
    assert span.attributes["resume.snapshot_hash"] == snapshot.snapshot_hash
    assert span.attributes["resume.diff_detected"] is False
    assert span.attributes["resume.diff_policy"] == "strict"
    assert span.attributes["resume.outcome"] == "resumed"


def test_resume_attempted_span_diff_aborted_outcome(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #2 — STRICT + diff yields resume.outcome = "diff_aborted"."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    # Capture at one anchor, resume at a different anchor → diff detected
    summary = _build_state_summary()
    capture_proto = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, "a" * 64),
    )
    snapshot = asyncio.run(
        capture_proto.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    resume_proto = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, "b" * 64),
    )
    result = asyncio.run(
        resume_proto.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    emit_resume_attempted_span(
        snapshot, result, tracer=tracer, diff_policy=MaterialDiffPolicy.STRICT
    )

    spans = list(exporter.get_finished_spans())
    span = _find_span(spans, "resume.attempted")
    assert span.attributes is not None
    assert span.attributes["resume.outcome"] == "diff_aborted"
    assert span.attributes["resume.diff_detected"] is True


def test_resume_attempted_span_arbitration_owed_outcome(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #2 — OPERATOR_ARBITRATE + diff yields resume.outcome = "arbitration_owed"."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    summary = _build_state_summary()
    capture_proto = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, "a" * 64),
    )
    snapshot = asyncio.run(
        capture_proto.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    resume_proto = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, "b" * 64),
    )
    result = asyncio.run(
        resume_proto.attempt_resume(
            snapshot,
            material_diff_policy=MaterialDiffPolicy.OPERATOR_ARBITRATE,
        )
    )
    emit_resume_attempted_span(
        snapshot,
        result,
        tracer=tracer,
        diff_policy=MaterialDiffPolicy.OPERATOR_ARBITRATE,
    )

    spans = list(exporter.get_finished_spans())
    span = _find_span(spans, "resume.attempted")
    assert span.attributes is not None
    assert span.attributes["resume.outcome"] == "arbitration_owed"
    assert span.attributes["resume.diff_policy"] == "operator_arbitrate"


def test_resume_attempted_attribute_names_byte_exact(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #4 — Pattern-P1 byte-exact alignment for resume.attempted."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    protocol = _build_protocol()
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.LENIENT)
    )
    emit_resume_attempted_span(
        snapshot, result, tracer=tracer, diff_policy=MaterialDiffPolicy.LENIENT
    )

    spans = list(exporter.get_finished_spans())
    span = _find_span(spans, "resume.attempted")
    assert span.attributes is not None
    assert set(span.attributes.keys()) == RESUME_ATTEMPTED_ATTRS


# --- AC #5 — integration: pause + resume + verify both spans ---------------


def test_integration_pause_then_resume_emits_both_spans(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #5 — full integration: capture pause + emit pause.captured span +
    attempt resume + emit resume.attempted span. Both spans observable via
    OTel in-memory collector with all 8 attributes Pattern-P1-aligned.
    """
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    protocol = _build_protocol(anchor="c" * 64)

    # Pause leg
    snapshot = asyncio.run(
        protocol.capture_pause_snapshot(
            workflow_id="wf-integration",
            run_id="run-integration",
            step_index=5,
            pause_reason=WorkflowPauseReason.VALIDATOR_ESCALATION,
        )
    )
    emit_pause_captured_span(snapshot, tracer=tracer)

    # Resume leg (clean — same anchor)
    result = asyncio.run(
        protocol.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.STRICT)
    )
    emit_resume_attempted_span(
        snapshot, result, tracer=tracer, diff_policy=MaterialDiffPolicy.STRICT
    )

    spans = list(exporter.get_finished_spans())
    assert len(spans) == 2

    pause_span = _find_span(spans, "pause.captured")
    resume_span = _find_span(spans, "resume.attempted")

    # Pause span — 4 attrs Pattern-P1 aligned
    assert pause_span.attributes is not None
    assert set(pause_span.attributes.keys()) == PAUSE_CAPTURED_ATTRS
    assert pause_span.attributes["pause.reason"] == "validator_escalation"
    assert pause_span.attributes["pause.snapshot_hash"] == snapshot.snapshot_hash
    assert pause_span.attributes["pause.step_index"] == 5
    assert pause_span.attributes["pause.state_ledger_anchor"] == "c" * 64

    # Resume span — 4 attrs Pattern-P1 aligned; clean resume
    assert resume_span.attributes is not None
    assert set(resume_span.attributes.keys()) == RESUME_ATTEMPTED_ATTRS
    assert resume_span.attributes["resume.snapshot_hash"] == snapshot.snapshot_hash
    assert resume_span.attributes["resume.diff_detected"] is False
    assert resume_span.attributes["resume.diff_policy"] == "strict"
    assert resume_span.attributes["resume.outcome"] == "resumed"


# --- LENIENT diff path — resumed=True + diff_detected=True -----------------


def test_resume_attempted_span_lenient_with_diff(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """LENIENT + diff → resume.outcome = "resumed" + resume.diff_detected = True.

    LENIENT permits resumption despite diff (§26.2). resume.outcome reflects
    that resumption succeeded; resume.diff_detected carries the marker for
    consumers to observe the diff was permitted-but-detected.
    """
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer(__name__)

    summary = _build_state_summary()
    capture_proto = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, "a" * 64),
    )
    snapshot = asyncio.run(
        capture_proto.capture_pause_snapshot(
            workflow_id="wf-1",
            run_id="run-1",
            step_index=0,
            pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
        )
    )
    resume_proto = PauseResumeProtocol(
        state_ledger_writer=object(),
        state_ledger_reader=object(),
        pause_context_reader=lambda: (summary, "b" * 64),
    )
    result = asyncio.run(
        resume_proto.attempt_resume(snapshot, material_diff_policy=MaterialDiffPolicy.LENIENT)
    )
    emit_resume_attempted_span(
        snapshot, result, tracer=tracer, diff_policy=MaterialDiffPolicy.LENIENT
    )

    spans = list(exporter.get_finished_spans())
    span = _find_span(spans, "resume.attempted")
    assert span.attributes is not None
    assert span.attributes["resume.outcome"] == "resumed"
    assert span.attributes["resume.diff_detected"] is True
    assert span.attributes["resume.diff_policy"] == "lenient"
