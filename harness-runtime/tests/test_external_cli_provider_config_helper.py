"""Tests for the external CLI provider temp-config helper."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def _load_helper() -> ModuleType:
    helper_path = (
        Path(__file__).resolve().parents[2] / "tools" / "external_cli_provider_config.py"
    )
    spec = importlib.util.spec_from_file_location("external_cli_provider_config", helper_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_config(root: Path) -> str:
    return f"""[runtime]
deployment_surface = "local-development"
repository_root = "{root.as_posix()}"
default_topology = "single-threaded-linear"
anthropic_optional = false
openai_optional = true
ollama_optional = true

[runtime.provider_secrets]
backend = "local-keyring-env-fallback"
keyring_service = "harness"

[runtime.otel]
otlp_endpoint = "http://localhost:4318"

[runtime.routing_manifest]
manifest_version = 1
per_role_bindings = {{}}
per_workload_overrides = {{}}
retry_policies = {{}}
fallback_chains = [
    {{ primary = {{ provider = "anthropic", model = "claude-haiku-4-5", family = "anthropic" }}, same_family = [], cross_family = [] }},
]
"""


def test_materialize_codex_config_without_modifying_base(tmp_path: Path) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_text = _base_config(repo_root)
    base_path.write_text(base_text, encoding="utf-8")
    out_path = tmp_path / "codex.toml"

    result = helper.materialize_external_cli_config(
        provider="codex",
        base_config=base_path,
        repo_root=repo_root,
        output=out_path,
        model="gpt-5",
    )

    assert result == out_path
    assert base_path.read_text(encoding="utf-8") == base_text
    data = tomllib.loads(out_path.read_text(encoding="utf-8"))
    runtime = data["runtime"]
    assert runtime["enabled_provider_names"] == ["codex"]
    provider = runtime["external_cli_providers"][0]
    assert provider["provider"] == "codex"
    assert provider["kind"] == "codex"
    assert provider["command"] == "codex"
    primary = runtime["routing_manifest"]["fallback_chains"][0]["primary"]
    assert primary == {"provider": "codex", "model": "gpt-5", "family": "openai"}


def test_materialize_custom_generic_config_with_argv_templates(tmp_path: Path) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_path.write_text(_base_config(repo_root), encoding="utf-8")
    out_path = tmp_path / "custom.toml"

    helper.materialize_external_cli_config(
        provider="generic-command",
        provider_name="local_llm",
        command="my-llm",
        args=("--model", "{model}", "--json"),
        auth_args=("auth", "status"),
        response_format="json",
        family="openai",
        model="demo-model",
        base_config=base_path,
        repo_root=repo_root,
        output=out_path,
    )

    runtime = tomllib.loads(out_path.read_text(encoding="utf-8"))["runtime"]
    provider = runtime["external_cli_providers"][0]
    assert provider["provider"] == "local_llm"
    assert provider["kind"] == "generic-command"
    assert provider["args"] == ["--model", "{model}", "--json"]
    assert provider["auth_args"] == ["auth", "status"]
    assert provider["response_format"] == "json"
    assert runtime["routing_manifest"]["fallback_chains"][0]["primary"] == {
        "provider": "local_llm",
        "model": "demo-model",
        "family": "openai",
    }


def test_cli_prints_materialized_provider_config_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_path.write_text(_base_config(repo_root), encoding="utf-8")
    out_path = tmp_path / "gemini.toml"

    exit_code = helper.main(
        [
            "gemini",
            "--base",
            str(base_path),
            "--repo-root",
            str(repo_root),
            "--output",
            str(out_path),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == out_path.as_posix()
    runtime = tomllib.loads(out_path.read_text(encoding="utf-8"))["runtime"]
    assert runtime["enabled_provider_names"] == ["gemini"]
    assert runtime["external_cli_providers"][0]["kind"] == "gemini"


def test_cli_materializes_antigravity_print_mode_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_path.write_text(_base_config(repo_root), encoding="utf-8")

    out_path = tmp_path / "antigravity.toml"

    exit_code = helper.main(
        [
            "antigravity",
            "--base",
            str(base_path),
            "--repo-root",
            str(repo_root),
            "--output",
            str(out_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == out_path.as_posix()
    assert captured.err == ""
    runtime = tomllib.loads(out_path.read_text(encoding="utf-8"))["runtime"]
    assert runtime["enabled_provider_names"] == ["antigravity"]
    provider = runtime["external_cli_providers"][0]
    assert provider["provider"] == "antigravity"
    assert provider["kind"] == "antigravity"
    assert provider["command"] == "agy"
    primary = runtime["routing_manifest"]["fallback_chains"][0]["primary"]
    assert primary == {
        "provider": "antigravity",
        "model": "Gemini 3.5 Flash (Low)",
        "family": "google",
    }
