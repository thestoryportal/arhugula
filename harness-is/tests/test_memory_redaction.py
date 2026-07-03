"""Tests for U-MEM-21 - redaction, tombstone, and retention (C-MEM-18)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import MemoryOperationKind
from harness_is.memory_path_registry import MemoryRootBinding
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
from harness_is.memory_redaction import (
    MemoryRedactionActor,
    MemoryRedactionKind,
    MemoryRedactionService,
)
from harness_is.memory_retrieval_index import DerivedRetrievalIndexQuery, DerivedRetrievalIndexStore
from harness_is.memory_store import (
    CanonicalMemoryStore,
    MemoryStoreRecord,
    MemoryStoreRecordUnavailableError,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass

_NOW = datetime(2026, 7, 2, 21, 0, 0, tzinfo=UTC)
_POLICY_REF = "policy:u-mem-21"
_RUN_ID = "run-u-mem-21"


def _binding(tmp_path: Path) -> MemoryRootBinding:
    return MemoryRootBinding(default_root=tmp_path / "memory")


def _store(
    binding: MemoryRootBinding,
    *,
    observed_index_invalidations: list[MemoryOperationKind] | None = None,
) -> CanonicalMemoryStore:
    store: CanonicalMemoryStore

    def _hook(_event: object) -> None:
        if observed_index_invalidations is None:
            return
        operations = store.read_memory_operations()
        if operations:
            observed_index_invalidations.append(operations[-1].operation_kind)

    store = CanonicalMemoryStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        derived_index_hook=_hook,
    )
    return store


def _index_store(binding: MemoryRootBinding) -> DerivedRetrievalIndexStore:
    return DerivedRetrievalIndexStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _record(*, statement: str, status: str = "active") -> MemoryStoreRecord:
    content: dict[str, object] = {
        "semantic_kind": MemoryRecordKind.SEMANTIC_FACT.value,
        "statement": statement,
        "confidence": "high",
        "status": status,
        "injection_policy": "retrieval_only",
        "tags": ["u-mem-21", "redaction"],
    }
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(
                MemoryTier.SEMANTIC,
                MemoryRecordKind.SEMANTIC_FACT,
                content_hash,
            ),
            schema_version="memory-store-record/v1",
            tier=MemoryTier.SEMANTIC,
            kind=MemoryRecordKind.SEMANTIC_FACT,
            created_at=_NOW,
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:u-mem-21"),),
            scope=MemoryScope(
                project="arhugula-v2",
                workflow="memory-substrate",
                cli_profile="codex",
                visibility=MemoryVisibility.WORKFLOW,
            ),
            content_hash=content_hash,
        ),
        content=content,
    )


def _tool_event_record(*, summary: str) -> MemoryStoreRecord:
    content: dict[str, object] = {
        "run_id": _RUN_ID,
        "tool_event_id": "tool-u-mem-21",
        "tool_name": "memory.redaction",
        "summary": summary,
    }
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(
                MemoryTier.EPISODIC,
                MemoryRecordKind.TOOL_EVENT,
                content_hash,
            ),
            schema_version="memory-store-record/v1",
            tier=MemoryTier.EPISODIC,
            kind=MemoryRecordKind.TOOL_EVENT,
            created_at=_NOW,
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:u-mem-21"),),
            scope=MemoryScope(
                project="arhugula-v2",
                workflow="memory-substrate",
                cli_profile="codex",
                visibility=MemoryVisibility.WORKFLOW,
            ),
            content_hash=content_hash,
        ),
        content=content,
    )


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class _FakeSpanExporter:
    def __init__(self) -> None:
        self._spans: list[_FakeSpan] = []

    def append(self, span: _FakeSpan) -> None:
        self._spans.append(span)

    def get_finished_spans(self) -> tuple[_FakeSpan, ...]:
        return tuple(self._spans)


class _FakeSpanContext:
    def __init__(self, exporter: _FakeSpanExporter, name: str) -> None:
        self._exporter = exporter
        self._span = _FakeSpan(name)

    def __enter__(self) -> _FakeSpan:
        return self._span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._exporter.append(self._span)


class _FakeTracer:
    def __init__(self, exporter: _FakeSpanExporter) -> None:
        self._exporter = exporter

    def start_as_current_span(self, name: str) -> _FakeSpanContext:
        return _FakeSpanContext(self._exporter, name)


class _FakeTracerProvider:
    def __init__(self, exporter: _FakeSpanExporter) -> None:
        self._exporter = exporter

    def get_tracer(self, name: str) -> _FakeTracer:
        _ = name
        return _FakeTracer(self._exporter)


def _tracer_provider() -> tuple[_FakeTracerProvider, _FakeSpanExporter]:
    exporter = _FakeSpanExporter()
    return _FakeTracerProvider(exporter), exporter


def _service(
    store: CanonicalMemoryStore,
    *,
    tracer_provider: object | None = None,
) -> MemoryRedactionService:
    return MemoryRedactionService(
        store=store,
        operation_actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        event_actor=MemoryRedactionActor.OPERATOR,
        policy_ref=_POLICY_REF,
        provider="openai",
        model="gpt-5",
        cli_profile="codex",
        tracer_provider=tracer_provider,
    )


def test_content_redaction_writes_event_before_physical_replacement(
    tmp_path: Path,
) -> None:
    observed_invalidations: list[MemoryOperationKind] = []
    binding = _binding(tmp_path)
    store = _store(binding, observed_index_invalidations=observed_invalidations)
    record = _record(statement="Sensitive API token sk-live-redacted must be removed.")
    store.write_record(record)
    observed_invalidations.clear()

    result = _service(store).redact_content(
        record.envelope.memory_id,
        record.envelope.kind,
        timestamp=_NOW,
        reason="operator requested physical secret removal",
        replacement_summary="Sensitive credential removed.",
    )

    assert observed_invalidations == [MemoryOperationKind.REDACT]
    assert result.event.redaction_kind is MemoryRedactionKind.CONTENT_REDACTION
    assert result.event.target_memory_id == record.envelope.memory_id
    assert result.event.old_content_hash == record.envelope.content_hash.hex()
    audit_record = store.read_record(
        record.envelope.memory_id,
        record.envelope.kind,
        audit_mode=True,
    )
    assert audit_record.envelope.memory_id == record.envelope.memory_id
    assert audit_record.envelope.redaction_state is RedactionState.REDACTED
    assert result.event.new_content_hash == audit_record.envelope.content_hash.hex()
    assert result.event.new_content_hash != result.event.old_content_hash
    stored_payload = store.record_path(audit_record).read_text()
    assert "sk-live-redacted" not in stored_payload
    assert "Sensitive credential removed." in stored_payload

    with pytest.raises(MemoryStoreRecordUnavailableError, match="redacted"):
        store.read_record(record.envelope.memory_id, record.envelope.kind)

    [operation] = store.read_memory_operations()
    assert operation.operation_kind is MemoryOperationKind.REDACT
    assert operation.memory_refs == (record.envelope.memory_id,)
    assert operation.redaction_event == result.event


def test_redaction_emits_c_mem_19_span(tmp_path: Path) -> None:
    tracer_provider, exporter = _tracer_provider()
    binding = _binding(tmp_path)
    store = _store(binding)
    record = _record(statement="Sensitive telemetry target should be redacted.")
    store.write_record(record)

    result = _service(store, tracer_provider=tracer_provider).redact_content(
        record.envelope.memory_id,
        record.envelope.kind,
        timestamp=_NOW,
        reason="operator requested telemetry redaction",
        replacement_summary="Sensitive telemetry target removed.",
    )

    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "redaction"
    assert attrs["memory.operation.kind"] == MemoryOperationKind.REDACT.value
    assert attrs["memory.tier"] == record.envelope.tier.value
    assert attrs["memory.provider"] == "openai"
    assert attrs["memory.model"] == "gpt-5"
    assert attrs["memory.cli_profile"] == "codex"
    assert attrs["memory.policy.decision"] == result.event.redaction_kind.value
    assert attrs["memory.record_count"] == 1


def test_tombstone_transition_remains_audit_visible_and_retrieval_excluded(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    record = _record(statement="A superseded memory should remain tombstone-visible.")
    store.write_record(record)

    result = _service(store).tombstone(
        record.envelope.memory_id,
        record.envelope.kind,
        timestamp=_NOW,
        reason="operator deleted obsolete memory",
    )

    assert result.event.redaction_kind is MemoryRedactionKind.TOMBSTONE
    audit_record = store.read_record(
        record.envelope.memory_id,
        record.envelope.kind,
        audit_mode=True,
    )
    assert audit_record.envelope.redaction_state is RedactionState.TOMBSTONED
    assert audit_record.content["status"] == "tombstoned"
    [operation] = store.read_memory_operations()
    assert operation.operation_kind is MemoryOperationKind.TOMBSTONE
    assert operation.redaction_event == result.event

    index_store = _index_store(binding)
    rebuilt = index_store.rebuild(indexed_at=_NOW)
    assert rebuilt.entries[0].redaction_state is RedactionState.TOMBSTONED
    retrieved = index_store.retrieve(
        DerivedRetrievalIndexQuery(query_summary="superseded tombstone-visible")
    )
    assert retrieved.selected_refs == ()


def test_jsonl_redaction_appends_and_reads_latest_episodic_state(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    record = _tool_event_record(summary="Tool event carried sensitive value secret-123.")
    store.write_record(record)

    result = _service(store).redact_content(
        record.envelope.memory_id,
        record.envelope.kind,
        timestamp=_NOW,
        reason="operator requested episodic tool-event removal",
        replacement_summary="Sensitive tool event content removed.",
        run_id=_RUN_ID,
    )

    with pytest.raises(MemoryStoreRecordUnavailableError, match="redacted"):
        store.read_record(record.envelope.memory_id, record.envelope.kind, run_id=_RUN_ID)
    audit_record = store.read_record(
        record.envelope.memory_id,
        record.envelope.kind,
        run_id=_RUN_ID,
        audit_mode=True,
    )
    assert audit_record.envelope.redaction_state is RedactionState.REDACTED
    assert audit_record.content["run_id"] == _RUN_ID
    assert audit_record.content["replacement_summary"] == "Sensitive tool event content removed."
    assert audit_record.envelope.content_hash.hex() == result.event.new_content_hash
    assert len(store.record_path(audit_record).read_text().splitlines()) == 2


def test_retention_expiry_is_ledgered_before_derived_index_removal(
    tmp_path: Path,
) -> None:
    observed_invalidations: list[MemoryOperationKind] = []
    binding = _binding(tmp_path)
    store = _store(binding, observed_index_invalidations=observed_invalidations)
    record = _record(statement="Expired retention content must leave retrieval indexes.")
    store.write_record(record)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)
    observed_invalidations.clear()

    result = _service(store).expire_for_retention(
        record.envelope.memory_id,
        record.envelope.kind,
        timestamp=_NOW,
        reason="policy retention horizon elapsed",
        replacement_summary="Expired by retention policy.",
    )

    assert observed_invalidations == [MemoryOperationKind.REDACT]
    assert result.event.redaction_kind is MemoryRedactionKind.RETENTION_EXPIRY
    [operation] = store.read_memory_operations()
    assert operation.redaction_event == result.event
    audit_record = store.read_record(
        record.envelope.memory_id,
        record.envelope.kind,
        audit_mode=True,
    )
    assert audit_record.content["status"] == "expired"

    index_store.rebuild(indexed_at=_NOW)
    retrieved = index_store.retrieve(
        DerivedRetrievalIndexQuery(query_summary="expired retention content")
    )
    assert retrieved.selected_refs == ()
