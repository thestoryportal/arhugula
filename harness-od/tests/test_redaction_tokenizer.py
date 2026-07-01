"""R-008 OD-4 gate (b) substrate — opaque redaction tokens.

These tests cover the provider-free OD substrate only: replacing content-bearing
span attributes with opaque tokens and keeping the raw value in a token-map
sink. The eval-grade semantic classifier and durable audit-ledger persistence
remain separate cross-axis work.
"""

from __future__ import annotations

from harness_od.redaction_token_audit import compose_redaction_token_audit_entry
from harness_od.redaction_tokenizer import (
    DeterministicRedactionClassifier,
    EvalGradeSemanticRedactionClassifier,
    InMemoryRedactionTokenMap,
    OpaqueRedactionTokenizer,
    RedactionTokenRecord,
)


def test_opaque_tokenizer_replaces_value_without_leaking_raw_content() -> None:
    token_map = InMemoryRedactionTokenMap()
    tokenizer = OpaqueRedactionTokenizer(token_map=token_map)

    token = tokenizer.tokenize(
        attribute_key="gen_ai.input.messages",
        raw_value="customer ssn 123-45-6789",
        trace_id="trace-1",
        span_id="span-1",
    )

    assert token.startswith("[REDACTED:CONTENT:")
    assert token.endswith("]")
    assert "customer" not in token
    assert "123-45-6789" not in token

    [record] = token_map.records
    assert record.token == token
    assert record.attribute_key == "gen_ai.input.messages"
    assert record.raw_value == "customer ssn 123-45-6789"
    assert record.trace_id == "trace-1"
    assert record.span_id == "span-1"


def test_opaque_tokenizer_assigns_distinct_tokens_per_record() -> None:
    token_map = InMemoryRedactionTokenMap()
    tokenizer = OpaqueRedactionTokenizer(token_map=token_map)

    first = tokenizer.tokenize(
        attribute_key="gen_ai.input.messages",
        raw_value="same raw value",
        trace_id="trace-1",
        span_id="span-1",
    )
    second = tokenizer.tokenize(
        attribute_key="gen_ai.input.messages",
        raw_value="same raw value",
        trace_id="trace-1",
        span_id="span-1",
    )

    assert first != second
    assert [record.token for record in token_map.records] == [first, second]


def test_classifier_tokenizer_emits_category_specific_pii_token() -> None:
    token_map = InMemoryRedactionTokenMap()
    tokenizer = OpaqueRedactionTokenizer(
        token_map=token_map,
        classifier=DeterministicRedactionClassifier(),
    )

    token = tokenizer.tokenize(
        attribute_key="gen_ai.input.messages",
        raw_value="customer ssn 123-45-6789",
        trace_id="trace-1",
        span_id="span-1",
    )

    assert token.startswith("[REDACTED:PII:")
    assert "123-45-6789" not in token

    [record] = token_map.records
    assert record.semantic_category == "PII"
    assert record.raw_value == "customer ssn 123-45-6789"


def test_classifier_uses_attribute_shape_for_mcp_argument_tokens() -> None:
    token_map = InMemoryRedactionTokenMap()
    tokenizer = OpaqueRedactionTokenizer(
        token_map=token_map,
        classifier=DeterministicRedactionClassifier(),
    )

    token = tokenizer.tokenize(
        attribute_key="mcp.tool.call.arguments",
        raw_value='{"query":"plain tool argument"}',
        trace_id="trace-1",
        span_id="span-1",
    )

    assert token.startswith("[REDACTED:MCP_ARG:")

    [record] = token_map.records
    assert record.semantic_category == "MCP_ARG"


def test_eval_grade_classifier_preserves_genai_prompt_category_without_pii() -> None:
    classifier = EvalGradeSemanticRedactionClassifier()

    classification = classifier.classify(
        attribute_key="gen_ai.input.messages",
        raw_value="summarize the public changelog",
    )

    assert classification.category == "GENAI_PROMPT"


def test_eval_grade_classifier_uses_tool_result_category() -> None:
    classifier = EvalGradeSemanticRedactionClassifier()

    classification = classifier.classify(
        attribute_key="gen_ai.tool.call.result",
        raw_value='{"status":"ok"}',
    )

    assert classification.category == "TOOL_RESULT"


def test_redaction_token_record_composes_signed_audit_entry() -> None:
    record = RedactionTokenRecord(
        token="[REDACTED:CONTENT:000000000001]",
        attribute_key="gen_ai.input.messages",
        raw_value="customer ssn 123-45-6789",
        trace_id="trace-1",
        span_id="span-1",
    )

    entry = compose_redaction_token_audit_entry(record, key_id="redaction-test-key")

    attrs = entry.payload.audit_namespace_attrs
    assert attrs["audit.redaction_token.action_id"].startswith("redaction_token:")
    assert attrs["audit.redaction_token.response"] == "token_mapped"
    assert attrs["audit.redaction_token.token"] == "[REDACTED:CONTENT:000000000001]"
    assert attrs["audit.redaction_token.semantic_category"] == "CONTENT"
    assert attrs["audit.redaction_token.attribute_key"] == "gen_ai.input.messages"
    assert attrs["audit.redaction_token.raw_value"] == "customer ssn 123-45-6789"
    assert attrs["audit.redaction_token.trace_id"] == "trace-1"
    assert attrs["audit.redaction_token.span_id"] == "span-1"
    assert len(attrs["audit.redaction_token.raw_value_sha256"]) == 64
    assert entry.signature_attrs.audit_signature_key_id == "redaction-test-key"
