"""Runtime memory durability bindings - U-MEM-19.

Implements the C-MEM-17 binding from the closed CP ``EngineClass`` taxonomy to
the memory-operation ledger's engine-class carrier and the durability evidence
each engine class must provide before retrieved or written memory is considered
stable.
"""

from __future__ import annotations

from enum import StrEnum

from harness_cp.engine_class import EngineClass
from harness_is.memory_operation_ledger import MemoryOperationEngineClass
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryDurabilityStrategy(StrEnum):
    """C-MEM-17 memory durability strategy per engine class."""

    ACTIVITY_SNAPSHOT = "activity_snapshot"
    CHECKPOINT_PACKET = "checkpoint_packet"
    STATE_LEDGER_WRITE = "state_ledger_write"
    RECONCILER_OBSERVED_VERSION = "reconciler_observed_version"
    WAL_REBUILD_PREWARM = "wal_rebuild_prewarm"


class MemoryDurabilityViolationError(ValueError):
    """Raised when an engine-class memory operation lacks required carriers."""


class MemoryDurabilityBinding(BaseModel):
    """Declared C-MEM-17 durability requirements for one engine class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: EngineClass
    operation_engine_class: MemoryOperationEngineClass
    strategy: MemoryDurabilityStrategy
    requires_activity_boundary: bool = False
    requires_store_version_or_packet_hash: bool = False
    requires_store_version: bool = False
    requires_packet_hash: bool = False
    requires_state_ledger_idempotency_key: bool = False
    requires_observed_version: bool = False
    requires_wal_segment_ref: bool = False
    requires_prewarm_plan_ref: bool = False
    stages_pending_writes_until_commit: bool = True


class MemoryDurabilityCarrier(BaseModel):
    """Runtime evidence carried with one memory durability decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    activity_boundary_ref: str | None = None
    snapshot_ref: str | None = None
    store_version: str | None = None
    packet_hash: str | None = None
    state_ledger_idempotency_key: str | None = None
    observed_version: str | None = None
    wal_segment_ref: str | None = None
    prewarm_plan_ref: str | None = None
    pending_write_refs: tuple[str, ...] = Field(default_factory=tuple)
    commit_boundary_ref: str | None = None

    @field_validator(
        "activity_boundary_ref",
        "snapshot_ref",
        "store_version",
        "packet_hash",
        "state_ledger_idempotency_key",
        "observed_version",
        "wal_segment_ref",
        "prewarm_plan_ref",
        "commit_boundary_ref",
    )
    @classmethod
    def _non_empty_optional_string(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("memory durability carrier strings cannot be empty")
        return value

    @field_validator("packet_hash")
    @classmethod
    def _packet_hash_is_sha256_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(char not in _HEX_DIGITS for char in value):
            raise ValueError("packet_hash must be 64-character hex")
        return value


class MemoryDurabilityDecision(BaseModel):
    """Validated memory durability state for runtime use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: EngineClass
    operation_engine_class: MemoryOperationEngineClass
    strategy: MemoryDurabilityStrategy
    activity_boundary_ref: str | None = None
    snapshot_ref: str | None = None
    store_version: str | None = None
    packet_hash: str | None = None
    state_ledger_idempotency_key: str | None = None
    observed_version: str | None = None
    wal_segment_ref: str | None = None
    prewarm_plan_ref: str | None = None
    pending_write_refs: tuple[str, ...] = Field(default_factory=tuple)
    commit_boundary_ref: str | None = None
    pending_writes_active: bool = False


def memory_operation_engine_class_for(
    engine_class: EngineClass,
) -> MemoryOperationEngineClass:
    """Return the memory-operation ledger carrier for a CP engine class."""

    return MemoryOperationEngineClass(engine_class.value)


def memory_durability_bindings() -> tuple[MemoryDurabilityBinding, ...]:
    """Return bindings for the closed five-value engine taxonomy."""

    return tuple(_BINDINGS[engine_class] for engine_class in EngineClass)


def memory_durability_binding_for(
    engine_class: EngineClass,
) -> MemoryDurabilityBinding:
    """Return the C-MEM-17 durability binding for ``engine_class``."""

    return _BINDINGS[engine_class]


def validate_memory_durability(
    engine_class: EngineClass,
    carrier: MemoryDurabilityCarrier,
) -> MemoryDurabilityDecision:
    """Validate C-MEM-17 durability evidence for one engine class."""

    binding = memory_durability_binding_for(engine_class)
    _validate_required_carriers(binding, carrier)
    return MemoryDurabilityDecision(
        engine_class=engine_class,
        operation_engine_class=binding.operation_engine_class,
        strategy=binding.strategy,
        activity_boundary_ref=carrier.activity_boundary_ref,
        snapshot_ref=carrier.snapshot_ref,
        store_version=carrier.store_version,
        packet_hash=carrier.packet_hash,
        state_ledger_idempotency_key=carrier.state_ledger_idempotency_key,
        observed_version=carrier.observed_version,
        wal_segment_ref=carrier.wal_segment_ref,
        prewarm_plan_ref=carrier.prewarm_plan_ref,
        pending_write_refs=carrier.pending_write_refs,
        commit_boundary_ref=carrier.commit_boundary_ref,
        pending_writes_active=bool(carrier.pending_write_refs and carrier.commit_boundary_ref),
    )


def _validate_required_carriers(
    binding: MemoryDurabilityBinding,
    carrier: MemoryDurabilityCarrier,
) -> None:
    if binding.requires_activity_boundary and not carrier.activity_boundary_ref:
        raise MemoryDurabilityViolationError(
            "event-sourced replay memory durability requires an activity boundary"
        )
    if binding.requires_store_version_or_packet_hash and not (
        carrier.store_version or carrier.packet_hash
    ):
        raise MemoryDurabilityViolationError(
            "event-sourced replay memory durability requires a recorded store version "
            "or packet hash"
        )
    if binding.requires_store_version and not carrier.store_version:
        raise MemoryDurabilityViolationError(
            "save-point checkpoint memory durability requires memory store version"
        )
    if binding.requires_packet_hash and not carrier.packet_hash:
        raise MemoryDurabilityViolationError(
            "save-point checkpoint memory durability requires packet hash"
        )
    if binding.requires_state_ledger_idempotency_key and not carrier.state_ledger_idempotency_key:
        raise MemoryDurabilityViolationError(
            "pure-pattern memory durability requires state-ledger idempotency key"
        )
    if binding.requires_observed_version and not carrier.observed_version:
        raise MemoryDurabilityViolationError(
            "reconciler memory durability requires observed version"
        )
    if binding.requires_wal_segment_ref and not carrier.wal_segment_ref:
        raise MemoryDurabilityViolationError("WAL memory durability requires WAL segment")
    if binding.requires_prewarm_plan_ref and not carrier.prewarm_plan_ref:
        raise MemoryDurabilityViolationError("WAL memory durability requires prewarm plan")


def _binding(
    engine_class: EngineClass,
    *,
    strategy: MemoryDurabilityStrategy,
    requires_activity_boundary: bool = False,
    requires_store_version_or_packet_hash: bool = False,
    requires_store_version: bool = False,
    requires_packet_hash: bool = False,
    requires_state_ledger_idempotency_key: bool = False,
    requires_observed_version: bool = False,
    requires_wal_segment_ref: bool = False,
    requires_prewarm_plan_ref: bool = False,
) -> MemoryDurabilityBinding:
    return MemoryDurabilityBinding(
        engine_class=engine_class,
        operation_engine_class=memory_operation_engine_class_for(engine_class),
        strategy=strategy,
        requires_activity_boundary=requires_activity_boundary,
        requires_store_version_or_packet_hash=requires_store_version_or_packet_hash,
        requires_store_version=requires_store_version,
        requires_packet_hash=requires_packet_hash,
        requires_state_ledger_idempotency_key=requires_state_ledger_idempotency_key,
        requires_observed_version=requires_observed_version,
        requires_wal_segment_ref=requires_wal_segment_ref,
        requires_prewarm_plan_ref=requires_prewarm_plan_ref,
    )


_BINDINGS: dict[EngineClass, MemoryDurabilityBinding] = {
    EngineClass.EVENT_SOURCED_REPLAY: _binding(
        EngineClass.EVENT_SOURCED_REPLAY,
        strategy=MemoryDurabilityStrategy.ACTIVITY_SNAPSHOT,
        requires_activity_boundary=True,
        requires_store_version_or_packet_hash=True,
    ),
    EngineClass.SAVE_POINT_CHECKPOINT: _binding(
        EngineClass.SAVE_POINT_CHECKPOINT,
        strategy=MemoryDurabilityStrategy.CHECKPOINT_PACKET,
        requires_store_version=True,
        requires_packet_hash=True,
    ),
    EngineClass.PURE_PATTERN_NO_ENGINE: _binding(
        EngineClass.PURE_PATTERN_NO_ENGINE,
        strategy=MemoryDurabilityStrategy.STATE_LEDGER_WRITE,
        requires_state_ledger_idempotency_key=True,
    ),
    EngineClass.RECONCILER_LOOP: _binding(
        EngineClass.RECONCILER_LOOP,
        strategy=MemoryDurabilityStrategy.RECONCILER_OBSERVED_VERSION,
        requires_observed_version=True,
    ),
    EngineClass.WAL_SEGMENT: _binding(
        EngineClass.WAL_SEGMENT,
        strategy=MemoryDurabilityStrategy.WAL_REBUILD_PREWARM,
        requires_wal_segment_ref=True,
        requires_prewarm_plan_ref=True,
    ),
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


__all__ = [
    "MemoryDurabilityBinding",
    "MemoryDurabilityCarrier",
    "MemoryDurabilityDecision",
    "MemoryDurabilityStrategy",
    "MemoryDurabilityViolationError",
    "memory_durability_binding_for",
    "memory_durability_bindings",
    "memory_operation_engine_class_for",
    "validate_memory_durability",
]
