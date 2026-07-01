"""U-RT-78 — `MemoryToolRegistry` class.

Implements runtime spec v1.17 §14.12.1 (`MemoryToolRegistry` class declaration —
`resolve_backend(deployment_surface) → MemoryToolStorageBackendProtocol` +
`configured_backend` property returning `MemoryToolStorageBackend` enum value) +
§14.12.5 invariant 1 (storage-backend resolved exactly once per bootstrap; no
re-resolution at dispatch-time).

Per L9-octies cluster discipline (runtime plan v2.15 §1):
- L1 within-cluster (←U-RT-76); no edge to U-RT-77.
- Constructor receives a pre-resolved backend (or a surface→backend map) +
  enum-value identity from the U-RT-80 factory; registry is a pure carrier
  (no resolution logic — that lives at the factory per §14.12.5 invariant 1).
- `resolve_backend(deployment_surface)` resolves the storage backend FOR the
  given surface from a bootstrap-frozen surface→backend map (R-FS-1 arc B5 —
  realizes the §14.12.1 surface-parametric reading; supersedes the prior MVP
  collapse that ignored the argument). The single-backend constructor
  (`__init__`) binds one backend for EVERY surface (operator-override + the
  historical single-surface shape); `from_surface_map` builds the
  discriminating per-surface map. A surface with no admissible config-free
  backend resolves to a frozen deferred `RT-FAIL-MEMORY-BACKEND-RESOLUTION`
  (raised only if that surface is queried; the active surface always resolves
  or bootstrap aborts).
"""

from __future__ import annotations

from collections.abc import Mapping

from harness_core.deployment_surface import DeploymentSurface

from harness_runtime.lifecycle.memory_tool_types import (
    MemoryBackendResolutionError,
    MemoryToolStorageBackend,
    MemoryToolStorageBackendProtocol,
)

__all__ = ["MemoryToolRegistry"]


class MemoryToolRegistry:
    """Registry binding `MemoryToolStorageBackendProtocol` implementations to
    deployment surfaces.

    Constructed at bootstrap stage 5 by `materialize_memory_tool_registry_stage`
    per §14.12.3 (factory landing at U-RT-80). Holds a bootstrap-frozen
    surface→backend map (R-FS-1 arc B5) realizing the §14.12.1 surface-parametric
    reading: `resolve_backend(surface)` returns the backend FOR that surface, or
    raises `RT-FAIL-MEMORY-BACKEND-RESOLUTION` for a surface with no admissible
    config-free backend. The `configured_backend` enum identifies the
    active-surface backend for span-attribute emission at C-RT-15 §14.5.1
    callback-injection composer-step.

    Per §14.12.5 invariant 1: all resolution happens once, at bootstrap (the
    factory); the registry is a pure carrier and never re-resolves at dispatch.
    """

    def __init__(
        self,
        *,
        backend: MemoryToolStorageBackendProtocol,
        configured_backend: MemoryToolStorageBackend,
    ) -> None:
        """Bind a single pre-resolved backend for EVERY deployment surface.

        The single-backend shape expresses the operator-override semantics
        (`memory_tool_backend_config` forces one backend regardless of surface
        per §14.12.1) and the historical single-surface registry: every
        `resolve_backend(surface)` returns `backend`. For surface-discriminating
        registries, use `from_surface_map`.

        Per §14.12.5 invariant 1: storage-backend resolution is bootstrap-time-
        frozen; re-resolution at dispatch-time is forbidden.
        """
        # Single-backend shape: `resolve_backend` short-circuits to this backend
        # for ANY surface argument (including non-enumerated sentinels on the
        # no-memory dispatch path). `from_surface_map` leaves this `None`.
        self._single_backend: MemoryToolStorageBackendProtocol | None = backend
        self._backends: dict[DeploymentSurface, MemoryToolStorageBackendProtocol] = {}
        self._resolution_errors: dict[DeploymentSurface, str] = {}
        self._configured_backend = configured_backend

    @classmethod
    def from_surface_map(
        cls,
        *,
        backends: Mapping[DeploymentSurface, MemoryToolStorageBackendProtocol],
        resolution_errors: Mapping[DeploymentSurface, str],
        configured_backend: MemoryToolStorageBackend,
    ) -> MemoryToolRegistry:
        """Build a surface-discriminating registry from a bootstrap-resolved map.

        `backends` maps each successfully-resolved surface to its backend
        implementation; `resolution_errors` maps each surface with no admissible
        config-free backend to the frozen `RT-FAIL-MEMORY-BACKEND-RESOLUTION`
        message `resolve_backend` replays when that surface is queried (the two
        keysets are disjoint). `configured_backend` is the active-surface
        backend's enum identity (for §14.5.1 span emission).

        Per §14.12.5 invariant 1: this map IS the once-per-bootstrap resolution
        outcome; `resolve_backend` is a pure lookup that never re-resolves.
        """
        registry = cls.__new__(cls)
        registry._single_backend = None
        registry._backends = dict(backends)
        registry._resolution_errors = dict(resolution_errors)
        registry._configured_backend = configured_backend
        return registry

    def resolve_backend(
        self,
        deployment_surface: DeploymentSurface,
    ) -> MemoryToolStorageBackendProtocol:
        """Return the storage-backend implementation for `deployment_surface`.

        Pure lookup over the bootstrap-frozen surface→backend map (§14.12.5
        invariant 1 — no re-resolution at dispatch-time). A surface with no
        admissible config-free backend replays its frozen
        `RT-FAIL-MEMORY-BACKEND-RESOLUTION` (raised here, but the resolution
        OUTCOME was decided once at bootstrap). The production dispatch path
        always queries the active `config.deployment_surface`, which is
        guaranteed resolved (else bootstrap aborted), so this raise is reachable
        only for an explicitly-queried non-active surface.
        """
        if self._single_backend is not None:
            # Single-backend shape (operator override / historical single-surface):
            # one backend regardless of the surface argument per §14.12.1.
            return self._single_backend
        backend = self._backends.get(deployment_surface)
        if backend is not None:
            return backend
        error = self._resolution_errors.get(deployment_surface)
        if error is not None:
            raise MemoryBackendResolutionError(error)
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: deployment surface "
            f"{deployment_surface!r} was not resolved at bootstrap"
        )

    @property
    def configured_backend(self) -> MemoryToolStorageBackend:
        """The `MemoryToolStorageBackend` enum value identifying the
        active-surface backend.

        Consumed at C-RT-15 §14.5.1 callback-injection composer-step for
        `memory.backend` span-attribute emission per AS spec v1.5 §14.7
        + §14.8 sampling-row at memory.operation spans. Per §14.12.1 the
        production dispatch reads `configured_backend` alongside
        `resolve_backend(config.deployment_surface)`, so the enum identifies
        the backend the active surface resolves to.
        """
        return self._configured_backend
