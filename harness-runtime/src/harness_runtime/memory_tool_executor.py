"""Provider-neutral standard memory tool executor - U-MEM-16."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime
from typing import Any, cast

from harness_as.memory_tool_contracts import MemoryToolName, memory_tool_contract
from harness_is.memory_observability import (
    MemoryTelemetryOperationName,
    classify_memory_failure,
    set_memory_telemetry_attributes,
)
from harness_is.memory_operation_ledger import (
    MemoryOperationEngineClass,
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
)
from harness_is.memory_policy import (
    AccessDecision,
    CaptureDecision,
    MemoryPolicyResolver,
    PromotionDecision,
    ReviewMode,
)
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordKind,
    MemoryScope,
    SourceRef,
    SourceRefType,
)
from harness_is.memory_retrieval import (
    MemoryPacketAccessMode,
    MemoryRetrievalRequest,
    MemoryRetriever,
)
from harness_is.memory_retrieval_index import DerivedRetrievalIndexEntry, DerivedRetrievalIndexStore
from harness_is.memory_store import (
    CanonicalMemoryStore,
    MemoryStoreRecord,
    MemoryStoreRecordUnavailableError,
)
from harness_is.state_ledger_entry_schema import Actor, Identifier
from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness_runtime.memory_capture import (
    EpisodicMemoryCapture,
    MemoryCaptureMode,
    MemoryCaptureStatus,
    SummaryProvenance,
    SummarySource,
)
from harness_runtime.memory_promotion import (
    PreferenceCandidateSource,
    PreferencePromotionDetails,
    PreferenceSourceAuthority,
    PreferenceStrength,
    PreferenceSubject,
    PromotionCandidate,
    PromotionCandidateConfidence,
    PromotionCandidateKind,
    PromotionDecisionService,
    PromotionRiskFlag,
    SemanticInjectionPolicy,
    SemanticRecordStatus,
)

type MemoryToolJSON = str | int | bool | None | list["MemoryToolJSON"] | dict[str, "MemoryToolJSON"]

_MAX_TOOL_TEXT_CHARS = 2_000


class MemoryToolExecutionError(Exception):
    """Base class for standard memory tool execution failures."""


class MemoryToolExecutionDeniedError(MemoryToolExecutionError):
    """Raised when policy denies a standard memory tool call."""


class MemoryToolExecutionInputError(MemoryToolExecutionError, ValueError):
    """Raised when a standard memory tool call has invalid arguments."""


class MemoryToolExecutionContext(BaseModel):
    """Runtime metadata attached to one provider-neutral memory tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    workflow_id: str | None = None
    workload_class: str | None = None
    step_id: str | None = None
    provider: str
    model: str
    cli_profile: str
    scope: MemoryScope
    scope_ref: str
    policy_ref: str
    token_budget: int = Field(default=120, ge=0)
    timestamp: datetime
    actor: Actor
    engine_class: MemoryOperationEngineClass | None = None
    procedural_snapshot_ref: str | None = None

    @field_validator("run_id", "provider", "model", "cli_profile", "scope_ref", "policy_ref")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value:
            raise ValueError("memory tool context strings cannot be empty")
        return value


class MemoryToolExecutionRequest(BaseModel):
    """One provider-neutral standard memory tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: MemoryToolName
    arguments: Mapping[str, object]
    context: MemoryToolExecutionContext


class StandardMemoryToolExecutor:
    """Execute C-MEM-14 provider-neutral memory tools under memory policy."""

    def __init__(
        self,
        *,
        store: CanonicalMemoryStore,
        index_store: DerivedRetrievalIndexStore,
        retriever: MemoryRetriever,
        policy_resolver: MemoryPolicyResolver,
        tracer_provider: Any | None = None,
    ) -> None:
        self._store = store
        self._index_store = index_store
        self._retriever = retriever
        self._policy_resolver = policy_resolver
        self._tracer_provider = tracer_provider

    def execute(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        """Execute one standard memory tool call and return its contract output."""

        memory_tool_contract(request.tool_name)
        span_cm = self._span_context(request)
        with span_cm as span:
            _set_span_attributes(span, request)
            try:
                self._require_standard_tool_access()
                result = self._execute_authorized(request)
                memory_refs = _memory_refs_from_result(request.tool_name, result)
                set_memory_telemetry_attributes(
                    span,
                    policy_decision="allowed",
                    record_count=len(memory_refs),
                )
                self._append_standard_tool_call(request, memory_refs=memory_refs)
                return result
            except Exception as exc:
                set_memory_telemetry_attributes(
                    span,
                    policy_decision="denied"
                    if isinstance(exc, MemoryToolExecutionDeniedError)
                    else "failed",
                    failure_class=classify_memory_failure(exc),
                )
                raise

    def _execute_authorized(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        tool_name = request.tool_name
        if tool_name is MemoryToolName.SEARCH:
            return self._search(request)
        if tool_name is MemoryToolName.READ:
            return self._read(request)
        if tool_name is MemoryToolName.WRITE_NOTE:
            return self._write_note(request)
        if tool_name is MemoryToolName.PROPOSE_PROMOTION:
            return self._propose_promotion(request)
        if tool_name is MemoryToolName.REQUEST_REDACTION:
            return self._request_redaction(request)
        raise MemoryToolExecutionInputError(f"unsupported memory tool {tool_name!s}")

    def _search(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        context = request.context
        args = request.arguments
        self._require_policy_ref(args, context)
        self._require_scope_ref(args, context)
        allowed_kinds = _allowed_kinds(args)
        retrieval = self._retriever.retrieve(
            MemoryRetrievalRequest(
                run_id=context.run_id,
                workflow_id=context.workflow_id,
                workload_class=context.workload_class,
                cli_profile=context.cli_profile,
                provider=context.provider,
                model=context.model,
                query_summary=_string_arg(args, "query"),
                scope=context.scope,
                token_budget=context.token_budget,
                allowed_kinds=allowed_kinds,
            ),
            timestamp=context.timestamp,
            actor=context.actor,
            access_mode=MemoryPacketAccessMode.STANDARD_MEMORY_TOOLS,
        )
        score_by_ref = {trace.memory_ref: trace.score for trace in retrieval.ranking_trace}
        limit = _positive_int_arg(args, "limit", default=len(retrieval.packet.sections))
        return {
            "results": [
                {
                    "memory_ref": str(section.memory_ref),
                    "record_kind": section.record_kind.value,
                    "packet_section_ref": section.section_id,
                    "packet_hash": retrieval.packet_hash,
                    "score": score_by_ref.get(section.memory_ref, 0),
                    "text": _packet_section_text(section.text, section.memory_ref),
                    "ranking_trace_ref": (
                        f"ranking:{retrieval.request_hash[:32]}:{section.memory_ref}"
                    ),
                }
                for section in retrieval.packet.sections[:limit]
            ],
            "policy_ref": context.policy_ref,
        }

    def _read(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        context = request.context
        args = request.arguments
        self._require_policy_ref(args, context)
        memory_ref = MemoryID(_string_arg(args, "memory_ref"))
        entry = self._allowed_index_entry(memory_ref, context)
        try:
            record = self._store.read_record(entry.memory_id, entry.record_kind)
        except MemoryStoreRecordUnavailableError as exc:
            raise MemoryToolExecutionDeniedError(
                f"memory ref {memory_ref!s} is unavailable"
            ) from exc
        packet_section_ref = _optional_string_arg(args, "packet_section_ref") or (
            f"record:{_stable_digest(str(memory_ref))[:32]}"
        )
        return {
            "memory_ref": str(record.envelope.memory_id),
            "record_kind": record.envelope.kind.value,
            "packet_section_ref": packet_section_ref,
            "content_hash": record.envelope.content_hash.hex(),
            "text": _record_text(record),
            "policy_ref": context.policy_ref,
        }

    def _write_note(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        context = request.context
        args = request.arguments
        self._require_policy_ref(args, context)
        self._require_scope_ref(args, context)
        capture = self._policy_resolver.resolve_capture()
        if capture.capture_decision is CaptureDecision.DENY:
            raise MemoryToolExecutionDeniedError("memory capture policy denies write_note")
        note = _string_arg(args, "note")
        capture_mode = _capture_mode_from_decision(capture.capture_decision)
        capture_api = EpisodicMemoryCapture(
            store=self._store,
            actor=context.actor,
            project=context.scope.project,
            visibility=context.scope.visibility,
            capture_mode=capture_mode,
        )
        tool_event_id = _tool_event_id(note, _optional_string_arg(args, "idempotency_key"))
        result = capture_api.capture_tool_event(
            run_id=context.run_id,
            tool_event_id=tool_event_id,
            tool_name=MemoryToolName.WRITE_NOTE.value,
            summary_text=note,
            summary=SummaryProvenance(source=SummarySource.MODEL_GENERATED, model=context.model),
            step_id=context.step_id,
            timestamp=context.timestamp,
            provider=context.provider,
            model=context.model,
            cli_profile=context.cli_profile,
            engine_class=context.engine_class,
            policy_ref=context.policy_ref,
            procedural_snapshot_ref=context.procedural_snapshot_ref,
            capture_mode=capture_mode,
        )
        if result.status is not MemoryCaptureStatus.CAPTURED or result.memory_id is None:
            raise MemoryToolExecutionError(result.failure_reason or "write_note capture failed")
        return {
            "memory_ref": str(result.memory_id),
            "operation_ref": str(result.operation_action_id),
            "policy_ref": context.policy_ref,
        }

    def _propose_promotion(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        context = request.context
        args = request.arguments
        self._require_policy_ref(args, context)
        promotion = self._policy_resolver.resolve_promotion()
        if promotion.promotion_decision is PromotionDecision.DISCARD:
            raise MemoryToolExecutionDeniedError("memory promotion policy discards candidates")
        if promotion.review_mode is ReviewMode.FORBIDDEN:
            raise MemoryToolExecutionDeniedError("memory promotion review is forbidden")
        source = self._read_retrievable_record_by_ref(
            MemoryID(_string_arg(args, "memory_ref")),
            context,
        )
        target_kind = _promotion_kind(_string_arg(args, "target_kind"))
        candidate = PromotionCandidate(
            candidate_id=_candidate_id(source, target_kind, context, args),
            source_refs=_source_refs(source, _optional_string_arg(args, "evidence_ref")),
            source_memory_refs=(source.envelope.memory_id,),
            proposed_kind=target_kind,
            statement=_promotion_statement(source),
            confidence=PromotionCandidateConfidence.HIGH,
            suggested_scope=context.scope,
            risk_flags=_promotion_risk_flags(source),
            preference_source=(
                PreferenceCandidateSource.INFERRED
                if target_kind is PromotionCandidateKind.PREFERENCE
                else None
            ),
            policy_decision=promotion.promotion_decision,
            review_mode=promotion.review_mode,
            review_required=_promotion_review_required(promotion),
            auto_promote_allowed=_promotion_auto_allowed(promotion),
        )
        service = PromotionDecisionService(
            store=self._store,
            actor=context.actor,
            policy_ref=context.policy_ref,
            procedural_snapshot_ref=context.procedural_snapshot_ref,
            run_id=context.run_id,
            step_id=context.step_id,
            provider=context.provider,
            model=context.model,
            cli_profile=context.cli_profile,
        )
        preference_details = _preference_details(candidate)
        if candidate.auto_promote_allowed:
            result = service.approve(
                candidate,
                timestamp=context.timestamp,
                injection_policy=SemanticInjectionPolicy.TOOL_ALLOWED,
                preference_details=preference_details,
            )
        else:
            result = service.propose_for_review(
                candidate,
                timestamp=context.timestamp,
                injection_policy=SemanticInjectionPolicy.TOOL_ALLOWED,
                preference_details=preference_details,
            )
        return {
            "promotion_ref": str(result.memory_id),
            "operation_ref": _promotion_operation_ref(result),
            "policy_ref": context.policy_ref,
            "review_required": result.status is SemanticRecordStatus.PROPOSED,
        }

    def _request_redaction(self, request: MemoryToolExecutionRequest) -> dict[str, object]:
        context = request.context
        args = request.arguments
        self._require_policy_ref(args, context)
        memory_ref = MemoryID(_string_arg(args, "memory_ref"))
        self._read_retrievable_record_by_ref(memory_ref, context)
        reason = _string_arg(args, "reason")
        event_hash = _hash_json(
            {
                "tool_name": request.tool_name.value,
                "memory_ref": str(memory_ref),
                "reason": reason,
                "policy_ref": context.policy_ref,
                "run_id": context.run_id,
            }
        )
        action_id = Identifier(f"delete_request:{event_hash[:32]}")
        self._store.append_memory_operation(
            MemoryOperationPayload(
                action_id=action_id,
                idempotency_key=Identifier(f"delete_request:{event_hash}"),
                actor=context.actor,
                timestamp=context.timestamp,
                operation_kind=MemoryOperationKind.DELETE_REQUEST,
                operation_projection=MemoryOperationProjection.NONE,
                run_id=context.run_id,
                step_id=context.step_id,
                provider=context.provider,
                model=context.model,
                cli_profile=context.cli_profile,
                engine_class=context.engine_class,
                memory_refs=(memory_ref,),
                policy_ref=context.policy_ref,
                procedural_snapshot_ref=context.procedural_snapshot_ref,
            )
        )
        return {
            "redaction_request_ref": f"redaction-request:{event_hash[:32]}",
            "operation_ref": str(action_id),
            "policy_ref": context.policy_ref,
        }

    def _allowed_index_entry(
        self,
        memory_ref: MemoryID,
        context: MemoryToolExecutionContext,
    ) -> DerivedRetrievalIndexEntry:
        index = self._index_store.read_current(require_fresh=False)
        for entry in index.entries:
            if entry.memory_id != memory_ref:
                continue
            access = self._policy_resolver.resolve_retrieval(
                record_kind=entry.record_kind,
                record_scope=entry.scope,
                requested_scope=context.scope,
            )
            if access.access_decision is AccessDecision.DENY:
                raise MemoryToolExecutionDeniedError(f"memory ref {memory_ref!s} denied by policy")
            if entry.redaction_state.value != "active" or entry.status in {
                "denied",
                "expired",
                "proposed",
                "superseded",
                "tombstoned",
            }:
                raise MemoryToolExecutionDeniedError(f"memory ref {memory_ref!s} is unavailable")
            if entry.superseded_by:
                raise MemoryToolExecutionDeniedError(f"memory ref {memory_ref!s} is superseded")
            return entry
        raise MemoryToolExecutionDeniedError(f"memory ref {memory_ref!s} is not retrievable")

    def _read_record_by_ref(
        self,
        memory_ref: MemoryID,
        context: MemoryToolExecutionContext,
    ) -> MemoryStoreRecord:
        kind = _kind_from_memory_ref(memory_ref)
        run_id = context.run_id if _episodic_kind(kind) else None
        try:
            return self._store.read_record(memory_ref, kind, run_id=run_id)
        except Exception as exc:
            raise MemoryToolExecutionDeniedError(
                f"memory ref {memory_ref!s} is unavailable"
            ) from exc

    def _read_retrievable_record_by_ref(
        self,
        memory_ref: MemoryID,
        context: MemoryToolExecutionContext,
    ) -> MemoryStoreRecord:
        record = self._read_record_by_ref(memory_ref, context)
        access = self._policy_resolver.resolve_retrieval(
            record_kind=record.envelope.kind,
            record_scope=record.envelope.scope,
            requested_scope=context.scope,
        )
        if access.access_decision is AccessDecision.DENY:
            raise MemoryToolExecutionDeniedError(f"memory ref {memory_ref!s} denied by policy")
        return record

    def _require_standard_tool_access(self) -> None:
        access = self._policy_resolver.resolve_standard_tools()
        if access.access_decision is not AccessDecision.STANDARD_TOOLS:
            raise MemoryToolExecutionDeniedError("standard memory tools denied by policy")

    def _require_policy_ref(
        self,
        args: Mapping[str, object],
        context: MemoryToolExecutionContext,
    ) -> None:
        if _string_arg(args, "policy_ref") != context.policy_ref:
            raise MemoryToolExecutionDeniedError("memory tool policy_ref does not match context")

    def _require_scope_ref(
        self,
        args: Mapping[str, object],
        context: MemoryToolExecutionContext,
    ) -> None:
        if _string_arg(args, "scope_ref") != context.scope_ref:
            raise MemoryToolExecutionDeniedError("memory tool scope_ref does not match context")

    def _append_standard_tool_call(
        self,
        request: MemoryToolExecutionRequest,
        *,
        memory_refs: tuple[MemoryID, ...],
    ) -> None:
        context = request.context
        event_hash = _hash_json(
            {
                "tool_name": request.tool_name.value,
                "arguments": _jsonable_mapping(request.arguments),
                "memory_refs": [str(memory_ref) for memory_ref in memory_refs],
                "policy_ref": context.policy_ref,
                "run_id": context.run_id,
                "step_id": context.step_id,
            }
        )
        action_id = Identifier(f"standard-tool-call:{event_hash[:32]}")
        self._store.append_memory_operation(
            MemoryOperationPayload(
                action_id=action_id,
                idempotency_key=Identifier(f"standard-tool-call:{event_hash}"),
                actor=context.actor,
                timestamp=context.timestamp,
                operation_kind=MemoryOperationKind.STANDARD_TOOL_CALL,
                operation_projection=MemoryOperationProjection.NONE,
                run_id=context.run_id,
                step_id=context.step_id,
                provider=context.provider,
                model=context.model,
                cli_profile=context.cli_profile,
                engine_class=context.engine_class,
                memory_refs=memory_refs,
                policy_ref=context.policy_ref,
                procedural_snapshot_ref=context.procedural_snapshot_ref,
            )
        )

    def _span_context(self, request: MemoryToolExecutionRequest) -> Any:
        if self._tracer_provider is None:
            return nullcontext(None)
        tracer = self._tracer_provider.get_tracer("harness.runtime.memory_tool_executor")
        return tracer.start_as_current_span("memory.tool_call")


def _set_span_attributes(span: Any, request: MemoryToolExecutionRequest) -> None:
    if span is None:
        return
    set_memory_telemetry_attributes(
        span,
        operation_name=MemoryTelemetryOperationName.STANDARD_TOOL_CALL,
        operation_kind=MemoryOperationKind.STANDARD_TOOL_CALL.value,
        access_mode=MemoryPacketAccessMode.STANDARD_MEMORY_TOOLS.value,
        provider=request.context.provider,
        model=request.context.model,
        cli_profile=request.context.cli_profile,
    )
    span.set_attribute("memory.tool.name", request.tool_name.value)
    span.set_attribute("memory.policy_ref", request.context.policy_ref)
    span.set_attribute("memory.scope_ref", request.context.scope_ref)


def _string_arg(args: Mapping[str, object], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise MemoryToolExecutionInputError(f"memory tool argument {name!r} must be a string")
    return value


def _optional_string_arg(args: Mapping[str, object], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MemoryToolExecutionInputError(f"memory tool argument {name!r} must be a string")
    return value


def _positive_int_arg(args: Mapping[str, object], name: str, *, default: int) -> int:
    value = args.get(name)
    if value is None:
        return max(0, default)
    if not isinstance(value, int) or value < 1:
        raise MemoryToolExecutionInputError(f"memory tool argument {name!r} must be >= 1")
    return value


def _allowed_kinds(args: Mapping[str, object]) -> tuple[MemoryRecordKind, ...]:
    raw = args.get("allowed_kinds")
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise MemoryToolExecutionInputError("allowed_kinds must be a sequence")
    kinds: list[MemoryRecordKind] = []
    for item in cast(Sequence[object], raw):
        if not isinstance(item, str):
            raise MemoryToolExecutionInputError("allowed_kinds entries must be strings")
        kinds.append(MemoryRecordKind(item))
    return tuple(kinds)


def _memory_refs_from_result(
    tool_name: MemoryToolName,
    result: Mapping[str, object],
) -> tuple[MemoryID, ...]:
    if tool_name is MemoryToolName.SEARCH:
        raw_results = result.get("results")
        if not isinstance(raw_results, Sequence) or isinstance(raw_results, str | bytes):
            return ()
        refs: list[MemoryID] = []
        for item in cast(Sequence[object], raw_results):
            if not isinstance(item, Mapping):
                continue
            result_item = cast(Mapping[str, object], item)
            ref = result_item.get("memory_ref")
            if isinstance(ref, str):
                refs.append(MemoryID(ref))
        return tuple(refs)
    if tool_name in {
        MemoryToolName.READ,
        MemoryToolName.WRITE_NOTE,
        MemoryToolName.PROPOSE_PROMOTION,
    }:
        key = "promotion_ref" if tool_name is MemoryToolName.PROPOSE_PROMOTION else "memory_ref"
        ref = result.get(key)
        return (MemoryID(ref),) if isinstance(ref, str) else ()
    return ()


def _tool_event_id(note: str, idempotency_key: str | None) -> str:
    if idempotency_key is not None:
        return idempotency_key
    return f"memory-write-note:{_stable_digest(note)[:32]}"


def _kind_from_memory_ref(memory_ref: MemoryID) -> MemoryRecordKind:
    parts = str(memory_ref).split(":", 3)
    if len(parts) != 4 or parts[0] != "mem":
        raise MemoryToolExecutionInputError(f"invalid memory_ref {memory_ref!s}")
    try:
        return MemoryRecordKind(parts[2])
    except ValueError as exc:
        raise MemoryToolExecutionInputError(f"unknown memory kind in {memory_ref!s}") from exc


def _episodic_kind(kind: MemoryRecordKind) -> bool:
    return kind in {
        MemoryRecordKind.EPISODIC_RUN,
        MemoryRecordKind.EPISODIC_TURN,
        MemoryRecordKind.TOOL_EVENT,
        MemoryRecordKind.COMPACTION_EVENT,
    }


def _promotion_kind(value: str) -> PromotionCandidateKind:
    aliases = {
        "semantic_fact": PromotionCandidateKind.FACT,
        "fact": PromotionCandidateKind.FACT,
        "decision": PromotionCandidateKind.DECISION,
        "convention": PromotionCandidateKind.CONVENTION,
        "failure_learning": PromotionCandidateKind.FAILURE_LEARNING,
        "research": PromotionCandidateKind.RESEARCH,
        "preference": PromotionCandidateKind.PREFERENCE,
        "procedural_snapshot": PromotionCandidateKind.PROCEDURAL_UPDATE,
        "procedural_update": PromotionCandidateKind.PROCEDURAL_UPDATE,
    }
    try:
        return aliases[value]
    except KeyError as exc:
        raise MemoryToolExecutionInputError(f"unsupported promotion target_kind {value!r}") from exc


def _candidate_id(
    source: MemoryStoreRecord,
    target_kind: PromotionCandidateKind,
    context: MemoryToolExecutionContext,
    args: Mapping[str, object],
) -> str:
    return "promocand:" + _hash_json(
        {
            "source_memory_ref": str(source.envelope.memory_id),
            "target_kind": target_kind.value,
            "statement": _promotion_statement(source),
            "scope": context.scope.model_dump(mode="json"),
            "evidence_ref": _optional_string_arg(args, "evidence_ref"),
        }
    )


def _source_refs(source: MemoryStoreRecord, evidence_ref: str | None) -> tuple[SourceRef, ...]:
    refs = list(source.envelope.source_refs)
    if evidence_ref is not None:
        refs.append(SourceRef(ref_type=SourceRefType.EXTERNAL, ref=evidence_ref))
    if not refs:
        refs.append(SourceRef(ref_type=SourceRefType.EXTERNAL, ref=str(source.envelope.memory_id)))
    return tuple(refs)


def _promotion_statement(source: MemoryStoreRecord) -> str:
    for key in ("statement", "summary", "response_summary", "prompt_summary", "procedural_update"):
        value = source.content.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return f"Memory evidence {source.envelope.memory_id}"


def _record_text(record: MemoryStoreRecord) -> str:
    for key in (
        "statement",
        "summary",
        "response_summary",
        "prompt_summary",
        "procedural_update",
    ):
        value = record.content.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded_text(value)
    return _bounded_text(
        json.dumps(
            _jsonable_mapping(record.content),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def _packet_section_text(text: str, memory_ref: MemoryID) -> str:
    prefix = f"[{memory_ref}] "
    if text.startswith(prefix):
        return _bounded_text(text[len(prefix) :])
    return _bounded_text(text)


def _bounded_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) <= _MAX_TOOL_TEXT_CHARS:
        return normalized
    return normalized[: _MAX_TOOL_TEXT_CHARS - 3].rstrip() + "..."


def _capture_mode_from_decision(decision: CaptureDecision) -> MemoryCaptureMode:
    if decision is CaptureDecision.CAPTURE_FULL:
        return MemoryCaptureMode.FULL
    if decision is CaptureDecision.SUMMARIZE_ONLY:
        return MemoryCaptureMode.SUMMARIZED
    if decision is CaptureDecision.CAPTURE_REDACTED:
        return MemoryCaptureMode.REDACTED
    raise MemoryToolExecutionDeniedError("memory capture policy denies write_note")


def _promotion_risk_flags(source: MemoryStoreRecord) -> tuple[PromotionRiskFlag, ...]:
    if source.envelope.redaction_state.value != "active":
        return (PromotionRiskFlag.SENSITIVE,)
    return ()


def _promotion_review_required(promotion: Any) -> bool:
    if promotion.review_mode is ReviewMode.OPERATOR_REQUIRED:
        return True
    return promotion.promotion_decision in {
        PromotionDecision.PROPOSE_SEMANTIC,
        PromotionDecision.PROPOSE_PROCEDURAL,
    }


def _promotion_auto_allowed(promotion: Any) -> bool:
    if promotion.review_mode is not ReviewMode.AUTOMATIC:
        return False
    return promotion.promotion_decision in {
        PromotionDecision.PROMOTE_SEMANTIC,
        PromotionDecision.PROMOTE_PROCEDURAL,
    }


def _preference_details(candidate: PromotionCandidate) -> PreferencePromotionDetails | None:
    if candidate.proposed_kind is not PromotionCandidateKind.PREFERENCE:
        return None
    return PreferencePromotionDetails(
        preference_subject=PreferenceSubject.OTHER,
        preference_strength=PreferenceStrength.NORMAL,
        source_authority=PreferenceSourceAuthority.INFERRED_FROM_REPETITION,
        confirmation_required=True,
    )


def _promotion_operation_ref(result: Any) -> str:
    return (
        f"promotion:{result.operation_kind.value}:"
        f"{result.record.content['candidate_id']}:{result.memory_id}"
    )


def _stable_digest(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _hash_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable_mapping(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _jsonable_mapping(value: Mapping[str, object]) -> dict[str, MemoryToolJSON]:
    normalized: dict[str, MemoryToolJSON] = {}
    for key, item in value.items():
        normalized[str(key)] = _jsonable(item)
    return normalized


def _jsonable(value: object) -> MemoryToolJSON:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, BaseModel):
        return _jsonable(cast(object, value.model_dump(mode="json")))
    if isinstance(value, Mapping):
        return _jsonable_mapping(cast(Mapping[str, object], value))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_jsonable(item) for item in cast(Sequence[object], value)]
    return str(value)


__all__ = [
    "MemoryToolExecutionContext",
    "MemoryToolExecutionDeniedError",
    "MemoryToolExecutionError",
    "MemoryToolExecutionInputError",
    "MemoryToolExecutionRequest",
    "StandardMemoryToolExecutor",
]
