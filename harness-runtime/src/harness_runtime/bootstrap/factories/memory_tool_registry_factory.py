"""U-RT-80 — Stage 5 factory `materialize_memory_tool_registry_stage(config, ctx)
→ MemoryToolRegistry`.

Per `Spec_Harness_Runtime_v1.md` v1.17 §14.12.3 stage-5 factory contract +
§14.12.5 invariant 2 (Protocol-conformance enforced at stage-5 binding via
`@runtime_checkable` introspection).

3-step composition body per spec §14.12.3 prose:

  1. Resolve the configured `MemoryToolStorageBackend` enum value per
     `config.memory_tool_backend_config` override (if present), else via the
     `harness_as.anthropic_graceful_degradation.memory_tool_storage_backend`
     resolver default for `config.deployment_surface` (picks the
     `FILESYSTEM` member if present in the returned frozenset — the only
     backend with an implementation landed at v2.15 per §14.D operator
     ratification).
  2. Construct the storage-backend implementation:
     - `FILESYSTEM` → `LocalFilesystemMemoryToolBackend` rooted at
       `config.repository_root / ".harness/memories"` (root path resolution
       per §14.12.7 implementation discretion; PathClass extension deferred).
     - `ENCRYPTED_FILESYSTEM` → the same backend rooted at
       `.harness/memories-encrypted` with a `FernetContentCodec` injected so
       at-rest content is ciphertext (`B-MEMORY-SURFACE-BACKEND-IMPLS`).
     - `OPERATOR_DEFINED` → the operator class resolved from
       `backend_params['class_qualified_name']` via importlib introspection
       (`B-MEMORY-SURFACE-BACKEND-IMPLS`).
     - All 5 enum members handled (`assert_never` tail); construction failures
       (missing/invalid `backend_params`) raise `MemoryBackendResolutionError`.
  3. Verify the constructed backend satisfies
     `MemoryToolStorageBackendProtocol` via `@runtime_checkable` isinstance
     introspection (§14.12.5 invariant 2). Defense-in-depth: also verify
     every method named on the Protocol resolves on the backend (catches
     non-callable shadowing that `@runtime_checkable` does not).
  4. Construct `MemoryToolRegistry(backend=..., configured_backend=...)`
     and bind to `ctx.memory_tool_registry`.

Stage-5 ordering per §14.12.3: arbitrary within stage 5 LOOP_INIT (no
ordering dependency on `materialize_runtime_tool_dispatcher_stage` — the
registry construction has no shared dependency with the tool dispatcher).
"""

from __future__ import annotations

import importlib
import inspect
import os
from pathlib import Path
from typing import Any, assert_never, cast

from harness_as.anthropic_graceful_degradation import (
    MemoryToolStorageBackend,
    memory_tool_storage_backend,
)
from harness_core.deployment_surface import DeploymentSurface

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.memory_tool_encrypted import FernetContentCodec, FernetLike
from harness_runtime.lifecycle.memory_tool_filesystem import (
    LocalFilesystemMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_managed_db import (
    ManagedSqlConnect,
    ManagedSqlMemoryToolBackend,
)
from harness_runtime.lifecycle.memory_tool_registry import MemoryToolRegistry
from harness_runtime.lifecycle.memory_tool_s3 import S3ClientProtocol, S3MemoryToolBackend
from harness_runtime.lifecycle.memory_tool_sqlite import SqliteMemoryToolBackend
from harness_runtime.lifecycle.memory_tool_types import (
    MemoryBackendResolutionError,
    MemoryToolBackendConfig,
    MemoryToolStorageBackendProtocol,
)
from harness_runtime.types import RuntimeConfig

__all__ = [
    "MEMORY_TOOL_DATABASE_SUBPATH",
    "MEMORY_TOOL_ENCRYPTED_FILESYSTEM_ROOT_SUBPATH",
    "MEMORY_TOOL_FILESYSTEM_ROOT_SUBPATH",
    "PROTOCOL_REQUIRED_METHODS",
    "materialize_memory_tool_registry_stage",
]


MEMORY_TOOL_FILESYSTEM_ROOT_SUBPATH = ".harness/memories"
"""Sub-path under `config.repository_root` for the FILESYSTEM backend root.

Per spec §14.12.7 implementation discretion + §14.12.3 step 2a suggestion of
`PathClass.MEMORY_TOOL_BACKEND_ROOT` (PathClass extension deferred). Mirrors
the `.harness/...` sibling sub-paths used by other runtime carriers
(`.harness/runtime.pid`, etc.)."""


MEMORY_TOOL_DATABASE_SUBPATH = ".harness/memories.db"
"""Default sub-path under `config.repository_root` for the DATABASE backend
SQLite file, used when `backend_params['connection_string']` is absent (R-830;
spec §14.12.3 DATABASE step). Sibling of the FILESYSTEM `.harness/memories`
root."""


MEMORY_TOOL_ENCRYPTED_FILESYSTEM_ROOT_SUBPATH = ".harness/memories-encrypted"
"""Sub-path under `config.repository_root` for the ENCRYPTED_FILESYSTEM backend
root (`B-MEMORY-SURFACE-BACKEND-IMPLS`; spec §14.12.3 ENCRYPTED_FILESYSTEM step).

A DISTINCT root from the plaintext FILESYSTEM `.harness/memories` so the two
backends never co-mingle plaintext and ciphertext files at one path (an operator
who switches backends would otherwise have the plaintext backend read ciphertext
as plaintext). ENCRYPTED_FILESYSTEM is override-only, so the two roots never both
populate in one process."""


def _resolve_database_connection_path(config: RuntimeConfig) -> Path:
    """Resolve the SQLite database path for the DATABASE backend.

    Per spec §14.12.3 DATABASE step, the connection is supplied via
    `backend_params['connection_string']`. When that key is absent (or
    `backend_params` is `None`), fall back to the workspace default
    `config.repository_root / MEMORY_TOOL_DATABASE_SUBPATH`.
    """
    backend_cfg = config.memory_tool_backend_config
    if backend_cfg is not None and backend_cfg.backend_params is not None:
        connection_string = backend_cfg.backend_params.get("connection_string")
        if connection_string:
            return Path(connection_string)
    return config.repository_root / MEMORY_TOOL_DATABASE_SUBPATH


def _resolve_database_connection_string(config: RuntimeConfig) -> str | None:
    """Return the operator DATABASE connection string, when supplied."""
    backend_cfg = config.memory_tool_backend_config
    if backend_cfg is not None and backend_cfg.backend_params is not None:
        connection_string = backend_cfg.backend_params.get("connection_string")
        if connection_string:
            return connection_string
    return None


def _is_managed_database_connection_string(connection_string: str | None) -> bool:
    """True for PostgreSQL-compatible managed DB connection strings."""
    if connection_string is None:
        return False
    lowered = connection_string.lower()
    return lowered.startswith(("postgres://", "postgresql://"))


def _require_backend_params(
    backend_cfg: MemoryToolBackendConfig | None,
    *,
    backend: MemoryToolStorageBackend,
) -> dict[str, str]:
    if backend_cfg is None or backend_cfg.backend_params is None:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend {backend.value!r} "
            f"requires memory_tool_backend_config.backend_params"
        )
    return dict(backend_cfg.backend_params)


def _create_s3_client_from_backend_params(params: dict[str, str]) -> S3ClientProtocol:
    """Lazily construct a boto3 S3 client from operator backend params.

    boto3 is intentionally optional: provider-free CI monkeypatches this
    function, while live MANAGED_CLOUD use requires the operator to install the
    dependency and provide ambient AWS credentials or equivalent boto3 config.
    """
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 's3' requires optional "
            "dependency boto3 for live client construction; provider-free tests "
            "may monkeypatch _create_s3_client_from_backend_params"
        ) from exc

    client_kwargs: dict[str, str] = {}
    for key in ("region_name", "endpoint_url", "profile_name"):
        value = params.get(key)
        if value:
            client_kwargs[key] = value

    boto3_module = cast(Any, boto3)
    if "profile_name" in client_kwargs:
        profile_name = client_kwargs.pop("profile_name")
        session = boto3_module.Session(profile_name=profile_name)
        return cast(S3ClientProtocol, session.client("s3", **client_kwargs))
    return cast(S3ClientProtocol, boto3_module.client("s3", **client_kwargs))


def _construct_s3_backend(config: RuntimeConfig) -> S3MemoryToolBackend:
    params = _require_backend_params(
        config.memory_tool_backend_config,
        backend=MemoryToolStorageBackend.S3,
    )
    bucket = params.get("bucket")
    if not bucket:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 's3' requires backend_params['bucket']"
        )
    client = _create_s3_client_from_backend_params(params)
    return S3MemoryToolBackend(
        bucket=bucket,
        key_prefix=params.get("key_prefix", ""),
        client=client,
    )


def _create_managed_sql_connect_from_backend_params(params: dict[str, str]) -> ManagedSqlConnect:
    """Lazily construct a psycopg connect callable for managed SQL backends."""
    try:
        psycopg = importlib.import_module("psycopg")
    except ImportError as exc:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'database' with a "
            "postgres:// or postgresql:// connection_string requires optional "
            "dependency psycopg for live managed-DB construction; provider-free "
            "tests may monkeypatch _create_managed_sql_connect_from_backend_params"
        ) from exc

    psycopg_module = cast(Any, psycopg)
    return cast(ManagedSqlConnect, psycopg_module.connect)


def _construct_database_backend(config: RuntimeConfig) -> MemoryToolStorageBackendProtocol:
    connection_string = _resolve_database_connection_string(config)
    if _is_managed_database_connection_string(connection_string):
        params = _require_backend_params(
            config.memory_tool_backend_config,
            backend=MemoryToolStorageBackend.DATABASE,
        )
        connect = _create_managed_sql_connect_from_backend_params(params)
        assert connection_string is not None
        return ManagedSqlMemoryToolBackend(
            connection_string=connection_string,
            connect=connect,
        )
    return SqliteMemoryToolBackend(db_path=_resolve_database_connection_path(config))


def _resolve_memory_encryption_key(params: dict[str, str]) -> bytes:
    """Resolve the ENCRYPTED_FILESYSTEM Fernet key from an operator key reference.

    Reference scheme (`B-MEMORY-SURFACE-BACKEND-IMPLS`, impl-discretion per spec
    §14.12.7): `backend_params['key_env_var']` names the ENVIRONMENT VARIABLE
    holding the urlsafe-base64 Fernet key. Per structure-not-content discipline
    (spec §14.12.1 + `MemoryToolBackendConfig`), the harness config carries the
    REFERENCE (an env-var name), never the key material. Env-var is the
    deliberately chosen scheme — it composes with the workspace's existing
    `.env`/just dotenv secret posture; python-keyring (Target Stack nominal) is
    not wired here (one working reference scheme satisfies full-spec;
    additional schemes would be speculative).

    Monkeypatchable for provider-free tests. Error messages name the env-var
    REFERENCE only — never the resolved key value (spec §14.12.5 invariant 4).
    """
    env_var = params.get("key_env_var")
    if not env_var:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'encrypted_filesystem' "
            "requires backend_params['key_env_var'] naming the environment "
            "variable that holds the encryption key (key reference, not key "
            "material — structure-not-content discipline)"
        )
    key = os.environ.get(env_var)
    if not key:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'encrypted_filesystem' "
            f"key reference env var {env_var!r} is unset or empty"
        )
    return key.encode("utf-8")


def _create_fernet_from_key(key: bytes) -> FernetLike:
    """Lazily construct a `cryptography` Fernet from the resolved key.

    `cryptography` is intentionally optional behind RT-FAIL (mirrors the boto3 /
    psycopg lazy-import pattern); provider-free tests may monkeypatch
    `_create_fernet_from_key`. A malformed key raises RT-FAIL WITHOUT echoing the
    key material (spec §14.12.5 invariant 4).
    """
    try:
        fernet_module = importlib.import_module("cryptography.fernet")
    except ImportError as exc:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'encrypted_filesystem' "
            "requires optional dependency cryptography for Fernet construction; "
            "provider-free tests may monkeypatch _create_fernet_from_key"
        ) from exc
    fernet_cls = cast(Any, fernet_module).Fernet
    try:
        fernet = fernet_cls(key)
    except (ValueError, TypeError) as exc:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'encrypted_filesystem' "
            "encryption key is malformed (expected a urlsafe-base64-encoded "
            "32-byte Fernet key)"
        ) from exc
    return cast(FernetLike, fernet)


def _construct_encrypted_filesystem_backend(
    config: RuntimeConfig,
) -> LocalFilesystemMemoryToolBackend:
    """Construct the ENCRYPTED_FILESYSTEM backend (`B-MEMORY-SURFACE-BACKEND-IMPLS`).

    The same `LocalFilesystemMemoryToolBackend` rooted at a distinct
    `.harness/memories-encrypted` path, with a `FernetContentCodec` injected so
    at-rest content is ciphertext (spec §14.12.3 ENCRYPTED_FILESYSTEM step).
    """
    params = _require_backend_params(
        config.memory_tool_backend_config,
        backend=MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM,
    )
    key = _resolve_memory_encryption_key(params)
    fernet = _create_fernet_from_key(key)
    return LocalFilesystemMemoryToolBackend(
        root=config.repository_root / MEMORY_TOOL_ENCRYPTED_FILESYSTEM_ROOT_SUBPATH,
        codec=FernetContentCodec(fernet),
    )


def _resolve_class_qualified_name(qualified_name: str) -> type[Any]:
    """Resolve a class-qualified name to a class via importlib introspection.

    Accepts `module.path:ClassName` (entry-point style) and
    `module.path.ClassName` (dotted, last segment = class). This ESTABLISHES the
    canonical class-qualified-name introspection convention that the validator
    framework resolution follows per spec §14.12.7 + §14.13.1 ("validator
    class-qualified-name resolution must use the same introspection-based
    discipline as MemoryToolStorageBackend.OPERATOR_DEFINED").

    Importing an operator-supplied module IS by design — OPERATOR_DEFINED is
    operator-TRUSTED config (spec §14.12.3), not an arbitrary-exec surface.
    Raises `MemoryBackendResolutionError` on malformed reference / import /
    attribute / non-class resolution.
    """
    if ":" in qualified_name:
        module_name, _, attr = qualified_name.partition(":")
    else:
        module_name, _, attr = qualified_name.rpartition(".")
    if not module_name or not attr:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'operator_defined' "
            f"class_qualified_name {qualified_name!r} is not a valid "
            f"'module:Class' or 'module.Class' reference"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'operator_defined' "
            f"module {module_name!r} (from {qualified_name!r}) failed to import: {exc}"
        ) from exc
    candidate = getattr(module, attr, None)
    if candidate is None:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'operator_defined' "
            f"attribute {attr!r} not found in module {module_name!r} "
            f"(from {qualified_name!r})"
        )
    if not isinstance(candidate, type):
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'operator_defined' "
            f"reference {qualified_name!r} resolved to a non-class object of "
            f"type {type(candidate).__name__!r}"
        )
    return candidate


def _construct_operator_defined_backend(config: RuntimeConfig) -> MemoryToolStorageBackendProtocol:
    """Construct the OPERATOR_DEFINED backend (`B-MEMORY-SURFACE-BACKEND-IMPLS`).

    Resolves `backend_params['class_qualified_name']` to a class, then
    instantiates it with the full `backend_params` Mapping as a single
    positional argument (the operator backend's `__init__(self, backend_params)`
    pulls whatever keys it needs). Protocol conformance is enforced by the
    caller's `_enforce_protocol_conformance` (spec §14.12.5 invariant 2), so a
    non-conformant operator class is rejected there. Spec §14.12.3 OPERATOR_DEFINED
    step; introspection mechanism per §14.12.7 impl-discretion.

    Path discipline (§14.12.5 invariant 3 — `/memories/` scope validated BEFORE
    I/O) is the OPERATOR class's responsibility: unlike the built-in backends,
    the harness cannot enforce it for an arbitrary operator implementation (the
    conformance gate checks the Protocol surface, not the scope-validation body).
    """
    params = _require_backend_params(
        config.memory_tool_backend_config,
        backend=MemoryToolStorageBackend.OPERATOR_DEFINED,
    )
    qualified_name = params.get("class_qualified_name")
    if not qualified_name:
        raise MemoryBackendResolutionError(
            "RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'operator_defined' "
            "requires backend_params['class_qualified_name'] (e.g. "
            "'my_pkg.my_module:MyBackend' or 'my_pkg.my_module.MyBackend')"
        )
    backend_cls = _resolve_class_qualified_name(qualified_name)
    try:
        instance = backend_cls(params)
    except Exception as exc:
        # Do NOT interpolate `{exc}` — operator backend_params may carry secrets
        # and the operator's __init__ exception text could echo them (Codex
        # review; mirrors the encrypted-path no-key-echo discipline + §14.12.5
        # invariant 4 structure-not-content). Surface the exception TYPE only;
        # the underlying exception stays chained for traceback debugging.
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend 'operator_defined' "
            f"class {qualified_name!r} raised {type(exc).__name__} during "
            f"construction (backend_params values are not echoed)"
        ) from exc
    return cast(MemoryToolStorageBackendProtocol, instance)


PROTOCOL_REQUIRED_METHODS: tuple[str, ...] = (
    "view",
    "create",
    "delete",
    "str_replace",
    "insert",
)
"""The 5 CRUD callbacks per ADR-D3 v1.2 §1.1 #11 + runtime spec v1.17
§14.12.1 `MemoryToolStorageBackendProtocol`. Used for the defensive
post-isinstance method-presence sweep at step 3 below."""


def _construct_backend(
    configured: MemoryToolStorageBackend,
    config: RuntimeConfig,
) -> MemoryToolStorageBackendProtocol:
    """Construct the storage-backend implementation for a resolved enum value.

    Per spec §14.12.3 step 2 — all 5 `MemoryToolStorageBackend` members are
    handled (FILESYSTEM / DATABASE (SQLite or managed-DB) / S3 at R-830;
    ENCRYPTED_FILESYSTEM / OPERATOR_DEFINED at `B-MEMORY-SURFACE-BACKEND-IMPLS`).
    The `assert_never` tail makes the match exhaustive (a future enum member
    without a branch is a type-check error). Missing/invalid `backend_params`
    (S3 bucket, managed-DB connection string, encryption key reference,
    operator class-qualified-name) raise `RT-FAIL-MEMORY-BACKEND-RESOLUTION`
    from the per-backend constructors.
    """
    if configured is MemoryToolStorageBackend.FILESYSTEM:
        return LocalFilesystemMemoryToolBackend(
            root=config.repository_root / MEMORY_TOOL_FILESYSTEM_ROOT_SUBPATH,
        )
    if configured is MemoryToolStorageBackend.ENCRYPTED_FILESYSTEM:
        # B-MEMORY-SURFACE-BACKEND-IMPLS: filesystem backend + injected Fernet
        # content codec (ciphertext at rest). Provider-free construction may
        # monkeypatch _create_fernet_from_key; live use reads the key reference
        # from the operator-named environment variable.
        return _construct_encrypted_filesystem_backend(config)
    if configured is MemoryToolStorageBackend.S3:
        # R-830 MANAGED_CLOUD cloud-vault backend. Provider-free construction
        # reaches this path through a monkeypatched client factory; live use
        # requires boto3 + operator-provided credentials and bucket params.
        return _construct_s3_backend(config)
    if configured is MemoryToolStorageBackend.DATABASE:
        # R-830 DATABASE backend. Plain/local connection strings route to the
        # existing SQLite implementation; postgres:// and postgresql:// route
        # to the optional managed-DB implementation.
        return _construct_database_backend(config)
    if configured is MemoryToolStorageBackend.OPERATOR_DEFINED:
        # B-MEMORY-SURFACE-BACKEND-IMPLS: operator class resolved from
        # backend_params['class_qualified_name'] via importlib introspection.
        return _construct_operator_defined_backend(config)
    assert_never(configured)


def _enforce_protocol_conformance(
    backend: MemoryToolStorageBackendProtocol,
    configured: MemoryToolStorageBackend,
) -> None:
    """Enforce `MemoryToolStorageBackendProtocol` per §14.12.5 invariant 2.

    Three-layer check, all of which `@runtime_checkable` isinstance admits (PEP
    544 only verifies attribute PRESENCE): (a) attribute-present sweep over the 5
    CRUD methods; (b) callable sweep (catches non-callable shadowing); (c)
    coroutine-function sweep — the Protocol callbacks are `async def` and the
    dispatcher `await`s them, so a sync `def` method must fail CLOSED at bootstrap
    (ADR-F4) rather than `TypeError` at first dispatch (matters for the arbitrary
    OPERATOR_DEFINED class; Codex review). Then the canonical `@runtime_checkable`
    isinstance. Raises `RT-FAIL-MEMORY-BACKEND-RESOLUTION` on any failure.
    """
    missing = tuple(name for name in PROTOCOL_REQUIRED_METHODS if not hasattr(backend, name))
    if missing:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: constructed backend for "
            f"{configured.value!r} does not satisfy "
            f"MemoryToolStorageBackendProtocol "
            f"(missing methods: {missing!r})"
        )
    non_callable = tuple(
        name for name in PROTOCOL_REQUIRED_METHODS if not callable(getattr(backend, name))
    )
    if non_callable:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend for "
            f"{configured.value!r} has non-callable Protocol method(s): "
            f"{non_callable!r}"
        )
    non_async = tuple(
        name
        for name in PROTOCOL_REQUIRED_METHODS
        if not inspect.iscoroutinefunction(getattr(backend, name))
    )
    if non_async:
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: backend for "
            f"{configured.value!r} has non-async Protocol method(s): "
            f"{non_async!r} (the MemoryToolStorageBackendProtocol callbacks are "
            f"async; the dispatcher awaits them — a sync def fails closed here "
            f"rather than raising TypeError at dispatch)"
        )
    # pyright sees `backend` as a concrete subtype at the override callsite
    # (the only constructor reached), but the isinstance is the spec §14.12.5
    # invariant 2 canonical Protocol assertion — kept intentionally as
    # defense-in-depth for tests that inject incomplete backends via
    # constructor monkey-patching (AC #7).
    if not isinstance(backend, MemoryToolStorageBackendProtocol):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise MemoryBackendResolutionError(
            f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: constructed backend for "
            f"{configured.value!r} does not satisfy "
            f"MemoryToolStorageBackendProtocol isinstance check"
        )


async def materialize_memory_tool_registry_stage(
    config: RuntimeConfig,
    ctx: _MutableHarnessContext,
) -> MemoryToolRegistry:
    """Compose the Memory tool storage-backend registry and bind to ctx.

    Mutates `ctx` in-place: binds `ctx.memory_tool_registry` to the
    constructed registry. Returns the registry for the stage-5 callsite to
    inspect.

    Resolution (R-FS-1 arc B5 — realizes the §14.12.1 surface-parametric
    reading; all resolution at bootstrap per §14.12.5 invariant 1):

    - **Operator override** (`memory_tool_backend_config` present): the named
      backend is forced for EVERY deployment surface per §14.12.1; construct it
      once and bind via the single-backend constructor. A construction failure
      (e.g. S3 without a bucket param) aborts bootstrap fail-closed.
    - **Default** (no override): resolve each `DeploymentSurface` independently.
      A surface admitting the config-free `FILESYSTEM` backend resolves to it
      (one shared instance per distinct enum); a surface that does NOT (e.g.
      `MANAGED_CLOUD`, which admits `{s3, database}` — both requiring operator
      `backend_params`) resolves to a frozen deferred
      `RT-FAIL-MEMORY-BACKEND-RESOLUTION`. The ACTIVE
      `config.deployment_surface` MUST resolve, else bootstrap aborts
      fail-closed (preserving the pre-B5 active-surface behavior).

    Per spec v1.17 §14.12.3 + plan v2.15 §1 U-RT-80 ACs.
    """
    if config.memory_tool_backend_config is not None:
        # --- Operator override: one backend, EVERY surface (§14.12.1) --------
        configured = config.memory_tool_backend_config.backend
        override_backend = _construct_backend(configured, config)
        _enforce_protocol_conformance(override_backend, configured)
        registry = MemoryToolRegistry(backend=override_backend, configured_backend=configured)
        ctx.memory_tool_registry = registry
        return registry

    # --- Default path: per-surface config-free resolution (R-FS-1 B5) --------
    backends: dict[DeploymentSurface, MemoryToolStorageBackendProtocol] = {}
    resolution_errors: dict[DeploymentSurface, str] = {}
    enum_by_surface: dict[DeploymentSurface, MemoryToolStorageBackend] = {}
    backend_by_enum: dict[MemoryToolStorageBackend, MemoryToolStorageBackendProtocol] = {}
    for surface in DeploymentSurface:
        admissible = memory_tool_storage_backend(surface)
        if MemoryToolStorageBackend.FILESYSTEM not in admissible:
            # No config-free backend for this surface — its admissible backends
            # (S3 / DATABASE) all require operator-supplied backend_params, so a
            # no-override config cannot provision them. Freeze a fail-closed
            # deferred raise (realizes §14.12.1 "backend not available for the
            # surface"); raised only if this surface is queried.
            resolution_errors[surface] = (
                f"RT-FAIL-MEMORY-BACKEND-RESOLUTION: deployment surface "
                f"{surface.value!r} admits backends "
                f"{sorted(b.value for b in admissible)!r}, none of which the "
                f"no-config default resolver provisions (it provisions only the "
                f"config-free {MemoryToolStorageBackend.FILESYSTEM.value!r} backend); "
                f"supply memory_tool_backend_config to select an admissible backend "
                f"for this surface (spec §14.12.1 + fork-doc §14.D)"
            )
            continue
        picked = MemoryToolStorageBackend.FILESYSTEM
        if picked not in backend_by_enum:
            constructed = _construct_backend(picked, config)
            _enforce_protocol_conformance(constructed, picked)
            backend_by_enum[picked] = constructed
        backends[surface] = backend_by_enum[picked]
        enum_by_surface[surface] = picked

    # The ACTIVE surface must resolve, else bootstrap aborts fail-closed
    # (preserves the pre-B5 active-MANAGED_CLOUD-no-override behavior).
    active = config.deployment_surface
    if active not in backends:
        raise MemoryBackendResolutionError(resolution_errors[active])

    registry = MemoryToolRegistry.from_surface_map(
        backends=backends,
        resolution_errors=resolution_errors,
        configured_backend=enum_by_surface[active],
    )
    ctx.memory_tool_registry = registry
    return registry
