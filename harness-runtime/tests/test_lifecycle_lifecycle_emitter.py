"""U-RT-41 — `materialize_lifecycle_emitter_stage` + `RuntimeLifecycleEventEmitter` tests.

ACs per Phase 2 Session 7 L8 stage 5 LOOP_INIT (U-RT-41 PARTIAL-LAND):

AC #1 (LANDED) — LifecycleEventEmitter Protocol concretized; emits any of
the 8 canonical `WorkflowEventClass` values per C-CP-05 §5.1; satisfies
the `LifecycleEventEmitter` Protocol type-check.

AC #2 (STRUCK; routed to Class 1 at
`.harness/class_1_tension_u_rt_41_drained_event_class_alignment.md`) —
C-RT-11 §11 step 2 `WorkflowEventClass.DRAINED` emit. No `DRAINED` value
exists in the closed-cardinality-8 `WorkflowEventClass` enum; spec §16 #9
authorizes the split.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workflow_event_class import WorkflowEventClass
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.lifecycle_emitter import (
    LifecycleEmitterBindError,
    LifecycleEmitterStage,
    RuntimeLifecycleEventEmitter,
    materialize_lifecycle_emitter_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    LifecycleEventEmitter,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
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


def _emitter(tmp_path: Path) -> RuntimeLifecycleEventEmitter:
    return materialize_lifecycle_emitter_stage(_config(tmp_path)).emitter


# ---------------------------------------------------------------------------
# Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = materialize_lifecycle_emitter_stage(_config(tmp_path))
    assert isinstance(stage, LifecycleEmitterStage)
    assert isinstance(stage.emitter, RuntimeLifecycleEventEmitter)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_lifecycle_emitter_stage(_config(tmp_path))
    with pytest.raises(AttributeError):
        stage.emitter = stage.emitter  # type: ignore[misc]


def test_bind_error_typed() -> None:
    assert isinstance(LifecycleEmitterBindError("test"), Exception)


# ---------------------------------------------------------------------------
# Protocol conformance.
# ---------------------------------------------------------------------------


def test_emitter_satisfies_protocol(tmp_path: Path) -> None:
    emitter = _emitter(tmp_path)
    assert isinstance(emitter, LifecycleEventEmitter)


# ---------------------------------------------------------------------------
# AC #1 — Emits any of the 8 canonical WorkflowEventClass values.
# ---------------------------------------------------------------------------


def test_emit_records_event(tmp_path: Path) -> None:
    emitter = _emitter(tmp_path)
    emitter.emit(WorkflowEventClass.WORKFLOW_START)
    assert emitter.emitted_events == (WorkflowEventClass.WORKFLOW_START,)


def test_emit_preserves_ordering(tmp_path: Path) -> None:
    emitter = _emitter(tmp_path)
    sequence = (
        WorkflowEventClass.WORKFLOW_START,
        WorkflowEventClass.STEP_BOUNDARY,
        WorkflowEventClass.RETRY_ATTEMPT,
        WorkflowEventClass.STEP_BOUNDARY,
        WorkflowEventClass.LEASE_ACQUIRED,
        WorkflowEventClass.LEASE_RELEASED,
    )
    for event in sequence:
        emitter.emit(event)
    assert emitter.emitted_events == sequence


def test_emit_accepts_all_eight_canonical_values(tmp_path: Path) -> None:
    """Every closed-at-8 WorkflowEventClass value emits cleanly."""
    emitter = _emitter(tmp_path)
    for event in WorkflowEventClass:
        emitter.emit(event)
    assert len(emitter.emitted_events) == 8
    assert set(emitter.emitted_events) == set(WorkflowEventClass)


def test_emitter_starts_empty(tmp_path: Path) -> None:
    assert _emitter(tmp_path).emitted_events == ()


def test_clear_resets_ring(tmp_path: Path) -> None:
    emitter = _emitter(tmp_path)
    emitter.emit(WorkflowEventClass.WORKFLOW_START)
    emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
    emitter.clear()
    assert emitter.emitted_events == ()


def test_emitted_events_returns_immutable_snapshot(tmp_path: Path) -> None:
    """`emitted_events` returns a tuple (immutable); list mutation doesn't leak."""
    emitter = _emitter(tmp_path)
    emitter.emit(WorkflowEventClass.WORKFLOW_START)
    snapshot = emitter.emitted_events
    assert isinstance(snapshot, tuple)
    emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
    # Snapshot is unchanged; the second emit only appears in a fresh read.
    assert snapshot == (WorkflowEventClass.WORKFLOW_START,)
    assert emitter.emitted_events == (
        WorkflowEventClass.WORKFLOW_START,
        WorkflowEventClass.STEP_BOUNDARY,
    )


# ---------------------------------------------------------------------------
# AC #2 (STRUCK) — DRAINED is not in the enum; Class 1 fork filed.
# ---------------------------------------------------------------------------


def test_workflow_event_class_does_not_carry_drained() -> None:
    """Closure verification: `WorkflowEventClass` lacks `DRAINED` (Class 1 root)."""
    values = {e.value for e in WorkflowEventClass}
    assert "drained" not in values
    assert "DRAINED" not in {e.name for e in WorkflowEventClass}
    assert len(list(WorkflowEventClass)) == 8


def test_emitter_cannot_emit_drained_no_enum_value() -> None:
    """`WorkflowEventClass.DRAINED` is not constructible — emit path is STRUCK."""
    with pytest.raises(AttributeError):
        # Accessing a non-existent enum member raises AttributeError; the
        # Class 1 record captures this as the unmaterialized spec commitment.
        _ = WorkflowEventClass.DRAINED  # type: ignore[attr-defined]
