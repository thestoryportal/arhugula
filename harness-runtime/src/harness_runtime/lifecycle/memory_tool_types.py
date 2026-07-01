"""U-RT-76 — Memory tool type carriers (Protocol + sub-model + typed exceptions).

Implements runtime spec v1.17 §14.12.1 (architectural surfaces introduced):

- `MemoryToolStorageBackendProtocol`: PEP-544 @runtime_checkable Protocol with
  5 async CRUD callbacks per ADR-D3 v1.2 §1.1 #11 filesystem-style interface
  foundation; 5-callback enumeration source is runtime spec §14.12.1
  (informed by Anthropic SDK `BetaAbstractMemoryTool` helper enumeration).
- `MemoryToolBackendConfig`: operator-supplied storage-backend selection
  override; consumed at `materialize_memory_tool_registry_stage` factory
  per §14.12.3.
- `MemoryPathViolationError` + `MemoryCallbackIOError`: typed exceptions
  consumed at C-RT-15 §14.5.1 callback-injection composer-step for fail-class
  propagation per C-RT-22 §14.12.4
  (`RT-FAIL-MEMORY-PATH-VIOLATION` permanent + `RT-FAIL-MEMORY-CALLBACK-IO`
  transient).

Per L9-octies cluster discipline (runtime plan v2.15 §1):
- L0 entry-point (no within-cluster deps).
- Cross-package import of `MemoryToolStorageBackend` from
  `harness_as.anthropic_graceful_degradation` (already-landed carrier;
  ZERO new CXA edge per fork doc §5 + architect §13.6.D).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend

__all__ = [
    "MemoryBackendResolutionError",
    "MemoryCallbackIOError",
    "MemoryPathViolationError",
    "MemoryToolBackendConfig",
    "MemoryToolStorageBackend",
    "MemoryToolStorageBackendProtocol",
]


@runtime_checkable
class MemoryToolStorageBackendProtocol(Protocol):
    """Storage-backend contract for the Anthropic Memory tool client-side primitive.

    Per ADR-D3 v1.2 §1.1 #11 + runtime spec v1.17 §14.12.1: harness implements
    the storage backend; filesystem-style interface in `/memories` paths Claude
    controls; operations are 5 CRUD callbacks invoked by the SDK tool-use →
    tool-result inner loop per C-RT-15 §14.5.1.
    """

    async def view(self, path: str) -> bytes:
        """Read the content of `/memories/{path}`.

        Raises `MemoryPathViolationError` if path escapes `/memories/` scope.
        Raises `MemoryCallbackIOError` on storage-backend I/O failure.
        """
        ...

    async def create(self, path: str, content: bytes) -> None:
        """Create `/memories/{path}` with `content`.

        Overwrites if exists. Same exception discipline as `view`.
        """
        ...

    async def delete(self, path: str) -> None:
        """Delete `/memories/{path}`.

        No-op if absent. Same exception discipline as `view`.
        """
        ...

    async def str_replace(self, path: str, old: str, new: str) -> None:
        """Replace `old` substring with `new` in `/memories/{path}`.

        Raises `MemoryCallbackIOError` if `old` not found.
        Same exception discipline as `view`.
        """
        ...

    async def insert(self, path: str, line: int, content: str) -> None:
        """Insert `content` at `line` in `/memories/{path}`.

        1-indexed lines per Anthropic Memory tool convention.
        Same exception discipline as `view`.
        """
        ...


@dataclass(frozen=True)
class MemoryToolBackendConfig:
    """Operator-supplied Memory tool storage-backend selection override.

    Optional at `RuntimeConfig.memory_tool_backend_config` (absent / `None`
    defers to graceful-degradation resolver at
    `harness_as.anthropic_graceful_degradation.memory_tool_storage_backend`).
    Present forces the named backend regardless of deployment surface.

    `backend_params` carries per-backend connection params (e.g., S3 bucket
    name, encryption-key reference). Structure-not-content discipline applies:
    values are opaque-to-harness configuration strings; the harness does not
    introspect or redact their content beyond OD-13..16 redaction discipline
    at span-attribute emission boundaries.
    """

    backend: MemoryToolStorageBackend
    backend_params: Mapping[str, str] | None = None


class MemoryPathViolationError(Exception):
    """Callback path arg escapes `/memories/` scope.

    Raised by backend implementations BEFORE I/O attempt per runtime spec
    v1.17 §14.12.5 invariant 3 (path discipline enforced at backend).
    Maps to `RT-FAIL-MEMORY-PATH-VIOLATION` (permanent) at C-RT-15 §14.5.1
    fail-class propagation per §14.12.4.

    Examples of escaping paths: path traversal `..`; absolute paths outside
    `/memories/`; paths not prefixed with `/memories/`.
    """


class MemoryCallbackIOError(Exception):
    """Storage-backend callback I/O failure.

    Raised by backend implementations on storage-backend I/O exceptions
    (filesystem permission denied; S3 5xx; database connection failure;
    `str_replace` `old` substring not found; etc.). Maps to
    `RT-FAIL-MEMORY-CALLBACK-IO` (transient) at C-RT-15 §14.5.1 fail-class
    propagation per §14.12.4.

    Per §14.12.2 invariant 4: no retry inside the callback boundary; retry
    MAY be wrapped at C-RT-15 dispatcher level (implementation discretion).
    """


class MemoryBackendResolutionError(Exception):
    """`materialize_memory_tool_registry_stage` cannot resolve or construct
    the storage-backend implementation.

    Maps to `RT-FAIL-MEMORY-BACKEND-RESOLUTION` (permanent — bootstrap
    aborts fail-closed per ADR-F4 v1.1 §Consequences (c)) per runtime spec
    v1.17 §14.12.4 fail-class taxonomy. Raised at U-RT-80 factory body when:

    - Required `backend_params` are missing or invalid for the configured
      backend (S3 bucket; managed-DB connection string; ENCRYPTED_FILESYSTEM
      key reference / malformed key; OPERATOR_DEFINED class-qualified-name /
      import / non-class). All 5 `MemoryToolStorageBackend` members are
      implemented (FILESYSTEM, S3, DATABASE, ENCRYPTED_FILESYSTEM,
      OPERATOR_DEFINED) per `B-MEMORY-SURFACE-BACKEND-IMPLS`.
    - Default-path resolver returns a frozenset whose intersection with the
      v2.15 implemented set is empty.
    - Constructed backend object fails `MemoryToolStorageBackendProtocol`
      `@runtime_checkable` introspection (missing one or more of the 5
      required CRUD methods) per §14.12.5 invariant 2.
    """
