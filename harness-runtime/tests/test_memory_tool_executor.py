"""Tests for U-MEM-16 - standard provider-neutral memory tool executor."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from harness_as.memory_tool_contracts import MemoryToolName
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import MemoryOperationKind
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import (
    AccessDecision,
    CaptureDecision,
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
    RedactionState,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_redaction import MemoryRedactionActor, MemoryRedactionService
from harness_is.memory_retrieval import MemoryRetriever
from harness_is.memory_retrieval_index import DerivedRetrievalIndexStore
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.memory_tool_executor import (
    MemoryToolExecutionContext,
    MemoryToolExecutionDeniedError,
    MemoryToolExecutionRequest,
    StandardMemoryToolExecutor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_NOW = datetime(2026, 7, 2, 18, 0, 0, tzinfo=UTC)
_POLICY_REF = "policy:u-mem-16"
_SCOPE_REF = "scope:u-mem-16"


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
    visibility: MemoryVisibility = MemoryVisibility.WORKFLOW,
) -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow=workflow,
        workload_class="coding-arc",
        provider_family="openai",
        cli_profile="codex",
        visibility=visibility,
    )


def _semantic_record(
    *,
    kind: MemoryRecordKind,
    statement: str,
    status: str = "active",
    redaction_state: RedactionState = RedactionState.ACTIVE,
) -> MemoryStoreRecord:
    content: dict[str, object] = {
        "semantic_kind": kind.value,
        "statement": statement,
        "confidence": "high",
        "source_authority": "operator_direct",
        "status": status,
        "injection_policy": "tool_allowed",
        "tags": ["codex", "memory", "workflow"],
    }
    if kind is MemoryRecordKind.PREFERENCE:
        content.update(
            {
                "preference_subject": "operator_workflow",
                "preference_strength": "strong",
                "confirmation_required": False,
            }
        )
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(MemoryTier.SEMANTIC, kind, content_hash),
            schema_version="memory-store-record/v1",
            tier=MemoryTier.SEMANTIC,
            kind=kind,
            created_at=_NOW,
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:u-mem-16"),),
            scope=_scope(),
            content_hash=content_hash,
            redaction_state=redaction_state,
        ),
        content=content,
    )


def _policy(**overrides: object) -> MemoryPolicyDocument:
    fields: dict[str, object] = {
        "policy_id": _POLICY_REF,
        "enabled": True,
        "capture_decision": CaptureDecision.CAPTURE_FULL,
        "promotion_decision": PromotionDecision.PROPOSE_SEMANTIC,
        "retrieval_access": AccessDecision.RETRIEVAL_ONLY,
        "standard_tool_access": AccessDecision.STANDARD_TOOLS,
        "review_mode": ReviewMode.OPERATOR_REQUIRED,
    }
    fields.update(overrides)
    return MemoryPolicyDocument(**fields)


def _context() -> MemoryToolExecutionContext:
    return MemoryToolExecutionContext(
        run_id="run-u-mem-16",
        workflow_id="memory-substrate",
        workload_class="coding-arc",
        step_id="step-memory-tools",
        provider="openai",
        model="gpt-5",
        cli_profile="codex",
        scope=_scope(),
        scope_ref=_SCOPE_REF,
        policy_ref=_POLICY_REF,
        token_budget=120,
        timestamp=_NOW,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
    )


def _executor(
    tmp_path: Path,
    *,
    policy: MemoryPolicyDocument | None = None,
    records: tuple[MemoryStoreRecord, ...] = (),
    tracer_provider: TracerProvider | None = None,
) -> tuple[CanonicalMemoryStore, StandardMemoryToolExecutor]:
    binding = _binding(tmp_path)
    store = _store(binding)
    for record in records:
        store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)
    resolver = MemoryPolicyResolver(policy or _policy())
    retriever = MemoryRetriever(
        store=store,
        index_store=index_store,
        policy_resolver=resolver,
        policy_ref=_POLICY_REF,
    )
    return store, StandardMemoryToolExecutor(
        store=store,
        index_store=index_store,
        retriever=retriever,
        policy_resolver=resolver,
        tracer_provider=tracer_provider,
    )


def _request(
    tool_name: MemoryToolName,
    arguments: dict[str, object],
) -> MemoryToolExecutionRequest:
    return MemoryToolExecutionRequest(
        tool_name=tool_name,
        arguments=arguments,
        context=_context(),
    )


def test_search_and_read_return_only_policy_allowed_refs(tmp_path: Path) -> None:
    allowed = _semantic_record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex memory tools may retrieve allowed project workflow preferences.",
    )
    denied_by_kind = _semantic_record(
        kind=MemoryRecordKind.CONVENTION,
        statement="Codex conventions are intentionally denied by the eligible kind filter.",
    )
    redacted = _semantic_record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex redacted memory must not be returned by standard tools.",
        redaction_state=RedactionState.REDACTED,
    )
    store, executor = _executor(
        tmp_path,
        policy=_policy(eligible_record_kinds=(MemoryRecordKind.PREFERENCE,)),
        records=(allowed, denied_by_kind, redacted),
    )

    search = executor.execute(
        _request(
            MemoryToolName.SEARCH,
            {
                "query": "codex memory tools workflow preferences",
                "scope_ref": _SCOPE_REF,
                "policy_ref": _POLICY_REF,
                "limit": 10,
                "allowed_kinds": ["preference", "convention"],
            },
        )
    )

    assert search["policy_ref"] == _POLICY_REF
    raw_results = search["results"]
    assert isinstance(raw_results, list)
    results = cast(list[dict[str, object]], raw_results)
    assert [item["memory_ref"] for item in results] == [str(allowed.envelope.memory_id)]
    result = results[0]
    assert result["record_kind"] == MemoryRecordKind.PREFERENCE.value
    assert result["packet_hash"]
    assert result["packet_section_ref"]

    read = executor.execute(
        _request(
            MemoryToolName.READ,
            {
                "memory_ref": result["memory_ref"],
                "packet_section_ref": result["packet_section_ref"],
                "policy_ref": _POLICY_REF,
            },
        )
    )

    assert read == {
        "memory_ref": str(allowed.envelope.memory_id),
        "record_kind": MemoryRecordKind.PREFERENCE.value,
        "packet_section_ref": result["packet_section_ref"],
        "content_hash": allowed.envelope.content_hash.hex(),
        "policy_ref": _POLICY_REF,
    }
    with pytest.raises(MemoryToolExecutionDeniedError):
        executor.execute(
            _request(
                MemoryToolName.READ,
                {"memory_ref": str(denied_by_kind.envelope.memory_id), "policy_ref": _POLICY_REF},
            )
        )

    ledger_kinds = [entry.operation_kind for entry in store.read_memory_operations()]
    assert MemoryOperationKind.RETRIEVE in ledger_kinds
    assert ledger_kinds.count(MemoryOperationKind.STANDARD_TOOL_CALL) == 2


def test_redaction_transition_excludes_standard_tool_search_and_read(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    record = _semantic_record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex standard tools must drop records after redaction transitions.",
    )
    store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)
    resolver = MemoryPolicyResolver(_policy(eligible_record_kinds=(MemoryRecordKind.PREFERENCE,)))
    retriever = MemoryRetriever(
        store=store,
        index_store=index_store,
        policy_resolver=resolver,
        policy_ref=_POLICY_REF,
    )
    executor = StandardMemoryToolExecutor(
        store=store,
        index_store=index_store,
        retriever=retriever,
        policy_resolver=resolver,
    )
    search_args = {
        "query": "codex standard tools redaction transitions",
        "scope_ref": _SCOPE_REF,
        "policy_ref": _POLICY_REF,
        "limit": 10,
        "allowed_kinds": ["preference"],
    }
    before = executor.execute(_request(MemoryToolName.SEARCH, search_args))
    before_results = cast(list[dict[str, object]], before["results"])
    assert [item["memory_ref"] for item in before_results] == [str(record.envelope.memory_id)]

    MemoryRedactionService(
        store=store,
        operation_actor=_context().actor,
        event_actor=MemoryRedactionActor.OPERATOR,
        policy_ref=_POLICY_REF,
    ).redact_content(
        record.envelope.memory_id,
        record.envelope.kind,
        timestamp=_NOW,
        reason="operator requested standard-tool redaction",
        replacement_summary="Removed from standard memory tools.",
    )
    with pytest.raises(MemoryToolExecutionDeniedError, match="unavailable"):
        executor.execute(
            _request(
                MemoryToolName.READ,
                {
                    "memory_ref": str(record.envelope.memory_id),
                    "policy_ref": _POLICY_REF,
                },
            )
        )
    index_store.rebuild(indexed_at=_NOW)

    after = executor.execute(_request(MemoryToolName.SEARCH, search_args))
    assert after["results"] == []
    with pytest.raises(MemoryToolExecutionDeniedError, match="unavailable"):
        executor.execute(
            _request(
                MemoryToolName.READ,
                {
                    "memory_ref": str(record.envelope.memory_id),
                    "policy_ref": _POLICY_REF,
                },
            )
        )
    ledger_kinds = [entry.operation_kind for entry in store.read_memory_operations()]
    assert MemoryOperationKind.RETRIEVE in ledger_kinds
    assert MemoryOperationKind.REDACT in ledger_kinds
    assert ledger_kinds.count(MemoryOperationKind.STANDARD_TOOL_CALL) == 2


def test_write_note_stays_episodic_and_records_capture_and_tool_call(tmp_path: Path) -> None:
    store, executor = _executor(tmp_path)

    result = executor.execute(
        _request(
            MemoryToolName.WRITE_NOTE,
            {
                "note": "Remember that U-MEM-16 standard memory tools use the canonical store.",
                "scope_ref": _SCOPE_REF,
                "policy_ref": _POLICY_REF,
                "idempotency_key": "note:u-mem-16",
            },
        )
    )

    memory_ref = MemoryID(str(result["memory_ref"]))
    record = store.read_record(memory_ref, MemoryRecordKind.TOOL_EVENT, run_id="run-u-mem-16")
    assert record.envelope.tier is MemoryTier.EPISODIC
    assert record.envelope.kind is MemoryRecordKind.TOOL_EVENT
    assert "standard memory tools" in str(record.content["summary"])
    ledger_kinds = [entry.operation_kind for entry in store.read_memory_operations()]
    assert MemoryOperationKind.CAPTURE in ledger_kinds
    assert MemoryOperationKind.STANDARD_TOOL_CALL in ledger_kinds


def test_promotion_and_redaction_requests_create_reviewable_durable_entries(
    tmp_path: Path,
) -> None:
    store, executor = _executor(tmp_path)
    write_result = executor.execute(
        _request(
            MemoryToolName.WRITE_NOTE,
            {
                "note": "Standard memory tool executor should be promoted as a project convention.",
                "scope_ref": _SCOPE_REF,
                "policy_ref": _POLICY_REF,
            },
        )
    )

    promotion = executor.execute(
        _request(
            MemoryToolName.PROPOSE_PROMOTION,
            {
                "memory_ref": write_result["memory_ref"],
                "target_kind": "convention",
                "evidence_ref": "tool-note:u-mem-16",
                "policy_ref": _POLICY_REF,
            },
        )
    )

    assert str(promotion["promotion_ref"]).startswith("mem:semantic:convention:")
    assert str(promotion["operation_ref"]).startswith("promotion:propose_promotion:")
    assert promotion["review_required"] is True

    redaction = executor.execute(
        _request(
            MemoryToolName.REQUEST_REDACTION,
            {
                "memory_ref": write_result["memory_ref"],
                "reason": "operator requested review of this note",
                "policy_ref": _POLICY_REF,
            },
        )
    )

    assert str(redaction["redaction_request_ref"]).startswith("redaction-request:")
    assert str(redaction["operation_ref"]).startswith("delete_request:")
    ledger_kinds = [entry.operation_kind for entry in store.read_memory_operations()]
    assert MemoryOperationKind.PROPOSE_PROMOTION in ledger_kinds
    assert MemoryOperationKind.DELETE_REQUEST in ledger_kinds
    assert ledger_kinds.count(MemoryOperationKind.STANDARD_TOOL_CALL) == 3


def test_standard_tool_policy_denial_fails_closed(tmp_path: Path) -> None:
    _, executor = _executor(
        tmp_path,
        policy=_policy(standard_tool_access=AccessDecision.DENY),
    )

    with pytest.raises(MemoryToolExecutionDeniedError, match="standard memory tools"):
        executor.execute(
            _request(
                MemoryToolName.SEARCH,
                {"query": "codex", "scope_ref": _SCOPE_REF, "policy_ref": _POLICY_REF},
            )
        )


def test_write_like_source_refs_respect_retrieval_policy(tmp_path: Path) -> None:
    denied_source = _semantic_record(
        kind=MemoryRecordKind.CONVENTION,
        statement="Denied source records must not seed write-like standard memory tools.",
    )
    store, executor = _executor(
        tmp_path,
        policy=_policy(eligible_record_kinds=(MemoryRecordKind.PREFERENCE,)),
        records=(denied_source,),
    )

    with pytest.raises(MemoryToolExecutionDeniedError, match="denied by policy"):
        executor.execute(
            _request(
                MemoryToolName.PROPOSE_PROMOTION,
                {
                    "memory_ref": str(denied_source.envelope.memory_id),
                    "target_kind": "convention",
                    "policy_ref": _POLICY_REF,
                },
            )
        )

    with pytest.raises(MemoryToolExecutionDeniedError, match="denied by policy"):
        executor.execute(
            _request(
                MemoryToolName.REQUEST_REDACTION,
                {
                    "memory_ref": str(denied_source.envelope.memory_id),
                    "reason": "review denied source handling",
                    "policy_ref": _POLICY_REF,
                },
            )
        )

    ledger_kinds = [entry.operation_kind for entry in store.read_memory_operations()]
    assert MemoryOperationKind.PROPOSE_PROMOTION not in ledger_kinds
    assert MemoryOperationKind.DELETE_REQUEST not in ledger_kinds


def test_standard_memory_tool_call_emits_span(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    record = _semantic_record(
        kind=MemoryRecordKind.PREFERENCE,
        statement="Codex memory tool spans should carry the standard tool name.",
    )
    _, executor = _executor(
        tmp_path,
        records=(record,),
        tracer_provider=tracer_provider,
    )

    executor.execute(
        _request(
            MemoryToolName.SEARCH,
            {
                "query": "codex memory tool spans",
                "scope_ref": _SCOPE_REF,
                "policy_ref": _POLICY_REF,
            },
        )
    )

    spans = exporter.get_finished_spans()
    assert [(span.name, span.attributes["memory.tool.name"]) for span in spans] == [
        ("memory.tool_call", MemoryToolName.SEARCH.value)
    ]
    assert (
        spans[0].attributes["memory.operation.kind"] == MemoryOperationKind.STANDARD_TOOL_CALL.value
    )
    assert spans[0].attributes["memory.policy_ref"] == _POLICY_REF
    assert spans[0].attributes["memory.operation.name"] == "standard_tool_call"
    assert spans[0].attributes["memory.access_mode"] == "standard_memory_tools"
    assert spans[0].attributes["memory.provider"] == "openai"
    assert spans[0].attributes["memory.model"] == "gpt-5"
    assert spans[0].attributes["memory.cli_profile"] == "codex"
    assert spans[0].attributes["memory.policy.decision"] == "allowed"
    assert spans[0].attributes["memory.record_count"] == 1


def test_standard_memory_tool_denial_emits_policy_failure_class(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    _, executor = _executor(
        tmp_path,
        policy=_policy(standard_tool_access=AccessDecision.DENY),
        tracer_provider=tracer_provider,
    )

    with pytest.raises(MemoryToolExecutionDeniedError, match="denied"):
        executor.execute(
            _request(
                MemoryToolName.SEARCH,
                {
                    "query": "denied telemetry",
                    "scope_ref": _SCOPE_REF,
                    "policy_ref": _POLICY_REF,
                },
            )
        )

    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.tool_call"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "standard_tool_call"
    assert attrs["memory.failure_class"] == "policy_denial"
    assert attrs["memory.policy.decision"] == "denied"
