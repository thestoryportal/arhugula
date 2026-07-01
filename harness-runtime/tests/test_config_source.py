"""Tests for U-RT-103 — 3-source RuntimeConfig loader (spec v1.35 §3.7).

Maps to acceptance criteria 1-9 at runtime plan v2.31 §1.3.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.config_source import (
    RUNTIME_CONFIG_LOAD_FAIL_CLASS,
    RuntimeConfigLoadError,
    RuntimeConfigSource,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _minimum_required_overrides() -> dict[str, Any]:
    """Minimum kwargs to construct a valid RuntimeConfig.

    Sub-configs are empty placeholders; mirrors the U-RT-04 test fixture.
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


@pytest.fixture(autouse=True)
def _clear_harness_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip every ``HARNESS_*`` var so a leaked dev-shell value can't pollute, and
    isolate CWD to a clean dir so an ambient ``harness.toml`` at the invocation
    directory can't be auto-discovered (§3.7, Reading A) and pollute the no-config
    tests. Tests that need a specific CWD ``monkeypatch.chdir`` again explicitly."""
    for key in list(os.environ):
        if key.startswith("HARNESS_"):
            monkeypatch.delenv(key, raising=False)
    cwd_iso = tmp_path / "_cwd"
    cwd_iso.mkdir()
    monkeypatch.chdir(cwd_iso)


# AC #1 — no env / no file / no CLI overrides composes to Pydantic defaults.
# RuntimeConfig has required fields with no class-level defaults; we supply
# the minimum required set via cli_overrides and verify defaults flow through
# for the remaining fields. Equivalent to `RuntimeConfig(**minimum)`.
def test_default_load_returns_runtime_config_pydantic_defaults() -> None:
    overrides = _minimum_required_overrides()
    cfg = RuntimeConfigSource.load(cli_overrides=overrides)
    direct = RuntimeConfig(**overrides)
    assert cfg == direct
    # Spot-check: an unset optional field inherits its declared default.
    assert cfg.tenant_id is None
    assert cfg.drain_timeout_seconds == 60.0
    assert cfg.step_dispatch_timeout_seconds == 30.0


# AC #2 — HARNESS_TENANT_ID → config.tenant_id.
def test_env_var_supplies_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TENANT_ID", "acme")
    cfg = RuntimeConfigSource.load(cli_overrides=_minimum_required_overrides())
    assert cfg.tenant_id == "acme"


# B-EFFECT-FENCE (§14.22 C-RT-31) — HARNESS_EFFECT_FENCING → config.effect_fencing.
# The flag gates a CORRECTNESS property (at-most-once execution), so the env path
# must reach it (out-of-family Codex + advisor caught the original omission).
def test_env_var_supplies_effect_fencing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_EFFECT_FENCING", "true")
    cfg = RuntimeConfigSource.load(cli_overrides=_minimum_required_overrides())
    assert cfg.effect_fencing is True
    # Absent → the opt-out default (byte-identical to pre-v1.60).
    monkeypatch.delenv("HARNESS_EFFECT_FENCING", raising=False)
    assert (
        RuntimeConfigSource.load(cli_overrides=_minimum_required_overrides()).effect_fencing
        is False
    )


# B-L2-EMBEDDING-ACTIVATION (C-CP-02 §2.2) — HARNESS_ROUTING_ACTIVATION →
# config.routing_activation. The flag gates a behavior-changing property (which model
# serves a workload), so the env path must reach it (the effect_fencing env-pairing).
def test_env_var_supplies_routing_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ROUTING_ACTIVATION", "true")
    cfg = RuntimeConfigSource.load(cli_overrides=_minimum_required_overrides())
    assert cfg.routing_activation is True
    # Absent → the opt-out default (byte-identical / zero blast radius).
    monkeypatch.delenv("HARNESS_ROUTING_ACTIVATION", raising=False)
    assert (
        RuntimeConfigSource.load(cli_overrides=_minimum_required_overrides()).routing_activation
        is False
    )


# AC #3 — config-file [runtime] table supplies tenant_id.
def test_config_file_runtime_table_supplies_tenant_id(tmp_path: Path) -> None:
    config_file = tmp_path / "harness.toml"
    config_file.write_text('[runtime]\ntenant_id = "acme"\n', encoding="utf-8")
    cfg = RuntimeConfigSource.load(
        config_file=config_file,
        cli_overrides=_minimum_required_overrides(),
    )
    assert cfg.tenant_id == "acme"


# AC #4 — CLI overrides supply tenant_id.
def test_cli_overrides_supply_tenant_id() -> None:
    overrides = _minimum_required_overrides() | {"tenant_id": "acme"}
    cfg = RuntimeConfigSource.load(cli_overrides=overrides)
    assert cfg.tenant_id == "acme"


# AC #5 — precedence: env=X + file=Y + CLI=Z → CLI wins.
def test_cli_overrides_win_over_file_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HARNESS_TENANT_ID", "env-tenant")
    config_file = tmp_path / "harness.toml"
    config_file.write_text('[runtime]\ntenant_id = "file-tenant"\n', encoding="utf-8")
    overrides = _minimum_required_overrides() | {"tenant_id": "cli-tenant"}
    cfg = RuntimeConfigSource.load(config_file=config_file, cli_overrides=overrides)
    assert cfg.tenant_id == "cli-tenant"


# AC #6 — precedence: env=X + file=Y (no CLI override of that field) → file wins.
def test_config_file_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_TENANT_ID", "env-tenant")
    config_file = tmp_path / "harness.toml"
    config_file.write_text('[runtime]\ntenant_id = "file-tenant"\n', encoding="utf-8")
    cfg = RuntimeConfigSource.load(
        config_file=config_file,
        cli_overrides=_minimum_required_overrides(),
    )
    assert cfg.tenant_id == "file-tenant"


# AC #7 — plaintext API key at config file → RT-FAIL-CLI-CONFIG-LOAD typed exc.
def test_plaintext_api_key_in_config_raises_secrets_excluded_error(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "harness.toml"
    config_file.write_text(
        '[runtime]\nanthropic_api_key = "sk-fake-leaked-token"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        RuntimeConfigSource.load(
            config_file=config_file,
            cli_overrides=_minimum_required_overrides(),
        )
    err = excinfo.value
    assert err.FAIL_CLASS == RUNTIME_CONFIG_LOAD_FAIL_CLASS == "RT-FAIL-CLI-CONFIG-LOAD"
    assert "ADR-F5" in str(err)
    assert "keyring" in str(err).lower()
    assert "anthropic_api_key" in str(err)


# AC #8 — TOML parse error → RT-FAIL-CLI-CONFIG-LOAD with file path.
def test_toml_parse_error_raises_config_load_error(tmp_path: Path) -> None:
    config_file = tmp_path / "broken.toml"
    config_file.write_text("[runtime\nthis is not valid TOML", encoding="utf-8")
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        RuntimeConfigSource.load(
            config_file=config_file,
            cli_overrides=_minimum_required_overrides(),
        )
    err = excinfo.value
    assert err.FAIL_CLASS == "RT-FAIL-CLI-CONFIG-LOAD"
    assert "TOML parse error" in str(err)
    assert str(config_file) in str(err)


# AC #9 — type mismatch (TOML integer for str field) → RT-FAIL-CLI-CONFIG-LOAD.
def test_type_mismatch_raises_config_load_error_with_pydantic_validation(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "harness.toml"
    config_file.write_text("[runtime]\ntenant_id = 42\n", encoding="utf-8")
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        RuntimeConfigSource.load(
            config_file=config_file,
            cli_overrides=_minimum_required_overrides(),
        )
    err = excinfo.value
    assert err.FAIL_CLASS == "RT-FAIL-CLI-CONFIG-LOAD"
    # Per probe-v4 adjacent finding (a) β-scope apply 2026-05-29: the error
    # payload now lists invalid fields by dotted path + message, rather
    # than emitting a Python dict-repr. The `tenant_id` field receives an
    # int and Pydantic surfaces an "Input should be a valid string"-shape
    # message; the dotted path identifies the field unambiguously.
    msg = str(err)
    assert "Invalid fields:" in msg
    assert "tenant_id" in msg
    assert "Pydantic validation failed" not in msg  # old format struck


# Probe-v4 adjacent finding (a) β-scope apply 2026-05-29: missing-required
# error renders as a clean bullet list of dotted-path field names + a
# pointer to the workspace-root template, rather than Python dict-repr.
# Anchors operator-facing fix at `.harness/class_1_fork_daemon_default_
# socket_path_pid_mismatch.md` §4 (a).
def test_missing_required_fields_render_as_human_readable_bullet_list() -> None:
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        # No env, no file, no CLI overrides → RuntimeConfig's 4 required
        # fields (deployment_surface, repository_root, otel,
        # default_topology) all missing.
        RuntimeConfigSource.load()
    err = excinfo.value
    msg = str(err)
    assert err.FAIL_CLASS == "RT-FAIL-CLI-CONFIG-LOAD"
    # Human-readable section heading
    assert "Missing required fields:" in msg
    # All 4 required fields surface as dotted-path bullets
    for field in (
        "deployment_surface",
        "repository_root",
        "otel",
        "default_topology",
    ):
        assert f"- {field}" in msg, f"missing field bullet for {field!r}"
    # Operator gets a pointer to a template they can copy
    assert "harness.toml.example" in msg
    # Old dict-repr format is gone
    assert "Pydantic validation failed" not in msg
    assert "'type': 'missing'" not in msg


def test_nested_invalid_field_renders_with_dotted_loc_path(
    tmp_path: Path,
) -> None:
    """Pydantic validation on a nested sub-config field renders with the
    full dotted path (e.g. ``otel.otlp_endpoint``), not a tuple-repr."""
    config_file = tmp_path / "harness.toml"
    # Endpoint without a `://` scheme triggers OTelConfig's field validator.
    config_file.write_text(
        '[runtime.otel]\notlp_endpoint = "no-scheme-host"\n',
        encoding="utf-8",
    )
    overrides = _minimum_required_overrides()
    # Drop the override's `otel` so the file-supplied value flows through.
    del overrides["otel"]
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        RuntimeConfigSource.load(
            config_file=config_file,
            cli_overrides=overrides,
        )
    msg = str(excinfo.value)
    assert "Invalid fields:" in msg
    assert "otel.otlp_endpoint" in msg
    assert "harness.toml.example" in msg


# Coverage extension: secrets-exclusion catches nested keys too (Q-L=b walks
# the full TOML document, not just the top-level table).
def test_nested_secret_key_in_config_raises_secrets_excluded_error(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "harness.toml"
    config_file.write_text(
        '[provider.anthropic]\nsecret_token = "sk-fake"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        RuntimeConfigSource.load(
            config_file=config_file,
            cli_overrides=_minimum_required_overrides(),
        )
    assert "secret_token" in str(excinfo.value)


# Per `[[finding-runtime-config-loader-unreachable-sub-configs]]` fix (A):
# `path_bindings`, `provider_secrets`, `collector` now default-factory; only
# `deployment_surface`, `repository_root`, `default_topology`, `otel` are
# required. Operators can author a minimal harness.toml with just the
# operator-specific fields (deployment surface, repo root, otel endpoint).
def test_minimal_harness_toml_loads_with_sub_config_defaults(
    tmp_path: Path,
) -> None:
    """A harness.toml with only the genuinely-required fields (deployment
    surface + repository root + default topology + otel endpoint) MUST
    load successfully — the three sub-configs without operator-specific
    values default-factory. Pre-fix (A), all three would raise pydantic
    `missing` errors at the merged-dict layer."""
    config_file = tmp_path / "harness.toml"
    config_file.write_text(
        "[runtime]\n"
        'deployment_surface = "local-development"\n'
        f'repository_root = "{tmp_path}"\n'
        'default_topology = "single-threaded-linear"\n'
        "\n"
        "[runtime.otel]\n"
        'otlp_endpoint = "http://localhost:4318"\n',
        encoding="utf-8",
    )
    cfg = RuntimeConfigSource.load(config_file=config_file)
    assert cfg.deployment_surface == DeploymentSurface.LOCAL_DEVELOPMENT
    assert cfg.path_bindings == PathBindingConfig()
    assert cfg.provider_secrets == ProviderSecretsConfig()
    assert cfg.collector == CollectorConfig()
    assert cfg.otel.otlp_endpoint == "http://localhost:4318"


# Per `[[finding-runtime-config-loader-unreachable-sub-configs]]` fix (B):
# the SCHEMA FIELD NAME `provider_secrets` (a TOML sub-table name) MUST NOT
# false-match the plaintext-secret detector. The leaf-vs-table discrimination
# (skip key-check when value is a dict) preserves the detector's purpose
# (catching plaintext-secret VALUES) while excluding schema field names.
def test_provider_secrets_sub_table_name_does_not_false_match_detector(
    tmp_path: Path,
) -> None:
    """A `[runtime.provider_secrets]` sub-table MUST load without raising
    the plaintext-secret false-positive. Pre-fix, the schema field name
    itself raised `RT-FAIL-CLI-CONFIG-LOAD: plaintext secret detected at
    'runtime.provider_secrets'`.

    NOTE: cli_overrides take precedence in the 3-source merge per
    `config_source.py:140` (`merged.update(cli_values)`), so we omit
    `provider_secrets` from the overrides here — otherwise the CLI default
    would clobber whatever the file specified. With fix (A) (default-
    factoried `provider_secrets`), the overrides no longer need to include
    it at all to satisfy pydantic validation.
    """
    overrides = _minimum_required_overrides()
    del overrides["provider_secrets"]
    config_file = tmp_path / "harness.toml"
    config_file.write_text(
        '[runtime]\n[runtime.provider_secrets]\nkeyring_service = "harness-test"\n',
        encoding="utf-8",
    )
    cfg = RuntimeConfigSource.load(
        config_file=config_file,
        cli_overrides=overrides,
    )
    assert cfg.provider_secrets.keyring_service == "harness-test"


# Per `[[finding-runtime-config-loader-unreachable-sub-configs]]` fix (B)
# regression guard: the detector MUST still fire on a real plaintext secret
# even when it sits inside a sub-table named with a regex-matching word.
def test_secret_leaf_inside_provider_secrets_sub_table_still_caught(
    tmp_path: Path,
) -> None:
    """An ACTUAL plaintext secret at a leaf inside `[runtime.provider_
    secrets]` MUST still raise. The leaf-vs-table discrimination doesn't
    weaken security; it only stops false-matching on TABLE NAMES.
    """
    config_file = tmp_path / "harness.toml"
    config_file.write_text(
        '[runtime.provider_secrets]\napi_key = "sk-leaked-via-config-file"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigLoadError) as excinfo:
        RuntimeConfigSource.load(
            config_file=config_file,
            cli_overrides=_minimum_required_overrides(),
        )
    assert "api_key" in str(excinfo.value)
    assert "ADR-F5" in str(excinfo.value)


# §3.7 auto-discovery (Reading A — CWD). Closes the declared-but-unimplemented gap at
# `.harness/class_1_fork_harness_toml_default_discovery_unimplemented.md` (A-4, Reading A).
def test_discovers_cwd_harness_toml_when_config_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "harness.toml").write_text(
        '[runtime]\ntenant_id = "discovered"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    cfg = RuntimeConfigSource.load(cli_overrides=_minimum_required_overrides())
    assert cfg.tenant_id == "discovered"


def test_no_cwd_harness_toml_falls_back_to_env_and_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No harness.toml at CWD → discovery returns None → env+CLI-only (today's behavior).
    monkeypatch.chdir(tmp_path)
    cfg = RuntimeConfigSource.load(
        cli_overrides=_minimum_required_overrides() | {"tenant_id": "cli-only"}
    )
    assert cfg.tenant_id == "cli-only"


def test_explicit_config_takes_precedence_over_cwd_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "harness.toml").write_text('[runtime]\ntenant_id = "cwd"\n', encoding="utf-8")
    explicit = tmp_path / "other.toml"
    explicit.write_text('[runtime]\ntenant_id = "explicit"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = RuntimeConfigSource.load(
        config_file=explicit, cli_overrides=_minimum_required_overrides()
    )
    assert cfg.tenant_id == "explicit"


def test_discover_default_config_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert RuntimeConfigSource._discover_default_config() is None
    (tmp_path / "harness.toml").write_text("[runtime]\n", encoding="utf-8")
    found = RuntimeConfigSource._discover_default_config()
    assert found is not None
    assert found.name == "harness.toml"
