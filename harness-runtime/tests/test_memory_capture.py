"""Tests for U-MEM-07 - automatic episodic capture API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import (
    MemoryOperationEngineClass,
    MemoryOperationKind,
    MemoryOperationProjection,
    MemoryOperationWriteResult,
)
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_record_envelope import MemoryRecordKind, MemoryTier
from harness_is.memory_store import CanonicalMemoryStore
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.memory_capture import (
    EpisodicMemoryCapture,
    MemoryCaptureMode,
    MemoryCaptureStatus,
    SummaryProvenance,
    SummarySource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_BASE_TIME = datetime(2026, 7, 1, 14, 0, 0, tzinfo=UTC)
_RUN_ID = "run-u-mem-07"
_POLICY_REF = "memory-policy:v1"
_SNAPSHOT_REF = "procedural-snapshot:abc123"
_ENGINE_CLASS = MemoryOperationEngineClass.PURE_PATTERN_NO_ENGINE


def _store(tmp_path: Path) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _recorder(tmp_path: Path) -> tuple[CanonicalMemoryStore, EpisodicMemoryCapture]:
    store = _store(tmp_path)
    return store, EpisodicMemoryCapture(
        store=store,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        project="arhugula-v2",
    )


def _tracer_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _model_summary() -> SummaryProvenance:
    return SummaryProvenance(source=SummarySource.MODEL_GENERATED, model="gpt-5")


def _common_kwargs(offset: int = 0) -> dict[str, object]:
    return {
        "timestamp": _BASE_TIME + timedelta(minutes=offset),
        "provider": "openai",
        "model": "gpt-5",
        "cli_profile": "codex",
        "engine_class": _ENGINE_CLASS,
        "policy_ref": _POLICY_REF,
        "procedural_snapshot_ref": _SNAPSHOT_REF,
    }


def _semantic_files(tmp_path: Path) -> list[str]:
    semantic_root = tmp_path / "memory" / "semantic"
    if not semantic_root.exists():
        return []
    return sorted(
        path.relative_to(semantic_root).as_posix()
        for path in semantic_root.rglob("*")
        if path.is_file()
    )


def test_supported_events_write_episodic_records_and_capture_operations(
    tmp_path: Path,
) -> None:
    """U-MEM-07 acceptance - each supported event captures a record and op."""
    store, recorder = _recorder(tmp_path)
    summary = _model_summary()

    results = [
        recorder.capture_run_start(
            run_id=_RUN_ID,
            workflow_id="workflow-memory",
            thread_id="thread-1",
            provider_route=("openai:gpt-5",),
            **_common_kwargs(0),
        ),
        recorder.capture_turn_completion(
            run_id=_RUN_ID,
            turn_id="turn-1",
            step_id="step-1",
            prompt_summary="operator asked to continue the memory substrate loop",
            response_summary="runtime capture API recorded the turn",
            summary=summary,
            tool_event_refs=(),
            failure_observations=(),
            promotion_candidates=(),
            token_usage={"input_tokens": 12, "output_tokens": 34},
            **_common_kwargs(1),
        ),
        recorder.capture_tool_event(
            run_id=_RUN_ID,
            tool_event_id="tool-1",
            tool_name="pytest",
            summary_text="focused memory capture tests ran",
            summary=summary,
            step_id="step-1",
            **_common_kwargs(2),
        ),
        recorder.capture_provider_route(
            run_id=_RUN_ID,
            route_id="route-1",
            provider_route=("openai:gpt-5", "anthropic:claude-sonnet"),
            step_id="step-1",
            **_common_kwargs(3),
        ),
        recorder.capture_failure_observation(
            run_id=_RUN_ID,
            turn_id="turn-2",
            step_id="step-2",
            observation="first write failed and was surfaced",
            summary=summary,
            **_common_kwargs(4),
        ),
        recorder.capture_compaction_event(
            run_id=_RUN_ID,
            compaction_id="compact-1",
            summary_text="context compacted with U-MEM-07 state",
            summary=summary,
            input_memory_refs=(),
            **_common_kwargs(5),
        ),
        recorder.capture_run_close(
            run_id=_RUN_ID,
            workflow_id="workflow-memory",
            thread_id="thread-1",
            provider_route=("openai:gpt-5",),
            close_status="completed",
            started_at=_BASE_TIME,
            **_common_kwargs(6),
        ),
    ]

    assert [result.status for result in results] == [MemoryCaptureStatus.CAPTURED] * 7
    for result in results:
        assert result.memory_id is not None
        assert result.operation_action_id is not None
        assert result.record_kind in {
            MemoryRecordKind.EPISODIC_RUN,
            MemoryRecordKind.EPISODIC_TURN,
            MemoryRecordKind.TOOL_EVENT,
            MemoryRecordKind.COMPACTION_EVENT,
        }
        record = store.read_record(result.memory_id, result.record_kind, run_id=_RUN_ID)
        assert record.envelope.memory_id == result.memory_id
        assert record.envelope.tier is MemoryTier.EPISODIC

    run_record_ids = [
        result.memory_id
        for result in results
        if result.record_kind is MemoryRecordKind.EPISODIC_RUN
    ]
    assert len(set(run_record_ids)) == 1

    operations = store.read_memory_operations()
    assert len(operations) == len(results)
    assert {entry.operation_kind for entry in operations} == {MemoryOperationKind.CAPTURE}
    assert {entry.operation_projection for entry in operations} == {MemoryOperationProjection.NONE}
    assert [entry.memory_refs for entry in operations] == [
        (result.memory_id,) for result in results
    ]
    assert {entry.policy_ref for entry in operations} == {_POLICY_REF}
    assert {entry.procedural_snapshot_ref for entry in operations} == {_SNAPSHOT_REF}
    assert _semantic_files(tmp_path) == ["index.jsonl"]


def test_capture_emits_c_mem_19_observability_span(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tracer_provider, exporter = _tracer_provider()
    recorder = EpisodicMemoryCapture(
        store=store,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        project="arhugula-v2",
        tracer_provider=tracer_provider,
    )

    result = recorder.capture_run_start(
        run_id=_RUN_ID,
        workflow_id="workflow-memory",
        thread_id="thread-1",
        provider_route=("openai:gpt-5",),
        **_common_kwargs(0),
    )

    assert result.status is MemoryCaptureStatus.CAPTURED
    [span] = [span for span in exporter.get_finished_spans() if span.name == "memory.operation"]
    attrs = dict(span.attributes or {})
    assert attrs["memory.operation.name"] == "capture"
    assert attrs["memory.operation.kind"] == MemoryOperationKind.CAPTURE.value
    assert attrs["memory.tier"] == MemoryTier.EPISODIC.value
    assert attrs["memory.provider"] == "openai"
    assert attrs["memory.model"] == "gpt-5"
    assert attrs["memory.cli_profile"] == "codex"
    assert attrs["memory.policy.decision"] == "captured"
    assert attrs["memory.record_count"] == 1


def test_stored_summaries_carry_provenance_and_hash(tmp_path: Path) -> None:
    store, recorder = _recorder(tmp_path)
    summary = _model_summary()

    result = recorder.capture_turn_completion(
        run_id=_RUN_ID,
        turn_id="turn-summary",
        step_id="step-summary",
        prompt_summary="summarized prompt",
        response_summary="summarized response",
        summary=summary,
        tool_event_refs=(),
        failure_observations=(),
        promotion_candidates=(),
        token_usage=None,
        **_common_kwargs(),
    )

    assert result.status is MemoryCaptureStatus.CAPTURED
    assert result.memory_id is not None
    record = store.read_record(result.memory_id, MemoryRecordKind.EPISODIC_TURN, run_id=_RUN_ID)
    assert record.content["summary_source"] == "model_generated"
    assert record.content["summary_model"] == "gpt-5"
    assert (
        record.content["summary_hash"]
        == hashlib.sha256(b"summarized prompt\nsummarized response").hexdigest()
    )


def test_redacted_capture_mode_persists_minimal_content(tmp_path: Path) -> None:
    store, recorder = _recorder(tmp_path)

    result = recorder.capture_turn_completion(
        run_id=_RUN_ID,
        turn_id="turn-redacted",
        step_id="step-redacted",
        prompt_summary="private prompt account 123",
        response_summary="private answer account 456",
        summary=SummaryProvenance(source=SummarySource.OPERATOR),
        capture_mode=MemoryCaptureMode.REDACTED,
        tool_event_refs=(),
        failure_observations=(),
        promotion_candidates=(),
        token_usage=None,
        **_common_kwargs(),
    )

    assert result.status is MemoryCaptureStatus.CAPTURED
    assert result.memory_id is not None
    record = store.read_record(result.memory_id, MemoryRecordKind.EPISODIC_TURN, run_id=_RUN_ID)
    serialized = json.dumps(record.content, sort_keys=True)
    assert record.content["capture_mode"] == "redacted"
    assert record.content["prompt_summary"] == "[redacted]"
    assert record.content["response_summary"] == "[redacted]"
    assert "account 123" not in serialized
    assert "account 456" not in serialized


def test_capture_does_not_write_record_when_operation_ledger_append_fails() -> None:
    class _LedgerFailingStore:
        records_written = 0

        def append_memory_operation(self, payload: object) -> object:
            raise OSError("ledger offline")

        def write_record(self, record: object) -> object:
            self.records_written += 1
            raise AssertionError("record write must not run before ledger append")

    store = _LedgerFailingStore()
    recorder = EpisodicMemoryCapture(
        store=store,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        project="arhugula-v2",
    )

    result = recorder.capture_run_start(
        run_id=_RUN_ID,
        workflow_id="workflow-memory",
        thread_id="thread-1",
        provider_route=("openai:gpt-5",),
        **_common_kwargs(),
    )

    assert result.status is MemoryCaptureStatus.FAILED
    assert result.memory_id is None
    assert result.operation_action_id is None
    assert result.failure_reason == "OSError: ledger offline"
    assert store.records_written == 0


def test_capture_record_write_failure_after_ledger_append_is_observable() -> None:
    class _FailingStore:
        appended = False

        def append_memory_operation(self, payload: object) -> object:
            self.appended = True
            return MemoryOperationWriteResult.APPENDED

        def write_record(self, record: object) -> object:
            assert self.appended is True
            raise OSError("disk full")

    store = _FailingStore()
    recorder = EpisodicMemoryCapture(
        store=store,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        project="arhugula-v2",
    )

    result = recorder.capture_run_start(
        run_id=_RUN_ID,
        workflow_id="workflow-memory",
        thread_id="thread-1",
        provider_route=("openai:gpt-5",),
        **_common_kwargs(),
    )

    assert result.status is MemoryCaptureStatus.FAILED
    assert result.memory_id is None
    assert result.operation_action_id is None
    assert result.failure_reason == "OSError: disk full"
    assert store.appended is True


def test_module_public_api_is_importable() -> None:
    import harness_runtime
    import harness_runtime.memory_capture as m

    assert harness_runtime.EpisodicMemoryCapture is EpisodicMemoryCapture
    assert m.EpisodicMemoryCapture is EpisodicMemoryCapture
    assert m.MemoryCaptureStatus is MemoryCaptureStatus
