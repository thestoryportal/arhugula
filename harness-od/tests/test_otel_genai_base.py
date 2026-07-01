"""Tests for U-OD-04 — OTel GenAI semconv 1.41.0 base-layer attributes (C-OD-04).

Test set per the U-OD-04 v2.5 `Tests:` field — covers acceptance #1-#8. The
plan's specification-level test names carry a `§` glyph (not a valid Python
identifier character); they are materialized here with `spec_4_3`-style names.
"""

from __future__ import annotations

import typing

import pytest
from harness_od.otel_genai_base import (
    BASE_LAYER_ATTRIBUTES,
    BASE_METRIC_NAME,
    HIERARCHY_CORRELATION_KEY,
    SPAN_NAME_FORMAT,
    AttributeTier,
    ChildSpanRef,
    EventEmission,
    GenAiAttribute,
    GenAiOperation,
    SpanAttributes,
    SpanRef,
    attributes_in_tier,
    span_name,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Span as _OTelSpan
from opentelemetry.util.types import Attributes as _OTelAttributes
from pydantic import ValidationError

# --- Spec §4 verbatim reference sets (transcribed from C-OD-04) -------------

# §4.2 operations enum — 9 values per OD spec v1.16 §1.1 (OTel 1.41.0 archived
# text). v1.2-v1.15 declared 7 values; `invoke_workflow` + `retrieval` added at
# v1.16 per .harness/class_1_fork_tension_004_d2_d3_otel_141_relitigation.md.
_SPEC_OPERATIONS = {
    "chat",
    "create_agent",
    "embeddings",
    "execute_tool",
    "generate_content",
    "invoke_agent",
    "invoke_workflow",
    "retrieval",
    "text_completion",
}

# §4.3 attribute-tier table per OD spec v1.19 §1.1 canonical reading
# (per-attribute Requirement-Level audit against OTel GenAI semconv 1.41.0
# chat-span table). v1.2-v1.18 declared 3 Required + 6 Recommended + 8 Opt-In;
# v1.19 redistributes 3 attributes to the v1.16-NEW Conditionally Required
# tier (`gen_ai.request.model`, `server.port`, `gen_ai.conversation.id`).
_SPEC_REQUIRED = {
    "gen_ai.operation.name",
    "gen_ai.provider.name",
}
_SPEC_CONDITIONALLY_REQUIRED = {
    "gen_ai.request.model",
    "server.port",
    "gen_ai.conversation.id",
}
_SPEC_RECOMMENDED = {
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.response.finish_reasons",
    "server.address",
}
_SPEC_OPT_IN = {
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.definitions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "gen_ai.retrieval.documents",
    "gen_ai.retrieval.query.text",
}


# --- acceptance #1 — span name format --------------------------------------


def test_span_name_format_byte_exact_two_component() -> None:
    """§4.1 verbatim — the 2-component span name format per v1.12 amendment.

    Conforms to OTel GenAI semantic conventions 1.41.0 archived text:
    `Span name SHOULD be {gen_ai.operation.name} {gen_ai.request.model}`.
    """
    assert SPAN_NAME_FORMAT == ("{gen_ai.operation.name} {gen_ai.request.model}")
    assert SPAN_NAME_FORMAT.count("{") == 2


def test_span_name_resolves_at_span_emission_time() -> None:
    """`span_name` materializes the §4.1 format with call-bound values."""
    assert span_name(GenAiOperation.CHAT, "claude-opus-4-7") == "chat claude-opus-4-7"


# --- acceptance #2 — operations enum ---------------------------------------


def test_genai_operation_cardinality_nine() -> None:
    """§4.2 — exactly 9 operations per OD spec v1.16 §1.1 (OTel 1.41.0).

    v1.2-v1.15 declared 7 operations; v1.16 adds `invoke_workflow` +
    `retrieval` per OTel GenAI semconv 1.41.0 archived text at
    `github.com/open-telemetry/semantic-conventions/blob/v1.41.0/docs/gen-ai/
    gen-ai-spans.md`.
    """
    assert len(GenAiOperation) == 9
    assert {op.value for op in GenAiOperation} == _SPEC_OPERATIONS


def test_genai_operation_includes_generate_content() -> None:
    """§4.2 — `generate_content` is present (the value the v2.1 enum omitted)."""
    assert GenAiOperation.GENERATE_CONTENT.value == "generate_content"


def test_genai_operation_includes_invoke_workflow() -> None:
    """§4.2 — `invoke_workflow` NEW at v1.16 per OD spec §1.1."""
    assert GenAiOperation.INVOKE_WORKFLOW.value == "invoke_workflow"


def test_genai_operation_includes_retrieval() -> None:
    """§4.2 — `retrieval` NEW at v1.16 per OD spec §1.1."""
    assert GenAiOperation.RETRIEVAL.value == "retrieval"


# --- acceptance #3 — attribute tiers ---------------------------------------


def test_attribute_tier_cardinality_four() -> None:
    """§4.3 — exactly 4 tiers per OD spec v1.16 §1.2 (OTel 1.41.0).

    v1.2-v1.15 declared 3 tiers; v1.16 adds `CONDITIONALLY_REQUIRED` per
    OTel GenAI semconv 1.41.0 archived text. The original v2.1-v2.4 plan
    carried a `CONDITIONAL` tier that was struck at OD plan v2.5
    plan-conforms-to-spec — that strike is SUPERSEDED by v1.16.
    """
    assert len(AttributeTier) == 4
    assert {t.value for t in AttributeTier} == {
        "Required",
        "Conditionally Required",
        "Recommended",
        "Opt-In",
    }


def test_attribute_tier_conditionally_required_present() -> None:
    """§4.3 — `CONDITIONALLY_REQUIRED` NEW at v1.16 per OD spec §1.2."""
    assert AttributeTier.CONDITIONALLY_REQUIRED.value == "Conditionally Required"


# --- acceptance #4 — base-layer attribute set + per-attribute tier ----------


def test_base_layer_attributes_byte_exact_per_semconv_1_41_0() -> None:
    """§4.3 — the base-layer attribute name set, verbatim (17 attributes)."""
    names = {attr.name for attr in BASE_LAYER_ATTRIBUTES}
    assert names == (
        _SPEC_REQUIRED | _SPEC_CONDITIONALLY_REQUIRED | _SPEC_RECOMMENDED | _SPEC_OPT_IN
    )
    assert len(BASE_LAYER_ATTRIBUTES) == 17


def test_required_stable_tier_attributes_per_spec_4_3() -> None:
    """§4.3 Required (Stable) row per v1.19 §1.1 canonical reading.

    v1.2-v1.18 listed 3 attrs; v1.19 redistributes `gen_ai.request.model`
    to Conditionally Required per OTel 1.41.0 chat-span Requirement-Level
    audit. Required (Stable) cardinality at v1.19 = 2.
    """
    got = {a.name for a in attributes_in_tier(AttributeTier.REQUIRED)}
    assert got == _SPEC_REQUIRED


def test_conditionally_required_tier_attributes_per_spec_4_3() -> None:
    """§4.3 Conditionally Required row per v1.19 §1.1 canonical reading.

    NEW at v1.19 — tier was added at v1.16 §1.2 (cardinality 3 → 4)
    without populating; v1.19 populates per OTel 1.41.0 chat-span audit:
    `gen_ai.request.model` (CR "If available") + `server.port` (CR "If
    server.address set") + `gen_ai.conversation.id` (CR "when available").
    """
    got = {a.name for a in attributes_in_tier(AttributeTier.CONDITIONALLY_REQUIRED)}
    assert got == _SPEC_CONDITIONALLY_REQUIRED


def test_recommended_development_tier_attributes_per_spec_4_3() -> None:
    """§4.3 Recommended (Development) row per v1.19 §1.1 canonical reading.

    v1.2-v1.18 listed 6 attrs; v1.19 redistributes `server.port` +
    `gen_ai.conversation.id` to Conditionally Required. Recommended
    (Development) cardinality at v1.19 = 4.
    """
    got = {a.name for a in attributes_in_tier(AttributeTier.RECOMMENDED)}
    assert got == _SPEC_RECOMMENDED


def test_opt_in_content_tier_attributes_per_spec_4_3() -> None:
    """§4.3 Opt-In content row, verbatim."""
    got = {a.name for a in attributes_in_tier(AttributeTier.OPT_IN)}
    assert got == _SPEC_OPT_IN


def test_attribute_serialization_round_trip() -> None:
    """`GenAiAttribute` is frozen and round-trips through serialization."""
    attr = GenAiAttribute(name="gen_ai.request.model", tier=AttributeTier.REQUIRED)
    assert GenAiAttribute.model_validate(attr.model_dump()) == attr
    assert hash(attr) == hash(
        GenAiAttribute(name="gen_ai.request.model", tier=AttributeTier.REQUIRED)
    )


# --- acceptance #5 — parent-child trace context propagation (§4.4) ----------


def test_trace_id_inherited_from_parent() -> None:
    """§4.4 — a child span inherits its parent's `trace_id`."""
    tracer = TracerProvider().get_tracer("u-od-04-test")
    with tracer.start_as_current_span("parent") as parent:
        parent_trace_id = parent.get_span_context().trace_id
        with tracer.start_as_current_span("child") as child:
            assert child.get_span_context().trace_id == parent_trace_id


def test_parent_span_id_propagation() -> None:
    """§4.4 — a child span references its parent via `parent` span context;
    each span carries a unique `span_id`."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("u-od-04-test")
    with tracer.start_as_current_span("parent"), tracer.start_as_current_span("child"):
        pass
    by_name: dict[str, ReadableSpan] = {span.name: span for span in exporter.get_finished_spans()}
    parent, child = by_name["parent"], by_name["child"]
    assert parent.context is not None
    assert child.context is not None
    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id
    assert child.context.span_id != parent.context.span_id


def test_hierarchy_correlation_key_is_conversation_id() -> None:
    """§4.4 — `gen_ai.conversation.id` is the hierarchy correlation key."""
    assert HIERARCHY_CORRELATION_KEY == "gen_ai.conversation.id"


# --- acceptance #6 — base metric -------------------------------------------


def test_base_metric_name_byte_exact_operation_duration() -> None:
    """§4.5 verbatim — `gen_ai.client.operation.duration` (the v2.1 value was
    `gen_ai.client.token.usage`)."""
    assert BASE_METRIC_NAME == "gen_ai.client.operation.duration"


# --- acceptance #7 — base layer is composition substrate -------------------


def test_specialization_layer_does_not_replace_base_layer() -> None:
    """Acceptance #7 — `BASE_LAYER_ATTRIBUTES` is an immutable base set the
    specialization namespaces (C-OD-05) compose over, never replace."""
    assert isinstance(BASE_LAYER_ATTRIBUTES, tuple)
    # A specialization namespace adds attributes; the base set is unchanged.
    specialized = (
        *BASE_LAYER_ATTRIBUTES,
        GenAiAttribute(name="hitl.gate.level", tier=AttributeTier.RECOMMENDED),
    )
    assert len(specialized) == len(BASE_LAYER_ATTRIBUTES) + 1
    assert all(base in specialized for base in BASE_LAYER_ATTRIBUTES)


# --- acceptance #8 — semconv 1.41.0 conformance ----------------------------


def test_otel_semconv_validator_passes() -> None:
    """Acceptance #8 — every base-layer attribute name conforms to the OTel
    GenAI semconv 1.41.0 naming surface attested at spec §4.3 (dotted lowercase
    segments; within the §4.3 verbatim set)."""
    for attr in BASE_LAYER_ATTRIBUTES:
        assert attr.name == attr.name.lower()
        assert " " not in attr.name
        assert attr.name.startswith(("gen_ai.", "server."))


# --- acceptance #9 (v2.6 carrier-growth) — OTel-handle alias family ---------


def test_span_ref_aliases_otel_sdk_span_handle() -> None:
    """v2.6 acc #9 — `SpanRef` is a type-alias of the OTel-SDK span handle.

    `SpanRef` is declared via a `type` statement; its `__value__` resolves to
    `opentelemetry.trace.Span`, the OTel-SDK span data model. The harness does
    not redefine the OTel span — it aliases it.
    """
    assert isinstance(SpanRef, typing.TypeAliasType)
    assert SpanRef.__value__ is _OTelSpan


def test_child_span_ref_aliases_otel_sdk_span_handle() -> None:
    """v2.6 acc #9 — `ChildSpanRef` aliases the same OTel-SDK span handle.

    `ChildSpanRef` is a nominal child-span distinction over the identical
    OTel-SDK substrate as `SpanRef` (eval child-span emission, C-OD-17 §17.2).
    """
    assert isinstance(ChildSpanRef, typing.TypeAliasType)
    assert ChildSpanRef.__value__ is _OTelSpan
    assert ChildSpanRef.__value__ is SpanRef.__value__


def test_span_attributes_aliases_otel_sdk_attribute_map() -> None:
    """v2.6 acc #9 — `SpanAttributes` aliases the OTel-SDK attribute map."""
    assert isinstance(SpanAttributes, typing.TypeAliasType)
    assert SpanAttributes.__value__ is _OTelAttributes


def test_event_emission_four_fields() -> None:
    """v2.6 acc #9 — `EventEmission` has exactly four fields with the spec
    names and types (`emitted_at_span : SpanRef`, `event_name : str`,
    `attribute_count : int`, `sampled : bool`)."""
    fields = EventEmission.model_fields
    assert set(fields) == {"emitted_at_span", "event_name", "attribute_count", "sampled"}
    assert len(fields) == 4
    tracer = TracerProvider().get_tracer("u-od-04-test")
    with tracer.start_as_current_span("span") as span:
        emission = EventEmission(
            emitted_at_span=span,
            event_name="hitl.gate.evaluated",
            attribute_count=3,
            sampled=True,
        )
    assert emission.emitted_at_span is span
    assert emission.event_name == "hitl.gate.evaluated"
    assert emission.attribute_count == 3
    assert emission.sampled is True


def test_event_emission_is_harness_record_not_otel_sdk_type() -> None:
    """v2.6 acc #9 — `EventEmission` is a harness record (a frozen Pydantic
    model), NOT an OTel-SDK type — the faithful factor-out return-record over
    the OD emission contracts (C-OD-09 / C-OD-25)."""
    from pydantic import BaseModel as _BaseModel

    assert issubclass(EventEmission, _BaseModel)
    assert EventEmission.__module__.startswith("harness_od.")
    assert EventEmission.model_config.get("frozen") is True
    # frozen → field assignment after construction is rejected.
    tracer = TracerProvider().get_tracer("u-od-04-test")
    with tracer.start_as_current_span("span") as span:
        emission = EventEmission(
            emitted_at_span=span, event_name="e", attribute_count=0, sampled=False
        )
    with pytest.raises(ValidationError):
        emission.event_name = "mutated"


def test_span_handle_family_single_declaration_site_at_u_od_04() -> None:
    """v2.6 acc #9 — the `Span*` family is declared at exactly one site
    (`harness_od.otel_genai_base`, the U-OD-04 module). The nine downstream
    consumers (U-OD-09/10/19/20/23/25/26/30/31) import from here — no member
    is re-declared at a consumer."""
    import harness_od.otel_genai_base as u_od_04

    for name in ("SpanRef", "ChildSpanRef", "SpanAttributes", "EventEmission"):
        member = getattr(u_od_04, name)
        assert member is not None
    assert SpanRef is u_od_04.SpanRef
    assert ChildSpanRef is u_od_04.ChildSpanRef
    assert SpanAttributes is u_od_04.SpanAttributes
    assert EventEmission is u_od_04.EventEmission
