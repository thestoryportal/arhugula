"""Tests for U-MEM-10 - derived retrieval indexes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_path_registry import MemoryRootBinding
from harness_is.memory_record_envelope import (
    MemoryRecordEnvelope,
    MemoryRecordKind,
    MemoryScope,
    MemoryTier,
    MemoryVisibility,
    SourceRef,
    SourceRefType,
    compute_memory_content_hash,
    derive_memory_id,
)
from harness_is.memory_retrieval_index import (
    CURRENT_DERIVED_RETRIEVAL_INDEX_VERSION,
    DerivedRetrievalIndexQuery,
    DerivedRetrievalIndexStaleError,
    DerivedRetrievalIndexStore,
)
from harness_is.memory_store import CanonicalMemoryStore, MemoryStoreRecord

_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _binding(tmp_path: Path) -> MemoryRootBinding:
    return MemoryRootBinding(default_root=tmp_path / "memory")


def _store(binding: MemoryRootBinding) -> CanonicalMemoryStore:
    return CanonicalMemoryStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _index_store(binding: MemoryRootBinding) -> DerivedRetrievalIndexStore:
    return DerivedRetrievalIndexStore(
        root_binding=binding,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )


def _scope(
    *,
    workflow: str = "memory-substrate",
    cli_profile: str = "codex",
) -> MemoryScope:
    return MemoryScope(
        project="arhugula-v2",
        workflow=workflow,
        cli_profile=cli_profile,
        visibility=MemoryVisibility.WORKFLOW,
    )


def _record(
    *,
    kind: MemoryRecordKind = MemoryRecordKind.SEMANTIC_FACT,
    statement: str,
    tags: tuple[str, ...] = (),
    created_at: datetime = _NOW,
    status: str = "active",
    scope: MemoryScope | None = None,
) -> MemoryStoreRecord:
    tier = (
        MemoryTier.PROCEDURAL
        if kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT
        else MemoryTier.SEMANTIC
    )
    if kind is MemoryRecordKind.PROCEDURAL_SNAPSHOT:
        content = {
            "snapshot_id": statement.lower().replace(" ", "-"),
            "workflow_id": "memory-substrate",
            "cli_profile": "codex",
            "status": status,
            "procedural_update": statement,
            "tags": list(tags),
        }
    else:
        content = {
            "semantic_kind": kind.value,
            "statement": statement,
            "confidence": "high",
            "status": status,
            "injection_policy": "retrieval_only",
            "tags": list(tags),
        }
    content_hash = compute_memory_content_hash(content)
    return MemoryStoreRecord(
        envelope=MemoryRecordEnvelope(
            memory_id=derive_memory_id(tier, kind, content_hash),
            schema_version="memory-store-record/v1",
            tier=tier,
            kind=kind,
            created_at=created_at,
            source_refs=(SourceRef(ref_type=SourceRefType.OPERATOR, ref="operator:u-mem-10"),),
            scope=scope or _scope(),
            content_hash=content_hash,
        ),
        content=content,
    )


def test_rebuild_writes_current_index_with_version_and_hash(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    fact = _record(statement="Use the indexed memory path for retrieval.", tags=("retrieval",))
    procedure = _record(
        kind=MemoryRecordKind.PROCEDURAL_SNAPSHOT,
        statement="Run the memory loop from a linked worktree.",
        tags=("workflow",),
        created_at=_NOW + timedelta(seconds=1),
    )
    store.write_record(fact)
    store.write_record(procedure)

    rebuilt = _index_store(binding).rebuild(indexed_at=_NOW)
    reread = _index_store(binding).read_current()

    assert rebuilt.index_version == CURRENT_DERIVED_RETRIEVAL_INDEX_VERSION
    assert reread.index_hash == rebuilt.index_hash
    assert reread.stale is False
    assert [entry.memory_id for entry in reread.entries] == [
        fact.envelope.memory_id,
        procedure.envelope.memory_id,
    ]
    assert {entry.record_kind for entry in reread.entries} == {
        MemoryRecordKind.SEMANTIC_FACT,
        MemoryRecordKind.PROCEDURAL_SNAPSHOT,
    }


def test_stale_index_is_detected_after_canonical_write(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    store.write_record(_record(statement="Initial durable memory fact."))
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)

    store.write_record(_record(statement="Later fact makes the derived index stale."))

    stale = index_store.read_current(require_fresh=False)
    assert stale.stale is True
    with pytest.raises(DerivedRetrievalIndexStaleError):
        index_store.read_current()


def test_empty_store_rebuild_returns_valid_empty_retrieval_base(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    index_store = _index_store(binding)

    rebuilt = index_store.rebuild(indexed_at=_NOW)
    result = index_store.retrieve(DerivedRetrievalIndexQuery(query_summary="anything"))

    assert rebuilt.entries == ()
    assert rebuilt.stale is False
    assert result.index_hash == rebuilt.index_hash
    assert result.selected_refs == ()
    assert result.considered_count == 0


def test_large_store_retrieval_uses_bounded_index_metadata(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    for index in range(80):
        store.write_record(
            _record(
                statement=f"Unrelated memory record {index}.",
                tags=("filler",),
                created_at=_NOW + timedelta(seconds=index),
            )
        )
    target = _record(
        statement="Derived retrieval indexes must select bounded metadata.",
        tags=("needle", "retrieval"),
        created_at=_NOW + timedelta(seconds=100),
    )
    store.write_record(target)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)
    for path in (tmp_path / "memory" / "semantic" / "facts").glob("*.json"):
        path.unlink()

    result = index_store.retrieve(
        DerivedRetrievalIndexQuery(
            query_summary="needle retrieval",
            allowed_kinds=(MemoryRecordKind.SEMANTIC_FACT,),
            scope=_scope(),
            limit=3,
        )
    )

    assert result.selected_refs == (target.envelope.memory_id,)
    assert result.considered_count == 81
    assert len(result.entries) == 1
    assert result.entries[0].search_terms
    assert "statement" not in result.entries[0].model_dump()


def test_search_accelerator_is_non_authoritative(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _store(binding)
    first = _record(statement="First indexed fact.", tags=("one",), created_at=_NOW)
    second = _record(
        statement="Second indexed fact.",
        tags=("two",),
        created_at=_NOW + timedelta(seconds=1),
    )
    store.write_record(first)
    store.write_record(second)
    index_store = _index_store(binding)
    index_store.rebuild(indexed_at=_NOW)

    result = index_store.retrieve(
        DerivedRetrievalIndexQuery(query_summary="indexed", limit=2),
        search_accelerator=lambda _query, _entries: (
            "mem:semantic:semantic_fact:" + "f" * 64,
            first.envelope.memory_id,
        ),
    )

    assert result.selected_refs == (first.envelope.memory_id, second.envelope.memory_id)
