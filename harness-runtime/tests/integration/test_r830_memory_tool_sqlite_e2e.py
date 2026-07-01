"""R-830 — SQLite ``DATABASE`` Memory-tool backend end-to-end (no LLM, no creds).

Exercises the full ``DATABASE`` activation path across a workflow lifecycle WITHOUT
a paid LLM (contrast the FILESYSTEM e2e ``test_u_rt_82``, which is
``ANTHROPIC_API_KEY``-gated and LLM-driven). The lifecycle is driven through the
real dispatch command-executor seam (``_invoke_protocol_callback`` — the same
per-``tool_use``-block executor the C-RT-15 inner loop calls), over a registry
bootstrapped by the stage-5 factory with an operator ``DATABASE`` binding:

  factory (operator binds DATABASE via memory_tool_backend_config)
    → MemoryToolRegistry → resolve_backend → SqliteMemoryToolBackend
    → _invoke_protocol_callback(create / view / str_replace / insert / delete)

Closes R-830 must_pass #1 (new backend implements the Protocol), #2 (operator binds
via ``RuntimeConfig.memory_tool_backend_config``), and #3 (read/write/delete e2e
across a workflow lifecycle). The MANAGED_CLOUD cloud-vault / managed-db backend
(S3 / managed DB with real creds) remains a separate operator-gated arc — this is
the SELF_HOSTED_SERVER ``DATABASE`` backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.factories.memory_tool_registry_factory import (
    materialize_memory_tool_registry_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.memory_tool_dispatch import _invoke_protocol_callback
from harness_runtime.lifecycle.memory_tool_sqlite import SqliteMemoryToolBackend
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


def _database_config(*, repository_root: Path, connection_string: str) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
        repository_root=repository_root,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.DATABASE,
            backend_params={"connection_string": connection_string},
        ),
    )


@pytest.mark.asyncio
async def test_sqlite_database_backend_full_lifecycle_e2e(tmp_path: Path) -> None:
    db_path = tmp_path / ".harness" / "memories.db"
    cfg = _database_config(repository_root=tmp_path, connection_string=str(db_path))
    ctx = _MutableHarnessContext()

    # --- bootstrap: operator DATABASE binding -> SQLite backend -------------
    registry = await materialize_memory_tool_registry_stage(cfg, ctx)
    assert registry.configured_backend is MemoryToolStorageBackend.DATABASE
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, SqliteMemoryToolBackend)

    path = "/memories/session/notes.md"

    # --- create (write) ----------------------------------------------------
    result, read, written = await _invoke_protocol_callback(
        backend, "create", {"path": path, "file_text": "alpha\nbeta\n"}
    )
    assert result == f"created {path}"
    assert read is None
    assert written == len(b"alpha\nbeta\n")

    # --- view (read) -------------------------------------------------------
    result, read, written = await _invoke_protocol_callback(backend, "view", {"path": path})
    assert result == "alpha\nbeta\n"
    assert read == len(b"alpha\nbeta\n")

    # --- str_replace (update) ---------------------------------------------
    await _invoke_protocol_callback(
        backend, "str_replace", {"path": path, "old_str": "beta", "new_str": "gamma"}
    )
    result, _read, _written = await _invoke_protocol_callback(backend, "view", {"path": path})
    assert result == "alpha\ngamma\n"

    # --- insert (update) ---------------------------------------------------
    await _invoke_protocol_callback(
        backend, "insert", {"path": path, "insert_line": 1, "insert_text": "header\n"}
    )
    result, _read, _written = await _invoke_protocol_callback(backend, "view", {"path": path})
    assert result == "header\nalpha\ngamma\n"

    # --- DB persistence across a fresh bootstrap (lifecycle boundary) ------
    # A re-materialized registry over the same connection_string sees the
    # accumulated state — proves storage is in the database, not process state.
    ctx2 = _MutableHarnessContext()
    registry2 = await materialize_memory_tool_registry_stage(cfg, ctx2)
    backend2 = registry2.resolve_backend(cfg.deployment_surface)
    result, _read, _written = await _invoke_protocol_callback(backend2, "view", {"path": path})
    assert result == "header\nalpha\ngamma\n"

    # --- delete ------------------------------------------------------------
    result, read, written = await _invoke_protocol_callback(backend2, "delete", {"path": path})
    assert result == f"deleted {path}"

    # --- view after delete propagates MemoryCallbackIOError ----------------
    with pytest.raises(MemoryCallbackIOError):
        await _invoke_protocol_callback(backend2, "view", {"path": path})

    assert db_path.exists()  # real on-disk SQLite database file
