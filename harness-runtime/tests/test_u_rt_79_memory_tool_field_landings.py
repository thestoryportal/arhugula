"""U-RT-79 — RuntimeConfig.memory_tool_backend_config + HarnessContext.memory_tool_registry tests.

ACs per runtime plan v2.15 §1 U-RT-79 (preserved from v2.14):

1. `RuntimeConfig(..., memory_tool_backend_config=None)` instantiates without
   ValidationError.
2. `RuntimeConfig(...)` instantiated WITHOUT the new field preserves v1.16-
   shape backwards-compatibility (field defaults to `None`).
3. `RuntimeConfig(..., memory_tool_backend_config=MemoryToolBackendConfig(...))`
   accepts an operator-supplied config instance and stores it on the frozen
   model.
4. `RuntimeConfig(..., memory_tool_backend_config="not_a_config", ...)` raises
   typed `ValidationError` per Pydantic field validation (type mismatch).
5. `HarnessContext.memory_tool_registry` accessible on a fully-bootstrapped
   context as a `MemoryToolRegistry` instance.
6. Per-field minor-version-bump invariant per C-RT-02 v1.1 version-evolution
   clause preserved (new optional field → minor bump v1.16 → v1.17 already
   absorbed at spec-writer arc).
7. Importable; pyright strict mode passes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.memory_tool_registry import MemoryToolRegistry
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryToolBackendConfig,
    MemoryToolStorageBackend,
)
from harness_runtime.types import (
    CollectorConfig,
    HarnessContext,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from pydantic import ValidationError

# ----------------------------------------------------------------------------
# Minimal RuntimeConfig kwargs fixture — fills required fields only.
# Mirrors `test_config_loader.py::_required_kwargs` shape.
# ----------------------------------------------------------------------------


def _minimal_runtime_config_kwargs(tmp_path: Path) -> dict[str, Any]:
    """Required-fields-only kwargs for a valid RuntimeConfig instance."""
    return {
        "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
        "repository_root": tmp_path,
        "path_bindings": PathBindingConfig(),
        "provider_secrets": ProviderSecretsConfig(),
        "otel": OTelConfig(otlp_endpoint="http://localhost:4318"),
        "collector": CollectorConfig(),
        "default_topology": TopologyPattern.SINGLE_THREADED_LINEAR,
    }


# AC #1 — explicit None at construction.


def test_runtime_config_accepts_explicit_none(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        memory_tool_backend_config=None,
    )
    assert config.memory_tool_backend_config is None


# AC #2 — backwards-compatibility (field omitted at construction).


def test_runtime_config_field_defaults_to_none_when_omitted(tmp_path: Path) -> None:
    """AC #2 — instantiating without the new field defaults to None
    (preserves v1.16-shape backwards-compatibility)."""
    config = RuntimeConfig(**_minimal_runtime_config_kwargs(tmp_path))
    assert config.memory_tool_backend_config is None


# AC #3 — operator-supplied config accepted.


def test_runtime_config_accepts_operator_supplied_config(tmp_path: Path) -> None:
    operator_config = MemoryToolBackendConfig(
        backend=MemoryToolStorageBackend.FILESYSTEM,
    )
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        memory_tool_backend_config=operator_config,
    )
    assert config.memory_tool_backend_config is operator_config
    assert operator_config.backend == MemoryToolStorageBackend.FILESYSTEM


def test_runtime_config_accepts_config_with_backend_params(tmp_path: Path) -> None:
    operator_config = MemoryToolBackendConfig(
        backend=MemoryToolStorageBackend.S3,
        backend_params={"bucket": "memories-prod", "region": "us-west-2"},
    )
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        memory_tool_backend_config=operator_config,
    )
    assert config.memory_tool_backend_config is not None
    assert config.memory_tool_backend_config.backend_params == {
        "bucket": "memories-prod",
        "region": "us-west-2",
    }


# AC #4 — type mismatch raises typed ValidationError.


def test_runtime_config_rejects_wrong_type(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig(
            **_minimal_runtime_config_kwargs(tmp_path),
            memory_tool_backend_config="not_a_config",  # type: ignore[arg-type]
        )


# AC #5 — HarnessContext.memory_tool_registry accessible post-freeze.


def test_mutable_context_carries_memory_tool_registry_field() -> None:
    """AC #5 (carrier shape) — _MutableHarnessContext exposes the field."""
    builder = _MutableHarnessContext()
    assert builder.memory_tool_registry is None  # default
    fake_backend_instance = _FakeBackend()
    registry = MemoryToolRegistry(
        backend=fake_backend_instance,
        configured_backend=MemoryToolStorageBackend.FILESYSTEM,
    )
    builder.memory_tool_registry = registry
    assert builder.memory_tool_registry is registry


def test_memory_tool_registry_in_required_fields_at_u_rt_80() -> None:
    """AC #5 (post-U-RT-80 landing) — `memory_tool_registry` is in
    `_REQUIRED_FIELDS` once the U-RT-80 factory binds it at stage 5
    LOOP_INIT. The U-RT-79 → U-RT-80 split per `[[halt-route-split-AC-pattern]]`
    deferred this addition to U-RT-80 to preserve atomic-rollback
    boundaries during the L9-octies traversal."""
    from harness_runtime.bootstrap.mutable_context import _REQUIRED_FIELDS

    assert "memory_tool_registry" in _REQUIRED_FIELDS, (
        "memory_tool_registry must be in _REQUIRED_FIELDS at U-RT-80 "
        "landing (the factory binds it at stage 5 LOOP_INIT)"
    )


def test_harness_context_schema_declares_memory_tool_registry() -> None:
    """AC #5 (schema shape) — HarnessContext Pydantic model declares the
    `memory_tool_registry` field at the frozen post-bootstrap schema."""
    assert "memory_tool_registry" in HarnessContext.model_fields


def test_freeze_signature_carries_memory_tool_registry_kwarg(
    tmp_path: Path,
) -> None:
    """AC #5 (freeze() plumbing) — the freeze() method body passes
    `memory_tool_registry=self.memory_tool_registry` to HarnessContext.

    Verified via source-inspection rather than runtime exercise: the
    full freeze() round-trip requires concrete instances of 30+ Pydantic-
    validated fields, which is integration-tested at U-RT-80 factory + full
    bootstrap test. This unit test asserts the plumbing edge (kwarg passed)
    via source-substring assertion."""
    import inspect

    from harness_runtime.bootstrap import mutable_context

    freeze_source = inspect.getsource(mutable_context._MutableHarnessContext.freeze)
    assert "memory_tool_registry=self.memory_tool_registry" in freeze_source, (
        "freeze() body must pass memory_tool_registry to HarnessContext per U-RT-79 AC #5"
    )


# AC #7 — importable (verified via imports above + this assertion).


def test_carriers_importable() -> None:
    from harness_runtime.types import HarnessContext as HC
    from harness_runtime.types import RuntimeConfig as RC

    # Verify the new fields exist in the Pydantic model schemas.
    assert "memory_tool_backend_config" in RC.model_fields
    assert "memory_tool_registry" in HC.model_fields


# ----------------------------------------------------------------------------
# Helpers — fully-populated _MutableHarnessContext for freeze() tests.
# ----------------------------------------------------------------------------


class _FakeBackend:
    """Minimal Protocol-satisfying backend (no I/O)."""

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


def _fully_populated_builder(
    tmp_path: Path,
    *,
    memory_tool_registry: Any = None,
) -> _MutableHarnessContext:
    """Build a _MutableHarnessContext with every `_REQUIRED_FIELDS` slot filled.

    Uses sentinel objects for fields whose construction cost would dwarf this
    test's intent (e.g., concrete IS / OTel / CP types). The freeze() path
    only checks for `is not None`; sentinels suffice. Sentinel `Any` typing
    sidesteps pyright's per-field type-check at assignment.
    """
    builder = _MutableHarnessContext()
    builder.config = RuntimeConfig(**_minimal_runtime_config_kwargs(tmp_path))
    builder.drained_flag = asyncio.Event()
    builder.pause_requested_flag = asyncio.Event()

    sentinel: Any = object()
    builder.path_resolver = sentinel
    builder.worktree_manager = sentinel
    builder.shadow_git = sentinel
    builder.ledger_writer = sentinel
    builder.ledger_reader = sentinel
    builder.index = sentinel
    builder.cache = sentinel
    builder.skills = {}
    builder.tool_contracts = {}
    builder.mcp_host = sentinel
    builder.mcp_clients = {}
    builder.mcp_client_hosts = sentinel
    builder.sandbox_dispatch = sentinel
    builder.providers = {}
    builder.routing_manifest = sentinel
    builder.engine_selector = sentinel
    builder.fallback_chain = sentinel
    builder.retry_breaker = sentinel
    builder.hitl_registry = sentinel
    builder.handoff_registry = sentinel
    builder.tracer_provider = sentinel
    builder.collector_daemon = sentinel
    builder.cost_chain = sentinel
    builder.audit_writer = sentinel
    builder.override_evaluator = sentinel
    builder.topology_dispatcher = sentinel
    builder.lifecycle_emitter = sentinel
    builder.llm_dispatcher = sentinel
    builder.sub_agent_dispatcher = sentinel
    builder.ask_user_question_surface = sentinel
    builder.step_dispatchers = sentinel
    builder.tool_dispatcher = sentinel
    builder.hitl_tool_loop = sentinel
    builder.engine_recovery_loop = sentinel
    builder.per_server_trust_evaluator = sentinel
    builder.mcp_namespace_emitter = sentinel
    builder.memory_tool_registry = memory_tool_registry
    builder.resume_context_holder = sentinel

    return builder
