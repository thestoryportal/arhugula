"""Memory redaction, tombstone, and retention transitions - U-MEM-21.

Implements C-MEM-18 over the canonical memory store and durable memory
operation ledger. Redaction and retention expiry write the durable operation
first, then replace content-bearing records with an audit-safe summary so the
derived-index invalidation happens after the ledgered state transition.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict

from harness_is.memory_observability import (
    MemoryTelemetryOperationName,
    classify_memory_failure,
    memory_telemetry_span,
    set_memory_telemetry_attributes,
)
from harness_is.memory_operation_ledger import (
    MemoryOperationKind,
    MemoryOperationPayload,
    MemoryOperationProjection,
)
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordKind,
    MemoryTier,
    RedactionState,
    compute_memory_content_hash,
)
from harness_is.memory_redaction_event import (
    MemoryRedactionActor,
    MemoryRedactionEvent,
    MemoryRedactionKind,
)
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord
from harness_is.state_ledger_entry_schema import Actor, Identifier

type RedactionJSON = str | int | bool | None | list["RedactionJSON"] | dict[str, "RedactionJSON"]


class MemoryRedactionWriteError(RuntimeError):
    """Raised when a redaction/tombstone transition cannot be durably written."""


class MemoryRedactionResult(BaseModel):
    """Result of a completed C-MEM-18 state transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: MemoryRedactionEvent
    operation_action_id: Identifier
    record: MemoryStoreRecord


class MemoryRedactionService:
    """Write C-MEM-18 redaction, tombstone, and retention transitions."""

    def __init__(
        self,
        *,
        store: CanonicalMemoryStore,
        operation_actor: Actor,
        event_actor: MemoryRedactionActor,
        policy_ref: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cli_profile: str | None = None,
        procedural_snapshot_ref: str | None = None,
        tracer_provider: object | None = None,
    ) -> None:
        self._store = store
        self._operation_actor = operation_actor
        self._event_actor = event_actor
        self._policy_ref = policy_ref
        self._run_id = run_id
        self._step_id = step_id
        self._provider = provider
        self._model = model
        self._cli_profile = cli_profile
        self._procedural_snapshot_ref = procedural_snapshot_ref
        self._tracer_provider = tracer_provider

    def redact_content(
        self,
        memory_id: MemoryID,
        kind: MemoryRecordKind,
        *,
        timestamp: datetime,
        reason: str,
        replacement_summary: str | None,
        run_id: str | None = None,
    ) -> MemoryRedactionResult:
        """Physically replace content and mark the target record redacted."""

        return self._transition(
            memory_id,
            kind,
            timestamp=timestamp,
            reason=reason,
            replacement_summary=replacement_summary,
            run_id=run_id,
            operation_kind=MemoryOperationKind.REDACT,
            redaction_kind=MemoryRedactionKind.CONTENT_REDACTION,
            redaction_state=RedactionState.REDACTED,
            status="redacted",
        )

    def tombstone(
        self,
        memory_id: MemoryID,
        kind: MemoryRecordKind,
        *,
        timestamp: datetime,
        reason: str,
        replacement_summary: str | None = None,
        run_id: str | None = None,
    ) -> MemoryRedactionResult:
        """Write a durable tombstone and hide the target outside audit mode."""

        return self._transition(
            memory_id,
            kind,
            timestamp=timestamp,
            reason=reason,
            replacement_summary=replacement_summary,
            run_id=run_id,
            operation_kind=MemoryOperationKind.TOMBSTONE,
            redaction_kind=MemoryRedactionKind.TOMBSTONE,
            redaction_state=RedactionState.TOMBSTONED,
            status="tombstoned",
        )

    def expire_for_retention(
        self,
        memory_id: MemoryID,
        kind: MemoryRecordKind,
        *,
        timestamp: datetime,
        reason: str,
        replacement_summary: str | None,
        run_id: str | None = None,
    ) -> MemoryRedactionResult:
        """Ledger retention expiry before derived indexes can drop the record."""

        return self._transition(
            memory_id,
            kind,
            timestamp=timestamp,
            reason=reason,
            replacement_summary=replacement_summary,
            run_id=run_id,
            operation_kind=MemoryOperationKind.REDACT,
            redaction_kind=MemoryRedactionKind.RETENTION_EXPIRY,
            redaction_state=RedactionState.REDACTED,
            status="expired",
        )

    def _transition(
        self,
        memory_id: MemoryID,
        kind: MemoryRecordKind,
        *,
        timestamp: datetime,
        reason: str,
        replacement_summary: str | None,
        run_id: str | None,
        operation_kind: MemoryOperationKind,
        redaction_kind: MemoryRedactionKind,
        redaction_state: RedactionState,
        status: str,
    ) -> MemoryRedactionResult:
        if not reason.strip():
            raise ValueError("redaction reason cannot be empty")
        record = self._store.read_record(
            memory_id,
            kind,
            run_id=run_id or self._run_id,
            audit_mode=True,
        )
        old_content_hash = record.envelope.content_hash.hex()
        replacement_content = _replacement_content(
            record,
            redaction_kind=redaction_kind,
            status=status,
            reason=reason,
            timestamp=timestamp,
            replacement_summary=replacement_summary,
            old_content_hash=old_content_hash,
        )
        new_content_hash = compute_memory_content_hash(replacement_content)
        updated_record = MemoryStoreRecord(
            envelope=record.envelope.model_copy(
                update={
                    "updated_at": timestamp,
                    "content_hash": new_content_hash,
                    "redaction_state": redaction_state,
                }
            ),
            content=replacement_content,
        )
        event = _event_for(
            target_memory_id=memory_id,
            redaction_kind=redaction_kind,
            reason=reason,
            actor=self._event_actor,
            timestamp=timestamp,
            replacement_summary=replacement_summary,
            old_content_hash=old_content_hash,
            new_content_hash=new_content_hash.hex(),
        )
        action_id = Identifier(f"{operation_kind.value}:{event.event_id.rsplit(':', 1)[-1]}")
        with memory_telemetry_span(
            self._tracer_provider,
            tracer_name="harness.is.memory_redaction",
            operation_name=(
                MemoryTelemetryOperationName.TOMBSTONE
                if operation_kind is MemoryOperationKind.TOMBSTONE
                else MemoryTelemetryOperationName.REDACTION
            ),
            operation_kind=operation_kind.value,
            tier=record.envelope.tier.value,
            provider=self._provider,
            model=self._model,
            cli_profile=self._cli_profile,
            policy_decision=redaction_kind.value,
            record_count=1,
        ) as span:
            try:
                self._store.append_memory_operation(
                    MemoryOperationPayload(
                        action_id=action_id,
                        idempotency_key=Identifier(f"{operation_kind.value}:{event.event_id}"),
                        actor=self._operation_actor,
                        timestamp=timestamp,
                        operation_kind=operation_kind,
                        operation_projection=MemoryOperationProjection.NONE,
                        run_id=run_id or self._run_id,
                        step_id=self._step_id,
                        provider=self._provider,
                        model=self._model,
                        cli_profile=self._cli_profile,
                        memory_refs=(memory_id,),
                        policy_ref=self._policy_ref,
                        procedural_snapshot_ref=self._procedural_snapshot_ref,
                        redaction_event=event,
                    )
                )
                self._store.write_record(updated_record)
            except Exception as exc:
                set_memory_telemetry_attributes(span, failure_class=classify_memory_failure(exc))
                raise MemoryRedactionWriteError(
                    f"failed to write {redaction_kind.value} transition for {memory_id!s}"
                ) from exc
        return MemoryRedactionResult(
            event=event,
            operation_action_id=action_id,
            record=updated_record,
        )


def _replacement_content(
    record: MemoryStoreRecord,
    *,
    redaction_kind: MemoryRedactionKind,
    status: str,
    reason: str,
    timestamp: datetime,
    replacement_summary: str | None,
    old_content_hash: str,
) -> Mapping[str, object]:
    timestamp_field = {
        MemoryRedactionKind.CONTENT_REDACTION: "redacted_at",
        MemoryRedactionKind.SCOPE_RESTRICTION: "redacted_at",
        MemoryRedactionKind.TOMBSTONE: "tombstoned_at",
        MemoryRedactionKind.RETENTION_EXPIRY: "retention_expired_at",
    }[redaction_kind]
    replacement: dict[str, object] = {
        "status": status,
        "redaction_kind": redaction_kind.value,
        "target_memory_id": str(record.envelope.memory_id),
        "target_record_kind": record.envelope.kind.value,
        "old_content_hash": old_content_hash,
        "reason": reason,
        "replacement_summary": replacement_summary,
        timestamp_field: timestamp,
    }
    if record.envelope.tier is MemoryTier.EPISODIC:
        run_id = record.content.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("episodic redaction replacement requires content.run_id")
        replacement["run_id"] = run_id
    return replacement


def _event_for(
    *,
    target_memory_id: MemoryID,
    redaction_kind: MemoryRedactionKind,
    reason: str,
    actor: MemoryRedactionActor,
    timestamp: datetime,
    replacement_summary: str | None,
    old_content_hash: str,
    new_content_hash: str,
) -> MemoryRedactionEvent:
    payload = {
        "target_memory_id": str(target_memory_id),
        "redaction_kind": redaction_kind.value,
        "reason": reason,
        "actor": actor.value,
        "timestamp": timestamp.isoformat(),
        "replacement_summary": replacement_summary,
        "old_content_hash": old_content_hash,
        "new_content_hash": new_content_hash,
    }
    event_hash = _hash_json(payload)
    return MemoryRedactionEvent(
        event_id=f"memory-redaction:{event_hash[:32]}",
        target_memory_id=target_memory_id,
        redaction_kind=redaction_kind,
        reason=reason,
        actor=actor,
        timestamp=timestamp,
        replacement_summary=replacement_summary,
        old_content_hash=old_content_hash,
        new_content_hash=new_content_hash,
    )


def _hash_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable_mapping(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _jsonable_mapping(value: Mapping[str, object]) -> dict[str, RedactionJSON]:
    return {key: _jsonable(item) for key, item in value.items()}


def _jsonable(value: object) -> RedactionJSON:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return _jsonable_mapping(cast("Mapping[str, object]", value))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_jsonable(item) for item in cast("Sequence[object]", value)]
    return str(value)


__all__ = [
    "MemoryRedactionActor",
    "MemoryRedactionEvent",
    "MemoryRedactionKind",
    "MemoryRedactionResult",
    "MemoryRedactionService",
    "MemoryRedactionWriteError",
]
