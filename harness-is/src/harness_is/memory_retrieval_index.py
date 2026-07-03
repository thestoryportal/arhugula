"""Derived memory retrieval index - U-MEM-10.

The canonical semantic/procedural records remain the source of truth. This
module rebuilds the derived ``semantic/index.jsonl`` projection from those
records and provides a bounded metadata retrieval base for later U-MEM-11
ranking and packet assembly.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast

from harness_core import DeploymentSurface
from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness_is.memory_path_registry import (
    MemoryPathClass,
    MemoryPathRegistry,
    MemoryRootBinding,
)
from harness_is.memory_record_envelope import (
    MemoryID,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    RedactionState,
)
from harness_is.memory_store import MemoryStoreRecord

CURRENT_DERIVED_RETRIEVAL_INDEX_VERSION = "derived-retrieval-index/v1"
"""Current rebuildable retrieval-index payload version."""

_REBUILT_EVENT: Literal["rebuilt"] = "rebuilt"
_STALE_EVENT: Literal["stale"] = "stale"
_MAX_SEARCH_TERMS_PER_RECORD = 64
_INACTIVE_STATUSES = {"denied", "expired", "proposed", "superseded", "tombstoned"}

type IndexJSON = str | int | bool | None | list[IndexJSON] | dict[str, IndexJSON]
DerivedRetrievalSearchAccelerator = Callable[
    ["DerivedRetrievalIndexQuery", tuple["DerivedRetrievalIndexEntry", ...]],
    Sequence[str],
]


class DerivedRetrievalIndexMissingError(LookupError):
    """Raised when no rebuilt retrieval index snapshot exists."""


class DerivedRetrievalIndexStaleError(ValueError):
    """Raised when canonical writes happened after the current index rebuild."""


class DerivedRetrievalIndexEntry(BaseModel):
    """One bounded metadata row in the derived retrieval index."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: MemoryID
    record_kind: MemoryRecordKind
    tier: MemoryTier
    scope: MemoryScope
    created_at: datetime
    updated_at: datetime | None = None
    content_hash: str
    redaction_state: RedactionState
    supersedes: tuple[MemoryID, ...] = ()
    superseded_by: tuple[MemoryID, ...] = ()
    status: str | None = None
    confidence: str | None = None
    tags: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if len(self.content_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 hex digest")
        try:
            bytes.fromhex(self.content_hash)
        except ValueError as exc:
            raise ValueError("content_hash must be a SHA-256 hex digest") from exc
        return self


class DerivedRetrievalIndex(BaseModel):
    """Current rebuilt index snapshot plus freshness state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index_version: str
    indexed_at: datetime
    index_hash: str
    entries: tuple[DerivedRetrievalIndexEntry, ...] = ()
    stale: bool = False


class DerivedRetrievalIndexQuery(BaseModel):
    """Metadata-only query over the rebuilt derived retrieval index."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_summary: str = ""
    allowed_kinds: tuple[MemoryRecordKind, ...] = ()
    scope: MemoryScope | None = None
    limit: int = Field(default=20, ge=0)


class DerivedRetrievalIndexResult(BaseModel):
    """Bounded retrieval base returned from the derived index."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index_version: str
    index_hash: str
    selected_refs: tuple[MemoryID, ...]
    entries: tuple[DerivedRetrievalIndexEntry, ...]
    considered_count: int
    stale: bool = False


class _DerivedRetrievalIndexLedgerEvent(BaseModel):
    """Append-only event stored in ``semantic/index.jsonl``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: Literal["rebuilt"]
    index_version: str
    indexed_at: datetime
    index_hash: str
    entries: tuple[DerivedRetrievalIndexEntry, ...]


class DerivedRetrievalIndexStore:
    """Filesystem-backed derived index API for C-MEM-02/C-MEM-11."""

    def __init__(
        self,
        *,
        root_binding: MemoryRootBinding | None = None,
        deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
    ) -> None:
        self._registry = MemoryPathRegistry(root_binding)
        self._deployment_surface = deployment_surface

    def index_path(self) -> Path:
        """Return the canonical derived semantic index ledger path."""

        return self._registry.resolve_path(
            MemoryPathClass.SEMANTIC_INDEX_LEDGER,
            self._deployment_surface,
        )

    def rebuild(self, *, indexed_at: datetime) -> DerivedRetrievalIndex:
        """Rebuild the current index from canonical semantic/procedural records."""

        entries = tuple(_iter_index_entries(self._registry, self._deployment_surface))
        index_hash = _compute_index_hash(
            entries,
            index_version=CURRENT_DERIVED_RETRIEVAL_INDEX_VERSION,
        )
        index = DerivedRetrievalIndex(
            index_version=CURRENT_DERIVED_RETRIEVAL_INDEX_VERSION,
            indexed_at=indexed_at,
            index_hash=index_hash,
            entries=entries,
            stale=False,
        )
        event = _DerivedRetrievalIndexLedgerEvent(
            event=_REBUILT_EVENT,
            index_version=index.index_version,
            indexed_at=index.indexed_at,
            index_hash=index.index_hash,
            entries=index.entries,
        )
        _append_jsonl(self.index_path(), _canonical_json_bytes(event.model_dump(mode="json")))
        return index

    def read_current(self, *, require_fresh: bool = True) -> DerivedRetrievalIndex:
        """Read the last rebuilt index and detect later stale markers."""

        current: DerivedRetrievalIndex | None = None
        stale_after_current = False
        path = self.index_path()
        if not path.exists():
            raise DerivedRetrievalIndexMissingError(f"retrieval index not found at {path!s}")
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            raw_object: object = json.loads(line)
            if not isinstance(raw_object, dict):
                continue
            raw = cast("Mapping[str, object]", raw_object)
            event = raw.get("event")
            if event == _REBUILT_EVENT:
                current = _index_from_event(raw)
                stale_after_current = False
            elif event == _STALE_EVENT and current is not None:
                stale_after_current = True
        if current is None:
            raise DerivedRetrievalIndexMissingError(f"retrieval index not found at {path!s}")
        if stale_after_current and require_fresh:
            raise DerivedRetrievalIndexStaleError("retrieval index is stale after canonical write")
        return current.model_copy(update={"stale": stale_after_current})

    def retrieve(
        self,
        query: DerivedRetrievalIndexQuery,
        *,
        search_accelerator: DerivedRetrievalSearchAccelerator | None = None,
    ) -> DerivedRetrievalIndexResult:
        """Return bounded metadata matches from the current derived index."""

        index = self.read_current()
        candidates = tuple(_filter_candidates(index.entries, query))
        ordered = _order_candidates(query, candidates, search_accelerator)
        selected_entries = tuple(ordered[: query.limit])
        return DerivedRetrievalIndexResult(
            index_version=index.index_version,
            index_hash=index.index_hash,
            selected_refs=tuple(entry.memory_id for entry in selected_entries),
            entries=selected_entries,
            considered_count=len(index.entries),
            stale=index.stale,
        )


def _iter_index_entries(
    registry: MemoryPathRegistry,
    deployment_surface: DeploymentSurface,
) -> tuple[DerivedRetrievalIndexEntry, ...]:
    records: list[MemoryStoreRecord] = []
    for path_class in _INDEXED_RECORD_DIRECTORIES:
        directory = registry.resolve_path(path_class, deployment_surface)
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json"), key=lambda item: item.as_posix()):
            records.append(_read_store_record(path))
    entries = [_entry_from_record(record) for record in records]
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                _tier_sort_key(entry.tier),
                entry.record_kind.value,
                str(entry.memory_id),
            ),
        )
    )


_INDEXED_RECORD_DIRECTORIES = (
    MemoryPathClass.SEMANTIC_FACTS_DIR,
    MemoryPathClass.SEMANTIC_PREFERENCES_DIR,
    MemoryPathClass.SEMANTIC_DECISIONS_DIR,
    MemoryPathClass.SEMANTIC_CONVENTIONS_DIR,
    MemoryPathClass.SEMANTIC_FAILURES_DIR,
    MemoryPathClass.SEMANTIC_RESEARCH_DIR,
    MemoryPathClass.PROCEDURAL_SNAPSHOTS_DIR,
)


def _read_store_record(path: Path) -> MemoryStoreRecord:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"memory record at {path!s} must be a JSON object")
    return MemoryStoreRecord.model_validate(
        {
            "envelope": _deserialize_envelope(cast("Mapping[str, object]", raw["envelope"])),
            "content": raw["content"],
        }
    )


def _deserialize_envelope(raw: Mapping[str, object]) -> dict[str, object]:
    envelope = dict(raw)
    envelope["content_hash"] = _bytes32_from_json(envelope["content_hash"])
    source_refs: list[dict[str, object]] = []
    source_refs_object = envelope.get("source_refs", ())
    if not isinstance(source_refs_object, Sequence) or isinstance(source_refs_object, str | bytes):
        raise ValueError("stored memory envelope source_refs must be a sequence")
    for source_ref in cast("Sequence[object]", source_refs_object):
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


def _entry_from_record(record: MemoryStoreRecord) -> DerivedRetrievalIndexEntry:
    content = record.content
    return DerivedRetrievalIndexEntry(
        memory_id=record.envelope.memory_id,
        record_kind=record.envelope.kind,
        tier=record.envelope.tier,
        scope=record.envelope.scope,
        created_at=record.envelope.created_at,
        updated_at=record.envelope.updated_at,
        content_hash=record.envelope.content_hash.hex(),
        redaction_state=record.envelope.redaction_state,
        supersedes=record.envelope.supersedes,
        superseded_by=record.envelope.superseded_by,
        status=_string_or_none(content.get("status")),
        confidence=_string_or_none(content.get("confidence")),
        tags=_string_tuple(content.get("tags")),
        search_terms=_search_terms_for(record),
    )


def _filter_candidates(
    entries: Sequence[DerivedRetrievalIndexEntry],
    query: DerivedRetrievalIndexQuery,
) -> tuple[DerivedRetrievalIndexEntry, ...]:
    query_terms = set(_tokenize(query.query_summary))
    candidates: list[DerivedRetrievalIndexEntry] = []
    for entry in entries:
        if not _is_active_retrieval_entry(entry):
            continue
        if query.allowed_kinds and entry.record_kind not in query.allowed_kinds:
            continue
        if query.scope is not None and not _scope_matches(entry.scope, query.scope):
            continue
        if query_terms and not (set(entry.search_terms) & query_terms):
            continue
        candidates.append(entry)
    return tuple(candidates)


def _order_candidates(
    query: DerivedRetrievalIndexQuery,
    candidates: tuple[DerivedRetrievalIndexEntry, ...],
    search_accelerator: DerivedRetrievalSearchAccelerator | None,
) -> tuple[DerivedRetrievalIndexEntry, ...]:
    default_order = tuple(sorted(candidates, key=lambda entry: _rank_key(query, entry)))
    if search_accelerator is None:
        return default_order

    by_id = {str(entry.memory_id): entry for entry in default_order}
    accelerator_order: list[DerivedRetrievalIndexEntry] = []
    seen: set[str] = set()
    for memory_id in search_accelerator(query, candidates):
        key = str(memory_id)
        if key in seen or key not in by_id:
            continue
        seen.add(key)
        accelerator_order.append(by_id[key])
    accelerator_order.extend(entry for entry in default_order if str(entry.memory_id) not in seen)
    return tuple(accelerator_order)


def _rank_key(
    query: DerivedRetrievalIndexQuery,
    entry: DerivedRetrievalIndexEntry,
) -> tuple[int, str, str]:
    query_terms = set(_tokenize(query.query_summary))
    match_count = len(query_terms & set(entry.search_terms)) if query_terms else 0
    return (-match_count, _reverse_iso(entry.created_at), str(entry.memory_id))


def _is_active_retrieval_entry(entry: DerivedRetrievalIndexEntry) -> bool:
    if entry.redaction_state is not RedactionState.ACTIVE:
        return False
    if entry.status is not None and entry.status in _INACTIVE_STATUSES:
        return False
    return not entry.superseded_by


def _scope_matches(record_scope: MemoryScope, requested_scope: MemoryScope) -> bool:
    for field_name in (
        "project",
        "workflow",
        "workload_class",
        "provider_family",
        "cli_profile",
        "tenant",
    ):
        requested_value = getattr(requested_scope, field_name)
        if requested_value is not None and getattr(record_scope, field_name) != requested_value:
            return False
    return _visibility_rank(record_scope.visibility) <= _visibility_rank(requested_scope.visibility)


def _visibility_rank(visibility: object) -> int:
    ranks = {
        "private": 0,
        "workflow": 1,
        "project": 2,
        "tenant": 3,
        "public": 4,
    }
    return ranks[str(visibility)]


def _search_terms_for(record: MemoryStoreRecord) -> tuple[str, ...]:
    content = record.content
    values: list[object] = [
        record.envelope.memory_id,
        record.envelope.kind,
        record.envelope.tier,
        record.envelope.scope.project,
        record.envelope.scope.workflow,
        record.envelope.scope.workload_class,
        record.envelope.scope.provider_family,
        record.envelope.scope.cli_profile,
        record.envelope.scope.tenant,
        content.get("semantic_kind"),
        content.get("statement"),
        content.get("confidence"),
        content.get("status"),
        content.get("injection_policy"),
        content.get("snapshot_id"),
        content.get("workflow_id"),
        content.get("cli_profile"),
        content.get("procedural_update"),
        content.get("tags"),
    ]
    terms: set[str] = set()
    for value in values:
        terms.update(_tokenize_value(value))
    return tuple(sorted(terms)[:_MAX_SEARCH_TERMS_PER_RECORD])


def _tokenize_value(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, StrEnum):
        return _tokenize(value.value)
    if isinstance(value, str):
        return _tokenize(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        terms: list[str] = []
        sequence = cast("Sequence[object]", value)
        for item in sequence:
            terms.extend(_tokenize_value(item))
        return tuple(terms)
    return ()


def _tokenize(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value).lower()
    return tuple(re.findall(r"[a-z0-9_:-]+", normalized))


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        return value
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    sequence = cast("Sequence[object]", value)
    return tuple(item for item in sequence if isinstance(item, str))


def _compute_index_hash(
    entries: tuple[DerivedRetrievalIndexEntry, ...],
    *,
    index_version: str,
) -> str:
    payload = {
        "index_version": index_version,
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _index_from_event(raw: Mapping[str, object]) -> DerivedRetrievalIndex:
    event = _DerivedRetrievalIndexLedgerEvent.model_validate(raw)
    index = DerivedRetrievalIndex(
        index_version=event.index_version,
        indexed_at=event.indexed_at,
        index_hash=event.index_hash,
        entries=event.entries,
        stale=False,
    )
    expected_hash = _compute_index_hash(index.entries, index_version=index.index_version)
    if index.index_hash != expected_hash:
        raise ValueError("stored retrieval index hash does not match entries")
    return index


def _tier_sort_key(tier: MemoryTier) -> int:
    return {MemoryTier.SEMANTIC: 0, MemoryTier.PROCEDURAL: 1}.get(tier, 99)


def _reverse_iso(value: datetime) -> str:
    return "".join(chr(255 - ord(character)) for character in value.isoformat())


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


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        cast("IndexJSON", payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _append_jsonl(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(payload + b"\n")


__all__ = [
    "CURRENT_DERIVED_RETRIEVAL_INDEX_VERSION",
    "DerivedRetrievalIndex",
    "DerivedRetrievalIndexEntry",
    "DerivedRetrievalIndexMissingError",
    "DerivedRetrievalIndexQuery",
    "DerivedRetrievalIndexResult",
    "DerivedRetrievalIndexStaleError",
    "DerivedRetrievalIndexStore",
    "DerivedRetrievalSearchAccelerator",
]
