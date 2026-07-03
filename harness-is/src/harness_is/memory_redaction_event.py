"""Memory redaction event schema - U-MEM-21.

Implements the C-MEM-18 redaction/tombstone/retention event payload carried by
durable memory-operation entries. The event records the target id and old/new
content hashes so physical content replacement remains auditable without
rewriting append-only operation history.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from harness_is.memory_record_envelope import MemoryID


class MemoryRedactionKind(StrEnum):
    """C-MEM-18 redaction transition kinds."""

    CONTENT_REDACTION = "content_redaction"
    SCOPE_RESTRICTION = "scope_restriction"
    TOMBSTONE = "tombstone"
    RETENTION_EXPIRY = "retention_expiry"


class MemoryRedactionActor(StrEnum):
    """C-MEM-18 event actor classes."""

    HARNESS = "harness"
    OPERATOR = "operator"
    POLICY = "policy"


class MemoryRedactionEvent(BaseModel):
    """C-MEM-18 durable redaction/tombstone/retention event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    target_memory_id: MemoryID
    redaction_kind: MemoryRedactionKind
    reason: str
    actor: MemoryRedactionActor
    timestamp: datetime
    replacement_summary: str | None = None
    old_content_hash: str
    new_content_hash: str

    @field_validator("event_id", "reason")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("redaction event strings cannot be empty")
        return value

    @field_validator("old_content_hash", "new_content_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("content hash must be a SHA-256 hex digest")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("content hash must be a SHA-256 hex digest") from exc
        return value

    @model_validator(mode="after")
    def _hashes_can_change(self) -> Self:
        if (
            self.redaction_kind
            in {MemoryRedactionKind.CONTENT_REDACTION, MemoryRedactionKind.RETENTION_EXPIRY}
            and self.old_content_hash == self.new_content_hash
        ):
            raise ValueError("physical redaction events must record a changed content hash")
        return self


__all__ = [
    "MemoryRedactionActor",
    "MemoryRedactionEvent",
    "MemoryRedactionKind",
]
