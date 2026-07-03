"""Tests for U-MEM-11 - retrieval, ranking, and packet assembly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import MemoryOperationKind, MemoryOperationProjection
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import (
    AccessDecision,
    MemoryPolicyDocument,
    MemoryPolicyResolver,
    ReviewMode,
)
from harness_is.memory_record_envelope import (
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    MemoryVisibility,
    RedactionState,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_retrieval import (
    MemoryPacketAccessMode,
    MemoryRetrievalRequest,
    MemoryRetriever,
    RetrievalExclusionReason,
)
from harness_is.memory_retrieval_index import DerivedRetrievalIndexStore
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, ActorClass

_NOW = datetime(2026, 7, 2, 14, 30, 0, tzinfo=UTC)


def _binding(tmp_path: Path) -> MemoryRootBinding:
    return MemoryRootBinding(default_root=tmp_path / "memory")


def _store(binding: MemoryRootBinding) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _index_store(binding: MemoryRootBinding) -> DerivedRetrievalIndexStore:
    return DerivedRetrievalIndexStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _scope(
    *,
    workflow: str = "memory-substrate",
    cli_profile: str = "codex",
    visibility: MemoryVisibility = MemoryVisibility.WORKFLOW,
) -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow=workflow,
        workload_class="coding-arc",
        cli_profile=cli_profile,
        provider_family="openai",
        visibility=visibility,
    )


def _content_for(
    kind: MemoryRecordKind,
    *,
    statement: str,
    tags: tuple[str, ...] = (),
    confidence: str = "high",
    source_authority: str = "operator_direct",
    status: str = "active",
    pinned: bool = False,
) -> dict[str, object]:
    common: dict[str, object] = {
        "semantic_kind": kind.value,
        "statement": statement,
        "confidence": confidence,
        "source_authority": source_authority,
        "status": status,
        "injection_policy": "retrieval_only",
        "tags": list(tags),
        "pinned": pinned,
    }
    if kind is MemoryRecordKind.PREFERENCE:
        return {
            **common,
            "preference_subject": "operator_workflow",
            "preference_strength": "strong",
            "confirmation_required": False,
        }
    if kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT:
        return {
            "snapshot_id": statement.lower().replace(" ", "-"),
            "workflow_id": "memory-substrate",
            "cli_profile": "codex",
            "status": status,
            "procedural_update": statement,
            "tags": list(tags),
        }
    return common


def _record(
    *,
    kind: MemoryRecordKind,
    statement: str,
    tags: tuple[str, ...] = (),
    confidence: str = "high",
    source_authority: str = "operator_direct",
    status: str = "active",
    pinned: bool = False,
    created_at: datetime = _NOW,
    scope: MemoryScope | None = None,
    redaction_state: RedactionState = RedactionState.ACTIVE,
) -> MemoryStoreRecord:
    tier = (
        MemoryTier.PROCEDURAL
        if kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT
        else MemoryTier.SEMANTIC
    )
    content = _content_for(
        kind,
        statement=statement,
        tags=tags,
        confidence=confidence,
        source_authority=source_authority,
        status=status,
        pinned=pinned,
    )
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(tier, kind, content_hash),
            schema_version="memory-store-record/v1",
            tier=tier,
            kind=kind,
            created_at=created_at,
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:u-mem-11"),),
            scope=scope or _scope(),
            content_hash=content_hash,
            redaction_state=redaction_state,
        ),
        content=content,
    )


def _enabled_policy(**overrides: object) -> MemoryPolicyDocument:
    return MemoryPolicyDocument(
        policy_id="policy:u-mem-11-test",
        enabled=True,
        retrieval_access=AccessDecision.RETRIEVAL_ONLY,
        injection_access=AccessDecision.PROMPT_PACKET,
        review_mode=ReviewMode.AUTOMATIC,
        **overrides,
    )


def _request(*, token_budget: int = 120) -> MemoryRetrievalRequest:
    return MemoryRetrievalRequest(
        run_id="run-u-mem-11",
        workflow_id="memory-substrate",
        workload_class="coding-arc",
        cli_profile="codex",
        provider="openai",
        model="gpt-5",
        query_summary="codex memory substrate recovery workflow",
        scope=_scope(),
        token_budget=token_budget,
        allowed_kinds=(
            MemoryRecordKind.PREFERENCE,
            MemoryRecordKind.CONVENTION,
            MemoryRecordKind.DECISION,
            MemoryRecordKind.FAILURE_LEARNING,
            MemoryRecordKind.RESEARCH,
            MemoryRecordKind.PROCEDURAL_SNAPSHOT,
        ),
    )


def _retriever(
    *,
    store: CanonicalMemoryStore,
    index_store: DerivedRetrievalIndexStore,
    policy: MemoryPolicyDocument | None = None,
) -> MemoryRetriever:
    return MemoryRetriever(
        store=store,
        index_store=index_store,
        policy_resolver=MemoryPolicyResolver(policy or _enabled_policy()),
        policy_ref="policy:u-mem-11-test",
    )


def test_retrieval_ranking_packet_and_event_are_stable(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    pinned = _record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Continue Codex memory substrate work from the current recovery point.",
        tags=("codex", "memory", "workflow"),
        pinned=True,
        created_at=_NOW - timedelta(days=1),
    )
    convention = _record(
        kind=MemoryRecordKind.CONVENTION,
        statement="Use isolated worktrees before editing memory substrate code.",
        tags=("codex", "memory", "workflow"),
        created_at=_NOW,
    )
    unrelated = _record(
        kind=MemoryRecordKind.RESEARCH,
        statement="A detached research fact about unrelated infrastructure.",
        tags=("unrelated",),
        created_at=_NOW + timedelta(seconds=1),
    )
    for record in (pinned, convention, unrelated):
        store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)

    result = _retriever(store=store, index_store=index_store).retrieve(
        _request(),
        timestamp=_NOW,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        access_mode=MemoryPacketAccessMode.PROMPT_EXTENSION_PACKET,
    )
    repeated = _retriever(store=store, index_store=index_store).retrieve(
        _request(),
        timestamp=_NOW,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        access_mode=MemoryPacketAccessMode.PROMPT_EXTENSION_PACKET,
    )

    assert result.selected_refs == (pinned.envelope.memory_id, convention.envelope.memory_id)
    assert repeated.selected_refs == result.selected_refs
    assert repeated.request_hash == result.request_hash
    assert repeated.packet_hash == result.packet_hash
    assert result.packet.packet_hash == result.packet_hash
    assert result.packet.selected_refs == result.selected_refs
    assert result.packet.policy_ref == "policy:u-mem-11-test"
    assert [section.section_id for section in result.packet.sections] == [
        "active_operator_project_preferences",
        "current_project_conventions",
    ]
    assert all(section.memory_ref in result.selected_refs for section in result.packet.sections)
    assert all(str(section.memory_ref) in section.text for section in result.packet.sections)

    ledger = store.read_memory_operations()
    assert len(ledger) == 1
    assert ledger[0].operation_kind is MemoryOperationKind.RETRIEVE
    assert ledger[0].operation_projection is MemoryOperationProjection.RETRIEVAL_EVENTS
    assert ledger[0].memory_refs == result.selected_refs
    assert ledger[0].policy_ref == "policy:u-mem-11-test"


def test_excluded_considered_refs_carry_deterministic_reasons(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    allowed = _record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex memory recovery should resume from verified loop state.",
        tags=("codex", "memory", "workflow"),
    )
    policy_denied = _record(
        kind=MemoryRecordKind.CONVENTION,
        statement="Codex conventions are denied by policy in this fixture.",
        tags=("codex", "memory", "workflow"),
    )
    redacted = _record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex redacted memory must not enter the packet.",
        tags=("codex", "memory"),
        redaction_state=RedactionState.REDACTED,
    )
    expired = _record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex expired memory must not enter the packet.",
        tags=("codex", "memory"),
        status="expired",
    )
    for record in (allowed, policy_denied, redacted, expired):
        store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)

    result = _retriever(
        store=store,
        index_store=index_store,
        policy=_enabled_policy(eligible_record_kinds=(MemoryRecordKind.PREFERENCE,)),
    ).retrieve(
        _request(),
        timestamp=_NOW,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        access_mode=MemoryPacketAccessMode.PROMPT_EXTENSION_PACKET,
    )

    reasons = {excluded.memory_ref: excluded.reason for excluded in result.excluded_refs}
    assert result.selected_refs == (allowed.envelope.memory_id,)
    assert reasons[policy_denied.envelope.memory_id] is RetrievalExclusionReason.POLICY_DENIED
    assert reasons[redacted.envelope.memory_id] is RetrievalExclusionReason.REDACTED
    assert reasons[expired.envelope.memory_id] is RetrievalExclusionReason.EXPIRED


def test_packet_sections_obey_stable_order_and_token_budget(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    records = (
        _record(
            kind=MemoryRecordKind.PROCEDURAL_SNAPSHOT,
            statement="Codex procedure for memory retrieval packets.",
            tags=("codex", "memory", "workflow"),
            created_at=_NOW + timedelta(seconds=5),
        ),
        _record(
            kind=MemoryRecordKind.FAILURE_LEARNING,
            statement="Codex failure learning for stale loop evidence.",
            tags=("codex", "memory", "workflow", "failure"),
            created_at=_NOW + timedelta(seconds=4),
        ),
        _record(
            kind=MemoryRecordKind.DECISION,
            statement="Codex decision to bind packet hashes to selected refs.",
            tags=("codex", "memory", "workflow"),
            created_at=_NOW + timedelta(seconds=3),
        ),
        _record(
            kind=MemoryRecordKind.CONVENTION,
            statement="Codex convention for stable memory section order.",
            tags=("codex", "memory", "workflow"),
            created_at=_NOW + timedelta(seconds=2),
        ),
        _record(
            kind=MemoryRecordKind.RESEARCH,
            statement="Codex research fact for packet budget accounting.",
            tags=("codex", "memory", "workflow"),
            created_at=_NOW + timedelta(seconds=1),
        ),
        _record(
            kind=MemoryRecordKind.PREFERENCE,
            statement="Codex preference records must be first in packets.",
            tags=("codex", "memory", "workflow"),
            created_at=_NOW,
        ),
    )
    for record in records:
        store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)

    result = _retriever(store=store, index_store=index_store).retrieve(
        _request(token_budget=42),
        timestamp=_NOW,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        access_mode=MemoryPacketAccessMode.PROMPT_EXTENSION_PACKET,
    )

    assert sum(section.token_estimate for section in result.packet.sections) <= 42
    assert [section.section_id for section in result.packet.sections] == [
        "active_operator_project_preferences",
        "current_project_conventions",
        "relevant_prior_decisions",
        "failure_learnings_and_hazards",
    ]
    assert {excluded.reason for excluded in result.excluded_refs} == {
        RetrievalExclusionReason.TOKEN_BUDGET_EXCEEDED
    }
