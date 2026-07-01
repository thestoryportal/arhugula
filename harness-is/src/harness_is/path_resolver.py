"""Path-resolver primitive — U-IS-02 (`path-resolver`).

Implements C-IS-01 §1 (canonical filesystem path contract). Resolves a
`(path_class, workflow_class, deployment_surface)` triple to a canonical
filesystem `Path`, consulting only the U-IS-02 path-binding configuration.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2 U-IS-02
(REVISED — R2); Spec_Information_Substrate_v1.md §1 C-IS-01.

Depends on: U-IS-01 (`PathClass`); `harness-core` U-CP-00 (`WorkloadClass`),
U-CORE-01 (`DeploymentSurface`) — the R2 carrier re-point.

Acceptance #5 ("resolver does not produce paths violating the C-IS-02
substrate-residence rule") holds by construction: the resolver performs no
path synthesis — it returns the config-bound path string verbatim. C-IS-01
declares no path-class -> artifact-tier mapping, so substrate-residence
correctness is the responsibility of the path-binding config author; the
resolver introduces no violating transform. No test is mandated for #5 by
the U-IS-02 `Tests:` field.
"""

from __future__ import annotations

from pathlib import Path

from harness_core import DeploymentSurface, WorkloadClass

from harness_is.path_binding import PathBinding
from harness_is.path_class_registry import PathClass


class PathBindingMissingError(KeyError):
    """Raised when no path binding exists for the requested triple.

    Surfaced (rather than a default path) per U-IS-02 acceptance #4 — the
    resolver hard-codes no path strings.
    """


class PathResolver:
    """Resolves path-class triples to canonical paths via a `PathBinding`."""

    def __init__(self, binding: PathBinding) -> None:
        self._map: dict[tuple[PathClass, WorkloadClass, DeploymentSurface], str] = {
            (e.path_class, e.workflow_class, e.deployment_surface): e.path for e in binding.entries
        }

    def resolve_path(
        self,
        path_class: PathClass,
        workflow_class: WorkloadClass,
        deployment_surface: DeploymentSurface,
    ) -> Path:
        """Resolve a triple to its canonical `Path`.

        Deterministic in `(triple, binding)`: repeated calls on the same
        triple return an equal `Path` (acceptance #1); a resolver built from
        the same config across runs returns the same `Path` (acceptance #2);
        a differing `workflow_class` MAY map to a differing path with no
        error (acceptance #3).

        Raises `PathBindingMissingError` if the triple is unbound
        (acceptance #4).
        """
        triple = (path_class, workflow_class, deployment_surface)
        raw_path = self._map.get(triple)
        if raw_path is None:
            raise PathBindingMissingError(f"no path binding for triple {triple!r}")
        return Path(raw_path)
