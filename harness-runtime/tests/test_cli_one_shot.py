"""Tests for U-RT-106 — one-shot mode (``harness run <file>``).

Maps to acceptance criteria 1–11 at runtime plan v2.31 §1.6 + NEW AC #4 at
v2.32 §2 (deployment-surface-keyed engine_class admissibility at dispatch
site, per spec v1.36 §14.18.4).

Strategy:
- mock ``harness_runtime.api.run`` so the test never invokes real bootstrap
- mock ``RuntimeConfigSource.load`` so we control the loaded ``RuntimeConfig``
  (in particular ``deployment_surface`` which drives the admissibility check)
- fixture YAML/TOML manifests at the operator file-extension boundary
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import harness_runtime.cli.app as _ensure_import_side_effect
import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_runtime.api import FailureCause, RunResult
from harness_runtime.cli.app import (
    EXIT_BOOTSTRAP_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_MANIFEST_ERROR,
    EXIT_SUCCESS,
    EXIT_WORKFLOW_FAIL,
    app,
)
from typer.testing import CliRunner

_cli_app_mod = sys.modules["harness_runtime.cli.app"]
assert _ensure_import_side_effect is not None  # silence reportUnusedImport
from harness_core.identity import WorkflowID
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.config_source import RuntimeConfigLoadError
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

runner = CliRunner()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_WME_FRAGMENT = """\
  layer_budgets: []
  fallback_chain:
    primary:
      provider: "anthropic"
      model: "claude-opus-4-7"
      family: "anthropic"
    same_family: []
    cross_family: []
  hitl_placements: []
  per_step_overrides: {}
"""


def _make_yaml(*, workflow_id: str, engine_class: str) -> str:
    return (
        "version: 1\n"
        "workflow:\n"
        f'  workflow_id: "{workflow_id}"\n'
        '  workload_class: "software-engineering"\n'
        '  persona_tier: "solo-developer"\n'
        f'  engine_class: "{engine_class}"\n'
        '  topology_pattern: "evaluator-optimizer"\n'
        + _WME_FRAGMENT
        + "default_model_binding:\n"
        + '  provider: "anthropic"\n'
        + '  model: "claude-opus-4-7"\n'
        + "steps:\n"
        + '  - step_id: "s1"\n'
        + '    step_kind: "inference-step"\n'
        + "    step_payload: {}\n"
    )


_VALID_YAML = _make_yaml(workflow_id="wf-cli-one-shot", engine_class="pure-pattern-no-engine")
_RECONCILER_YAML = _make_yaml(workflow_id="wf-cli-reconciler", engine_class="reconciler-loop")


def _write_yaml(tmp_path: Path, body: str = _VALID_YAML) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _runtime_config(
    *,
    deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
    tenant_id: str | None = None,
) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        tenant_id=tenant_id,
    )


def _run_result(
    *, status: str = "completed", failure_cause: FailureCause | None = None
) -> RunResult:
    return RunResult(
        status=status,  # type: ignore[arg-type]
        workflow_id=WorkflowID("wf-cli-one-shot"),
        terminal_state={},
        audit_ledger_head_hash="0" * 64,
        trace_ids=(),
        cost_attribution=(),
        failure_cause=failure_cause,
    )


@pytest.fixture
def mock_config_load(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., None]:
    """Install a fake ``RuntimeConfigSource.load``."""

    captured: dict[str, Any] = {}

    def _install(
        *,
        deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
        raises: BaseException | None = None,
    ) -> None:
        def _fake_load(
            cls: Any,
            config_file: Path | None = None,
            cli_overrides: dict[str, Any] | None = None,
        ) -> RuntimeConfig:
            captured["config_file"] = config_file
            captured["cli_overrides"] = dict(cli_overrides or {})
            if raises is not None:
                raise raises
            return _runtime_config(
                deployment_surface=deployment_surface,
                tenant_id=(cli_overrides or {}).get("tenant_id"),
            )

        monkeypatch.setattr(
            _cli_app_mod.RuntimeConfigSource,
            "load",
            classmethod(_fake_load),
        )

    _install.captured = captured  # type: ignore[attr-defined]
    return _install


@pytest.fixture
def mock_api_run(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install a fake ``harness_runtime.api.run``."""

    captured: dict[str, Any] = {}

    def _install(
        *,
        result: RunResult | None = None,
        raises: BaseException | None = None,
    ) -> None:
        async def _fake_run(
            workflow: Any,
            *,
            config: Any = None,
        ) -> RunResult:
            captured["workflow"] = workflow
            captured["config"] = config
            if raises is not None:
                raise raises
            return result if result is not None else _run_result()

        monkeypatch.setattr("harness_runtime.api.run", _fake_run)

    _install.captured = captured  # type: ignore[attr-defined]
    return _install


# ---------------------------------------------------------------------------
# AC #1 — SUCCESS path emits RunResult to stdout + exit 0
# ---------------------------------------------------------------------------


def test_ac1_success_path_emits_run_result_and_exits_zero(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="completed"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    out = _plain(result.stdout)
    assert "completed" in out
    assert "wf-cli-one-shot" in out


# ---------------------------------------------------------------------------
# AC #2 — --output=json emits JSON-serialized RunResult
# ---------------------------------------------------------------------------


def test_ac2_output_json_emits_json_serialized_run_result(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="completed"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest), "--output", "json"])
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["status"] == "completed"
    assert parsed["workflow_id"] == "wf-cli-one-shot"


# ---------------------------------------------------------------------------
# AC #3 — --output=text (default) emits human-readable RunResult
# ---------------------------------------------------------------------------


def test_ac3_output_text_default_emits_human_readable(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="completed"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    out = _plain(result.stdout)
    # Text mode emits ``status:`` / ``workflow:`` / ``ledger:`` labels; JSON
    # mode would not.
    assert "status:" in out
    assert "workflow:" in out


# ---------------------------------------------------------------------------
# NEW AC #4 (v2.32 §2) — deployment-surface-keyed admissibility at dispatch
# ---------------------------------------------------------------------------


def test_engine_class_not_admissible_for_deployment_surface_raises_admissibility_error(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    # LOCAL_DEVELOPMENT excludes RECONCILER_LOOP per §7.2 ("requires K8s
    # control plane"). Manifest engine_class=reconciler-loop must fail
    # admissibility at dispatch site → exit 2.
    mock_config_load(deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT)
    mock_api_run()  # would PASS if reached — but admissibility blocks first
    manifest = _write_yaml(tmp_path, body=_RECONCILER_YAML)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_MANIFEST_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-CLI-MANIFEST-ADMISSIBILITY" in result.stderr
    assert "reconciler-loop" in result.stderr or "RECONCILER_LOOP" in result.stderr
    # api.run must not have been invoked.
    assert "workflow" not in mock_api_run.captured  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC #5 (was #4) — Workflow FAILED status → exit 1
# ---------------------------------------------------------------------------


def test_ac5_workflow_failed_status_exits_one(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(
        result=_run_result(
            status="failed",
            failure_cause=FailureCause(
                runtime_fail_class="RT-FAIL-WORKFLOW",
                detail="step s1 failed",
            ),
        )
    )
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_WORKFLOW_FAIL, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# AC #6 (was #5) — Workflow DRAINED status → exit 1
# ---------------------------------------------------------------------------


def test_ac6_workflow_drained_status_exits_one(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="drained"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_WORKFLOW_FAIL, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# AC #7 (was #6) — Manifest load error → exit 2 + fail-class to stderr
# ---------------------------------------------------------------------------


def test_ac7_manifest_load_error_exits_two_and_emits_fail_class(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run()
    # Missing required field 'engine_class' → ManifestSchemaError → exit 2.
    bad_yaml = """\
version: 1
workflow:
  workflow_id: "wf-bad"
  workload_class: "software-engineering"
  persona_tier: "solo-developer"
  topology_pattern: "evaluator-optimizer"
default_model_binding:
  provider: "anthropic"
  model: "claude-opus-4-7"
steps:
  - step_id: "s1"
    step_kind: "inference-step"
    step_payload: {}
"""
    manifest = _write_yaml(tmp_path, body=bad_yaml)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_MANIFEST_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-CLI-MANIFEST-" in result.stderr


# ---------------------------------------------------------------------------
# AC #8 (was #7) — Config load error → exit 3
# ---------------------------------------------------------------------------


def test_ac8_config_load_error_exits_three(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load(raises=RuntimeConfigLoadError("synthetic test failure", source="test"))
    mock_api_run()
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_CONFIG_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-CLI-CONFIG-LOAD" in result.stderr


# ---------------------------------------------------------------------------
# AC #9 (was #8) — Bootstrap error → exit 4
# ---------------------------------------------------------------------------


def test_ac9_bootstrap_error_exits_four(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    from harness_runtime.bootstrap import BootstrapFailure
    from harness_runtime.types import BootstrapStage

    mock_config_load()
    mock_api_run(
        raises=BootstrapFailure(
            BootstrapStage.LOOP_INIT,
            RuntimeError("synthetic bootstrap failure"),
        )
    )
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_BOOTSTRAP_ERROR, result.stdout + result.stderr
    assert "RT-FAIL-BOOTSTRAP" in result.stderr


# ---------------------------------------------------------------------------
# AC #10 (was #9) — SIGINT → DRAINED → exit 1 (structural via mock)
# ---------------------------------------------------------------------------


def test_ac10_sigint_drain_surface_returns_drained_status(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    # api.run() is the contract surface for drain propagation: bootstrap
    # installs signal handlers at stage 7 (per drain.py); when SIGINT fires
    # mid-workflow, ctx.drained_flag is set and the CP loop returns a
    # RunResult with status='drained'. The CLI body must map that to
    # exit 1. We validate the CLI-side mapping here; the bootstrap-side
    # signal-handler install is covered at test_bootstrap.
    mock_config_load()
    mock_api_run(result=_run_result(status="drained"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest)])
    assert result.exit_code == EXIT_WORKFLOW_FAIL, result.stdout + result.stderr
    out = _plain(result.stdout)
    assert "drained" in out


# ---------------------------------------------------------------------------
# AC #11 (was #10) — --provider / --model override default_model_binding
# ---------------------------------------------------------------------------


def test_ac11_provider_and_model_flags_override_default_model_binding(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="completed"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(
        app,
        ["run", str(manifest), "--provider", "openai", "--model", "gpt-4o"],
    )
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    workflow = mock_api_run.captured["workflow"]  # type: ignore[attr-defined]
    assert workflow.default_model_binding.provider == "openai"
    assert workflow.default_model_binding.model == "gpt-4o"


# ---------------------------------------------------------------------------
# AC #12 (was #11) — --config <path> threads through to RuntimeConfigSource
# ---------------------------------------------------------------------------


def test_ac12_config_flag_threads_path_to_config_source(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="completed"))
    manifest = _write_yaml(tmp_path)
    config_path = tmp_path / "harness.toml"
    config_path.write_text("# placeholder\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["run", str(manifest), "--config", str(config_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    captured = mock_config_load.captured  # type: ignore[attr-defined]
    assert captured["config_file"] == config_path


# ---------------------------------------------------------------------------
# Adjacent — --tenant-id propagates to cli_overrides
# ---------------------------------------------------------------------------


def test_tenant_id_flag_propagates_to_cli_overrides(
    tmp_path: Path,
    mock_config_load: Callable[..., None],
    mock_api_run: Callable[..., None],
) -> None:
    mock_config_load()
    mock_api_run(result=_run_result(status="completed"))
    manifest = _write_yaml(tmp_path)
    result = runner.invoke(app, ["run", str(manifest), "--tenant-id", "tenant-x"])
    assert result.exit_code == EXIT_SUCCESS, result.stdout + result.stderr
    captured = mock_config_load.captured  # type: ignore[attr-defined]
    assert captured["cli_overrides"].get("tenant_id") == "tenant-x"
