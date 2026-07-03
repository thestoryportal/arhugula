"""Tests for U-MEM-08 - promotion candidate extraction."""

from __future__ import annotations

from datetime import UTC, datetime

from harness_is.memory_policy import (
    MemoryPolicyDocument,
    MemoryPolicyResolver,
    PromotionDecision,
    ReviewMode,
)
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
from harness_is.memory_store import MemoryStoreRecord
from harness_runtime.memory_promotion import (
    PreferenceCandidateSource,
    PromotionCandidateConfidence,
    PromotionCandidateExtractor,
    PromotionCandidateKind,
    PromotionRiskFlag,
)

_NOW = datetime(2026, 7, 1, 16, 0, 0, tzinfo=UTC)


def _scope(
    *,
    project: str | None = "arhugula-v2",
    workflow: str | None = "memory-substrate",
    visibility: MemoryVisibility = MemoryVisibility.WORKFLOW,
) -> MemoryScope:
    return MemoryScope(project=project, workflow=workflow, visibility=visibility)


def _record(
    *,
    kind: MemoryRecordKind,
    content: dict[str, object],
    source_ref: SourceRef,
    scope: MemoryScope | None = None,
) -> MemoryStoreRecord:
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(MemoryTier.EPISODIC, kind, content_hash),
            schema_version="test-episodic/v1",
            tier=MemoryTier.EPISODIC,
            kind=kind,
            created_at=_NOW,
            updated_at=None,
            source_refs=(source_ref,),
            scope=scope or _scope(),
            content_hash=content_hash,
        ),
        content=content,
    )


def _candidate(
    *,
    kind: str,
    statement: str,
    confidence: str = "high",
    suggested_scope: MemoryScope | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "proposed_kind": kind,
        "statement": statement,
        "confidence": confidence,
        "suggested_scope": (suggested_scope or _scope()).model_dump(mode="json"),
    }
    payload.update(extra)
    return payload


def test_extracts_source_linked_candidates_for_all_supported_kinds() -> None:
    record = _record(
        kind=MemoryRecordKind.EPISODIC_TURN,
        source_ref=SourceRef(ref_type=SourceRefType.TURN, ref="turn-1"),
        content={
            "event_type": "turn_completion",
            "run_id": "run-1",
            "turn_id": "turn-1",
            "summary_source": "operator",
            "promotion_candidates": [
                _candidate(kind="fact", statement="The repo uses rtk shell wrappers."),
                _candidate(kind="decision", statement="Use provider-free gates first."),
                _candidate(kind="convention", statement="Record loop gates in order."),
                _candidate(kind="preference", statement="The operator prefers CLI flow."),
                _candidate(
                    kind="failure_learning",
                    statement="Sandboxed localhost tests require escalation.",
                ),
                _candidate(kind="research", statement="C-MEM-10 has no overlay files yet."),
                _candidate(
                    kind="procedural_update",
                    statement="Refresh the roadmap after the implementation PR merges.",
                ),
            ],
        },
    )

    candidates = PromotionCandidateExtractor().extract_from_records((record,))

    assert {candidate.proposed_kind for candidate in candidates} == {
        PromotionCandidateKind.FACT,
        PromotionCandidateKind.DECISION,
        PromotionCandidateKind.CONVENTION,
        PromotionCandidateKind.PREFERENCE,
        PromotionCandidateKind.FAILURE_LEARNING,
        PromotionCandidateKind.RESEARCH,
        PromotionCandidateKind.PROCEDURAL_UPDATE,
    }
    assert all(candidate.candidate_id.startswith("promocand:") for candidate in candidates)
    assert all(candidate.source_refs == record.envelope.source_refs for candidate in candidates)
    assert all(
        candidate.source_memory_refs == (record.envelope.memory_id,) for candidate in candidates
    )


def test_preference_candidates_distinguish_operator_direct_from_inferred() -> None:
    operator_record = _record(
        kind=MemoryRecordKind.EPISODIC_TURN,
        source_ref=SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:turn-2"),
        content={
            "event_type": "turn_completion",
            "summary_source": "operator",
            "promotion_candidates": [
                _candidate(kind="preference", statement="Always use CLI-first workflows."),
            ],
        },
    )
    inferred_record = _record(
        kind=MemoryRecordKind.EPISODIC_TURN,
        source_ref=SourceRef(ref_type=SourceRefType.TURN, ref="turn-3"),
        content={
            "event_type": "turn_completion",
            "summary_source": "model_generated",
            "promotion_candidates": [
                _candidate(kind="preference", statement="The operator may prefer short updates."),
            ],
        },
    )

    candidates = PromotionCandidateExtractor().extract_from_records(
        (operator_record, inferred_record)
    )

    by_source: dict[MemoryID, PreferenceCandidateSource] = {}
    for candidate in candidates:
        assert candidate.preference_source is not None
        by_source[candidate.source_memory_refs[0]] = candidate.preference_source
    assert (
        by_source[operator_record.envelope.memory_id] is PreferenceCandidateSource.OPERATOR_DIRECT
    )
    assert by_source[inferred_record.envelope.memory_id] is PreferenceCandidateSource.INFERRED


def test_sensitive_low_confidence_cross_scope_behavior_changes_are_flagged() -> None:
    source_scope = _scope(visibility=MemoryVisibility.WORKFLOW)
    broader_scope = _scope(workflow=None, visibility=MemoryVisibility.PROJECT)
    record = _record(
        kind=MemoryRecordKind.EPISODIC_TURN,
        source_ref=SourceRef(ref_type=SourceRefType.TURN, ref="turn-risk"),
        scope=source_scope,
        content={
            "event_type": "turn_completion",
            "summary_source": "model_generated",
            "promotion_candidates": [
                _candidate(
                    kind="procedural_update",
                    statement="Change future review behavior for all project work.",
                    confidence="low",
                    suggested_scope=broader_scope,
                    sensitive=True,
                    behavior_changing=True,
                ),
            ],
        },
    )

    candidate = PromotionCandidateExtractor().extract_from_records((record,))[0]

    assert candidate.confidence is PromotionCandidateConfidence.LOW
    assert set(candidate.risk_flags) == {
        PromotionRiskFlag.SENSITIVE,
        PromotionRiskFlag.LOW_CONFIDENCE,
        PromotionRiskFlag.CROSS_SCOPE,
        PromotionRiskFlag.BEHAVIOR_CHANGING,
    }


def test_low_confidence_candidates_do_not_auto_promote_when_review_required() -> None:
    record = _record(
        kind=MemoryRecordKind.EPISODIC_TURN,
        source_ref=SourceRef(ref_type=SourceRefType.TURN, ref="turn-low-confidence"),
        content={
            "event_type": "turn_completion",
            "summary_source": "operator",
            "promotion_candidates": [
                _candidate(
                    kind="fact",
                    statement="A low confidence fact still needs human review.",
                    confidence="low",
                ),
            ],
        },
    )
    policy = MemoryPolicyResolver(
        MemoryPolicyDocument(
            policy_id="policy:operator-review",
            enabled=True,
            promotion_decision=PromotionDecision.PROMOTE_SEMANTIC,
            review_mode=ReviewMode.OPERATOR_REQUIRED,
        )
    )

    candidate = PromotionCandidateExtractor(policy_resolver=policy).extract_from_records((record,))[
        0
    ]

    assert candidate.review_required is True
    assert candidate.auto_promote_allowed is False
