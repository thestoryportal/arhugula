"""Tests for U-MEM-17 - Anthropic native memory adapter over canonical store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
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
from harness_is.memory_record_envelope import MemoryRecordKind, MemoryScope, MemoryVisibility
from harness_is.memory_store import CanonicalMemoryStore
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
    MemoryToolStorageBackendProtocol,
)
from harness_runtime.lifecycle.native_memory_adapter import CanonicalNativeMemoryToolBackend
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_NOW = datetime(2026, 7, 2, 20, 30, 0, tzinfo=UTC)
_POLICY_REF = "policy:u-mem-17"


def _scope() -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        workload_class="coding-arc",
        provider_family="anthropic",
        cli_profile="claude",
        visibility=MemoryVisibility.WORKFLOW,
    )


def _policy(**overrides: object) -> MemoryPolicyDocument:
    fields: dict[str, object] = {
        "policy_id": _POLICY_REF,
        "enabled": True,
        "capture_decision": CaptureDecision.CAPTURE_FULL,
        "promotion_decision": PromotionDecision.KEEP_EPISODIC,
        "retrieval_access": AccessDecision.RETRIEVAL_ONLY,
        "native_memory_access": AccessDecision.NATIVE_PROVIDER,
        "review_mode": ReviewMode.OPERATOR_REQUIRED,
    }
    fields.update(overrides)
    return MemoryPolicyDocument(**fields)


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


def _backend(
    tmp_path: Path,
    *,
    store: CanonicalMemoryStore | None = None,
    policy: MemoryPolicyDocument | None = None,
    tracer_provider: TracerProvider | None = None,
) -> tuple[CanonicalMemoryStore, CanonicalNativeMemoryToolBackend]:
    canonical_store = store or _store(tmp_path)
    return canonical_store, CanonicalNativeMemoryToolBackend(
        store=canonical_store,
        policy_resolver=MemoryPolicyResolver(policy or _policy()),
        scope=_scope(),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        run_id="run-u-mem-17",
        step_id="step-native-memory",
        provider="anthropic",
        model="claude-test",
        cli_profile="claude",
        policy_ref=_POLICY_REF,
        clock=lambda: _NOW,
        tracer_provider=tracer_provider,
    )


@pytest.mark.asyncio
async def test_canonical_native_backend_preserves_callback_semantics_and_ledgers(
    tmp_path: Path,
) -> None:
    store, backend = _backend(tmp_path)
    assert isinstance(backend, MemoryToolStorageBackendProtocol)

    await backend.create("/memories/project/notes.txt", b"hello world\n")
    assert await backend.view("/memories/project/notes.txt") == b"hello world\n"
    await backend.str_replace("/memories/project/notes.txt", "world", "canonical")
    await backend.insert("/memories/project/notes.txt", 2, "inserted\n")
    assert await backend.view("/memories/project/notes.txt") == b"hello canonical\ninserted\n"
    await backend.delete("/memories/project/notes.txt")
    with pytest.raises(MemoryCallbackIOError, match="not found"):
        await backend.view("/memories/project/notes.txt")

    operations = store.read_memory_operations()
    assert {entry.operation_kind for entry in operations} == {
        MemoryOperationKind.NATIVE_ADAPTER_CALL
    }
    assert len(operations) >= 5
    written_refs = [ref for entry in operations for ref in entry.memory_refs]
    assert written_refs
    for memory_ref in written_refs:
        record = store.read_record(
            memory_ref,
            MemoryRecordKind.TOOL_EVENT,
            run_id="run-u-mem-17",
            audit_mode=True,
        )
        assert record.envelope.kind is MemoryRecordKind.TOOL_EVENT
        assert record.content["memory_path"] == "/memories/project/notes.txt"


@pytest.mark.asyncio
async def test_native_writes_stay_episodic_and_do_not_silently_promote(
    tmp_path: Path,
) -> None:
    store, backend = _backend(tmp_path)

    await backend.create("/memories/preference.md", b"Always promote this? no.\n")

    [operation] = store.read_memory_operations()
    [memory_ref] = operation.memory_refs
    record = store.read_record(
        memory_ref,
        MemoryRecordKind.TOOL_EVENT,
        run_id="run-u-mem-17",
    )
    assert record.envelope.kind is MemoryRecordKind.TOOL_EVENT
    assert record.content["command"] == "create"
    with pytest.raises(Exception):
        store.read_record(memory_ref, MemoryRecordKind.PREFERENCE)


@pytest.mark.asyncio
async def test_native_adapter_emits_c_mem_19_success_span(tmp_path: Path) -> None:
    tracer_provider, exporter = _tracer_provider()
    _, backend = _backend(tmp_path, tracer_provider=tracer_provider)

    await backend.create("/memories/observability.txt", b"native adapter telemetry")

    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "native_adapter_call"
    assert attrs["memory.operation.kind"] == MemoryOperationKind.NATIVE_ADAPTER_CALL.value
    assert attrs["memory.path"] == "/memories/observability.txt"
    assert attrs["memory.provider"] == "anthropic"
    assert attrs["memory.model"] == "claude-test"
    assert attrs["memory.cli_profile"] == "claude"
    assert attrs["memory.policy.decision"] == "allowed"
    assert attrs["memory.record_count"] == 1


@pytest.mark.asyncio
async def test_native_adapter_path_violation_emits_failure_class(tmp_path: Path) -> None:
    tracer_provider, exporter = _tracer_provider()
    _, backend = _backend(tmp_path, tracer_provider=tracer_provider)

    with pytest.raises(MemoryPathViolationError):
        await backend.create("/memories/../outside.txt", b"secret")

    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "native_adapter_call"
    assert attrs["memory.failure_class"] == "path_violation"
    assert attrs["memory.path"] == "/memories/../outside.txt"


@pytest.mark.asyncio
async def test_native_adapter_ledgers_repeated_identical_reads(
    tmp_path: Path,
) -> None:
    store, backend = _backend(tmp_path)

    await backend.create("/memories/repeated.txt", b"same content\n")
    assert await backend.view("/memories/repeated.txt") == b"same content\n"
    assert await backend.view("/memories/repeated.txt") == b"same content\n"

    operations = store.read_memory_operations()
    assert [entry.operation_kind for entry in operations] == [
        MemoryOperationKind.NATIVE_ADAPTER_CALL,
        MemoryOperationKind.NATIVE_ADAPTER_CALL,
        MemoryOperationKind.NATIVE_ADAPTER_CALL,
    ]


@pytest.mark.asyncio
async def test_native_adapter_policy_denies_capture_without_writing(
    tmp_path: Path,
) -> None:
    store, backend = _backend(
        tmp_path,
        policy=_policy(capture_decision=CaptureDecision.DENY),
    )

    with pytest.raises(MemoryCallbackIOError, match="capture policy denies"):
        await backend.create("/memories/denied.txt", b"secret")

    assert store.read_memory_operations() == []


@pytest.mark.asyncio
async def test_native_adapter_policy_denies_native_access(
    tmp_path: Path,
) -> None:
    tracer_provider, exporter = _tracer_provider()
    store, backend = _backend(
        tmp_path,
        policy=_policy(native_memory_access=AccessDecision.DENY),
        tracer_provider=tracer_provider,
    )

    with pytest.raises(MemoryCallbackIOError, match="native memory policy denies"):
        await backend.create("/memories/denied.txt", b"secret")

    assert store.read_memory_operations() == []
    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "native_adapter_call"
    assert attrs["memory.failure_class"] == "policy_denial"
    assert attrs["memory.path"] == "/memories/denied.txt"


@pytest.mark.asyncio
async def test_native_adapter_policy_denies_retrieval_after_write(
    tmp_path: Path,
) -> None:
    store, backend = _backend(
        tmp_path,
        policy=_policy(retrieval_access=AccessDecision.DENY),
    )

    await backend.create("/memories/hidden.txt", b"captured but not readable")

    with pytest.raises(MemoryCallbackIOError, match="retrieval policy denies"):
        await backend.view("/memories/hidden.txt")

    operations = store.read_memory_operations()
    assert [entry.operation_kind for entry in operations] == [
        MemoryOperationKind.NATIVE_ADAPTER_CALL
    ]


@pytest.mark.asyncio
async def test_native_adapter_reuses_memories_path_validation_before_writes(
    tmp_path: Path,
) -> None:
    store, backend = _backend(tmp_path)

    with pytest.raises(MemoryPathViolationError):
        await backend.create("/memories/../outside.txt", b"secret")

    assert store.read_memory_operations() == []
