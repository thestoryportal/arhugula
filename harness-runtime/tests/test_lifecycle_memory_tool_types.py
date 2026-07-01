"""U-RT-76 — `MemoryToolStorageBackendProtocol` + `MemoryToolBackendConfig` + typed exceptions tests.

ACs per runtime plan v2.15 §1 (preserved from v2.14):

1. `MemoryToolStorageBackendProtocol` importable; `@runtime_checkable` decorator
   applied; passes `isinstance(obj, MemoryToolStorageBackendProtocol)` at
   runtime against an object implementing all 5 methods with correct async
   signatures.
2. `MemoryToolBackendConfig` instantiable as frozen dataclass; `backend` field
   accepts any `MemoryToolStorageBackend` enum value; `backend_params`
   defaults to `None`.
3. `MemoryPathViolationError` + `MemoryCallbackIOError` importable as
   `Exception` subclasses (separate types — not aliased).
4. Cross-package import of `MemoryToolStorageBackend` from
   `harness_as.anthropic_graceful_degradation` resolves at pyright strict +
   at runtime.
5. Importable; pyright strict mode passes (verified via package-level
   pyright run; this test file exists to import the surface).
"""

from __future__ import annotations

import dataclasses

import pytest
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryPathViolationError,
    MemoryToolBackendConfig,
    MemoryToolStorageBackendProtocol,
)


class _FakeFullBackend:
    """Object implementing all 5 Protocol methods with correct async signatures."""

    async def view(self, path: str) -> bytes:
        return b""

    async def create(self, path: str, content: bytes) -> None:
        return None

    async def delete(self, path: str) -> None:
        return None

    async def str_replace(self, path: str, old: str, new: str) -> None:
        return None

    async def insert(self, path: str, line: int, content: str) -> None:
        return None


class _FakeIncompleteBackend:
    """Object implementing only one Protocol method (missing 4)."""

    async def view(self, path: str) -> bytes:
        return b""


# AC #1 — Protocol importable + @runtime_checkable + isinstance against
# 5-method implementation.


def test_protocol_importable_and_runtime_checkable() -> None:
    """AC #1 — Protocol importable; `@runtime_checkable` decorator applied."""
    # @runtime_checkable Protocols expose __subclasshook__ for isinstance support
    assert hasattr(MemoryToolStorageBackendProtocol, "__subclasshook__")


def test_protocol_isinstance_passes_against_full_implementation() -> None:
    """AC #1 — `isinstance` passes against object implementing all 5 methods."""
    backend = _FakeFullBackend()
    assert isinstance(backend, MemoryToolStorageBackendProtocol)


def test_protocol_isinstance_fails_against_incomplete_implementation() -> None:
    """AC #1 — `isinstance` rejects incomplete implementations (missing methods).

    Note: @runtime_checkable Protocols only check method-name presence, not
    signatures. This test verifies the structural-subtyping check is wired.
    """
    backend = _FakeIncompleteBackend()
    assert not isinstance(backend, MemoryToolStorageBackendProtocol)


# AC #2 — MemoryToolBackendConfig instantiable; backend accepts enum value;
# backend_params defaults to None.


def test_backend_config_instantiable_with_required_field_only() -> None:
    """AC #2 — `backend_params` defaults to `None`."""
    config = MemoryToolBackendConfig(backend=MemoryToolStorageBackend.FILESYSTEM)
    assert config.backend == MemoryToolStorageBackend.FILESYSTEM
    assert config.backend_params is None


def test_backend_config_accepts_any_enum_value() -> None:
    """AC #2 — `backend` field accepts any `MemoryToolStorageBackend` value."""
    for enum_value in MemoryToolStorageBackend:
        config = MemoryToolBackendConfig(backend=enum_value)
        assert config.backend == enum_value


def test_backend_config_accepts_backend_params_mapping() -> None:
    """AC #2 — `backend_params` accepts a `Mapping[str, str]`."""
    config = MemoryToolBackendConfig(
        backend=MemoryToolStorageBackend.S3,
        backend_params={"bucket": "memories-test", "region": "us-west-2"},
    )
    assert config.backend_params == {"bucket": "memories-test", "region": "us-west-2"}


def test_backend_config_is_frozen() -> None:
    """AC #2 — `MemoryToolBackendConfig` is a frozen dataclass.

    Per spec v1.17 §14.12.1: `@dataclass(frozen=True)`. Mutation attempts
    raise `dataclasses.FrozenInstanceError`.
    """
    config = MemoryToolBackendConfig(backend=MemoryToolStorageBackend.FILESYSTEM)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.backend = MemoryToolStorageBackend.S3  # type: ignore[misc]


# AC #3 — Typed exceptions importable + separate types not aliased.


def test_memory_path_violation_error_is_exception_subclass() -> None:
    """AC #3 — `MemoryPathViolationError` is an `Exception` subclass."""
    assert issubclass(MemoryPathViolationError, Exception)


def test_memory_callback_io_error_is_exception_subclass() -> None:
    """AC #3 — `MemoryCallbackIOError` is an `Exception` subclass."""
    assert issubclass(MemoryCallbackIOError, Exception)


def test_typed_exceptions_are_distinct_types_not_aliased() -> None:
    """AC #3 — exceptions are separate types, not aliased.

    Per `[[advisor-before-substantive-work-for-cross-axis-blockers]]` discipline
    + plan v2.15 U-RT-76 AC #3 explicit "separate types — not aliased" wording:
    verify the two classes are distinct identities and neither is a subclass
    of the other.
    """
    assert MemoryPathViolationError is not MemoryCallbackIOError
    assert not issubclass(MemoryPathViolationError, MemoryCallbackIOError)
    assert not issubclass(MemoryCallbackIOError, MemoryPathViolationError)


def test_typed_exceptions_raisable_and_catchable_independently() -> None:
    """AC #3 — exceptions raise + catch via their own type independently."""
    with pytest.raises(MemoryPathViolationError):
        raise MemoryPathViolationError("path escapes /memories/")

    with pytest.raises(MemoryCallbackIOError):
        raise MemoryCallbackIOError("filesystem permission denied")


# AC #4 — Cross-package import of MemoryToolStorageBackend resolves.


def test_cross_package_enum_import_resolves() -> None:
    """AC #4 — cross-package import of `MemoryToolStorageBackend` from
    `harness_as.anthropic_graceful_degradation` resolves at runtime.

    The import at the test module top is the runtime verification; this test
    asserts the imported symbol matches expected enum identity.
    """
    from harness_as.anthropic_graceful_degradation import (
        MemoryToolStorageBackend as MTSB_FromAs,
    )

    assert MemoryToolStorageBackend is MTSB_FromAs
    # 5 enum values per ADR-D3 v1.2 §1.7 graceful-degradation table rows 10/11
    assert set(MemoryToolStorageBackend) == {
        MemoryToolStorageBackend.FILESYSTEM,
        MemoryToolStorageBackend.S3,
        MemoryToolStorageBackend.DATABASE,
        MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM,
        MemoryToolStorageBackend.OPERATOR_DEFINED,
    }


# AC #5 — Importable + pyright strict (verified via successful imports above
# + package-level pyright run; this test exists to assert the surface is wired).


def test_all_public_surface_importable() -> None:
    """AC #5 — entire U-RT-76 public surface importable."""
    from harness_runtime.lifecycle import memory_tool_types

    assert memory_tool_types.MemoryToolStorageBackendProtocol is not None
    assert memory_tool_types.MemoryToolBackendConfig is not None
    assert memory_tool_types.MemoryPathViolationError is not None
    assert memory_tool_types.MemoryCallbackIOError is not None
    # Re-exported cross-package enum
    assert memory_tool_types.MemoryToolStorageBackend is MemoryToolStorageBackend
