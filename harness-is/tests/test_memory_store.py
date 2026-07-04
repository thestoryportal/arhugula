"""Tests for U-MEM-06 - canonical memory store (C-MEM-02..C-MEM-07)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import (
    MemoryLedgerVerificationStatus,
    MemoryOperationEngineClass,
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
    MemoryOperationWriteResult,
    canonicalize_memory_operation,
    verify_memory_operation_entries,
)
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
from harness_is.memory_store import (
    CanonicalMemoryStore,
    MemoryStoreRecord,
    MemoryStoreRecordUnavailableError,
    MemoryStoreWriteResult,
    canonicalize_memory_store_record,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier

_BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
_RUN_ID = "run-123"
_NON_DURABLE_RECORD_KINDS = tuple(
    kind for kind in MemoryRecordKind if kind is not MemoryRecordKind.MEMORY_OPERATION
)


def _store(tmp_path: Path) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _tier_for(kind: MemoryRecordKind) -> MemoryTier:
    if kind in {
        MemoryRecordKind.EPISODIC_RUN,
        MemoryRecordKind.EPISODIC_TURN,
        MemoryRecordKind.TOOL_EVENT,
        MemoryRecordKind.COMPACTION_EVENT,
    }:
        return MemoryTier.EPISODIC
    if kind in {
        MemoryRecordKind.SEMANTIC_FACT,
        MemoryRecordKind.PREFERENCE,
        MemoryRecordKind.DECISION,
        MemoryRecordKind.CONVENTION,
        MemoryRecordKind.FAILURE_LEARNING,
        MemoryRecordKind.RESEARCH,
    }:
        return MemoryTier.SEMANTIC
    if kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT:
        return MemoryTier.PROCEDURAL
    raise AssertionError(f"unmapped test kind {kind}")


def _content_for(kind: MemoryRecordKind) -> Mapping[str, object]:
    semantic_common = {
        "statement": f"{kind.value} statement",
        "rationale": "U-MEM-06 test fixture",
        "evidence": ["operator:memory-substrate-plan"],
        "confidence": "verified",
        "status": "active",
        "injection_policy": "retrieval_only",
        "tags": ["memory", kind.value],
    }
    match kind:
        case MemoryRecordKind.EPISODIC_RUN:
            return {
                "run_id": _RUN_ID,
                "workflow_id": "wf-memory",
                "thread_id": "thread-1",
                "engine_class": "pure-pattern-no-engine",
                "cli_profile": "codex",
                "provider_route": ["codex"],
                "started_at": _BASE_TIME,
                "closed_at": None,
                "close_status": "unknown",
            }
        case MemoryRecordKind.EPISODIC_TURN:
            return {
                "run_id": _RUN_ID,
                "turn_id": "turn-1",
                "step_id": "step-1",
                "prompt_summary": "asked to continue U-MEM-06",
                "response_summary": "added canonical memory store",
                "summary_source": "harness_rule",
                "summary_model": None,
                "summary_hash": b"\x21" * 32,
                "tool_event_refs": [],
                "failure_observations": [],
                "promotion_candidates": [],
                "token_usage": None,
            }
        case MemoryRecordKind.TOOL_EVENT:
            return {
                "run_id": _RUN_ID,
                "tool_event_id": "tool-1",
                "tool_name": "pytest",
                "summary": "focused memory store tests",
            }
        case MemoryRecordKind.COMPACTION_EVENT:
            return {
                "run_id": _RUN_ID,
                "compaction_id": "compact-1",
                "summary": "context compacted with U-MEM-06 state",
            }
        case MemoryRecordKind.SEMANTIC_FACT:
            return {"semantic_kind": "fact", **semantic_common}
        case MemoryRecordKind.PREFERENCE:
            return {
                "semantic_kind": "preference",
                **semantic_common,
                "preference_subject": "tool_use",
                "preference_strength": "strong",
                "source_authority": "operator_direct",
                "confirmation_required": False,
            }
        case MemoryRecordKind.DECISION:
            return {"semantic_kind": "decision", **semantic_common}
        case MemoryRecordKind.CONVENTION:
            return {"semantic_kind": "convention", **semantic_common}
        case MemoryRecordKind.FAILURE_LEARNING:
            return {"semantic_kind": "failure_learning", **semantic_common}
        case MemoryRecordKind.RESEARCH:
            return {"semantic_kind": "research", **semantic_common}
        case MemoryRecordKind.PROCEDURAL_SNAPSHOT:
            return {
                "snapshot_id": "snapshot-1",
                "workflow_id": "wf-memory",
                "cli_profile": "codex",
                "prompt_refs": [],
                "skill_refs": [],
                "routing_manifest_ref": None,
                "instruction_file_refs": [
                    {
                        "path_or_uri": "AGENTS.md",
                        "content_hash": b"\x44" * 32,
                        "kind": "instruction_file",
                    }
                ],
                "memory_policy_ref": {
                    "path_or_uri": ".harness/memory/policy.json",
                    "content_hash": b"\x45" * 32,
                    "kind": "memory_policy",
                },
            }
        case MemoryRecordKind.MEMORY_OPERATION:
            raise AssertionError("durable memory operations use MemoryOperationPayload")
    raise AssertionError(f"unmapped test kind {kind}")


def _record(
    kind: MemoryRecordKind,
    *,
    redaction_state: RedactionState = RedactionState.ACTIVE,
) -> MemoryStoreRecord:
    content = _content_for(kind)
    tier = _tier_for(kind)
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(tier, kind, content_hash),
            schema_version="memory-store-record/v1",
            tier=tier,
            kind=kind,
            created_at=_BASE_TIME,
            source_refs=(
                SourceRef(
                    ref_type=SourceRefType.OPERATOR,
                    ref="operator:approved-u-mem-06",
                    content_hash=None,
                ),
            ),
            scope=MemoryScope(project="arhugula-v2", visibility=MemoryVisibility.PROJECT),
            content_hash=content_hash,
            redaction_state=redaction_state,
        ),
        content=content,
    )


def _run_id_for(record: MemoryStoreRecord) -> str | None:
    if record.envelope.tier is not MemoryTier.EPISODIC:
        return None
    run_id = record.content["run_id"]
    assert isinstance(run_id, str)
    return run_id


def _jsonl_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert isinstance(record, dict)
        records.append(cast("dict[str, object]", record))
    return records


def _json_record(path: Path) -> dict[str, object]:
    record = json.loads(path.read_text())
    assert isinstance(record, dict)
    return cast("dict[str, object]", record)


def _envelope_memory_id(record: Mapping[str, object]) -> object:
    envelope = record["envelope"]
    assert isinstance(envelope, dict)
    return cast("Mapping[str, object]", envelope)["memory_id"]


def _memory_operation_payload() -> MemoryOperationPayload:
    return MemoryOperationPayload(
        action_id=Identifier("mem-op-1"),
        idempotency_key=Identifier("idem-mem-op-1"),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        timestamp=_BASE_TIME,
        operation_kind=MemoryOperationKind.CAPTURE,
        operation_projection=MemoryOperationProjection.NONE,
        run_id=_RUN_ID,
        step_id="step-1",
        provider="codex",
        model="gpt-5",
        cli_profile="codex",
        engine_class=MemoryOperationEngineClass.PURE_PATTERN_NO_ENGINE,
        memory_refs=(),
        policy_ref="policy:v1",
        procedural_snapshot_ref="snapshot:1",
    )


def test_store_round_trips_envelope_records_byte_stably(tmp_path: Path) -> None:
    """U-MEM-06 acceptance - canonical envelope records read back byte-stably."""
    store = _store(tmp_path)

    for kind in _NON_DURABLE_RECORD_KINDS:
        record = _record(kind)
        before = canonicalize_memory_store_record(record)

        assert store.write_record(record) is MemoryStoreWriteResult.WRITTEN

        restored = store.read_record(
            record.envelope.memory_id,
            kind,
            run_id=_run_id_for(record),
        )
        assert canonicalize_memory_store_record(restored) == before


def test_store_round_trips_durable_memory_operation_byte_stably(tmp_path: Path) -> None:
    """U-MEM-06 acceptance - durable records use the U-MEM-03 ledger format."""
    store = _store(tmp_path)
    payload = _memory_operation_payload()

    assert store.append_memory_operation(payload) is MemoryOperationWriteResult.APPENDED

    [entry] = store.read_memory_operations()
    first_read_bytes = canonicalize_memory_operation(entry)
    [reread_entry] = store.read_memory_operations()
    assert canonicalize_memory_operation(reread_entry) == first_read_bytes
    assert (
        verify_memory_operation_entries(store.read_memory_operations()).status
        is MemoryLedgerVerificationStatus.VALID
    )
    assert entry.action_id == payload.action_id
    assert entry.run_id == _RUN_ID


def test_store_uses_c_mem_02_json_and_jsonl_paths(tmp_path: Path) -> None:
    """C-MEM-02 path classes back every store write."""
    store = _store(tmp_path)
    fact = _record(MemoryRecordKind.SEMANTIC_FACT)
    turn = _record(MemoryRecordKind.EPISODIC_TURN)
    operation = _memory_operation_payload()

    store.write_record(fact)
    store.write_record(turn)
    store.append_memory_operation(operation)

    fact_path = store.record_path(fact)
    assert fact_path.parent == tmp_path / "memory" / "semantic" / "facts"
    assert fact_path.name.endswith(".json")
    assert _envelope_memory_id(_json_record(fact_path)) == fact.envelope.memory_id

    turn_path = store.record_path(turn)
    assert turn_path == tmp_path / "memory" / "episodic" / "runs" / _RUN_ID / "turns.jsonl"
    assert _envelope_memory_id(_jsonl_records(turn_path)[0]) == turn.envelope.memory_id

    operation_path = store.memory_operation_ledger_path()
    assert operation_path == tmp_path / "memory" / "durable" / "memory_ops.jsonl"
    assert _jsonl_records(operation_path)[0]["action_id"] == operation.action_id


def test_store_marks_derived_semantic_index_stale_after_canonical_write(
    tmp_path: Path,
) -> None:
    """U-MEM-06 acceptance - derived indexes are invalidated after writes."""
    store = _store(tmp_path)
    fact = _record(MemoryRecordKind.SEMANTIC_FACT)

    store.write_record(fact)

    [marker] = _jsonl_records(tmp_path / "memory" / "semantic" / "index.jsonl")
    assert marker == {
        "event": "stale",
        "reason": "canonical_write",
        "memory_id": fact.envelope.memory_id,
        "record_kind": "semantic_fact",
        "content_hash": fact.envelope.content_hash.hex(),
    }


@pytest.mark.parametrize(
    ("kind", "redaction_state"),
    (
        (MemoryRecordKind.SEMANTIC_FACT, RedactionState.REDACTED),
        (MemoryRecordKind.DECISION, RedactionState.TOMBSTONED),
    ),
)
def test_redacted_and_tombstoned_records_require_audit_mode(
    tmp_path: Path,
    kind: MemoryRecordKind,
    redaction_state: RedactionState,
) -> None:
    """U-MEM-06 acceptance - inactive records stay inspectable under audit mode."""
    store = _store(tmp_path)
    record = _record(kind, redaction_state=redaction_state)
    store.write_record(record)

    with pytest.raises(MemoryStoreRecordUnavailableError, match=redaction_state.value):
        store.read_record(record.envelope.memory_id, kind)

    audit_record = store.read_record(record.envelope.memory_id, kind, audit_mode=True)
    assert audit_record.envelope.redaction_state is redaction_state


def test_memory_store_package_re_exports() -> None:
    """The package-level IS API exposes the U-MEM-06 store surface."""
    import harness_is
    import harness_is.memory_store as m

    assert harness_is.CanonicalMemoryStore is m.CanonicalMemoryStore
    assert harness_is.MemoryStoreRecord is m.MemoryStoreRecord
    assert harness_is.canonicalize_memory_store_record is m.canonicalize_memory_store_record
