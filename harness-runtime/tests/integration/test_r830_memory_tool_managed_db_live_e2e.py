"""R-830 — live managed-DB Memory-tool backend end-to-end.

This test is intentionally marked ``e2e`` and skip-gated on an operator-provided
PostgreSQL-compatible connection string. It exercises the real
``MemoryToolStorageBackend.DATABASE`` MANAGED_CLOUD path through the same
dispatch seam as the SQLite and S3 e2es:

  factory (operator binds DATABASE via memory_tool_backend_config)
    → MemoryToolRegistry → resolve_backend → ManagedSqlMemoryToolBackend
    → _invoke_protocol_callback(create / view / str_replace / insert / delete)

Required environment:

- ``R830_MANAGED_DB_CONNECTION_STRING``: a ``postgres://`` or ``postgresql://``
  DSN for a disposable/provisioned managed database.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.factories.memory_tool_registry_factory import (
    materialize_memory_tool_registry_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.memory_tool_dispatch import _invoke_protocol_callback
from harness_runtime.lifecycle.memory_tool_managed_db import ManagedSqlMemoryToolBackend
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryCallbackIOError,
    MemoryToolBackendConfig,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _require_live_managed_db_params() -> dict[str, str]:
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip(
            "R-830 live managed-DB e2e requires psycopg; use `just r830-managed-db-live-e2e`"
        )

    connection_string = os.environ.get("R830_MANAGED_DB_CONNECTION_STRING", "").strip()
    if not connection_string:
        pytest.skip("R-830 live managed-DB e2e requires R830_MANAGED_DB_CONNECTION_STRING")

    if not connection_string.lower().startswith(("postgres://", "postgresql://")):
        pytest.fail(
            "R-830 live managed-DB e2e requires a postgres:// or postgresql:// "
            "R830_MANAGED_DB_CONNECTION_STRING"
        )
    return {"connection_string": connection_string}


def _managed_db_config(*, repository_root: Path, backend_params: dict[str, str]) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
        repository_root=repository_root,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.DATABASE,
            backend_params=backend_params,
        ),
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_managed_db_backend_live_read_write_delete_e2e(tmp_path: Path) -> None:
    params = _require_live_managed_db_params()
    cfg = _managed_db_config(repository_root=tmp_path, backend_params=params)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)
    assert registry.configured_backend is MemoryToolStorageBackend.DATABASE
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, ManagedSqlMemoryToolBackend)

    path = f"/memories/live/{uuid4().hex}.md"
    created = False

    try:
        result, read, written = await _invoke_protocol_callback(
            backend, "create", {"path": path, "file_text": "alpha\nbeta\n"}
        )
        created = True
        assert result == f"created {path}"
        assert read is None
        assert written == len(b"alpha\nbeta\n")

        result, read, written = await _invoke_protocol_callback(backend, "view", {"path": path})
        assert result == "alpha\nbeta\n"
        assert read == len(b"alpha\nbeta\n")
        assert written is None

        await _invoke_protocol_callback(
            backend, "str_replace", {"path": path, "old_str": "beta", "new_str": "gamma"}
        )
        result, _read, _written = await _invoke_protocol_callback(backend, "view", {"path": path})
        assert result == "alpha\ngamma\n"

        await _invoke_protocol_callback(
            backend, "insert", {"path": path, "insert_line": 1, "insert_text": "header\n"}
        )
        result, _read, _written = await _invoke_protocol_callback(backend, "view", {"path": path})
        assert result == "header\nalpha\ngamma\n"

        ctx2 = _MutableHarnessContext()
        registry2 = await materialize_memory_tool_registry_stage(cfg, ctx2)
        backend2 = registry2.resolve_backend(cfg.deployment_surface)
        result, _read, _written = await _invoke_protocol_callback(backend2, "view", {"path": path})
        assert result == "header\nalpha\ngamma\n"

        result, read, written = await _invoke_protocol_callback(backend2, "delete", {"path": path})
        assert result == f"deleted {path}"
        assert read is None
        assert written is None

        with pytest.raises(MemoryCallbackIOError):
            await _invoke_protocol_callback(backend2, "view", {"path": path})
    finally:
        if created:
            try:
                await backend.delete(path)
            except MemoryCallbackIOError:
                pass
