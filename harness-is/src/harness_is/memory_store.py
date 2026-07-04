"""Canonical memory store - U-MEM-06.

Implements the C-MEM-02..C-MEM-07 filesystem store boundary over the existing
memory path registry, common record envelope, and durable memory operation
ledger. Runtime capture, retrieval, ranking, and injection remain later units.
"""

from __future__ import annotations

import json
import os
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast
from urllib.parse import quote

from harness_core import DeploymentSurface
from pydantic import BaseModel, ConfigDict, model_validator

from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle
from harness_is.memory_operation_ledger import (
    MemoryOperationEntry,
    MemoryOperationPayload,
    MemoryOperationWriteResult,
    append_memory_operation,
    read_memory_operation_ledger,
)
from harness_is.memory_path_registry import (
    MemoryPathClass,
    MemoryPathRegistry,
    MemoryRootBinding,
)
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryTier,
    RedactionState,
    compute_memory_content_hash,
)

type StoreJSON = str | int | bool | None | list[StoreJSON] | dict[str, StoreJSON]

_FILE_WRITE_LOCK = threading.Lock()
_JSONL_WRITE_LOCK = threading.Lock()


class MemoryStoreWriteResult(StrEnum):
    """Outcome of a canonical memory store record write."""

    WRITTEN = "written"


class MemoryStoreRecordNotFoundError(LookupError):
    """Raised when a requested canonical memory record is absent."""


class MemoryStoreRecordUnavailableError(LookupError):
    """Raised when a record exists but is hidden outside audit mode."""


class MemoryStoreUnsupportedKindError(ValueError):
    """Raised when a caller uses the wrong store surface for a record kind."""


class MemoryStoreContentHashMismatchError(ValueError):
    """Raised when envelope content hash does not match canonical content."""


class MemoryStoreRecord(BaseModel):
    """Envelope-backed canonical record stored outside the operation ledger.

    Durable memory operations are intentionally not represented by this model;
    they use the U-MEM-03 ``MemoryOperationEntry`` ledger format at
    ``durable/memory_ops.jsonl``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope: MemoryRecordEnvelope
    content: Mapping[str, object]

    @model_validator(mode="after")
    def _matches_envelope(self) -> Self:
        expected_tier = _tier_for_kind(self.envelope.kind)
        if self.envelope.tier != expected_tier:
            raise ValueError(
                f"{self.envelope.kind.value} records must use {expected_tier.value} tier"
            )
        expected_hash = compute_memory_content_hash(self.content)
        if self.envelope.content_hash != expected_hash:
            raise MemoryStoreContentHashMismatchError(
                "envelope.content_hash does not match canonical content"
            )
        return self


class DerivedIndexInvalidation(BaseModel):
    """Rebuildable semantic-index stale marker emitted after canonical writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["stale"] = "stale"
    reason: Literal["canonical_write"] = "canonical_write"
    memory_id: MemoryID
    record_kind: MemoryRecordKind
    content_hash: str


DerivedIndexInvalidationHook = Callable[[DerivedIndexInvalidation], None]
"""Hook invoked after the semantic derived-index stale marker is written."""


_TIER_BY_KIND: Mapping[MemoryRecordKind, MemoryTier] = {
    MemoryRecordKind.EPISODIC_RUN: MemoryTier.EPISODIC,
    MemoryRecordKind.EPISODIC_TURN: MemoryTier.EPISODIC,
    MemoryRecordKind.TOOL_EVENT: MemoryTier.EPISODIC,
    MemoryRecordKind.COMPACTION_EVENT: MemoryTier.EPISODIC,
    MemoryRecordKind.SEMANTIC_FACT: MemoryTier.SEMANTIC,
    MemoryRecordKind.PREFERENCE: MemoryTier.SEMANTIC,
    MemoryRecordKind.DECISION: MemoryTier.SEMANTIC,
    MemoryRecordKind.CONVENTION: MemoryTier.SEMANTIC,
    MemoryRecordKind.FAILURE_LEARNING: MemoryTier.SEMANTIC,
    MemoryRecordKind.RESEARCH: MemoryTier.SEMANTIC,
    MemoryRecordKind.PROCEDURAL_SNAPSHOT: MemoryTier.PROCEDURAL,
}

_JSON_DIR_BY_KIND: Mapping[MemoryRecordKind, MemoryPathClass] = {
    MemoryRecordKind.SEMANTIC_FACT: MemoryPathClass.SEMANTIC_FACTS_DIR,
    MemoryRecordKind.PREFERENCE: MemoryPathClass.SEMANTIC_PREFERENCES_DIR,
    MemoryRecordKind.DECISION: MemoryPathClass.SEMANTIC_DECISIONS_DIR,
    MemoryRecordKind.CONVENTION: MemoryPathClass.SEMANTIC_CONVENTIONS_DIR,
    MemoryRecordKind.FAILURE_LEARNING: MemoryPathClass.SEMANTIC_FAILURES_DIR,
    MemoryRecordKind.RESEARCH: MemoryPathClass.SEMANTIC_RESEARCH_DIR,
    MemoryRecordKind.PROCEDURAL_SNAPSHOT: MemoryPathClass.PROCEDURAL_SNAPSHOTS_DIR,
}

_JSONL_BY_KIND: Mapping[MemoryRecordKind, MemoryPathClass] = {
    MemoryRecordKind.EPISODIC_TURN: MemoryPathClass.EPISODIC_TURNS_LEDGER,
    MemoryRecordKind.TOOL_EVENT: MemoryPathClass.EPISODIC_TOOL_EVENTS_LEDGER,
    MemoryRecordKind.COMPACTION_EVENT: MemoryPathClass.EPISODIC_COMPACTIONS_LEDGER,
}


class CanonicalMemoryStore:
    """Filesystem-backed canonical memory store."""

    def __init__(
        self,
        *,
        root_binding: MemoryRootBinding | None = None,
        deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
        derived_index_hook: DerivedIndexInvalidationHook | None = None,
    ) -> None:
        self._registry = MemoryPathRegistry(root_binding)
        self._deployment_surface = deployment_surface
        self._derived_index_hook = derived_index_hook

    def record_path(self, record: MemoryStoreRecord) -> Path:
        """Return the canonical path used to store an envelope-backed record."""

        return self._path_for_record(
            record.envelope.kind,
            memory_id=record.envelope.memory_id,
            run_id=_run_id_from_record(record),
        )

    def write_record(self, record: MemoryStoreRecord) -> MemoryStoreWriteResult:
        """Write one envelope-backed record, then invalidate derived indexes."""

        payload = canonicalize_memory_store_record(record)
        path = self.record_path(record)
        if record.envelope.kind in _JSONL_BY_KIND:
            _append_jsonl(path, payload)
        else:
            _write_file_atomically(path, payload)
        self._mark_semantic_index_stale(record)
        return MemoryStoreWriteResult.WRITTEN

    def read_record(
        self,
        memory_id: MemoryID,
        kind: MemoryRecordKind,
        *,
        run_id: str | None = None,
        audit_mode: bool = False,
    ) -> MemoryStoreRecord:
        """Read one envelope-backed record by id and kind."""

        _tier_for_kind(kind)
        path = self._path_for_record(kind, memory_id=memory_id, run_id=run_id)
        if kind in _JSONL_BY_KIND:
            record = _read_jsonl_record(path, memory_id)
        else:
            record = _read_json_record(path)
        if record.envelope.kind != kind:
            raise MemoryStoreRecordUnavailableError(
                f"record {memory_id!s} has kind {record.envelope.kind.value}, not {kind.value}"
            )
        if record.envelope.redaction_state is not RedactionState.ACTIVE and not audit_mode:
            raise MemoryStoreRecordUnavailableError(
                f"record {memory_id!s} is {record.envelope.redaction_state.value}"
            )
        return record

    def memory_operation_ledger_path(self) -> Path:
        """Return the canonical durable memory operation ledger path."""

        return self._registry.resolve_path(
            MemoryPathClass.DURABLE_MEMORY_OPS_LEDGER,
            self._deployment_surface,
        )

    def append_memory_operation(
        self,
        payload: MemoryOperationPayload,
    ) -> MemoryOperationWriteResult:
        """Append a durable memory operation through the U-MEM-03 ledger format."""

        return append_memory_operation(self._memory_operation_handle(), payload)

    def read_memory_operations(self) -> list[MemoryOperationEntry]:
        """Read durable memory operation ledger entries."""

        return read_memory_operation_ledger(self._memory_operation_handle())

    def _path_for_record(
        self,
        kind: MemoryRecordKind,
        *,
        memory_id: MemoryID,
        run_id: str | None,
    ) -> Path:
        if kind is MemoryRecordKind.EPISODIC_RUN:
            return self._registry.resolve_path(
                MemoryPathClass.EPISODIC_RUN_RECORD,
                self._deployment_surface,
                run_id=_required_run_id(kind, run_id),
            )
        if kind in _JSONL_BY_KIND:
            return self._registry.resolve_path(
                _JSONL_BY_KIND[kind],
                self._deployment_surface,
                run_id=_required_run_id(kind, run_id),
            )
        if kind in _JSON_DIR_BY_KIND:
            directory = self._registry.resolve_path(
                _JSON_DIR_BY_KIND[kind],
                self._deployment_surface,
            )
            return directory / _record_filename(memory_id)
        _tier_for_kind(kind)
        raise AssertionError(f"unreachable kind mapping for {kind.value}")

    def _memory_operation_handle(self) -> JsonlLedgerHandle:
        path = self.memory_operation_ledger_path()
        entry_count = 0
        if path.exists():
            entry_count = sum(1 for line in path.read_text().splitlines() if line.strip())
        return JsonlLedgerHandle(
            canonical_path=path,
            exists=path.exists(),
            entry_count=entry_count,
        )

    def _mark_semantic_index_stale(self, record: MemoryStoreRecord) -> None:
        event = DerivedIndexInvalidation(
            memory_id=record.envelope.memory_id,
            record_kind=record.envelope.kind,
            content_hash=record.envelope.content_hash.hex(),
        )
        path = self._registry.resolve_path(
            MemoryPathClass.SEMANTIC_INDEX_LEDGER,
            self._deployment_surface,
        )
        _append_jsonl(path, _canonical_json_bytes(_normalize_for_store_json(event)))
        if self._derived_index_hook is not None:
            self._derived_index_hook(event)


def canonicalize_memory_store_record(record: MemoryStoreRecord) -> bytes:
    """Serialize an envelope-backed memory record to deterministic JSON bytes."""

    return _canonical_json_bytes(
        _normalize_for_store_json(
            {
                "envelope": record.envelope,
                "content": record.content,
            }
        )
    )


def _tier_for_kind(kind: MemoryRecordKind) -> MemoryTier:
    try:
        return _TIER_BY_KIND[kind]
    except KeyError as exc:
        if kind is MemoryRecordKind.MEMORY_OPERATION:
            raise MemoryStoreUnsupportedKindError(
                "memory_operation records use append_memory_operation/read_memory_operations"
            ) from exc
        raise MemoryStoreUnsupportedKindError(
            f"unsupported memory record kind {kind.value}"
        ) from exc


def _required_run_id(kind: MemoryRecordKind, run_id: str | None) -> str:
    if run_id is None:
        raise ValueError(f"{kind.value} records require run_id")
    return run_id


def _run_id_from_record(record: MemoryStoreRecord) -> str | None:
    if record.envelope.tier is not MemoryTier.EPISODIC:
        return None
    run_id = record.content.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError(f"{record.envelope.kind.value} records require string content.run_id")
    return run_id


def _record_filename(memory_id: MemoryID) -> str:
    return f"{quote(str(memory_id), safe='')}.json"


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _normalize_for_store_json(value: object) -> StoreJSON:
    if isinstance(value, StrEnum):
        return _nfc(value.value)
    if isinstance(value, str):
        return _nfc(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("Memory store canonicalization does not accept float values")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _normalize_for_store_json(cast("Mapping[str, object]", value.model_dump()))
    if isinstance(value, Mapping):
        normalized: dict[str, StoreJSON] = {}
        mapping = cast("Mapping[object, object]", value)
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise TypeError("Memory store canonicalization requires string mapping keys")
            normalized_key = _nfc(key)
            if normalized_key in normalized:
                raise ValueError(
                    f"Memory store payload has duplicate canonical key {normalized_key!r}"
                )
            normalized[normalized_key] = _normalize_for_store_json(item)
        return normalized
    if isinstance(value, Sequence):
        sequence = cast("Sequence[object]", value)
        return [_normalize_for_store_json(item) for item in sequence]
    raise TypeError(f"Unsupported memory store value: {type(value).__name__}")


def _canonical_json_bytes(payload: StoreJSON) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _write_file_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with _FILE_WRITE_LOCK:
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _append_jsonl(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JSONL_WRITE_LOCK:
        with path.open("ab") as fh:
            fh.write(payload + b"\n")


def _read_json_record(path: Path) -> MemoryStoreRecord:
    if not path.exists():
        raise MemoryStoreRecordNotFoundError(f"memory record not found at {path!s}")
    return _deserialize_store_record(json.loads(path.read_text()))


def _read_jsonl_record(path: Path, memory_id: MemoryID) -> MemoryStoreRecord:
    if not path.exists():
        raise MemoryStoreRecordNotFoundError(f"memory record ledger not found at {path!s}")
    matched_record: MemoryStoreRecord | None = None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw_object = json.loads(line)
        if not isinstance(raw_object, dict):
            continue
        raw = cast("dict[str, object]", raw_object)
        envelope = raw.get("envelope")
        if isinstance(envelope, dict):
            envelope_payload = cast("Mapping[str, object]", envelope)
            if envelope_payload.get("memory_id") == memory_id:
                matched_record = _deserialize_store_record(raw)
    if matched_record is not None:
        return matched_record
    raise MemoryStoreRecordNotFoundError(f"memory record {memory_id!s} not found at {path!s}")


def _deserialize_store_record(raw: Mapping[str, object]) -> MemoryStoreRecord:
    envelope_object = raw.get("envelope")
    content_object = raw.get("content")
    if not isinstance(envelope_object, dict):
        raise ValueError("stored memory record is missing envelope object")
    if not isinstance(content_object, dict):
        raise ValueError("stored memory record is missing content object")
    envelope = cast("dict[str, object]", envelope_object)
    content = cast("dict[str, object]", content_object)
    return MemoryStoreRecord.model_validate(
        {
            "envelope": _deserialize_envelope(envelope),
            "content": content,
        }
    )


def _deserialize_envelope(raw: Mapping[str, object]) -> dict[str, object]:
    envelope = dict(raw)
    envelope["content_hash"] = _bytes32_from_json(envelope["content_hash"])
    source_refs: list[dict[str, object]] = []
    source_refs_object = envelope.get("source_refs", ())
    if not isinstance(source_refs_object, Sequence) or isinstance(source_refs_object, str | bytes):
        raise ValueError("stored memory envelope source_refs must be a sequence")
    source_refs_sequence = cast("Sequence[object]", source_refs_object)
    for source_ref in source_refs_sequence:
        if not isinstance(source_ref, dict):
            raise ValueError("stored memory envelope source_ref must be an object")
        source_ref_payload = dict(cast("Mapping[str, object]", source_ref))
        if source_ref_payload.get("content_hash") is not None:
            source_ref_payload["content_hash"] = _bytes32_from_json(
                source_ref_payload["content_hash"]
            )
        source_refs.append(source_ref_payload)
    envelope["source_refs"] = source_refs
    return envelope


def _bytes32_from_json(value: object) -> bytes:
    if isinstance(value, bytes):
        digest = value
    elif isinstance(value, str):
        digest = bytes.fromhex(value)
    else:
        raise TypeError("expected SHA-256 digest hex string")
    if len(digest) != 32:
        raise ValueError("expected SHA-256 digest to be exactly 32 bytes")
    return digest


__all__ = [
    "CanonicalMemoryStore",
    "DerivedIndexInvalidation",
    "DerivedIndexInvalidationHook",
    "MemoryStoreContentHashMismatchError",
    "MemoryStoreRecord",
    "MemoryStoreRecordNotFoundError",
    "MemoryStoreRecordUnavailableError",
    "MemoryStoreUnsupportedKindError",
    "MemoryStoreWriteResult",
    "canonicalize_memory_store_record",
]
