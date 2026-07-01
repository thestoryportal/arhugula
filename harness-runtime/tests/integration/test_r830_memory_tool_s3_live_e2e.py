"""R-830 — live S3 Memory-tool backend end-to-end.

This test is intentionally marked ``e2e`` and skip-gated on operator-provided
S3 configuration. It exercises the real ``MemoryToolStorageBackend.S3``
activation path through the same dispatch seam as the SQLite e2e:

  factory (operator binds S3 via memory_tool_backend_config)
    → MemoryToolRegistry → resolve_backend → S3MemoryToolBackend
    → _invoke_protocol_callback(create / view / str_replace / insert / delete)

Preferred environment:

- ``R830_S3_BUCKET``: real bucket name.
- ``R830_S3_PROFILE``: AWS CLI profile authenticated with ``aws login`` or
  another boto3-supported profile credential source.

Static-key fallback environment:

- ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / optional
  ``AWS_SESSION_TOKEN``.

Optional environment:

- ``R830_S3_KEY_PREFIX``: prefix for test objects; defaults to
  ``r830-live-e2e``.
- ``R830_S3_REGION``: forwarded to boto3 as ``region_name``.
- ``R830_S3_ENDPOINT_URL``: forwarded to boto3 as ``endpoint_url`` for
  S3-compatible providers.
"""

from __future__ import annotations

import importlib.util
import os
import re
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
from harness_runtime.lifecycle.memory_tool_s3 import S3MemoryToolBackend
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


def _require_live_s3_params() -> dict[str, str]:
    if importlib.util.find_spec("boto3") is None:
        pytest.skip("R-830 live S3 e2e requires boto3; use `just r830-s3-live-e2e`")

    bucket = os.environ.get("R830_S3_BUCKET", "").strip()
    if not bucket:
        pytest.skip("R-830 live S3 e2e requires R830_S3_BUCKET")
    profile_name = os.environ.get("R830_S3_PROFILE", "").strip()
    if not profile_name and "R830_S3_ENDPOINT_URL" not in os.environ:
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        if access_key and not re.fullmatch(r"(AKIA|ASIA)[A-Z0-9]{16}", access_key):
            pytest.fail(
                "R-830 live S3 e2e AWS_ACCESS_KEY_ID is malformed; expected "
                "a 20-character AWS access key id such as AKIA... or ASIA..."
            )

    params = {
        "bucket": bucket,
        "key_prefix": os.environ.get("R830_S3_KEY_PREFIX", "r830-live-e2e").strip("/"),
    }
    for env_name, param_name in (
        ("R830_S3_REGION", "region_name"),
        ("R830_S3_ENDPOINT_URL", "endpoint_url"),
        ("R830_S3_PROFILE", "profile_name"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            params[param_name] = value
            if env_name == "R830_S3_REGION":
                os.environ.setdefault("AWS_REGION", value)
                os.environ.setdefault("AWS_DEFAULT_REGION", value)
    return params


def _s3_config(*, repository_root: Path, backend_params: dict[str, str]) -> RuntimeConfig:
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
            backend=MemoryToolStorageBackend.S3,
            backend_params=backend_params,
        ),
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_s3_backend_live_read_write_delete_e2e(tmp_path: Path) -> None:
    params = _require_live_s3_params()
    cfg = _s3_config(repository_root=tmp_path, backend_params=params)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)
    assert registry.configured_backend is MemoryToolStorageBackend.S3
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, S3MemoryToolBackend)

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

        result, read, written = await _invoke_protocol_callback(backend, "delete", {"path": path})
        assert result == f"deleted {path}"
        assert read is None
        assert written is None

        with pytest.raises(MemoryCallbackIOError):
            await _invoke_protocol_callback(backend, "view", {"path": path})
    finally:
        if created:
            try:
                await backend.delete(path)
            except MemoryCallbackIOError:
                pass
