"""Regression: the shipped self-hosted-local template and documented wrappers.

``deploy/self-hosted-local/README.md`` tells operators to copy
``harness.selfhosted.local.example.toml`` and run three live e2e ``just``
wrappers. The template's path bindings must match the portable scaffold
(``skills/`` and a ``.harness/`` STATE_LEDGER directory the runtime appends
``state.jsonl`` to), and each documented wrapper must forward to the shipped
script with its positional config argument. No live service is contacted.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.jsonl_event_ledger_lifecycle import initialize_jsonl_event_ledger
from harness_is.path_class_registry import PathClass
from harness_runtime.lifecycle.path_registry import materialize_path_registry
from harness_runtime.types import PathBindingConfig

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "self-hosted-local" / "harness.selfhosted.local.example.toml"
PLACEHOLDER = "/absolute/path/to/arhugula-v2"

LIVE_E2E_WRAPPERS = {
    "r420-self-hosted-live-e2e": "tools/r420_self_hosted_live_e2e.py",
    "r430-tail-keep-live-e2e": "tools/r430_tail_keep_collector_live_e2e.py",
    "r500-multitenant-live-e2e": "tools/r500_multitenant_selfhosted_live_e2e.py",
}
# One real optional flag per script, with a space in its value to prove quoting.
OPTIONAL_ARGS = {
    "r420-self-hosted-live-e2e": ["--workflow", "examples/dir with space/wf.toml"],
    "r430-tail-keep-live-e2e": ["--tempo-url", "http://127.0.0.1:3200"],
    "r500-multitenant-live-e2e": ["--tempo-url", "http://127.0.0.1:3200"],
}


def _raw_entries(workspace: Path) -> list[dict[str, str]]:
    text = TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, str(workspace))
    return tomllib.loads(text)["runtime"]["path_bindings"]["raw_entries"]


@pytest.mark.parametrize(
    "workload", [WorkloadClass.PIPELINE_AUTOMATION, WorkloadClass.SOFTWARE_ENGINEERING]
)
def test_selfhosted_template_paths_match_portable_scaffold(
    tmp_path: Path, workload: WorkloadClass
) -> None:
    registry = materialize_path_registry(
        PathBindingConfig(raw_entries=tuple(_raw_entries(tmp_path))),
        workflow_class=workload,
        deployment_surface=DeploymentSurface.SELF_HOSTED_SERVER,
    )
    assert registry.resolved_paths[PathClass.SKILLS] == tmp_path / "skills"
    assert registry.resolved_paths[PathClass.STATE_LEDGER] == tmp_path / ".harness"
    assert registry.resolved_paths[PathClass.STATE_LEDGER].is_dir()

    ledger = initialize_jsonl_event_ledger(
        registry.resolver, workload, DeploymentSurface.SELF_HOSTED_SERVER
    )
    assert ledger.canonical_path == tmp_path / ".harness" / "state.jsonl"
    assert ledger.canonical_path.is_file()


def test_readme_documents_exactly_the_wrapped_live_e2e_commands() -> None:
    readme = (ROOT / "deploy" / "self-hosted-local" / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"just (r\d+-[a-z0-9-]+-live-e2e) ", readme))
    assert documented == set(LIVE_E2E_WRAPPERS)
    for script in LIVE_E2E_WRAPPERS.values():
        assert (ROOT / script).is_file()


@pytest.mark.parametrize(("recipe", "script"), sorted(LIVE_E2E_WRAPPERS.items()))
def test_justfile_declares_live_e2e_wrapper(recipe: str, script: str) -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(recipe)} config \*args:\n((?:    .*\n)+)", justfile, re.M)
    assert match is not None, f"missing just recipe {recipe}"
    assert match.group(1).strip() == f'uv run python {script} "$@"'


@pytest.mark.skipif(shutil.which("just") is None, reason="just is not installed")
@pytest.mark.parametrize(("recipe", "script"), sorted(LIVE_E2E_WRAPPERS.items()))
def test_live_e2e_wrapper_forwards_quoted_arguments(
    tmp_path: Path, recipe: str, script: str
) -> None:
    # A stub `uv` records its argv instead of launching the live e2e.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv.json"
    stub = bin_dir / "uv"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['STUB_UV_RECORD'], 'w') as fh:\n"
        "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, fh)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_UV_RECORD": str(record),
    }
    config = str(tmp_path / "dir with space" / "harness.selfhosted.local.toml")
    subprocess.run(
        ["just", "--justfile", str(ROOT / "justfile"), recipe, config, *OPTIONAL_ARGS[recipe]],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    recorded = json.loads(record.read_text(encoding="utf-8"))
    assert recorded["argv"] == ["run", "python", script, config, *OPTIONAL_ARGS[recipe]]
    assert Path(recorded["cwd"]) == ROOT


def test_packaging_gate_requires_live_e2e_wrappers() -> None:
    spec = importlib.util.spec_from_file_location(
        "q4_packaging_gate_under_test", ROOT / "tools" / "q4_packaging_gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert set(LIVE_E2E_WRAPPERS) <= set(module.READINESS_RECIPES)
    assert module._readiness_recipe_check(ROOT).ok
