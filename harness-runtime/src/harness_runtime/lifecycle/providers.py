"""Provider SDK lifecycle — stage 3a CP_CLIENTS (U-RT-17/18/19/20).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-05) and Phase 2 Session 3
Track A plan v2.1 §L4. The runtime owns construction, lifetime, and close
of three async provider clients (`anthropic.AsyncAnthropic`,
`openai.AsyncOpenAI`, `ollama.AsyncClient`) wrapped behind the
`ProviderClient` Protocol (concretized at U-RT-17).

Spec §5 line 346: "Runtime wraps each in a thin adapter (per-provider module
under `harness_runtime/lifecycle/providers.py`) so all three satisfy
`ProviderClient.aclose()` uniformly. Adapters are runtime-defined; the
Protocol is the canonical contract."

Per-unit landing posture:
- **U-RT-17** (this commit): `AnthropicAdapter` + `construct_anthropic_adapter`
  + the typed fail-mode taxonomy (`ProviderSecretMissingError`,
  `ProviderAuthError`, `ProviderTransientError`,
  `ProviderDegradedWarning`).
- **U-RT-18**: `OpenAIAdapter` + `construct_openai_adapter`.
- **U-RT-19**: `OllamaAdapter` + `construct_ollama_adapter` (with the
  `RT-FAIL-PROVIDER-DEGRADED` branch when `ollama_optional=True`) +
  `materialize_provider_clients_stage` aggregating the three.
- **U-RT-20**: capability-aware binding (engine-class → providers lookup)
  consuming `harness_cp.engine_class_candidate.ENGINE_CLASS_CANDIDATES`.

Ping-mechanism injection. Per the spec §5 line 373 "Deferred to
implementation discretion" — async ping mechanism is a callable injected on
the adapter rather than a hard-wired SDK method call. This (a) keeps unit
tests free of live network calls (matches the workspace convention of
pyright-strict-clean fakes; see `test_lifecycle_mcp_host.py`) and (b)
isolates the ping surface from per-SDK version drift. Operator-driven
integration tests bind real SDK methods (`client.models.list()` for
Anthropic/OpenAI, `client.list()` for Ollama).

Idempotent close. Per C-RT-05 §5 line 343 (`aclose()` docstring "Idempotent")
+ C-RT-10 reverse-shutdown contract: every adapter tracks a `_closed` flag
and short-circuits subsequent `aclose()` calls. The underlying SDK's
`close()` is invoked exactly once.

Failure-mode taxonomy (spec §5 lines 367-371):

| Adapter exception                | RT-FAIL-* spec class                |
|----------------------------------|-------------------------------------|
| `ProviderSecretMissingError`     | `RT-FAIL-SECRET-MISSING` (permanent)|
| `ProviderTransientError`         | `RT-FAIL-TRANSIENT`     (transient) |
| `ProviderAuthError`              | `RT-FAIL-PROVIDER-AUTH` (permanent) |
| `ProviderDegradedWarning`        | `RT-FAIL-PROVIDER-DEGRADED` (deg.)  |

Bounded-retry policy (max 3 per stage policy per spec line 369) is wired by
the stage-3a materialize function (U-RT-19) which sits above the adapter
construction calls — adapters themselves raise once and let the materialize
loop decide retry vs. escalation.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from anthropic import AsyncAnthropic
from harness_cp.engine_class import EngineClass
from harness_cp.engine_class_candidate import ENGINE_CLASS_CANDIDATES
from ollama import AsyncClient as AsyncOllamaClient
from openai import AsyncOpenAI

from harness_runtime.config.provider_secrets import (
    ProviderSecretResolver,
    SecretResolutionError,
)
from harness_runtime.types import ProviderClient, RuntimeConfig

__all__ = [
    "ANTHROPIC_KEYRING_NAME",
    "DEFAULT_STAGE_3A_MAX_ATTEMPTS",
    "OPENAI_KEYRING_NAME",
    "AnthropicAdapter",
    "EmptyProviderCoverageError",
    "OllamaAdapter",
    "OpenAIAdapter",
    "ProviderAuthError",
    "ProviderCapabilityBinding",
    "ProviderCapabilityBindings",
    "ProviderClientsStage",
    "ProviderDegradedWarning",
    "ProviderNoneConfiguredError",
    "ProviderSecretMissingError",
    "ProviderTransientError",
    "construct_anthropic_adapter",
    "construct_ollama_adapter",
    "construct_openai_adapter",
    "materialize_capability_bindings",
    "materialize_provider_clients_stage",
]

DEFAULT_STAGE_3A_MAX_ATTEMPTS: Final[int] = 3
"""Bounded-retry attempt count per spec §5 line 369 ('max 3 per stage policy').

No backoff at U-RT-19 — the retry loop here just bounds the count. Real
backoff + circuit-breaker policy lives at U-RT-24 (retry/breaker registry).
Each transient failure is re-attempted up to (max_attempts - 1) times; the
final attempt's exception propagates per the spec's "persistent → escalation"
disposition.
"""


# ---------------------------------------------------------------------------
# Keyring entry names (per spec §5 lines 352-353; bootstrap-only lookup).
# ---------------------------------------------------------------------------
ANTHROPIC_KEYRING_NAME: Final[str] = "anthropic_key"
"""Keyring entry name for the Anthropic API key per spec §5 line 352
(`AsyncAnthropic(api_key=keyring_resolve('anthropic_key'), ...)`)."""

OPENAI_KEYRING_NAME: Final[str] = "openai_key"
"""Keyring entry name for the OpenAI API key per spec §5 line 353
(`AsyncOpenAI(api_key=keyring_resolve('openai_key'), ...)`)."""


# ---------------------------------------------------------------------------
# Typed exceptions — one per spec §5 fail-mode row.
# ---------------------------------------------------------------------------
class ProviderSecretMissingError(Exception):
    """Stage-3a `RT-FAIL-SECRET-MISSING` (permanent; spec §5 line 368).

    Raised when a provider-required keyring entry is absent. Carries the
    provider identity per "Construction errors surface as stage 3a failure
    with provider identity attached" (spec §5 line 360).
    """

    def __init__(self, provider: str, keyring_name: str) -> None:
        super().__init__(f"provider={provider!r}: keyring entry {keyring_name!r} not found")
        self.provider = provider
        self.keyring_name = keyring_name


class ProviderTransientError(Exception):
    """Stage-3a `RT-FAIL-TRANSIENT` (transient; spec §5 line 369).

    Raised when the adapter's async ping fails with a network-ish error.
    The stage-3a materialize loop (U-RT-19) bounds retry at 3 attempts;
    persistent transient → escalate to `ProviderAuthError` or
    `ProviderTransientError` re-raised as permanent.
    """

    def __init__(self, provider: str, cause: BaseException) -> None:
        super().__init__(f"provider={provider!r}: transient ping failure: {cause}")
        self.provider = provider
        self.cause = cause


class ProviderAuthError(Exception):
    """Stage-3a `RT-FAIL-PROVIDER-AUTH` (permanent; spec §5 line 370).

    Raised when the adapter's async ping fails with an auth-class error
    (401 / 403). No retry; surface typed and naming the provider.
    """

    def __init__(self, provider: str, cause: BaseException) -> None:
        super().__init__(f"provider={provider!r}: auth failure: {cause}")
        self.provider = provider
        self.cause = cause


class ProviderNoneConfiguredError(Exception):
    """Stage-3a `RT-FAIL-PROVIDER-NONE-CONFIGURED` (permanent).

    Raised at the end of `materialize_provider_clients_stage` when every
    provider degraded via `*_optional=True` + construction failure, leaving
    `ctx.providers` empty. The stage-5 LLM dispatcher binding requires at
    least one provider per `llm_dispatch.py:1025`; surfacing this at stage 3a
    gives operators a clear "no provider configured" message instead of an
    opaque downstream failure.

    Added per `.harness/class_1_fork_provider_construction_allowlist_semantic.md`
    operator-ratified 2026-05-28 (E-prod-3).
    """

    def __init__(self) -> None:
        super().__init__(
            "stage 3a CP_CLIENTS: all providers failed to construct AND were "
            "marked optional; no provider remains. Configure at least one "
            "provider keyring entry, OR mark fewer providers optional."
        )


class ProviderDegradedWarning(Warning):
    """Stage-3a `RT-FAIL-PROVIDER-DEGRADED` (degraded; spec §5 line 371).

    Surfaced (not raised) when Ollama is unreachable AND
    `RuntimeConfig.ollama_optional == True`. The materialize loop logs this
    and continues with a 2-provider context per the spec's "stage continues
    with 2-provider context" disposition. Wired at U-RT-19; declared at
    U-RT-17 to keep the fail-mode taxonomy in one place.
    """

    def __init__(self, provider: str, cause: BaseException) -> None:
        super().__init__(f"provider={provider!r}: degraded (unreachable): {cause}")
        self.provider = provider
        self.cause = cause


# ---------------------------------------------------------------------------
# AnthropicAdapter — U-RT-17.
# ---------------------------------------------------------------------------
# Type aliases for the ping-callable surface. The async ping returns `None`
# on success; on failure it raises an exception that the adapter classifies
# as transient vs. auth. Keeping the callable generic across providers lets
# tests inject deterministic fakes without monkeypatching SDK internals.
AsyncPing = Callable[[], Awaitable[None]]


def _default_anthropic_ping(client: AsyncAnthropic) -> AsyncPing:
    """Build the default ping callable for an `AsyncAnthropic` client.

    Uses `client.models.list()` per spec §5 line 373 suggestion ("low-cost
    `count_tokens` or model-list call per provider"). The default ping is
    only invoked when the operator does not inject a custom one — tests
    always inject; production code paths use this default.
    """

    async def ping() -> None:
        await client.models.list()

    return ping


@dataclass
class AnthropicAdapter:
    """Stage-3a Anthropic adapter — U-RT-17.

    Wraps an `anthropic.AsyncAnthropic` client behind the `ProviderClient`
    Protocol. Holds the client + a ping callable + a `_closed` flag for
    idempotent `aclose()`.

    Not frozen (vs. the L3 dataclass pattern) because `_closed` is mutated
    on shutdown. The instance handle is immutable post-construction except
    for the close-flag transition.
    """

    client: AsyncAnthropic
    ping: AsyncPing
    _closed: bool = field(default=False)

    async def aclose(self) -> None:
        """Idempotent close. Calls `client.close()` exactly once."""
        if self._closed:
            return
        self._closed = True
        await self.client.close()


def _classify_anthropic_ping_failure(exc: BaseException) -> Exception:
    """Map a ping-call exception to the typed fail class.

    Anthropic SDK raises `anthropic.AuthenticationError` (401) and
    `anthropic.PermissionDeniedError` (403) for auth failures, both
    subclasses of `anthropic.APIStatusError`. Network errors raise
    `anthropic.APIConnectionError`. Anything else is treated as transient
    (the stage-3a retry loop will re-attempt; persistent → permanent).

    Import is local so the import cost is paid once per construction call,
    not at module load time, and so the classifier is testable with
    fakes that mimic the auth-class duck shape.
    """
    # Local import: avoids paying the cost at module load (consistent
    # with the workspace convention of lazy-importing SDK error classes).
    from anthropic import AuthenticationError, PermissionDeniedError

    if isinstance(exc, AuthenticationError | PermissionDeniedError):
        return ProviderAuthError("anthropic", exc)
    return ProviderTransientError("anthropic", exc)


async def construct_anthropic_adapter(
    config: RuntimeConfig,
    resolver: ProviderSecretResolver,
    *,
    ping_override: AsyncPing | None = None,
    client_factory: Callable[[str], AsyncAnthropic] | None = None,
) -> AnthropicAdapter:
    """Construct + ping-verify an `AnthropicAdapter` for stage 3a.

    Steps:
    1. Resolve the Anthropic API key via the provider-secret resolver.
    2. Construct `AsyncAnthropic(api_key=...)`.
    3. Invoke the (injected or default) async ping. Auth-class exception →
       `ProviderAuthError`; anything else → `ProviderTransientError`.
    4. Return the adapter; caller (materialize loop, U-RT-19) decides retry
       vs. escalate on transient failure.

    Parameters
    ----------
    config :
        Frozen `RuntimeConfig`. Not consumed here (Anthropic SDK construction
        needs only the API key) but kept on the signature for symmetry with
        `construct_openai_adapter` / `construct_ollama_adapter` (U-RT-18/19,
        which read `ollama_host` / `ollama_optional`).
    resolver :
        `ProviderSecretResolver` built at stage 0 PREAMBLE (U-RT-06/R-421).
        Provides the bootstrap-only `resolve_bootstrap_value` path.
    ping_override :
        Test-injection point. When `None`, the default ping calls
        `client.models.list()`. Tests pass a deterministic awaitable.
    client_factory :
        Test-injection point for the SDK constructor. When `None`, calls
        `AsyncAnthropic(api_key=key)`. Tests pass a fake that records the
        key + returns a stub satisfying the close-method shape.

    Returns
    -------
    AnthropicAdapter
        Ready adapter; `_closed=False`; ping has succeeded.

    Raises
    ------
    ProviderSecretMissingError
        Keyring lookup for `anthropic_key` returned `None`.
    ProviderAuthError
        Ping raised an Anthropic auth-class error.
    ProviderTransientError
        Ping raised any other exception (network, timeout, unexpected).
    """
    # `config` is accepted for cross-adapter signature symmetry (U-RT-18/19
    # read fields from it). Anthropic itself needs only the API key.
    _ = config
    try:
        api_key = resolver.resolve_bootstrap_value(ANTHROPIC_KEYRING_NAME)
    except SecretResolutionError as exc:
        raise ProviderSecretMissingError("anthropic", ANTHROPIC_KEYRING_NAME) from exc

    if client_factory is None:
        client = AsyncAnthropic(api_key=api_key)
    else:
        client = client_factory(api_key)

    ping = ping_override if ping_override is not None else _default_anthropic_ping(client)
    try:
        await ping()
    except (ProviderAuthError, ProviderTransientError):
        # Already typed — propagate. (Allows test-injected ping callables
        # to raise the typed errors directly without re-classification.)
        raise
    except BaseException as exc:
        raise _classify_anthropic_ping_failure(exc) from exc

    return AnthropicAdapter(client=client, ping=ping)


# Protocol conformance assertion — surfaces at module load time. If the
# AnthropicAdapter ever drifts from the `ProviderClient` shape, mypy /
# pyright will flag the assignment line below.
_ANTHROPIC_PROTOCOL_CHECK: type[ProviderClient] = AnthropicAdapter
del _ANTHROPIC_PROTOCOL_CHECK


# ---------------------------------------------------------------------------
# OpenAIAdapter — U-RT-18.
# Same shape as AnthropicAdapter. Per-SDK error-class import is local to the
# classifier so the import cost is paid once per construction call. OpenAI's
# AuthenticationError + PermissionDeniedError share the same APIStatusError
# parent as Anthropic's; the classification logic is symmetric.
# ---------------------------------------------------------------------------


def _default_openai_ping(client: AsyncOpenAI) -> AsyncPing:
    """Build the default ping callable for an `AsyncOpenAI` client.

    Uses `client.models.list()` per spec §5 line 373 suggestion. Symmetric
    with `_default_anthropic_ping`.
    """

    async def ping() -> None:
        await client.models.list()

    return ping


@dataclass
class OpenAIAdapter:
    """Stage-3a OpenAI adapter — U-RT-18.

    Wraps an `openai.AsyncOpenAI` client behind the `ProviderClient` Protocol.
    Same idempotent-close discipline as `AnthropicAdapter`.
    """

    client: AsyncOpenAI
    ping: AsyncPing
    _closed: bool = field(default=False)

    async def aclose(self) -> None:
        """Idempotent close. Calls `client.close()` exactly once."""
        if self._closed:
            return
        self._closed = True
        await self.client.close()


def _classify_openai_ping_failure(exc: BaseException) -> Exception:
    """Map an OpenAI ping exception to the typed fail class.

    OpenAI SDK raises `openai.AuthenticationError` (401) and
    `openai.PermissionDeniedError` (403) for auth failures. Network errors
    raise `openai.APIConnectionError`. Symmetric with Anthropic classifier.
    """
    from openai import AuthenticationError, PermissionDeniedError

    if isinstance(exc, AuthenticationError | PermissionDeniedError):
        return ProviderAuthError("openai", exc)
    return ProviderTransientError("openai", exc)


async def construct_openai_adapter(
    config: RuntimeConfig,
    resolver: ProviderSecretResolver,
    *,
    ping_override: AsyncPing | None = None,
    client_factory: Callable[[str], AsyncOpenAI] | None = None,
) -> OpenAIAdapter:
    """Construct + ping-verify an `OpenAIAdapter` for stage 3a.

    Steps + parameters + fail-mode contract mirror `construct_anthropic_adapter`
    — see that function's docstring for the canonical narrative. The only
    per-provider variance is the keyring name (`openai_key`), the SDK class
    (`AsyncOpenAI`), and the error-class set the classifier checks.

    Raises
    ------
    ProviderSecretMissingError
        Keyring lookup for `openai_key` returned `None`.
    ProviderAuthError
        Ping raised an OpenAI auth-class error.
    ProviderTransientError
        Ping raised any other exception.
    """
    _ = config
    try:
        api_key = resolver.resolve_bootstrap_value(OPENAI_KEYRING_NAME)
    except SecretResolutionError as exc:
        raise ProviderSecretMissingError("openai", OPENAI_KEYRING_NAME) from exc

    if client_factory is None:
        client = AsyncOpenAI(api_key=api_key)
    else:
        client = client_factory(api_key)

    ping = ping_override if ping_override is not None else _default_openai_ping(client)
    try:
        await ping()
    except (ProviderAuthError, ProviderTransientError):
        raise
    except BaseException as exc:
        raise _classify_openai_ping_failure(exc) from exc

    return OpenAIAdapter(client=client, ping=ping)


_OPENAI_PROTOCOL_CHECK: type[ProviderClient] = OpenAIAdapter
del _OPENAI_PROTOCOL_CHECK


# ---------------------------------------------------------------------------
# OllamaAdapter — U-RT-19.
# Ollama is local-tier and credential-less per spec §5 line 354. No keyring
# resolution; construction uses `host=config.ollama_host or default`. The
# fail-mode set differs from Anthropic / OpenAI: there is no auth class
# (local daemon), so every ping failure is `ProviderTransientError` — which
# the materialize stage further reclassifies to `ProviderDegradedWarning`
# when `RuntimeConfig.ollama_optional == True` per spec §5 line 371.
# ---------------------------------------------------------------------------


def _default_ollama_ping(client: AsyncOllamaClient) -> AsyncPing:
    """Default ping for an `ollama.AsyncClient` — `client.list()` per spec §5 line 373."""

    async def ping() -> None:
        await client.list()

    return ping


@dataclass
class OllamaAdapter:
    """Stage-3a Ollama adapter — U-RT-19.

    Wraps `ollama.AsyncClient`. Same idempotent-close discipline as
    `AnthropicAdapter` / `OpenAIAdapter`. Ollama's `close()` is exposed and
    awaitable per the SDK contract.
    """

    client: AsyncOllamaClient
    ping: AsyncPing
    _closed: bool = field(default=False)

    async def aclose(self) -> None:
        """Idempotent close. Calls `client.close()` exactly once."""
        if self._closed:
            return
        self._closed = True
        await self.client.close()


def _classify_ollama_ping_failure(exc: BaseException) -> Exception:
    """Map an Ollama ping exception to `ProviderTransientError`.

    Ollama leaks the underlying `builtins.ConnectionError` (daemon
    unreachable) and exposes its own `ollama.RequestError` /
    `ollama.ResponseError`. None of these are auth-class — Ollama is local
    and credential-less. Everything is treated as transient at this layer;
    the materialize stage decides whether to retry, escalate, or degrade.
    """
    return ProviderTransientError("ollama", exc)


async def construct_ollama_adapter(
    config: RuntimeConfig,
    resolver: ProviderSecretResolver | None = None,
    *,
    ping_override: AsyncPing | None = None,
    client_factory: Callable[[str | None], AsyncOllamaClient] | None = None,
) -> OllamaAdapter:
    """Construct + ping-verify an `OllamaAdapter` for stage 3a.

    Unlike Anthropic / OpenAI, Ollama is credential-less; the `resolver`
    parameter is accepted for cross-adapter signature symmetry but is unused
    (defaults to None). The host URL comes from `config.ollama_host` (per
    spec §5 line 354); `None` lets `ollama.AsyncClient` apply its built-in
    default (`http://localhost:11434`).

    Degraded-mode reclassification (`ProviderDegradedWarning`) is the
    materialize stage's job, not this constructor's — this function always
    raises `ProviderTransientError` on ping failure. The materialize loop
    decides whether the operator opted into degraded mode.

    Parameters
    ----------
    config :
        Frozen `RuntimeConfig`. Reads `ollama_host`; `ollama_optional` is
        consumed at the materialize layer, not here.
    resolver :
        Accepted for symmetry; unused.
    ping_override :
        Test-injection point. When `None`, uses `client.list()`.
    client_factory :
        Test-injection point for the SDK constructor. Signature is
        `Callable[[str | None], AsyncOllamaClient]` to mirror the
        `host=config.ollama_host` argument shape.

    Raises
    ------
    ProviderTransientError
        Ping raised any exception (network, daemon-down, response error).
    """
    _ = resolver  # accepted for signature symmetry; Ollama is credential-less.

    if client_factory is None:
        client = AsyncOllamaClient(host=config.ollama_host)
    else:
        client = client_factory(config.ollama_host)

    ping = ping_override if ping_override is not None else _default_ollama_ping(client)
    try:
        await ping()
    except (ProviderAuthError, ProviderTransientError):
        raise
    except BaseException as exc:
        raise _classify_ollama_ping_failure(exc) from exc

    return OllamaAdapter(client=client, ping=ping)


_OLLAMA_PROTOCOL_CHECK: type[ProviderClient] = OllamaAdapter
del _OLLAMA_PROTOCOL_CHECK


# ---------------------------------------------------------------------------
# materialize_provider_clients_stage — U-RT-19.
# Aggregates the three adapters into the `dict[str, ProviderClient]` shape
# `HarnessContext.providers` expects (C-RT-04 line 283). Per the advisor:
# three explicit per-provider construction calls (not a loop) so the Ollama
# degraded branch reads cleanly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderClientsStage:
    """Result of `materialize_provider_clients_stage`.

    `providers` maps provider key → ready adapter. Keys are `"anthropic"`,
    `"openai"`, and `"ollama"` per spec C-RT-04 line 283. When
    `ollama_optional=True` and Ollama is unreachable, the `"ollama"` key is
    ABSENT from the dict and a `ProviderDegradedWarning` was surfaced via
    `warnings.warn` per spec §5 line 371 (stage continues with 2-provider
    context). When `ollama_optional=False`, an unreachable Ollama is a hard
    stage-3a failure.
    """

    providers: Mapping[str, ProviderClient]


async def _attempt_with_bounded_retry(
    name: str,
    construct: Callable[[], Awaitable[ProviderClient]],
    max_attempts: int = DEFAULT_STAGE_3A_MAX_ATTEMPTS,
) -> ProviderClient:
    """Run `construct()` with bounded-retry on `ProviderTransientError`.

    Per spec §5 line 369: transient failures retry up to `max_attempts`
    times; the final attempt's exception propagates ("persistent →
    escalation"). `ProviderAuthError` and `ProviderSecretMissingError` are
    permanent — no retry; raised on the first attempt.

    No backoff sleep at U-RT-19 — real backoff policy is wired at U-RT-24
    (retry/breaker registry). The `name` argument is captured into the
    propagated exception via the adapter's own `provider=` field, not here.
    """
    last_transient: ProviderTransientError | None = None
    for attempt in range(max_attempts):
        _ = attempt  # attempt index informational; retained for future logging.
        try:
            return await construct()
        except ProviderTransientError as exc:
            last_transient = exc
            continue
    # All `max_attempts` attempts raised transient — escalate.
    assert last_transient is not None, f"unreachable: {name} retry loop exited without exc"
    raise last_transient


async def materialize_provider_clients_stage(
    config: RuntimeConfig,
    resolver: ProviderSecretResolver,
    *,
    anthropic_construct: Callable[[], Awaitable[ProviderClient]] | None = None,
    openai_construct: Callable[[], Awaitable[ProviderClient]] | None = None,
    ollama_construct: Callable[[], Awaitable[ProviderClient]] | None = None,
    max_attempts: int = DEFAULT_STAGE_3A_MAX_ATTEMPTS,
) -> ProviderClientsStage:
    """Build the stage 3a CP_CLIENTS provider dict per C-RT-02 invariants.

    Steps:
    1. Construct anthropic adapter (bounded retry on transient).
    2. Construct openai adapter (bounded retry on transient).
    3. Construct ollama adapter:
       - If `config.ollama_optional == False` and transient persists →
         raise (hard stage-3a failure per multi-LLM commitment).
       - If `config.ollama_optional == True` and transient persists →
         surface `ProviderDegradedWarning` via `warnings.warn`; omit
         `"ollama"` from the providers dict (2-provider context).

    The per-provider `*_construct` overrides are test-injection points so the
    materialize-stage loop can be exercised without re-exercising the
    per-adapter SDK construction path (covered by U-RT-17 / U-RT-18 / U-RT-19
    construct-function tests).

    Parameters
    ----------
    config :
        Frozen `RuntimeConfig`. Drives `ollama_host` + `ollama_optional`.
    resolver :
        Stage-0 `ProviderSecretResolver` (U-RT-06/R-421). Anthropic + OpenAI
        adapters consume it for the bootstrap-value path.
    anthropic_construct, openai_construct, ollama_construct :
        Test injection. When `None`, default to the per-provider construct
        functions (no overrides — production path).
    max_attempts :
        Bounded-retry attempt count. Default = `DEFAULT_STAGE_3A_MAX_ATTEMPTS`.

    Returns
    -------
    ProviderClientsStage
        Frozen handle carrying the `dict[str, ProviderClient]` mapping. The
        dict has 2 entries when Ollama was degraded; 3 otherwise.
    """
    # Bind default per-provider constructors. The Awaitable[ProviderClient]
    # return type is upcast from each adapter's concrete type.
    if anthropic_construct is None:

        async def anthropic_construct_default() -> ProviderClient:
            return await construct_anthropic_adapter(config, resolver)

        anthropic_construct = anthropic_construct_default

    if openai_construct is None:

        async def openai_construct_default() -> ProviderClient:
            return await construct_openai_adapter(config, resolver)

        openai_construct = openai_construct_default

    if ollama_construct is None:

        async def ollama_construct_default() -> ProviderClient:
            return await construct_ollama_adapter(config, resolver)

        ollama_construct = ollama_construct_default

    providers: dict[str, ProviderClient] = {}

    # Step 1: Anthropic. ProviderAuthError ALWAYS propagates (operator
    # misconfig). ProviderTransientError + ProviderSecretMissingError swallow
    # only if `anthropic_optional=True` per
    # `.harness/class_1_fork_provider_construction_allowlist_semantic.md`
    # (E-prod-3, 2026-05-28).
    try:
        providers["anthropic"] = await _attempt_with_bounded_retry(
            "anthropic", anthropic_construct, max_attempts=max_attempts
        )
    except (ProviderTransientError, ProviderSecretMissingError) as exc:
        if config.anthropic_optional:
            cause = exc.cause if isinstance(exc, ProviderTransientError) else exc
            warnings.warn(
                ProviderDegradedWarning("anthropic", cause),
                stacklevel=2,
            )
            # `"anthropic"` key intentionally absent from providers dict.
        else:
            raise

    # Step 2: OpenAI. Symmetric to anthropic.
    try:
        providers["openai"] = await _attempt_with_bounded_retry(
            "openai", openai_construct, max_attempts=max_attempts
        )
    except (ProviderTransientError, ProviderSecretMissingError) as exc:
        if config.openai_optional:
            cause = exc.cause if isinstance(exc, ProviderTransientError) else exc
            warnings.warn(
                ProviderDegradedWarning("openai", cause),
                stacklevel=2,
            )
        else:
            raise

    # Step 3: Ollama. Keyring-less (local-tier); only ProviderTransientError
    # is possible at construction (no SecretMissingError path). Matches the
    # pre-E-prod-3 ollama_optional behavior.
    try:
        providers["ollama"] = await _attempt_with_bounded_retry(
            "ollama", ollama_construct, max_attempts=max_attempts
        )
    except ProviderTransientError as transient_exc:
        if config.ollama_optional:
            # Surface degraded; continue with reduced-provider context.
            # Unwrap to `transient_exc.cause` (e.g., ConnectionError) so the
            # warning identifies the underlying network failure, not the
            # ProviderTransientError wrapper that's an internal carry.
            warnings.warn(
                ProviderDegradedWarning("ollama", transient_exc.cause),
                stacklevel=2,
            )
            # `"ollama"` key intentionally absent from providers dict.
        else:
            # Hard stage-3a failure per multi-LLM commitment.
            raise

    # Post-loop invariant: at least one provider successfully constructed.
    # If every provider was optional + degraded, surface the typed
    # `RT-FAIL-PROVIDER-NONE-CONFIGURED` here at the construction site rather
    # than letting it bubble to stage 5 as an opaque `LLMDispatchBindError`.
    # (Runtime spec v1.47 §2.1: this composer is only invoked for an
    # inference-bearing workflow — stage 3a skips it entirely for a tool-only
    # / non-inference workflow, which needs no provider.)
    if len(providers) == 0:
        raise ProviderNoneConfiguredError()

    return ProviderClientsStage(providers=providers)


# ---------------------------------------------------------------------------
# Capability-aware binding — U-RT-20.
# Per Phase 2 Session 3 Track A plan v2.1 §L4 U-RT-20:
#   "bind 3 async provider clients behind CP provider_capabilities;
#    populate per-engine-class candidates per engine_class_candidate."
#   AC: "each EngineClass resolves to at least one capable provider;
#        capability assertions exhaustively typed."
#
# AC interpretation (per advisor consultation). `EngineClass` (durability
# substrate: replay / checkpoint / reconciler-loop / etc.) and
# `ProviderCapabilities` (LLM capability: TOOLS / CACHING / THINKING /
# BATCH) are orthogonal in the landed CP surface — no per-EngineClass
# capability-requirement function exists. "Each EngineClass resolves to at
# least one capable provider" therefore reduces structurally to:
#   ENGINE_CLASS_CANDIDATES[deployment_surface].candidate_set != ∅
#   ∧ bindings != ∅
# If a deeper coverage check is intended, that surface is a Class 2/3 CP
# spec amendment, not a silent extension at U-RT-20 (X-AL-3).
#
# CP-AL-1 watch. U-RT-20 is *capability surface binding* — not EngineSelector,
# not TopologyDispatcher, not RetryBreakerRegistry. Those land at L5
# (stage 3b CP_ROUTING). Per-request reflection of capabilities via
# `harness_cp.provider_capabilities.reflect_provider_capabilities(provider,
# model)` happens at request time (model is a per-request concept); stage-3a
# binding holds adapter + provider_name only.
# ---------------------------------------------------------------------------


class EmptyProviderCoverageError(Exception):
    """Stage-3a `RT-FAIL-BOOTSTRAP` — no capable provider for any EngineClass.

    Raised when either (a) the deployment surface's engine-class candidate
    set is empty (would be a CP-side defect — every surface should ship a
    non-empty set), or (b) the bindings map is empty (no providers landed).
    The typed exception surfaces at the stage-3a fail-mode per spec §5.
    """

    def __init__(self, deployment_surface: str, reason: str) -> None:
        super().__init__(
            f"empty provider coverage at deployment_surface={deployment_surface!r}: {reason}"
        )
        self.deployment_surface = deployment_surface
        self.reason = reason


@dataclass(frozen=True)
class ProviderCapabilityBinding:
    """One provider's stage-3a capability binding handle.

    Holds the adapter (for `aclose()` on shutdown) and the provider identity
    (consumed by `harness_cp.provider_capabilities.reflect_provider_capabilities`
    at request time, when the model is known). Model is intentionally NOT
    bound here — model selection is a per-request concept owned by CP
    routing (L5).
    """

    adapter: ProviderClient
    provider_name: str


@dataclass(frozen=True)
class ProviderCapabilityBindings:
    """Aggregate of per-provider capability bindings + coverage attestation.

    `bindings` is keyed by provider name (`"anthropic"`, `"openai"`,
    `"ollama"`). `engine_class_candidate_set` is the frozen candidate set
    pulled from `ENGINE_CLASS_CANDIDATES` for `config.deployment_surface` —
    pinned here so downstream consumers (L5 routing) get a typed handle.
    """

    bindings: Mapping[str, ProviderCapabilityBinding]
    engine_class_candidate_set: frozenset[EngineClass]
    """The deployment-surface's admissible `EngineClass` values per
    `harness_cp.engine_class_candidate.ENGINE_CLASS_CANDIDATES`. L5 routing
    consumers pull this directly — no cast needed."""


def materialize_capability_bindings(
    stage: ProviderClientsStage,
    config: RuntimeConfig,
) -> ProviderCapabilityBindings:
    """Bind the stage-3a providers to CP's capability surface — U-RT-20.

    Steps:
    1. Pull the deployment-surface's engine-class candidate set from
       `ENGINE_CLASS_CANDIDATES`. Validate non-empty (CP-side invariant
       re-asserted at the runtime boundary).
    2. For each provider in the stage, build a `ProviderCapabilityBinding`
       (adapter + provider_name).
    3. Validate at least one binding exists. With Ollama-degraded the
       count drops to 2, which still satisfies the coverage assertion.
    4. Return the frozen `ProviderCapabilityBindings` handle.

    Parameters
    ----------
    stage :
        `ProviderClientsStage` returned by `materialize_provider_clients_stage`.
    config :
        Frozen `RuntimeConfig`. Drives the deployment-surface candidate-set
        lookup.

    Returns
    -------
    ProviderCapabilityBindings
        Frozen aggregate with `bindings: Mapping[str, ProviderCapabilityBinding]`
        + the deployment-surface's `engine_class_candidate_set`.

    Raises
    ------
    EmptyProviderCoverageError
        Either the engine-class candidate set is empty (CP-side defect) or
        no providers landed (impossible from materialize_provider_clients_stage
        which always raises if anthropic/openai fail; included for defense-
        in-depth).
    """
    # Step 1: Pull candidate set for this deployment surface.
    candidate = next(
        (c for c in ENGINE_CLASS_CANDIDATES if c.deployment_surface == config.deployment_surface),
        None,
    )
    if candidate is None or len(candidate.candidate_set) == 0:
        raise EmptyProviderCoverageError(
            str(config.deployment_surface),
            "deployment surface has no admissible EngineClass values "
            "(CP-side ENGINE_CLASS_CANDIDATES defect)",
        )

    # Step 2: Build per-provider bindings.
    bindings: dict[str, ProviderCapabilityBinding] = {}
    for provider_name, adapter in stage.providers.items():
        bindings[provider_name] = ProviderCapabilityBinding(
            adapter=adapter,
            provider_name=provider_name,
        )

    # Step 3: Validate ≥1 binding (coverage assertion).
    if not bindings:
        raise EmptyProviderCoverageError(
            str(config.deployment_surface),
            "no providers landed at stage 3a (empty providers dict)",
        )

    # Step 4: Freeze + return.
    return ProviderCapabilityBindings(
        bindings=bindings,
        engine_class_candidate_set=frozenset(candidate.candidate_set),
    )
