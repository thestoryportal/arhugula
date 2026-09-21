"""Shipped defaults must join bootstrap paths to the native inspector."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.jsonl_event_ledger_lifecycle import initialize_jsonl_event_ledger
from harness_runtime.admin.inspect import main as inspect_main
from harness_runtime.lifecycle.path_registry import materialize_path_registry
from harness_runtime.types import PathBindingConfig


@pytest.mark.parametrize(
    "workload", [WorkloadClass.PIPELINE_AUTOMATION, WorkloadClass.SOFTWARE_ENGINEERING]
)
def test_shipped_paths_support_workflow_and_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workload: WorkloadClass
) -> None:
    root = Path(__file__).resolve().parents[2]
    template = (
        (root / "harness.toml.example")
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
