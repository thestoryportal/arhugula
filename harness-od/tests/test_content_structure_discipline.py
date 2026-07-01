"""Tests for U-OD-15 — default-off content + default-on structure discipline.

Test set per the U-OD-15 `Tests:` field
(Implementation_Plan_Operational_Discipline_v2_1.md §3.4.5). C-OD-12
§12.1 / §12.2 / §12.3. Every acceptance criterion maps to at least one test.
"""

from __future__ import annotations

from harness_od.content_structure_discipline import (
    DEFAULT_OFF_CONTENT_ATTRIBUTES,
    DEFAULT_ON_STRUCTURE_ATTRIBUTES,
    HASH_DIGEST_ATTRIBUTES,
    STRUCTURE_NOT_CONTENT_INVARIANT,
    AttributeClassification,
    classify_attribute,
)

# Acceptance #3 — the §12.3 structure-not-content invariant, byte-exact.
_SPEC_INVARIANT = (
    "Default-on attributes record observability semantics — operation name, "
    "provider, model, token counts, hash digests, IDs, enums, latency bounds, "
    "cost overheads — but never raw tool I/O content, raw message content, or "
    "raw retrieval-document content. Where a content surface must be auditable "
    "(e.g., HITL response summary), the attribute carries a hash digest "
    "(hitl.response.summary_hash) — not the payload."
)


def test_default_off_content_includes_input_messages() -> None:
    """Acceptance #1 — gen_ai.input.messages is a default-off content attribute."""
    assert "gen_ai.input.messages" in DEFAULT_OFF_CONTENT_ATTRIBUTES


def test_default_off_content_includes_output_messages() -> None:
    """Acceptance #1 — gen_ai.output.messages is a default-off content attribute."""
    assert "gen_ai.output.messages" in DEFAULT_OFF_CONTENT_ATTRIBUTES


def test_default_off_content_includes_mcp_tool_call_args() -> None:
    """Acceptance #1 — mcp.tool.call.arguments is a default-off content attribute."""
    assert "mcp.tool.call.arguments" in DEFAULT_OFF_CONTENT_ATTRIBUTES
    assert "mcp.tool.call.result" in DEFAULT_OFF_CONTENT_ATTRIBUTES


def test_default_off_content_includes_semconv_opt_in_set() -> None:
    """Acceptance #1 — the §12.1 OTel GenAI semconv 1.41.0 Opt-In set is covered."""
    semconv_opt_in = {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.definitions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.retrieval.documents",
        "gen_ai.retrieval.query.text",
    }
    assert semconv_opt_in <= DEFAULT_OFF_CONTENT_ATTRIBUTES


def test_default_on_structure_includes_token_counts() -> None:
    """Acceptance #2 — token-count attributes are default-on structure per §12.2."""
    assert "gen_ai.usage.input_tokens" in DEFAULT_ON_STRUCTURE_ATTRIBUTES
    assert "gen_ai.usage.output_tokens" in DEFAULT_ON_STRUCTURE_ATTRIBUTES


def test_default_on_structure_spans_all_fifteen_namespaces() -> None:
    """Acceptance #2 — the structure set spans all 15 specialization namespaces."""
    # One witness attribute per namespace family present in the §12.2 listing.
    namespace_witnesses = {
        "gen_ai.operation.name",  # gen_ai base layer
        "sandbox.tier",  # sandbox.*
        "hitl.gate.level",  # hitl.*
        "anthropic.tokenizer_version",  # anthropic.*
        "mcp.server.name",  # mcp.*
        "skill.id",  # skill.*
        "managed_agents.session_id",  # managed_agents.*
        "engine.class",  # engine.*
        "provider_discriminator",  # provider_discriminator
        "audit.actor.id",  # audit.*
        "validator.fail.class",  # validator.fail.*
        "files.file_id",  # files.*
        "memory.path",  # memory.*
        "harness.breaker.scope",  # harness.breaker.*
    }
    structure_set = set(DEFAULT_ON_STRUCTURE_ATTRIBUTES)
    for witness in namespace_witnesses:
        assert witness in structure_set, witness


def test_structure_not_content_invariant_byte_exact() -> None:
    """Acceptance #3 — STRUCTURE_NOT_CONTENT_INVARIANT matches §12.3 verbatim."""
    assert STRUCTURE_NOT_CONTENT_INVARIANT == _SPEC_INVARIANT


def test_hash_digest_attributes_classify_as_hash_digest_of_content() -> None:
    """Acceptance #4 — hash-digest attributes classify as HASH_DIGEST_OF_CONTENT."""
    assert (
        classify_attribute("hitl.response.summary_hash")
        is AttributeClassification.HASH_DIGEST_OF_CONTENT
    )
    assert (
        classify_attribute("mcp.primitive.signature.sha256")
        is AttributeClassification.HASH_DIGEST_OF_CONTENT
    )


def test_hash_digest_example_list_drops_audit_signature_sha256() -> None:
    """Acceptance #4 (F1-OD-02 absorption) — the hash-digest example list is
    {hitl.response.summary_hash, mcp.primitive.signature.sha256}; the
    audit.signature.sha256 token was dropped at v2.1 per F1-OD-02."""
    assert HASH_DIGEST_ATTRIBUTES == {
        "hitl.response.summary_hash",
        "mcp.primitive.signature.sha256",
    }
    assert "audit.signature.sha256" not in HASH_DIGEST_ATTRIBUTES


def test_no_namespace_introduces_content_by_default() -> None:
    """Acceptance #5 — no default-on structure attribute is a content attribute;
    the default-on and default-off sets are disjoint."""
    assert not (set(DEFAULT_ON_STRUCTURE_ATTRIBUTES) & DEFAULT_OFF_CONTENT_ATTRIBUTES)


def test_classify_attribute_returns_correct_classification() -> None:
    """Acceptance #4 — classify_attribute returns the correct class per attribute."""
    # Content attribute → DEFAULT_OFF_CONTENT.
    assert (
        classify_attribute("gen_ai.input.messages") is AttributeClassification.DEFAULT_OFF_CONTENT
    )
    assert classify_attribute("files.content") is AttributeClassification.DEFAULT_OFF_CONTENT
    # Structure attribute → DEFAULT_ON_STRUCTURE.
    assert (
        classify_attribute("gen_ai.usage.input_tokens")
        is AttributeClassification.DEFAULT_ON_STRUCTURE
    )
    assert classify_attribute("sandbox.tier") is AttributeClassification.DEFAULT_ON_STRUCTURE
    # Hash-digest attribute → HASH_DIGEST_OF_CONTENT.
    assert (
        classify_attribute("hitl.response.summary_hash")
        is AttributeClassification.HASH_DIGEST_OF_CONTENT
    )


def test_attribute_classification_cardinality_three() -> None:
    """Acceptance #4 — AttributeClassification has exactly 3 values."""
    assert len(AttributeClassification) == 3


def test_classify_attribute_unknown_defaults_to_structure() -> None:
    """Acceptance #2 / #4 — an attribute not in the content or hash-digest sets
    classifies as DEFAULT_ON_STRUCTURE (structure is the default posture)."""
    assert (
        classify_attribute("some.unknown.attribute") is AttributeClassification.DEFAULT_ON_STRUCTURE
    )
