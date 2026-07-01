"""Default-off content + default-on structure attribute discipline — U-OD-15.

Implements C-OD-12 §12.1 (default-off content attributes), §12.2 (default-on
structure attributes), §12.3 (structure-not-content invariant).

The redaction discipline at the OD axis: content-bearing attributes (raw
message / tool I/O / retrieval-document content, PII-bearing per OTel GenAI
semconv 1.41.0 Opt-In classification) are default-off at all cells;
structure-bearing attributes (operation name, provider, model, token counts,
hash digests, IDs, enums, latency bounds, cost overheads) are default-on. Where
a content surface must be auditable, the attribute carries a hash digest — never
the payload (the hash-not-payload discipline).

`classify_attribute` partitions any attribute name into one of three classes:
`DEFAULT_OFF_CONTENT`, `DEFAULT_ON_STRUCTURE`, or `HASH_DIGEST_OF_CONTENT` —
the last composing with the structure-not-content invariant (hash digests are
default-on structure that stand in for content).

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.4.5 U-OD-15
(v2.1 absorbs F1-OD-02 per §0.3 — `audit.signature.sha256` dropped from the
acceptance-#4 hash-digest example list; preserved verbatim through v2.5 §0.3 +
v2.6 §3 — no delta); Spec_Operational_Discipline_v1_2.md §12 C-OD-12
§12.1 / §12.2 / §12.3 (preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1
§1.4 redaction discipline.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "DEFAULT_OFF_CONTENT_ATTRIBUTES",
    "DEFAULT_ON_STRUCTURE_ATTRIBUTES",
    "HASH_DIGEST_ATTRIBUTES",
    "STRUCTURE_NOT_CONTENT_INVARIANT",
    "AttributeClassification",
    "classify_attribute",
]


class AttributeClassification(StrEnum):
    """The 3-class partition of any observability attribute (C-OD-12 §12.1-§12.3).

    `DEFAULT_OFF_CONTENT` — content-bearing (PII-bearing); default-off at all
    cells. `DEFAULT_ON_STRUCTURE` — structure-bearing observability semantics;
    default-on. `HASH_DIGEST_OF_CONTENT` — a hash digest standing in for an
    auditable content surface; default-on, composing with the
    structure-not-content invariant.
    """

    DEFAULT_OFF_CONTENT = "DEFAULT_OFF_CONTENT"
    DEFAULT_ON_STRUCTURE = "DEFAULT_ON_STRUCTURE"
    HASH_DIGEST_OF_CONTENT = "HASH_DIGEST_OF_CONTENT"


#: Content-bearing attributes — default-off at all cells per C-OD-12 §12.1.
#: Per-persona-tier override gradient at C-OD-13 (U-OD-16). The §12.1 OTel
#: GenAI semconv 1.41.0 Opt-In set plus the cross-namespace content surfaces.
DEFAULT_OFF_CONTENT_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.definitions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.retrieval.documents",
        "gen_ai.retrieval.query.text",
        "mcp.tool.call.arguments",
        "mcp.tool.call.result",
        "skill.body_content",
        "memory.content",
        "files.content",
    }
)


#: Default-on structure attributes — observability semantics, never raw content
#: (C-OD-12 §12.2). The ~50-attribute set spanning the 15 specialization-layer
#: namespaces, transcribed verbatim from the §12.2 per-namespace listing.
DEFAULT_ON_STRUCTURE_ATTRIBUTES: tuple[str, ...] = (
    # gen_ai base layer
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "server.address",
    "server.port",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.response.finish_reasons",
    "gen_ai.conversation.id",
    # sandbox.*
    "sandbox.tier",
    "sandbox.tech",
    "sandbox.provider",
    "sandbox.fail.class",
    "sandbox.policy.assigned_tier_reason",
    "sandbox.cost.tier_overhead_ms",
    "sandbox.cost.tier_overhead_usd",
    # hitl.*
    "hitl.gate.level",
    "hitl.gate.persona_tier",
    "hitl.gate.required",
    "hitl.invocation.placement",
    "hitl.invocation.handoff_context_size_bytes",
    "hitl.invocation.audit_ledger_entry_id",
    "hitl.response.class",
    "hitl.response.latency_ms",
    "hitl.response.summary_hash",
    "hitl.timeout.duration_ms",
    "hitl.timeout.degradation_mode_applied",
    # anthropic.*
    "anthropic.cache_creation_input_tokens",
    "anthropic.cache_read_input_tokens",
    "anthropic.cache_breakpoint_id",
    "anthropic.cache_ttl_seconds",
    "anthropic.thinking_mode",
    "anthropic.thinking_budget_tokens",
    "anthropic.thinking_effort",
    "anthropic.batch_id",
    "anthropic.tokenizer_version",
    "anthropic.inference_geo",
    # mcp.*
    "mcp.server.name",
    "mcp.server.trust_tier",
    "mcp.protocol_version",
    "mcp.transport",
    "mcp.auth_present",
    "mcp.primitive.kind",
    "mcp.primitive.signature.sha256",
    # skill.*
    "skill.id",
    "skill.name",
    "skill.version_sha",
    "skill.frontmatter.version",
    "skill.body_tokens",
    "skill.activation_mode",
    # managed_agents.*
    "managed_agents.runtime_ms",
    "managed_agents.session_id",
    "managed_agents.billable_seconds",
    # engine.*
    "engine.class",
    "engine.event_history.tier",
    "engine.event.id",
    # provider_discriminator
    "provider_discriminator",
    # audit.*
    "audit.signature.sha256",
    "audit.signature.prior_hash",
    "audit.actor.id",
    "audit.signature.value",
    "audit.signature.algorithm",
    "audit.signature.key_id",
    "audit.signature.key_period",
    # validator.fail.*
    "validator.fail.class",
    "validator.fail.cause_attribution",
    "validator.fail.permanence",
    # files.*
    "files.operation.kind",
    "files.file_id",
    "files.filename",
    "files.mime_type",
    "files.size_bytes",
    "files.workspace_id",
    "files.batch_composition",
    "files.code_execution_composition",
    # memory.*
    "memory.operation.kind",
    "memory.path",
    "memory.backend",
    "memory.bytes_read",
    "memory.bytes_written",
    "memory.context_editing_active",
    # harness.breaker.*
    "harness.breaker.scope",
    "harness.breaker.from_state",
    "harness.breaker.to_state",
    "harness.breaker.trigger_count",
    "harness.breaker.permanent_fail_repeats",
    "harness.breaker.tool_id",
    "harness.breaker.model_version",
)


#: Hash-digest attributes — default-on structure that stands in for an auditable
#: content surface per the §12.3 hash-not-payload discipline. Per the U-OD-15
#: acceptance-#4 example list (F1-OD-02-absorbed at v2.1 — `audit.signature.sha256`
#: dropped from the example list as it is a hash-chain link, not a
#: content-summary digest). These classify as `HASH_DIGEST_OF_CONTENT`.
HASH_DIGEST_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "hitl.response.summary_hash",
        "mcp.primitive.signature.sha256",
    }
)


#: The structure-not-content invariant — C-OD-12 §12.3, transcribed verbatim
#: from the U-OD-15 plan Signatures block (acceptance #3, byte-exact).
STRUCTURE_NOT_CONTENT_INVARIANT: str = (
    "Default-on attributes record observability semantics — operation name, "
    "provider, model, token counts, hash digests, IDs, enums, latency bounds, "
    "cost overheads — but never raw tool I/O content, raw message content, or "
    "raw retrieval-document content. Where a content surface must be auditable "
    "(e.g., HITL response summary), the attribute carries a hash digest "
    "(hitl.response.summary_hash) — not the payload."
)


def classify_attribute(attr: str) -> AttributeClassification:
    """Classify an observability attribute name per C-OD-12 §12.1-§12.3.

    Returns `HASH_DIGEST_OF_CONTENT` for hash-digest attributes (they are
    default-on but stand in for a content surface); `DEFAULT_OFF_CONTENT` for
    content-bearing attributes; `DEFAULT_ON_STRUCTURE` otherwise. The hash-digest
    check precedes the structure check so the more-specific class wins
    (acceptance #4).
    """
    if attr in HASH_DIGEST_ATTRIBUTES:
        return AttributeClassification.HASH_DIGEST_OF_CONTENT
    if attr in DEFAULT_OFF_CONTENT_ATTRIBUTES:
        return AttributeClassification.DEFAULT_OFF_CONTENT
    return AttributeClassification.DEFAULT_ON_STRUCTURE
