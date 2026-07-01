"""Tests for U-RT-104 — WorkflowManifestLoader (spec v1.36 §14.19 / plan v2.32 §1.4).

Acceptance criteria mapped per plan v2.32 §1.2 (AC #11 reframed to enum-validity
coverage extension of AC #9 per Reading β):

  AC #1  → test_yaml_extension_dispatches_to_strictyaml
  AC #2  → test_toml_extension_dispatches_to_tomllib
  AC #3  → test_unsupported_extension_raises_format_error
  AC #4  → test_version_absent_or_not_1_raises_version_error
  AC #5  → test_syntax_error_raises_parse_error_with_file_path
  AC #6  → test_unknown_top_level_field_raises_schema_error
  AC #7  → test_unknown_nested_field_raises_schema_error
  AC #8  → test_required_field_missing_raises_schema_error
  AC #9  → test_invalid_enum_value_raises_enum_value_error (+ AC #11 coverage)
  AC #10 → test_duplicate_step_id_raises_step_id_collision_error
  AC #11 → REFRAMED at v2.32 — merges into AC #9 enum-validity coverage
  AC #12 → test_topology_pattern_not_admissible_for_workload_raises_admissibility_error
  AC #13 → test_eager_validation_all_checks_at_load_time
  AC #14 → test_load_is_idempotent
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core.persona_tier import PersonaTier
from harness_core.workload_class import WorkloadClass
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_manifest_entry import (
    FanoutTimeoutDisposition,
    WorkflowManifestEntry,
)
from harness_runtime.api import WorkflowObject
from harness_runtime.lifecycle.workflow_manifest_loader import (
    LoadedWorkflow,
    ManifestEnumValueError,
    ManifestParseError,
    ManifestSchemaError,
    ManifestStepIDCollisionError,
    UnsupportedManifestFormatError,
    UnsupportedManifestVersionError,
    WorkflowManifest,
    WorkflowManifestLoader,
    WorkflowManifestLoadError,
)

# ---------------------------------------------------------------------------
# Fixture manifests (5 valid; per plan §1.4 Tests-line "5 fixture-driven valid")
# ---------------------------------------------------------------------------


_VALID_YAML_MINIMUM = """\
version: 1
workflow:
  workflow_id: "wf-min"
  workload_class: "software-engineering"
  persona_tier: "solo-developer"
  engine_class: "pure-pattern-no-engine"
  topology_pattern: "evaluator-optimizer"
default_model_binding:
  provider: "anthropic"
  model: "claude-opus-4-7"
steps:
  - step_id: "s1"
    step_kind: "inference-step"
    step_payload: {}
"""

_VALID_TOML_MINIMUM = """\
version = 1

[workflow]
workflow_id = "wf-min-toml"
workload_class = "software-engineering"
persona_tier = "solo-developer"
engine_class = "pure-pattern-no-engine"
topology_pattern = "evaluator-optimizer"

[default_model_binding]
provider = "anthropic"
model = "claude-opus-4-7"

[[steps]]
step_id = "s1"
step_kind = "inference-step"
step_payload = {}
"""

_VALID_TOML_FULL_OPTIONAL = """\
version = 1

[workflow]
workflow_id = "wf-full"
workload_class = "software-engineering"
persona_tier = "team-binding"
engine_class = "save-point-checkpoint"
topology_pattern = "evaluator-optimizer"
entry_version = 2
default_gate_level = "deny"

[default_model_binding]
provider = "openai"
model = "gpt-4o"

[[steps]]
step_id = "s1"
step_kind = "inference-step"
step_payload = {}

[[steps]]
step_id = "s2"
step_kind = "tool-step"
step_payload = { tool_name = "search" }
"""

_VALID_YAML_MULTI_STEP = """\
version: 1
workflow:
  workflow_id: "wf-multi"
  workload_class: "software-engineering"
  persona_tier: "solo-developer"
  engine_class: "pure-pattern-no-engine"
  topology_pattern: "evaluator-optimizer"
default_model_binding:
  provider: "ollama"
  model: "llama3.3"
steps:
  - step_id: "step-a"
    step_kind: "inference-step"
    step_payload: {}
  - step_id: "step-b"
    step_kind: "declarative-step"
    step_payload: {}
  - step_id: "step-c"
    step_kind: "HITL-step"
    step_payload: {}
"""

_VALID_TOML_DECLARATIVE = """\
version = 1

[workflow]
workflow_id = "wf-declarative"
workload_class = "software-engineering"
persona_tier = "solo-developer"
engine_class = "pure-pattern-no-engine"
topology_pattern = "evaluator-optimizer"

[default_model_binding]
provider = "anthropic"
model = "claude-haiku-4-5-20251001"

[[steps]]
step_id = "only-step"
step_kind = "declarative-step"
step_payload = {}
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 5 fixture-driven valid manifests (per plan §1.4 Tests-line)
# ---------------------------------------------------------------------------


def test_fixture_valid_yaml_minimum_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, "min.yaml", _VALID_YAML_MINIMUM)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.workflow.workflow_id == "wf-min"
    assert len(manifest.steps) == 1


def test_fixture_valid_toml_minimum_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, "min.toml", _VALID_TOML_MINIMUM)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.workflow.workflow_id == "wf-min-toml"


def test_fixture_valid_toml_full_optional_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, "full.toml", _VALID_TOML_FULL_OPTIONAL)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.workflow.entry_version == 2
    assert manifest.workflow.default_gate_level is not None
    assert len(manifest.steps) == 2


def test_fixture_valid_yaml_multi_step_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, "multi.yaml", _VALID_YAML_MULTI_STEP)
    manifest = WorkflowManifestLoader.load(path)
    assert [s.step_id for s in manifest.steps] == ["step-a", "step-b", "step-c"]


def test_fixture_valid_toml_declarative_only_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, "decl.toml", _VALID_TOML_DECLARATIVE)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.steps[0].step_kind.value == "declarative-step"


# ---------------------------------------------------------------------------
# AC #1 — .yaml / .yml dispatches to strictyaml
# ---------------------------------------------------------------------------


def test_yaml_extension_dispatches_to_strictyaml(tmp_path: Path) -> None:
    yaml_path = _write(tmp_path, "wf.yaml", _VALID_YAML_MINIMUM)
    yml_path = _write(tmp_path, "wf.yml", _VALID_YAML_MINIMUM.replace("wf-min", "wf-yml"))
    assert WorkflowManifestLoader.load(yaml_path).workflow.workflow_id == "wf-min"
    assert WorkflowManifestLoader.load(yml_path).workflow.workflow_id == "wf-yml"


# AC #2 — .toml dispatches to tomllib
def test_toml_extension_dispatches_to_tomllib(tmp_path: Path) -> None:
    path = _write(tmp_path, "wf.toml", _VALID_TOML_MINIMUM)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.version == 1


# AC #3 — other extensions raise UnsupportedManifestFormatError
def test_unsupported_extension_raises_format_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "wf.json", '{"version": 1}')
    with pytest.raises(UnsupportedManifestFormatError) as exc:
        WorkflowManifestLoader.load(path)
    assert exc.value.FAIL_CLASS == "RT-FAIL-CLI-MANIFEST-FORMAT-UNSUPPORTED"
    # All typed exceptions inherit from WorkflowManifestLoadError per §14.19.2
    assert isinstance(exc.value, WorkflowManifestLoadError)


# AC #4 — version absent OR != 1 raises UnsupportedManifestVersionError
def test_version_absent_raises_version_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM.replace("version = 1\n\n", "")
    path = _write(tmp_path, "no-version.toml", body)
    with pytest.raises(UnsupportedManifestVersionError):
        WorkflowManifestLoader.load(path)


def test_version_not_1_raises_version_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM.replace("version = 1", "version = 2")
    path = _write(tmp_path, "v2.toml", body)
    with pytest.raises(UnsupportedManifestVersionError) as exc:
        WorkflowManifestLoader.load(path)
    assert "2" in str(exc.value)


# AC #5 — syntax error → ManifestParseError with file path
def test_yaml_syntax_error_raises_parse_error_with_file_path(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.yaml", "version: 1\n  bad: indent\n   really: bad\n")
    # strictyaml does accept some loose YAML; force a real syntax error
    path.write_text("version: 1\nworkflow: [unclosed\n", encoding="utf-8")
    with pytest.raises(ManifestParseError) as exc:
        WorkflowManifestLoader.load(path)
    assert str(path) in str(exc.value)


def test_toml_syntax_error_raises_parse_error_with_file_path(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken.toml", "[workflow\nthis is not toml")
    with pytest.raises(ManifestParseError) as exc:
        WorkflowManifestLoader.load(path)
    assert str(path) in str(exc.value)


# AC #6 — unknown top-level field → ManifestSchemaError (closed schema Q-B4=a)
def test_unknown_top_level_field_raises_schema_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM + '\nunknown_top_level = "nope"\n'
    path = _write(tmp_path, "extra.toml", body)
    with pytest.raises(ManifestSchemaError) as exc:
        WorkflowManifestLoader.load(path)
    assert exc.value.FAIL_CLASS == "RT-FAIL-CLI-MANIFEST-SCHEMA"


# AC #7 — unknown nested field → ManifestSchemaError
def test_unknown_nested_field_in_workflow_raises_schema_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM.replace(
        'topology_pattern = "evaluator-optimizer"',
        'topology_pattern = "evaluator-optimizer"\nunknown_inner_field = "boom"',
    )
    path = _write(tmp_path, "inner-extra.toml", body)
    with pytest.raises(ManifestSchemaError):
        WorkflowManifestLoader.load(path)


# AC #8 — required field missing → ManifestSchemaError naming the field
def test_required_field_missing_raises_schema_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM.replace('workflow_id = "wf-min-toml"\n', "")
    path = _write(tmp_path, "missing.toml", body)
    with pytest.raises(ManifestSchemaError) as exc:
        WorkflowManifestLoader.load(path)
    assert "workflow_id" in str(exc.value)


# AC #9 — invalid enum value → ManifestEnumValueError (covers AC #11 too)
def test_invalid_enum_value_raises_enum_value_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM.replace(
        'workload_class = "software-engineering"',
        'workload_class = "WRONG_VALUE"',
    )
    path = _write(tmp_path, "bad-enum.toml", body)
    with pytest.raises(ManifestEnumValueError) as exc:
        WorkflowManifestLoader.load(path)
    assert exc.value.FAIL_CLASS == "RT-FAIL-CLI-MANIFEST-ENUM-VALUE"
    assert "workload_class" in str(exc.value)


# AC #11 REFRAMED — engine_class invalid enum value → ManifestEnumValueError
# (per plan v2.32 §1.2 Reading β: load-time scope is enum-validity only)
def test_engine_class_invalid_enum_value_raises_manifest_enum_value_error(
    tmp_path: Path,
) -> None:
    body = _VALID_TOML_MINIMUM.replace(
        'engine_class = "pure-pattern-no-engine"',
        'engine_class = "NOT-A-VALID-ENGINE-CLASS"',
    )
    path = _write(tmp_path, "bad-engine.toml", body)
    with pytest.raises(ManifestEnumValueError):
        WorkflowManifestLoader.load(path)


# AC #10 — duplicate step_id → ManifestStepIDCollisionError naming the collision
def test_duplicate_step_id_raises_step_id_collision_error(tmp_path: Path) -> None:
    body = _VALID_TOML_MINIMUM + (
        '\n[[steps]]\nstep_id = "s1"\nstep_kind = "tool-step"\nstep_payload = {}\n'
    )
    path = _write(tmp_path, "dup.toml", body)
    with pytest.raises(ManifestStepIDCollisionError) as exc:
        WorkflowManifestLoader.load(path)
    assert "s1" in str(exc.value)


# AC #12 (v1.38 Reading A) — topology_pattern admissibility DEFERRED to runtime.
# Per `.harness/class_1_fork_topology_admissibility_check_load_time_vs_runtime_asymmetry.md`
# operator-ratified Reading A (PR #80): the loader no longer enforces the
# `is_topology_permitted_for_workload` predicate. Runtime authority is the
# sub-agent-dispatch site (`sub_agent_dispatch.py:585`); single-step workflows
# that never dispatch sub-agents pass through unchallenged — by design.
# Pattern-consistent with v1.36 Reading β (engine_class admissibility loader →
# U-RT-106). Test below asserts the previously-rejected combo now LOADS.
def test_topology_pattern_admissibility_deferred_to_runtime_per_v1_38_reading_a(
    tmp_path: Path,
) -> None:
    # SOFTWARE_ENGINEERING's C-CP-11 §11.1 permitted set is
    # {EVALUATOR_OPTIMIZER, ORCHESTRATOR_WORKERS}; SINGLE_THREADED_LINEAR is
    # NOT in that set per the matrix design intent — BUT MVP-scope materializes
    # only SINGLE_THREADED_LINEAR per probe finding #5. v1.38 Reading A defers
    # the check to runtime so SE workflows at MVP scope can run.
    body = _VALID_TOML_MINIMUM.replace(
        'topology_pattern = "evaluator-optimizer"',
        'topology_pattern = "single-threaded-linear"',
    )
    path = _write(tmp_path, "se-single-threaded.toml", body)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.workflow.workload_class.value == "software-engineering"
    assert manifest.workflow.topology_pattern.value == "single-threaded-linear"


def test_topology_pattern_admissibility_deferred_for_research_workload(
    tmp_path: Path,
) -> None:
    # Sibling assertion: RESEARCH + SINGLE_THREADED_LINEAR was previously
    # rejected at load (matrix permits only ORCHESTRATOR_WORKERS for RESEARCH).
    # v1.38 Reading A: loader accepts; runtime is the authority.
    body = _VALID_TOML_MINIMUM.replace(
        'workload_class = "software-engineering"', 'workload_class = "research"'
    ).replace(
        'topology_pattern = "evaluator-optimizer"',
        'topology_pattern = "single-threaded-linear"',
    )
    path = _write(tmp_path, "research-single-threaded.toml", body)
    manifest = WorkflowManifestLoader.load(path)
    assert manifest.workflow.workload_class.value == "research"
    assert manifest.workflow.topology_pattern.value == "single-threaded-linear"


# AC #13 — eager validation (all checks at .load() time)
def test_eager_validation_all_checks_at_load_time(tmp_path: Path) -> None:
    # If any check were deferred, .load() would return successfully and the
    # error would surface later. Verify a manifest with multiple defects
    # raises immediately on the FIRST applicable check.
    body = _VALID_TOML_MINIMUM.replace(
        'workload_class = "software-engineering"', 'workload_class = "INVALID"'
    )
    path = _write(tmp_path, "eager.toml", body)
    with pytest.raises(WorkflowManifestLoadError):
        WorkflowManifestLoader.load(path)


# AC #14 — idempotency: repeated load returns equal carriers
def test_load_is_idempotent(tmp_path: Path) -> None:
    path = _write(tmp_path, "idem.yaml", _VALID_YAML_MINIMUM)
    a = WorkflowManifestLoader.load(path)
    b = WorkflowManifestLoader.load(path)
    assert a == b
    assert isinstance(a, WorkflowManifest)


# ===========================================================================
# U-RT-105 — LoadedWorkflow projection tests (plan v2.31 §1.5)
# ===========================================================================


_WME_REQUIRED_YAML_FRAGMENT = """\
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

_WME_REQUIRED_TOML_FRAGMENT = """
layer_budgets = []
hitl_placements = []
per_step_overrides = {}
fallback_chain = { primary = { provider = "anthropic", model = "claude-opus-4-7", family = "anthropic" }, same_family = [], cross_family = [] }
"""


def _yaml_with_wme(extra_workflow_lines: str = "") -> str:
    """Build a YAML manifest body that supplies the full WME field-set."""
    return (
        "version: 1\n"
        "workflow:\n"
        '  workflow_id: "wf-projected"\n'
        '  workload_class: "software-engineering"\n'
        '  persona_tier: "solo-developer"\n'
        '  engine_class: "pure-pattern-no-engine"\n'
        '  topology_pattern: "evaluator-optimizer"\n'
        + extra_workflow_lines
        + _WME_REQUIRED_YAML_FRAGMENT
        + "default_model_binding:\n"
        + '  provider: "anthropic"\n'
        + '  model: "claude-opus-4-7"\n'
        + "steps:\n"
        + '  - step_id: "s1"\n'
        + '    step_kind: "inference-step"\n'
        + "    step_payload: {}\n"
    )


def _toml_with_wme(extra_workflow_lines: str = "") -> str:
    return (
        "version = 1\n\n"
        "[workflow]\n"
        'workflow_id = "wf-projected"\n'
        'workload_class = "software-engineering"\n'
        'persona_tier = "solo-developer"\n'
        'engine_class = "pure-pattern-no-engine"\n'
        'topology_pattern = "evaluator-optimizer"\n'
        + extra_workflow_lines
        + _WME_REQUIRED_TOML_FRAGMENT
        + "\n[default_model_binding]\n"
        + 'provider = "anthropic"\n'
        + 'model = "claude-opus-4-7"\n\n'
        + "[[steps]]\n"
        + 'step_id = "s1"\n'
        + 'step_kind = "inference-step"\n'
        + "step_payload = {}\n"
    )


# AC #1 — minimum-required manifest produces WorkflowObject Protocol value
def test_minimum_required_manifest_produces_workflow_object(tmp_path: Path) -> None:
    path = _write(tmp_path, "min-wme.yaml", _yaml_with_wme())
    workflow = WorkflowManifestLoader.load_workflow(path)
    assert isinstance(workflow, LoadedWorkflow)
    assert isinstance(workflow, WorkflowObject)  # Protocol-conformance
    assert workflow.workflow_id == "wf-projected"
    assert workflow.workload_class is WorkloadClass.SOFTWARE_ENGINEERING
    assert workflow.manifest_entry.engine_class is EngineClass.PURE_PATTERN_NO_ENGINE
    assert workflow.manifest_entry.topology_pattern is TopologyPattern.EVALUATOR_OPTIMIZER
    assert workflow.default_model_binding.provider == "anthropic"
    assert len(workflow.steps) == 1


# AC #2 — full-optional manifest matches manual WME construction
def test_full_optional_manifest_matches_manual_construction(tmp_path: Path) -> None:
    extra = (
        '  entry_version: 2\n  default_gate_level: "deny"\n'
        '  fanout_timeout_disposition: "recover-as-terminal"\n'
    )
    path = _write(tmp_path, "full-wme.yaml", _yaml_with_wme(extra))
    workflow = WorkflowManifestLoader.load_workflow(path)
    manual_wme = WorkflowManifestEntry(
        workflow_id="wf-projected",
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.EVALUATOR_OPTIMIZER,
        layer_budgets=(),
        fallback_chain=FallbackChain(
            primary=ProviderCandidate(
                provider="anthropic",
                model="claude-opus-4-7",
                family=ProviderFamily.ANTHROPIC,
            ),
            same_family=(),
            cross_family=(),
        ),
        hitl_placements=(),
        per_step_overrides={},
        entry_version=2,
        default_gate_level=__import__(
            "harness_cp.gate_level_rule", fromlist=["GateLevel"]
        ).GateLevel.DENY,
        fanout_timeout_disposition=FanoutTimeoutDisposition.RECOVER_AS_TERMINAL,
    )
    assert workflow.manifest_entry == manual_wme
    # B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY (CP spec v1.63 §1) — the disposition is plumbed
    # through the manifest loader (the §14.19.4 byte-exact-projection invariant): an operator
    # CAN set it in the manifest file (not a built-but-vacuous schema-only field).
    assert (
        workflow.manifest_entry.fanout_timeout_disposition
        is FanoutTimeoutDisposition.RECOVER_AS_TERMINAL
    )


# AC #3 — Pydantic-default discipline (§14.19.4 #6): absent optional field
# inherits the carrier's declared default, not a loader-supplied value
def test_optional_field_absent_uses_pydantic_carrier_default(tmp_path: Path) -> None:
    path = _write(tmp_path, "default.yaml", _yaml_with_wme())
    workflow = WorkflowManifestLoader.load_workflow(path)
    # `entry_version` not present in manifest → WME default of 1 applies.
    assert workflow.manifest_entry.entry_version == 1
    # `default_gate_level` not present → WME default of None applies.
    assert workflow.manifest_entry.default_gate_level is None
    # `sub_agent_briefs` not present → WME default of None applies.
    assert workflow.manifest_entry.sub_agent_briefs is None
    # `fanout_timeout_disposition` not present → WME default FAIL_CLOSED applies (v1.55
    # byte-identical; B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY, CP spec v1.63 §1).
    assert (
        workflow.manifest_entry.fanout_timeout_disposition is FanoutTimeoutDisposition.FAIL_CLOSED
    )


# AC #4 — YAML↔TOML round-trip invariant (§14.19.4 #8)
def test_yaml_toml_equivalent_inputs_produce_equivalent_workflow(
    tmp_path: Path,
) -> None:
    yaml_path = _write(tmp_path, "equivalent.yaml", _yaml_with_wme())
    toml_path = _write(tmp_path, "equivalent.toml", _toml_with_wme())
    yaml_workflow = WorkflowManifestLoader.load_workflow(yaml_path)
    toml_workflow = WorkflowManifestLoader.load_workflow(toml_path)
    assert yaml_workflow == toml_workflow


# AC #5 — step_payload JSON-serializability (§14.19.4 #9)
def test_non_json_serializable_step_payload_raises_schema_error(
    tmp_path: Path,
) -> None:
    # TOML offset-datetime → tomllib produces a `datetime.datetime`, which
    # is NOT json.dumps serializable. Fixture verifies eager catch.
    body = (
        "version = 1\n\n"
        "[workflow]\n"
        'workflow_id = "wf-bad-payload"\n'
        'workload_class = "software-engineering"\n'
        'persona_tier = "solo-developer"\n'
        'engine_class = "pure-pattern-no-engine"\n'
        'topology_pattern = "evaluator-optimizer"\n'
        + _WME_REQUIRED_TOML_FRAGMENT
        + "\n[default_model_binding]\n"
        + 'provider = "anthropic"\n'
        + 'model = "claude-opus-4-7"\n\n'
        + "[[steps]]\n"
        + 'step_id = "s1"\n'
        + 'step_kind = "inference-step"\n'
        + "step_payload = { scheduled_at = 2026-05-28T10:00:00Z }\n"
    )
    path = _write(tmp_path, "bad-payload.toml", body)
    with pytest.raises(ManifestSchemaError) as exc:
        WorkflowManifestLoader.load_workflow(path)
    assert "JSON-serializable" in str(exc.value)


# AC #6 — idempotency: repeated load_workflow returns equal LoadedWorkflow
def test_load_workflow_is_idempotent(tmp_path: Path) -> None:
    path = _write(tmp_path, "idem-projected.yaml", _yaml_with_wme())
    a = WorkflowManifestLoader.load_workflow(path)
    b = WorkflowManifestLoader.load_workflow(path)
    assert a == b
    assert isinstance(a, LoadedWorkflow)


# v1.39 §14.19 Reading A — pyyaml StrictSafeLoader preserves native scalar types
# Closes use-the-product probe finding #16/#17 (PR #79): pre-v1.39 strictyaml.dirty_load
# stringified every YAML scalar; max_tokens: 8 arrived as "8" at LLM SDK.
def test_yaml_native_scalar_typing_per_v1_39_reading_a(tmp_path: Path) -> None:
    body = (
        "version: 1\n"
        "workflow:\n"
        '  workflow_id: "wf-native-scalars"\n'
        '  workload_class: "pipeline-automation"\n'
        '  persona_tier: "solo-developer"\n'
        '  engine_class: "pure-pattern-no-engine"\n'
        '  topology_pattern: "single-threaded-linear"\n'
        "  layer_budgets: []\n"
        "  fallback_chain:\n"
        "    primary:\n"
        '      provider: "anthropic"\n'
        '      model: "claude-haiku-4-5"\n'
        '      family: "anthropic"\n'
        "    same_family: []\n"
        "    cross_family: []\n"
        "  hitl_placements: []\n"
        "  per_step_overrides: {}\n"
        "default_model_binding:\n"
        '  provider: "anthropic"\n'
        '  model: "claude-haiku-4-5"\n'
        "steps:\n"
        '  - step_id: "s1"\n'
        '    step_kind: "inference-step"\n'
        "    step_payload:\n"
        "      max_tokens: 8\n"
        "      temperature: 0.7\n"
        "      stop_sequences:\n"
        '        - "END"\n'
    )
    path = _write(tmp_path, "native.yaml", body)
    wf = WorkflowManifestLoader.load_workflow(path)
    payload = wf.steps[0].step_payload
    assert payload["max_tokens"] == 8
    assert isinstance(payload["max_tokens"], int)
    assert payload["temperature"] == 0.7
    assert isinstance(payload["temperature"], float)


def test_yaml_duplicate_key_rejected_per_strict_safe_loader(tmp_path: Path) -> None:
    body = (
        "version: 1\n"
        "workflow:\n"
        '  workflow_id: "wf-dup"\n'
        '  workflow_id: "wf-dup-2"\n'  # duplicate key
    )
    path = _write(tmp_path, "dup.yaml", body)
    with pytest.raises(WorkflowManifestLoadError) as exc:
        WorkflowManifestLoader.load(path)
    assert "duplicate" in str(exc.value).lower()


def test_yaml_anchor_alias_rejected_per_strict_safe_loader(tmp_path: Path) -> None:
    body = 'version: 1\nworkflow: &anchor\n  workflow_id: "wf-anchor"\nalias: *anchor\n'
    path = _write(tmp_path, "anchor.yaml", body)
    with pytest.raises(WorkflowManifestLoadError) as exc:
        WorkflowManifestLoader.load(path)
    assert "anchor" in str(exc.value).lower() or "alias" in str(exc.value).lower()


def test_yaml_version_as_string_rejected_per_v1_39_native_type_discipline(
    tmp_path: Path,
) -> None:
    # Pre-v1.39: strictyaml stringified all scalars, so `version: 1` arrived
    # as "1" and was coerced. v1.39: native int required; quoted "1" rejected.
    body = 'version: "1"\nworkflow: {}\n'
    path = _write(tmp_path, "str-ver.yaml", body)
    with pytest.raises(UnsupportedManifestVersionError):
        WorkflowManifestLoader.load(path)
