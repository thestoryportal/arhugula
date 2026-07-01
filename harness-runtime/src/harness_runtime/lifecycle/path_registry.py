"""U-RT-10 — Path-class registry materialization.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 (C-RT-02 stage 1 IS post-conditions)
and Phase 2 Session 3 plan v2.1 §2 L2, this module:

- Constructs the `harness_is.PathResolver` from a `PathBindingConfig`.
- Resolves every `PathClass` member for a given `(workflow_class,
  deployment_surface)` pair against the resolver.
- Idempotently creates each resolved path on the filesystem.

`PATH_CLASS_REGISTRY` (C-IS-01 §1) is a module-level immutable constant in
`harness_is.path_class_registry`; we consume it to enumerate the 4 PathClass
members and assert the operator binding covers them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.path_class_registry import PATH_CLASS_REGISTRY, PathClass
from harness_is.path_resolver import PathResolver

from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.types import PathBindingConfig

__all__ = [
    "MaterializedPathRegistry",
    "PathClassUnresolvedError",
    "materialize_path_registry",
]


class PathClassUnresolvedError(Exception):
    """Raised when the binding does not cover one of the four PathClass members."""

    def __init__(self, path_class: PathClass) -> None:
        super().__init__(
            f"PathBinding has no entry for {path_class.value!r} at the requested "
            f"(workflow_class, deployment_surface) cell"
        )
        self.path_class = path_class


@dataclass(frozen=True)
class MaterializedPathRegistry:
    """Result of `materialize_path_registry`: the resolver + the resolved paths."""

    resolver: PathResolver
    """Wraps the validated `PathBinding`; passes through to `PathResolver(binding)`."""

    resolved_paths: dict[PathClass, Path]
    """Map from every `PathClass` member to its resolved + filesystem-created path."""


def materialize_path_registry(
    config: PathBindingConfig,
    *,
    workflow_class: WorkloadClass,
    deployment_surface: DeploymentSurface,
) -> MaterializedPathRegistry:
    """Materialize the path registry for the runtime's stage 1 IS bootstrap.

    Steps:
    1. Build a validated `PathBinding` from `config.raw_entries`.
    2. Construct `PathResolver(binding)`.
    3. For each member of `PATH_CLASS_REGISTRY` (4 PathClass values), resolve
       the `(path_class, workflow_class, deployment_surface)` triple.
    4. Idempotently create each resolved filesystem path
       (`Path.mkdir(parents=True, exist_ok=True)`).

    Parameters
    ----------
    config :
        L1-enriched `PathBindingConfig` with `raw_entries` covering all four
        PathClass members at the `(workflow_class, deployment_surface)` cell.
    workflow_class :
        The runtime's current workload class (typically inherited from
        `RuntimeConfig` consumers; opt-ins live separately at `config.opt_ins`).
    deployment_surface :
        The runtime's deployment surface (carries from `RuntimeConfig`).

    Returns
    -------
    MaterializedPathRegistry
        Frozen handle carrying the resolver + every resolved path.

    Raises
    ------
    PathClassUnresolvedError
        Binding doesn't cover one of the four PathClass members at the given
        `(workflow_class, deployment_surface)` cell.
    """
    binding = build_path_binding(config)
    resolver = PathResolver(binding)

    resolved: dict[PathClass, Path] = {}
    for path_class in PATH_CLASS_REGISTRY:
        try:
            resolved_path = resolver.resolve_path(
                path_class,
                workflow_class,
                deployment_surface,
            )
        except KeyError as exc:
            raise PathClassUnresolvedError(path_class) from exc

        # Idempotent fs create (parents=True for nested paths).
        resolved_path.mkdir(parents=True, exist_ok=True)
        resolved[path_class] = resolved_path

    return MaterializedPathRegistry(resolver=resolver, resolved_paths=resolved)
