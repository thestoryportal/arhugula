"""Tests for U-MEM-03 - durable memory operation ledger (C-MEM-08)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.memory_operation_ledger import (
    MemoryLedgerVerificationStatus,
    MemoryOperationEngineClass,
    MemoryOperationIdempotencyConflictError,
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
    MemoryOperationProjectionHandles,
    MemoryOperationWriteResult,
    append_memory_operation,
    compute_memory_operation_response_hash,
    read_memory_operation_ledger,
    rebuild_memory_operation_projections,
    verify_memory_operation_ledger,
)
from harness_is.memory_record_envelope import MemoryID
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    Identifier,
)

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-memory")
_BASE_TIME = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _ledger_handle(tmp_path: Path) -> JsonlLedgerHandle:
    return JsonlLedgerHandle(
        canonical_path=tmp_path / "memory" / "durable" / "memory_ops.jsonl",
        exists=False,
        entry_count=0,
    )


def _projection_handles(tmp_path: Path) -> MemoryOperationProjectionHandles:
    durable = tmp_path / "memory" / "durable"
    return MemoryOperationProjectionHandles(
        promotion_decisions=JsonlLedgerHandle(
            canonical_path=durable / "promotion_decisions.jsonl",
            exists=False,
            entry_count=0,
        ),
        injection_decisions=JsonlLedgerHandle(
            canonical_path=durable / "injection_decisions.jsonl",
            exists=False,
            entry_count=0,
        ),
        retrieval_events=JsonlLedgerHandle(
            canonical_path=durable / "retrieval_events.jsonl",
            exists=False,
            entry_count=0,
        ),
    )


def _payload(
    i: int,
    *,
    kind: MemoryOperationKind = MemoryOperationKind.CAPTURE,
    idempotency_key: str | None = None,
    provider: str | None = "openai",
    timestamp: datetime | None = None,
) -> MemoryOperationPayload:
    return MemoryOperationPayload(
        action_id=Identifier(f"mem-op-{i}"),
        idempotency_key=Identifier(idempotency_key or f"idem-mem-op-{i}"),
        actor=_ACTOR,
        timestamp=timestamp or _BASE_TIME + timedelta(minutes=i),
        operation_kind=kind,
        operation_projection=MemoryOperationProjection.for_operation_kind(kind),
        run_id="run-123",
        step_id=f"step-{i}",
        provider=provider,
        model="gpt-5",
        cli_profile="codex",
        engine_class=MemoryOperationEngineClass.PURE_PATTERN_NO_ENGINE,
        memory_refs=(MemoryID(f"mem:semantic:semantic_fact:{i:064x}"),),
        policy_ref="policy:v1",
        procedural_snapshot_ref="snapshot:abc123",
    )


def _json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_memory_operation_vocabularies_match_c_mem_08() -> None:
    assert {kind.value for kind in MemoryOperationKind} == {
        "capture",
        "retrieve",
        "inject",
        "promote",
        "propose_promotion",
        "deny_promotion",
        "redact",
        "tombstone",
        "delete_request",
        "native_adapter_call",
        "standard_tool_call",
        "compaction_decision",
    }
    assert {projection.value for projection in MemoryOperationProjection} == {
        "none",
        "promotion_decisions",
        "injection_decisions",
        "retrieval_events",
    }
    assert {engine_class.value for engine_class in MemoryOperationEngineClass} == {
        "event-sourced-replay",
        "save-point-checkpoint",
        "pure-pattern-no-engine",
        "reconciler-loop",
        "WAL-segment",
    }


def test_append_writes_canonical_memory_ops_jsonl_and_chains(tmp_path: Path) -> None:
    handle = _ledger_handle(tmp_path)

    assert append_memory_operation(handle, _payload(0)) is MemoryOperationWriteResult.APPENDED
    assert append_memory_operation(handle, _payload(1)) is MemoryOperationWriteResult.APPENDED

    raw = _json_lines(handle.canonical_path)
    assert [line["action_id"] for line in raw] == ["mem-op-0", "mem-op-1"]
    assert raw[0]["operation_kind"] == "capture"
    assert raw[0]["operation_projection"] == "none"
    assert raw[0]["memory_refs"] == ["mem:semantic:semantic_fact:" + "0" * 64]

    first, second = read_memory_operation_ledger(handle)
    assert first.prior_event_hash == ALL_ZEROS_SENTINEL
    assert second.prior_event_hash == compute_memory_operation_response_hash(first)
    assert verify_memory_operation_ledger(handle).status is MemoryLedgerVerificationStatus.VALID


def test_duplicate_equivalent_idempotency_key_is_safe_noop(tmp_path: Path) -> None:
    handle = _ledger_handle(tmp_path)
    payload = _payload(0)
    retry = payload.model_copy(update={"timestamp": payload.timestamp + timedelta(seconds=30)})

    assert append_memory_operation(handle, payload) is MemoryOperationWriteResult.APPENDED
    assert append_memory_operation(handle, retry) is MemoryOperationWriteResult.IDEMPOTENT_NOOP

    [entry] = read_memory_operation_ledger(handle)
    assert entry.timestamp == payload.timestamp


def test_duplicate_non_equivalent_idempotency_key_fails_loudly(tmp_path: Path) -> None:
    handle = _ledger_handle(tmp_path)
    append_memory_operation(handle, _payload(0, provider="openai"))

    with pytest.raises(MemoryOperationIdempotencyConflictError):
        append_memory_operation(
            handle,
            _payload(99, idempotency_key="idem-mem-op-0", provider="anthropic"),
        )

    assert len(read_memory_operation_ledger(handle)) == 1


def test_hash_chain_verification_detects_memory_sidecar_tampering(tmp_path: Path) -> None:
    handle = _ledger_handle(tmp_path)
    append_memory_operation(handle, _payload(0, provider="openai"))
    append_memory_operation(handle, _payload(1, provider="openai"))

    raw = _json_lines(handle.canonical_path)
    raw[0]["provider"] = "tampered-provider"
    handle.canonical_path.write_text("\n".join(json.dumps(line) for line in raw) + "\n")

    result = verify_memory_operation_ledger(handle)
    assert result.status is MemoryLedgerVerificationStatus.INVALID
    assert result.failure_position == 1


def test_parallel_appends_do_not_fork_canonical_memory_operation_ledger(
    tmp_path: Path,
) -> None:
    handle = _ledger_handle(tmp_path)

    def _write(i: int) -> None:
        append_memory_operation(handle, _payload(i))

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ledger = read_memory_operation_ledger(handle)
    non_inception_priors = {
        entry.prior_event_hash for entry in ledger if entry.prior_event_hash != ALL_ZEROS_SENTINEL
    }
    assert len(ledger) == 12
    assert len(non_inception_priors) == 11
    assert verify_memory_operation_ledger(handle).status is MemoryLedgerVerificationStatus.VALID


def test_projection_rebuilds_are_keyed_by_canonical_action_id(tmp_path: Path) -> None:
    ledger_handle = _ledger_handle(tmp_path)
    handles = _projection_handles(tmp_path)
    append_memory_operation(ledger_handle, _payload(0, kind=MemoryOperationKind.RETRIEVE))
    append_memory_operation(ledger_handle, _payload(1, kind=MemoryOperationKind.INJECT))
    append_memory_operation(ledger_handle, _payload(2, kind=MemoryOperationKind.PROMOTE))
    append_memory_operation(ledger_handle, _payload(3, kind=MemoryOperationKind.CAPTURE))

    counts = rebuild_memory_operation_projections(ledger_handle, handles)

    assert counts == {
        MemoryOperationProjection.PROMOTION_DECISIONS: 1,
        MemoryOperationProjection.INJECTION_DECISIONS: 1,
        MemoryOperationProjection.RETRIEVAL_EVENTS: 1,
    }
    assert _json_lines(handles.retrieval_events.canonical_path) == [
        {
            "action_id": "mem-op-0",
            "idempotency_key": "idem-mem-op-0",
            "operation_kind": "retrieve",
            "operation_projection": "retrieval_events",
        }
    ]
    promotion = _json_lines(handles.promotion_decisions.canonical_path)
    assert promotion[0]["action_id"] == "mem-op-2"
    assert "prior_event_hash" not in promotion[0]
    assert "response_hash" not in promotion[0]


def test_projection_rebuild_replaces_stale_projection_files(tmp_path: Path) -> None:
    ledger_handle = _ledger_handle(tmp_path)
    handles = _projection_handles(tmp_path)
    handles.retrieval_events.canonical_path.parent.mkdir(parents=True)
    handles.retrieval_events.canonical_path.write_text(
        '{"action_id":"stale","prior_event_hash":"not-authoritative"}\n'
    )
    append_memory_operation(ledger_handle, _payload(0, kind=MemoryOperationKind.RETRIEVE))

    rebuild_memory_operation_projections(ledger_handle, handles)

    assert _json_lines(handles.retrieval_events.canonical_path) == [
        {
            "action_id": "mem-op-0",
            "idempotency_key": "idem-mem-op-0",
            "operation_kind": "retrieve",
            "operation_projection": "retrieval_events",
        }
    ]
