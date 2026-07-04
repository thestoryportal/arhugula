"""Memory record vocabulary and common envelope - U-MEM-01.

Implements the C-MEM-03 provider-neutral memory record identity envelope plus
the minimal deterministic helpers later memory-store units build on. This
module is schema/hash substrate only: it does not capture, retrieve, route, or
persist memory records.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import NewType, cast

from pydantic import BaseModel, ConfigDict, Field

from harness_is.state_ledger_entry_schema import Bytes32

MemoryID = NewType("MemoryID", str)
"""Opaque memory record identifier."""

Timestamp = datetime
"""Wall-clock time-instant binding for memory envelope timestamps."""

type CanonicalJSON = str | int | bool | None | list[CanonicalJSON] | dict[str, CanonicalJSON]

DERIVED_INDEX_FIELD = "derived_indexes"
"""Top-level canonical content hashing sidecar for derived index material."""


class MemoryTier(StrEnum):
    """The five memory substrate tiers."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    DURABLE = "durable"


class MemoryRecordKind(StrEnum):
    """Canonical memory record payload families named by the memory spec."""

    EPISODIC_RUN = "episodic_run"
    EPISODIC_TURN = "episodic_turn"
    TOOL_EVENT = "tool_event"
    COMPACTION_EVENT = "compaction_event"
    SEMANTIC_FACT = "semantic_fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    CONVENTION = "convention"
    FAILURE_LEARNING = "failure_learning"
    RESEARCH = "research"
    PROCEDURAL_SNAPSHOT = "procedural_snapshot"
    MEMORY_OPERATION = "memory_operation"


class SourceRefType(StrEnum):
    """Source reference classes accepted by the common memory envelope."""

    RUN = "run"
    TURN = "turn"
    TOOL_EVENT = "tool_event"
    COMPACTION = "compaction"
    FILE = "file"
    GIT_COMMIT = "git_commit"
    OPERATOR = "operator"
    PROVIDER_RESPONSE = "provider_response"
    EXTERNAL = "external"


class MemoryVisibility(StrEnum):
    """Visibility scope for a memory record."""

    PRIVATE = "private"
    PROJECT = "project"
    WORKFLOW = "workflow"
    TENANT = "tenant"
    PUBLIC = "public"


class RedactionState(StrEnum):
    """Durable redaction state carried on every memory envelope."""

    ACTIVE = "active"
    REDACTED = "redacted"
    TOMBSTONED = "tombstoned"


class SourceRef(BaseModel):
    """Reference to source material that produced or justified a memory record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref_type: SourceRefType
    ref: str
    content_hash: Bytes32 | None = None


def _empty_source_refs() -> tuple[SourceRef, ...]:
    return ()


def _empty_memory_ids() -> tuple[MemoryID, ...]:
    return ()


class MemoryScope(BaseModel):
    """Provider-neutral project/workflow/provider/tenant scope for memory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str | None = None
    workflow: str | None = None
    workload_class: str | None = None
    provider_family: str | None = None
    cli_profile: str | None = None
    tenant: str | None = None
    visibility: MemoryVisibility


class MemoryRecordEnvelope(BaseModel):
    """Common identity envelope present on every canonical memory record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: MemoryID
    schema_version: str
    tier: MemoryTier
    kind: MemoryRecordKind
    created_at: Timestamp
    updated_at: Timestamp | None = None
    source_refs: tuple[SourceRef, ...] = Field(default_factory=_empty_source_refs)
    scope: MemoryScope
    content_hash: Bytes32
    supersedes: tuple[MemoryID, ...] = Field(default_factory=_empty_memory_ids)
    superseded_by: tuple[MemoryID, ...] = Field(default_factory=_empty_memory_ids)
    redaction_state: RedactionState = RedactionState.ACTIVE


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _normalize_for_json(
    value: object,
    *,
    exclude_top_level_derived_indexes: bool = False,
) -> CanonicalJSON:
    if isinstance(value, StrEnum):
        return _nfc(value.value)
    if isinstance(value, str):
        return _nfc(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("Memory content canonicalization does not accept float values")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _normalize_for_json(cast("Mapping[str, object]", value.model_dump()))
    if isinstance(value, Mapping):
        normalized: dict[str, CanonicalJSON] = {}
        mapping = cast("Mapping[object, object]", value)
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise TypeError("Memory content canonicalization requires string mapping keys")
            normalized_key = _nfc(key)
            if exclude_top_level_derived_indexes and normalized_key == DERIVED_INDEX_FIELD:
                continue
            if normalized_key in normalized:
                raise ValueError(f"Memory content has duplicate canonical key {normalized_key!r}")
            normalized[normalized_key] = _normalize_for_json(item)
        return normalized
    if isinstance(value, Sequence):
        sequence = cast("Sequence[object]", value)
        return [_normalize_for_json(item) for item in sequence]
    raise TypeError(f"Unsupported memory content value: {type(value).__name__}")


def canonicalize_memory_content(content: Mapping[str, object]) -> bytes:
    """Serialize memory content to deterministic UTF-8 JSON bytes.

    Top-level ``derived_indexes`` material is omitted because derived indexes
    are rebuildable. Nested fields with the same name remain canonical content.
    Floats are rejected because this stdlib canonicalizer does not define an
    RFC 8785-compatible float rendering contract.
    """

    normalized = _normalize_for_json(content, exclude_top_level_derived_indexes=True)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_memory_content_hash(content: Mapping[str, object]) -> Bytes32:
    """Compute the SHA-256 digest for canonical memory record content."""

    return hashlib.sha256(canonicalize_memory_content(content)).digest()


def derive_memory_id(
    tier: MemoryTier,
    kind: MemoryRecordKind,
    content_hash: Bytes32,
) -> MemoryID:
    """Derive a stable, content-addressed memory identifier."""

    if len(content_hash) != 32:
        raise ValueError("content_hash must be exactly 32 bytes")
    return MemoryID(f"mem:{tier.value}:{kind.value}:{content_hash.hex()}")


__all__ = [
    "DERIVED_INDEX_FIELD",
    "MemoryID",
    "MemoryRecordEnvelope",
    "MemoryRecordKind",
    "MemoryScope",
    "MemoryTier",
    "MemoryVisibility",
    "RedactionState",
    "SourceRef",
    "SourceRefType",
    "Timestamp",
    "canonicalize_memory_content",
    "compute_memory_content_hash",
    "derive_memory_id",
]
