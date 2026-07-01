"""C-OD-12 + C-OD-13 RedactionSpanProcessor tests.

Closes H_T-OD-4 retirement gate "pre-collector redaction SpanProcessor at SDK
boundary BEFORE BatchSpanProcessor buffer". Tests verify §12.1 default-off
content-attribute strip discipline, §12.2 default-on structure-attribute
preservation, OTel SpanProcessor lifecycle no-ops, MultiSpanProcessor
ordering (redaction fires before BSP-equivalent observer), and operator-
injected custom strip-set surface.
"""

from __future__ import annotations

import asyncio

import pytest
from harness_core import PersonaTier
from harness_od.content_structure_discipline import DEFAULT_OFF_CONTENT_ATTRIBUTES
from harness_od.redaction_span_processor import (
    RedactionSpanProcessor,
    session_content_capture,
    session_content_capture_enabled,
)
from harness_od.redaction_tokenizer import (
    DeterministicRedactionClassifier,
    InMemoryRedactionTokenMap,
    OpaqueRedactionTokenizer,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# --- §12.1 default-off content keys ----------------------------------------


def test_default_redacted_attributes_is_spec_canonical_set() -> None:
    """AC #1: default strip-set matches C-OD-12 §12.1 13-attribute set."""
    processor = RedactionSpanProcessor()
    assert processor.redacted_attributes is DEFAULT_OFF_CONTENT_ATTRIBUTES
    assert len(processor.redacted_attributes) == 13


def test_redacted_attribute_keys_match_otel_genai_semconv_141_opt_in_subset() -> None:
    """AC #2: §12.1 includes OTel GenAI semconv 1.41.0 8-attribute Opt-In set."""
    expected_opt_in_subset = frozenset(
        {
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.system_instructions",
            "gen_ai.tool.definitions",
            "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result",
            "gen_ai.retrieval.documents",
            "gen_ai.retrieval.query.text",
        }
    )
    processor = RedactionSpanProcessor()
    assert expected_opt_in_subset.issubset(processor.redacted_attributes)


def test_redacted_attributes_includes_cross_namespace_content_surfaces() -> None:
    """AC #3: §12.1 extends OTel Opt-In with 5 cross-namespace content surfaces."""
    expected_cross_namespace = frozenset(
        {
            "mcp.tool.call.arguments",
            "mcp.tool.call.result",
            "skill.body_content",
            "memory.content",
            "files.content",
        }
    )
    processor = RedactionSpanProcessor()
    assert expected_cross_namespace.issubset(processor.redacted_attributes)


# --- on_end strip discipline ------------------------------------------------


@pytest.fixture()
def exporter_and_provider() -> tuple[InMemorySpanExporter, TracerProvider]:
    """TracerProvider with RedactionSpanProcessor BEFORE InMemorySpanExporter.

    Mirrors production composition: redaction processor fires first; the
    exporter (here `SimpleSpanProcessor(InMemorySpanExporter)`, standing in
    for BSP+OTLP at HEAD) sees the redacted span.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RedactionSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def test_on_end_strips_gen_ai_input_messages(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #4: gen_ai.input.messages key removed from exported span."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test")
    span = tracer.start_span("anthropic.messages.create")
    span.set_attribute("gen_ai.input.messages", "PII content")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.end()
    [exported] = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes
    assert exported.attributes["gen_ai.operation.name"] == "chat"


def test_on_end_strips_all_13_content_attributes(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #5: full §12.1 13-attribute set stripped at on_end."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    for key in DEFAULT_OFF_CONTENT_ATTRIBUTES:
        span.set_attribute(key, f"content for {key}")
    span.set_attribute("gen_ai.operation.name", "preserved")
    span.end()
    [exported] = exporter.get_finished_spans()
    for key in DEFAULT_OFF_CONTENT_ATTRIBUTES:
        assert key not in exported.attributes, f"redaction failed to strip {key}"
    assert exported.attributes["gen_ai.operation.name"] == "preserved"


def test_on_end_tokenizes_content_attributes_when_tokenizer_configured() -> None:
    """R-008 gate (b) substrate: token mode keeps shape, not raw content."""
    token_map = InMemoryRedactionTokenMap()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        RedactionSpanProcessor(tokenizer=OpaqueRedactionTokenizer(token_map=token_map))
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    span.set_attribute("gen_ai.input.messages", "PII content")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.end()

    [exported] = exporter.get_finished_spans()
    token = exported.attributes["gen_ai.input.messages"]
    assert isinstance(token, str)
    assert token.startswith("[REDACTED:CONTENT:")
    assert "PII content" not in token
    assert exported.attributes["gen_ai.operation.name"] == "chat"

    [record] = token_map.records
    assert record.token == token
    assert record.attribute_key == "gen_ai.input.messages"
    assert record.raw_value == "PII content"
    assert record.trace_id == exported.context.trace_id.to_bytes(16, "big").hex()
    assert record.span_id == exported.context.span_id.to_bytes(8, "big").hex()


def test_on_end_can_emit_category_specific_tokens() -> None:
    """R-008 gate (b): classifier-backed token mode emits category labels."""
    token_map = InMemoryRedactionTokenMap()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        RedactionSpanProcessor(
            tokenizer=OpaqueRedactionTokenizer(
                token_map=token_map,
                classifier=DeterministicRedactionClassifier(),
            )
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    span.set_attribute("gen_ai.input.messages", "customer ssn 123-45-6789")
    span.end()

    [exported] = exporter.get_finished_spans()
    token = exported.attributes["gen_ai.input.messages"]
    assert isinstance(token, str)
    assert token.startswith("[REDACTED:PII:")
    assert "123-45-6789" not in token

    [record] = token_map.records
    assert record.semantic_category == "PII"


def test_solo_developer_capture_toggle_bypasses_tokenization() -> None:
    """Existing solo capture semantics win over opt-in tokenization."""
    token_map = InMemoryRedactionTokenMap()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        RedactionSpanProcessor(tokenizer=OpaqueRedactionTokenizer(token_map=token_map))
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with session_content_capture():
        span = tracer.start_span("s")
        span.set_attribute("gen_ai.input.messages", "operator-approved raw")
        span.end()

    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.input.messages"] == "operator-approved raw"
    assert token_map.records == ()


def test_on_end_preserves_structure_attributes(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #6: §12.2 default-on structure attributes survive redaction."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.provider.name", "anthropic")
    span.set_attribute("gen_ai.request.model", "claude-opus-4-7")
    span.set_attribute("sandbox.tier", "tier_1_process")
    span.set_attribute("hitl.gate.evaluated", True)
    span.end()
    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.operation.name"] == "chat"
    assert exported.attributes["gen_ai.provider.name"] == "anthropic"
    assert exported.attributes["gen_ai.request.model"] == "claude-opus-4-7"
    assert exported.attributes["sandbox.tier"] == "tier_1_process"
    assert exported.attributes["hitl.gate.evaluated"] is True


def test_on_end_handles_span_with_no_redacted_keys(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #7: span carrying zero redacted keys passes through unchanged."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test")
    span = tracer.start_span("structure_only")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.usage.input_tokens", 100)
    span.end()
    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.operation.name"] == "chat"
    assert exported.attributes["gen_ai.usage.input_tokens"] == 100


def test_on_end_handles_span_with_zero_attributes(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #8: empty-attribute span (no set_attribute calls) handled."""
    exporter, provider = exporter_and_provider
    tracer = provider.get_tracer("test")
    span = tracer.start_span("empty")
    span.end()
    [exported] = exporter.get_finished_spans()
    # OTel may carry an empty BoundedAttributes; redaction never raises.
    assert dict(exported.attributes) == {}


# --- ordering invariant -----------------------------------------------------


def test_redaction_observable_at_exporter_retrieval_time() -> None:
    """AC #9: span retrieved post-end carries no redacted keys.

    `InMemorySpanExporter` stores `ReadableSpan` references at on_end, not
    snapshots — so the retrieved attribute bag reflects mutations from any
    processor in the chain regardless of registration order. The load-
    bearing invariant for production is that, by the time the SpanData is
    serialized for export, redaction has fired. For synchronous OTLP
    serialization this requires redaction to be registered BEFORE the BSP
    on the TracerProvider (enforced at `materialize_span_processor_stage`).
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RedactionSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    span.set_attribute("gen_ai.input.messages", "PII")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.end()
    [exported] = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes


# --- lifecycle no-ops -------------------------------------------------------


def test_on_start_is_no_op() -> None:
    """AC #11: on_start returns None and mutates nothing.

    Verified indirectly — on_start is invoked at start_span; if it raised,
    span construction would fail.
    """
    provider = TracerProvider()
    provider.add_span_processor(RedactionSpanProcessor())
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    # Pre-end attribute set after start: no crash from on_start.
    span.set_attribute("gen_ai.input.messages", "PII")
    span.end()


def test_force_flush_returns_true() -> None:
    """AC #12: force_flush no-op returns True."""
    processor = RedactionSpanProcessor()
    assert processor.force_flush() is True
    assert processor.force_flush(timeout_millis=5_000) is True


def test_shutdown_is_no_op() -> None:
    """AC #13: shutdown returns None; idempotent."""
    processor = RedactionSpanProcessor()
    assert processor.shutdown() is None
    assert processor.shutdown() is None  # idempotent


# --- operator-injected strip-set -------------------------------------------


def test_operator_injected_custom_strip_set() -> None:
    """AC #14: operator can construct with a custom redacted_attributes set."""
    custom = frozenset({"custom.secret.attr", "another.private"})
    processor = RedactionSpanProcessor(redacted_attributes=custom)
    assert processor.redacted_attributes == custom


def test_custom_strip_set_actually_strips() -> None:
    """AC #15: custom strip-set applied at on_end (not just exposed)."""
    custom = frozenset({"x.custom.private"})
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RedactionSpanProcessor(redacted_attributes=custom))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    span.set_attribute("x.custom.private", "secret")
    span.set_attribute("gen_ai.input.messages", "NOT in custom set")
    span.end()
    [exported] = exporter.get_finished_spans()
    assert "x.custom.private" not in exported.attributes
    # gen_ai.input.messages is in DEFAULT_OFF but NOT in the custom set:
    assert exported.attributes["gen_ai.input.messages"] == "NOT in custom set"


# --- §13.1 gate (a): per-session content-capture toggle --------------------
#
# R-008 OD-4 gate (a). The solo-developer per-session toggle (§13.1 row 1,
# `PER_PERSONA_TIER_REDACTION[SOLO_DEVELOPER].toggleable=True`): an operator may
# enable raw content capture for a session via `session_content_capture()`,
# honored ONLY at solo-developer. team-binding + multi-tenant-compliance are
# non-toggleable (§13.3 monotonic-tightening / downgrade-rejection). The
# propagation tests below emit the span across an asyncio task / thread boundary
# so a same-frame hollow pass cannot satisfy them — the toggle must genuinely
# reach `on_end` in the span-emitting context (PD-3 grep-presence !=
# verified-working).
#
# SCOPE (honest bound). These verify the LANGUAGE-LEVEL primitive propagation:
# `asyncio.create_task` / `asyncio.to_thread` copy the ContextVar into the
# child task/thread. They do NOT drive the operator trigger path
# `with session_content_capture(): api.run(wf)`, which crosses the in-process
# MCP transport (api.run -> session.call_tool -> server-task-dispatched
# run_workflow -> execute_workflow spans). Whether the toggle reaches those
# server-side spans depends on the MCP helper's context-copy at server-task
# creation — that hop is part of the §13.3-deferred toggle UX (the specific
# operator surfacing of the toggle) and is intentionally NOT exercised here. It
# may turn out the production trigger must be set INSIDE the run_workflow
# handler rather than around api.run; that is a decision for the UX arc, not
# this mechanism close.


def _provider_for(persona_tier: PersonaTier) -> tuple[InMemorySpanExporter, TracerProvider]:
    """RedactionSpanProcessor(persona_tier) BEFORE InMemorySpanExporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RedactionSpanProcessor(persona_tier=persona_tier))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


# --- context-manager mechanics ---------------------------------------------


def test_session_content_capture_default_off() -> None:
    """AC: outside any scope the override reads False (§13.1 default-off)."""
    assert session_content_capture_enabled() is False


def test_session_content_capture_enables_in_scope_and_resets() -> None:
    """AC: True inside the scope; prior value restored on exit (reset)."""
    assert session_content_capture_enabled() is False
    with session_content_capture():
        assert session_content_capture_enabled() is True
    assert session_content_capture_enabled() is False


def test_session_content_capture_nested_restore() -> None:
    """AC: `reset` (not set-to-False) restores the enclosing scope's value."""
    with session_content_capture(enabled=True):
        assert session_content_capture_enabled() is True
        with session_content_capture(enabled=False):
            assert session_content_capture_enabled() is False
        # restored to the outer scope's True, not the module default False:
        assert session_content_capture_enabled() is True
    assert session_content_capture_enabled() is False


# --- per-tier honoring (same-frame: necessary but not sufficient) ----------


def test_solo_developer_default_strips() -> None:
    """AC: solo-developer with NO toggle strips (default-off preserved)."""
    exporter, provider = _provider_for(PersonaTier.SOLO_DEVELOPER)
    tracer = provider.get_tracer("test")
    span = tracer.start_span("s")
    span.set_attribute("gen_ai.input.messages", "PII")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.end()
    [exported] = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes
    assert exported.attributes["gen_ai.operation.name"] == "chat"


def test_solo_developer_toggle_keeps_content() -> None:
    """AC: solo-developer + toggle enabled keeps content-bearing attributes."""
    exporter, provider = _provider_for(PersonaTier.SOLO_DEVELOPER)
    tracer = provider.get_tracer("test")
    with session_content_capture():
        span = tracer.start_span("s")
        span.set_attribute("gen_ai.input.messages", "PII")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.end()
    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.input.messages"] == "PII"
    assert exported.attributes["gen_ai.operation.name"] == "chat"


def test_team_binding_ignores_toggle() -> None:
    """AC: team-binding is non-toggleable — strips even with toggle enabled."""
    exporter, provider = _provider_for(PersonaTier.TEAM_BINDING)
    tracer = provider.get_tracer("test")
    with session_content_capture():
        span = tracer.start_span("s")
        span.set_attribute("gen_ai.input.messages", "PII")
        span.end()
    [exported] = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes


def test_multi_tenant_ignores_toggle() -> None:
    """AC: multi-tenant-compliance is non-toggleable (§13.3 downgrade-reject)."""
    exporter, provider = _provider_for(PersonaTier.MULTI_TENANT_COMPLIANCE)
    tracer = provider.get_tracer("test")
    with session_content_capture():
        span = tracer.start_span("s")
        span.set_attribute("gen_ai.input.messages", "PII")
        span.end()
    [exported] = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes


# --- propagation across the production async boundary (forcing function) ----


async def _emit_span(provider: TracerProvider, *, content: str) -> None:
    """Emit a content-bearing span via the production `start_as_current_span`
    idiom; `on_end` fires synchronously at the `with` exit in THIS task's
    context (whatever context the task was created/copied with).
    """
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("workflow.span") as span:
        span.set_attribute("gen_ai.input.messages", content)
        span.set_attribute("gen_ai.operation.name", "chat")


def test_toggle_propagates_into_child_task_solo_developer() -> None:
    """AC: toggle set in the outer frame propagates into a child `create_task`.

    The span is emitted+ended INSIDE a child task created within the
    `session_content_capture()` scope — so the assertion can only pass if the
    ContextVar value was copied into the child task's context and read at
    `on_end`. A same-frame set would not exercise this.
    """
    exporter, provider = _provider_for(PersonaTier.SOLO_DEVELOPER)

    async def run() -> None:
        with session_content_capture():
            await asyncio.create_task(_emit_span(provider, content="PII-kept"))

    asyncio.run(run())
    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.input.messages"] == "PII-kept"


def test_no_toggle_strips_across_child_task_solo_developer() -> None:
    """AC: without the toggle, the same child-task emission is stripped."""
    exporter, provider = _provider_for(PersonaTier.SOLO_DEVELOPER)

    async def run() -> None:
        await asyncio.create_task(_emit_span(provider, content="PII-stripped"))

    asyncio.run(run())
    [exported] = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in exported.attributes


def test_toggle_propagates_across_to_thread_bridge_solo_developer() -> None:
    """AC: toggle propagates across `asyncio.to_thread` (the production bridge).

    `run_workflow` dispatches blocking work via `asyncio.to_thread`, which
    copies the caller's context into the worker thread. A span emitted there
    must still observe a toggle set in the awaiting frame.
    """
    exporter, provider = _provider_for(PersonaTier.SOLO_DEVELOPER)

    def _emit_sync() -> None:
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("workflow.span") as span:
            span.set_attribute("gen_ai.input.messages", "PII-thread-kept")

    async def run() -> None:
        with session_content_capture():
            await asyncio.to_thread(_emit_sync)

    asyncio.run(run())
    [exported] = exporter.get_finished_spans()
    assert exported.attributes["gen_ai.input.messages"] == "PII-thread-kept"


def test_toggle_does_not_leak_past_scope_into_later_span() -> None:
    """AC: a span emitted AFTER the scope exits is redacted again (no leak)."""
    exporter, provider = _provider_for(PersonaTier.SOLO_DEVELOPER)
    tracer = provider.get_tracer("test")
    with session_content_capture():
        s1 = tracer.start_span("inside")
        s1.set_attribute("gen_ai.input.messages", "kept")
        s1.end()
    s2 = tracer.start_span("outside")
    s2.set_attribute("gen_ai.input.messages", "stripped")
    s2.end()
    inside, outside = exporter.get_finished_spans()
    assert inside.attributes["gen_ai.input.messages"] == "kept"
    assert "gen_ai.input.messages" not in outside.attributes
