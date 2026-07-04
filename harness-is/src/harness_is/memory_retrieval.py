"""Memory retrieval, ranking, and packet assembly - U-MEM-11."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness_is.memory_operation_ledger import (
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
)
from harness_is.memory_policy import AccessDecision, MemoryPolicyResolver
from harness_is.memory_record_envelope import MemoryID, MemoryRecordKind, MemoryScope
from harness_is.memory_retrieval_index import (
    DerivedRetrievalIndex,
    DerivedRetrievalIndexEntry,
    DerivedRetrievalIndexStore,
)
from harness_is.memory_store import (
    CanonicalMemoryStore,
    MemoryStoreRecord,
    MemoryStoreRecordUnavailableError,
)
from harness_is.state_ledger_entry_schema import Actor, Identifier

type RetrievalJSON = str | int | bool | None | list["RetrievalJSON"] | dict[str, "RetrievalJSON"]


class MemoryPacketAccessMode(StrEnum):
    """C-MEM-12 packet access modes."""

    NATIVE_PROVIDER_MEMORY = "native_provider_memory"
    STANDARD_MEMORY_TOOLS = "standard_memory_tools"
    PROMPT_EXTENSION_PACKET = "prompt_extension_packet"
    NO_MEMORY_ACCESS = "no_memory_access"


class RetrievalExclusionReason(StrEnum):
    """Deterministic reasons for considered records excluded from retrieval."""

    KIND_NOT_ALLOWED = "kind_not_allowed"
    QUERY_MISMATCH = "query_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    POLICY_DENIED = "policy_denied"
    REDACTED = "redacted"
    TOMBSTONED = "tombstoned"
    EXPIRED = "expired"
    DENIED = "denied"
    PROPOSED = "proposed"
    SUPERSEDED = "superseded"
    RECORD_UNAVAILABLE = "record_unavailable"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"


_INACTIVE_STATUS_REASONS: Mapping[str, RetrievalExclusionReason] = {
    "denied": RetrievalExclusionReason.DENIED,
    "expired": RetrievalExclusionReason.EXPIRED,
    "proposed": RetrievalExclusionReason.PROPOSED,
    "superseded": RetrievalExclusionReason.SUPERSEDED,
    "tombstoned": RetrievalExclusionReason.TOMBSTONED,
}


class MemoryRetrievalRequest(BaseModel):
    """C-MEM-11 retrieval request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    workflow_id: str | None = None
    workload_class: str | None = None
    cli_profile: str
    provider: str
    model: str
    query_summary: str
    scope: MemoryScope
    token_budget: int = Field(ge=0)
    allowed_kinds: tuple[MemoryRecordKind, ...] = ()


class ExcludedMemoryRef(BaseModel):
    """A considered memory ref excluded from the result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_ref: MemoryID
    record_kind: MemoryRecordKind
    reason: RetrievalExclusionReason


class RankingTraceEntry(BaseModel):
    """Deterministic ranking trace for one eligible memory ref."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_ref: MemoryID
    record_kind: MemoryRecordKind
    score: int
    factors: tuple[str, ...]
    selected: bool = False


class MemoryPacketSection(BaseModel):
    """One source-linked packet section."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str
    memory_ref: MemoryID
    record_kind: MemoryRecordKind
    text: str
    token_estimate: int = Field(ge=0)


class MemoryPacket(BaseModel):
    """C-MEM-12 bounded source-linked memory packet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: str
    packet_hash: str
    token_budget: int = Field(ge=0)
    access_mode: MemoryPacketAccessMode
    sections: tuple[MemoryPacketSection, ...]
    selected_refs: tuple[MemoryID, ...]
    policy_ref: str

    @model_validator(mode="after")
    def _hash_shape(self) -> Self:
        _validate_sha256_hex(self.packet_hash, field_name="packet_hash")
        return self


class MemoryRetrievalResult(BaseModel):
    """C-MEM-11 retrieval result plus assembled C-MEM-12 packet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_hash: str
    selected_refs: tuple[MemoryID, ...]
    excluded_refs: tuple[ExcludedMemoryRef, ...]
    packet_hash: str
    ranking_trace: tuple[RankingTraceEntry, ...]
    packet: MemoryPacket

    @model_validator(mode="after")
    def _hashes_match_packet(self) -> Self:
        _validate_sha256_hex(self.request_hash, field_name="request_hash")
        _validate_sha256_hex(self.packet_hash, field_name="packet_hash")
        if self.packet_hash != self.packet.packet_hash:
            raise ValueError("packet_hash must match packet.packet_hash")
        if self.selected_refs != self.packet.selected_refs:
            raise ValueError("selected_refs must match packet.selected_refs")
        return self


class _EligibleRecord(BaseModel):
    """Internal candidate carrying index metadata and canonical content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: DerivedRetrievalIndexEntry
    record: MemoryStoreRecord
    score: int
    factors: tuple[str, ...]


class MemoryRetriever:
    """Retrieve, rank, packetize, and ledger memory reads."""

    def __init__(
        self,
        *,
        store: CanonicalMemoryStore,
        index_store: DerivedRetrievalIndexStore,
        policy_resolver: MemoryPolicyResolver,
        policy_ref: str,
    ) -> None:
        self._store = store
        self._index_store = index_store
        self._policy_resolver = policy_resolver
        self._policy_ref = policy_ref

    def retrieve(
        self,
        request: MemoryRetrievalRequest,
        *,
        timestamp: datetime,
        actor: Actor,
        access_mode: MemoryPacketAccessMode,
    ) -> MemoryRetrievalResult:
        """Return a stable retrieval result and append a durable retrieval event."""

        index = self._index_store.read_current()
        request_hash = _request_hash(
            request,
            index=index,
            policy_ref=self._policy_ref,
        )
        eligible, excluded = self._eligible_records(request, index)
        ordered_for_packet = tuple(sorted(eligible, key=_packet_candidate_sort_key))
        sections, selected, budget_excluded = _assemble_sections(
            ordered_for_packet,
            token_budget=request.token_budget,
        )
        selected_refs = tuple(item.entry.memory_id for item in selected)
        packet = _packet_for(
            sections,
            selected_refs=selected_refs,
            token_budget=request.token_budget,
            access_mode=access_mode,
            policy_ref=self._policy_ref,
        )
        ranking_trace = _ranking_trace(eligible, selected_refs=selected_refs)
        result = MemoryRetrievalResult(
            request_hash=request_hash,
            selected_refs=selected_refs,
            excluded_refs=tuple(
                sorted(
                    (*excluded, *budget_excluded),
                    key=lambda excluded_ref: str(excluded_ref.memory_ref),
                )
            ),
            packet_hash=packet.packet_hash,
            ranking_trace=ranking_trace,
            packet=packet,
        )
        self._write_retrieval_event(
            result,
            request=request,
            timestamp=timestamp,
            actor=actor,
        )
        return result

    def _eligible_records(
        self,
        request: MemoryRetrievalRequest,
        index: DerivedRetrievalIndex,
    ) -> tuple[tuple[_EligibleRecord, ...], tuple[ExcludedMemoryRef, ...]]:
        query_terms = set(_tokenize(request.query_summary))
        eligible: list[_EligibleRecord] = []
        excluded: list[ExcludedMemoryRef] = []
        for entry in index.entries:
            reason = _static_exclusion_reason(entry, request)
            if reason is not None:
                excluded.append(_excluded(entry, reason))
                continue
            policy = self._policy_resolver.resolve_retrieval(
                record_kind=entry.record_kind,
                record_scope=entry.scope,
                requested_scope=request.scope,
            )
            if policy.access_decision is AccessDecision.DENY:
                excluded.append(_excluded(entry, RetrievalExclusionReason.POLICY_DENIED))
                continue
            try:
                record = self._store.read_record(entry.memory_id, entry.record_kind)
            except MemoryStoreRecordUnavailableError:
                excluded.append(_excluded(entry, RetrievalExclusionReason.RECORD_UNAVAILABLE))
                continue
            content_terms = _record_query_terms(record)
            if query_terms and not query_terms.intersection(content_terms):
                excluded.append(_excluded(entry, RetrievalExclusionReason.QUERY_MISMATCH))
                continue
            score, factors = _score_record(
                request,
                entry,
                record,
                query_terms=query_terms,
                content_terms=content_terms,
            )
            eligible.append(
                _EligibleRecord(
                    entry=entry,
                    record=record,
                    score=score,
                    factors=factors,
                )
            )
        return (
            tuple(sorted(eligible, key=_ranking_sort_key)),
            tuple(excluded),
        )

    def _write_retrieval_event(
        self,
        result: MemoryRetrievalResult,
        *,
        request: MemoryRetrievalRequest,
        timestamp: datetime,
        actor: Actor,
    ) -> None:
        event_hash = _hash_json(
            {
                "actor": actor.model_dump(mode="json"),
                "request_hash": result.request_hash,
                "packet_hash": result.packet_hash,
                "selected_refs": list(result.selected_refs),
                "policy_ref": self._policy_ref,
            }
        )
        self._store.append_memory_operation(
            MemoryOperationPayload(
                action_id=Identifier(f"retrieval:{event_hash[:32]}"),
                idempotency_key=Identifier(f"retrieval:{event_hash}"),
                actor=actor,
                timestamp=timestamp,
                operation_kind=MemoryOperationKind.RETRIEVE,
                operation_projection=MemoryOperationProjection.RETRIEVAL_EVENTS,
                run_id=request.run_id,
                provider=request.provider,
                model=request.model,
                cli_profile=request.cli_profile,
                memory_refs=result.selected_refs,
                policy_ref=self._policy_ref,
                procedural_snapshot_ref=_procedural_snapshot_ref(result.packet.sections),
            )
        )


def _validate_sha256_hex(value: str, *, field_name: str) -> None:
    if len(value) != 64:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest") from exc


def _excluded(
    entry: DerivedRetrievalIndexEntry,
    reason: RetrievalExclusionReason,
) -> ExcludedMemoryRef:
    return ExcludedMemoryRef(
        memory_ref=entry.memory_id,
        record_kind=entry.record_kind,
        reason=reason,
    )


def _static_exclusion_reason(
    entry: DerivedRetrievalIndexEntry,
    request: MemoryRetrievalRequest,
) -> RetrievalExclusionReason | None:
    if request.allowed_kinds and entry.record_kind not in request.allowed_kinds:
        return RetrievalExclusionReason.KIND_NOT_ALLOWED
    if entry.redaction_state.value == "redacted":
        return RetrievalExclusionReason.REDACTED
    if entry.redaction_state.value == "tombstoned":
        return RetrievalExclusionReason.TOMBSTONED
    status = entry.status
    if status is not None and status in _INACTIVE_STATUS_REASONS:
        return _INACTIVE_STATUS_REASONS[status]
    if entry.superseded_by:
        return RetrievalExclusionReason.SUPERSEDED
    if _scope_mismatch(entry.scope, request.scope):
        return RetrievalExclusionReason.SCOPE_MISMATCH
    return None


def _scope_mismatch(record_scope: MemoryScope, request_scope: MemoryScope) -> bool:
    for field_name in (
        "project",
        "workflow",
        "workload_class",
        "provider_family",
        "cli_profile",
        "tenant",
    ):
        record_value = getattr(record_scope, field_name)
        request_value = getattr(request_scope, field_name)
        if record_value is not None and request_value is not None and record_value != request_value:
            return True
    return False


def _score_record(
    request: MemoryRetrievalRequest,
    entry: DerivedRetrievalIndexEntry,
    record: MemoryStoreRecord,
    *,
    query_terms: set[str],
    content_terms: set[str],
) -> tuple[int, tuple[str, ...]]:
    score = 0
    factors: list[str] = []
    match_count = len(query_terms.intersection(content_terms))
    if match_count:
        score += match_count * 100
        factors.append(f"query_match:{match_count}")
    confidence_score = _confidence_score(record.content.get("confidence"))
    if confidence_score:
        score += confidence_score * 10
        factors.append(f"confidence:{confidence_score}")
    authority_score = _authority_score(record.content.get("source_authority"))
    if authority_score:
        score += authority_score * 10
        factors.append(f"source_authority:{authority_score}")
    if _is_pinned(record):
        score += 500
        factors.append("pinned")
    if request.workflow_id is not None and entry.scope.workflow == request.workflow_id:
        score += 25
        factors.append("workflow")
    if request.workload_class is not None and entry.scope.workload_class == request.workload_class:
        score += 20
        factors.append("workload_class")
    if entry.scope.cli_profile == request.cli_profile:
        score += 15
        factors.append("cli_profile")
    if _failure_risk_relevant(entry, record, query_terms):
        score += 30
        factors.append("failure_risk")
    section_priority = max(0, 10 - _section_sort_key(entry.record_kind))
    score += section_priority
    factors.append(f"section_priority:{section_priority}")
    return score, tuple(factors)


def _ranking_sort_key(candidate: _EligibleRecord) -> tuple[int, str, str]:
    return (
        -candidate.score,
        _reverse_iso(candidate.entry.created_at),
        str(candidate.entry.memory_id),
    )


def _packet_candidate_sort_key(candidate: _EligibleRecord) -> tuple[int, int, str, str]:
    return (
        _section_sort_key(candidate.entry.record_kind),
        -candidate.score,
        _reverse_iso(candidate.entry.created_at),
        str(candidate.entry.memory_id),
    )


def _ranking_trace(
    eligible: tuple[_EligibleRecord, ...],
    *,
    selected_refs: tuple[MemoryID, ...],
) -> tuple[RankingTraceEntry, ...]:
    selected_set = set(selected_refs)
    return tuple(
        RankingTraceEntry(
            memory_ref=candidate.entry.memory_id,
            record_kind=candidate.entry.record_kind,
            score=candidate.score,
            factors=candidate.factors,
            selected=candidate.entry.memory_id in selected_set,
        )
        for candidate in eligible
    )


def _assemble_sections(
    candidates: tuple[_EligibleRecord, ...],
    *,
    token_budget: int,
) -> tuple[
    tuple[MemoryPacketSection, ...],
    tuple[_EligibleRecord, ...],
    tuple[ExcludedMemoryRef, ...],
]:
    remaining = token_budget
    sections: list[MemoryPacketSection] = []
    selected: list[_EligibleRecord] = []
    excluded: list[ExcludedMemoryRef] = []
    for candidate in candidates:
        section = _section_for(candidate)
        if section.token_estimate > remaining:
            excluded.append(
                _excluded(candidate.entry, RetrievalExclusionReason.TOKEN_BUDGET_EXCEEDED)
            )
            continue
        sections.append(section)
        selected.append(candidate)
        remaining -= section.token_estimate
    return tuple(sections), tuple(selected), tuple(excluded)


def _section_for(candidate: _EligibleRecord) -> MemoryPacketSection:
    text = f"[{candidate.entry.memory_id}] {_record_text(candidate.record)}"
    return MemoryPacketSection(
        section_id=_section_id(candidate.entry.record_kind),
        memory_ref=candidate.entry.memory_id,
        record_kind=candidate.entry.record_kind,
        text=text,
        token_estimate=_token_estimate(text),
    )


def _packet_for(
    sections: tuple[MemoryPacketSection, ...],
    *,
    selected_refs: tuple[MemoryID, ...],
    token_budget: int,
    access_mode: MemoryPacketAccessMode,
    policy_ref: str,
) -> MemoryPacket:
    payload = {
        "token_budget": token_budget,
        "access_mode": access_mode.value,
        "sections": [section.model_dump(mode="json") for section in sections],
        "selected_refs": list(selected_refs),
        "policy_ref": policy_ref,
    }
    packet_hash = _hash_json(payload)
    return MemoryPacket(
        packet_id=f"memory-packet:{packet_hash[:32]}",
        packet_hash=packet_hash,
        token_budget=token_budget,
        access_mode=access_mode,
        sections=sections,
        selected_refs=selected_refs,
        policy_ref=policy_ref,
    )


def _request_hash(
    request: MemoryRetrievalRequest,
    *,
    index: DerivedRetrievalIndex,
    policy_ref: str,
) -> str:
    return _hash_json(
        {
            "request": request.model_dump(mode="json"),
            "index_version": index.index_version,
            "index_hash": index.index_hash,
            "policy_ref": policy_ref,
        }
    )


def _hash_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(_normalize_for_json(payload))).hexdigest()


def _canonical_json_bytes(payload: RetrievalJSON) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _normalize_for_json(value: object) -> RetrievalJSON:
    if isinstance(value, StrEnum):
        return unicodedata.normalize("NFC", value.value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("retrieval canonicalization does not accept float values")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _normalize_for_json(cast("Mapping[str, object]", value.model_dump(mode="json")))
    if isinstance(value, Mapping):
        normalized: dict[str, RetrievalJSON] = {}
        for key, item in cast("Mapping[object, object]", value).items():
            if not isinstance(key, str):
                raise TypeError("retrieval canonicalization requires string mapping keys")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError(f"duplicate canonical key {normalized_key!r}")
            normalized[normalized_key] = _normalize_for_json(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_normalize_for_json(item) for item in cast("Sequence[object]", value)]
    raise TypeError(f"unsupported retrieval canonicalization value: {type(value).__name__}")


def _record_text(record: MemoryStoreRecord) -> str:
    if record.envelope.kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT:
        value = record.content.get("procedural_update") or record.content.get("snapshot_id")
    else:
        value = record.content.get("statement")
    if isinstance(value, str) and value:
        return unicodedata.normalize("NFC", value)
    return _canonical_json_bytes(_normalize_for_json(record.content)).decode("utf-8")


def _confidence_score(value: object) -> int:
    if not isinstance(value, str):
        return 0
    return {
        "verified": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
    }.get(value, 0)


def _authority_score(value: object) -> int:
    if not isinstance(value, str):
        return 0
    return {
        "operator_direct": 4,
        "operator": 3,
        "project_doc": 2,
        "tool_observation": 1,
    }.get(value, 0)


def _is_pinned(record: MemoryStoreRecord) -> bool:
    pinned = record.content.get("pinned")
    if pinned is True:
        return True
    tags = record.content.get("tags")
    return isinstance(tags, Sequence) and not isinstance(tags, str | bytes) and "pinned" in tags


def _failure_risk_relevant(
    entry: DerivedRetrievalIndexEntry,
    record: MemoryStoreRecord,
    query_terms: set[str],
) -> bool:
    if entry.record_kind is MemoryRecordKind.FAILURE_LEARNING:
        return True
    risk_terms = {"failure", "fail", "risk", "hazard", "error", "stale"}
    if not risk_terms.intersection(query_terms):
        return False
    tags = record.content.get("tags")
    if not isinstance(tags, Sequence) or isinstance(tags, str | bytes):
        return False
    tag_terms = {str(tag) for tag in cast("Sequence[object]", tags)}
    return bool(risk_terms.intersection(tag_terms))


def _section_id(kind: MemoryRecordKind) -> str:
    return {
        MemoryRecordKind.PREFERENCE: "active_operator_project_preferences",
        MemoryRecordKind.CONVENTION: "current_project_conventions",
        MemoryRecordKind.DECISION: "relevant_prior_decisions",
        MemoryRecordKind.FAILURE_LEARNING: "failure_learnings_and_hazards",
        MemoryRecordKind.SEMANTIC_FACT: "research_or_domain_facts",
        MemoryRecordKind.RESEARCH: "research_or_domain_facts",
        MemoryRecordKind.PROCEDURAL_SNAPSHOT: "procedural_notes",
    }.get(kind, "research_or_domain_facts")


def _section_sort_key(kind: MemoryRecordKind) -> int:
    return {
        MemoryRecordKind.PREFERENCE: 0,
        MemoryRecordKind.CONVENTION: 1,
        MemoryRecordKind.DECISION: 2,
        MemoryRecordKind.FAILURE_LEARNING: 3,
        MemoryRecordKind.SEMANTIC_FACT: 4,
        MemoryRecordKind.RESEARCH: 4,
        MemoryRecordKind.PROCEDURAL_SNAPSHOT: 5,
    }.get(kind, 99)


def _token_estimate(text: str) -> int:
    return len(text.split()) + 1


def _record_query_terms(record: MemoryStoreRecord) -> set[str]:
    values = (
        record.content.get("semantic_kind"),
        record.content.get("statement"),
        record.content.get("preference_subject"),
        record.content.get("snapshot_id"),
        record.content.get("procedural_update"),
        record.content.get("tags"),
    )
    terms: set[str] = set()
    for value in values:
        terms.update(_tokenize_value(value))
    return terms


def _tokenize_value(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, StrEnum):
        return _tokenize(value.value)
    if isinstance(value, str):
        return _tokenize(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        terms: list[str] = []
        for item in cast("Sequence[object]", value):
            terms.extend(_tokenize_value(item))
        return tuple(terms)
    return ()


def _procedural_snapshot_ref(sections: tuple[MemoryPacketSection, ...]) -> str | None:
    for section in sections:
        if section.record_kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT:
            return str(section.memory_ref)
    return None


def _tokenize(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value).lower()
    return tuple(re.findall(r"[a-z0-9_:-]+", normalized))


def _reverse_iso(value: datetime) -> str:
    return "".join(chr(255 - ord(character)) for character in value.isoformat())


__all__ = [
    "ExcludedMemoryRef",
    "MemoryPacket",
    "MemoryPacketAccessMode",
    "MemoryPacketSection",
    "MemoryRetrievalRequest",
    "MemoryRetrievalResult",
    "MemoryRetriever",
    "RankingTraceEntry",
    "RetrievalExclusionReason",
]
