"""U-RT-22 — engine-class selector tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L5 U-RT-22:
  #1 every WorkloadClass resolves to an EngineClass
     -> test_every_workload_persona_combination_binds
     -> test_select_returns_engine_class_for_all_combinations
  #2 missing binding raises typed error at bootstrap, not at runtime
     -> test_bind_failure_surfaces_at_bootstrap_not_runtime
     -> test_bind_error_carries_failure_set
  Bonus coverage (manifest-override behavior per W-2):
     -> test_manifest_engine_class_override_wins_over_selection
     -> test_override_applies_across_all_persona_tiers
     -> test_no_override_falls_through_to_bindings

Test convention notes:
- The 4 WorkloadClass * 3 PersonaTier = 12 combinations are exercised
  exhaustively in `test_every_workload_persona_combination_binds`.
- Manifest-override semantics are W-2-derived; the override SKIPS persona-tier
  admissibility (the operator forces the class), so a pure-pattern override at
  multi-tenant-compliance returns pure-pattern (not the persona-tier-admissible
  alternative). This matches the W-2 docstring at
  `harness_cp.routing_manifest_residence`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core import DeploymentSurface, PersonaTier, WorkloadClass
from harness_cp.engine_class import EngineClass
from harness_cp.routing_manifest_residence import (
    RoutingManifest,
    WorkloadRoutingOverride,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.engine_selector import (
    EngineSelectorBindError,
    RuntimeEngineSelector,
    materialize_engine_selector,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal RuntimeConfig with optional manifest override.
# ---------------------------------------------------------------------------


def _config(
    tmp_path: Path,
    *,
    deployment_surface: DeploymentSurface = DeploymentSurface.LOCAL_DEVELOPMENT,
    manifest: RoutingManifest | None = None,
) -> RuntimeConfig:
    if manifest is None:
        return RuntimeConfig(
            deployment_surface=deployment_surface,
            repository_root=tmp_path,
            path_bindings=PathBindingConfig(),
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            collector=CollectorConfig(),
            default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        )
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        routing_manifest=manifest,
    )


def _manifest_with_override(
    workload_class: WorkloadClass,
    engine_class: EngineClass,
) -> RoutingManifest:
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={
            workload_class: WorkloadRoutingOverride(
                engine_class_override=engine_class,
            ),
        },
        fallback_chains=(),
        retry_policies={},
    )


# ---------------------------------------------------------------------------
# AC #1 — every WorkloadClass resolves to an EngineClass.
# ---------------------------------------------------------------------------


def test_every_workload_persona_combination_binds(tmp_path: Path) -> None:
    """All 4 * 3 = 12 `(WorkloadClass, PersonaTier)` combinations resolve at
    `LOCAL_DEVELOPMENT` deployment surface. AC #1."""
    selector = materialize_engine_selector(_config(tmp_path))
    assert isinstance(selector, RuntimeEngineSelector)
    assert len(selector.bindings) == len(WorkloadClass) * len(PersonaTier)
    # Every combination produces a real `EngineClass` (no None / placeholder).
    for wc in WorkloadClass:
        for pt in PersonaTier:
            assert isinstance(selector.bindings[(wc, pt)], EngineClass)


def test_select_returns_engine_class_for_all_combinations(tmp_path: Path) -> None:
    """`select()` returns an `EngineClass` for every input. AC #1."""
    selector = materialize_engine_selector(_config(tmp_path))
    for wc in WorkloadClass:
        for pt in PersonaTier:
            assert isinstance(selector.select(wc, pt), EngineClass)


def test_select_is_deterministic(tmp_path: Path) -> None:
    """Two `select()` calls against the same input return the same class
    (deterministic per CP `select_engine_class` acceptance #2; here the
    selector caches at bootstrap so this is trivially true, but pinning it
    against future drift is cheap)."""
    selector = materialize_engine_selector(_config(tmp_path))
    for wc in WorkloadClass:
        for pt in PersonaTier:
            a = selector.select(wc, pt)
            b = selector.select(wc, pt)
            assert a is b


def test_software_engineering_solo_developer_resolves_per_spec(tmp_path: Path) -> None:
    """Spot-check one well-known cell: software-engineering + solo-developer
    favors `SAVE_POINT_CHECKPOINT` per the §7.3 workload-class favoring
    table at `_WORKLOAD_CLASS_FAVORED`. Pins the wiring without re-asserting
    CP's selection logic (those tests live at
    `harness-cp/tests/test_workload_binding_engine_class_selection.py`)."""
    selector = materialize_engine_selector(_config(tmp_path))
    assert (
        selector.select(
            WorkloadClass.SOFTWARE_ENGINEERING,
            PersonaTier.SOLO_DEVELOPER,
        )
        is EngineClass.SAVE_POINT_CHECKPOINT
    )


# ---------------------------------------------------------------------------
# AC #2 — missing binding raises at bootstrap, not at runtime.
# ---------------------------------------------------------------------------


def test_bind_failure_surfaces_at_bootstrap_not_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `select_engine_class` raises `WorkloadBindingError` for any
    combination, `materialize_engine_selector` raises `EngineSelectorBindError`
    at bootstrap — the runtime selector is never constructed. AC #2."""
    from harness_cp.workload_binding_engine_class_selection import (
        WorkloadBindingError,
    )
    from harness_runtime.lifecycle import engine_selector as mod

    def _always_fail(_input: object) -> object:
        raise WorkloadBindingError("synthetic bootstrap failure")

    monkeypatch.setattr(mod, "select_engine_class", _always_fail)

    with pytest.raises(EngineSelectorBindError):
        materialize_engine_selector(_config(tmp_path))


def test_bind_error_carries_failure_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The raised `EngineSelectorBindError` carries the exhaustive list of
    failing `(WorkloadClass, PersonaTier, reason)` tuples — bootstrap surfaces
    every failure, not just the first."""
    from harness_cp.workload_binding_engine_class_selection import (
        WorkloadBindingError,
    )
    from harness_runtime.lifecycle import engine_selector as mod

    def _always_fail(_input: object) -> object:
        raise WorkloadBindingError("synthetic")

    monkeypatch.setattr(mod, "select_engine_class", _always_fail)

    with pytest.raises(EngineSelectorBindError) as excinfo:
        materialize_engine_selector(_config(tmp_path))
    # All 12 combinations are reported.
    assert len(excinfo.value.failures) == len(WorkloadClass) * len(PersonaTier)
    # Each failure tuple is well-formed.
    for wc, pt, reason in excinfo.value.failures:
        assert isinstance(wc, WorkloadClass)
        assert isinstance(pt, PersonaTier)
        assert "synthetic" in reason


# ---------------------------------------------------------------------------
# Manifest-override behavior (W-2 `engine_class_override`).
# ---------------------------------------------------------------------------


def test_manifest_engine_class_override_wins_over_selection(tmp_path: Path) -> None:
    """`WorkloadRoutingOverride.engine_class_override` forces the engine class
    for that workload, overriding the CP `select_engine_class` result."""
    # software-engineering normally favors SAVE_POINT_CHECKPOINT.
    manifest = _manifest_with_override(
        WorkloadClass.SOFTWARE_ENGINEERING,
        EngineClass.EVENT_SOURCED_REPLAY,
    )
    selector = materialize_engine_selector(_config(tmp_path, manifest=manifest))
    assert (
        selector.select(
            WorkloadClass.SOFTWARE_ENGINEERING,
            PersonaTier.SOLO_DEVELOPER,
        )
        is EngineClass.EVENT_SOURCED_REPLAY
    )


def test_override_applies_across_all_persona_tiers(tmp_path: Path) -> None:
    """The override is per-workload-class — it applies regardless of persona
    tier (W-2 forces the class; persona-tier admissibility is not consulted)."""
    manifest = _manifest_with_override(
        WorkloadClass.RESEARCH,
        EngineClass.RECONCILER_LOOP,
    )
    selector = materialize_engine_selector(_config(tmp_path, manifest=manifest))
    for pt in PersonaTier:
        assert selector.select(WorkloadClass.RESEARCH, pt) is EngineClass.RECONCILER_LOOP


def test_no_override_falls_through_to_bindings(tmp_path: Path) -> None:
    """When a workload class has no `engine_class_override`, selection falls
    through to the bootstrap-resolved binding (the CP `select_engine_class`
    result)."""
    # Override CONTENT_CREATION only.
    manifest = _manifest_with_override(
        WorkloadClass.CONTENT_CREATION,
        EngineClass.PURE_PATTERN_NO_ENGINE,
    )
    selector = materialize_engine_selector(_config(tmp_path, manifest=manifest))
    # CONTENT_CREATION returns the override.
    assert (
        selector.select(WorkloadClass.CONTENT_CREATION, PersonaTier.SOLO_DEVELOPER)
        is EngineClass.PURE_PATTERN_NO_ENGINE
    )
    # SOFTWARE_ENGINEERING is unaffected — falls through to bindings.
    assert (
        selector.select(
            WorkloadClass.SOFTWARE_ENGINEERING,
            PersonaTier.SOLO_DEVELOPER,
        )
        is EngineClass.SAVE_POINT_CHECKPOINT
    )


def test_selector_is_frozen(tmp_path: Path) -> None:
    """`RuntimeEngineSelector` is a frozen dataclass — mutation is rejected."""
    from dataclasses import FrozenInstanceError

    selector = materialize_engine_selector(_config(tmp_path))
    with pytest.raises(FrozenInstanceError):
        selector.bindings = {}  # type: ignore[misc]
