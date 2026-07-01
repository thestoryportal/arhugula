"""U-RT-39 — `materialize_override_evaluator_stage` + `RuntimePerStepOverrideEvaluator` tests.

ACs per Phase 2 Session 7 L8 stage 5 LOOP_INIT (U-RT-39 opens L8):

1. Composer returns stage; frozen; bind error typed.
2. Evaluator satisfies the narrowed `PerStepOverrideEvaluator` Protocol
   (runtime_checkable instance check passes).
3. `resolve_step_binding` delegates to CP — same `StepEffectiveBinding`
   output as calling CP's `resolve_step_binding` directly.
4. Override path: when manifest carries a per-step override, the result
   has `override_applied=True` and `override_audit_ref` non-None.
5. Default path: no override → `override_applied=False`, manifest
   defaults inherited field-by-field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import (
    StepEffectiveBinding,
    resolve_step_binding,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from harness_runtime.lifecycle.override_evaluator import (
    OverrideEvaluatorBindError,
    OverrideEvaluatorStage,
    RuntimePerStepOverrideEvaluator,
    materialize_override_evaluator_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    PerStepOverrideEvaluator,
    ProviderSecretsConfig,
    RuntimeConfig,
)

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="default-model")
_OVERRIDE_BINDING = ModelBinding(provider="anthropic", model="override-model")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(provider="anthropic", model="m", family=ProviderFamily.ANTHROPIC),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _evaluator(tmp_path: Path) -> RuntimePerStepOverrideEvaluator:
    return materialize_override_evaluator_stage(_config(tmp_path)).evaluator


def _manifest(**over: object) -> WorkflowManifestEntry:
    base: dict[str, object] = {
        "workflow_id": "wf-1",
        "workload_class": WorkloadClass.PIPELINE_AUTOMATION,
        "persona_tier": PersonaTier.TEAM_BINDING,
        "engine_class": EngineClass.PURE_PATTERN_NO_ENGINE,
        "topology_pattern": TopologyPattern.SINGLE_THREADED_LINEAR,
        "layer_budgets": (),
        "fallback_chain": _CHAIN,
        "hitl_placements": (),
        "per_step_overrides": {},
    }
    base.update(over)
    return WorkflowManifestEntry(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC #1 — Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = materialize_override_evaluator_stage(_config(tmp_path))
    assert isinstance(stage, OverrideEvaluatorStage)
    assert isinstance(stage.evaluator, RuntimePerStepOverrideEvaluator)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_override_evaluator_stage(_config(tmp_path))
    with pytest.raises(AttributeError):
        stage.evaluator = stage.evaluator  # type: ignore[misc]


def test_bind_error_typed() -> None:
    assert isinstance(OverrideEvaluatorBindError("test"), Exception)


# ---------------------------------------------------------------------------
# AC #2 — Protocol conformance.
# ---------------------------------------------------------------------------


def test_evaluator_satisfies_protocol(tmp_path: Path) -> None:
    """RuntimePerStepOverrideEvaluator satisfies the narrowed Protocol."""
    evaluator = _evaluator(tmp_path)
    assert isinstance(evaluator, PerStepOverrideEvaluator)


# ---------------------------------------------------------------------------
# AC #3 — Delegation parity with CP.
# ---------------------------------------------------------------------------


def test_default_path_matches_cp_direct(tmp_path: Path) -> None:
    """No override → result identical to calling CP's resolve_step_binding."""
    manifest = _manifest()
    runtime_result = _evaluator(tmp_path).resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    cp_result = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert isinstance(runtime_result, StepEffectiveBinding)
    assert runtime_result == cp_result


def test_override_path_matches_cp_direct(tmp_path: Path) -> None:
    """Override → runtime output is the same shape as CP-direct output."""
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    runtime_result = _evaluator(tmp_path).resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    cp_result = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert isinstance(runtime_result, StepEffectiveBinding)
    # `override_audit_ref` carries a fresh UUID inside the CP composer, so
    # the two results diverge on that field. Compare the deterministic
    # subset (model_binding / engine_class / override_applied).
    assert runtime_result.model_binding == cp_result.model_binding
    assert runtime_result.engine_class == cp_result.engine_class
    assert runtime_result.override_applied == cp_result.override_applied


# ---------------------------------------------------------------------------
# AC #4 — Override path: audit ref non-None, override_applied True.
# ---------------------------------------------------------------------------


def test_override_path_populates_audit_ref(tmp_path: Path) -> None:
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    result = _evaluator(tmp_path).resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert isinstance(result, StepEffectiveBinding)
    assert result.override_applied is True
    assert result.override_audit_ref is not None
    assert result.model_binding == _OVERRIDE_BINDING


# ---------------------------------------------------------------------------
# AC #5 — Default path: no override, manifest defaults inherited.
# ---------------------------------------------------------------------------


def test_default_path_no_override(tmp_path: Path) -> None:
    manifest = _manifest()
    result = _evaluator(tmp_path).resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert isinstance(result, StepEffectiveBinding)
    assert result.override_applied is False
    assert result.override_audit_ref is None
    assert result.model_binding == _DEFAULT_BINDING
    assert result.engine_class is EngineClass.PURE_PATTERN_NO_ENGINE


def test_evaluator_is_stateless_between_calls(tmp_path: Path) -> None:
    """Two calls with same inputs → equal deterministic-subset results."""
    evaluator = _evaluator(tmp_path)
    manifest = _manifest()
    a = evaluator.resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    b = evaluator.resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert a == b
