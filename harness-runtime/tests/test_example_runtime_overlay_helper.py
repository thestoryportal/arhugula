"""Tests for the operator helper that materializes example runtime overlays."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def _load_helper() -> ModuleType:
    helper_path = Path(__file__).resolve().parents[2] / "tools" / "apply_example_runtime_overlay.py"
    spec = importlib.util.spec_from_file_location("apply_example_runtime_overlay", helper_path)
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

[[runtime.path_bindings.raw_entries]]
path_class = "SKILLS"
workflow_class = "pipeline-automation"
deployment_surface = "local-development"
path = "{(root / 'skills').as_posix()}"

[runtime.routing_manifest]
manifest_version = 1
per_role_bindings = {{}}
per_workload_overrides = {{}}
retry_policies = {{}}
fallback_chains = [
    {{ primary = {{ provider = "anthropic", model = "claude-haiku-4-5", family = "anthropic" }}, same_family = [], cross_family = [] }},
]
"""


def test_apply_overlay_replaces_placeholder_and_does_not_modify_base(tmp_path: Path) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_text = _base_config(repo_root)
    base_path.write_text(base_text, encoding="utf-8")
    overlay_path = tmp_path / "overlay.toml"
    overlay_path.write_text(
        """[runtime]
anthropic_optional = true
openai_optional = true
ollama_optional = false
ollama_host = "http://127.0.0.1:11434"

[runtime.otel]
otlp_endpoint = "http://127.0.0.1:4317"

[[runtime.path_bindings.raw_entries]]
path_class = "SKILLS"
workflow_class = "research"
deployment_surface = "local-development"
path = "/absolute/path/to/arhugula/skills"
""",
        encoding="utf-8",
    )

    out_path = tmp_path / "materialized.toml"
    result = helper.apply_overlay(
        base_config=base_path,
        overlay=overlay_path,
        repo_root=repo_root,
        output=out_path,
    )

    assert result == out_path
    assert base_path.read_text(encoding="utf-8") == base_text
    rendered = out_path.read_text(encoding="utf-8")
    assert "/absolute/path/to/arhugula" not in rendered
    data = tomllib.loads(rendered)
    runtime = data["runtime"]
    assert runtime["anthropic_optional"] is True
    assert runtime["openai_optional"] is True
    assert runtime["ollama_optional"] is False
    assert runtime["otel"]["otlp_endpoint"] == "http://127.0.0.1:4317"
    entries = runtime["path_bindings"]["raw_entries"]
    assert entries == [
        {
            "path_class": "SKILLS",
            "workflow_class": "research",
            "deployment_surface": "local-development",
            "path": (repo_root / "skills").as_posix(),
        }
    ]


def test_apply_overlay_replaces_routing_manifest_only(tmp_path: Path) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_path.write_text(_base_config(repo_root), encoding="utf-8")
    overlay_path = tmp_path / "routing.toml"
    overlay_path.write_text(
        """[runtime.routing_manifest]
manifest_version = 1
per_role_bindings = {}
per_workload_overrides = {}
retry_policies = {}
fallback_chains = [
    { primary = { provider = "anthropic", model = "claude-sonnet-4-6", family = "anthropic" }, same_family = [], cross_family = [] },
]
""",
        encoding="utf-8",
    )

    out_path = tmp_path / "routing-materialized.toml"
    helper.apply_overlay(
        base_config=base_path,
        overlay=overlay_path,
        repo_root=repo_root,
        output=out_path,
    )

    data = tomllib.loads(out_path.read_text(encoding="utf-8"))
    runtime = data["runtime"]
    assert runtime["deployment_surface"] == "local-development"
    assert runtime["routing_manifest"]["fallback_chains"][0]["primary"]["model"] == (
        "claude-sonnet-4-6"
    )
    assert runtime["path_bindings"]["raw_entries"][0]["workflow_class"] == (
        "pipeline-automation"
    )


def test_cli_prints_output_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_path.write_text(_base_config(repo_root), encoding="utf-8")
    overlay_path = tmp_path / "overlay.toml"
    overlay_path.write_text("[runtime]\nopenai_optional = false\n", encoding="utf-8")
    out_path = tmp_path / "out.toml"

    exit_code = helper.main(
        [
            str(overlay_path),
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
    assert out_path.exists()


def test_cli_without_output_prints_created_temp_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load_helper()
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    base_path = tmp_path / "harness.toml"
    base_text = _base_config(repo_root)
    base_path.write_text(base_text, encoding="utf-8")
    overlay_path = tmp_path / "overlay.toml"
    overlay_path.write_text("[runtime]\nopenai_optional = false\n", encoding="utf-8")

    exit_code = helper.main(
        [
            str(overlay_path),
            "--base",
            str(base_path),
            "--repo-root",
            str(repo_root),
        ]
    )

    printed = Path(capsys.readouterr().out.strip())
    try:
        assert exit_code == 0
        assert printed.exists()
        assert printed != base_path
        assert base_path.read_text(encoding="utf-8") == base_text
        data = tomllib.loads(printed.read_text(encoding="utf-8"))
        assert data["runtime"]["openai_optional"] is False
    finally:
        printed.unlink(missing_ok=True)
