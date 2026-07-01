"""U-RT-80 — Stage 5 factory ``materialize_memory_tool_registry_stage`` tests.

ACs per ``Implementation_Plan_Harness_Runtime_v2_14.md`` §1 U-RT-80 (preserved
verbatim at v2.15). Spec contract: ``Spec_Harness_Runtime_v1.md`` v1.17
§14.12.3 stage-5 factory contract + §14.12.4 fail-class taxonomy + §14.12.5
invariant 2 Protocol-conformance enforcement.

Note on plan-prose typo: plan AC #1 names ``DeploymentSurface.LOCAL_DEV``;
the actual enum value is ``DeploymentSurface.LOCAL_DEVELOPMENT`` per
``harness_core/deployment_surface.py:33``. Tests use the correct identifier.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from harness_as.anthropic_graceful_degradation import MemoryToolStorageBackend
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.bootstrap.factories.memory_tool_registry_factory import (
    MEMORY_TOOL_DATABASE_SUBPATH,
    MEMORY_TOOL_ENCRYPTED_FILESYSTEM_ROOT_SUBPATH,
    MEMORY_TOOL_FILESYSTEM_ROOT_SUBPATH,
    materialize_memory_tool_registry_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.memory_tool_filesystem import (
    LocalFilesystemMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_managed_db import (
    ManagedSqlConnection,
    ManagedSqlCursor,
    ManagedSqlMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_registry import MemoryToolRegistry
from harness_runtime.lifecycle.memory_tool_s3 import S3MemoryToolBackend
from harness_runtime.lifecycle.memory_tool_sqlite import SqliteMemoryToolBackend
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryBackendResolutionError,
    MemoryToolBackendConfig,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


class _ManagedDbCursor:
    def fetchone(self) -> tuple[object, ...] | None:
        return None


class _ManagedDbConnection:
    def execute(self, query: str, params: tuple[object, ...] = ()) -> ManagedSqlCursor:
        _ = (query, params)
        return _ManagedDbCursor()

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# B-MEMORY-SURFACE-BACKEND-IMPLS — OPERATOR_DEFINED test fixtures.
# Module-level so `importlib.import_module(__name__)` + getattr resolves them
# (the introspection path the factory exercises).
# ---------------------------------------------------------------------------


class _OperatorBackend:
    """Minimal conformant operator-defined backend (in-memory store)."""

    def __init__(self, backend_params: Mapping[str, str]) -> None:
        self.backend_params = dict(backend_params)
        self._store: dict[str, bytes] = {}

    async def view(self, path: str) -> bytes:
        return self._store[path]

    async def create(self, path: str, content: bytes) -> None:
        self._store[path] = content

    async def delete(self, path: str) -> None:
        self._store.pop(path, None)

    async def str_replace(self, path: str, old: str, new: str) -> None:
        self._store[path] = self._store[path].replace(old.encode(), new.encode())

    async def insert(self, path: str, line: int, content: str) -> None:
        self._store[path] = self._store.get(path, b"") + content.encode()


class _NonConformantOperatorBackend:
    """Missing create/delete/str_replace/insert — rejected by conformance."""

    def __init__(self, backend_params: Mapping[str, str]) -> None:
        self._params = dict(backend_params)

    async def view(self, path: str) -> bytes:
        return b""


class _SyncOperatorBackend:
    """All 5 methods present + callable but SYNC `def` (not `async def`). The
    @runtime_checkable Protocol admits it (presence only); the factory must
    reject it at bootstrap so the dispatcher's `await` never TypeErrors."""

    def __init__(self, backend_params: Mapping[str, str]) -> None:
        self._store: dict[str, bytes] = {}

    def view(self, path: str) -> bytes:
        return self._store.get(path, b"")

    def create(self, path: str, content: bytes) -> None:
        self._store[path] = content

    def delete(self, path: str) -> None:
        self._store.pop(path, None)

    def str_replace(self, path: str, old: str, new: str) -> None:
        return None

    def insert(self, path: str, line: int, content: str) -> None:
        return None


class _RaisingOperatorBackend:
    """Operator backend whose constructor raises an exception that echoes a
    backend_params value — used to prove the factory does NOT leak it."""

    def __init__(self, backend_params: Mapping[str, str]) -> None:
        raise ValueError(f"operator __init__ boom: token={backend_params.get('api_token')}")


_NOT_A_CLASS = "i am a module-level string, not a class"


def _operator_ref(attr: str) -> str:
    """`module:attr` class-qualified-name pointing into THIS test module."""
    return f"{__name__}:{attr}"


def _config(
    *,
    memory_tool_backend_config: MemoryToolBackendConfig | None = None,
    deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
    repository_root: Path | None = None,
) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        repository_root=repository_root if repository_root is not None else Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        memory_tool_backend_config=memory_tool_backend_config,
    )


# ---------------------------------------------------------------------------
# AC #1 — default path: backend_config=None at LOCAL_DEVELOPMENT → FILESYSTEM.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_resolver_picks_filesystem_at_local_development(
    tmp_path: Path,
) -> None:
    cfg = _config(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert isinstance(registry, MemoryToolRegistry)
    assert registry.configured_backend is MemoryToolStorageBackend.FILESYSTEM


# ---------------------------------------------------------------------------
# AC #2 — operator override: explicit FILESYSTEM honored.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_override_filesystem_honored(tmp_path: Path) -> None:
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.FILESYSTEM,
        ),
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.FILESYSTEM


# ---------------------------------------------------------------------------
# AC #3 (B-MEMORY-SURFACE-BACKEND-IMPLS) — ENCRYPTED_FILESYSTEM + OPERATOR_DEFINED
# are now IMPLEMENTED (they no longer raise as "unimplemented"). The factory
# raises only on missing/invalid backend_params. All 5 enum members handled.
# ---------------------------------------------------------------------------


def _encrypted_cfg(
    *,
    repository_root: Path,
    key_env_var: str | None = "HARNESS_TEST_MEM_KEY",
) -> RuntimeConfig:
    params: dict[str, str] = {}
    if key_env_var is not None:
        params["key_env_var"] = key_env_var
    return _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM,
            backend_params=params,
        ),
        repository_root=repository_root,
    )


@pytest.mark.asyncio
async def test_encrypted_filesystem_round_trips_and_is_ciphertext_at_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-Fernet round-trip via the env-var key reference; on-disk bytes are
    ciphertext (the load-bearing ENCRYPTED_FILESYSTEM property)."""
    monkeypatch.setenv("HARNESS_TEST_MEM_KEY", Fernet.generate_key().decode("ascii"))
    cfg = _encrypted_cfg(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, LocalFilesystemMemoryToolBackend)

    await backend.create("/memories/secret.txt", b"top secret note")
    assert await backend.view("/memories/secret.txt") == b"top secret note"

    on_disk = (tmp_path / MEMORY_TOOL_ENCRYPTED_FILESYSTEM_ROOT_SUBPATH / "secret.txt").read_bytes()
    assert b"top secret note" not in on_disk


@pytest.mark.asyncio
async def test_encrypted_filesystem_requires_key_env_var_param(tmp_path: Path) -> None:
    cfg = _encrypted_cfg(repository_root=tmp_path, key_env_var=None)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="key_env_var"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_encrypted_filesystem_unset_env_var_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARNESS_TEST_MEM_KEY", raising=False)
    cfg = _encrypted_cfg(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="unset or empty"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_encrypted_filesystem_malformed_key_raises_without_echoing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inv 4 — a malformed key raises 'malformed' WITHOUT echoing the key value."""
    bad_key = "this-is-not-a-valid-fernet-key"
    monkeypatch.setenv("HARNESS_TEST_MEM_KEY", bad_key)
    cfg = _encrypted_cfg(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)
    msg = str(excinfo.value)
    assert "malformed" in msg
    assert bad_key not in msg  # the key material is never echoed
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_encrypted_filesystem_missing_cryptography_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy-import-behind-RT-FAIL branch: cryptography absent → RT-FAIL."""
    monkeypatch.setenv("HARNESS_TEST_MEM_KEY", Fernet.generate_key().decode("ascii"))
    real_import = importlib.import_module

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "cryptography.fernet":
            raise ImportError("simulated: cryptography not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "harness_runtime.bootstrap.factories.memory_tool_registry_factory.importlib.import_module",
        _fake_import,
    )
    cfg = _encrypted_cfg(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="cryptography"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


# --- OPERATOR_DEFINED ------------------------------------------------------


def _operator_cfg(repository_root: Path, class_qualified_name: str | None) -> RuntimeConfig:
    params: dict[str, str] = {}
    if class_qualified_name is not None:
        params["class_qualified_name"] = class_qualified_name
    return _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.OPERATOR_DEFINED,
            backend_params=params,
        ),
        repository_root=repository_root,
    )


@pytest.mark.asyncio
async def test_operator_defined_constructs_operator_class_with_params(tmp_path: Path) -> None:
    """class_qualified_name resolves the operator class; the full backend_params
    Mapping is passed to its constructor."""
    cfg = _operator_cfg(tmp_path, _operator_ref("_OperatorBackend"))
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.OPERATOR_DEFINED
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, _OperatorBackend)
    # The full backend_params Mapping (incl. the routing key) reached __init__.
    assert backend.backend_params["class_qualified_name"] == _operator_ref("_OperatorBackend")
    # And it actually works as a Protocol backend.
    await backend.create("/memories/x", b"v")
    assert await backend.view("/memories/x") == b"v"


@pytest.mark.asyncio
async def test_operator_defined_accepts_dotted_reference(tmp_path: Path) -> None:
    """`module.Class` dotted form resolves identically to `module:Class`."""
    cfg = _operator_cfg(tmp_path, f"{__name__}._OperatorBackend")
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)
    assert isinstance(registry.resolve_backend(cfg.deployment_surface), _OperatorBackend)


@pytest.mark.asyncio
async def test_operator_defined_requires_class_qualified_name(tmp_path: Path) -> None:
    cfg = _operator_cfg(tmp_path, None)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="class_qualified_name"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_operator_defined_bad_module_raises(tmp_path: Path) -> None:
    cfg = _operator_cfg(tmp_path, "totally.nonexistent.module:Backend")
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="failed to import"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_operator_defined_missing_class_raises(tmp_path: Path) -> None:
    cfg = _operator_cfg(tmp_path, _operator_ref("DoesNotExistBackend"))
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="not found"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_operator_defined_non_class_reference_raises(tmp_path: Path) -> None:
    cfg = _operator_cfg(tmp_path, _operator_ref("_NOT_A_CLASS"))
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="non-class"):
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_operator_defined_instantiation_failure_does_not_echo_params(tmp_path: Path) -> None:
    """A constructor failure surfaces the exception TYPE but never echoes a
    backend_params value (symmetric with the encrypted-path no-key-echo
    discipline; §14.12.5 invariant 4). Codex-review-driven."""
    secret = "super-secret-api-token-xyz"
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.OPERATOR_DEFINED,
            backend_params={
                "class_qualified_name": _operator_ref(_RaisingOperatorBackend.__name__),
                "api_token": secret,
            },
        ),
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)
    msg = str(excinfo.value)
    assert "ValueError" in msg  # the exception TYPE is surfaced for debugging
    assert secret not in msg  # ... but the secret param value is NOT echoed
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_operator_defined_non_conformant_class_raises(tmp_path: Path) -> None:
    """A constructed operator class missing Protocol methods is rejected by the
    existing _enforce_protocol_conformance step (§14.12.5 invariant 2)."""
    cfg = _operator_cfg(tmp_path, _operator_ref(_NonConformantOperatorBackend.__name__))
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)
    msg = str(excinfo.value)
    assert "RT-FAIL-MEMORY-BACKEND-RESOLUTION" in msg
    assert any(name in msg for name in ("create", "delete", "str_replace", "insert"))
    assert ctx.memory_tool_registry is None


@pytest.mark.asyncio
async def test_operator_defined_sync_methods_rejected_at_bootstrap(tmp_path: Path) -> None:
    """A sync-`def` operator backend (Protocol-present but not async) fails CLOSED
    at bootstrap (ADR-F4), not with a TypeError at first dispatch. Codex-review-
    driven — @runtime_checkable isinstance does NOT catch sync-vs-async."""
    cfg = _operator_cfg(tmp_path, _operator_ref(_SyncOperatorBackend.__name__))
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="non-async") as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)
    assert "RT-FAIL-MEMORY-BACKEND-RESOLUTION" in str(excinfo.value)
    assert ctx.memory_tool_registry is None


# ---------------------------------------------------------------------------
# AC #3a (R-830) — operator override: S3 constructs the cloud-vault backend
# when bucket params are present. Provider-free test monkeypatches the S3
# client constructor; live AWS credentials remain an operator-gated e2e.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_override_constructs_s3_backend_with_bucket_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeS3Client:
        def put_object(self, **kwargs: object) -> None:
            return None

        def get_object(self, **kwargs: object) -> dict[str, object]:
            return {"Body": b""}

        def delete_object(self, **kwargs: object) -> None:
            return None

    fake_client = FakeS3Client()

    monkeypatch.setattr(
        "harness_runtime.bootstrap.factories.memory_tool_registry_factory."
        "_create_s3_client_from_backend_params",
        lambda params: fake_client,
    )

    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.S3,
            backend_params={"bucket": "memory-bucket", "key_prefix": "tenant-a"},
        ),
        repository_root=tmp_path,
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
    )
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.S3
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, S3MemoryToolBackend)
    assert backend.bucket == "memory-bucket"
    assert backend.key_prefix == "tenant-a"


@pytest.mark.asyncio
async def test_s3_override_requires_bucket_param(tmp_path: Path) -> None:
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.S3,
            backend_params={"key_prefix": "tenant-a"},
        ),
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError, match="bucket"):
        await materialize_memory_tool_registry_stage(cfg, ctx)

    assert ctx.memory_tool_registry is None


# ---------------------------------------------------------------------------
# AC #3b (R-830) — operator override: DATABASE constructs the SQLite backend
# (default connection path under repository_root) + honors an explicit
# backend_params['connection_string'].
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_override_constructs_sqlite_backend_default_path(
    tmp_path: Path,
) -> None:
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.DATABASE,
        ),
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.DATABASE
    backend = registry.resolve_backend(cfg.deployment_surface)
    assert isinstance(backend, SqliteMemoryToolBackend)
    # Default connection path created under repository_root.
    assert (tmp_path / MEMORY_TOOL_DATABASE_SUBPATH).exists()


@pytest.mark.asyncio
async def test_database_override_honors_connection_string_param(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "custom" / "operator.db"
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.DATABASE,
            backend_params={"connection_string": str(db_path)},
        ),
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.DATABASE
    assert isinstance(registry.resolve_backend(cfg.deployment_surface), SqliteMemoryToolBackend)
    # The operator-supplied connection_string path is used, not the default.
    assert db_path.exists()
    assert not (tmp_path / MEMORY_TOOL_DATABASE_SUBPATH).exists()


@pytest.mark.asyncio
async def test_database_override_postgres_connection_string_constructs_managed_db_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_string = "postgresql://db.example.invalid/harness"
    seen_params: list[dict[str, str]] = []

    def fake_connect_factory(params: dict[str, str]):
        seen_params.append(params)

        def connect(_connection_string: str) -> ManagedSqlConnection:
            assert _connection_string == connection_string
            return _ManagedDbConnection()

        return connect

    monkeypatch.setattr(
        "harness_runtime.bootstrap.factories.memory_tool_registry_factory."
        "_create_managed_sql_connect_from_backend_params",
        fake_connect_factory,
    )
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.DATABASE,
            backend_params={"connection_string": connection_string},
        ),
        deployment_surface=DeploymentSurface.MANAGED_CLOUD,
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.DATABASE
    assert isinstance(registry.resolve_backend(cfg.deployment_surface), ManagedSqlMemoryToolBackend)
    assert seen_params == [{"connection_string": connection_string}]
    assert not (tmp_path / MEMORY_TOOL_DATABASE_SUBPATH).exists()


# ---------------------------------------------------------------------------
# AC #4 — stage-5 LOOP_INIT invocation: ctx.memory_tool_registry bound after
# stage 5 with non-None .backend (verified via the resolve_backend Protocol
# accessor + pre-bound builder state mirroring full-bootstrap invariants).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_binds_registry_on_ctx_with_non_none_backend(
    tmp_path: Path,
) -> None:
    cfg = _config(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    await materialize_memory_tool_registry_stage(cfg, ctx)

    assert ctx.memory_tool_registry is not None
    backend = ctx.memory_tool_registry.resolve_backend(cfg.deployment_surface)
    assert backend is not None
    assert isinstance(backend, LocalFilesystemMemoryToolBackend)


# ---------------------------------------------------------------------------
# AC #5 — bootstrap-abort behavior: a raise from the factory propagates as
# fail-closed (no swallowing); ctx.memory_tool_registry remains None per AC #3.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_failure_propagates_fail_closed(tmp_path: Path) -> None:
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.S3,
        ),
        repository_root=tmp_path,
    )
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError):
        await materialize_memory_tool_registry_stage(cfg, ctx)

    assert ctx.memory_tool_registry is None


# ---------------------------------------------------------------------------
# AC #6 — integration: registry.configured_backend == FILESYSTEM and
# resolve_backend(LOCAL_DEVELOPMENT) returns a LocalFilesystemMemoryToolBackend
# under the default-config path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_default_path_yields_filesystem_backend(tmp_path: Path) -> None:
    cfg = _config(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    await materialize_memory_tool_registry_stage(cfg, ctx)

    assert ctx.memory_tool_registry.configured_backend is MemoryToolStorageBackend.FILESYSTEM
    backend = ctx.memory_tool_registry.resolve_backend(DeploymentSurface.LOCAL_DEVELOPMENT)
    assert isinstance(backend, LocalFilesystemMemoryToolBackend)


# ---------------------------------------------------------------------------
# AC #7 — Protocol-conformance enforcement: backend missing a method raises
# RT-FAIL-MEMORY-BACKEND-RESOLUTION naming the missing method(s).
# Verified via monkey-patching the FILESYSTEM constructor to return an
# incomplete object — exercises step 3 introspection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incomplete_protocol_backend_raises_with_missing_method_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteBackend:
        async def view(self, path: str) -> bytes:
            return b""

        # create / delete / str_replace / insert intentionally missing.

    def _fake_ctor(*, root: Path) -> IncompleteBackend:
        return IncompleteBackend()

    monkeypatch.setattr(
        "harness_runtime.bootstrap.factories.memory_tool_registry_factory."
        "LocalFilesystemMemoryToolBackend",
        _fake_ctor,
    )

    cfg = _config(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)

    msg = str(excinfo.value)
    assert "RT-FAIL-MEMORY-BACKEND-RESOLUTION" in msg
    # Names at least one of the missing Protocol methods.
    assert any(name in msg for name in ("create", "delete", "str_replace", "insert"))


@pytest.mark.asyncio
async def test_non_callable_protocol_attribute_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: PEP 544 @runtime_checkable accepts non-callable
    attributes that shadow method names; the factory rejects them."""

    class NonCallableBackend:
        view = None  # non-callable shadow
        create = None
        delete = None
        str_replace = None
        insert = None

    def _fake_ctor(*, root: Path) -> NonCallableBackend:
        return NonCallableBackend()

    monkeypatch.setattr(
        "harness_runtime.bootstrap.factories.memory_tool_registry_factory."
        "LocalFilesystemMemoryToolBackend",
        _fake_ctor,
    )

    cfg = _config(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)

    assert "non-callable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC #8 — importable + pyright strict pass. Importable verified by the
# imports at top of this module; pyright strict run separately at CI/local
# `pyright --project harness-runtime`.
# ---------------------------------------------------------------------------


def test_factory_symbol_importable() -> None:
    # Re-import via module-level attribute to assert public-API surface.
    from harness_runtime.bootstrap.factories import memory_tool_registry_factory

    assert callable(memory_tool_registry_factory.materialize_memory_tool_registry_stage)
    assert memory_tool_registry_factory.MEMORY_TOOL_FILESYSTEM_ROOT_SUBPATH == (".harness/memories")
    assert memory_tool_registry_factory.PROTOCOL_REQUIRED_METHODS == (
        "view",
        "create",
        "delete",
        "str_replace",
        "insert",
    )


# ---------------------------------------------------------------------------
# Additional: filesystem-root path is resolved per
# config.repository_root / ".harness/memories" — verifies step 2a sub-path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filesystem_backend_rooted_at_repository_subpath(tmp_path: Path) -> None:
    cfg = _config(repository_root=tmp_path)
    ctx = _MutableHarnessContext()

    await materialize_memory_tool_registry_stage(cfg, ctx)

    backend = ctx.memory_tool_registry.resolve_backend(cfg.deployment_surface)
    expected_root = (tmp_path / MEMORY_TOOL_FILESYSTEM_ROOT_SUBPATH).resolve()
    # LocalFilesystemMemoryToolBackend keeps the resolved root at `_root`.
    assert backend._root == expected_root  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# R-FS-1 arc B5 — per-deployment-surface backend selection (built-but-vacuous
# fix). resolve_backend no longer ignores its argument: the default-config
# registry resolves each surface independently. Anti-vacuity proof at the
# factory layer is constructs-vs-raises (the single global override config
# cannot populate two distinct constructed types without collapsing the map).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b5_default_registry_discriminates_by_surface(tmp_path: Path) -> None:
    """ANTI-VACUITY: a default-config registry resolves FILESYSTEM for surfaces
    that admit it and RAISES for MANAGED_CLOUD (which admits only {s3, database},
    both needing operator params) — the argument is genuinely read."""
    cfg = _config(repository_root=tmp_path, deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    # FILESYSTEM-admitting surfaces resolve to a filesystem backend.
    assert isinstance(
        registry.resolve_backend(DeploymentSurface.LOCAL_DEVELOPMENT),
        LocalFilesystemMemoryToolBackend,
    )
    assert isinstance(
        registry.resolve_backend(DeploymentSurface.SELF_HOSTED_SERVER),
        LocalFilesystemMemoryToolBackend,
    )
    # MANAGED_CLOUD admits only {s3, database} → no config-free backend → RAISE.
    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        registry.resolve_backend(DeploymentSurface.MANAGED_CLOUD)
    msg = str(excinfo.value)
    assert "RT-FAIL-MEMORY-BACKEND-RESOLUTION" in msg
    assert "managed-cloud" in msg
    assert "s3" in msg and "database" in msg
    assert "memory_tool_backend_config" in msg
    assert "§14.D" in msg


@pytest.mark.asyncio
async def test_b5_default_shares_one_filesystem_instance_per_distinct_backend(
    tmp_path: Path,
) -> None:
    """§14.12.5 invariant 1 (resolved exactly once): the two FILESYSTEM-admitting
    surfaces share ONE constructed backend instance, not two."""
    cfg = _config(repository_root=tmp_path, deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.resolve_backend(DeploymentSurface.LOCAL_DEVELOPMENT) is (
        registry.resolve_backend(DeploymentSurface.SELF_HOSTED_SERVER)
    )


@pytest.mark.asyncio
async def test_b5_active_managed_cloud_no_override_aborts_bootstrap_fail_closed(
    tmp_path: Path,
) -> None:
    """PRESERVED FAIL-CLOSED LOCK: an active MANAGED_CLOUD surface with no
    operator override aborts bootstrap (the pre-B5 behavior B5 preserves — a
    future edit must not silently degrade it to an ephemeral local backend)."""
    cfg = _config(repository_root=tmp_path, deployment_surface=DeploymentSurface.MANAGED_CLOUD)
    ctx = _MutableHarnessContext()

    with pytest.raises(MemoryBackendResolutionError) as excinfo:
        await materialize_memory_tool_registry_stage(cfg, ctx)

    msg = str(excinfo.value)
    assert "RT-FAIL-MEMORY-BACKEND-RESOLUTION" in msg
    assert "managed-cloud" in msg
    assert "§14.D" in msg
    assert ctx.memory_tool_registry is None  # never bound on resolution failure


@pytest.mark.asyncio
async def test_b5_default_self_hosted_active_resolves_filesystem(tmp_path: Path) -> None:
    """An active SELF_HOSTED_SERVER surface (admits FILESYSTEM) resolves the
    filesystem backend at the active surface (the production-path query)."""
    cfg = _config(repository_root=tmp_path, deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER)
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    assert registry.configured_backend is MemoryToolStorageBackend.FILESYSTEM
    assert isinstance(
        registry.resolve_backend(cfg.deployment_surface),
        LocalFilesystemMemoryToolBackend,
    )


@pytest.mark.asyncio
async def test_b5_operator_override_collapses_map_to_one_backend_all_surfaces(
    tmp_path: Path,
) -> None:
    """Operator override forces ONE backend for EVERY surface (§14.12.1) — the
    map collapses; resolve_backend is surface-independent under override."""
    cfg = _config(
        memory_tool_backend_config=MemoryToolBackendConfig(
            backend=MemoryToolStorageBackend.FILESYSTEM,
        ),
        repository_root=tmp_path,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    ctx = _MutableHarnessContext()

    registry = await materialize_memory_tool_registry_stage(cfg, ctx)

    local = registry.resolve_backend(DeploymentSurface.LOCAL_DEVELOPMENT)
    self_hosted = registry.resolve_backend(DeploymentSurface.SELF_HOSTED_SERVER)
    managed = registry.resolve_backend(DeploymentSurface.MANAGED_CLOUD)
    # Same instance for every surface — the override is surface-independent.
    assert local is self_hosted is managed
    assert isinstance(local, LocalFilesystemMemoryToolBackend)
