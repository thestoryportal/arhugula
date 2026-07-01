"""Path-binding configuration + loader — U-IS-02 (`path-binding-loader`).

Implements C-IS-01 §1 (canonical filesystem path contract). Supplies the
implementation-time path-binding configuration consumed by the path
resolver: canonical path strings per `(path_class, workflow_class,
deployment_surface)` cell.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2 U-IS-02
(REVISED — R2); Spec_Information_Substrate_v1.md §1 C-IS-01 ("Deferred to
implementation discretion: specific canonical path strings per workflow class
per deployment-surface cell").

R2 carrier re-point: U-IS-02's `workflow_class` / `deployment_surface` signature
positions consume the cross-axis carriers — `WorkloadClass` from `harness-core`
(U-CP-00) and `DeploymentSurface` from `harness-core` (U-CORE-01). The v2.1/v2.2
landed source modelled both as IS-local opaque `str` newtypes (no carrier
existed); per the carrier-map T2 FACTOR-OUT verdict the concepts are
spec-committed and the carriers are canonical, so the local declarations are
deleted and the types re-pointed. `harness-core` is shared substrate — the
import is not an outbound CXA edge (IS = 0 outbound edges holds).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from harness_core import DeploymentSurface, WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_is.path_class_registry import PathClass


class PathBindingEntry(BaseModel):
    """One canonical-path binding for a `(class, workflow, surface)` cell."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path_class: PathClass
    workflow_class: WorkloadClass
    deployment_surface: DeploymentSurface
    path: str
    """Canonical path string for this cell (implementation-time discretion)."""


class PathBindingDuplicateError(ValueError):
    """Raised when the binding config declares a triple more than once."""


class PathBinding(BaseModel):
    """Validated path-binding configuration — the resolver's only path source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[PathBindingEntry, ...]


def load_path_binding(raw_entries: Iterable[Mapping[str, object]]) -> PathBinding:
    """Build a validated `PathBinding` from raw configuration records.

    Each raw record is validated into a `PathBindingEntry`. Duplicate
    `(path_class, workflow_class, deployment_surface)` triples are rejected
    — a single triple must bind to exactly one path for the resolver's
    stability invariant (U-IS-02 acceptance #1/#2) to hold.
    """
    entries = tuple(PathBindingEntry.model_validate(record) for record in raw_entries)
    seen: set[tuple[PathClass, WorkloadClass, DeploymentSurface]] = set()
    for entry in entries:
        triple = (entry.path_class, entry.workflow_class, entry.deployment_surface)
        if triple in seen:
            raise PathBindingDuplicateError(f"duplicate path binding for triple {triple!r}")
        seen.add(triple)
    return PathBinding(entries=entries)
