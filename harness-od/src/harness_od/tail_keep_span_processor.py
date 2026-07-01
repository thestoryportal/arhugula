"""C-OD-09 §9.1 production-time tail-based-sampling + §10.2 tail-keep-on-classification.

H_T-OD-3 PARTIAL → RETIRE-READY substrate (gate (a)). Closes the
"tail-keep-on-classification at the OTLP collector boundary per §9.1"
deferral inherited from OD spec v1.2 §9.3 implementer-discretion clause.

**Algorithm shape (per §9.3 implementer-discretion choice).**

OTel-Python's SpanProcessor chain invokes EACH registered processor
independently at `on_end` — a processor cannot prevent a sibling processor
(e.g., the `BatchSpanProcessor` exporter) from receiving a span. To
implement tail-based KEEP-OR-DROP semantics over a span chain, this
processor takes the **wrap-BSP** approach: the downstream processor (the
BSP that holds the OTLP exporter) is NOT registered directly on the
`TracerProvider`; instead, this `TailKeepSpanProcessor` wraps it as
`downstream=`, intercepting all `on_end` calls and selectively forwarding
buffered spans on root-close based on §10.2 classification triggers.

**Engagement gate (per §9.1).** At local-development deployment surface,
§9.1 mandates **head-based sampling** — the sampler at span creation is
the binding decision; tail-keep semantics do NOT apply. At
self-hosted-server + managed-cloud surfaces, §9.1 mandates **tail-based
sampling with tail-keep-on-classification**. Engagement is therefore
gated at construction (`materialize_span_processor_stage` chooses to wrap
the BSP with this processor iff `deployment_surface !=
LOCAL_DEVELOPMENT`); when wrapped, this processor honors the
§10.2 triggers exhaustively.

**Per-trace buffering algorithm.**

1. `on_end(span)` — span ends; the processor:
   - If the span's name is in the always-sampled set per §9.2 (per
     `is_always_sampled`) → forward immediately to `downstream`. Always-
     sampled spans skip the buffer to keep memory bounded; their tree-
     siblings buffered separately get the keep-decision on root close.
   - Else → append the span to the per-trace buffer keyed by `trace_id`.
     Inspect `is_classification_trigger(span)` and OR-merge into the per-
     trace keep flag.
   - If the span is a **root close** (parent SpanContext is None) →
     materialize the keep decision for the trace: forward buffered spans
     to `downstream` iff the keep flag is True; otherwise drop them.
     Clear the buffer + keep flag entries for this trace_id.

2. `force_flush(timeout_millis)` — flush any still-buffered traces (treat
   them as keep-all to avoid silent loss on shutdown) + delegate to
   `downstream.force_flush()`. Returns the downstream flush result.

3. `shutdown()` — flush + delegate to `downstream.shutdown()`.

**Drop semantics (false-but-bounded).** Because trace-end (root close) is
detected by parent-context inspection at `on_end`, a trace that never
materializes a root-close span will accumulate in the buffer until
`force_flush` / `shutdown`. The bootstrap-orchestrator's drain path
(C-RT-10) calls `force_flush` before `shutdown`, so accumulated traces are
flushed before exporter teardown — the keep decision defaults to keep-all
at flush time to avoid silent loss. This is the **trust-sampler-on-base-
rate** posture: the upstream `HarnessCompositeSampler` (per H_T-OD-3
substrate batch-34) has already applied the §10.3 per-cell base-rate to
the head-based sampling decision; this processor only adds the §10.2
classification-trigger PRESERVATION layer atop that decision. Spans the
sampler dropped never enter `on_end`; this processor cannot resurrect
them. The keep semantics here apply to spans the sampler RECORDED — for
those, classification triggers ensure trace-tree preservation across the
batch-export boundary.

**Bounded-buffer bounds (OD spec v1.28 — §9.3 implementer-discretion).**
The v1.27 §2(a) carve-out (MVP did NOT bound buffer size) is CLOSED at OD
spec v1.28: the processor now accepts two optional operator-tunable
ceilings, supplied in production from `CollectorConfig`:

- ``max_buffered_traces`` — ceiling on the number of distinct traces
  buffered pending root-close. When a NEW trace that will REMAIN pending
  would exceed the ceiling, the **oldest buffered trace is evicted**
  (drop-oldest / insertion-order FIFO) and counted at
  ``dropped_trace_count``. This directly bounds the pathological case (a
  producer that opens 10^6 roots without closing them): the stale
  never-closing traces are the oldest, so they are shed first to make room.
  A new trace whose FIRST observed span is already its root-close
  materializes + frees its slot in the same ``on_end`` (no steady-state
  buffer pressure), so it does NOT trigger eviction.
- ``max_spans_per_trace`` — ceiling on the non-always-sampled spans
  buffered for a single trace. Overflow **non-root** spans are dropped and
  counted at ``dropped_span_count``; the root-close span ALWAYS processes
  (so the trace materializes and frees its slot rather than leaking).

Both default to ``None`` (unbounded — preserves the v1.27 MVP behavior for
direct construction; the production materializer always passes the
``CollectorConfig`` ceilings, so production is bounded by default).

**Eviction fidelity tradeoff (documented choice).** Drop-oldest may evict
a *keep-flagged* trace (one whose §10.2 classification trigger fired)
under buffer pressure. This is an accepted, bounded loss: the trigger span
itself is in the §9.2 always-sampled set and so was **forwarded
immediately** at ``on_end`` (it bypasses the buffer) — the failure
*signal* survives eviction; only the buffered tree-*context* (sibling
spans the sampler recorded) is shed. This is consistent with the
processor's "trust-sampler-on-base-rate, best-effort preservation"
posture. A keep-flag-preferential eviction (evict non-keep traces first)
is a documented future refinement; drop-oldest is chosen for O(1)
eviction and because the failure signal is already preserved. Alternative
considered: drop-NEW (reject the incoming trace) — rejected because it
lets stale never-closing traces hog the buffer indefinitely, the opposite
of the pathology the bound exists to contain.

**Spec authority.** OD spec v1.2 §C-OD-09 §9.1 (per-deployment-surface
sampling mode) + §9.2 (always-sampled exception set) + §9.3 (sampling-
discipline invariants + implementer-discretion clause on tail-based
algorithm) + §C-OD-10 §10.2 (3 classification triggers).

Authority anchors: `harness-od/src/harness_od/base_rate_set_and_envelope.py`
canonical `TAIL_KEEP_RULES` declaration site;
`harness-od/src/harness_od/tail_keep_classification.py` per-span trigger
predicate; `harness-od/src/harness_od/sampling_mode.py` always-sampled
set + `is_always_sampled` decomposed-prefix lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from harness_od.sampling_mode import is_always_sampled
from harness_od.tail_keep_classification import (
    SUBAGENT_RESULT_STATUS_ATTR,
    SUBAGENT_RESULT_STATUS_FAILED_VALUE,
    SUBAGENT_SPAN_NAME,
    is_classification_trigger,
)

if TYPE_CHECKING:
    pass

__all__ = [
    "TailKeepSpanProcessor",
]


class TailKeepSpanProcessor(SpanProcessor):
    """OTel SpanProcessor wrapping a downstream processor with tail-keep-on-classification.

    Per OD spec §C-OD-09 §9.1 (production-time tail-based-sampling) +
    §10.2 (3 classification triggers) + §9.3 (implementer-discretion
    algorithm). Buffers non-always-sampled spans per trace_id; on root-
    close, forwards the buffer to `downstream` iff any span in the trace
    carried a §10.2 classification trigger, else drops. Always-sampled
    spans (per §9.2) bypass the buffer and forward immediately.

    The downstream processor is typically a `BatchSpanProcessor(exporter)`;
    this processor is the registered processor on the `TracerProvider`.
    """

    def __init__(
        self,
        *,
        downstream: SpanProcessor,
        max_buffered_traces: int | None = None,
        max_spans_per_trace: int | None = None,
    ) -> None:
        self._downstream: SpanProcessor = downstream
        # Per-trace_id buffer of non-always-sampled spans pending root-close
        # keep decision. Keyed by the int form of OTel trace_id. Python dict
        # insertion order makes `next(iter(...))` the oldest trace (FIFO
        # drop-oldest eviction under `max_buffered_traces` pressure).
        self._buffer: dict[int, list[ReadableSpan]] = {}
        # Per-trace_id keep flag — True iff any span in the trace carried a
        # §10.2 classification trigger. OR-merged at on_end.
        self._keep: dict[int, bool] = {}
        # OD spec v1.28 §9.3 operator-tunable bounded-buffer ceilings. None =
        # unbounded (v1.27 MVP behavior); the production materializer passes
        # the `CollectorConfig` ceilings so production is bounded by default.
        self._max_buffered_traces: int | None = max_buffered_traces
        self._max_spans_per_trace: int | None = max_spans_per_trace
        # Drop counters (observability + test introspection).
        self._dropped_trace_count: int = 0
        self._dropped_span_count: int = 0

    @property
    def downstream(self) -> SpanProcessor:
        """The wrapped downstream processor (test introspection)."""
        return self._downstream

    @property
    def buffered_trace_count(self) -> int:
        """Number of traces currently buffered (test introspection)."""
        return len(self._buffer)

    @property
    def dropped_trace_count(self) -> int:
        """Traces evicted (drop-oldest) under the `max_buffered_traces` ceiling."""
        return self._dropped_trace_count

    @property
    def dropped_span_count(self) -> int:
        """Non-root spans dropped under the `max_spans_per_trace` ceiling."""
        return self._dropped_span_count

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        """Forward to downstream; no buffering at start (decisions are at end)."""
        self._downstream.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """Buffer non-always-sampled spans by trace_id; flush-or-drop on root close.

        Always-sampled spans (per §9.2) forward immediately to `downstream`.
        Non-always-sampled spans buffer pending root close; on root close,
        the trace's buffered spans flush to `downstream` iff any span in
        the trace carried a §10.2 classification trigger, else drop.
        """
        # §9.2 always-sampled set: forward immediately, do not buffer.
        # These spans (`audit.*`, `sandbox.violation`, `hitl.*`, etc.) ARE
        # always-sampled per spec and their tree-siblings are buffered
        # separately under the trace_id; the always-sampled span itself
        # never depends on tail-keep buffering.
        #
        # B-TAIL-CONDITIONAL-SAMPLING: pass `span.attributes` so the §9.2
        # ATTRIBUTE-CONDITIONAL rows (`files.operation` at non-mutation `kind`,
        # `memory.operation` at non-mutation `kind`, `validator.fail.*` at
        # `permanence=transient`) resolve to NOT-always-sampled at the TAIL and
        # buffer for the §10.2 tail-keep decision — rather than force-forwarding
        # unconditionally. This is the production enforcement point: producers set
        # `files.operation.kind` / `memory.operation.kind` / `validator.fail.permanence`
        # DURING the span, so at `on_end` (here) the discriminating attribute is
        # finalized, whereas at the head `should_sample` (span start) it is absent →
        # conservative-always-sample (the B7-landed head half). Conservative-absent is
        # preserved by construction: a missing attribute still returns always-sample,
        # so this only NARROWS behavior for present-and-non-mutation/transient spans
        # (never under-samples the §9.3 inviolable floor).
        #
        # B-TAIL also enforces the §9.2 `subagent.span (root)` ROOT-conditional row at
        # the tail: only the ROOT `subagent.span` is always-sampled; a non-root (nested)
        # `subagent.span` → §10.1 base-rate (buffer). The head enforces this structurally
        # via `ParentBased` (the inner sampler is consulted ONLY for root spans), but the
        # tail has no `ParentBased` wrapper, so it gates here with a parent-check (advisor:
        # a processor parent-check, NOT an SSOT `is_root` param — it is the one
        # root-conditional row, and root-ness is structural, not a name/attribute).
        #
        # EXCEPTION — a FAILED `subagent.span` is force-forwarded (eviction-safe) regardless
        # of depth (out-of-family Codex round 2): §14.3's tail-keep-on-failure decision is
        # determined by the span's OWN `result_status`, KNOWN at on_end, so it needs no
        # root-close buffering. Buffering it would expose the failure SIGNAL to §9.3
        # eviction/overflow — the `_evict_oldest_trace` fidelity tradeoff explicitly assumes
        # keep-TRIGGER spans are always-sampled/immediate (so eviction only sheds buffered
        # siblings, never the trigger itself); a buffered failure-trigger would be silently
        # lost under buffer pressure. So only a SUCCEEDED non-root `subagent.span` buffers.
        _failed_subagent = (
            span.name == SUBAGENT_SPAN_NAME
            and (span.attributes or {}).get(SUBAGENT_RESULT_STATUS_ATTR)
            == SUBAGENT_RESULT_STATUS_FAILED_VALUE
        )
        _buffer_nonroot_subagent = (
            span.name == SUBAGENT_SPAN_NAME and span.parent is not None and not _failed_subagent
        )
        if not _buffer_nonroot_subagent and is_always_sampled(span.name, span.attributes):
            self._downstream.on_end(span)
            # Still mark the trace keep-flag if the always-sampled span is
            # a classification trigger (sandbox.violation + breaker.tripped
            # are both in §9.2 AND in §10.2) so tree-siblings buffered
            # under the same trace get preserved at root close.
            ctx = span.get_span_context()
            assert ctx is not None  # a span reaching on_end always has a context
            if is_classification_trigger(span):
                self._keep[ctx.trace_id] = True
            return

        ctx = span.get_span_context()
        assert ctx is not None  # a span reaching on_end always has a context
        trace_id = ctx.trace_id
        is_root_close = span.parent is None

        # OD spec v1.28 §9.3 max-buffered-traces ceiling: a NEW trace that will
        # REMAIN pending evicts the oldest buffered trace (drop-oldest FIFO).
        # Gate on `not is_root_close`: a root-close-first trace materializes +
        # frees its slot in this same on_end (no steady-state buffer pressure),
        # so it must NOT shed an existing pending trace's context.
        if (
            not is_root_close
            and trace_id not in self._buffer
            and self._max_buffered_traces is not None
            and len(self._buffer) >= self._max_buffered_traces
        ):
            self._evict_oldest_trace()

        bucket = self._buffer.setdefault(trace_id, [])
        # OD spec v1.28 §9.3 max-spans-per-trace ceiling: drop overflow
        # non-root spans (the root-close span always processes below so the
        # trace materializes and frees its slot rather than leaking).
        if (
            not is_root_close
            and self._max_spans_per_trace is not None
            and len(bucket) >= self._max_spans_per_trace
        ):
            self._dropped_span_count += 1
        else:
            bucket.append(span)

        if is_classification_trigger(span):
            self._keep[trace_id] = True

        # Root close detection: parent SpanContext is None means this span
        # has no parent in the recorded trace (it is the local-root). At
        # span-end, OTel `ReadableSpan.parent` is `None` for the root.
        if is_root_close:
            self._materialize_trace_decision(trace_id)

    def _evict_oldest_trace(self) -> None:
        """Drop the oldest buffered trace (FIFO) under the max-traces ceiling.

        Python dict preserves insertion order, so `next(iter(self._buffer))`
        is the oldest-inserted trace_id. Its buffered spans are dropped
        (memory freed) and the trace is counted at `dropped_trace_count`. See
        the module docstring "Eviction fidelity tradeoff" for the rationale
        (drop-oldest may shed a keep-flagged trace's buffered context, but its
        always-sampled trigger span was already forwarded immediately).
        """
        oldest_trace_id = next(iter(self._buffer))
        self._buffer.pop(oldest_trace_id, None)
        self._keep.pop(oldest_trace_id, None)
        self._dropped_trace_count += 1

    def _materialize_trace_decision(self, trace_id: int) -> None:
        """Flush or drop the buffered spans for `trace_id` per the keep flag."""
        buffered = self._buffer.pop(trace_id, [])
        keep = self._keep.pop(trace_id, False)
        if not keep:
            return
        for span in buffered:
            self._downstream.on_end(span)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush any still-buffered traces (keep-all) + delegate to downstream.

        At force-flush, accumulated traces (those never having materialized
        a root-close span) are forwarded to `downstream` as keep-all to
        avoid silent loss on shutdown. Then delegates to
        `downstream.force_flush(timeout_millis)`.
        """
        # Drain the buffer — keep-all on shutdown to avoid silent loss.
        for trace_id in list(self._buffer.keys()):
            for span in self._buffer[trace_id]:
                self._downstream.on_end(span)
            self._buffer.pop(trace_id, None)
            self._keep.pop(trace_id, None)
        return self._downstream.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        """Flush + delegate to downstream.shutdown()."""
        self.force_flush()
        self._downstream.shutdown()
