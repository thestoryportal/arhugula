"""U-RT-17 — `AnthropicAdapter` + `construct_anthropic_adapter` tests.

ACs per Phase 2 Session 3 Track A plan v2.1 §L4 U-RT-17:
- Client constructs (with resolved secret).
- Async ping succeeds; close idempotent and awaitable.
- Structural conformance to `ProviderClient` Protocol.

Failure modes (spec §5 lines 367-371) — covered:
- `RT-FAIL-SECRET-MISSING` → `ProviderSecretMissingError`.
- `RT-FAIL-TRANSIENT`      → `ProviderTransientError`.
- `RT-FAIL-PROVIDER-AUTH`  → `ProviderAuthError`.

Test convention notes:
- No live network calls. The SDK constructor and the async ping are both
  injected per the workspace pyright-strict-clean fake convention; matches
  `test_lifecycle_mcp_host.py` (placeholder primitive, no real FastMCP).
- The `_FakeKeyring` backend fixture is duplicated from
  `test_config_provider_secrets.py` to keep test modules self-contained.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import keyring
import pytest
from anthropic import (
    APIConnectionError,
    AsyncAnthropic,
    AuthenticationError,
)
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.config.provider_secrets import (
    ProviderSecretResolver,
    make_keyring_resolver,
)
from harness_runtime.lifecycle.providers import (
    ANTHROPIC_KEYRING_NAME,
    DEFAULT_STAGE_3A_MAX_ATTEMPTS,
    OPENAI_KEYRING_NAME,
    AnthropicAdapter,
    EmptyProviderCoverageError,
    OllamaAdapter,
    OpenAIAdapter,
    ProviderAuthError,
    ProviderCapabilityBinding,
    ProviderCapabilityBindings,
    ProviderClientsStage,
    ProviderDegradedWarning,
    ProviderNoneConfiguredError,
    ProviderSecretMissingError,
    ProviderTransientError,
    construct_anthropic_adapter,
    construct_ollama_adapter,
    construct_openai_adapter,
    materialize_capability_bindings,
    materialize_provider_clients_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    ExternalCLIProviderConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderClient,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from keyring.backend import KeyringBackend
from openai import APIConnectionError as OpenAIConnectionError
from openai import AsyncOpenAI
from openai import AuthenticationError as OpenAIAuthError

# ---------------------------------------------------------------------------
# Lightweight subclasses that satisfy `isinstance(exc, AuthenticationError)`
# / `isinstance(exc, APIConnectionError)` without going through the SDK's
# heavyweight `__init__` (which expects a real `httpx.Response` /
# `httpx.Request`). The adapter only does an isinstance check.
# ---------------------------------------------------------------------------


class _FakeAuthError(AuthenticationError):
    def __init__(self, message: str = "fake 401") -> None:
        Exception.__init__(self, message)


class _FakeConnectionError(APIConnectionError):
    def __init__(self, message: str = "fake connection refused") -> None:
        Exception.__init__(self, message)


class _FakeOpenAIAuthError(OpenAIAuthError):
    def __init__(self, message: str = "fake 401") -> None:
        Exception.__init__(self, message)


class _FakeOpenAIConnectionError(OpenAIConnectionError):
    def __init__(self, message: str = "fake connection refused") -> None:
        Exception.__init__(self, message)


# ---------------------------------------------------------------------------
# Fixtures — in-memory keyring + minimal RuntimeConfig.
# ---------------------------------------------------------------------------


class _FakeKeyring(KeyringBackend):
    """In-memory keyring backend (mirrors `test_config_provider_secrets.py`)."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeKeyring]:
    """Install an in-memory keyring for the duration of one test."""
    backend = _FakeKeyring()
    original = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        yield backend
    finally:
        keyring.set_keyring(original)


def _runtime_config(tmp_path: Path) -> RuntimeConfig:
    """Minimal `RuntimeConfig` for adapter-construction tests.

    Adapter construction at U-RT-17 doesn't consume `config` beyond
    signature symmetry; the value is only meaningful at U-RT-18/19 (Ollama
    host + optional flag). Built here so the U-RT-17 test signature
    matches what U-RT-18/19 will exercise.
    """
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _resolver_with_anthropic_key(value: str = "sk-ant-fake-test") -> ProviderSecretResolver:
    """Seed a keyring entry for `anthropic_key` and return a resolver.

    Caller must hold the `fake_keyring` fixture so the backend is installed.
    """
    keyring.set_password("harness", ANTHROPIC_KEYRING_NAME, value)
    return make_keyring_resolver(ProviderSecretsConfig())


# ---------------------------------------------------------------------------
# Fake SDK client — minimal surface to satisfy adapter construction + close.
# ---------------------------------------------------------------------------


class _FakeAsyncAnthropic:
    """Fake `AsyncAnthropic` for tests; records construction + close-count.

    Surfaces only the `.close()` coroutine the adapter calls. The ping is
    injected separately via `ping_override`, so this fake doesn't need
    `.models.list()`.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def _factory(records: list[str]) -> Callable[[str], AsyncAnthropic]:
    """Build a `client_factory` callable that records the api_key it receives.

    Returns an `AsyncAnthropic`-typed object via `cast` to satisfy the
    `client_factory: Callable[[str], AsyncAnthropic]` parameter type. The
    real `AsyncAnthropic` is duck-equivalent at the `.close()` call site,
    which is all the adapter uses post-construction at U-RT-17.
    """

    def factory(api_key: str) -> AsyncAnthropic:
        records.append(api_key)
        return cast(AsyncAnthropic, _FakeAsyncAnthropic(api_key=api_key))

    return factory


class _FakeAsyncOpenAI:
    """Fake `AsyncOpenAI` (U-RT-18). Same surface shape as `_FakeAsyncAnthropic`."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def _openai_factory(records: list[str]) -> Callable[[str], AsyncOpenAI]:
    def factory(api_key: str) -> AsyncOpenAI:
        records.append(api_key)
        return cast(AsyncOpenAI, _FakeAsyncOpenAI(api_key=api_key))

    return factory


def _seed_openai_key(value: str = "sk-openai-fake-test") -> ProviderSecretResolver:
    keyring.set_password("harness", OPENAI_KEYRING_NAME, value)
    return make_keyring_resolver(ProviderSecretsConfig())


# ---------------------------------------------------------------------------
# Happy path: construct + ping + idempotent close.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_anthropic_adapter_happy_path(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """End-to-end happy path: secret resolves → client constructs → ping passes."""
    resolver = _resolver_with_anthropic_key("sk-ant-happy")
    config = _runtime_config(tmp_path)
    captured_keys: list[str] = []
    ping_call_count = 0

    async def stub_ping() -> None:
        nonlocal ping_call_count
        ping_call_count += 1

    adapter = await construct_anthropic_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_factory(captured_keys),  # type: ignore[arg-type]
    )

    assert isinstance(adapter, AnthropicAdapter)
    assert captured_keys == ["sk-ant-happy"]
    assert ping_call_count == 1


@pytest.mark.asyncio
async def test_anthropic_adapter_aclose_is_idempotent(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`aclose()` calls SDK `close()` exactly once even on repeated invocation."""
    resolver = _resolver_with_anthropic_key()
    config = _runtime_config(tmp_path)

    async def stub_ping() -> None:
        return None

    captured: list[str] = []
    adapter = await construct_anthropic_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_factory(captured),  # type: ignore[arg-type]
    )

    # The fake client tracks close_count; downcast via the captured handle.
    fake_client = cast(_FakeAsyncAnthropic, adapter.client)
    assert fake_client.close_count == 0
    await adapter.aclose()
    assert fake_client.close_count == 1
    await adapter.aclose()  # idempotent
    await adapter.aclose()  # idempotent
    assert fake_client.close_count == 1


# ---------------------------------------------------------------------------
# Protocol conformance.
# ---------------------------------------------------------------------------


def test_anthropic_adapter_satisfies_provider_client_protocol(
    fake_keyring: _FakeKeyring,
) -> None:
    """`AnthropicAdapter` structurally satisfies `ProviderClient` (runtime check).

    Per C-RT-05 v1.1 `@runtime_checkable` Protocol — `isinstance(..., ProviderClient)`
    must be True for every adapter.
    """

    async def noop_ping() -> None:
        return None

    fake = cast(AsyncAnthropic, _FakeAsyncAnthropic(api_key="x"))
    adapter = AnthropicAdapter(client=fake, ping=noop_ping)
    assert isinstance(adapter, ProviderClient)


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_secret_raises_provider_secret_missing(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """No `anthropic_key` in keyring → `ProviderSecretMissingError`."""
    # Note: do NOT seed the keyring.
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config(tmp_path)

    async def unreached_ping() -> None:  # pragma: no cover — must not be invoked
        raise AssertionError("ping should not run when secret is missing")

    with pytest.raises(ProviderSecretMissingError) as excinfo:
        await construct_anthropic_adapter(
            config,
            resolver,
            ping_override=unreached_ping,
            client_factory=_factory([]),  # type: ignore[arg-type]
        )
    assert excinfo.value.provider == "anthropic"
    assert excinfo.value.keyring_name == ANTHROPIC_KEYRING_NAME


@pytest.mark.asyncio
async def test_ping_auth_failure_raises_provider_auth_error(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """Anthropic `AuthenticationError` from ping → `ProviderAuthError` (permanent)."""
    resolver = _resolver_with_anthropic_key()
    config = _runtime_config(tmp_path)

    async def failing_ping() -> None:
        raise _FakeAuthError()

    with pytest.raises(ProviderAuthError) as excinfo:
        await construct_anthropic_adapter(
            config,
            resolver,
            ping_override=failing_ping,
            client_factory=_factory([]),  # type: ignore[arg-type]
        )
    assert excinfo.value.provider == "anthropic"


@pytest.mark.asyncio
async def test_ping_transient_failure_raises_provider_transient(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """Connection error from ping → `ProviderTransientError` (bounded retry upstream)."""
    resolver = _resolver_with_anthropic_key()
    config = _runtime_config(tmp_path)

    async def failing_ping() -> None:
        raise _FakeConnectionError()

    with pytest.raises(ProviderTransientError) as excinfo:
        await construct_anthropic_adapter(
            config,
            resolver,
            ping_override=failing_ping,
            client_factory=_factory([]),  # type: ignore[arg-type]
        )
    assert excinfo.value.provider == "anthropic"


@pytest.mark.asyncio
async def test_ping_typed_error_propagates_unwrapped(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """A ping that already raises `ProviderAuthError` is propagated as-is.

    Lets test-injected pings raise the typed exceptions directly without the
    adapter re-classifying them — keeps the test surface symmetric with how
    a future retry-loop wrapper might already-classified failures.
    """
    resolver = _resolver_with_anthropic_key()
    config = _runtime_config(tmp_path)
    original = ProviderAuthError("anthropic", RuntimeError("pre-classified"))

    async def already_typed_ping() -> None:
        raise original

    with pytest.raises(ProviderAuthError) as excinfo:
        await construct_anthropic_adapter(
            config,
            resolver,
            ping_override=already_typed_ping,
            client_factory=_factory([]),  # type: ignore[arg-type]
        )
    assert excinfo.value is original


# ---------------------------------------------------------------------------
# U-RT-18 — OpenAIAdapter parity tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_construct_openai_adapter_happy_path(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """Happy path for OpenAI: secret resolves → client constructs → ping passes."""
    resolver = _seed_openai_key("sk-openai-happy")
    config = _runtime_config(tmp_path)
    captured_keys: list[str] = []
    ping_call_count = 0

    async def stub_ping() -> None:
        nonlocal ping_call_count
        ping_call_count += 1

    adapter = await construct_openai_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_openai_factory(captured_keys),
    )

    assert isinstance(adapter, OpenAIAdapter)
    assert captured_keys == ["sk-openai-happy"]
    assert ping_call_count == 1


@pytest.mark.asyncio
async def test_openai_adapter_aclose_is_idempotent(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`aclose()` calls SDK `close()` exactly once even on repeated invocation."""
    resolver = _seed_openai_key()
    config = _runtime_config(tmp_path)

    async def stub_ping() -> None:
        return None

    adapter = await construct_openai_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_openai_factory([]),
    )

    fake_client = cast(_FakeAsyncOpenAI, adapter.client)
    assert fake_client.close_count == 0
    await adapter.aclose()
    assert fake_client.close_count == 1
    await adapter.aclose()
    await adapter.aclose()
    assert fake_client.close_count == 1


def test_openai_adapter_satisfies_provider_client_protocol(
    fake_keyring: _FakeKeyring,
) -> None:
    """Structural conformance to `ProviderClient` for OpenAI adapter."""

    async def noop_ping() -> None:
        return None

    fake = cast(AsyncOpenAI, _FakeAsyncOpenAI(api_key="x"))
    adapter = OpenAIAdapter(client=fake, ping=noop_ping)
    assert isinstance(adapter, ProviderClient)


@pytest.mark.asyncio
async def test_openai_missing_secret_raises_provider_secret_missing(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """No `openai_key` in keyring → `ProviderSecretMissingError`."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config(tmp_path)

    async def unreached_ping() -> None:  # pragma: no cover
        raise AssertionError("ping should not run when secret is missing")

    with pytest.raises(ProviderSecretMissingError) as excinfo:
        await construct_openai_adapter(
            config,
            resolver,
            ping_override=unreached_ping,
            client_factory=_openai_factory([]),
        )
    assert excinfo.value.provider == "openai"
    assert excinfo.value.keyring_name == OPENAI_KEYRING_NAME


@pytest.mark.asyncio
async def test_openai_ping_auth_failure_raises_provider_auth_error(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """OpenAI `AuthenticationError` from ping → `ProviderAuthError`."""
    resolver = _seed_openai_key()
    config = _runtime_config(tmp_path)

    async def failing_ping() -> None:
        raise _FakeOpenAIAuthError()

    with pytest.raises(ProviderAuthError) as excinfo:
        await construct_openai_adapter(
            config,
            resolver,
            ping_override=failing_ping,
            client_factory=_openai_factory([]),
        )
    assert excinfo.value.provider == "openai"


@pytest.mark.asyncio
async def test_openai_ping_transient_failure_raises_provider_transient(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """OpenAI `APIConnectionError` from ping → `ProviderTransientError`."""
    resolver = _seed_openai_key()
    config = _runtime_config(tmp_path)

    async def failing_ping() -> None:
        raise _FakeOpenAIConnectionError()

    with pytest.raises(ProviderTransientError) as excinfo:
        await construct_openai_adapter(
            config,
            resolver,
            ping_override=failing_ping,
            client_factory=_openai_factory([]),
        )
    assert excinfo.value.provider == "openai"


# ---------------------------------------------------------------------------
# U-RT-19 — OllamaAdapter + materialize_provider_clients_stage tests.
# ---------------------------------------------------------------------------


class _FakeAsyncOllama:
    """Fake `ollama.AsyncClient` for tests; records host + close-count."""

    def __init__(self, host: str | None) -> None:
        self.host = host
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


def _ollama_factory(
    records: list[str | None],
) -> Callable[[str | None], object]:
    """Build a client_factory recording the `host` it receives.

    Returns `object` rather than `ollama.AsyncClient` to dodge the strict
    nominal-typing constraint at the construct callsite; the adapter only
    uses `.close()`. Cast to `AsyncOllamaClient` happens via cast() at the
    use site (cf. _openai_factory pattern).
    """

    def factory(host: str | None) -> object:
        records.append(host)
        return _FakeAsyncOllama(host=host)

    return factory


def _runtime_config_with_ollama(
    tmp_path: Path,
    *,
    ollama_host: str | None = None,
    ollama_optional: bool = False,
    anthropic_optional: bool = False,
    openai_optional: bool = False,
    enabled_provider_names: tuple[str, ...] = ("anthropic", "openai", "ollama"),
    external_cli_providers: tuple[ExternalCLIProviderConfig, ...] = (),
) -> RuntimeConfig:
    """`RuntimeConfig` with explicit Ollama config — for U-RT-19 tests."""
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        ollama_host=ollama_host,
        ollama_optional=ollama_optional,
        anthropic_optional=anthropic_optional,
        openai_optional=openai_optional,
        enabled_provider_names=enabled_provider_names,
        external_cli_providers=external_cli_providers,
    )


@pytest.mark.asyncio
async def test_construct_ollama_adapter_happy_path(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """Happy path for Ollama: client constructs (no secret) → ping passes."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path, ollama_host="http://my-ollama:11434")
    captured_hosts: list[str | None] = []
    ping_call_count = 0

    async def stub_ping() -> None:
        nonlocal ping_call_count
        ping_call_count += 1

    adapter = await construct_ollama_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_ollama_factory(captured_hosts),  # type: ignore[arg-type]
    )

    assert isinstance(adapter, OllamaAdapter)
    assert captured_hosts == ["http://my-ollama:11434"]
    assert ping_call_count == 1


@pytest.mark.asyncio
async def test_ollama_adapter_default_host_is_none(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`ollama_host=None` → adapter passes `None` to SDK (SDK applies its default)."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)  # ollama_host=None default
    captured: list[str | None] = []

    async def stub_ping() -> None:
        return None

    await construct_ollama_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_ollama_factory(captured),  # type: ignore[arg-type]
    )
    assert captured == [None]


@pytest.mark.asyncio
async def test_ollama_adapter_aclose_is_idempotent(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`aclose()` calls SDK `close()` exactly once even on repeated invocation."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)

    async def stub_ping() -> None:
        return None

    adapter = await construct_ollama_adapter(
        config,
        resolver,
        ping_override=stub_ping,
        client_factory=_ollama_factory([]),  # type: ignore[arg-type]
    )

    fake_client = cast(_FakeAsyncOllama, adapter.client)
    assert fake_client.close_count == 0
    await adapter.aclose()
    assert fake_client.close_count == 1
    await adapter.aclose()
    await adapter.aclose()
    assert fake_client.close_count == 1


def test_ollama_adapter_satisfies_provider_client_protocol(
    fake_keyring: _FakeKeyring,
) -> None:
    """Structural conformance to `ProviderClient` for Ollama adapter."""

    async def noop_ping() -> None:
        return None

    fake = cast("object", _FakeAsyncOllama(host=None))
    # Adapter holds an opaque client; cast to the SDK type for typing.
    from ollama import AsyncClient as AsyncOllamaClient  # local to mirror module pattern

    adapter = OllamaAdapter(client=cast(AsyncOllamaClient, fake), ping=noop_ping)
    assert isinstance(adapter, ProviderClient)


@pytest.mark.asyncio
async def test_ollama_ping_failure_raises_provider_transient(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """ConnectionError from ping → `ProviderTransientError` (no auth class for Ollama)."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)

    async def failing_ping() -> None:
        raise ConnectionError("daemon unreachable")

    with pytest.raises(ProviderTransientError) as excinfo:
        await construct_ollama_adapter(
            config,
            resolver,
            ping_override=failing_ping,
            client_factory=_ollama_factory([]),  # type: ignore[arg-type]
        )
    assert excinfo.value.provider == "ollama"


# ---------------------------------------------------------------------------
# materialize_provider_clients_stage — happy + degraded + hard-fail paths.
# ---------------------------------------------------------------------------


def _ready_adapter(provider: str) -> ProviderClient:
    """Build a minimal `ProviderClient` instance for stage-injection tests.

    Uses `AnthropicAdapter` regardless of `provider` — it's the simplest
    concrete shape and structurally satisfies `ProviderClient`. The provider
    name is only used in the stage dict key.
    """

    async def noop_ping() -> None:
        return None

    fake = cast(AsyncAnthropic, _FakeAsyncAnthropic(api_key=f"sk-{provider}-stub"))
    return AnthropicAdapter(client=fake, ping=noop_ping)


@pytest.mark.asyncio
async def test_materialize_stage_three_providers_happy_path(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """All three constructors succeed → 3-entry providers dict."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)

    async def anthropic_c() -> ProviderClient:
        return _ready_adapter("anthropic")

    async def openai_c() -> ProviderClient:
        return _ready_adapter("openai")

    async def ollama_c() -> ProviderClient:
        return _ready_adapter("ollama")

    stage = await materialize_provider_clients_stage(
        config,
        resolver,
        anthropic_construct=anthropic_c,
        openai_construct=openai_c,
        ollama_construct=ollama_c,
    )

    assert isinstance(stage, ProviderClientsStage)
    assert set(stage.providers.keys()) == {"anthropic", "openai", "ollama"}
    for key in ("anthropic", "openai", "ollama"):
        assert isinstance(stage.providers[key], ProviderClient)


@pytest.mark.asyncio
async def test_materialize_stage_respects_enabled_provider_names_for_external_cli(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """CLI-only config constructs only the named external provider."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    cli_config = ExternalCLIProviderConfig(
        provider="claude_code",
        kind="claude-code",
        command="claude",
    )
    config = _runtime_config_with_ollama(
        tmp_path,
        enabled_provider_names=("claude_code",),
        external_cli_providers=(cli_config,),
    )
    external_seen: list[str] = []

    async def external_c(provider_config: ExternalCLIProviderConfig) -> ProviderClient:
        external_seen.append(provider_config.provider)
        return _ready_adapter(provider_config.provider)

    async def disabled_builtin() -> ProviderClient:
        raise AssertionError("disabled built-in provider should not construct")

    stage = await materialize_provider_clients_stage(
        config,
        resolver,
        anthropic_construct=disabled_builtin,
        openai_construct=disabled_builtin,
        ollama_construct=disabled_builtin,
        external_cli_construct=external_c,
    )

    assert external_seen == ["claude_code"]
    assert set(stage.providers) == {"claude_code"}


@pytest.mark.asyncio
async def test_materialize_stage_errors_when_enabled_external_provider_missing_config(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """An enabled external provider must have a matching config entry."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(
        tmp_path,
        enabled_provider_names=("claude_code",),
        external_cli_providers=(),
    )

    with pytest.raises(ProviderNoneConfiguredError, match="claude_code"):
        await materialize_provider_clients_stage(config, resolver)


@pytest.mark.asyncio
async def test_materialize_stage_bounded_retry_on_transient(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """Transient failure retries up to max_attempts; succeeds on the last attempt."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)
    attempt_count = 0

    async def flaky_anthropic() -> ProviderClient:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < DEFAULT_STAGE_3A_MAX_ATTEMPTS:
            raise ProviderTransientError("anthropic", RuntimeError(f"attempt {attempt_count}"))
        return _ready_adapter("anthropic")

    async def stable_openai() -> ProviderClient:
        return _ready_adapter("openai")

    async def stable_ollama() -> ProviderClient:
        return _ready_adapter("ollama")

    stage = await materialize_provider_clients_stage(
        config,
        resolver,
        anthropic_construct=flaky_anthropic,
        openai_construct=stable_openai,
        ollama_construct=stable_ollama,
    )
    assert attempt_count == DEFAULT_STAGE_3A_MAX_ATTEMPTS
    assert "anthropic" in stage.providers


@pytest.mark.asyncio
async def test_materialize_stage_persistent_transient_escalates(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """Persistent transient (all max_attempts fail) → ProviderTransientError raised."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)
    attempt_count = 0

    async def always_transient() -> ProviderClient:
        nonlocal attempt_count
        attempt_count += 1
        raise ProviderTransientError("anthropic", RuntimeError("network down"))

    async def unreached() -> ProviderClient:  # pragma: no cover
        raise AssertionError("subsequent providers should not be constructed")

    with pytest.raises(ProviderTransientError):
        await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=always_transient,
            openai_construct=unreached,
            ollama_construct=unreached,
        )
    assert attempt_count == DEFAULT_STAGE_3A_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_materialize_stage_auth_error_no_retry(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`ProviderAuthError` is permanent — no retry, no further providers attempted."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)
    attempt_count = 0

    async def auth_fail() -> ProviderClient:
        nonlocal attempt_count
        attempt_count += 1
        raise ProviderAuthError("anthropic", RuntimeError("401"))

    async def unreached() -> ProviderClient:  # pragma: no cover
        raise AssertionError("subsequent providers should not be constructed")

    with pytest.raises(ProviderAuthError):
        await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=auth_fail,
            openai_construct=unreached,
            ollama_construct=unreached,
        )
    assert attempt_count == 1


@pytest.mark.asyncio
async def test_materialize_stage_ollama_optional_degraded(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`ollama_optional=True` + Ollama transient → degraded warning + 2-provider dict.

    Spec §5 line 371: 'Surface typed warning; stage continues with 2-provider
    context'.
    """
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path, ollama_optional=True)

    async def stable_anthropic() -> ProviderClient:
        return _ready_adapter("anthropic")

    async def stable_openai() -> ProviderClient:
        return _ready_adapter("openai")

    async def ollama_down() -> ProviderClient:
        raise ProviderTransientError("ollama", ConnectionError("daemon unreachable"))

    with pytest.warns(ProviderDegradedWarning):
        stage = await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=stable_anthropic,
            openai_construct=stable_openai,
            ollama_construct=ollama_down,
        )

    assert set(stage.providers.keys()) == {"anthropic", "openai"}
    assert "ollama" not in stage.providers


@pytest.mark.asyncio
async def test_materialize_stage_ollama_not_optional_hard_fail(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`ollama_optional=False` (default) + Ollama unreachable → hard stage-3a failure.

    Multi-LLM commitment per ADR-F1 v1.2: Ollama unreachability is not silently
    absorbed unless the operator explicitly opted into degraded mode.
    """
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)  # ollama_optional=False default

    async def stable_anthropic() -> ProviderClient:
        return _ready_adapter("anthropic")

    async def stable_openai() -> ProviderClient:
        return _ready_adapter("openai")

    async def ollama_down() -> ProviderClient:
        raise ProviderTransientError("ollama", ConnectionError("daemon unreachable"))

    with pytest.raises(ProviderTransientError) as excinfo:
        await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=stable_anthropic,
            openai_construct=stable_openai,
            ollama_construct=ollama_down,
        )
    assert excinfo.value.provider == "ollama"


# ---------------------------------------------------------------------------
# E-prod-3 — per-provider optional tests (anthropic_optional + openai_optional).
# Per `.harness/class_1_fork_provider_construction_allowlist_semantic.md`
# operator-ratified 2026-05-28.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_stage_anthropic_optional_swallows_secret_missing(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`anthropic_optional=True` + ProviderSecretMissingError → degraded warning.

    This is the daemon-startup-unblock case from the original finding —
    operator without an `anthropic_key` keyring entry can start the daemon
    when `anthropic_optional=True`.
    """
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path, anthropic_optional=True)

    async def anthropic_no_key() -> ProviderClient:
        raise ProviderSecretMissingError("anthropic", ANTHROPIC_KEYRING_NAME)

    async def stable_openai() -> ProviderClient:
        return _ready_adapter("openai")

    async def stable_ollama() -> ProviderClient:
        return _ready_adapter("ollama")

    with pytest.warns(ProviderDegradedWarning):
        stage = await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=anthropic_no_key,
            openai_construct=stable_openai,
            ollama_construct=stable_ollama,
        )

    assert "anthropic" not in stage.providers
    assert set(stage.providers.keys()) == {"openai", "ollama"}


@pytest.mark.asyncio
async def test_materialize_stage_anthropic_optional_swallows_transient(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`anthropic_optional=True` + persistent transient → degraded warning."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path, anthropic_optional=True)

    async def anthropic_transient() -> ProviderClient:
        raise ProviderTransientError("anthropic", ConnectionError("api unreachable"))

    async def stable_openai() -> ProviderClient:
        return _ready_adapter("openai")

    async def stable_ollama() -> ProviderClient:
        return _ready_adapter("ollama")

    with pytest.warns(ProviderDegradedWarning):
        stage = await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=anthropic_transient,
            openai_construct=stable_openai,
            ollama_construct=stable_ollama,
        )

    assert "anthropic" not in stage.providers


@pytest.mark.asyncio
async def test_materialize_stage_anthropic_optional_does_not_swallow_auth_error(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`anthropic_optional=True` + `ProviderAuthError` → STILL RAISES.

    Auth errors indicate operator HAS a keyring entry but it's invalid — that
    misconfig must surface even with `*_optional=True`. Per fork doc §5.3
    operator-UX framing.
    """
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path, anthropic_optional=True)

    async def anthropic_auth_fail() -> ProviderClient:
        raise ProviderAuthError("anthropic", RuntimeError("401"))

    async def unreached() -> ProviderClient:  # pragma: no cover
        raise AssertionError("openai should not be reached after auth fail raises")

    with pytest.raises(ProviderAuthError):
        await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=anthropic_auth_fail,
            openai_construct=unreached,
            ollama_construct=unreached,
        )


@pytest.mark.asyncio
async def test_materialize_stage_openai_optional_swallows_secret_missing(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`openai_optional=True` + ProviderSecretMissingError → degraded warning."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path, openai_optional=True)

    async def stable_anthropic() -> ProviderClient:
        return _ready_adapter("anthropic")

    async def openai_no_key() -> ProviderClient:
        raise ProviderSecretMissingError("openai", OPENAI_KEYRING_NAME)

    async def stable_ollama() -> ProviderClient:
        return _ready_adapter("ollama")

    with pytest.warns(ProviderDegradedWarning):
        stage = await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=stable_anthropic,
            openai_construct=openai_no_key,
            ollama_construct=stable_ollama,
        )

    assert "openai" not in stage.providers
    assert set(stage.providers.keys()) == {"anthropic", "ollama"}


@pytest.mark.asyncio
async def test_materialize_stage_all_three_optional_all_degraded_raises_none_configured(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """All three optional + all three degrade → `ProviderNoneConfiguredError`.

    The post-loop empty-providers invariant. Surfacing at stage 3a (not
    stage 5 LLM-dispatcher) gives operators a clear error message.
    """
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(
        tmp_path,
        ollama_optional=True,
        anthropic_optional=True,
        openai_optional=True,
    )

    async def anthropic_no_key() -> ProviderClient:
        raise ProviderSecretMissingError("anthropic", ANTHROPIC_KEYRING_NAME)

    async def openai_no_key() -> ProviderClient:
        raise ProviderSecretMissingError("openai", OPENAI_KEYRING_NAME)

    async def ollama_down() -> ProviderClient:
        raise ProviderTransientError("ollama", ConnectionError("daemon unreachable"))

    with pytest.warns(ProviderDegradedWarning):
        with pytest.raises(ProviderNoneConfiguredError):
            await materialize_provider_clients_stage(
                config,
                resolver,
                anthropic_construct=anthropic_no_key,
                openai_construct=openai_no_key,
                ollama_construct=ollama_down,
            )


@pytest.mark.asyncio
async def test_materialize_stage_anthropic_not_optional_secret_missing_hard_fail(
    fake_keyring: _FakeKeyring, tmp_path: Path
) -> None:
    """`anthropic_optional=False` (default) + ProviderSecretMissingError → hard fail.

    Regression guard: default behavior unchanged at `anthropic_optional=False`.
    """
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    config = _runtime_config_with_ollama(tmp_path)  # all *_optional default False

    async def anthropic_no_key() -> ProviderClient:
        raise ProviderSecretMissingError("anthropic", ANTHROPIC_KEYRING_NAME)

    async def unreached() -> ProviderClient:  # pragma: no cover
        raise AssertionError("openai should not be reached when anthropic hard-fails")

    with pytest.raises(ProviderSecretMissingError):
        await materialize_provider_clients_stage(
            config,
            resolver,
            anthropic_construct=anthropic_no_key,
            openai_construct=unreached,
            ollama_construct=unreached,
        )


# ---------------------------------------------------------------------------
# U-RT-20 — capability-aware binding tests.
# ---------------------------------------------------------------------------


def _ready_stage_three_providers() -> ProviderClientsStage:
    """Build a 3-provider `ProviderClientsStage` directly (no async needed)."""
    return ProviderClientsStage(
        providers={
            "anthropic": _ready_adapter("anthropic"),
            "openai": _ready_adapter("openai"),
            "ollama": _ready_adapter("ollama"),
        }
    )


def _ready_stage_two_providers() -> ProviderClientsStage:
    """2-provider stage (Ollama degraded path)."""
    return ProviderClientsStage(
        providers={
            "anthropic": _ready_adapter("anthropic"),
            "openai": _ready_adapter("openai"),
        }
    )


def test_materialize_capability_bindings_three_providers(tmp_path: Path) -> None:
    """Happy path: 3 providers → 3 bindings; non-empty candidate set."""
    stage = _ready_stage_three_providers()
    config = _runtime_config_with_ollama(tmp_path)

    bindings = materialize_capability_bindings(stage, config)

    assert isinstance(bindings, ProviderCapabilityBindings)
    assert set(bindings.bindings.keys()) == {"anthropic", "openai", "ollama"}
    for name, binding in bindings.bindings.items():
        assert isinstance(binding, ProviderCapabilityBinding)
        assert binding.provider_name == name
        assert binding.adapter is stage.providers[name]
    # Local-development surface: 4 admissible engine classes per CP §7.2.
    assert len(bindings.engine_class_candidate_set) >= 1


def test_materialize_capability_bindings_degraded_two_providers(tmp_path: Path) -> None:
    """Degraded path (Ollama dropped): 2 bindings still satisfies coverage.

    Confirms the AC's 'each EngineClass resolves to ≥1 capable provider'
    holds under the 2-provider degraded context per spec §5 line 371.
    """
    stage = _ready_stage_two_providers()
    config = _runtime_config_with_ollama(tmp_path, ollama_optional=True)

    bindings = materialize_capability_bindings(stage, config)

    assert set(bindings.bindings.keys()) == {"anthropic", "openai"}
    assert len(bindings.bindings) == 2
    assert "ollama" not in bindings.bindings


def test_materialize_capability_bindings_empty_providers_raises(tmp_path: Path) -> None:
    """No providers landed → typed `EmptyProviderCoverageError`."""
    empty_stage = ProviderClientsStage(providers={})
    config = _runtime_config_with_ollama(tmp_path)

    with pytest.raises(EmptyProviderCoverageError) as excinfo:
        materialize_capability_bindings(empty_stage, config)
    assert "no providers landed" in excinfo.value.reason


def test_materialize_capability_bindings_engine_class_candidate_set_typed(
    tmp_path: Path,
) -> None:
    """Candidate set is a frozenset (typed; not list/tuple/dict).

    Per the AC 'capability assertions exhaustively typed'.
    """
    stage = _ready_stage_three_providers()
    config = _runtime_config_with_ollama(tmp_path)
    bindings = materialize_capability_bindings(stage, config)
    assert isinstance(bindings.engine_class_candidate_set, frozenset)


def test_provider_capability_bindings_is_frozen(tmp_path: Path) -> None:
    """`ProviderCapabilityBindings` is frozen (dataclass)."""
    stage = _ready_stage_three_providers()
    config = _runtime_config_with_ollama(tmp_path)
    bindings = materialize_capability_bindings(stage, config)
    # Frozen dataclasses raise FrozenInstanceError on assignment.
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        bindings.bindings = {}  # type: ignore[misc]


def test_provider_capability_binding_carries_adapter_for_aclose(tmp_path: Path) -> None:
    """`ProviderCapabilityBinding.adapter` is the same handle aclose() runs on.

    Pins the invariant that the L5 routing layer can reach `binding.adapter`
    and the L7+ shutdown can iterate stage.providers — both refer to the
    same adapter instance.
    """
    stage = _ready_stage_three_providers()
    config = _runtime_config_with_ollama(tmp_path)
    bindings = materialize_capability_bindings(stage, config)
    for name in ("anthropic", "openai", "ollama"):
        assert bindings.bindings[name].adapter is stage.providers[name]
