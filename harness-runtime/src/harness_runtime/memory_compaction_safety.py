"""Runtime compaction safety hook - U-MEM-20.

Implements the C-MEM-10 compaction disposition invariant while preserving
C-MEM-06 preference candidates for the existing promotion pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from harness_is.memory_operation_ledger import (
    MemoryOperationEngineClass,
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
    MemoryOperationWriteResult,
)
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_store import MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, Identifier
from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness_runtime.memory_promotion import PromotionCandidate, PromotionCandidateExtractor


class CompactionCandidateDisposition(StrEnum):
    """Allowed U-MEM-20 disposition for a compaction candidate."""

    DISCARD = "discard"
    KEEP_EPISODIC = "keep_episodic"
    PROMOTE = "promote"
    QUEUE = "queue"


class CompactionDispositionRequiredError(ValueError):
    """Raised when compaction lacks exactly one disposition per candidate."""


class CompactionDispositionWriteError(RuntimeError):
    """Raised when the durable compaction disposition cannot be written."""


class CompactionCandidateDispositionRecord(BaseModel):
    """Auditable disposition for one extracted compaction candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    disposition: CompactionCandidateDisposition
    rationale: str
    target_memory_ref: MemoryID | None = None

    @model_validator(mode="after")
    def _rationale_is_not_empty(self) -> CompactionCandidateDispositionRecord:
        if not self.candidate_id.strip():
            raise ValueError("compaction candidate_id cannot be empty")
        if not self.rationale.strip():
            raise ValueError("compaction disposition rationale cannot be empty")
        if (
            self.disposition is CompactionCandidateDisposition.PROMOTE
            and self.target_memory_ref is None
        ):
            raise ValueError("promoted compaction candidates require target_memory_ref")
        return self


class CompactionDecisionResult(BaseModel):
    """Result returned after compaction disposition is durably recorded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: MemoryStoreRecord
    memory_id: MemoryID
    operation_result: MemoryOperationWriteResult
    candidate_count: int = Field(ge=0)


class CompactionDecisionStore(Protocol):
    """Store surface consumed by ``CompactionSafetyHook``."""

    def write_record(self, record: MemoryStoreRecord) -> object: ...

    def append_memory_operation(
        self,
        payload: MemoryOperationPayload,
    ) -> MemoryOperationWriteResult: ...


class CompactionSafetyHook:
    """Extract candidates and persist required dispositions before compaction."""

    def __init__(
        self,
        *,
        store: CompactionDecisionStore,
        actor: Actor,
        run_id: str,
        step_id: str | None = None,
        policy_ref: str | None = None,
        procedural_snapshot_ref: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cli_profile: str | None = None,
        engine_class: MemoryOperationEngineClass | None = None,
        extractor: PromotionCandidateExtractor | None = None,
    ) -> None:
        self._store = store
        self._actor = actor
        self._run_id = run_id
        self._step_id = step_id
        self._policy_ref = policy_ref
        self._procedural_snapshot_ref = procedural_snapshot_ref
        self._provider = provider
        self._model = model
        self._cli_profile = cli_profile
        self._engine_class = engine_class
        self._extractor = extractor or PromotionCandidateExtractor()

    def extract_candidates(
        self,
        source_records: Sequence[MemoryStoreRecord],
    ) -> tuple[PromotionCandidate, ...]:
        """Extract source-linked promotion candidates before context loss."""

        return tuple(self._extractor.extract_from_records(source_records))

    def complete_compaction(
        self,
        *,
        compaction_id: str,
        candidates: Sequence[PromotionCandidate],
        dispositions: Sequence[CompactionCandidateDispositionRecord],
        timestamp: datetime,
        summary: str,
        scope: MemoryScope | None = None,
    ) -> CompactionDecisionResult:
        """Persist the auditable disposition set before compaction completes."""

        if not compaction_id:
            raise ValueError("compaction_id cannot be empty")
        if not summary.strip():
            raise ValueError("compaction summary cannot be empty")
        ordered_dispositions = _validate_dispositions(candidates, dispositions)
        event_scope = scope or _scope_from_candidates(candidates)
        record = _compaction_event_record(
            compaction_id=compaction_id,
            run_id=self._run_id,
            step_id=self._step_id,
            summary=summary,
            candidates=candidates,
            dispositions=ordered_dispositions,
            timestamp=timestamp,
            scope=event_scope,
        )
        try:
            self._store.write_record(record)
            operation_result = self._store.append_memory_operation(
                self._operation_payload(
                    compaction_id=compaction_id,
                    record=record,
                    timestamp=timestamp,
                )
            )
        except Exception as exc:
            raise CompactionDispositionWriteError(str(exc)) from exc
        return CompactionDecisionResult(
            record=record,
            memory_id=record.envelope.memory_id,
            operation_result=operation_result,
            candidate_count=len(candidates),
        )

    def _operation_payload(
        self,
        *,
        compaction_id: str,
        record: MemoryStoreRecord,
        timestamp: datetime,
    ) -> MemoryOperationPayload:
        action_id = Identifier(f"compaction:{compaction_id}:{record.envelope.memory_id}")
        return MemoryOperationPayload(
            action_id=action_id,
            idempotency_key=Identifier(f"idempotent:{action_id}"),
            actor=self._actor,
            timestamp=timestamp,
            operation_kind=MemoryOperationKind.COMPACTION_DECISION,
            operation_projection=MemoryOperationProjection.NONE,
            run_id=self._run_id,
            step_id=self._step_id,
            provider=self._provider,
            model=self._model,
            cli_profile=self._cli_profile,
            engine_class=self._engine_class,
            memory_refs=(record.envelope.memory_id,),
            policy_ref=self._policy_ref,
            procedural_snapshot_ref=self._procedural_snapshot_ref,
        )


def _validate_dispositions(
    candidates: Sequence[PromotionCandidate],
    dispositions: Sequence[CompactionCandidateDispositionRecord],
) -> tuple[CompactionCandidateDispositionRecord, ...]:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    disposition_by_id: dict[str, CompactionCandidateDispositionRecord] = {}
    duplicate_ids: set[str] = set()
    for disposition in dispositions:
        if disposition.candidate_id in disposition_by_id:
            duplicate_ids.add(disposition.candidate_id)
        disposition_by_id[disposition.candidate_id] = disposition
    if duplicate_ids:
        raise CompactionDispositionRequiredError(
            f"duplicate dispositions for candidates: {sorted(duplicate_ids)!r}"
        )
    unknown = sorted(set(disposition_by_id) - set(candidate_ids))
    if unknown:
        raise CompactionDispositionRequiredError(
            f"unknown dispositions for candidates: {unknown!r}"
        )
    missing = sorted(set(candidate_ids) - set(disposition_by_id))
    if missing:
        raise CompactionDispositionRequiredError(
            f"missing dispositions for candidates: {missing!r}"
        )
    return tuple(disposition_by_id[candidate_id] for candidate_id in candidate_ids)


def _scope_from_candidates(candidates: Sequence[PromotionCandidate]) -> MemoryScope:
    if not candidates:
        raise ValueError("compaction with no candidates requires scope")
    return candidates[0].suggested_scope


def _compaction_event_record(
    *,
    compaction_id: str,
    run_id: str,
    step_id: str | None,
    summary: str,
    candidates: Sequence[PromotionCandidate],
    dispositions: Sequence[CompactionCandidateDispositionRecord],
    timestamp: datetime,
    scope: MemoryScope,
) -> MemoryStoreRecord:
    content: dict[str, object] = {
        "event_type": "compaction_decision",
        "compaction_id": compaction_id,
        "run_id": run_id,
        "step_id": step_id,
        "summary": summary,
        "candidate_count": len(candidates),
        "candidate_dispositions": [
            _disposition_content(candidate, disposition)
            for candidate, disposition in zip(candidates, dispositions, strict=True)
        ],
    }
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(
                MemoryTier.EPISODIC,
                MemoryRecordKind.COMPACTION_EVENT,
                content_hash,
            ),
            schema_version="compaction-decision/v1",
            tier=MemoryTier.EPISODIC,
            kind=MemoryRecordKind.COMPACTION_EVENT,
            created_at=timestamp,
            updated_at=None,
            source_refs=_source_refs(compaction_id, candidates),
            scope=scope,
            content_hash=content_hash,
        ),
        content=content,
    )


def _disposition_content(
    candidate: PromotionCandidate,
    disposition: CompactionCandidateDispositionRecord,
) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "disposition": disposition.disposition.value,
        "rationale": disposition.rationale,
        "target_memory_ref": (
            str(disposition.target_memory_ref)
            if disposition.target_memory_ref is not None
            else None
        ),
        "proposed_kind": candidate.proposed_kind.value,
        "statement": candidate.statement,
        "confidence": candidate.confidence.value,
        "suggested_scope": candidate.suggested_scope.model_dump(mode="json"),
        "source_memory_refs": [str(memory_id) for memory_id in candidate.source_memory_refs],
        "source_refs": [ref.model_dump(mode="json") for ref in candidate.source_refs],
    }


def _source_refs(
    compaction_id: str,
    candidates: Sequence[PromotionCandidate],
) -> tuple[SourceRef, ...]:
    refs: list[SourceRef] = [SourceRef(ref_type=SourceRefType.COMPACTION, ref=compaction_id)]
    seen: set[tuple[str, str, bytes | None]] = {
        (SourceRefType.COMPACTION.value, compaction_id, None)
    }
    for candidate in candidates:
        for ref in candidate.source_refs:
            key = (ref.ref_type.value, ref.ref, ref.content_hash)
            if key not in seen:
                refs.append(ref)
                seen.add(key)
    return tuple(refs)


__all__ = [
    "CompactionCandidateDisposition",
    "CompactionCandidateDispositionRecord",
    "CompactionDecisionResult",
    "CompactionDecisionStore",
    "CompactionDispositionRequiredError",
    "CompactionDispositionWriteError",
    "CompactionSafetyHook",
]
