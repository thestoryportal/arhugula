"""Shipped defaults must join bootstrap paths to the native inspector."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.jsonl_event_ledger_lifecycle import (
    JsonlLedgerHandle,
    initialize_jsonl_event_ledger,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteKey, append_ledger_entry
from harness_runtime.admin.inspect import main as inspect_main
from harness_runtime.lifecycle.path_registry import materialize_path_registry
from harness_runtime.types import PathBindingConfig

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_OVERLAY = ROOT / "examples/topology-parallelization-ollama.runtime-overlay.toml.example"


def _load_tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _initialized_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for template in ("harness.toml.example", ".env.example"):
        shutil.copyfile(ROOT / template, workspace / template)
    _load_tool("portable_init").initialize(workspace)
    return workspace


def _open_ledger(config_path: Path, workload: WorkloadClass) -> JsonlLedgerHandle:
    entries = tomllib.loads(config_path.read_text())["runtime"]["path_bindings"]["raw_entries"]
    registry = materialize_path_registry(
        PathBindingConfig(raw_entries=tuple(entries)),
        workflow_class=workload,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    return initialize_jsonl_event_ledger(
        registry.resolver, workload, DeploymentSurface.LOCAL_DEVELOPMENT
    )


def _apply_research_overlay(workspace: Path) -> Path:
    return _load_tool("apply_example_runtime_overlay").apply_overlay(
        base_config=workspace / "harness.toml",
        overlay=RESEARCH_OVERLAY,
        repo_root=workspace,
        output=workspace / "research-harness.toml",
    )


@pytest.mark.parametrize(
    "workload", [WorkloadClass.PIPELINE_AUTOMATION, WorkloadClass.SOFTWARE_ENGINEERING]
)
def test_shipped_paths_support_workflow_and_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workload: WorkloadClass
) -> None:
    template = (
        (ROOT / "harness.toml.example")
        .read_text()
        .replace("/absolute/path/to/your/workspace", str(tmp_path))
    )
    entries = tomllib.loads(template)["runtime"]["path_bindings"]["raw_entries"]
    registry = materialize_path_registry(
        PathBindingConfig(raw_entries=tuple(entries)),
        workflow_class=workload,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    ledger = initialize_jsonl_event_ledger(
        registry.resolver, workload, DeploymentSurface.LOCAL_DEVELOPMENT
    )
    assert ledger.canonical_path == tmp_path / ".harness/state.jsonl"
    assert ledger.canonical_path.is_file()
    monkeypatch.chdir(tmp_path)
    assert inspect_main(["--json"]) == 0


def test_research_overlay_initializes_regular_ledger_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _initialized_workspace(tmp_path)
    ledger = _open_ledger(_apply_research_overlay(workspace), WorkloadClass.RESEARCH)
    assert ledger.canonical_path == workspace / ".harness/state.jsonl"
    assert ledger.canonical_path.is_file()
    assert ledger.entry_count == 0
    monkeypatch.chdir(workspace)
    assert inspect_main(["--json"]) == 0


def test_research_overlay_preserves_existing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _initialized_workspace(tmp_path)
    handle = _open_ledger(workspace / "harness.toml", WorkloadClass.PIPELINE_AUTOMATION)
    actor = Actor(actor_class=ActorClass.AGENT, actor_id="harness-runtime")
    for i in range(2):
        payload = EntryPayload(
            action_id=Identifier(f"action-{i}"),
            idempotency_key=Identifier(f"idem-{i}"),
            actor=actor,
            timestamp=datetime(2026, 9, 21, 12, 0, i, tzinfo=UTC),
        )
        write_key = WriteKey(
            thread_id=Identifier(f"thread-{i}"),
            step_id=Identifier(f"step-{i}"),
            idempotency_key=Identifier(f"idem-{i}"),
        )
        append_ledger_entry(handle, payload, write_key)
        handle = JsonlLedgerHandle(
            canonical_path=handle.canonical_path, exists=True, entry_count=i + 1
        )
    existing = handle.canonical_path.read_bytes()

    ledger = _open_ledger(_apply_research_overlay(workspace), WorkloadClass.RESEARCH)

    assert ledger.canonical_path == handle.canonical_path == workspace / ".harness/state.jsonl"
    assert ledger.canonical_path.is_file()
    assert ledger.entry_count == 2
    assert ledger.canonical_path.read_bytes() == existing
    monkeypatch.chdir(workspace)
    capsys.readouterr()
    assert inspect_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total_entries"] == 2
