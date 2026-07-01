"""U-RT-06 — `ProviderSecretsConfig` + `KeyringSecretResolver` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L1:
- Keyring miss raises typed `SecretFailClass`.
- Allowlist enforced (operator + tool intersection).
- Fetch audit event emitted via AS primitives — DEFERRED to L2+ (audit-writer
  binding lands at U-RT-32); composition is the caller's responsibility per
  U-AS-26 separation. At L1 we verify the resolver returns SecretRef in a
  shape the caller can compose into a SecretFetchEvent.

Class 1 risk-flag absorption: the plan flagged this unit as "Class 1 candidate
if AS spec is silent on fetch *site*." Pre-flight reading of AS spec C-AS-05
§5.4 confirmed the keyring-library binding is EXPLICITLY DEFERRED to
implementation discretion. The runtime authored the binding under
Target_Stack_Commitment_v1 §5.1 + ADR-F5 v1.1; not a silent extension; no
back-flow required.
"""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.secret_allowlist import AllowlistDecision
from harness_as.secret_fail_class import SecretFailClass
from harness_as.secret_fetch import SecretRef, SecretScope
from harness_as.tool_contract import SecretAllowlistEntry, ToolContract
from harness_runtime.config.provider_secrets import (
    GcpSecretAccessResult,
    KeyringSecretResolver,
    SecretAllowlistDeniedError,
    SecretResolutionError,
    make_keyring_resolver,
)
from harness_runtime.types import ProviderSecretBackend, ProviderSecretsConfig
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError
from pydantic import ValidationError


class _FakeKeyring(KeyringBackend):
    """In-memory keyring backend for tests."""

    priority = 1  # type: ignore[assignment]  # keyring API uses a property; classvar override is fine for tests

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


class _FailingKeyring(KeyringBackend):
    """Keyring backend that simulates headless/subprocess backend failure."""

    priority = 1  # type: ignore[assignment]

    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringError("backend unavailable")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringError("backend unavailable")

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringError("backend unavailable")


@pytest.fixture
def fake_keyring() -> Iterator[_FakeKeyring]:
    """Install an in-memory keyring for the duration of one test."""
    backend = _FakeKeyring()
    original = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(original)


def _scope(name: str = "default") -> SecretScope:
    return SecretScope(name=name)


def _tool_with_allowed_secrets(
    *entries: SecretAllowlistEntry,
) -> ToolContract:
    """Minimal `ToolContract` for allowlist tests; only `required_secrets` matters here."""
    return ToolContract(
        name="test-tool",
        description="test",
        input_schema={},
        output_schema={},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
        required_secrets=entries,
    )


# ---------------------------------------------------------------------------
# Config invariants.
# ---------------------------------------------------------------------------


def test_provider_secrets_config_defaults() -> None:
    """Empty config: default keyring_service='harness', empty operator allowlist."""
    config = ProviderSecretsConfig()
    assert config.backend is ProviderSecretBackend.LOCAL_KEYRING_ENV_FALLBACK
    assert config.keyring_service == "harness"
    assert config.operator_allowlist == ()


def test_provider_secrets_config_is_frozen() -> None:
    """`ProviderSecretsConfig` is frozen per C-RT-03 invariant."""
    assert ProviderSecretsConfig.model_config.get("frozen") is True


def test_provider_secrets_config_no_secret_values() -> None:
    """C-RT-03 invariant: no secret values in RuntimeConfig (allowlist keys only).

    Pinned via field-name discipline: there is no `secret_values` /
    `passwords` / `tokens` field. Any future addition is a back-flow event.
    """
    field_names = set(ProviderSecretsConfig.model_fields.keys())
    forbidden = {"secret_values", "passwords", "tokens", "credentials"}
    assert not (field_names & forbidden)


def test_gcp_secret_manager_backend_requires_project_id() -> None:
    """R-421: GCP Secret Manager configs must name the GCP project."""
    with pytest.raises(ValidationError) as excinfo:
        ProviderSecretsConfig(backend=ProviderSecretBackend.GCP_SECRET_MANAGER)

    assert "gcp_project_id is required" in str(excinfo.value)


def test_gcp_secret_manager_backend_rejects_project_display_name() -> None:
    """R-421: use the GCP project ID/number, not the display name."""
    with pytest.raises(ValidationError) as excinfo:
        ProviderSecretsConfig(
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
            gcp_project_id="My First Project",
        )

    assert "project ID or numeric project number" in str(excinfo.value)


def test_gcp_secret_manager_backend_accepts_project_id_and_number() -> None:
    """R-421: Secret Manager resource paths accept IDs or project numbers."""
    by_id = ProviderSecretsConfig(
        backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
        gcp_project_id="project-ba535aa4-f08d-46b2-ba6",
    )
    by_number = ProviderSecretsConfig(
        backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
        gcp_project_id="123456789012",
    )

    assert by_id.gcp_project_id == "project-ba535aa4-f08d-46b2-ba6"
    assert by_number.gcp_project_id == "123456789012"


# ---------------------------------------------------------------------------
# Resolver construction.
# ---------------------------------------------------------------------------


def test_make_keyring_resolver_returns_resolver() -> None:
    """`make_keyring_resolver` produces a `KeyringSecretResolver`."""
    config = ProviderSecretsConfig(keyring_service="test-service")
    resolver = make_keyring_resolver(config)
    assert isinstance(resolver, KeyringSecretResolver)
    assert resolver.keyring_service == "test-service"


def test_make_keyring_resolver_returns_gcp_resolver_for_gcp_backend() -> None:
    """R-421: the provider-secret factory dispatches to the GCP resolver."""
    calls: list[str] = []

    def accessor(resource_name: str) -> str:
        calls.append(resource_name)
        return "sk-gcp-from-secret-manager"

    resolver = make_keyring_resolver(
        ProviderSecretsConfig(
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
            gcp_project_id="harness-test-project",
        ),
        gcp_secret_accessor=accessor,
    )

    assert type(resolver).__name__ == "GcpSecretManagerResolver"
    assert resolver.resolve_bootstrap_value("anthropic_key") == "sk-gcp-from-secret-manager"
    assert calls == [
        "projects/harness-test-project/secrets/anthropic_key/versions/latest",
    ]


def test_keyring_resolver_is_frozen() -> None:
    """Resolver is a frozen dataclass; mutation rejected."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    with pytest.raises((AttributeError, Exception)):
        resolver.keyring_service = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Keyring miss raises typed SecretResolutionError (plan AC).
# ---------------------------------------------------------------------------


def test_keyring_miss_raises_secret_resolution_error(fake_keyring: _FakeKeyring) -> None:
    """Missing key → `SecretResolutionError` carrying `SECRET_UNKNOWN` (plan AC)."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    with pytest.raises(SecretResolutionError) as exc_info:
        resolver.resolve("missing-key", _scope(), SandboxTier.TIER_1_PROCESS)
    assert exc_info.value.fail_class is SecretFailClass.SECRET_UNKNOWN
    assert exc_info.value.name == "missing-key"


def test_keyring_hit_returns_secret_ref(fake_keyring: _FakeKeyring) -> None:
    """Present key → `SecretRef` bound to `(name, scope, tier)`."""
    keyring.set_password("harness", "anthropic-api-key", "sk-test-value")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    ref = resolver.resolve("anthropic-api-key", _scope("prod"), SandboxTier.TIER_1_PROCESS)
    assert isinstance(ref, SecretRef)
    assert ref.name == "anthropic-api-key"
    assert ref.scope.name == "prod"
    assert ref.tier is SandboxTier.TIER_1_PROCESS


def test_keyring_service_isolation(fake_keyring: _FakeKeyring) -> None:
    """Resolver only sees keys under its configured `keyring_service`."""
    keyring.set_password("other-service", "key", "value")
    resolver = make_keyring_resolver(ProviderSecretsConfig(keyring_service="harness"))
    with pytest.raises(SecretResolutionError):
        resolver.resolve("key", _scope(), SandboxTier.TIER_1_PROCESS)


# ---------------------------------------------------------------------------
# Allowlist enforcement (plan AC).
# ---------------------------------------------------------------------------


def test_allowlist_intersection_permits_listed_entry(fake_keyring: _FakeKeyring) -> None:
    """Tool ∩ operator allowlist both contain entry → resolution proceeds."""
    keyring.set_password("harness", "shared-key", "value")
    scope = _scope()
    entry = SecretAllowlistEntry(name="shared-key", scope=scope)
    config = ProviderSecretsConfig(operator_allowlist=(entry,))
    resolver = make_keyring_resolver(config)
    tool = _tool_with_allowed_secrets(entry)
    ref = resolver.resolve("shared-key", scope, SandboxTier.TIER_1_PROCESS, tool=tool)
    assert ref.name == "shared-key"


def test_allowlist_denies_when_tool_disallows(fake_keyring: _FakeKeyring) -> None:
    """Operator-allowed but tool-disallowed → `DENIED_NOT_IN_TOOL_ALLOWLIST`."""
    keyring.set_password("harness", "k", "v")
    scope = _scope()
    entry = SecretAllowlistEntry(name="k", scope=scope)
    config = ProviderSecretsConfig(operator_allowlist=(entry,))
    resolver = make_keyring_resolver(config)
    tool = _tool_with_allowed_secrets()  # empty required_secrets
    with pytest.raises(SecretAllowlistDeniedError) as exc_info:
        resolver.resolve("k", scope, SandboxTier.TIER_1_PROCESS, tool=tool)
    assert exc_info.value.decision is AllowlistDecision.DENIED_NOT_IN_TOOL_ALLOWLIST


def test_allowlist_denies_when_operator_policy_disallows(fake_keyring: _FakeKeyring) -> None:
    """Tool-allowed but operator-disallowed → `DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE`."""
    keyring.set_password("harness", "k", "v")
    scope = _scope()
    entry = SecretAllowlistEntry(name="k", scope=scope)
    config = ProviderSecretsConfig(operator_allowlist=())  # empty operator allowlist
    resolver = make_keyring_resolver(config)
    tool = _tool_with_allowed_secrets(entry)
    with pytest.raises(SecretAllowlistDeniedError) as exc_info:
        resolver.resolve("k", scope, SandboxTier.TIER_1_PROCESS, tool=tool)
    assert exc_info.value.decision is AllowlistDecision.DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE


def test_no_tool_skips_allowlist_check(fake_keyring: _FakeKeyring) -> None:
    """Without a tool reference, allowlist check is skipped (bootstrap-only fetch)."""
    keyring.set_password("harness", "k", "v")
    # No allowlist entries; tool=None.
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    ref = resolver.resolve("k", _scope(), SandboxTier.TIER_1_PROCESS, tool=None)
    assert ref.name == "k"


# ---------------------------------------------------------------------------
# Audit-event composition surface (deferred, but shape verified).
# ---------------------------------------------------------------------------


def test_secret_ref_carries_audit_composition_fields(fake_keyring: _FakeKeyring) -> None:
    """SecretRef carries the fields the caller needs to compose SecretFetchEvent.

    Per U-AS-26: the caller composes `SecretFetchEvent(secret_name, secret_scope,
    secret_last_rotated_at, actor, timestamp, thread_id, step_id)`. The
    resolver supplies (secret_name → ref.name, secret_scope → ref.scope) and
    the caller supplies the rest from its execution context. This test pins
    the shape so a future SecretRef change surfaces here.
    """
    keyring.set_password("harness", "audit-shape-key", "v")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    ref = resolver.resolve("audit-shape-key", _scope("audit"), SandboxTier.TIER_1_PROCESS)
    # The two fields the caller needs from the resolver to compose SecretFetchEvent:
    assert ref.name == "audit-shape-key"
    assert ref.scope.name == "audit"


# ---------------------------------------------------------------------------
# Bootstrap-value resolution path (U-RT-17 amendment).
# ---------------------------------------------------------------------------


def test_resolve_bootstrap_value_returns_literal_value(fake_keyring: _FakeKeyring) -> None:
    """`resolve_bootstrap_value` returns the literal keyring value as a str.

    This is the stage-3a CP_CLIENTS path: provider SDKs require `api_key=str`
    at `AsyncAnthropic(...)` / `AsyncOpenAI(...)` construction time, distinct
    from the `SecretRef` handle returned by `.resolve()` for tool call sites.
    """
    keyring.set_password("harness", "anthropic_key", "sk-ant-test-value")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    value = resolver.resolve_bootstrap_value("anthropic_key")
    assert value == "sk-ant-test-value"


def test_resolve_bootstrap_value_missing_raises_secret_unknown(
    fake_keyring: _FakeKeyring,
) -> None:
    """Missing key → `SecretResolutionError(SECRET_UNKNOWN)` (spec §5 line 368)."""
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_bootstrap_value("absent-bootstrap-key")
    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNKNOWN
    assert excinfo.value.name == "absent-bootstrap-key"


def test_resolve_bootstrap_value_skips_allowlist_check(fake_keyring: _FakeKeyring) -> None:
    """Bootstrap path is pre-tool-registration; allowlist is not consulted.

    Verified indirectly: the resolver has an empty operator allowlist (which
    would deny every `.resolve()` call), yet `resolve_bootstrap_value` still
    returns the value because it has no tool/allowlist branch.
    """
    keyring.set_password("harness", "openai_key", "sk-test-no-allowlist-needed")
    # Empty operator_allowlist → .resolve(tool=ToolContract(...)) would deny.
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    # Bootstrap path bypasses allowlist entirely.
    value = resolver.resolve_bootstrap_value("openai_key")
    assert value == "sk-test-no-allowlist-needed"


# ---------------------------------------------------------------------------
# Env-var fallback per ADR-F5 v1.1 §(b)(i) headless-mode framing.
# Discretion record: .harness/binding_fix_keyring_resolver_env_var_fallback.md
# ---------------------------------------------------------------------------


def test_resolve_bootstrap_value_falls_back_to_env_var_when_keyring_returns_none(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic keyring miss + ANTHROPIC_API_KEY set → env-var value returned."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    value = resolver.resolve_bootstrap_value("anthropic_key")
    assert value == "sk-ant-from-env"


def test_resolve_bootstrap_value_env_var_openai(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI keyring miss + OPENAI_API_KEY set → env-var value returned."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa-from-env")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    value = resolver.resolve_bootstrap_value("openai_key")
    assert value == "sk-oa-from-env"


def test_resolve_bootstrap_value_falls_back_when_keyring_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCAL keyring backend error + mapped env var still reaches headless fallback."""
    original = keyring.get_keyring()
    keyring.set_keyring(_FailingKeyring())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    try:
        resolver = make_keyring_resolver(ProviderSecretsConfig())
        value = resolver.resolve_bootstrap_value("anthropic_key")
        assert value == "sk-ant-from-env"
    finally:
        keyring.set_keyring(original)


def test_resolve_bootstrap_value_keyring_wins_over_env_var(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precedence: when both keyring and env var are present, keyring wins.

    Preserves keyring as primary trust anchor per ADR-F5 LOCAL_DEV tier.
    """
    keyring.set_password("harness", "anthropic_key", "sk-ant-from-keyring")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    value = resolver.resolve_bootstrap_value("anthropic_key")
    assert value == "sk-ant-from-keyring"


def test_resolve_bootstrap_value_no_mapping_no_fallback(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names without an env-var mapping fall through to keyring-only.

    Setting an arbitrary env var must NOT satisfy an unmapped keyring miss.
    Preserves prior behavior for non-provider secrets.
    """
    monkeypatch.setenv("UNRELATED_API_KEY", "should-be-ignored")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_bootstrap_value("unmapped-name")
    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNKNOWN
    assert excinfo.value.name == "unmapped-name"


def test_resolve_bootstrap_value_keyring_backend_error_without_fallback_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SELF_HOSTED keyring-only backend does not use env when keyring errors."""
    original = keyring.get_keyring()
    keyring.set_keyring(_FailingKeyring())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    try:
        resolver = make_keyring_resolver(
            ProviderSecretsConfig(backend=ProviderSecretBackend.SELF_HOSTED_KEYRING)
        )
        with pytest.raises(SecretResolutionError) as excinfo:
            resolver.resolve_bootstrap_value("anthropic_key")
        assert excinfo.value.fail_class is SecretFailClass.SECRET_UNAVAILABLE
        assert excinfo.value.name == "anthropic_key"
    finally:
        keyring.set_keyring(original)


def test_resolve_bootstrap_value_neither_keyring_nor_env_raises(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SECRET_UNKNOWN still raised when both keyring AND env var are absent."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_bootstrap_value("anthropic_key")
    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNKNOWN


def test_self_hosted_keyring_backend_does_not_fall_back_to_env_var(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-440: SELF_HOSTED backend must not silently use LOCAL env fallback."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    resolver = make_keyring_resolver(
        ProviderSecretsConfig(backend=ProviderSecretBackend.SELF_HOSTED_KEYRING)
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_bootstrap_value("anthropic_key")

    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNKNOWN
    assert excinfo.value.name == "anthropic_key"


def test_self_hosted_keyring_backend_resolves_keyring_value(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-440: SELF_HOSTED backend resolves from keyring when the key exists."""
    keyring.set_password("harness", "anthropic_key", "sk-ant-from-keyring")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    resolver = make_keyring_resolver(
        ProviderSecretsConfig(backend=ProviderSecretBackend.SELF_HOSTED_KEYRING)
    )

    assert resolver.resolve_bootstrap_value("anthropic_key") == "sk-ant-from-keyring"


def test_gcp_secret_manager_backend_does_not_fall_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-421: MANAGED_CLOUD backend must not silently use LOCAL env fallback."""

    def unavailable_accessor(resource_name: str) -> str:
        raise RuntimeError(f"unavailable: {resource_name}")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    resolver = make_keyring_resolver(
        ProviderSecretsConfig(
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
            gcp_project_id="harness-test-project",
        ),
        gcp_secret_accessor=unavailable_accessor,
    )

    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_bootstrap_value("anthropic_key")

    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNAVAILABLE
    assert excinfo.value.name == "anthropic_key"


def test_gcp_secret_manager_resolve_honors_allowlist() -> None:
    """R-421: GCP-backed SecretRef resolution preserves AS allowlist checks."""

    def accessor(resource_name: str) -> str:
        assert resource_name == "projects/harness-test-project/secrets/e2b-secret/versions/5"
        return "e2b-secret-value"

    scope = _scope("r421-managed-cloud")
    entry = SecretAllowlistEntry(name="e2b-secret", scope=scope)
    resolver = make_keyring_resolver(
        ProviderSecretsConfig(
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
            gcp_project_id="harness-test-project",
            gcp_secret_version="5",
            operator_allowlist=(entry,),
        ),
        gcp_secret_accessor=accessor,
    )
    tool = _tool_with_allowed_secrets(entry)

    ref = resolver.resolve("e2b-secret", scope, SandboxTier.TIER_2_CONTAINER, tool=tool)

    assert ref.name == "e2b-secret"
    assert ref.scope == scope
    assert ref.tier is SandboxTier.TIER_2_CONTAINER


def test_gcp_secret_manager_resolve_with_audit_metadata_returns_rotation_metadata() -> None:
    """R-CXA-1: managed-cloud scoped fetch supplies non-hollow audit metadata."""

    def accessor(resource_name: str) -> GcpSecretAccessResult:
        assert resource_name == "projects/harness-test-project/secrets/e2b-secret/versions/5"
        return GcpSecretAccessResult(
            value="e2b-secret-value",
            last_rotated_at="2026-06-08T00:00:00+00:00",
        )

    scope = _scope("r421-managed-cloud")
    entry = SecretAllowlistEntry(name="e2b-secret", scope=scope)
    resolver = make_keyring_resolver(
        ProviderSecretsConfig(
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
            gcp_project_id="harness-test-project",
            gcp_secret_version="5",
            operator_allowlist=(entry,),
        ),
        gcp_secret_accessor=accessor,
    )
    tool = _tool_with_allowed_secrets(entry)

    result = resolver.resolve_with_audit_metadata(
        "e2b-secret",
        scope,
        SandboxTier.TIER_2_CONTAINER,
        tool=tool,
    )

    assert result.ref.name == "e2b-secret"
    assert result.ref.scope == scope
    assert result.ref.tier is SandboxTier.TIER_2_CONTAINER
    assert result.secret_last_rotated_at == "2026-06-08T00:00:00+00:00"
    assert result.backend == "gcp-secret-manager"
    assert result.policy_access_decision_reason == "permitted"


def test_gcp_secret_manager_audit_metadata_requires_backend_rotation_metadata() -> None:
    """String-only accessors are accepted for resolve(), but not audit closure."""

    def accessor(_resource_name: str) -> str:
        return "e2b-secret-value"

    scope = _scope("r421-managed-cloud")
    entry = SecretAllowlistEntry(name="e2b-secret", scope=scope)
    resolver = make_keyring_resolver(
        ProviderSecretsConfig(
            backend=ProviderSecretBackend.GCP_SECRET_MANAGER,
            gcp_project_id="harness-test-project",
            operator_allowlist=(entry,),
        ),
        gcp_secret_accessor=accessor,
    )
    tool = _tool_with_allowed_secrets(entry)

    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_with_audit_metadata(
            "e2b-secret",
            scope,
            SandboxTier.TIER_2_CONTAINER,
            tool=tool,
        )

    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNAVAILABLE


def test_keyring_audit_metadata_is_unavailable_without_rotation_source(
    fake_keyring: _FakeKeyring,
) -> None:
    """R-CXA-1 avoids keyring sentinels when the backend has no rotation metadata."""
    keyring.set_password("harness", "local-secret", "value")
    scope = _scope("local")
    entry = SecretAllowlistEntry(name="local-secret", scope=scope)
    resolver = make_keyring_resolver(ProviderSecretsConfig(operator_allowlist=(entry,)))
    tool = _tool_with_allowed_secrets(entry)

    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve_with_audit_metadata(
            "local-secret",
            scope,
            SandboxTier.TIER_1_PROCESS,
            tool=tool,
        )

    assert excinfo.value.fail_class is SecretFailClass.SECRET_UNAVAILABLE


def test_resolve_env_var_fallback_honors_allowlist(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-sourced secrets go through allowlist intersection identically.

    Allowlist is access control orthogonal to secret-source per
    `.harness/binding_fix_keyring_resolver_env_var_fallback.md` §3 (d). A
    tool with NO `anthropic_key` in `required_secrets` is denied even when
    the value comes from the env var.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    resolver = make_keyring_resolver(ProviderSecretsConfig())
    # ToolContract with empty required_secrets → allowlist denies.
    tool = _tool_with_allowed_secrets()
    with pytest.raises(SecretAllowlistDeniedError) as excinfo:
        resolver.resolve(
            "anthropic_key",
            _scope(),
            SandboxTier.TIER_1_PROCESS,
            tool=tool,
        )
    assert excinfo.value.decision is not AllowlistDecision.PERMITTED
