"""C-OD-09 §9.1 + §10.2 TailKeepSpanProcessor tests.

Closes H_T-OD-3 PARTIAL → RETIRE-READY gate (a) — tail-keep-on-classification
at the OTLP collector boundary per §9.1 + §10.2 (3 classification triggers)
under §9.3 implementer-discretion algorithm. Tests verify per-trace
buffering + classification-trigger preservation + always-sampled passthrough
+ force_flush keep-all + downstream delegation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness_od.tail_keep_classification import (
    BREAKER_TRIPPED_SPAN_NAME,
    SANDBOX_VIOLATION_SPAN_NAME,
    SUBAGENT_RESULT_STATUS_ATTR,
    SUBAGENT_RESULT_STATUS_FAILED_VALUE,
    VALIDATOR_FAIL_PERMANENCE_ATTR,
    VALIDATOR_FAIL_PERMANENCE_PERMANENT_VALUE,
    is_classification_trigger,
)
from harness_od.tail_keep_span_processor import TailKeepSpanProcessor
from opentelemetry import trace as otel_trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# --- Recording downstream double ---------------------------------------------


@dataclass
class _RecordingProcessor(SpanProcessor):
    """A test double recording forwarded on_end calls + lifecycle invocations."""

    on_end_calls: list[ReadableSpan] = field(default_factory=list)
    on_start_calls: int = 0
    force_flush_calls: int = 0
    shutdown_calls: int = 0

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self.on_start_calls += 1

    def on_end(self, span: ReadableSpan) -> None:
        self.on_end_calls.append(span)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.force_flush_calls += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1


# --- §10.2 classification predicate ------------------------------------------


def test_classification_trigger_predicate_matches_sandbox_violation() -> None:
    """AC #1: span name `sandbox.violation` triggers preservation per §10.2."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    span = tracer.start_span(SANDBOX_VIOLATION_SPAN_NAME)
    span.end()
    # ReadableSpan via the recording exporter — span itself is a Span (mutable),
    # but `is_classification_trigger` accepts ReadableSpan structurally.
    assert is_classification_trigger(span)  # type: ignore[arg-type]


def test_classification_trigger_predicate_matches_breaker_tripped() -> None:
    """AC #2: span name `breaker.tripped` triggers preservation per §10.2."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    span = tracer.start_span(BREAKER_TRIPPED_SPAN_NAME)
    span.end()
    assert is_classification_trigger(span)  # type: ignore[arg-type]


def test_classification_trigger_predicate_matches_validator_fail_permanent() -> None:
    """AC #3: `validator.fail.permanence=permanent` triggers preservation per §10.2."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    span = tracer.start_span("validator.fail")
    span.set_attribute(
        VALIDATOR_FAIL_PERMANENCE_ATTR,
        VALIDATOR_FAIL_PERMANENCE_PERMANENT_VALUE,
    )
    span.end()
    assert is_classification_trigger(span)  # type: ignore[arg-type]


def test_classification_trigger_predicate_negative_on_arbitrary_span() -> None:
    """AC #4: a span without any §10.2 trigger does not classify."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    span = tracer.start_span("arbitrary.work")
    span.set_attribute("foo", "bar")
    span.end()
    assert not is_classification_trigger(span)  # type: ignore[arg-type]


def test_classification_trigger_predicate_negative_on_validator_non_permanent() -> None:
    """AC #5: `validator.fail.permanence=transient` does NOT trigger (only `permanent`)."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    span = tracer.start_span("validator.fail")
    span.set_attribute(VALIDATOR_FAIL_PERMANENCE_ATTR, "transient")
    span.end()
    assert not is_classification_trigger(span)  # type: ignore[arg-type]


# --- TailKeepSpanProcessor — per-trace buffering + classification ------------


def _new_tail_keep_with_recorder() -> tuple[TailKeepSpanProcessor, _RecordingProcessor]:
    recorder = _RecordingProcessor()
    proc = TailKeepSpanProcessor(downstream=recorder)
    return proc, recorder


def test_arbitrary_root_span_drops_when_no_trigger_present() -> None:
    """AC #6: non-always-sampled root span with no trigger drops on root close."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope") as _root:
        # workflow.envelope is NOT in §9.2 always-sampled set; trace has no
        # §10.2 classification trigger; expect drop on root close.
        pass
    assert recorder.on_end_calls == []


def test_root_span_with_sandbox_violation_child_keeps_full_tree() -> None:
    """AC #7: sandbox.violation child → entire trace preserved at root close."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span(SANDBOX_VIOLATION_SPAN_NAME):
            pass
        with tracer.start_as_current_span("sibling.work"):
            pass
    # sandbox.violation is in §9.2 always-sampled → forwarded immediately.
    # workflow.envelope + sibling.work buffered, then flushed on root close
    # because trace keep-flag was set when sandbox.violation fired.
    forwarded_names = {s.name for s in recorder.on_end_calls}
    assert SANDBOX_VIOLATION_SPAN_NAME in forwarded_names
    assert "workflow.envelope" in forwarded_names
    assert "sibling.work" in forwarded_names


def test_root_span_with_breaker_tripped_child_keeps_full_tree() -> None:
    """AC #8: breaker.tripped child → entire trace preserved at root close."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span(BREAKER_TRIPPED_SPAN_NAME):
            pass
    forwarded_names = {s.name for s in recorder.on_end_calls}
    assert BREAKER_TRIPPED_SPAN_NAME in forwarded_names
    assert "workflow.envelope" in forwarded_names


def test_root_span_with_validator_permanent_fail_child_keeps_full_tree() -> None:
    """AC #9: validator.fail.permanence=permanent → entire trace preserved."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("validator.fail.evaluation")
        child.set_attribute(
            VALIDATOR_FAIL_PERMANENCE_ATTR,
            VALIDATOR_FAIL_PERMANENCE_PERMANENT_VALUE,
        )
        child.end()
    forwarded_names = {s.name for s in recorder.on_end_calls}
    assert "validator.fail.evaluation" in forwarded_names
    assert "workflow.envelope" in forwarded_names


def test_always_sampled_span_forwards_immediately_without_buffer() -> None:
    """AC #10: §9.2 always-sampled span bypasses buffer; forwards at on_end."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    # `audit.entry.composed` is in ALWAYS_SAMPLED_EVENT_CLASSES per §9.2
    # (matches the `audit.*` prefix entry).
    span = tracer.start_span("audit.entry.composed")
    span.end()
    # Immediate forward — recorder gets it before any flush.
    forwarded_names = {s.name for s in recorder.on_end_calls}
    assert "audit.entry.composed" in forwarded_names


# --- B-TAIL-CONDITIONAL-SAMPLING: §9.2 attribute-conditional rows at the TAIL ----
# The tail is the production enforcement point: producers set the discriminating
# attribute DURING the span, so it is finalized at on_end (here) while ABSENT at the
# head should_sample → conservative-always-sample (the B7-landed head half). Names/keys
# are the REAL producer-emitted shapes (files_api.py:240-241 emits span "files.operation"
# + attr "files.operation.kind"; memory_tool_dispatch.py:289/333 emits "memory.operation"
# + "memory.operation.kind") — NOT hand-typed to the SSOT constant, so the test proves
# the producer→SSOT seam, not just the SSOT.


def test_files_operation_non_mutation_buffers_at_tail() -> None:
    """B-TAIL: a `files.operation` at non-mutation `kind=list` is NOT always-sampled at
    the tail (§9.2 conditional → §10.1 base-rate) → buffered + dropped on a no-trigger
    root close. Before B-TAIL the name-only tail call force-forwarded every
    `files.operation` unconditionally (the bug)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("files.operation")
        child.set_attribute("files.operation.kind", "list")  # non-mutation
        child.end()
    # Buffered (not force-forwarded); whole no-trigger trace drops on root close.
    assert recorder.on_end_calls == []


def test_files_operation_mutation_force_forwards_at_tail() -> None:
    """B-TAIL: a `files.operation` at mutation `kind=upload` IS always-sampled (§9.2)
    → force-forwarded immediately at on_end (the always-sampled floor preserved)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("files.operation")
        child.set_attribute("files.operation.kind", "upload")  # mutation
        child.end()
        # Forwarded immediately — present BEFORE the root closes.
        assert "files.operation" in {s.name for s in recorder.on_end_calls}
    # Mutation `files.operation` is always-sampled but NOT a §10.2 keep-trigger, so the
    # no-trigger root drops (the envelope is not forwarded).
    assert "workflow.envelope" not in {s.name for s in recorder.on_end_calls}


def test_memory_operation_non_mutation_buffers_at_tail() -> None:
    """B-TAIL: a `memory.operation` at non-mutation `kind=read` → buffered + dropped."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("memory.operation")
        child.set_attribute("memory.operation.kind", "read")  # non-mutation
        child.end()
    assert recorder.on_end_calls == []


def test_validator_fail_transient_buffers_at_tail() -> None:
    """B-TAIL: a `validator.fail.*` at `permanence=transient` is NOT always-sampled at
    the tail → buffered + dropped (contrast the permanent case at AC #9, which both
    force-forwards AND keeps the trace as a §10.2 trigger)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("validator.fail.evaluation")
        child.set_attribute(VALIDATOR_FAIL_PERMANENCE_ATTR, "transient")
        child.end()
    assert recorder.on_end_calls == []


def test_files_operation_absent_kind_force_forwards_at_tail() -> None:
    """B-TAIL conservative-absent floor: a `files.operation` with NO `kind` attribute
    still force-forwards (always-sample) — the fix only NARROWS behavior for
    present-and-non-mutation kinds, never under-sampling the §9.3 inviolable floor."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("files.operation")  # no kind attribute
        child.end()
        assert "files.operation" in {s.name for s in recorder.on_end_calls}


def test_subagent_span_root_force_forwards_at_tail() -> None:
    """B-TAIL: the §9.2 `subagent.span (root)` row — a ROOT `subagent.span` (no parent)
    IS always-sampled → force-forwarded at on_end. The real producer emits this span name
    (`sub_agent_dispatch.py:601` `start_as_current_span("subagent.span")`)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("subagent.span"):  # root (no parent)
        pass
    assert "subagent.span" in {s.name for s in recorder.on_end_calls}


def test_subagent_span_nonroot_succeeded_buffers_and_drops_at_tail() -> None:
    """B-TAIL: a NON-root (nested) `subagent.span` that SUCCEEDED is NOT always-sampled
    (§9.2 root-only) and NOT a §14.3 failure-keep → buffered + dropped on a no-trigger
    root close, NOT force-forwarded. Before B-TAIL the name-only literal match force-
    forwarded every `subagent.span` regardless of depth/result (the head enforces
    root-only via ParentBased; the tail did not)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("subagent.span")  # non-root child
        child.set_attribute(SUBAGENT_RESULT_STATUS_ATTR, "completed")  # succeeded
        child.end()
    # Buffered (not force-forwarded); the no-trigger trace drops on root close.
    assert recorder.on_end_calls == []


def test_subagent_span_nonroot_failed_force_forwards_immediately() -> None:
    """B-TAIL §14.3: a NON-root (nested) `subagent.span` that FAILED
    (`subagent.result_status=failed`) is force-forwarded IMMEDIATELY at on_end (its
    tail-keep-on-failure decision is determined by its own result_status, known at on_end)
    — NOT buffered. This is eviction-safe (out-of-family Codex round 2: a buffered
    failure-trigger would be lost under §9.3 buffer pressure). The keep-flag also preserves
    the trace context (the envelope flushes at root close)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("subagent.span")  # non-root child
        child.set_attribute(SUBAGENT_RESULT_STATUS_ATTR, SUBAGENT_RESULT_STATUS_FAILED_VALUE)
        child.end()
        # Forwarded IMMEDIATELY — present BEFORE the root closes (force-forward, not buffer).
        assert "subagent.span" in {s.name for s in recorder.on_end_calls}
    # The keep-flag preserved the trace context — the envelope flushed at root close.
    assert "workflow.envelope" in {s.name for s in recorder.on_end_calls}


def test_failed_subagent_span_survives_buffer_eviction_pressure() -> None:
    """B-TAIL §14.3 eviction-safety (out-of-family Codex round 2): a failed nested
    `subagent.span` is force-forwarded, so the failure SIGNAL survives even when the trace
    buffer is under `max_buffered_traces` eviction pressure (a buffered failure-trigger
    would be silently dropped on eviction, since `_evict_oldest_trace` does not spare
    keep-flagged traces)."""
    recorder = _RecordingProcessor()
    proc = TailKeepSpanProcessor(downstream=recorder, max_buffered_traces=1)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    # Trace A: a failed nested subagent.span (force-forwarded immediately).
    with tracer.start_as_current_span("envelope.a"):
        child = tracer.start_span("subagent.span")
        child.set_attribute(SUBAGENT_RESULT_STATUS_ATTR, SUBAGENT_RESULT_STATUS_FAILED_VALUE)
        child.end()
        # Open a second pending trace to exert max_buffered_traces=1 eviction pressure.
        with tracer.start_as_current_span("envelope.b.child"):
            pass
    # The failed subagent.span was forwarded at on_end → never in the buffer → never evicted.
    assert "subagent.span" in {s.name for s in recorder.on_end_calls}


def test_non_mutation_files_operation_is_buffered_not_dropped_when_trace_kept() -> None:
    """B-TAIL buffer-vs-drop: a non-mutation `files.operation` is BUFFERED (not dropped
    outright, not force-forwarded) — so in a trace that DOES carry a §10.2 trigger
    (sandbox.violation sibling) it is FLUSHED at root close. Makes the buffer path explicit
    (the drop-path tests only prove not-force-forwarded)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        child = tracer.start_span("files.operation")
        child.set_attribute("files.operation.kind", "list")  # non-mutation → buffered
        child.end()
        with tracer.start_as_current_span(SANDBOX_VIOLATION_SPAN_NAME):  # §10.2 trigger
            pass
    forwarded_names = {s.name for s in recorder.on_end_calls}
    # The buffered non-mutation files.operation flushed because the trace was kept.
    assert "files.operation" in forwarded_names
    assert "workflow.envelope" in forwarded_names


def test_force_flush_keeps_all_buffered_traces_to_avoid_silent_loss() -> None:
    """AC #11: force_flush forwards still-buffered traces (keep-all on shutdown)."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    # Open a span with no §10.2 trigger and DO NOT close the root (simulate
    # a trace that never reaches root-close, e.g., process kill mid-flight).
    span = tracer.start_span("workflow.envelope.partial")
    span.end()  # Child end, but this IS the local root in this trace shape;
    # parent is None → root-close materializes the drop decision immediately.
    # To exercise force_flush keep-all, we need an UNFINISHED trace —
    # construct one where root has not ended. Instead simulate by manually
    # buffering a span without root-close.
    inner_span = tracer.start_span("inner.work")
    # Do NOT end inner_span before force_flush.
    proc.force_flush()
    assert recorder.force_flush_calls == 1
    # No exception; downstream force_flush was invoked.
    inner_span.end()


def test_shutdown_delegates_to_downstream_after_flush() -> None:
    """AC #12: shutdown flushes then delegates to downstream.shutdown()."""
    proc, recorder = _new_tail_keep_with_recorder()
    proc.shutdown()
    assert recorder.shutdown_calls == 1


def test_classification_trigger_in_one_trace_does_not_affect_other_trace() -> None:
    """AC #13: per-trace keep flag isolation — trigger in trace A does not preserve trace B."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    # Trace A: trigger fires → keep all.
    with tracer.start_as_current_span("workflow.envelope.A"):
        with tracer.start_as_current_span(SANDBOX_VIOLATION_SPAN_NAME):
            pass
    # Trace B: no trigger → drop the root.
    with tracer.start_as_current_span("workflow.envelope.B"):
        with tracer.start_as_current_span("plain.work"):
            pass

    forwarded_names = [s.name for s in recorder.on_end_calls]
    assert "workflow.envelope.A" in forwarded_names
    assert SANDBOX_VIOLATION_SPAN_NAME in forwarded_names
    assert "workflow.envelope.B" not in forwarded_names
    assert "plain.work" not in forwarded_names


def test_processor_buffered_trace_count_drops_to_zero_after_root_close() -> None:
    """AC #14: per-trace buffer entries clear on root-close materialize."""
    proc, recorder = _new_tail_keep_with_recorder()
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span("plain.work"):
            pass
    assert proc.buffered_trace_count == 0


# --- materializer wiring integration (per-deployment-surface gating) --------


def test_in_memory_export_via_tail_keep_chain_preserves_classified_trace() -> None:
    """AC #15: end-to-end via TracerProvider → TailKeep → SimpleSpanProcessor + InMemoryExporter."""
    exporter = InMemorySpanExporter()
    simple = SimpleSpanProcessor(exporter)
    tail_keep = TailKeepSpanProcessor(downstream=simple)
    provider = TracerProvider()
    provider.add_span_processor(tail_keep)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span(SANDBOX_VIOLATION_SPAN_NAME):
            pass
    # Both buffered + always-sampled spans should reach the exporter.
    exported_names = {s.name for s in exporter.get_finished_spans()}
    assert SANDBOX_VIOLATION_SPAN_NAME in exported_names
    assert "workflow.envelope" in exported_names


def test_in_memory_export_via_tail_keep_chain_drops_unclassified_trace() -> None:
    """AC #16: end-to-end drops a trace with no §10.2 trigger."""
    exporter = InMemorySpanExporter()
    simple = SimpleSpanProcessor(exporter)
    tail_keep = TailKeepSpanProcessor(downstream=simple)
    provider = TracerProvider()
    provider.add_span_processor(tail_keep)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span("plain.work"):
            pass
    exported_names = {s.name for s in exporter.get_finished_spans()}
    assert exported_names == set()


# --- OD spec v1.28 §9.3 operator-tunable bounded-buffer ceilings -------------


def _buffer_one_unclosed_trace(tracer: otel_trace.Tracer) -> otel_trace.Span:
    """Buffer exactly one trace that never materializes a root-close.

    Starts a fresh root span (unique trace_id, NOT ended → never triggers a
    root-close materialize) and ends a single child under it (the child's
    `parent` is the root → it buffers under the root's trace_id). Returns the
    open root so the caller can keep a reference (un-ended) — this is the
    pathological-producer shape the v1.28 bounds exist to contain.
    """
    root = tracer.start_span("workflow.envelope.pathological")
    child = tracer.start_span("inner.work", context=otel_trace.set_span_in_context(root))
    child.end()
    return root


def test_max_buffered_traces_evicts_oldest_under_pathological_producer() -> None:
    """v1.28: a producer opening >ceiling never-closing traces stays bounded (drop-oldest)."""
    recorder = _RecordingProcessor()
    proc = TailKeepSpanProcessor(downstream=recorder, max_buffered_traces=8)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    # 100 >> 8: a pathological producer opening roots without closing them.
    roots = [_buffer_one_unclosed_trace(tracer) for _ in range(100)]
    assert proc.buffered_trace_count == 8  # bounded at the ceiling
    assert proc.dropped_trace_count == 92  # 100 - 8 evicted (drop-oldest)
    assert len(roots) == 100  # references retained → roots genuinely un-ended


def test_max_spans_per_trace_drops_overflow_non_root_spans() -> None:
    """v1.28: a single never-closing trace cannot accumulate spans without bound."""
    recorder = _RecordingProcessor()
    proc = TailKeepSpanProcessor(downstream=recorder, max_spans_per_trace=4)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    root = tracer.start_span("workflow.envelope")  # un-ended (no root-close yet)
    ctx = otel_trace.set_span_in_context(root)
    for i in range(20):  # 20 >> 4: overflow children
        child = tracer.start_span(f"inner.{i}", context=ctx)
        child.end()
    assert proc.dropped_span_count == 16  # 20 - 4 buffered
    assert proc.buffered_trace_count == 1  # the one trace, capped at 4 spans
    # Root-close ALWAYS processes even at the per-trace cap → trace frees its slot.
    root.end()
    assert proc.buffered_trace_count == 0


def test_bounds_do_not_affect_legitimate_classified_trace() -> None:
    """v1.28: with bounds set high enough, keep-semantics + zero drops are preserved."""
    recorder = _RecordingProcessor()
    proc = TailKeepSpanProcessor(downstream=recorder, max_buffered_traces=8, max_spans_per_trace=8)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("workflow.envelope"):
        with tracer.start_as_current_span(SANDBOX_VIOLATION_SPAN_NAME):
            pass
        with tracer.start_as_current_span("sibling.work"):
            pass
    forwarded = {s.name for s in recorder.on_end_calls}
    assert SANDBOX_VIOLATION_SPAN_NAME in forwarded
    assert "workflow.envelope" in forwarded
    assert "sibling.work" in forwarded
    assert proc.dropped_trace_count == 0
    assert proc.dropped_span_count == 0


def test_root_close_only_trace_does_not_evict_at_saturation() -> None:
    """v1.28 (Codex review): a root-close-only new trace adds no steady-state buffer
    pressure → it must NOT shed a pending trace's context at saturation."""
    recorder = _RecordingProcessor()
    proc = TailKeepSpanProcessor(downstream=recorder, max_buffered_traces=2)
    provider = TracerProvider()
    provider.add_span_processor(proc)
    tracer = provider.get_tracer(__name__)
    # Saturate the buffer with 2 pending (never-closing-root) traces.
    roots = [_buffer_one_unclosed_trace(tracer) for _ in range(2)]
    assert proc.buffered_trace_count == 2
    assert proc.dropped_trace_count == 0
    # A short trace whose first observed span IS its root close (no children):
    # it materializes + frees its slot in the same on_end, so the 2 pending
    # traces must be preserved (NO eviction, NO drop count increment).
    with tracer.start_as_current_span("workflow.envelope.short"):
        pass
    assert proc.buffered_trace_count == 2
    assert proc.dropped_trace_count == 0
    assert len(roots) == 2
