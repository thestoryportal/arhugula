"""Durable memory operation ledger - U-MEM-03.

Implements C-MEM-08 as a memory-specific, additive derivative of the C-IS
state-ledger shape. The canonical ``durable/memory_ops.jsonl`` ledger owns the
global operation order; projection files are rebuildable filtered views keyed
by canonical ledger ``action_id`` and carry no independent hash-chain fields.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.memory_record_envelope import MemoryID
from harness_is.memory_redaction_event import MemoryRedactionEvent, MemoryRedactionKind
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Actor,
    ActorClass,
    BranchMetadata,
    Bytes32,
    Identifier,
    StateLedgerEntry,
    Timestamp,
)

_WRITE_LOCK = threading.Lock()


class MemoryOperationKind(StrEnum):
    """Operation kinds declared by C-MEM-08."""

    CAPTURE = "capture"
    RETRIEVE = "retrieve"
    INJECT = "inject"
    PROMOTE = "promote"
    PROPOSE_PROMOTION = "propose_promotion"
    DENY_PROMOTION = "deny_promotion"
    REDACT = "redact"
    TOMBSTONE = "tombstone"
    DELETE_REQUEST = "delete_request"
    NATIVE_ADAPTER_CALL = "native_adapter_call"
    STANDARD_TOOL_CALL = "standard_tool_call"
    COMPACTION_DECISION = "compaction_decision"


class MemoryOperationProjection(StrEnum):
    """Rebuildable projection classes declared by C-MEM-08."""

    NONE = "none"
    PROMOTION_DECISIONS = "promotion_decisions"
    INJECTION_DECISIONS = "injection_decisions"
    RETRIEVAL_EVENTS = "retrieval_events"

    @classmethod
    def for_operation_kind(cls, kind: MemoryOperationKind) -> MemoryOperationProjection:
        if kind is MemoryOperationKind.RETRIEVE:
            return cls.RETRIEVAL_EVENTS
        if kind is MemoryOperationKind.INJECT:
            return cls.INJECTION_DECISIONS
        if kind in {
            MemoryOperationKind.PROMOTE,
            MemoryOperationKind.PROPOSE_PROMOTION,
            MemoryOperationKind.DENY_PROMOTION,
        }:
            return cls.PROMOTION_DECISIONS
        return cls.NONE


class MemoryOperationEngineClass(StrEnum):
    """Engine-class values recorded on memory operations by C-MEM-08."""

    EVENT_SOURCED_REPLAY = "event-sourced-replay"
    SAVE_POINT_CHECKPOINT = "save-point-checkpoint"
    PURE_PATTERN_NO_ENGINE = "pure-pattern-no-engine"
    RECONCILER_LOOP = "reconciler-loop"
    WAL_SEGMENT = "WAL-segment"


class MemoryOperationWriteResult(StrEnum):
    """Outcome of an append attempt against ``memory_ops.jsonl``."""

    APPENDED = "appended"
    IDEMPOTENT_NOOP = "idempotent_noop"


class MemoryLedgerVerificationStatus(StrEnum):
    """Overall memory-ledger verification status."""

    VALID = "valid"
    INVALID = "invalid"


class MemoryLedgerVerificationFailureType(StrEnum):
    """Failure classes surfaced by the memory-ledger verifier."""

    INCEPTION_SENTINEL_MISMATCH = "inception_sentinel_mismatch"
    RESPONSE_HASH_MISMATCH = "response_hash_mismatch"
    CHAIN_LINK_MISMATCH = "chain_link_mismatch"


class MemoryOperationIdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different operation."""


class MemoryOperationProjectionMismatchError(ValueError):
    """Raised when an operation kind is paired with the wrong projection."""


class MemoryOperationRedactionEventMismatchError(ValueError):
    """Raised when a redaction/tombstone operation lacks its C-MEM-18 event."""


class MemoryOperationEntry(StateLedgerEntry):
    """C-MEM-08 memory operation entry.

    The inherited state-ledger fields remain the F-layer shape. The memory
    fields below are sidecar payload included in the memory-operation hash.
    """

    operation_kind: MemoryOperationKind
    operation_projection: MemoryOperationProjection
    run_id: str | None = None
    step_id: str | None = None
    provider: str | None = None
    model: str | None = None
    cli_profile: str | None = None
    engine_class: MemoryOperationEngineClass | None = None
    memory_refs: tuple[MemoryID, ...] = Field(default_factory=tuple)
    policy_ref: str | None = None
    procedural_snapshot_ref: str | None = None
    redaction_event: MemoryRedactionEvent | None = None

    @model_validator(mode="after")
    def _projection_matches_kind(self) -> Self:
        expected = MemoryOperationProjection.for_operation_kind(self.operation_kind)
        if self.operation_projection is not expected:
            raise MemoryOperationProjectionMismatchError(
                f"{self.operation_kind.value} operations project to {expected.value}"
            )
        _validate_redaction_event(self.operation_kind, self.redaction_event)
        return self


class MemoryOperationPayload(BaseModel):
    """Caller-supplied memory operation content before hash-chain fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: Identifier
    idempotency_key: Identifier
    actor: Actor
    timestamp: Timestamp
    operation_kind: MemoryOperationKind
    operation_projection: MemoryOperationProjection
    run_id: str | None = None
    step_id: str | None = None
    provider: str | None = None
    model: str | None = None
    cli_profile: str | None = None
    engine_class: MemoryOperationEngineClass | None = None
    memory_refs: tuple[MemoryID, ...] = Field(default_factory=tuple)
    policy_ref: str | None = None
    procedural_snapshot_ref: str | None = None
    redaction_event: MemoryRedactionEvent | None = None
    procedural_tier_snapshot_ref: Identifier | None = None
    branch_metadata: BranchMetadata | None = None

    @model_validator(mode="after")
    def _projection_matches_kind(self) -> Self:
        expected = MemoryOperationProjection.for_operation_kind(self.operation_kind)
        if self.operation_projection is not expected:
            raise MemoryOperationProjectionMismatchError(
                f"{self.operation_kind.value} operations project to {expected.value}"
            )
        _validate_redaction_event(self.operation_kind, self.redaction_event)
        return self


def _validate_redaction_event(
    operation_kind: MemoryOperationKind,
    redaction_event: MemoryRedactionEvent | None,
) -> None:
    if operation_kind in {MemoryOperationKind.REDACT, MemoryOperationKind.TOMBSTONE}:
        if redaction_event is None:
            raise MemoryOperationRedactionEventMismatchError(
                f"{operation_kind.value} operations require a C-MEM-18 redaction_event"
            )
    elif redaction_event is not None:
        raise MemoryOperationRedactionEventMismatchError(
            f"{operation_kind.value} operations cannot carry redaction_event"
        )
    if redaction_event is None:
        return
    if operation_kind is MemoryOperationKind.TOMBSTONE:
        if redaction_event.redaction_kind is not MemoryRedactionKind.TOMBSTONE:
            raise MemoryOperationRedactionEventMismatchError(
                "tombstone operations require redaction_kind=tombstone"
            )
    elif redaction_event.redaction_kind is MemoryRedactionKind.TOMBSTONE:
        raise MemoryOperationRedactionEventMismatchError(
            "tombstone redaction events require operation_kind=tombstone"
        )


class MemoryLedgerVerificationResult(BaseModel):
    """Read-only verification result for ``memory_ops.jsonl``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MemoryLedgerVerificationStatus
    failure_position: int | None
    failure_type: MemoryLedgerVerificationFailureType | None
    entries_verified: int


class MemoryOperationProjectionHandles(BaseModel):
    """Handles for the three rebuildable C-MEM-08 projection files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    promotion_decisions: JsonlLedgerHandle
    injection_decisions: JsonlLedgerHandle
    retrieval_events: JsonlLedgerHandle


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _optional_nfc(value: str | None) -> str | None:
    return _nfc(value) if value is not None else None


def _actor_payload(actor: Actor) -> dict[str, str]:
    return {
        "actor_class": _nfc(actor.actor_class.value),
        "actor_id": _nfc(actor.actor_id),
    }


def _branch_metadata_payload(branch_metadata: BranchMetadata) -> dict[str, object]:
    return {
        "parent_action_id": _nfc(branch_metadata.parent_action_id),
        "branch_index": branch_metadata.branch_index,
        "terminal_status": _optional_nfc(branch_metadata.terminal_status),
    }


def _canonical_payload(entry: MemoryOperationEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_id": _nfc(entry.action_id),
        "idempotency_key": _nfc(entry.idempotency_key),
        "actor": _actor_payload(entry.actor),
        "timestamp": entry.timestamp.isoformat(),
        "prior_event_hash": entry.prior_event_hash.hex(),
        "operation_kind": _nfc(entry.operation_kind.value),
        "operation_projection": _nfc(entry.operation_projection.value),
        "run_id": _optional_nfc(entry.run_id),
        "step_id": _optional_nfc(entry.step_id),
        "provider": _optional_nfc(entry.provider),
        "model": _optional_nfc(entry.model),
        "cli_profile": _optional_nfc(entry.cli_profile),
        "engine_class": (
            _nfc(entry.engine_class.value) if entry.engine_class is not None else None
        ),
        "memory_refs": [_nfc(memory_ref) for memory_ref in entry.memory_refs],
        "policy_ref": _optional_nfc(entry.policy_ref),
        "procedural_snapshot_ref": _optional_nfc(entry.procedural_snapshot_ref),
    }
    if entry.redaction_event is not None:
        payload["redaction_event"] = _redaction_event_payload(entry.redaction_event)
    if entry.procedural_tier_snapshot_ref is not None:
        payload["procedural_tier_snapshot_ref"] = _nfc(entry.procedural_tier_snapshot_ref)
    if entry.branch_metadata is not None:
        payload["branch_metadata"] = _branch_metadata_payload(entry.branch_metadata)
    return payload


def _redaction_event_payload(event: MemoryRedactionEvent) -> dict[str, object]:
    return {
        "event_id": _nfc(event.event_id),
        "target_memory_id": _nfc(event.target_memory_id),
        "redaction_kind": _nfc(event.redaction_kind.value),
        "reason": _nfc(event.reason),
        "actor": _nfc(event.actor.value),
        "timestamp": event.timestamp.isoformat(),
        "replacement_summary": _optional_nfc(event.replacement_summary),
        "old_content_hash": _nfc(event.old_content_hash),
        "new_content_hash": _nfc(event.new_content_hash),
    }


def canonicalize_memory_operation(entry: MemoryOperationEntry) -> bytes:
    """Canonicalize a memory operation entry excluding ``response_hash``."""

    return json.dumps(
        _canonical_payload(entry),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_memory_operation_response_hash(entry: MemoryOperationEntry) -> Bytes32:
    """Compute the sidecar-aware C-MEM-08 response hash."""

    return hashlib.sha256(canonicalize_memory_operation(entry)).digest()


def _serialize_entry(entry: MemoryOperationEntry) -> str:
    payload = _canonical_payload(entry)
    payload["response_hash"] = entry.response_hash.hex()
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _deserialize_actor(raw: dict[str, object]) -> Actor:
    return Actor(
        actor_class=ActorClass(str(raw["actor_class"])),
        actor_id=str(raw["actor_id"]),
    )


def _deserialize_branch_metadata(raw: dict[str, object] | None) -> BranchMetadata | None:
    if raw is None:
        return None
    branch_index = raw["branch_index"]
    if not isinstance(branch_index, int):
        raise ValueError("branch_metadata.branch_index must be an integer")
    return BranchMetadata(
        parent_action_id=Identifier(str(raw["parent_action_id"])),
        branch_index=branch_index,
        terminal_status=raw["terminal_status"],  # type: ignore[arg-type]
    )


def _deserialize_entry(line: str) -> MemoryOperationEntry:
    raw = json.loads(line)
    return MemoryOperationEntry(
        action_id=Identifier(raw["action_id"]),
        idempotency_key=Identifier(raw["idempotency_key"]),
        actor=_deserialize_actor(raw["actor"]),
        response_hash=bytes.fromhex(raw["response_hash"]),
        timestamp=datetime.fromisoformat(raw["timestamp"]),
        prior_event_hash=bytes.fromhex(raw["prior_event_hash"]),
        procedural_tier_snapshot_ref=(
            Identifier(raw["procedural_tier_snapshot_ref"])
            if raw.get("procedural_tier_snapshot_ref") is not None
            else None
        ),
        branch_metadata=_deserialize_branch_metadata(raw.get("branch_metadata")),
        operation_kind=MemoryOperationKind(raw["operation_kind"]),
        operation_projection=MemoryOperationProjection(raw["operation_projection"]),
        run_id=raw.get("run_id"),
        step_id=raw.get("step_id"),
        provider=raw.get("provider"),
        model=raw.get("model"),
        cli_profile=raw.get("cli_profile"),
        engine_class=(
            MemoryOperationEngineClass(raw["engine_class"])
            if raw.get("engine_class") is not None
            else None
        ),
        memory_refs=tuple(MemoryID(ref) for ref in raw.get("memory_refs", [])),
        policy_ref=raw.get("policy_ref"),
        procedural_snapshot_ref=raw.get("procedural_snapshot_ref"),
        redaction_event=(
            MemoryRedactionEvent.model_validate(raw["redaction_event"])
            if raw.get("redaction_event") is not None
            else None
        ),
    )


def read_memory_operation_ledger(
    ledger_handle: JsonlLedgerHandle,
) -> list[MemoryOperationEntry]:
    """Read the canonical memory operation ledger."""

    if not ledger_handle.canonical_path.exists():
        return []
    return [
        _deserialize_entry(line)
        for line in ledger_handle.canonical_path.read_text().splitlines()
        if line.strip()
    ]


def _prior_event_hash(prior_entry: MemoryOperationEntry | None) -> Bytes32:
    if prior_entry is None:
        return ALL_ZEROS_SENTINEL
    return compute_memory_operation_response_hash(prior_entry)


def _entry_from_payload(
    payload: MemoryOperationPayload,
    *,
    prior_entry: MemoryOperationEntry | None,
) -> MemoryOperationEntry:
    draft = MemoryOperationEntry(
        action_id=payload.action_id,
        idempotency_key=payload.idempotency_key,
        actor=payload.actor,
        response_hash=ALL_ZEROS_SENTINEL,
        timestamp=payload.timestamp,
        prior_event_hash=_prior_event_hash(prior_entry),
        procedural_tier_snapshot_ref=payload.procedural_tier_snapshot_ref,
        branch_metadata=payload.branch_metadata,
        operation_kind=payload.operation_kind,
        operation_projection=payload.operation_projection,
        run_id=payload.run_id,
        step_id=payload.step_id,
        provider=payload.provider,
        model=payload.model,
        cli_profile=payload.cli_profile,
        engine_class=payload.engine_class,
        memory_refs=payload.memory_refs,
        policy_ref=payload.policy_ref,
        procedural_snapshot_ref=payload.procedural_snapshot_ref,
        redaction_event=payload.redaction_event,
    )
    return draft.model_copy(update={"response_hash": compute_memory_operation_response_hash(draft)})


def _equivalence_payload_from_entry(entry: MemoryOperationEntry) -> dict[str, object]:
    return {
        "action_id": entry.action_id,
        "idempotency_key": entry.idempotency_key,
        "actor": entry.actor,
        "procedural_tier_snapshot_ref": entry.procedural_tier_snapshot_ref,
        "branch_metadata": entry.branch_metadata,
        "operation_kind": entry.operation_kind,
        "operation_projection": entry.operation_projection,
        "run_id": entry.run_id,
        "step_id": entry.step_id,
        "provider": entry.provider,
        "model": entry.model,
        "cli_profile": entry.cli_profile,
        "engine_class": entry.engine_class,
        "memory_refs": entry.memory_refs,
        "policy_ref": entry.policy_ref,
        "procedural_snapshot_ref": entry.procedural_snapshot_ref,
        "redaction_event": entry.redaction_event,
    }


def _equivalence_payload_from_payload(payload: MemoryOperationPayload) -> dict[str, object]:
    return {
        "action_id": payload.action_id,
        "idempotency_key": payload.idempotency_key,
        "actor": payload.actor,
        "procedural_tier_snapshot_ref": payload.procedural_tier_snapshot_ref,
        "branch_metadata": payload.branch_metadata,
        "operation_kind": payload.operation_kind,
        "operation_projection": payload.operation_projection,
        "run_id": payload.run_id,
        "step_id": payload.step_id,
        "provider": payload.provider,
        "model": payload.model,
        "cli_profile": payload.cli_profile,
        "engine_class": payload.engine_class,
        "memory_refs": payload.memory_refs,
        "policy_ref": payload.policy_ref,
        "procedural_snapshot_ref": payload.procedural_snapshot_ref,
        "redaction_event": payload.redaction_event,
    }


def append_memory_operation(
    ledger_handle: JsonlLedgerHandle,
    payload: MemoryOperationPayload,
) -> MemoryOperationWriteResult:
    """Append one memory operation with strict idempotency semantics."""

    with _WRITE_LOCK:
        ledger = read_memory_operation_ledger(ledger_handle)
        for entry in ledger:
            if entry.idempotency_key != payload.idempotency_key:
                continue
            if _equivalence_payload_from_entry(entry) == _equivalence_payload_from_payload(payload):
                return MemoryOperationWriteResult.IDEMPOTENT_NOOP
            raise MemoryOperationIdempotencyConflictError(
                f"idempotency_key {payload.idempotency_key!r} already records a different operation"
            )

        entry = _entry_from_payload(payload, prior_entry=ledger[-1] if ledger else None)
        ledger_handle.canonical_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_handle.canonical_path.open("a") as fh:
            fh.write(_serialize_entry(entry) + "\n")
    return MemoryOperationWriteResult.APPENDED


def _valid_result(entries_verified: int) -> MemoryLedgerVerificationResult:
    return MemoryLedgerVerificationResult(
        status=MemoryLedgerVerificationStatus.VALID,
        failure_position=None,
        failure_type=None,
        entries_verified=entries_verified,
    )


def _invalid_result(
    *,
    failure_position: int,
    failure_type: MemoryLedgerVerificationFailureType,
    entries_verified: int,
) -> MemoryLedgerVerificationResult:
    return MemoryLedgerVerificationResult(
        status=MemoryLedgerVerificationStatus.INVALID,
        failure_position=failure_position,
        failure_type=failure_type,
        entries_verified=entries_verified,
    )


def verify_memory_operation_entries(
    ledger: list[MemoryOperationEntry],
) -> MemoryLedgerVerificationResult:
    """Verify response hashes and prior-event links for loaded entries."""

    if not ledger:
        return _valid_result(entries_verified=0)
    for index, entry in enumerate(ledger):
        position = index + 1
        expected_prior = ALL_ZEROS_SENTINEL if index == 0 else _prior_event_hash(ledger[index - 1])
        if entry.prior_event_hash != expected_prior:
            failure_type = (
                MemoryLedgerVerificationFailureType.INCEPTION_SENTINEL_MISMATCH
                if index == 0
                else MemoryLedgerVerificationFailureType.CHAIN_LINK_MISMATCH
            )
            return _invalid_result(
                failure_position=position,
                failure_type=failure_type,
                entries_verified=index,
            )
        if entry.response_hash != compute_memory_operation_response_hash(entry):
            return _invalid_result(
                failure_position=position,
                failure_type=MemoryLedgerVerificationFailureType.RESPONSE_HASH_MISMATCH,
                entries_verified=index,
            )
    return _valid_result(entries_verified=len(ledger))


def verify_memory_operation_ledger(
    ledger_handle: JsonlLedgerHandle,
) -> MemoryLedgerVerificationResult:
    """Read and verify the canonical ``memory_ops.jsonl`` ledger."""

    return verify_memory_operation_entries(read_memory_operation_ledger(ledger_handle))


def _projection_handle(
    handles: MemoryOperationProjectionHandles,
    projection: MemoryOperationProjection,
) -> JsonlLedgerHandle:
    if projection is MemoryOperationProjection.PROMOTION_DECISIONS:
        return handles.promotion_decisions
    if projection is MemoryOperationProjection.INJECTION_DECISIONS:
        return handles.injection_decisions
    if projection is MemoryOperationProjection.RETRIEVAL_EVENTS:
        return handles.retrieval_events
    raise ValueError("projection none has no projection handle")


def _projection_record(entry: MemoryOperationEntry) -> dict[str, str]:
    return {
        "action_id": entry.action_id,
        "idempotency_key": entry.idempotency_key,
        "operation_kind": entry.operation_kind.value,
        "operation_projection": entry.operation_projection.value,
    }


def _write_projection_file(path: Path, records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("")
        return
    path.write_text("".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records))


def rebuild_memory_operation_projections(
    ledger_handle: JsonlLedgerHandle,
    handles: MemoryOperationProjectionHandles,
) -> dict[MemoryOperationProjection, int]:
    """Rebuild all non-authoritative C-MEM-08 projection files."""

    buckets: dict[MemoryOperationProjection, list[dict[str, str]]] = {
        MemoryOperationProjection.PROMOTION_DECISIONS: [],
        MemoryOperationProjection.INJECTION_DECISIONS: [],
        MemoryOperationProjection.RETRIEVAL_EVENTS: [],
    }
    for entry in read_memory_operation_ledger(ledger_handle):
        if entry.operation_projection is MemoryOperationProjection.NONE:
            continue
        buckets[entry.operation_projection].append(_projection_record(entry))

    for projection, records in buckets.items():
        _write_projection_file(_projection_handle(handles, projection).canonical_path, records)
    return {projection: len(records) for projection, records in buckets.items()}


__all__ = [
    "MemoryLedgerVerificationFailureType",
    "MemoryLedgerVerificationResult",
    "MemoryLedgerVerificationStatus",
    "MemoryOperationEngineClass",
    "MemoryOperationEntry",
    "MemoryOperationIdempotencyConflictError",
    "MemoryOperationKind",
    "MemoryOperationPayload",
    "MemoryOperationProjection",
    "MemoryOperationProjectionHandles",
    "MemoryOperationProjectionMismatchError",
    "MemoryOperationRedactionEventMismatchError",
    "MemoryOperationWriteResult",
    "append_memory_operation",
    "canonicalize_memory_operation",
    "compute_memory_operation_response_hash",
    "read_memory_operation_ledger",
    "rebuild_memory_operation_projections",
    "verify_memory_operation_entries",
    "verify_memory_operation_ledger",
]
