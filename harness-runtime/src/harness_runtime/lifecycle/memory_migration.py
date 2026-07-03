"""Callback-backed memory migration into the canonical memory root.

U-MEM-23 / C-MEM-15 compatibility layer: existing Anthropic Memory callback
backends remain usable as migration sources while canonical writes are explicit
and durable through the native adapter operation ledger.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from harness_is.memory_record_envelope import MemoryID

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryToolStorageBackendProtocol,
)
from harness_runtime.lifecycle.native_memory_adapter import (
    CanonicalNativeMemoryToolBackend,
    normalize_native_memory_path,
)

__all__ = [
    "CallbackMemoryMigrationService",
    "MemoryMigrationItem",
    "MemoryMigrationItemStatus",
    "MemoryMigrationReport",
]


class MemoryMigrationItemStatus(StrEnum):
    """Per-path migration outcome."""

    READY = "ready"
    MIGRATED = "migrated"
    FAILED = "failed"


@dataclass(frozen=True)
class MemoryMigrationItem:
    """Dry-run or apply result for one `/memories/...` path."""

    path: str
    status: MemoryMigrationItemStatus
    bytes_read: int
    content_sha256: str | None
    memory_ref: MemoryID | None = None
    error: str | None = None


@dataclass(frozen=True)
class MemoryMigrationReport:
    """Migration result over an explicit path set."""

    migration_id: str
    source_backend_name: str
    dry_run: bool
    items: tuple[MemoryMigrationItem, ...]


class CallbackMemoryMigrationService:
    """Migrate callback-only memory into a canonical native-memory backend."""

    def __init__(
        self,
        *,
        source_backend: MemoryToolStorageBackendProtocol,
        canonical_backend: CanonicalNativeMemoryToolBackend,
        migration_id: str,
        source_backend_name: str,
    ) -> None:
        self._source_backend = source_backend
        self._canonical_backend = canonical_backend
        self._migration_id = migration_id
        self._source_backend_name = source_backend_name

    async def dry_run(self, paths: Sequence[str]) -> MemoryMigrationReport:
        """Read legacy callback paths and report what would migrate."""

        return await self._run(paths, dry_run=True)

    async def migrate(self, paths: Sequence[str]) -> MemoryMigrationReport:
        """Read legacy callback paths and write them into canonical memory."""

        return await self._run(paths, dry_run=False)

    async def _run(
        self,
        paths: Sequence[str],
        *,
        dry_run: bool,
    ) -> MemoryMigrationReport:
        items: list[MemoryMigrationItem] = []
        for path in paths:
            normalized_path = normalize_native_memory_path(path)
            try:
                content = await self._source_backend.view(normalized_path)
            except MemoryCallbackIOError as exc:
                items.append(
                    MemoryMigrationItem(
                        path=normalized_path,
                        status=MemoryMigrationItemStatus.FAILED,
                        bytes_read=0,
                        content_sha256=None,
                        error=str(exc),
                    )
                )
                continue

            content_sha256 = hashlib.sha256(content).hexdigest()
            if dry_run:
                items.append(
                    MemoryMigrationItem(
                        path=normalized_path,
                        status=MemoryMigrationItemStatus.READY,
                        bytes_read=len(content),
                        content_sha256=content_sha256,
                    )
                )
                continue

            memory_ref = await self._canonical_backend.migrate_from_callback(
                normalized_path,
                content,
                migration_id=self._migration_id,
                source_backend_name=self._source_backend_name,
            )
            items.append(
                MemoryMigrationItem(
                    path=normalized_path,
                    status=MemoryMigrationItemStatus.MIGRATED,
                    bytes_read=len(content),
                    content_sha256=content_sha256,
                    memory_ref=memory_ref,
                )
            )
        return MemoryMigrationReport(
            migration_id=self._migration_id,
            source_backend_name=self._source_backend_name,
            dry_run=dry_run,
            items=tuple(items),
        )
