"""Promotion candidate extraction and review decisions.

This module implements the C-MEM-10 extraction boundary and the U-MEM-09
promotion-decision boundary. It validates structured candidate hints from
episodic/operator source records, links each candidate back to source evidence,
annotates risk, resolves whether the current memory policy permits automatic
promotion, and persists review decisions through the canonical memory store and
durable memory-operation ledger.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self, cast

from harness_is.memory_observability import (
    MemoryTelemetryOperationName,
    memory_telemetry_span,
)
from harness_is.memory_operation_ledger import (
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
    MemoryOperationWriteResult,
)
from harness_is.memory_policy import (
    MemoryPolicyResolver,
    MemoryPromotionResolution,
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
from harness_is.state_ledger_entry_schema import Actor, Identifier
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PromotionCandidateKind(StrEnum):
    """Candidate kinds declared by C-MEM-10."""

    FACT = "fact"
    DECISION = "decision"
    CONVENTION = "convention"
    FAILURE_LEARNING = "failure_learning"
    RESEARCH = "research"
    PREFERENCE = "preference"
    PROCEDURAL_UPDATE = "procedural_update"


class PromotionCandidateConfidence(StrEnum):
    """Confidence values declared by C-MEM-10."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PromotionRiskFlag(StrEnum):
    """Risk flags required by U-MEM-08."""

    SENSITIVE = "sensitive"
    LOW_CONFIDENCE = "low_confidence"
    CROSS_SCOPE = "cross_scope"
    BEHAVIOR_CHANGING = "behavior_changing"


class PreferenceCandidateSource(StrEnum):
    """Preference provenance required to avoid model-proposed preference drift."""

    OPERATOR_DIRECT = "operator_direct"
    INFERRED = "inferred"


class SemanticRecordStatus(StrEnum):
    """Semantic/procedural promotion statuses declared by C-MEM-05."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    DENIED = "denied"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class SemanticInjectionPolicy(StrEnum):
    """Injection policy values declared by C-MEM-05."""

    NEVER = "never"
    RETRIEVAL_ONLY = "retrieval_only"
    PROMPT_PACKET_ALLOWED = "prompt_packet_allowed"
    TOOL_ALLOWED = "tool_allowed"
    NATIVE_ALLOWED = "native_allowed"


class PreferenceSubject(StrEnum):
    """Preference subjects declared by C-MEM-06."""

    OPERATOR = "operator"
    PROJECT = "project"
    WORKFLOW = "workflow"
    CODE_STYLE = "code_style"
    TOOL_USE = "tool_use"
    PROVIDER = "provider"
    REVIEW = "review"
    OTHER = "other"


class PreferenceStrength(StrEnum):
    """Preference strengths declared by C-MEM-06."""

    WEAK = "weak"
    NORMAL = "normal"
    STRONG = "strong"
    MANDATORY = "mandatory"


class PreferenceSourceAuthority(StrEnum):
    """Preference source-authority values declared by C-MEM-06."""

    OPERATOR_DIRECT = "operator_direct"
    INFERRED_FROM_REPETITION = "inferred_from_repetition"
    IMPORTED = "imported"
    POLICY = "policy"


class PromotionReviewRequiredError(ValueError):
    """Raised when a caller tries to activate a candidate that still needs review."""


class PreferencePromotionValidationError(ValueError):
    """Raised when a preference candidate lacks C-MEM-06 required metadata."""


def _empty_source_refs() -> tuple[SourceRef, ...]:
    return ()


def _empty_risk_flags() -> tuple[PromotionRiskFlag, ...]:
    return ()


def _empty_memory_refs() -> tuple[MemoryID, ...]:
    return ()


class PromotionCandidateHint(BaseModel):
    """Structured candidate material carried by an episodic/operator source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposed_kind: PromotionCandidateKind
    statement: str
    confidence: PromotionCandidateConfidence
    suggested_scope: MemoryScope
    source_refs: tuple[SourceRef, ...] = Field(default_factory=_empty_source_refs)
    risk_flags: tuple[PromotionRiskFlag, ...] = Field(default_factory=_empty_risk_flags)
    preference_source: PreferenceCandidateSource | None = None
    sensitive: bool = False
    behavior_changing: bool = False

    @model_validator(mode="after")
    def _validate_preference_source(self) -> Self:
        if (
            self.preference_source is not None
            and self.proposed_kind is not PromotionCandidateKind.PREFERENCE
        ):
            raise ValueError("preference_source is only valid for preference candidates")
        if not self.statement.strip():
            raise ValueError("promotion candidate statement cannot be empty")
        return self


class PromotionCandidate(BaseModel):
    """C-MEM-10 promotion candidate extracted from source evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    source_refs: tuple[SourceRef, ...]
    source_memory_refs: tuple[MemoryID, ...] = Field(default_factory=_empty_memory_refs)
    proposed_kind: PromotionCandidateKind
    statement: str
    confidence: PromotionCandidateConfidence
    suggested_scope: MemoryScope
    risk_flags: tuple[PromotionRiskFlag, ...] = Field(default_factory=_empty_risk_flags)
    preference_source: PreferenceCandidateSource | None = None
    policy_decision: PromotionDecision
    review_mode: ReviewMode
    review_required: bool
    auto_promote_allowed: bool

    @model_validator(mode="after")
    def _validate_preference_source(self) -> Self:
        if not self.source_refs:
            raise ValueError("promotion candidates require at least one source_ref")
        if (
            self.preference_source is not None
            and self.proposed_kind is not PromotionCandidateKind.PREFERENCE
        ):
            raise ValueError("preference_source is only valid for preference candidates")
        if (
            self.proposed_kind is PromotionCandidateKind.PREFERENCE
            and self.preference_source is None
        ):
            raise ValueError("preference candidates require preference_source")
        return self


class PreferencePromotionDetails(BaseModel):
    """C-MEM-06 preference-only fields supplied during promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preference_subject: PreferenceSubject
    preference_strength: PreferenceStrength
    source_authority: PreferenceSourceAuthority
    confirmation_required: bool


class PromotionDecisionResult(BaseModel):
    """Result of applying or queueing one promotion decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SemanticRecordStatus
    record: MemoryStoreRecord
    memory_id: MemoryID
    operation_kind: MemoryOperationKind
    operation_result: MemoryOperationWriteResult


class PromotionDecisionStore(Protocol):
    """Store surface consumed by ``PromotionDecisionService``."""

    def write_record(self, record: MemoryStoreRecord) -> object: ...

    def append_memory_operation(
        self,
        payload: MemoryOperationPayload,
    ) -> MemoryOperationWriteResult: ...


class PromotionCandidateExtractor:
    """Extract C-MEM-10 candidates from episodic/operator memory records."""

    def __init__(self, policy_resolver: MemoryPolicyResolver | None = None) -> None:
        self._policy_resolver = policy_resolver or MemoryPolicyResolver()

    def extract_from_records(
        self,
        records: Sequence[MemoryStoreRecord],
    ) -> list[PromotionCandidate]:
        """Extract source-linked promotion candidates from stored source records."""

        resolution = self._policy_resolver.resolve_promotion()
        candidates: list[PromotionCandidate] = []
        for record in records:
            for hint in _hints_from_record(record):
                candidates.append(_candidate_from_hint(record, hint, resolution))
        return candidates


class PromotionDecisionService:
    """Apply C-MEM-10 promotion decisions through the canonical store and ledger."""

    def __init__(
        self,
        *,
        store: PromotionDecisionStore,
        actor: Actor,
        policy_ref: str | None = None,
        procedural_snapshot_ref: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cli_profile: str | None = None,
        tracer_provider: object | None = None,
    ) -> None:
        self._store = store
        self._actor = actor
        self._policy_ref = policy_ref
        self._procedural_snapshot_ref = procedural_snapshot_ref
        self._run_id = run_id
        self._step_id = step_id
        self._provider = provider
        self._model = model
        self._cli_profile = cli_profile
        self._tracer_provider = tracer_provider

    def propose_for_review(
        self,
        candidate: PromotionCandidate,
        *,
        timestamp: datetime,
        injection_policy: SemanticInjectionPolicy,
        preference_details: PreferencePromotionDetails | None = None,
        rationale: str | None = None,
        tags: Sequence[str] = (),
    ) -> PromotionDecisionResult:
        """Persist a proposed semantic/procedural record for operator review."""

        return self._persist_decision(
            candidate,
            status=SemanticRecordStatus.PROPOSED,
            operation_kind=MemoryOperationKind.PROPOSE_PROMOTION,
            timestamp=timestamp,
            injection_policy=injection_policy,
            preference_details=preference_details,
            rationale=rationale,
            tags=tags,
            review_reason=None,
            supersedes=(),
            statement_override=None,
        )

    def approve(
        self,
        candidate: PromotionCandidate,
        *,
        timestamp: datetime,
        injection_policy: SemanticInjectionPolicy | None = None,
        preference_details: PreferencePromotionDetails | None = None,
        operator_approved: bool = False,
        rationale: str | None = None,
        tags: Sequence[str] = (),
        supersedes: Sequence[MemoryID] = (),
    ) -> PromotionDecisionResult:
        """Persist an active record when policy or operator review allows it."""

        if not candidate.auto_promote_allowed and not operator_approved:
            raise PromotionReviewRequiredError(
                "candidate cannot become active until operator review approves it"
            )
        if injection_policy is None:
            if candidate.proposed_kind is PromotionCandidateKind.PREFERENCE:
                raise PreferencePromotionValidationError(
                    "preference promotion requires injection_policy"
                )
            raise ValueError("active promotion requires an injection_policy")
        return self._persist_decision(
            candidate,
            status=SemanticRecordStatus.ACTIVE,
            operation_kind=MemoryOperationKind.PROMOTE,
            timestamp=timestamp,
            injection_policy=injection_policy,
            preference_details=preference_details,
            rationale=rationale,
            tags=tags,
            review_reason=None,
            supersedes=supersedes,
            statement_override=None,
        )

    def deny(
        self,
        candidate: PromotionCandidate,
        *,
        timestamp: datetime,
        reason: str,
        tags: Sequence[str] = (),
    ) -> PromotionDecisionResult:
        """Persist a denied record and append a denial ledger entry."""

        return self._persist_decision(
            candidate,
            status=SemanticRecordStatus.DENIED,
            operation_kind=MemoryOperationKind.DENY_PROMOTION,
            timestamp=timestamp,
            injection_policy=SemanticInjectionPolicy.NEVER,
            preference_details=None,
            rationale=None,
            tags=tags,
            review_reason=reason,
            supersedes=(),
            statement_override=None,
        )

    def edit_and_approve(
        self,
        candidate: PromotionCandidate,
        *,
        statement: str,
        timestamp: datetime,
        injection_policy: SemanticInjectionPolicy,
        preference_details: PreferencePromotionDetails | None = None,
        operator_approved: bool = False,
        rationale: str | None = None,
        tags: Sequence[str] = (),
        supersedes: Sequence[MemoryID] = (),
    ) -> PromotionDecisionResult:
        """Apply an operator-edited statement and persist it as active."""

        if not candidate.auto_promote_allowed and not operator_approved:
            raise PromotionReviewRequiredError(
                "candidate cannot become active until operator review approves it"
            )
        if not statement.strip():
            raise ValueError("edited promotion statement cannot be empty")
        return self._persist_decision(
            candidate,
            status=SemanticRecordStatus.ACTIVE,
            operation_kind=MemoryOperationKind.PROMOTE,
            timestamp=timestamp,
            injection_policy=injection_policy,
            preference_details=preference_details,
            rationale=rationale,
            tags=tags,
            review_reason=None,
            supersedes=supersedes,
            statement_override=statement,
        )

    def _persist_decision(
        self,
        candidate: PromotionCandidate,
        *,
        status: SemanticRecordStatus,
        operation_kind: MemoryOperationKind,
        timestamp: datetime,
        injection_policy: SemanticInjectionPolicy,
        preference_details: PreferencePromotionDetails | None,
        rationale: str | None,
        tags: Sequence[str],
        review_reason: str | None,
        supersedes: Sequence[MemoryID],
        statement_override: str | None,
    ) -> PromotionDecisionResult:
        _validate_preference_promotion(
            candidate,
            status=status,
            injection_policy=injection_policy,
            preference_details=preference_details,
        )
        record = _promotion_record(
            candidate,
            status=status,
            timestamp=timestamp,
            injection_policy=injection_policy,
            preference_details=preference_details,
            rationale=rationale,
            tags=tags,
            review_reason=review_reason,
            supersedes=supersedes,
            statement_override=statement_override,
            policy_ref=self._policy_ref,
        )
        with memory_telemetry_span(
            self._tracer_provider,
            tracer_name="harness.runtime.memory_promotion",
            operation_name=MemoryTelemetryOperationName.PROMOTION,
            operation_kind=operation_kind.value,
            tier=record.envelope.tier.value,
            provider=self._provider,
            model=self._model,
            cli_profile=self._cli_profile,
            policy_decision=status.value,
            record_count=1,
        ):
            self._store.write_record(record)
            operation_result = self._store.append_memory_operation(
                self._operation_payload(
                    candidate,
                    record=record,
                    operation_kind=operation_kind,
                    timestamp=timestamp,
                )
            )
        return PromotionDecisionResult(
            status=status,
            record=record,
            memory_id=record.envelope.memory_id,
            operation_kind=operation_kind,
            operation_result=operation_result,
        )

    def _operation_payload(
        self,
        candidate: PromotionCandidate,
        *,
        record: MemoryStoreRecord,
        operation_kind: MemoryOperationKind,
        timestamp: datetime,
    ) -> MemoryOperationPayload:
        action_id = Identifier(
            f"promotion:{operation_kind.value}:{candidate.candidate_id}:{record.envelope.memory_id}"
        )
        return MemoryOperationPayload(
            action_id=action_id,
            idempotency_key=Identifier(f"idempotent:{action_id}"),
            actor=self._actor,
            timestamp=timestamp,
            operation_kind=operation_kind,
            operation_projection=MemoryOperationProjection.for_operation_kind(operation_kind),
            run_id=self._run_id,
            step_id=self._step_id,
            provider=self._provider,
            model=self._model,
            cli_profile=self._cli_profile,
            engine_class=None,
            memory_refs=(record.envelope.memory_id,),
            policy_ref=self._policy_ref,
            procedural_snapshot_ref=self._procedural_snapshot_ref,
        )


def _hints_from_record(record: MemoryStoreRecord) -> list[PromotionCandidateHint]:
    raw_candidates = record.content.get("promotion_candidates")
    if raw_candidates is None:
        return []
    if isinstance(raw_candidates, str) or not isinstance(raw_candidates, Sequence):
        raise TypeError("promotion_candidates must be a sequence of structured candidates")

    hints: list[PromotionCandidateHint] = []
    for raw_item in cast("Sequence[object]", raw_candidates):
        item: object = raw_item
        if isinstance(item, str):
            item = _json_candidate_hint(item)
        if not isinstance(item, Mapping) and not isinstance(item, PromotionCandidateHint):
            raise TypeError("promotion candidate entries must be mappings or JSON mappings")
        hints.append(PromotionCandidateHint.model_validate(item))
    return hints


def _json_candidate_hint(value: str) -> Mapping[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise TypeError("promotion candidate JSON entries must decode to mappings")
    return cast("Mapping[str, object]", parsed)


def _candidate_from_hint(
    record: MemoryStoreRecord,
    hint: PromotionCandidateHint,
    resolution: MemoryPromotionResolution,
) -> PromotionCandidate:
    source_refs = _merge_source_refs(record.envelope.source_refs, hint.source_refs)
    risk_flags = _risk_flags(
        hint,
        source_scope=record.envelope.scope,
    )
    preference_source = _preference_source(
        hint,
        source_refs=source_refs,
        source_content=record.content,
    )
    review_required = _review_required(hint, resolution)
    auto_promote_allowed = _auto_promote_allowed(
        hint,
        resolution=resolution,
        review_required=review_required,
    )
    return PromotionCandidate(
        candidate_id=_candidate_id(record.envelope.memory_id, hint, preference_source),
        source_refs=source_refs,
        source_memory_refs=(record.envelope.memory_id,),
        proposed_kind=hint.proposed_kind,
        statement=hint.statement,
        confidence=hint.confidence,
        suggested_scope=hint.suggested_scope,
        risk_flags=risk_flags,
        preference_source=preference_source,
        policy_decision=resolution.promotion_decision,
        review_mode=resolution.review_mode,
        review_required=review_required,
        auto_promote_allowed=auto_promote_allowed,
    )


def _merge_source_refs(
    record_refs: Sequence[SourceRef],
    hint_refs: Sequence[SourceRef],
) -> tuple[SourceRef, ...]:
    refs: list[SourceRef] = []
    seen: set[tuple[str, str, bytes | None]] = set()
    for ref in (*record_refs, *hint_refs):
        key = (ref.ref_type.value, ref.ref, ref.content_hash)
        if key not in seen:
            refs.append(ref)
            seen.add(key)
    return tuple(refs)


def _risk_flags(
    hint: PromotionCandidateHint,
    *,
    source_scope: MemoryScope,
) -> tuple[PromotionRiskFlag, ...]:
    flags = set(hint.risk_flags)
    if hint.sensitive:
        flags.add(PromotionRiskFlag.SENSITIVE)
    if hint.confidence is PromotionCandidateConfidence.LOW:
        flags.add(PromotionRiskFlag.LOW_CONFIDENCE)
    if _scope_escapes_source(hint.suggested_scope, source_scope):
        flags.add(PromotionRiskFlag.CROSS_SCOPE)
    if hint.behavior_changing:
        flags.add(PromotionRiskFlag.BEHAVIOR_CHANGING)
    return tuple(sorted(flags, key=lambda flag: flag.value))


_VISIBILITY_RANK = {
    MemoryVisibility.PRIVATE: 0,
    MemoryVisibility.WORKFLOW: 1,
    MemoryVisibility.PROJECT: 2,
    MemoryVisibility.TENANT: 3,
    MemoryVisibility.PUBLIC: 4,
}


def _scope_escapes_source(candidate_scope: MemoryScope, source_scope: MemoryScope) -> bool:
    if _VISIBILITY_RANK[candidate_scope.visibility] > _VISIBILITY_RANK[source_scope.visibility]:
        return True
    for field_name in (
        "project",
        "workflow",
        "workload_class",
        "provider_family",
        "cli_profile",
        "tenant",
    ):
        source_value = getattr(source_scope, field_name)
        candidate_value = getattr(candidate_scope, field_name)
        if source_value is not None and candidate_value != source_value:
            return True
    return False


def _preference_source(
    hint: PromotionCandidateHint,
    *,
    source_refs: Sequence[SourceRef],
    source_content: Mapping[str, object],
) -> PreferenceCandidateSource | None:
    if hint.proposed_kind is not PromotionCandidateKind.PREFERENCE:
        return None
    if hint.preference_source is not None:
        return hint.preference_source
    if any(ref.ref_type is SourceRefType.OPERATOR for ref in source_refs):
        return PreferenceCandidateSource.OPERATOR_DIRECT
    if source_content.get("summary_source") == "operator":
        return PreferenceCandidateSource.OPERATOR_DIRECT
    return PreferenceCandidateSource.INFERRED


def _review_required(
    hint: PromotionCandidateHint,
    resolution: MemoryPromotionResolution,
) -> bool:
    if hint.confidence is PromotionCandidateConfidence.LOW:
        return True
    if resolution.review_mode is ReviewMode.OPERATOR_REQUIRED:
        return True
    return resolution.promotion_decision in {
        PromotionDecision.PROPOSE_SEMANTIC,
        PromotionDecision.PROPOSE_PROCEDURAL,
    }


def _auto_promote_allowed(
    hint: PromotionCandidateHint,
    *,
    resolution: MemoryPromotionResolution,
    review_required: bool,
) -> bool:
    if review_required:
        return False
    if resolution.review_mode is not ReviewMode.AUTOMATIC:
        return False
    if hint.confidence is PromotionCandidateConfidence.LOW:
        return False
    if hint.proposed_kind is PromotionCandidateKind.PROCEDURAL_UPDATE:
        return resolution.promotion_decision is PromotionDecision.PROMOTE_PROCEDURAL
    return resolution.promotion_decision is PromotionDecision.PROMOTE_SEMANTIC


def _candidate_id(
    source_memory_id: MemoryID,
    hint: PromotionCandidateHint,
    preference_source: PreferenceCandidateSource | None,
) -> str:
    payload = {
        "source_memory_id": str(source_memory_id),
        "proposed_kind": hint.proposed_kind.value,
        "statement": unicodedata.normalize("NFC", hint.statement),
        "confidence": hint.confidence.value,
        "suggested_scope": hint.suggested_scope.model_dump(mode="json"),
        "preference_source": preference_source.value if preference_source is not None else None,
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"promocand:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _promotion_record(
    candidate: PromotionCandidate,
    *,
    status: SemanticRecordStatus,
    timestamp: datetime,
    injection_policy: SemanticInjectionPolicy,
    preference_details: PreferencePromotionDetails | None,
    rationale: str | None,
    tags: Sequence[str],
    review_reason: str | None,
    supersedes: Sequence[MemoryID],
    statement_override: str | None,
    policy_ref: str | None,
) -> MemoryStoreRecord:
    kind = _record_kind_for_candidate(candidate)
    tier = _tier_for_record_kind(kind)
    content = _record_content(
        candidate,
        status=status,
        injection_policy=injection_policy,
        preference_details=preference_details,
        rationale=rationale,
        tags=tags,
        review_reason=review_reason,
        statement_override=statement_override,
        policy_ref=policy_ref,
    )
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(tier, kind, content_hash),
            schema_version="promotion-record/v1",
            tier=tier,
            kind=kind,
            created_at=timestamp,
            updated_at=None,
            source_refs=candidate.source_refs,
            scope=candidate.suggested_scope,
            content_hash=content_hash,
            supersedes=tuple(supersedes),
        ),
        content=content,
    )


def _record_kind_for_candidate(candidate: PromotionCandidate) -> MemoryRecordKind:
    if candidate.proposed_kind is PromotionCandidateKind.FACT:
        return MemoryRecordKind.SEMANTIC_FACT
    if candidate.proposed_kind is PromotionCandidateKind.DECISION:
        return MemoryRecordKind.DECISION
    if candidate.proposed_kind is PromotionCandidateKind.CONVENTION:
        return MemoryRecordKind.CONVENTION
    if candidate.proposed_kind is PromotionCandidateKind.FAILURE_LEARNING:
        return MemoryRecordKind.FAILURE_LEARNING
    if candidate.proposed_kind is PromotionCandidateKind.RESEARCH:
        return MemoryRecordKind.RESEARCH
    if candidate.proposed_kind is PromotionCandidateKind.PREFERENCE:
        return MemoryRecordKind.PREFERENCE
    if candidate.proposed_kind is PromotionCandidateKind.PROCEDURAL_UPDATE:
        return MemoryRecordKind.PROCEDURAL_SNAPSHOT
    raise AssertionError(f"unhandled promotion kind {candidate.proposed_kind.value}")


def _tier_for_record_kind(kind: MemoryRecordKind) -> MemoryTier:
    if kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT:
        return MemoryTier.PROCEDURAL
    return MemoryTier.SEMANTIC


def _record_content(
    candidate: PromotionCandidate,
    *,
    status: SemanticRecordStatus,
    injection_policy: SemanticInjectionPolicy,
    preference_details: PreferencePromotionDetails | None,
    rationale: str | None,
    tags: Sequence[str],
    review_reason: str | None,
    statement_override: str | None,
    policy_ref: str | None,
) -> dict[str, object]:
    if candidate.proposed_kind is PromotionCandidateKind.PROCEDURAL_UPDATE:
        return _procedural_record_content(
            candidate,
            status=status,
            injection_policy=injection_policy,
            rationale=rationale,
            tags=tags,
            review_reason=review_reason,
            statement_override=statement_override,
            policy_ref=policy_ref,
        )
    return _semantic_record_content(
        candidate,
        status=status,
        injection_policy=injection_policy,
        preference_details=preference_details,
        rationale=rationale,
        tags=tags,
        review_reason=review_reason,
        statement_override=statement_override,
    )


def _semantic_record_content(
    candidate: PromotionCandidate,
    *,
    status: SemanticRecordStatus,
    injection_policy: SemanticInjectionPolicy,
    preference_details: PreferencePromotionDetails | None,
    rationale: str | None,
    tags: Sequence[str],
    review_reason: str | None,
    statement_override: str | None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "source_memory_refs": [str(memory_id) for memory_id in candidate.source_memory_refs],
        "semantic_kind": candidate.proposed_kind.value,
        "statement": statement_override or candidate.statement,
        "rationale": rationale,
        "evidence": [ref.model_dump(mode="json") for ref in candidate.source_refs],
        "confidence": candidate.confidence.value,
        "status": status.value,
        "ttl": None,
        "expires_at": None,
        "injection_policy": injection_policy.value,
        "tags": [str(tag) for tag in tags],
    }
    if review_reason is not None:
        content["review_reason"] = review_reason
    if candidate.proposed_kind is PromotionCandidateKind.PREFERENCE:
        assert preference_details is not None
        content.update(
            {
                "preference_subject": preference_details.preference_subject.value,
                "preference_strength": preference_details.preference_strength.value,
                "source_authority": preference_details.source_authority.value,
                "confirmation_required": preference_details.confirmation_required,
            }
        )
    return content


def _procedural_record_content(
    candidate: PromotionCandidate,
    *,
    status: SemanticRecordStatus,
    injection_policy: SemanticInjectionPolicy,
    rationale: str | None,
    tags: Sequence[str],
    review_reason: str | None,
    statement_override: str | None,
    policy_ref: str | None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "snapshot_id": candidate.candidate_id,
        "workflow_id": candidate.suggested_scope.workflow,
        "cli_profile": candidate.suggested_scope.cli_profile,
        "prompt_refs": [],
        "skill_refs": [],
        "routing_manifest_ref": None,
        "instruction_file_refs": [],
        "memory_policy_ref": policy_ref,
        "procedural_update": statement_override or candidate.statement,
        "rationale": rationale,
        "evidence": [ref.model_dump(mode="json") for ref in candidate.source_refs],
        "confidence": candidate.confidence.value,
        "status": status.value,
        "injection_policy": injection_policy.value,
        "tags": [str(tag) for tag in tags],
    }
    if review_reason is not None:
        content["review_reason"] = review_reason
    return content


def _validate_preference_promotion(
    candidate: PromotionCandidate,
    *,
    status: SemanticRecordStatus,
    injection_policy: SemanticInjectionPolicy,
    preference_details: PreferencePromotionDetails | None,
) -> None:
    if candidate.proposed_kind is not PromotionCandidateKind.PREFERENCE:
        if preference_details is not None:
            raise PreferencePromotionValidationError(
                "preference_details are only valid for preference candidates"
            )
        return
    if preference_details is None:
        raise PreferencePromotionValidationError("preference promotion requires preference_details")
    if not candidate.source_refs:
        raise PreferencePromotionValidationError("preference promotion requires source evidence")
    if (
        status is SemanticRecordStatus.ACTIVE
        and preference_details.source_authority
        is PreferenceSourceAuthority.INFERRED_FROM_REPETITION
        and len(candidate.source_refs) < 2
    ):
        raise PreferencePromotionValidationError(
            "inferred preference promotion requires at least two source refs "
            "or must remain proposed"
        )
    if (
        preference_details.preference_strength is PreferenceStrength.MANDATORY
        and not _scope_has_binding(candidate.suggested_scope)
    ):
        raise PreferencePromotionValidationError(
            "mandatory preference promotion requires a scoped binding"
        )


def _scope_has_binding(scope: MemoryScope) -> bool:
    return any(
        getattr(scope, field_name) is not None
        for field_name in (
            "project",
            "workflow",
            "workload_class",
            "provider_family",
            "cli_profile",
            "tenant",
        )
    )


__all__ = [
    "PreferenceCandidateSource",
    "PreferencePromotionDetails",
    "PreferencePromotionValidationError",
    "PreferenceSourceAuthority",
    "PreferenceStrength",
    "PreferenceSubject",
    "PromotionCandidate",
    "PromotionCandidateConfidence",
    "PromotionCandidateExtractor",
    "PromotionCandidateHint",
    "PromotionCandidateKind",
    "PromotionDecisionResult",
    "PromotionDecisionService",
    "PromotionDecisionStore",
    "PromotionReviewRequiredError",
    "PromotionRiskFlag",
    "SemanticInjectionPolicy",
    "SemanticRecordStatus",
]
