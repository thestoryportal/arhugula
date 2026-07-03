"""Tests for U-MEM-02 - canonical memory path registry (C-MEM-02)."""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

import pytest
from harness_core import DeploymentSurface
from harness_is.memory_path_registry import (
    DEFAULT_MEMORY_ROOT,
    MEMORY_PATH_REGISTRY,
    MemoryPathClass,
    MemoryPathKind,
    MemoryPathRegistry,
    MemoryPathTraversalError,
    MemoryRootBinding,
)

_SPEC_RELATIVE_PATHS = {
    MemoryPathClass.MANIFEST: Path("manifest.json"),
    MemoryPathClass.POLICY: Path("policy.json"),
    MemoryPathClass.EPISODIC_RUN_RECORD: Path("episodic/runs/run-123/run.json"),
    MemoryPathClass.EPISODIC_TURNS_LEDGER: Path("episodic/runs/run-123/turns.jsonl"),
    MemoryPathClass.EPISODIC_TOOL_EVENTS_LEDGER: Path("episodic/runs/run-123/tool_events.jsonl"),
    MemoryPathClass.EPISODIC_COMPACTIONS_LEDGER: Path("episodic/runs/run-123/compactions.jsonl"),
    MemoryPathClass.EPISODIC_SUMMARIES_DIR: Path("episodic/runs/run-123/summaries"),
    MemoryPathClass.SEMANTIC_FACTS_DIR: Path("semantic/facts"),
    MemoryPathClass.SEMANTIC_PREFERENCES_DIR: Path("semantic/preferences"),
    MemoryPathClass.SEMANTIC_DECISIONS_DIR: Path("semantic/decisions"),
    MemoryPathClass.SEMANTIC_CONVENTIONS_DIR: Path("semantic/conventions"),
    MemoryPathClass.SEMANTIC_FAILURES_DIR: Path("semantic/failures"),
    MemoryPathClass.SEMANTIC_RESEARCH_DIR: Path("semantic/research"),
    MemoryPathClass.SEMANTIC_INDEX_LEDGER: Path("semantic/index.jsonl"),
    MemoryPathClass.PROCEDURAL_SNAPSHOTS_DIR: Path("procedural/snapshots"),
    MemoryPathClass.PROCEDURAL_PROMOTED_DIR: Path("procedural/promoted"),
    MemoryPathClass.DURABLE_MEMORY_OPS_LEDGER: Path("durable/memory_ops.jsonl"),
    MemoryPathClass.DURABLE_PROMOTION_DECISIONS_LEDGER: Path("durable/promotion_decisions.jsonl"),
    MemoryPathClass.DURABLE_INJECTION_DECISIONS_LEDGER: Path("durable/injection_decisions.jsonl"),
    MemoryPathClass.DURABLE_RETRIEVAL_EVENTS_LEDGER: Path("durable/retrieval_events.jsonl"),
}

_INVALID_RUN_IDS = (
    "",
    ".",
    "..",
    "../escaped",
    "/etc/passwd",
    r"..\escaped",
)


def test_memory_registry_covers_every_c_mem_02_path() -> None:
    assert set(MEMORY_PATH_REGISTRY) == set(_SPEC_RELATIVE_PATHS)
    assert len(MemoryPathClass) == 20

    registry = MemoryPathRegistry()
    for path_class, relative_path in _SPEC_RELATIVE_PATHS.items():
        kwargs = {"run_id": "run-123"} if MEMORY_PATH_REGISTRY[path_class].requires_run_id else {}
        assert (
            registry.resolve_path(
                path_class,
                DeploymentSurface.LOCAL_DEVELOPMENT,
                **kwargs,
            )
            == DEFAULT_MEMORY_ROOT / relative_path
        )


def test_run_scoped_paths_require_run_id() -> None:
    registry = MemoryPathRegistry()

    with pytest.raises(ValueError, match="requires run_id"):
        registry.resolve_path(
            MemoryPathClass.EPISODIC_RUN_RECORD,
            DeploymentSurface.LOCAL_DEVELOPMENT,
        )


def test_non_run_paths_reject_unexpected_run_id() -> None:
    registry = MemoryPathRegistry()

    with pytest.raises(ValueError, match="does not accept run_id"):
        registry.resolve_path(
            MemoryPathClass.MANIFEST,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            run_id="run-123",
        )


@pytest.mark.parametrize(
    "run_id",
    _INVALID_RUN_IDS,
)
def test_invalid_run_id_is_rejected(run_id: str) -> None:
    registry = MemoryPathRegistry()

    with pytest.raises(MemoryPathTraversalError):
        registry.resolve_path(
            MemoryPathClass.EPISODIC_RUN_RECORD,
            DeploymentSurface.LOCAL_DEVELOPMENT,
            run_id=run_id,
        )


@pytest.mark.parametrize("run_id", _INVALID_RUN_IDS)
def test_ensure_canonical_roots_rejects_invalid_run_id(
    tmp_path: Path,
    run_id: str,
) -> None:
    registry = MemoryPathRegistry(MemoryRootBinding(default_root=tmp_path / "memory"))

    with pytest.raises(MemoryPathTraversalError):
        registry.ensure_canonical_roots(
            DeploymentSurface.LOCAL_DEVELOPMENT,
            run_id=run_id,
        )


def test_deployment_surface_remapping_preserves_logical_relative_path(tmp_path: Path) -> None:
    local_root = tmp_path / "local-memory"
    managed_root = tmp_path / "managed-memory"
    registry = MemoryPathRegistry(
        MemoryRootBinding(
            default_root=local_root,
            surface_roots={DeploymentSurface.MANAGED_CLOUD: managed_root},
        )
    )

    local_path = registry.resolve_path(
        MemoryPathClass.DURABLE_MEMORY_OPS_LEDGER,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    managed_path = registry.resolve_path(
        MemoryPathClass.DURABLE_MEMORY_OPS_LEDGER,
        DeploymentSurface.MANAGED_CLOUD,
    )

    assert local_path == local_root / "durable/memory_ops.jsonl"
    assert managed_path == managed_root / "durable/memory_ops.jsonl"
    assert local_path.relative_to(local_root) == managed_path.relative_to(managed_root)


def test_run_scoped_surface_remapping_preserves_template_parts(tmp_path: Path) -> None:
    local_root = tmp_path / "local-memory"
    managed_root = tmp_path / "managed-memory"
    registry = MemoryPathRegistry(
        MemoryRootBinding(
            default_root=local_root,
            surface_roots={DeploymentSurface.MANAGED_CLOUD: managed_root},
        )
    )

    local_path = registry.resolve_path(
        MemoryPathClass.EPISODIC_RUN_RECORD,
        DeploymentSurface.LOCAL_DEVELOPMENT,
        run_id="run-123",
    )
    managed_path = registry.resolve_path(
        MemoryPathClass.EPISODIC_RUN_RECORD,
        DeploymentSurface.MANAGED_CLOUD,
        run_id="run-123",
    )

    assert local_path == local_root / "episodic/runs/run-123/run.json"
    assert managed_path == managed_root / "episodic/runs/run-123/run.json"
    assert local_path.relative_to(local_root) == managed_path.relative_to(managed_root)


def test_ensure_canonical_roots_creates_only_directories(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    registry = MemoryPathRegistry(MemoryRootBinding(default_root=root))

    created = registry.ensure_canonical_roots(
        DeploymentSurface.LOCAL_DEVELOPMENT,
        run_id="run-123",
    )

    expected_dirs = {
        root,
        root / "episodic",
        root / "episodic/runs",
        root / "episodic/runs/run-123",
        root / "episodic/runs/run-123/summaries",
        root / "semantic",
        root / "semantic/facts",
        root / "semantic/preferences",
        root / "semantic/decisions",
        root / "semantic/conventions",
        root / "semantic/failures",
        root / "semantic/research",
        root / "procedural",
        root / "procedural/snapshots",
        root / "procedural/promoted",
        root / "durable",
    }
    assert set(created) == expected_dirs
    assert all(path.is_dir() for path in expected_dirs)
    assert not (root / "manifest.json").exists()
    assert not (root / "durable/memory_ops.jsonl").exists()


def test_ensure_canonical_roots_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    registry = MemoryPathRegistry(MemoryRootBinding(default_root=root))

    first = registry.ensure_canonical_roots(
        DeploymentSurface.LOCAL_DEVELOPMENT,
        run_id="run-123",
    )
    second = registry.ensure_canonical_roots(
        DeploymentSurface.LOCAL_DEVELOPMENT,
        run_id="run-123",
    )

    assert second == first
    assert all(path.is_dir() for path in second)


def test_ensure_canonical_roots_without_run_id_skips_run_directory(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    registry = MemoryPathRegistry(MemoryRootBinding(default_root=root))

    created = set(registry.ensure_canonical_roots(DeploymentSurface.LOCAL_DEVELOPMENT))

    assert root / "episodic/runs" in created
    assert root / "semantic/facts" in created
    assert root / "procedural/snapshots" in created
    assert root / "durable" in created
    assert not (root / "episodic/runs/run-123").exists()
    assert not (root / "manifest.json").exists()
    assert not (root / "durable/memory_ops.jsonl").exists()


def test_ensure_canonical_roots_materializes_every_registered_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "memory"
    registry = MemoryPathRegistry(MemoryRootBinding(default_root=root))

    created = set(
        registry.ensure_canonical_roots(
            DeploymentSurface.LOCAL_DEVELOPMENT,
            run_id="run-123",
        )
    )

    for metadata in MEMORY_PATH_REGISTRY.values():
        if metadata.kind is not MemoryPathKind.DIRECTORY:
            continue
        kwargs = {"run_id": "run-123"} if metadata.requires_run_id else {}
        assert (
            registry.resolve_path(
                metadata.path_class,
                DeploymentSurface.LOCAL_DEVELOPMENT,
                **kwargs,
            )
            in created
        )


def test_registry_marks_files_and_directories() -> None:
    assert MEMORY_PATH_REGISTRY[MemoryPathClass.MANIFEST].kind is MemoryPathKind.FILE
    assert MEMORY_PATH_REGISTRY[MemoryPathClass.SEMANTIC_FACTS_DIR].kind is MemoryPathKind.DIRECTORY


def test_registry_and_surface_root_bindings_are_immutable(tmp_path: Path) -> None:
    binding = MemoryRootBinding(
        surface_roots={DeploymentSurface.MANAGED_CLOUD: tmp_path / "managed-memory"}
    )
    mutable_registry = cast(MutableMapping[MemoryPathClass, object], MEMORY_PATH_REGISTRY)
    mutable_surface_roots = cast(MutableMapping[DeploymentSurface, Path], binding.surface_roots)

    with pytest.raises(TypeError):
        mutable_registry[MemoryPathClass.MANIFEST] = MEMORY_PATH_REGISTRY[MemoryPathClass.MANIFEST]
    with pytest.raises(TypeError):
        mutable_surface_roots[DeploymentSurface.MANAGED_CLOUD] = tmp_path / "other-memory"
