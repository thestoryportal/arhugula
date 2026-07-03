from __future__ import annotations

import pytest
from harness_cp.engine_class import EngineClass
from harness_is.memory_operation_ledger import MemoryOperationEngineClass
from harness_runtime import (
    MemoryDurabilityCarrier,
    MemoryDurabilityStrategy,
    MemoryDurabilityViolationError,
    memory_durability_binding_for,
    memory_durability_bindings,
    memory_operation_engine_class_for,
    validate_memory_durability,
)
from pydantic import ValidationError

_HASH = "a" * 64


def test_bindings_cover_closed_engine_taxonomy_and_ledger_enum() -> None:
    assert {engine.value for engine in EngineClass} == {
        "event-sourced-replay",
        "save-point-checkpoint",
        "pure-pattern-no-engine",
        "reconciler-loop",
        "WAL-segment",
    }
    assert {engine.value for engine in MemoryOperationEngineClass} == {
        engine.value for engine in EngineClass
    }

    bindings = memory_durability_bindings()

    assert {binding.engine_class for binding in bindings} == set(EngineClass)
    assert {binding.operation_engine_class for binding in bindings} == set(
        MemoryOperationEngineClass
    )
    for engine_class in EngineClass:
        assert memory_operation_engine_class_for(engine_class).value == engine_class.value


def test_each_engine_class_has_c_mem_17_durability_strategy() -> None:
    expected = {
        EngineClass.EVENT_SOURCED_REPLAY: MemoryDurabilityStrategy.ACTIVITY_SNAPSHOT,
        EngineClass.SAVE_POINT_CHECKPOINT: MemoryDurabilityStrategy.CHECKPOINT_PACKET,
        EngineClass.PURE_PATTERN_NO_ENGINE: MemoryDurabilityStrategy.STATE_LEDGER_WRITE,
        EngineClass.RECONCILER_LOOP: MemoryDurabilityStrategy.RECONCILER_OBSERVED_VERSION,
        EngineClass.WAL_SEGMENT: MemoryDurabilityStrategy.WAL_REBUILD_PREWARM,
    }

    for engine_class, strategy in expected.items():
        binding = memory_durability_binding_for(engine_class)
        assert binding.strategy is strategy
        assert binding.operation_engine_class.value == engine_class.value


def test_replay_requires_activity_boundary_and_stabilized_retrieval_carrier() -> None:
    with pytest.raises(MemoryDurabilityViolationError, match="activity boundary"):
        validate_memory_durability(
            EngineClass.EVENT_SOURCED_REPLAY,
            MemoryDurabilityCarrier(packet_hash=_HASH),
        )

    with pytest.raises(MemoryDurabilityViolationError, match="store version or packet hash"):
        validate_memory_durability(
            EngineClass.EVENT_SOURCED_REPLAY,
            MemoryDurabilityCarrier(activity_boundary_ref="activity:retrieve"),
        )

    decision = validate_memory_durability(
        EngineClass.EVENT_SOURCED_REPLAY,
        MemoryDurabilityCarrier(
            activity_boundary_ref="activity:retrieve",
            packet_hash=_HASH,
            snapshot_ref="snapshot:memory-packet",
        ),
    )

    assert decision.snapshot_ref == "snapshot:memory-packet"
    assert decision.strategy is MemoryDurabilityStrategy.ACTIVITY_SNAPSHOT
    assert decision.pending_writes_active is False


def test_packet_hash_carriers_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError, match="packet_hash must be 64-character hex"):
        MemoryDurabilityCarrier(packet_hash="not-a-hash")


def test_checkpoint_requires_memory_store_version_and_packet_hash() -> None:
    with pytest.raises(MemoryDurabilityViolationError, match="memory store version"):
        validate_memory_durability(
            EngineClass.SAVE_POINT_CHECKPOINT,
            MemoryDurabilityCarrier(packet_hash=_HASH),
        )

    with pytest.raises(MemoryDurabilityViolationError, match="packet hash"):
        validate_memory_durability(
            EngineClass.SAVE_POINT_CHECKPOINT,
            MemoryDurabilityCarrier(store_version="mem-store:v4"),
        )

    decision = validate_memory_durability(
        EngineClass.SAVE_POINT_CHECKPOINT,
        MemoryDurabilityCarrier(
            store_version="mem-store:v4",
            packet_hash=_HASH,
        ),
    )

    assert decision.store_version == "mem-store:v4"
    assert decision.packet_hash == _HASH


def test_pure_pattern_requires_state_ledger_idempotency_key() -> None:
    with pytest.raises(MemoryDurabilityViolationError, match="state-ledger idempotency key"):
        validate_memory_durability(
            EngineClass.PURE_PATTERN_NO_ENGINE,
            MemoryDurabilityCarrier(),
        )

    decision = validate_memory_durability(
        EngineClass.PURE_PATTERN_NO_ENGINE,
        MemoryDurabilityCarrier(state_ledger_idempotency_key="idem:memory-write"),
    )

    assert decision.strategy is MemoryDurabilityStrategy.STATE_LEDGER_WRITE


def test_reconciler_requires_observed_version() -> None:
    with pytest.raises(MemoryDurabilityViolationError, match="observed version"):
        validate_memory_durability(
            EngineClass.RECONCILER_LOOP,
            MemoryDurabilityCarrier(),
        )

    decision = validate_memory_durability(
        EngineClass.RECONCILER_LOOP,
        MemoryDurabilityCarrier(observed_version="resourceVersion:12"),
    )

    assert decision.strategy is MemoryDurabilityStrategy.RECONCILER_OBSERVED_VERSION


def test_wal_requires_segment_and_prewarm_plan() -> None:
    with pytest.raises(MemoryDurabilityViolationError, match="WAL segment"):
        validate_memory_durability(
            EngineClass.WAL_SEGMENT,
            MemoryDurabilityCarrier(prewarm_plan_ref="prewarm:memory"),
        )

    with pytest.raises(MemoryDurabilityViolationError, match="prewarm"):
        validate_memory_durability(
            EngineClass.WAL_SEGMENT,
            MemoryDurabilityCarrier(wal_segment_ref="wal:segment:3"),
        )

    decision = validate_memory_durability(
        EngineClass.WAL_SEGMENT,
        MemoryDurabilityCarrier(
            wal_segment_ref="wal:segment:3",
            prewarm_plan_ref="prewarm:memory",
        ),
    )

    assert decision.strategy is MemoryDurabilityStrategy.WAL_REBUILD_PREWARM


def test_pending_writes_are_not_active_semantic_memory_before_commit_boundary() -> None:
    pending = validate_memory_durability(
        EngineClass.SAVE_POINT_CHECKPOINT,
        MemoryDurabilityCarrier(
            store_version="mem-store:v4",
            packet_hash=_HASH,
            pending_write_refs=("mem-write:pending",),
        ),
    )

    committed = validate_memory_durability(
        EngineClass.SAVE_POINT_CHECKPOINT,
        MemoryDurabilityCarrier(
            store_version="mem-store:v4",
            packet_hash=_HASH,
            pending_write_refs=("mem-write:pending",),
            commit_boundary_ref="commit:42",
        ),
    )

    assert pending.pending_write_refs == ("mem-write:pending",)
    assert pending.pending_writes_active is False
    assert committed.pending_writes_active is True
