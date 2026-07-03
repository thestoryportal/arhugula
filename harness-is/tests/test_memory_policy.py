"""Tests for U-MEM-04 - memory policy model (C-MEM-09)."""

from __future__ import annotations

import importlib

from harness_is.memory_record_envelope import MemoryRecordKind, MemoryScope, MemoryVisibility


def _policy_module():
    return importlib.import_module("harness_is.memory_policy")


def _scope(
    *,
    project: str | None = "arhugula-v2",
    workflow: str | None = "workflow-a",
    provider_family: str | None = None,
    visibility: MemoryVisibility = MemoryVisibility.WORKFLOW,
) -> MemoryScope:
    return MemoryScope(
        project=project,
        workflow=workflow,
        provider_family=provider_family,
        visibility=visibility,
    )


def _enabled_policy(**overrides: object):
    m = _policy_module()
    return m.MemoryPolicyDocument(
        policy_id="policy:test",
        enabled=True,
        **overrides,
    )


def test_memory_policy_vocabularies_match_c_mem_09() -> None:
    m = _policy_module()

    assert {decision.value for decision in m.CaptureDecision} == {
        "deny",
        "summarize_only",
        "capture_full",
        "capture_redacted",
    }
    assert {decision.value for decision in m.PromotionDecision} == {
        "discard",
        "keep_episodic",
        "propose_semantic",
        "promote_semantic",
        "propose_procedural",
        "promote_procedural",
    }
    assert {decision.value for decision in m.AccessDecision} == {
        "deny",
        "retrieval_only",
        "prompt_packet",
        "standard_tools",
        "native_provider",
    }
    assert {mode.value for mode in m.ReviewMode} == {
        "automatic",
        "operator_required",
        "forbidden",
    }
    assert {decision.value for decision in m.RetentionDecision} == {
        "retain",
        "expire",
        "prune",
        "tombstone",
    }
    assert {decision.value for decision in m.RedactionDecision} == {
        "none",
        "redact",
        "tombstone",
    }


def test_policy_document_declares_schema_fields_and_disabled_default() -> None:
    m = _policy_module()

    assert set(m.MemoryPolicyDocument.model_fields) == {
        "schema_version",
        "policy_id",
        "enabled",
        "capture_decision",
        "promotion_decision",
        "retrieval_access",
        "injection_access",
        "native_memory_access",
        "standard_tool_access",
        "review_mode",
        "retention_decision",
        "redaction_decision",
        "eligible_record_kinds",
    }

    default = m.DEFAULT_DISABLED_MEMORY_POLICY
    assert default.enabled is False
    assert default.capture_decision is m.CaptureDecision.DENY
    assert default.promotion_decision is m.PromotionDecision.DISCARD
    assert default.retrieval_access is m.AccessDecision.DENY
    assert default.injection_access is m.AccessDecision.DENY
    assert default.native_memory_access is m.AccessDecision.DENY
    assert default.standard_tool_access is m.AccessDecision.DENY
    assert default.review_mode is m.ReviewMode.FORBIDDEN


def test_default_disabled_policy_denies_every_memory_surface() -> None:
    m = _policy_module()
    resolver = m.MemoryPolicyResolver()
    scope = _scope()

    assert resolver.resolve_capture().capture_decision is m.CaptureDecision.DENY
    assert resolver.resolve_promotion().promotion_decision is m.PromotionDecision.DISCARD
    assert (
        resolver.resolve_retrieval(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=scope,
            requested_scope=scope,
        ).access_decision
        is m.AccessDecision.DENY
    )
    assert (
        resolver.resolve_injection(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=scope,
            requested_scope=scope,
        ).access_decision
        is m.AccessDecision.DENY
    )
    assert resolver.resolve_native_memory().access_decision is m.AccessDecision.DENY
    assert resolver.resolve_standard_tools().access_decision is m.AccessDecision.DENY


def test_capture_resolution_supports_spec_matrix() -> None:
    m = _policy_module()

    for decision in m.CaptureDecision:
        resolver = m.MemoryPolicyResolver(
            _enabled_policy(capture_decision=decision, review_mode=m.ReviewMode.AUTOMATIC)
        )
        result = resolver.resolve_capture()
        assert result.capture_decision is decision
        assert result.review_mode is m.ReviewMode.AUTOMATIC


def test_promotion_resolution_supports_discard_queue_and_allow() -> None:
    m = _policy_module()

    expected = {
        m.PromotionDecision.DISCARD,
        m.PromotionDecision.KEEP_EPISODIC,
        m.PromotionDecision.PROPOSE_SEMANTIC,
        m.PromotionDecision.PROMOTE_SEMANTIC,
        m.PromotionDecision.PROPOSE_PROCEDURAL,
        m.PromotionDecision.PROMOTE_PROCEDURAL,
    }
    observed = set()
    for decision in expected:
        resolver = m.MemoryPolicyResolver(
            _enabled_policy(
                promotion_decision=decision,
                review_mode=m.ReviewMode.OPERATOR_REQUIRED,
            )
        )
        result = resolver.resolve_promotion()
        observed.add(result.promotion_decision)
        assert result.review_mode is m.ReviewMode.OPERATOR_REQUIRED

    assert observed == expected


def test_record_kind_filter_denies_unlisted_retrieval_and_injection() -> None:
    m = _policy_module()
    scope = _scope()
    resolver = m.MemoryPolicyResolver(
        _enabled_policy(
            retrieval_access=m.AccessDecision.RETRIEVAL_ONLY,
            injection_access=m.AccessDecision.PROMPT_PACKET,
            eligible_record_kinds=(MemoryRecordKind.PREFERENCE,),
        )
    )

    assert (
        resolver.resolve_retrieval(
            record_kind=MemoryRecordKind.PREFERENCE,
            record_scope=scope,
            requested_scope=scope,
        ).access_decision
        is m.AccessDecision.RETRIEVAL_ONLY
    )
    assert (
        resolver.resolve_retrieval(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=scope,
            requested_scope=scope,
        ).access_decision
        is m.AccessDecision.DENY
    )
    assert (
        resolver.resolve_injection(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=scope,
            requested_scope=scope,
        ).access_decision
        is m.AccessDecision.DENY
    )


def test_injection_cannot_be_broader_than_record_scope() -> None:
    m = _policy_module()
    record_scope = _scope(workflow="workflow-a", visibility=MemoryVisibility.WORKFLOW)
    same_scope = _scope(workflow="workflow-a", visibility=MemoryVisibility.WORKFLOW)
    narrower_scope = _scope(
        workflow="workflow-a",
        provider_family="openai",
        visibility=MemoryVisibility.PRIVATE,
    )
    broader_scope = _scope(workflow=None, visibility=MemoryVisibility.PROJECT)
    resolver = m.MemoryPolicyResolver(
        _enabled_policy(injection_access=m.AccessDecision.PROMPT_PACKET)
    )

    assert (
        resolver.resolve_injection(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=record_scope,
            requested_scope=same_scope,
        ).access_decision
        is m.AccessDecision.PROMPT_PACKET
    )
    assert (
        resolver.resolve_injection(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=record_scope,
            requested_scope=narrower_scope,
        ).access_decision
        is m.AccessDecision.PROMPT_PACKET
    )
    assert (
        resolver.resolve_injection(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=record_scope,
            requested_scope=broader_scope,
        ).access_decision
        is m.AccessDecision.DENY
    )


def test_policy_failure_denies_promotion_and_injection() -> None:
    m = _policy_module()

    def _broken_policy() -> object:
        raise RuntimeError("policy store unavailable")

    resolver = m.MemoryPolicyResolver(policy_source=_broken_policy)
    scope = _scope()

    promotion = resolver.resolve_promotion()
    injection = resolver.resolve_injection(
        record_kind=MemoryRecordKind.SEMANTIC_FACT,
        record_scope=scope,
        requested_scope=scope,
    )

    assert promotion.promotion_decision is m.PromotionDecision.DISCARD
    assert promotion.failure_reason == "policy store unavailable"
    assert injection.access_decision is m.AccessDecision.DENY
    assert injection.failure_reason == "policy store unavailable"


def test_native_and_standard_tool_access_cannot_bypass_policy() -> None:
    m = _policy_module()
    scope = _scope()
    resolver = m.MemoryPolicyResolver(
        _enabled_policy(
            injection_access=m.AccessDecision.PROMPT_PACKET,
            native_memory_access=m.AccessDecision.DENY,
            standard_tool_access=m.AccessDecision.DENY,
        )
    )

    assert (
        resolver.resolve_injection(
            record_kind=MemoryRecordKind.SEMANTIC_FACT,
            record_scope=scope,
            requested_scope=scope,
        ).access_decision
        is m.AccessDecision.PROMPT_PACKET
    )
    assert resolver.resolve_native_memory().access_decision is m.AccessDecision.DENY
    assert resolver.resolve_standard_tools().access_decision is m.AccessDecision.DENY

    permissive = m.MemoryPolicyResolver(
        _enabled_policy(
            native_memory_access=m.AccessDecision.NATIVE_PROVIDER,
            standard_tool_access=m.AccessDecision.STANDARD_TOOLS,
        )
    )
    assert permissive.resolve_native_memory().access_decision is m.AccessDecision.NATIVE_PROVIDER
    assert permissive.resolve_standard_tools().access_decision is m.AccessDecision.STANDARD_TOOLS


def test_record_kind_filter_does_not_hide_tool_surface_access() -> None:
    m = _policy_module()
    resolver = m.MemoryPolicyResolver(
        _enabled_policy(
            standard_tool_access=m.AccessDecision.STANDARD_TOOLS,
            eligible_record_kinds=(MemoryRecordKind.PREFERENCE,),
        )
    )

    assert resolver.resolve_standard_tools().access_decision is m.AccessDecision.STANDARD_TOOLS


def test_memory_policy_package_re_exports() -> None:
    h_is = importlib.import_module("harness_is")
    m = _policy_module()

    assert h_is.MemoryPolicyDocument is m.MemoryPolicyDocument
    assert h_is.MemoryPolicyResolver is m.MemoryPolicyResolver
    assert h_is.DEFAULT_DISABLED_MEMORY_POLICY is m.DEFAULT_DISABLED_MEMORY_POLICY
