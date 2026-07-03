"""Tests for U-MEM-20 - compaction safety hook."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import (
    MemoryOperationKind,
    MemoryOperationProjection,
    MemoryOperationWriteResult,
)
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    MemoryVisibility,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime import (
    CompactionCandidateDisposition,
    CompactionCandidateDispositionRecord,
    CompactionDispositionRequiredError,
    CompactionDispositionWriteError,
    CompactionSafetyHook,
)
from harness_runtime.memory_promotion import (
    PromotionCandidate,
    PromotionCandidateConfidence,
    PromotionCandidateKind,
)

_NOW = datetime(2026, 7, 2, 22, 0, 0, tzinfo=UTC)
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="codex")


def _scope() -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        visibility=MemoryVisibility.WORKFLOW,
    )


def _store(tmp_path: Path) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _source_record() -> MemoryStoreRecord:
    content: dict[str, object] = {
        "event_type": "turn_completion",
        "run_id": "run-u-mem-20",
        "turn_id": "turn-1",
        "summary_source": "operator",
        "promotion_candidates": [
            {
                "proposed_kind": "fact",
                "statement": "Compaction found a durable fact.",
                "confidence": "high",
                "suggested_scope": _scope().model_dump(mode="json"),
            }
        ],
    }
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(
                MemoryTier.EPISODIC, MemoryRecordKind.EPISODIC_TURN, content_hash
            ),
            schema_version="test-episodic/v1",
            tier=MemoryTier.EPISODIC,
            kind=MemoryRecordKind.EPISODIC_TURN,
            created_at=_NOW,
            updated_at=None,
            source_refs=(SourceRef(ref_type=SourceRefType.TURN, ref="turn-1"),),
            scope=_scope(),
            content_hash=content_hash,
        ),
        content=content,
    )


def _candidate(candidate_id: str = "candidate:fact") -> PromotionCandidate:
    return PromotionCandidate(
        candidate_id=candidate_id,
        source_refs=(SourceRef(ref_type=SourceRefType.TURN, ref="turn-1"),),
        source_memory_refs=(MemoryID("mem:episodic:episodic_turn:" + "1" * 64),),
        proposed_kind=PromotionCandidateKind.FACT,
        statement="Compaction found a durable fact.",
        confidence=PromotionCandidateConfidence.HIGH,
        suggested_scope=_scope(),
        risk_flags=(),
        preference_source=None,
        policy_decision="promote_semantic",
        review_mode="automatic",
        review_required=False,
        auto_promote_allowed=True,
    )


def _hook(store: object) -> CompactionSafetyHook:
    return CompactionSafetyHook(
        store=store,
        actor=_ACTOR,
        run_id="run-u-mem-20",
        step_id="step-compaction",
        policy_ref="policy:memory-test",
        cli_profile="codex",
    )


def test_extracts_compaction_candidates_before_context_loss(tmp_path: Path) -> None:
    hook = _hook(_store(tmp_path))

    [candidate] = hook.extract_candidates((_source_record(),))

    assert candidate.candidate_id.startswith("promocand:")
    assert candidate.source_refs == (SourceRef(ref_type=SourceRefType.TURN, ref="turn-1"),)
    assert candidate.source_memory_refs == (_source_record().envelope.memory_id,)


def test_compaction_cannot_complete_without_disposition_for_every_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    hook = _hook(store)
    first = _candidate("candidate:first")
    second = _candidate("candidate:second")

    with pytest.raises(CompactionDispositionRequiredError, match="missing dispositions"):
        hook.complete_compaction(
            compaction_id="compaction:missing",
            candidates=(first, second),
            dispositions=(
                CompactionCandidateDispositionRecord(
                    candidate_id=first.candidate_id,
                    disposition=CompactionCandidateDisposition.DISCARD,
                    rationale="not load-bearing",
                ),
            ),
            timestamp=_NOW,
            summary="drop non-load-bearing summary",
        )

    assert store.read_memory_operations() == []


def test_complete_compaction_writes_auditable_event_and_durable_decision(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    hook = _hook(store)
    candidates = (
        _candidate("candidate:discard"),
        _candidate("candidate:keep"),
        _candidate("candidate:promote"),
        _candidate("candidate:queue"),
    )

    result = hook.complete_compaction(
        compaction_id="compaction:all-dispositions",
        candidates=candidates,
        dispositions=(
            CompactionCandidateDispositionRecord(
                candidate_id="candidate:discard",
                disposition=CompactionCandidateDisposition.DISCARD,
                rationale="not load-bearing",
            ),
            CompactionCandidateDispositionRecord(
                candidate_id="candidate:keep",
                disposition=CompactionCandidateDisposition.KEEP_EPISODIC,
                rationale="episodic evidence remains useful",
            ),
            CompactionCandidateDispositionRecord(
                candidate_id="candidate:promote",
                disposition=CompactionCandidateDisposition.PROMOTE,
                rationale="semantic fact should survive compaction",
                target_memory_ref=MemoryID("mem:semantic:semantic_fact:" + "2" * 64),
            ),
            CompactionCandidateDispositionRecord(
                candidate_id="candidate:queue",
                disposition=CompactionCandidateDisposition.QUEUE,
                rationale="requires operator review",
            ),
        ),
        timestamp=_NOW,
        summary="compacted four candidates",
    )

    [operation] = store.read_memory_operations()
    assert operation.operation_kind is MemoryOperationKind.COMPACTION_DECISION
    assert operation.operation_projection is MemoryOperationProjection.NONE
    assert operation.memory_refs == (result.memory_id,)
    assert operation.policy_ref == "policy:memory-test"

    event = store.read_record(
        result.memory_id,
        MemoryRecordKind.COMPACTION_EVENT,
        run_id="run-u-mem-20",
    )
    assert event.content["compaction_id"] == "compaction:all-dispositions"
    assert event.content["summary"] == "compacted four candidates"
    disposition_rows = cast("list[dict[str, object]]", event.content["candidate_dispositions"])
    assert [item["disposition"] for item in disposition_rows] == [
        "discard",
        "keep_episodic",
        "promote",
        "queue",
    ]
    assert disposition_rows[0]["suggested_scope"] == _scope().model_dump(mode="json")


def test_duplicate_or_unknown_dispositions_fail_before_any_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hook = _hook(store)
    candidate = _candidate("candidate:one")

    with pytest.raises(CompactionDispositionRequiredError, match="duplicate dispositions"):
        hook.complete_compaction(
            compaction_id="compaction:duplicate",
            candidates=(candidate,),
            dispositions=(
                CompactionCandidateDispositionRecord(
                    candidate_id="candidate:one",
                    disposition=CompactionCandidateDisposition.DISCARD,
                    rationale="first",
                ),
                CompactionCandidateDispositionRecord(
                    candidate_id="candidate:one",
                    disposition=CompactionCandidateDisposition.KEEP_EPISODIC,
                    rationale="second",
                ),
            ),
            timestamp=_NOW,
            summary="duplicate",
        )

    with pytest.raises(CompactionDispositionRequiredError, match="unknown dispositions"):
        hook.complete_compaction(
            compaction_id="compaction:unknown",
            candidates=(candidate,),
            dispositions=(
                CompactionCandidateDispositionRecord(
                    candidate_id="candidate:unknown",
                    disposition=CompactionCandidateDisposition.KEEP_EPISODIC,
                    rationale="not part of this compaction",
                ),
            ),
            timestamp=_NOW,
            summary="unknown",
        )

    assert store.read_memory_operations() == []


def test_promoted_disposition_requires_target_memory_ref() -> None:
    with pytest.raises(ValueError, match="target_memory_ref"):
        CompactionCandidateDispositionRecord(
            candidate_id="candidate:promote",
            disposition=CompactionCandidateDisposition.PROMOTE,
            rationale="semantic fact should survive compaction",
        )


class _FailingDecisionStore:
    def write_record(self, record: MemoryStoreRecord) -> object:
        return record

    def append_memory_operation(self, payload: object) -> MemoryOperationWriteResult:
        _ = payload
        raise RuntimeError("ledger unavailable")


def test_compaction_fails_closed_when_durable_disposition_write_fails() -> None:
    hook = _hook(_FailingDecisionStore())

    with pytest.raises(CompactionDispositionWriteError, match="ledger unavailable"):
        hook.complete_compaction(
            compaction_id="compaction:write-failure",
            candidates=(_candidate(),),
            dispositions=(
                CompactionCandidateDispositionRecord(
                    candidate_id="candidate:fact",
                    disposition=CompactionCandidateDisposition.QUEUE,
                    rationale="requires review",
                ),
            ),
            timestamp=_NOW,
            summary="must fail closed",
        )
