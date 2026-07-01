"""U-RT-04 — `materialize_runtime_config()` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L1:
- Precedence tested via three-source fixture (default vs env vs kwargs).
- Missing-required raises typed error.
- Unknown keys rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.config.loader import (
    ENV_PREFIX,
    materialize_runtime_config,
)
from harness_runtime.types import (
    CollectorConfig,
    ExternalCLIProviderConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from pydantic import ValidationError


def _required_kwargs() -> dict[str, Any]:
    """The minimum required-field kwargs for `RuntimeConfig` at L1.

    Sub-configs are empty placeholders until U-RT-05..U-RT-08 enrich them.
    """
    return {
        "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
        "repository_root": Path("/tmp"),
        "path_bindings": PathBindingConfig(),
        "provider_secrets": ProviderSecretsConfig(),
        "otel": OTelConfig(otlp_endpoint="http://localhost:4318"),
        "collector": CollectorConfig(),
        "default_topology": TopologyPattern.SINGLE_THREADED_LINEAR,
    }


def test_kwargs_only_materializes() -> None:
    """No env, full kwargs — produces a `RuntimeConfig` byte-equal to direct ctor."""
    cfg = materialize_runtime_config(env={}, **_required_kwargs())
    direct = RuntimeConfig(**_required_kwargs())
    assert cfg == direct


def test_enabled_provider_names_defaults_to_existing_three_providers() -> None:
    """R-CLI-1 keeps current bootstrap behavior unless operators opt in."""
    cfg = materialize_runtime_config(env={}, **_required_kwargs())
    assert cfg.enabled_provider_names == ("anthropic", "openai", "ollama")
    assert cfg.external_cli_providers == ()


def test_external_cli_provider_config_materializes_from_kwargs() -> None:
    """Operators can opt into a local authenticated CLI provider without secrets."""
    cfg = materialize_runtime_config(
        env={},
        **_required_kwargs(),
        enabled_provider_names=("claude_code",),
        external_cli_providers=(
            ExternalCLIProviderConfig(
                provider="claude_code",
                kind="claude-code",
                command="claude",
                timeout_seconds=90.0,
            ),
        ),
    )
    assert cfg.enabled_provider_names == ("claude_code",)
    assert cfg.external_cli_providers[0].provider == "claude_code"
    assert cfg.external_cli_providers[0].command == "claude"


def test_env_supplies_scalar_fields_when_kwargs_omit_them() -> None:
    """Env supplies the 4 scalar top-level fields when kwargs don't override."""
    env = {
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "managed-cloud",
        f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "orchestrator-workers",
        f"{ENV_PREFIX}TENANT_ID": "tenant-a",
    }
    # Sub-configs still required via kwargs (they're not env-keyed at L1).
    cfg = materialize_runtime_config(
        env=env,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    assert cfg.deployment_surface is DeploymentSurface.MANAGED_CLOUD
    assert cfg.repository_root == Path("/tmp")
    assert cfg.default_topology is TopologyPattern.ORCHESTRATOR_WORKERS
    assert cfg.tenant_id == "tenant-a"


def test_kwargs_override_env() -> None:
    """When both env and kwargs supply a field, kwargs wins (spec precedence)."""
    env = {
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "managed-cloud",
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "orchestrator-workers",
    }
    cfg = materialize_runtime_config(
        env=env,
        # kwargs override these two:
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        default_topology=TopologyPattern.PARALLELIZATION,
        # remaining required fields:
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    assert cfg.deployment_surface is DeploymentSurface.LOCAL_DEVELOPMENT
    assert cfg.default_topology is TopologyPattern.PARALLELIZATION


def test_three_source_precedence_fixture() -> None:
    """Default vs env vs kwargs — kwargs wins, env wins over default, default fills the rest.

    Plan §2 L1 AC: 'precedence tested via three-source fixture'.
    """
    env = {
        # env supplies deployment_surface; kwargs leaves it alone.
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "self-hosted-server",
        # env AND kwargs supply default_topology; kwargs wins.
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "orchestrator-workers",
        # env supplies tenant_id; kwargs leaves it alone.
        f"{ENV_PREFIX}TENANT_ID": "tenant-from-env",
    }
    cfg = materialize_runtime_config(
        env=env,
        # kwargs override default_topology.
        default_topology=TopologyPattern.HIERARCHICAL_DELEGATION,
        # repository_root is required and has no default → must come from kwargs (or env).
        repository_root=Path("/tmp"),
        # Sub-configs required.
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    # env-supplied (no kwarg):
    assert cfg.deployment_surface is DeploymentSurface.SELF_HOSTED_SERVER
    assert cfg.tenant_id == "tenant-from-env"
    # kwargs-overridden (env had a different value):
    assert cfg.default_topology is TopologyPattern.HIERARCHICAL_DELEGATION
    # default-supplied (neither env nor kwargs):
    assert cfg.mcp_clients == []


def test_env_supplies_ollama_fields() -> None:
    """`HARNESS_OLLAMA_HOST` + `HARNESS_OLLAMA_OPTIONAL` reach RuntimeConfig.

    Pinned because the loader uses an explicit `_ENV_SCALAR_FIELDS` map (not
    iteration over `model_fields`); adding fields to `RuntimeConfig` without
    updating the map silently drops the env-var precedence path.
    """
    env = {
        f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "local-development",
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "single-threaded-linear",
        f"{ENV_PREFIX}OLLAMA_HOST": "http://my-ollama:11434",
        f"{ENV_PREFIX}OLLAMA_OPTIONAL": "true",
    }
    cfg = materialize_runtime_config(
        env=env,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    assert cfg.ollama_host == "http://my-ollama:11434"
    assert cfg.ollama_optional is True


def test_env_supplies_effect_fencing() -> None:
    """`HARNESS_EFFECT_FENCING` reaches RuntimeConfig (B-EFFECT-FENCE §14.22).

    Pinned because the flag gates a CORRECTNESS property (at-most-once execution):
    an operator who sets the env var must NOT be silently left without the fence
    (the no-silent-failure discipline; out-of-family Codex + advisor caught the
    original env-loader omission).
    """
    base = {
        f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "local-development",
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "single-threaded-linear",
    }
    sub = dict(
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    on = materialize_runtime_config(env={**base, f"{ENV_PREFIX}EFFECT_FENCING": "true"}, **sub)
    assert on.effect_fencing is True
    # Absent env var → the opt-out default (byte-identical to pre-v1.60).
    off = materialize_runtime_config(env=base, **sub)
    assert off.effect_fencing is False


def test_env_supplies_routing_activation() -> None:
    """`HARNESS_ROUTING_ACTIVATION` reaches RuntimeConfig (B-L2-EMBEDDING-ACTIVATION,
    C-CP-02 §2.2 routing-activation gate).

    Pinned because the flag gates a BEHAVIOR-changing property (which model serves a
    workload): an operator who sets the env var must NOT be silently dropped (the
    no-silent-failure discipline; the same env-loader pairing effect_fencing needed —
    [[runtimeconfig-scalar-needs-both-env-loaders]]).
    """
    base = {
        f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "local-development",
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "single-threaded-linear",
    }
    sub = dict(
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    on = materialize_runtime_config(env={**base, f"{ENV_PREFIX}ROUTING_ACTIVATION": "true"}, **sub)
    assert on.routing_activation is True
    # Absent env var → the opt-out default (byte-identical / zero blast radius).
    off = materialize_runtime_config(env=base, **sub)
    assert off.routing_activation is False


def test_ollama_optional_bool_parsing() -> None:
    """`_parse_bool` accepts the common truthy spellings; everything else is False."""
    # The Python `bool("False") == True` trap necessitates explicit parsing.
    for truthy in ("1", "true", "TRUE", "Yes", "on", "  true  "):
        env = {
            f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
            f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "local-development",
            f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "single-threaded-linear",
            f"{ENV_PREFIX}OLLAMA_OPTIONAL": truthy,
        }
        cfg = materialize_runtime_config(
            env=env,
            path_bindings=PathBindingConfig(),
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
            collector=CollectorConfig(),
        )
        assert cfg.ollama_optional is True, f"truthy {truthy!r} should parse True"

    for falsy in ("0", "false", "False", "no", "off", "", "anything-else"):
        env = {
            f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
            f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "local-development",
            f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "single-threaded-linear",
            f"{ENV_PREFIX}OLLAMA_OPTIONAL": falsy,
        }
        cfg = materialize_runtime_config(
            env=env,
            path_bindings=PathBindingConfig(),
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
            collector=CollectorConfig(),
        )
        assert cfg.ollama_optional is False, f"falsy {falsy!r} should parse False"


def test_kwargs_override_env_for_ollama_fields() -> None:
    """`ollama_host` / `ollama_optional` kwargs win over env per the standard precedence."""
    env = {
        f"{ENV_PREFIX}REPOSITORY_ROOT": "/tmp",
        f"{ENV_PREFIX}DEPLOYMENT_SURFACE": "local-development",
        f"{ENV_PREFIX}DEFAULT_TOPOLOGY": "single-threaded-linear",
        f"{ENV_PREFIX}OLLAMA_HOST": "http://env-ollama:1",
        f"{ENV_PREFIX}OLLAMA_OPTIONAL": "true",
    }
    cfg = materialize_runtime_config(
        env=env,
        ollama_host="http://kwarg-ollama:2",
        ollama_optional=False,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
    )
    assert cfg.ollama_host == "http://kwarg-ollama:2"
    assert cfg.ollama_optional is False


def test_missing_required_raises_validation_error() -> None:
    """Required-field absence from all three precedence levels raises typed error.

    Per C-RT-03 RT-FAIL-CONFIG (permanent).

    Required fields after `[[finding-runtime-config-loader-unreachable-sub-
    configs]]` fix (A): `deployment_surface`, `repository_root`,
    `default_topology`, `otel` (the operator-specific endpoint).
    `path_bindings`, `provider_secrets`, `collector` now default-factory.
    """
    with pytest.raises(ValidationError) as exc_info:
        # Missing `repository_root` + `otel`; the three default-factoried
        # sub-configs are NOT missing per fix (A).
        materialize_runtime_config(
            env={},
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        )
    missing_fields = {err["loc"][0] for err in exc_info.value.errors()}
    assert "repository_root" in missing_fields
    assert "otel" in missing_fields
    # Default-factoried fields MUST NOT appear in the missing set.
    assert "path_bindings" not in missing_fields
    assert "provider_secrets" not in missing_fields
    assert "collector" not in missing_fields


def test_unknown_kwarg_rejected() -> None:
    """Unknown keys rejected via `RuntimeConfig.extra='forbid'` (C-RT-03)."""
    with pytest.raises(ValidationError):
        materialize_runtime_config(env={}, unknown_field="x", **_required_kwargs())


def test_env_defaults_to_os_environ_when_none() -> None:
    """`env=None` falls back to `os.environ`; passing `{}` asserts env-independence."""
    # If os.environ doesn't have a HARNESS_* clash, this behaves like env={}.
    import os

    assert not any(k.startswith(ENV_PREFIX) for k in os.environ), (
        "test env contamination: clear HARNESS_* env vars before running this test"
    )
    cfg = materialize_runtime_config(**_required_kwargs())
    assert cfg.deployment_surface is DeploymentSurface.LOCAL_DEVELOPMENT


def test_no_file_loading_per_spec() -> None:
    """C-RT-03: 'No file-loading; that is Track B.' Verified by signature surface.

    The `materialize_runtime_config` signature has no path-to-config-file
    parameter. This test pins the surface: any future addition of a file-path
    parameter is a back-flow event requiring C-RT-03 amendment.
    """
    import inspect

    sig = inspect.signature(materialize_runtime_config)
    param_names = set(sig.parameters.keys())
    # The only fixed parameters are `env` and `**kwargs`. No `config_file`,
    # `path`, `from_file`, or equivalent.
    forbidden = {"config_file", "config_path", "from_file", "path", "file"}
    assert not (param_names & forbidden), (
        f"file-loading parameter detected: {param_names & forbidden}"
    )
