"""U-RT-40 — `materialize_topology_dispatcher_stage` + `RuntimeTopologyDispatcher` tests.

ACs per Phase 2 Session 7 L8 stage 5 LOOP_INIT (U-RT-40):

1. Composer returns stage; frozen; bind error typed.
2. Dispatcher satisfies the narrowed `TopologyDispatcher` Protocol
   (runtime_checkable instance check passes).
3. `dispatch(manifest_entry)` returns the manifest's bound
   `topology_pattern` field per C-CP-06 §6.1.
4. `is_admissible(pattern, workload)` delegates to CP's pure predicate
   (per C-CP-10 §10.3); returns True for §10.3-annotated cells, False
   otherwise.
5. Tension 002 closure verified: CP `TopologyPattern` enum carries the
   spec-canonical 6-value taxonomy (no carry-forward divergence).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core import PersonaTier, WorkloadClass
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_runtime.lifecycle.topology_dispatcher import (
    RuntimeTopologyDispatcher,
    TopologyDispatcherBindError,
    TopologyDispatcherStage,
    materialize_topology_dispatcher_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
    TopologyDispatcher,
)

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


def _dispatcher(tmp_path: Path) -> RuntimeTopologyDispatcher:
    return materialize_topology_dispatcher_stage(_config(tmp_path)).dispatcher


def _manifest(
    *,
    topology_pattern: TopologyPattern = TopologyPattern.SINGLE_THREADED_LINEAR,
    workload_class: WorkloadClass = WorkloadClass.PIPELINE_AUTOMATION,
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id="wf-1",
        workload_class=workload_class,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=topology_pattern,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


# ---------------------------------------------------------------------------
# AC #1 — Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = materialize_topology_dispatcher_stage(_config(tmp_path))
    assert isinstance(stage, TopologyDispatcherStage)
    assert isinstance(stage.dispatcher, RuntimeTopologyDispatcher)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_topology_dispatcher_stage(_config(tmp_path))
    with pytest.raises(AttributeError):
        stage.dispatcher = stage.dispatcher  # type: ignore[misc]


def test_bind_error_typed() -> None:
    assert isinstance(TopologyDispatcherBindError("test"), Exception)


# ---------------------------------------------------------------------------
# AC #2 — Protocol conformance.
# ---------------------------------------------------------------------------


def test_dispatcher_satisfies_protocol(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    assert isinstance(dispatcher, TopologyDispatcher)


# ---------------------------------------------------------------------------
# AC #3 — dispatch returns manifest's bound topology_pattern.
# ---------------------------------------------------------------------------


def test_dispatch_returns_manifest_topology_pattern(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    for pattern in TopologyPattern:
        manifest = _manifest(topology_pattern=pattern)
        assert dispatcher.dispatch(manifest) is pattern


# ---------------------------------------------------------------------------
# AC #4 — is_admissible delegates to CP predicate per §10.3.
# ---------------------------------------------------------------------------


def test_is_admissible_true_for_annotated_cells(tmp_path: Path) -> None:
    """§10.3-annotated cells return True (per ADR-D4 v1.1 §1.2)."""
    dispatcher = _dispatcher(tmp_path)
    annotated_cells = [
        (TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.SOFTWARE_ENGINEERING),
        (TopologyPattern.HIERARCHICAL_DELEGATION, WorkloadClass.RESEARCH),
        (TopologyPattern.DECENTRALIZED_HANDOFF, WorkloadClass.PIPELINE_AUTOMATION),
        (TopologyPattern.PARALLELIZATION, WorkloadClass.RESEARCH),
        (TopologyPattern.PARALLELIZATION, WorkloadClass.CONTENT_CREATION),
    ]
    for pattern, workload in annotated_cells:
        assert dispatcher.is_admissible(pattern, workload) is True


def test_is_admissible_false_for_non_annotated_cells(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    # `single-threaded-linear` is the universal primary pattern, NOT §10.3
    # cross-pattern annotated for any workload.
    for workload in WorkloadClass:
        assert dispatcher.is_admissible(TopologyPattern.SINGLE_THREADED_LINEAR, workload) is False


def test_is_admissible_orchestrator_workers_not_cross_pattern(tmp_path: Path) -> None:
    """`orchestrator-workers` is not §10.3 annotated for any workload."""
    dispatcher = _dispatcher(tmp_path)
    for workload in WorkloadClass:
        assert dispatcher.is_admissible(TopologyPattern.ORCHESTRATOR_WORKERS, workload) is False


# ---------------------------------------------------------------------------
# AC #5 — Tension 002 closure: TopologyPattern carries spec-canonical 6 values.
# ---------------------------------------------------------------------------


def test_topology_pattern_enum_cardinality_six() -> None:
    """C-CP-10 §10.1 — closed at cardinality 6 (Tension 002 conformed)."""
    assert len(list(TopologyPattern)) == 6


def test_topology_pattern_enum_string_values_spec_verbatim() -> None:
    """Member string values are C-CP-10 §10.1 verbatim (Tension 002 Set 2)."""
    expected = {
        "single-threaded-linear",
        "orchestrator-workers",
        "decentralized-handoff",
        "hierarchical-delegation",
        "evaluator-optimizer",
        "parallelization",
    }
    assert {p.value for p in TopologyPattern} == expected


def test_dispatcher_is_stateless(tmp_path: Path) -> None:
    """Two calls with same inputs → same result."""
    dispatcher = _dispatcher(tmp_path)
    manifest = _manifest(topology_pattern=TopologyPattern.EVALUATOR_OPTIMIZER)
    assert dispatcher.dispatch(manifest) is dispatcher.dispatch(manifest)
