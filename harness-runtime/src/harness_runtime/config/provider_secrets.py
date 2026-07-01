"""U-RT-06 — keyring-backed provider-secret resolver driver.

Per `Spec_Harness_Runtime_v1.md` v1.1 §3 (C-RT-03 `provider_secrets` field)
and Phase 2 Session 3 plan v2.1 §2 L1, this module:

- Provides the typed runtime-side secret resolver bound to `python-keyring`.
- Enforces C-AS-06 §6.2 allowlist intersection when a `ToolContract` is
  supplied at fetch time.
- Maps keyring misses to typed `SecretResolutionError` carrying the C-AS-07
  `SecretFailClass` for downstream C5/C9 routing.
- Selects whether vendor env-var fallback is allowed from
  `ProviderSecretsConfig.backend`.

Implementation-discretion choices (per AS spec C-AS-05 §5.4):
- Keyring library: `python-keyring` (committed at `Target_Stack_Commitment_v1`
  §5.1 + ADR-F5 v1.1).
- Service-name discipline: configured at `ProviderSecretsConfig.keyring_service`;
  default `"harness"`.
- `secret_unknown` (keyring miss) → permanent C5 / no-retry C9 per C-AS-07
  §7.1 row 1 (`SECRET_UNKNOWN` → `PERMANENT_FAIL` / `NO_RETRY_ROUTE_TO_HITL`).

Audit-event composition (`SecretFetchEvent`) lives at the FETCH CALL SITE
(per U-AS-26 separation), not in this driver. The driver returns `SecretRef`
on success; the caller composes the audit event with its `actor` / `timestamp`
/ `thread_id` / `step_id` context and routes through the IS state-ledger
writer once that lands (L2+ wiring at U-RT-12 / U-RT-32).

NOT in scope for U-RT-06 (deferred):
- Tier-aware resolution mechanism (`SecretResolutionMechanism` per
  C-AS-05 §5.2). The keyring path is the LOCAL_DEVELOPMENT /
  SELF_HOSTED_SERVER tier mechanism; MANAGED_CLOUD tiers use the in-sandbox
  HTTP bootstrap-token mechanism per AS spec §5.2 row 4, wired at L4
  (U-RT-17..U-RT-20) when provider clients exist.
- Per-backend breaker placement (C-AS-07 §7.3). Wired at U-RT-24
  (retry/breaker registry).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Final, Protocol, cast, runtime_checkable

import keyring
from harness_as.sandbox_tier import SandboxTier
from harness_as.secret_allowlist import AllowlistDecision, check_secret_allowlist
from harness_as.secret_fail_class import SecretFailClass
from harness_as.secret_fetch import SecretRef, SecretScope
from harness_as.tool_contract import ToolContract
from keyring.errors import KeyringError

from harness_runtime.types import ProviderSecretBackend, ProviderSecretsConfig

__all__ = [
    "GcpSecretAccessResult",
    "GcpSecretAccessor",
    "GcpSecretManagerResolver",
    "KeyringSecretResolver",
    "ProviderSecretResolver",
    "SecretAllowlistDeniedError",
    "SecretBackendUnavailableError",
    "SecretResolutionAuditResult",
    "SecretResolutionError",
    "make_keyring_resolver",
    "make_provider_secret_resolver",
]

_KEYRING_TO_ENV_VAR: Final[dict[str, str]] = {
    "anthropic_key": "ANTHROPIC_API_KEY",
    "openai_key": "OPENAI_API_KEY",
}
"""Keyring-name → vendor-canonical env-var name mapping.

Per ADR-F5 v1.1 §(b)(i) headless-mode framing ("Headless modes use ...
environment-pre-seeded values") + binding-fix discretion record at
`.harness/binding_fix_keyring_resolver_env_var_fallback.md` §3 (a). Names
without a mapping fall through to keyring-only (closed at this arc; future
non-provider secrets can extend the map or remain keyring-only)."""


class SecretResolutionError(Exception):
    """Raised when keyring resolution fails. Carries the C-AS-07 fail class."""

    def __init__(self, fail_class: SecretFailClass, name: str) -> None:
        super().__init__(f"{fail_class.value}: {name}")
        self.fail_class = fail_class
        self.name = name


class SecretAllowlistDeniedError(Exception):
    """Raised when the allowlist intersection denies the request (C-AS-06 §6.2)."""

    def __init__(self, decision: AllowlistDecision, name: str, scope: SecretScope) -> None:
        super().__init__(f"{decision.value}: name={name!r} scope={scope.name!r}")
        self.decision = decision
        self.name = name
        self.scope = scope


class SecretBackendUnavailableError(Exception):
    """Raised by backend adapters when the secret backend cannot be reached."""


@dataclass(frozen=True)
class GcpSecretAccessResult:
    """GCP Secret Manager access result with payload plus version metadata."""

    value: str
    last_rotated_at: str


@dataclass(frozen=True)
class SecretResolutionAuditResult:
    """Metadata-bearing scoped secret resolution result for R-CXA-1 emission."""

    ref: SecretRef
    secret_last_rotated_at: str
    backend: str
    cache_tier_overhead_ms: int = 0
    policy_access_decision_reason: str = "permitted"


GcpSecretAccessor = Callable[[str], str | GcpSecretAccessResult]
"""Test-injectable accessor for one fully-qualified GCP Secret Manager resource."""


@runtime_checkable
class ProviderSecretResolver(Protocol):
    """Shared provider-secret resolver surface consumed by runtime bootstrap."""

    def resolve(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> SecretRef:
        """Resolve a secret reference, enforcing allowlist when a tool is supplied."""
        ...

    def resolve_bootstrap_value(self, name: str) -> str:
        """Resolve the literal provider-secret value for SDK construction."""
        ...

    def resolve_with_audit_metadata(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> SecretResolutionAuditResult:
        """Resolve a scoped secret and return backend rotation metadata."""
        ...


def _enforce_allowlist(
    *,
    operator_allowlist: frozenset[object],
    name: str,
    scope: SecretScope,
    tool: ToolContract | None,
) -> None:
    if tool is None:
        return
    decision = check_secret_allowlist(
        tool=tool,
        requested_name=name,
        requested_scope=scope,
        operator_policy_override=operator_allowlist,  # type: ignore[arg-type]
    )
    if decision is not AllowlistDecision.PERMITTED:
        raise SecretAllowlistDeniedError(decision, name, scope)


@dataclass(frozen=True)
class KeyringSecretResolver:
    """Runtime keyring-backed provider-secret resolver.

    Construct via `make_keyring_resolver(config)`. The resolver is frozen
    (dataclass `frozen=True`); reconfiguration requires building a new
    instance from an updated `ProviderSecretsConfig`.
    """

    keyring_service: str
    allow_env_fallback: bool
    operator_allowlist: frozenset[object]
    """Stored as `frozenset[object]` to dodge `SecretAllowlistEntry` hashability
    fragility (Pydantic v2 frozen models are hashable only with `frozen=True`
    AND no mutable fields); the set is consumed by `check_secret_allowlist`
    which casts via `set(...)` internally.

    NOTE: at L1 the operator-allowlist intersection is exercised through the
    AS-landed `check_secret_allowlist` function; this driver does not
    re-implement allowlist semantics."""

    def _lookup(self, name: str) -> str | None:
        """Keyring-first lookup with env-var fallback per ADR-F5 v1.1 §(b)(i).

        Returns the secret value when the keyring has it OR when the
        vendor-canonical env var (per `_KEYRING_TO_ENV_VAR`) is set. Returns
        `None` only when both sources are absent — at which point the caller
        raises `SecretResolutionError(SECRET_UNKNOWN, ...)`.

        Names without an env-var mapping fall through to keyring-only.
        """
        try:
            value = keyring.get_password(self.keyring_service, name)
        except KeyringError as exc:
            env_var = _KEYRING_TO_ENV_VAR.get(name)
            if self.allow_env_fallback and env_var is not None:
                value = os.environ.get(env_var)
                if value is not None:
                    return value
            raise SecretResolutionError(SecretFailClass.SECRET_UNAVAILABLE, name) from exc
        if value is not None:
            return value
        env_var = _KEYRING_TO_ENV_VAR.get(name)
        if self.allow_env_fallback and env_var is not None:
            return os.environ.get(env_var)
        return None

    def resolve(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> SecretRef:
        """Resolve a secret reference; raises on miss or allowlist denial.

        Parameters
        ----------
        name :
            The secret identifier (per `SecretRef.name`).
        scope :
            Credential-dimension session key.
        tier :
            Sandbox tier of the call site (lifetime-bound anchor).
        tool :
            Tool contract for allowlist intersection (C-AS-06 §6.2). When
            `None`, allowlist intersection is skipped — used by runtime
            self-tests + bootstrap-only fetches before tools are registered.

        Returns
        -------
        SecretRef
            Opaque handle bound to `(name, scope, tier)`.

        Raises
        ------
        SecretAllowlistDeniedError
            Tool was provided AND intersection denied the request.
        SecretResolutionError
            Keyring returned `None` for `(keyring_service, name)`.
        """
        _enforce_allowlist(
            operator_allowlist=self.operator_allowlist,
            name=name,
            scope=scope,
            tool=tool,
        )

        value = self._lookup(name)
        if value is None:
            raise SecretResolutionError(SecretFailClass.SECRET_UNKNOWN, name)

        return SecretRef(name=name, scope=scope, tier=tier)

    def resolve_with_audit_metadata(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> SecretResolutionAuditResult:
        """Keyring has no truthful rotation/version metadata; fail closed.

        R-CXA-1 requires `secret_last_rotated_at` to be a real backend version
        attribute. Local keyring/env fallback can prove existence of a value
        but does not expose such metadata through this binding, so using a
        sentinel would hollow the AS→IS fingerprint.
        """
        _enforce_allowlist(
            operator_allowlist=self.operator_allowlist,
            name=name,
            scope=scope,
            tool=tool,
        )
        value = self._lookup(name)
        if value is None:
            raise SecretResolutionError(SecretFailClass.SECRET_UNKNOWN, name)
        raise SecretResolutionError(SecretFailClass.SECRET_UNAVAILABLE, name)

    def resolve_bootstrap_value(self, name: str) -> str:
        """Resolve a secret to its literal value for stage-3a SDK construction.

        Bootstrap-only path used at U-RT-17/18 to pass `api_key=...` into
        `AsyncAnthropic(...)` / `AsyncOpenAI(...)` per spec §5 line 352-353.
        Distinct from `resolve()` (which returns an opaque `SecretRef` handle
        for tool call sites) because the provider SDKs require the literal
        string at construction time.

        Bypasses the AS allowlist intersection (`check_secret_allowlist`)
        because tool contracts are not registered yet at stage 3a CP_CLIENTS
        (it precedes stage 5 LOOP_INIT). This mirrors the `tool=None` branch
        in `.resolve()` and is documented at AS spec C-AS-05 §5.4 as the
        bootstrap-only secret resolution path.

        Parameters
        ----------
        name :
            The keyring entry name (e.g., `"anthropic_key"`, `"openai_key"`).

        Returns
        -------
        str
            The literal secret value from the OS keyring.

        Raises
        ------
        SecretResolutionError
            Keyring returned `None` for `(keyring_service, name)` —
            `SecretFailClass.SECRET_UNKNOWN` per C-AS-07 §7.1 row 1. Surfaces
            at the stage-3a fail-mode `RT-FAIL-SECRET-MISSING` per
            `Spec_Harness_Runtime_v1.md` §5 line 368.
        """
        value = self._lookup(name)
        if value is None:
            raise SecretResolutionError(SecretFailClass.SECRET_UNKNOWN, name)
        return value


def _decode_gcp_secret_payload(raw_data: object, resource_name: str) -> str:
    if isinstance(raw_data, bytes):
        try:
            return raw_data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretBackendUnavailableError(
                f"GCP Secret Manager returned non-UTF-8 payload for {resource_name}"
            ) from exc
    if isinstance(raw_data, str):
        return raw_data
    raise SecretBackendUnavailableError(
        f"GCP Secret Manager returned unsupported payload type for {resource_name}"
    )


def _format_gcp_timestamp(value: object, resource_name: str) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    to_datetime = getattr(value, "ToDatetime", None)
    if callable(to_datetime):
        to_datetime_fn = cast(Callable[..., datetime], to_datetime)
        try:
            dt = to_datetime_fn(tzinfo=UTC)
        except TypeError:
            dt = to_datetime_fn()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    raise SecretBackendUnavailableError(
        f"GCP Secret Manager version metadata missing create_time for {resource_name}"
    )


def _coerce_gcp_access_result(result: str | GcpSecretAccessResult) -> GcpSecretAccessResult:
    if isinstance(result, GcpSecretAccessResult):
        return result
    return GcpSecretAccessResult(value=result, last_rotated_at="")


def _default_gcp_secret_accessor(resource_name: str) -> GcpSecretAccessResult:
    """Read one GCP Secret Manager secret version via the optional Google SDK."""
    try:
        secretmanager = import_module("google.cloud.secretmanager")
    except ModuleNotFoundError as exc:
        raise SecretBackendUnavailableError("google-cloud-secret-manager is not installed") from exc

    try:
        client_factory: Any = secretmanager.SecretManagerServiceClient
        client: Any = client_factory()
        response: Any = client.access_secret_version(request={"name": resource_name})
        raw_data: object = response.payload.data
        version_name = getattr(response, "name", resource_name)
        version_metadata: Any = client.get_secret_version(request={"name": version_name})
    except Exception as exc:
        raise SecretBackendUnavailableError(
            f"GCP Secret Manager access failed for {resource_name}"
        ) from exc

    return GcpSecretAccessResult(
        value=_decode_gcp_secret_payload(raw_data, resource_name),
        last_rotated_at=_format_gcp_timestamp(
            getattr(version_metadata, "create_time", None),
            resource_name,
        ),
    )


@dataclass(frozen=True)
class GcpSecretManagerResolver:
    """GCP Secret Manager backed provider-secret resolver for R-421."""

    project_id: str
    secret_version: str
    operator_allowlist: frozenset[object]
    secret_accessor: GcpSecretAccessor = _default_gcp_secret_accessor

    def _resource_name(self, name: str) -> str:
        if not name or "/" in name:
            raise SecretResolutionError(SecretFailClass.SECRET_UNKNOWN, name)
        return f"projects/{self.project_id}/secrets/{name}/versions/{self.secret_version}"

    def _lookup_record(self, name: str) -> GcpSecretAccessResult:
        resource_name = self._resource_name(name)
        try:
            return _coerce_gcp_access_result(self.secret_accessor(resource_name))
        except SecretResolutionError:
            raise
        except Exception as exc:
            raise SecretResolutionError(SecretFailClass.SECRET_UNAVAILABLE, name) from exc

    def _lookup(self, name: str) -> str:
        return self._lookup_record(name).value

    def resolve(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> SecretRef:
        """Resolve a managed-cloud secret reference after allowlist enforcement."""
        _enforce_allowlist(
            operator_allowlist=self.operator_allowlist,
            name=name,
            scope=scope,
            tool=tool,
        )
        self._lookup(name)
        return SecretRef(name=name, scope=scope, tier=tier)

    def resolve_with_audit_metadata(
        self,
        name: str,
        scope: SecretScope,
        tier: SandboxTier,
        *,
        tool: ToolContract | None = None,
    ) -> SecretResolutionAuditResult:
        """Resolve a managed-cloud secret reference with version metadata."""
        _enforce_allowlist(
            operator_allowlist=self.operator_allowlist,
            name=name,
            scope=scope,
            tool=tool,
        )
        record = self._lookup_record(name)
        if not record.last_rotated_at.strip():
            raise SecretResolutionError(SecretFailClass.SECRET_UNAVAILABLE, name)
        return SecretResolutionAuditResult(
            ref=SecretRef(name=name, scope=scope, tier=tier),
            secret_last_rotated_at=record.last_rotated_at,
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER.value,
            cache_tier_overhead_ms=0,
            policy_access_decision_reason="permitted",
        )

    def resolve_bootstrap_value(self, name: str) -> str:
        """Resolve the literal managed-cloud secret value for SDK construction."""
        return self._lookup(name)


def make_provider_secret_resolver(
    config: ProviderSecretsConfig,
    *,
    gcp_secret_accessor: GcpSecretAccessor | None = None,
) -> ProviderSecretResolver:
    """Build the backend-specific provider-secret resolver from config."""
    if config.backend is ProviderSecretBackend.GCP_SECRET_MANAGER:
        assert config.gcp_project_id is not None
        return GcpSecretManagerResolver(
            project_id=config.gcp_project_id.strip(),
            secret_version=config.gcp_secret_version,
            operator_allowlist=frozenset(config.operator_allowlist),
            secret_accessor=gcp_secret_accessor or _default_gcp_secret_accessor,
        )
    return KeyringSecretResolver(
        keyring_service=config.keyring_service,
        allow_env_fallback=config.backend is ProviderSecretBackend.LOCAL_KEYRING_ENV_FALLBACK,
        operator_allowlist=frozenset(config.operator_allowlist),
    )


def make_keyring_resolver(
    config: ProviderSecretsConfig,
    *,
    gcp_secret_accessor: GcpSecretAccessor | None = None,
) -> ProviderSecretResolver:
    """Build the configured provider-secret resolver.

    The historical function name is retained for bootstrap compatibility; for
    new code prefer `make_provider_secret_resolver`.
    """
    return make_provider_secret_resolver(config, gcp_secret_accessor=gcp_secret_accessor)
