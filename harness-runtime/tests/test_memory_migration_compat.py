"""Tests for U-MEM-23 migration and compatibility defaults."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_operation_ledger import MemoryOperationKind
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_policy import (
    AccessDecision,
    CaptureDecision,
    MemoryPolicyDocument,
    MemoryPolicyResolver,
    PromotionDecision,
    ReviewMode,
)
from harness_is.memory_record_envelope import MemoryRecordKind, MemoryScope, MemoryVisibility
from harness_is.memory_store import CanonicalMemoryStore
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.memory_migration import (
    CallbackMemoryMigrationService,
    MemoryMigrationItemStatus,
)
from harness_runtime.lifecycle.memory_tool_registry import MemoryToolRegistry
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryToolStorageBackend,
)
from harness_runtime.lifecycle.native_memory_adapter import CanonicalNativeMemoryToolBackend

_NOW = datetime(2026, 7, 3, 4, 45, 0, tzinfo=UTC)
_POLICY_REF = "policy:u-mem-23"


class _LegacyCallbackBackend:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self._entries = dict(entries)

    async def view(self, path: str) -> bytes:
        try:
            return self._entries[path]
        except KeyError as exc:
            raise MemoryCallbackIOError(f"view({path!r}) failed: not found") from exc

    async def create(self, path: str, content: bytes) -> None:
        self._entries[path] = content

    async def delete(self, path: str) -> None:
        self._entries.pop(path, None)

    async def str_replace(self, path: str, old: str, new: str) -> None:
        current = (await self.view(path)).decode("utf-8")
        if old not in current:
            raise MemoryCallbackIOError(f"str_replace({path!r}): substring {old!r} not found")
        self._entries[path] = current.replace(old, new).encode("utf-8")

    async def insert(self, path: str, line: int, content: str) -> None:
        current = (await self.view(path)).decode("utf-8")
        lines = current.splitlines(keepends=True)
        if line < 1 or line > len(lines) + 1:
            raise MemoryCallbackIOError(f"insert({path!r}, line={line}): out of range")
        lines.insert(line - 1, content)
        self._entries[path] = "".join(lines).encode("utf-8")


def _scope() -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow="memory-substrate",
        workload_class="coding-arc",
        provider_family="anthropic",
        cli_profile="claude",
        visibility=MemoryVisibility.WORKFLOW,
    )


def _policy() -> MemoryPolicyDocument:
    return MemoryPolicyDocument(
        policy_id=_POLICY_REF,
        enabled=True,
        capture_decision=CaptureDecision.CAPTURE_FULL,
        promotion_decision=PromotionDecision.KEEP_EPISODIC,
        retrieval_access=AccessDecision.RETRIEVAL_ONLY,
        native_memory_access=AccessDecision.NATIVE_PROVIDER,
        review_mode=ReviewMode.OPERATOR_REQUIRED,
    )


def _store(tmp_path: Path) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=MemoryRootBinding(default_root=tmp_path / "memory"),
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _canonical_backend(
    store: CanonicalMemoryStore,
) -> CanonicalNativeMemoryToolBackend:
    return CanonicalNativeMemoryToolBackend(
        store=store,
        policy_resolver=MemoryPolicyResolver(_policy()),
        scope=_scope(),
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="codex"),
        run_id="run-u-mem-23",
        step_id="step-migration",
        provider="anthropic",
        model="claude-test",
        cli_profile="claude",
        policy_ref=_POLICY_REF,
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_callback_backend_dry_run_reads_without_canonical_writes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    legacy = _LegacyCallbackBackend({"/memories/project/notes.txt": b"legacy memory\n"})
    registry = MemoryToolRegistry(
        backend=legacy,
        configured_backend=MemoryToolStorageBackend.FILESYSTEM,
    )
    service = CallbackMemoryMigrationService(
        source_backend=registry.resolve_backend(DeploymentSurface.LOCAL_DEVELOPMENT),
        canonical_backend=_canonical_backend(store),
        migration_id="migration:u-mem-23",
        source_backend_name=registry.configured_backend.value,
    )

    report = await service.dry_run(["/memories/project/notes.txt"])

    assert report.dry_run is True
    assert report.migration_id == "migration:u-mem-23"
    assert report.source_backend_name == MemoryToolStorageBackend.FILESYSTEM.value
    assert [item.status for item in report.items] == [MemoryMigrationItemStatus.READY]
    assert report.items[0].path == "/memories/project/notes.txt"
    assert report.items[0].bytes_read == len(b"legacy memory\n")
    assert report.items[0].memory_ref is None
    assert store.read_memory_operations() == []


@pytest.mark.asyncio
async def test_callback_backend_apply_migration_writes_explicit_native_ledger(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    legacy = _LegacyCallbackBackend({"/memories/project/notes.txt": b"legacy memory\n"})
    canonical = _canonical_backend(store)
    service = CallbackMemoryMigrationService(
        source_backend=legacy,
        canonical_backend=canonical,
        migration_id="migration:u-mem-23",
        source_backend_name="legacy-callback",
    )

    report = await service.migrate(["/memories/project/notes.txt"])

    assert report.dry_run is False
    [item] = report.items
    assert item.status is MemoryMigrationItemStatus.MIGRATED
    assert item.memory_ref is not None
    assert await canonical.view("/memories/project/notes.txt") == b"legacy memory\n"

    operations = store.read_memory_operations()
    assert [entry.operation_kind for entry in operations] == [
        MemoryOperationKind.NATIVE_ADAPTER_CALL,
        MemoryOperationKind.NATIVE_ADAPTER_CALL,
    ]
    [migrate_ref] = operations[0].memory_refs
    record = store.read_record(
        migrate_ref,
        MemoryRecordKind.TOOL_EVENT,
        run_id="run-u-mem-23",
        audit_mode=True,
    )
    assert record.content["command"] == "migrate"
    assert record.content["migration_id"] == "migration:u-mem-23"
    assert record.content["migration_source_backend"] == "legacy-callback"
    assert record.content["memory_path"] == "/memories/project/notes.txt"
