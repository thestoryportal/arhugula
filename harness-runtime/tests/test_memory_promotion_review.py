"""Tests for U-MEM-09 - promotion and review queue."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import (
    MemoryOperationKind,
    MemoryOperationProjection,
)
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import PromotionDecision, ReviewMode
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordKind,
    MemoryScope,
    MemoryVisibility,
    SourceRef,
    SourceRefType,
)
from harness_is.memory_store import CanonicalMemoryStore
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.memory_promotion import (
    PreferenceCandidateSource,
    PreferencePromotionDetails,
    PreferencePromotionValidationError,
    PreferenceSourceAuthority,
    PreferenceStrength,
    PreferenceSubject,
    PromotionCandidate,
    PromotionCandidateConfidence,
    PromotionCandidateKind,
    PromotionDecisionService,
    PromotionReviewRequiredError,
    SemanticInjectionPolicy,
    SemanticRecordStatus,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_NOW = datetime(2026, 7, 1, 18, 0, 0, tzinfo=UTC)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="codex")


def _store(tmp_path: Path) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _scope() -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        visibility=MemoryVisibility.WORKFLOW,
    )


def _source_ref(ref: str = "turn-1") -> SourceRef:
    return SourceRef(ref_type=SourceRefType.TURN, ref=ref)


def _candidate(
    *,
    kind: PromotionCandidateKind = PromotionCandidateKind.FACT,
    confidence: PromotionCandidateConfidence = PromotionCandidateConfidence.HIGH,
    source_refs: tuple[SourceRef, ...] | None = None,
    preference_source: PreferenceCandidateSource | None = None,
    review_required: bool = False,
    auto_promote_allowed: bool = True,
) -> PromotionCandidate:
    return PromotionCandidate(
        candidate_id=f"candidate:{kind.value}",
        source_refs=source_refs or (_source_ref(),),
        source_memory_refs=(MemoryID("mem:episodic:episodic_turn:" + "1" * 64),),
        proposed_kind=kind,
        statement=f"Promote {kind.value} statement.",
        confidence=confidence,
        suggested_scope=_scope(),
        risk_flags=(),
        preference_source=preference_source,
        policy_decision=PromotionDecision.PROMOTE_SEMANTIC,
        review_mode=ReviewMode.OPERATOR_REQUIRED if review_required else ReviewMode.AUTOMATIC,
        review_required=review_required,
        auto_promote_allowed=auto_promote_allowed,
    )


def _service(
    store: CanonicalMemoryStore,
    *,
    tracer_provider: TracerProvider | None = None,
) -> PromotionDecisionService:
    return PromotionDecisionService(
        store=store,
        actor=_ACTOR,
        policy_ref="policy:memory-test",
        run_id="run-u-mem-09",
        cli_profile="codex",
        provider="openai",
        model="gpt-5",
        tracer_provider=tracer_provider,
    )


def _preference_details(
    *,
    authority: PreferenceSourceAuthority = PreferenceSourceAuthority.OPERATOR_DIRECT,
) -> PreferencePromotionDetails:
    return PreferencePromotionDetails(
        preference_subject=PreferenceSubject.TOOL_USE,
        preference_strength=PreferenceStrength.STRONG,
        source_authority=authority,
        confirmation_required=False,
    )


def test_approve_writes_active_semantic_record_and_promote_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = _service(store)

    result = service.approve(
        _candidate(),
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
    )

    assert result.status is SemanticRecordStatus.ACTIVE
    assert result.record.envelope.kind is MemoryRecordKind.SEMANTIC_FACT
    assert result.record.content["semantic_kind"] == "fact"
    assert result.record.content["status"] == "active"
    assert result.record.content["evidence"] == [_source_ref().model_dump(mode="json")]
    assert result.record.content["injection_policy"] == "retrieval_only"

    stored = store.read_record(result.memory_id, MemoryRecordKind.SEMANTIC_FACT)
    assert stored.envelope.memory_id == result.memory_id

    [operation] = store.read_memory_operations()
    assert operation.operation_kind is MemoryOperationKind.PROMOTE
    assert operation.operation_projection is MemoryOperationProjection.PROMOTION_DECISIONS
    assert operation.memory_refs == (result.memory_id,)
    assert operation.policy_ref == "policy:memory-test"


def test_promotion_decision_emits_c_mem_19_span(tmp_path: Path) -> None:
    tracer_provider, exporter = _tracer_provider()
    store = _store(tmp_path)
    service = _service(store, tracer_provider=tracer_provider)

    result = service.approve(
        _candidate(),
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
    )

    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "promotion"
    assert attrs["memory.operation.kind"] == MemoryOperationKind.PROMOTE.value
    assert attrs["memory.provider"] == "openai"
    assert attrs["memory.model"] == "gpt-5"
    assert attrs["memory.cli_profile"] == "codex"
    assert attrs["memory.policy.decision"] == SemanticRecordStatus.ACTIVE.value
    assert attrs["memory.record_count"] == 1
    assert attrs["memory.tier"] == result.record.envelope.tier.value


def test_review_required_candidate_cannot_be_active_until_operator_approves(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = _service(store)
    candidate = _candidate(review_required=True, auto_promote_allowed=False)

    with pytest.raises(PromotionReviewRequiredError):
        service.approve(
            candidate,
            timestamp=_NOW,
            injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
        )

    proposed = service.propose_for_review(
        candidate,
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
    )
    approved = service.approve(
        candidate,
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
        operator_approved=True,
    )

    assert proposed.status is SemanticRecordStatus.PROPOSED
    assert approved.status is SemanticRecordStatus.ACTIVE
    assert [operation.operation_kind for operation in store.read_memory_operations()] == [
        MemoryOperationKind.PROPOSE_PROMOTION,
        MemoryOperationKind.PROMOTE,
    ]


def test_deny_persists_denied_record_and_denial_ledger_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = _service(store)

    result = service.deny(_candidate(), timestamp=_NOW, reason="not durable enough")

    assert result.status is SemanticRecordStatus.DENIED
    assert result.record.content["status"] == "denied"
    assert result.record.content["review_reason"] == "not durable enough"
    [operation] = store.read_memory_operations()
    assert operation.operation_kind is MemoryOperationKind.DENY_PROMOTION
    assert operation.memory_refs == (result.memory_id,)


def test_proposed_procedural_update_is_persisted_for_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = _service(store)

    result = service.propose_for_review(
        _candidate(
            kind=PromotionCandidateKind.PROCEDURAL_UPDATE,
            review_required=True,
            auto_promote_allowed=False,
        ),
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.NEVER,
    )

    assert result.status is SemanticRecordStatus.PROPOSED
    assert result.record.envelope.kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT
    assert result.record.content["status"] == "proposed"
    assert result.record.content["procedural_update"] == "Promote procedural_update statement."
    [operation] = store.read_memory_operations()
    assert operation.operation_kind is MemoryOperationKind.PROPOSE_PROMOTION


def test_preference_promotion_requires_details_and_injection_policy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = _service(store)
    candidate = _candidate(
        kind=PromotionCandidateKind.PREFERENCE,
        preference_source=PreferenceCandidateSource.OPERATOR_DIRECT,
    )

    with pytest.raises(PreferencePromotionValidationError):
        service.approve(candidate, timestamp=_NOW)
    with pytest.raises(PreferencePromotionValidationError):
        service.approve(
            candidate,
            timestamp=_NOW,
            injection_policy=SemanticInjectionPolicy.PROMPT_PACKET_ALLOWED,
        )

    result = service.approve(
        candidate,
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.PROMPT_PACKET_ALLOWED,
        preference_details=_preference_details(),
    )

    assert result.record.envelope.kind is MemoryRecordKind.PREFERENCE
    assert result.record.content["preference_subject"] == "tool_use"
    assert result.record.content["preference_strength"] == "strong"
    assert result.record.content["source_authority"] == "operator_direct"
    assert result.record.content["injection_policy"] == "prompt_packet_allowed"


def test_inferred_preference_with_one_source_remains_proposed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = _service(store)
    candidate = _candidate(
        kind=PromotionCandidateKind.PREFERENCE,
        preference_source=PreferenceCandidateSource.INFERRED,
    )

    proposed = service.propose_for_review(
        candidate,
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.NEVER,
        preference_details=_preference_details(
            authority=PreferenceSourceAuthority.INFERRED_FROM_REPETITION
        ),
    )
    with pytest.raises(PreferencePromotionValidationError):
        service.approve(
            candidate,
            timestamp=_NOW,
            injection_policy=SemanticInjectionPolicy.NEVER,
            preference_details=_preference_details(
                authority=PreferenceSourceAuthority.INFERRED_FROM_REPETITION
            ),
            operator_approved=True,
        )

    assert proposed.status is SemanticRecordStatus.PROPOSED


def test_edit_and_supersede_flow_replaces_statement_and_links_superseded_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = _service(store)
    candidate = _candidate()
    first = service.approve(
        candidate,
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
    )

    edited = service.edit_and_approve(
        candidate,
        statement="Edited operator-approved statement.",
        timestamp=_NOW,
        injection_policy=SemanticInjectionPolicy.RETRIEVAL_ONLY,
        operator_approved=True,
        supersedes=(first.memory_id,),
    )

    assert edited.record.content["statement"] == "Edited operator-approved statement."
    assert edited.record.envelope.supersedes == (first.memory_id,)
    assert [operation.operation_kind for operation in store.read_memory_operations()] == [
        MemoryOperationKind.PROMOTE,
        MemoryOperationKind.PROMOTE,
    ]
