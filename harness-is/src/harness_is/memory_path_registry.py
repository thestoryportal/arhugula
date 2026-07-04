"""Canonical memory path registry - U-MEM-02.

Implements C-MEM-02's provider-neutral filesystem layout under the canonical
memory root. This module only resolves and materializes paths; it does not read
or write memory records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from harness_core import DeploymentSurface

DEFAULT_MEMORY_ROOT = Path(".harness/memory")
"""Default canonical memory root from C-MEM-02."""

_RUN_ID_PLACEHOLDER = "{run_id}"


class MemoryPathTraversalError(ValueError):
    """Raised when a memory path would escape its configured memory root."""


class MemoryPathKind(StrEnum):
    """Filesystem node kind for a canonical memory path."""

    FILE = "file"
    DIRECTORY = "directory"


class MemoryPathClass(StrEnum):
    """Canonical logical path classes declared by C-MEM-02."""

    MANIFEST = "manifest"
    POLICY = "policy"
    EPISODIC_RUN_RECORD = "episodic_run_record"
    EPISODIC_TURNS_LEDGER = "episodic_turns_ledger"
    EPISODIC_TOOL_EVENTS_LEDGER = "episodic_tool_events_ledger"
    EPISODIC_COMPACTIONS_LEDGER = "episodic_compactions_ledger"
    EPISODIC_SUMMARIES_DIR = "episodic_summaries_dir"
    SEMANTIC_FACTS_DIR = "semantic_facts_dir"
    SEMANTIC_PREFERENCES_DIR = "semantic_preferences_dir"
    SEMANTIC_DECISIONS_DIR = "semantic_decisions_dir"
    SEMANTIC_CONVENTIONS_DIR = "semantic_conventions_dir"
    SEMANTIC_FAILURES_DIR = "semantic_failures_dir"
    SEMANTIC_RESEARCH_DIR = "semantic_research_dir"
    SEMANTIC_INDEX_LEDGER = "semantic_index_ledger"
    PROCEDURAL_SNAPSHOTS_DIR = "procedural_snapshots_dir"
    PROCEDURAL_PROMOTED_DIR = "procedural_promoted_dir"
    DURABLE_MEMORY_OPS_LEDGER = "durable_memory_ops_ledger"
    DURABLE_PROMOTION_DECISIONS_LEDGER = "durable_promotion_decisions_ledger"
    DURABLE_INJECTION_DECISIONS_LEDGER = "durable_injection_decisions_ledger"
    DURABLE_RETRIEVAL_EVENTS_LEDGER = "durable_retrieval_events_ledger"


@dataclass(frozen=True)
class MemoryPathMetadata:
    """Registered metadata for one canonical memory path class."""

    path_class: MemoryPathClass
    relative_parts: tuple[str, ...]
    kind: MemoryPathKind

    @property
    def requires_run_id(self) -> bool:
        """Whether the relative path template contains a run-id slot."""

        return _RUN_ID_PLACEHOLDER in self.relative_parts


def _empty_surface_roots() -> Mapping[DeploymentSurface, Path]:
    return {}


@dataclass(frozen=True)
class MemoryRootBinding:
    """Memory-root binding with optional deployment-surface overrides."""

    default_root: Path = DEFAULT_MEMORY_ROOT
    surface_roots: Mapping[DeploymentSurface, Path] = field(default_factory=_empty_surface_roots)

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_roots", MappingProxyType(dict(self.surface_roots)))

    def root_for(self, deployment_surface: DeploymentSurface) -> Path:
        """Return the root configured for a deployment surface."""

        return self.surface_roots.get(deployment_surface, self.default_root)


def _metadata(
    path_class: MemoryPathClass,
    relative_path: str,
    kind: MemoryPathKind,
) -> MemoryPathMetadata:
    return MemoryPathMetadata(path_class, tuple(Path(relative_path).parts), kind)


MEMORY_PATH_REGISTRY = MappingProxyType(
    {
        MemoryPathClass.MANIFEST: _metadata(
            MemoryPathClass.MANIFEST,
            "manifest.json",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.POLICY: _metadata(
            MemoryPathClass.POLICY,
            "policy.json",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.EPISODIC_RUN_RECORD: _metadata(
            MemoryPathClass.EPISODIC_RUN_RECORD,
            "episodic/runs/{run_id}/run.json",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.EPISODIC_TURNS_LEDGER: _metadata(
            MemoryPathClass.EPISODIC_TURNS_LEDGER,
            "episodic/runs/{run_id}/turns.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.EPISODIC_TOOL_EVENTS_LEDGER: _metadata(
            MemoryPathClass.EPISODIC_TOOL_EVENTS_LEDGER,
            "episodic/runs/{run_id}/tool_events.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.EPISODIC_COMPACTIONS_LEDGER: _metadata(
            MemoryPathClass.EPISODIC_COMPACTIONS_LEDGER,
            "episodic/runs/{run_id}/compactions.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.EPISODIC_SUMMARIES_DIR: _metadata(
            MemoryPathClass.EPISODIC_SUMMARIES_DIR,
            "episodic/runs/{run_id}/summaries",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_FACTS_DIR: _metadata(
            MemoryPathClass.SEMANTIC_FACTS_DIR,
            "semantic/facts",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_PREFERENCES_DIR: _metadata(
            MemoryPathClass.SEMANTIC_PREFERENCES_DIR,
            "semantic/preferences",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_DECISIONS_DIR: _metadata(
            MemoryPathClass.SEMANTIC_DECISIONS_DIR,
            "semantic/decisions",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_CONVENTIONS_DIR: _metadata(
            MemoryPathClass.SEMANTIC_CONVENTIONS_DIR,
            "semantic/conventions",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_FAILURES_DIR: _metadata(
            MemoryPathClass.SEMANTIC_FAILURES_DIR,
            "semantic/failures",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_RESEARCH_DIR: _metadata(
            MemoryPathClass.SEMANTIC_RESEARCH_DIR,
            "semantic/research",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.SEMANTIC_INDEX_LEDGER: _metadata(
            MemoryPathClass.SEMANTIC_INDEX_LEDGER,
            "semantic/index.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.PROCEDURAL_SNAPSHOTS_DIR: _metadata(
            MemoryPathClass.PROCEDURAL_SNAPSHOTS_DIR,
            "procedural/snapshots",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.PROCEDURAL_PROMOTED_DIR: _metadata(
            MemoryPathClass.PROCEDURAL_PROMOTED_DIR,
            "procedural/promoted",
            MemoryPathKind.DIRECTORY,
        ),
        MemoryPathClass.DURABLE_MEMORY_OPS_LEDGER: _metadata(
            MemoryPathClass.DURABLE_MEMORY_OPS_LEDGER,
            "durable/memory_ops.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.DURABLE_PROMOTION_DECISIONS_LEDGER: _metadata(
            MemoryPathClass.DURABLE_PROMOTION_DECISIONS_LEDGER,
            "durable/promotion_decisions.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.DURABLE_INJECTION_DECISIONS_LEDGER: _metadata(
            MemoryPathClass.DURABLE_INJECTION_DECISIONS_LEDGER,
            "durable/injection_decisions.jsonl",
            MemoryPathKind.FILE,
        ),
        MemoryPathClass.DURABLE_RETRIEVAL_EVENTS_LEDGER: _metadata(
            MemoryPathClass.DURABLE_RETRIEVAL_EVENTS_LEDGER,
            "durable/retrieval_events.jsonl",
            MemoryPathKind.FILE,
        ),
    }
)
"""Immutable canonical memory path registry."""


class MemoryPathRegistry:
    """Resolve canonical memory paths under a deployment-specific root."""

    def __init__(self, binding: MemoryRootBinding | None = None) -> None:
        self._binding = binding or MemoryRootBinding()

    def resolve_path(
        self,
        path_class: MemoryPathClass,
        deployment_surface: DeploymentSurface,
        *,
        run_id: str | None = None,
    ) -> Path:
        """Resolve a logical memory path class to a filesystem path."""

        metadata = MEMORY_PATH_REGISTRY[path_class]
        if metadata.requires_run_id:
            if run_id is None:
                raise ValueError(f"{path_class.value} requires run_id")
            normalized_run_id = _validate_run_id(run_id)
        elif run_id is not None:
            raise ValueError(f"{path_class.value} does not accept run_id")
        else:
            normalized_run_id = None

        parts = _materialize_relative_parts(metadata, normalized_run_id)
        root = self._binding.root_for(deployment_surface)
        return _join_under_root(root, parts)

    def ensure_canonical_roots(
        self,
        deployment_surface: DeploymentSurface,
        *,
        run_id: str | None = None,
    ) -> tuple[Path, ...]:
        """Create canonical memory directories without creating ledger files."""

        normalized_run_id = _validate_run_id(run_id) if run_id is not None else None
        root = self._binding.root_for(deployment_surface)
        directories: set[Path] = set()
        for metadata in MEMORY_PATH_REGISTRY.values():
            directory_parts = _canonical_directory_parts(metadata, normalized_run_id)
            for ancestor_parts in _with_ancestors(directory_parts):
                directories.add(_join_under_root(root, ancestor_parts))
        ordered = tuple(sorted(directories, key=lambda path: (len(path.parts), path.as_posix())))
        for directory in ordered:
            directory.mkdir(parents=True, exist_ok=True)
        return ordered


def _validate_run_id(run_id: str) -> str:
    path = Path(run_id)
    if (
        not run_id
        or path.is_absolute()
        or "/" in run_id
        or "\\" in run_id
        or len(path.parts) != 1
        or path.parts[0] in {".", ".."}
    ):
        raise MemoryPathTraversalError(f"invalid memory run_id {run_id!r}")
    return run_id


def _materialize_relative_parts(
    metadata: MemoryPathMetadata,
    run_id: str | None,
) -> tuple[str, ...]:
    parts: list[str] = []
    for part in metadata.relative_parts:
        if part == _RUN_ID_PLACEHOLDER:
            if run_id is None:
                raise ValueError(f"{metadata.path_class.value} requires run_id")
            parts.append(run_id)
        else:
            parts.append(part)
    return tuple(parts)


def _canonical_directory_parts(
    metadata: MemoryPathMetadata,
    run_id: str | None,
) -> tuple[str, ...]:
    if metadata.requires_run_id and run_id is None:
        run_id_index = metadata.relative_parts.index(_RUN_ID_PLACEHOLDER)
        return metadata.relative_parts[:run_id_index]

    parts = _materialize_relative_parts(metadata, run_id)
    if metadata.kind is MemoryPathKind.FILE:
        return parts[:-1]
    return parts


def _with_ancestors(parts: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(parts[:index] for index in range(len(parts) + 1))


def _join_under_root(root: Path, parts: tuple[str, ...]) -> Path:
    candidate = root.joinpath(*parts)
    root_resolved = root.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise MemoryPathTraversalError(f"memory path {candidate!s} escapes root {root!s}") from exc
    return candidate


__all__ = [
    "DEFAULT_MEMORY_ROOT",
    "MEMORY_PATH_REGISTRY",
    "MemoryPathClass",
    "MemoryPathKind",
    "MemoryPathMetadata",
    "MemoryPathRegistry",
    "MemoryPathTraversalError",
    "MemoryRootBinding",
]
