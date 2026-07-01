"""U-RT-10 — `materialize_path_registry` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L2:
- All PathClass members resolve.
- Missing paths created idempotently.
- Resolver stored on HarnessContext (verified by the
  `HarnessContext.path_resolver` field declared at U-RT-02; this unit's
  contribution is producing a `PathResolver` instance suitable for the field).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.path_class_registry import PATH_CLASS_REGISTRY, PathClass
from harness_runtime.lifecycle.path_registry import (
    MaterializedPathRegistry,
    PathClassUnresolvedError,
    materialize_path_registry,
)
from harness_runtime.types import PathBindingConfig


def _full_binding_config(tmp_path: Path) -> PathBindingConfig:
    """Build a `PathBindingConfig` with entries for all 4 PathClass members.

    Each path lives under `tmp_path` so tests don't touch the real filesystem
    outside the pytest tmpdir.
    """
    return PathBindingConfig(
        raw_entries=tuple(
            {
                "path_class": path_class,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(tmp_path / path_class.value.lower()),
            }
            for path_class in PATH_CLASS_REGISTRY
        ),
    )


# ---------------------------------------------------------------------------
# All PathClass members resolve (plan AC).
# ---------------------------------------------------------------------------


def test_all_path_class_members_resolve(tmp_path: Path) -> None:
    """Full binding → every PathClass member resolves."""
    result = materialize_path_registry(
        _full_binding_config(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert set(result.resolved_paths.keys()) == set(PATH_CLASS_REGISTRY)


def test_partial_binding_raises_path_class_unresolved(tmp_path: Path) -> None:
    """Binding missing one PathClass entry → typed `PathClassUnresolvedError`."""
    # Only 3 of 4 PathClass entries present.
    config = PathBindingConfig(
        raw_entries=(
            {
                "path_class": PathClass.SKILLS,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(tmp_path / "skills"),
            },
        ),
    )
    with pytest.raises(PathClassUnresolvedError):
        materialize_path_registry(
            config,
            workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        )


# ---------------------------------------------------------------------------
# Missing paths created idempotently (plan AC).
# ---------------------------------------------------------------------------


def test_missing_paths_are_created(tmp_path: Path) -> None:
    """First materialization creates each resolved filesystem path."""
    config = _full_binding_config(tmp_path)
    result = materialize_path_registry(
        config,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    for path in result.resolved_paths.values():
        assert path.exists()
        assert path.is_dir()


def test_existing_paths_not_disturbed(tmp_path: Path) -> None:
    """Second materialization is idempotent — existing paths remain unchanged."""
    config = _full_binding_config(tmp_path)
    # First pass creates the paths.
    first = materialize_path_registry(
        config,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    # Drop a marker file into each path; it must survive a second materialize.
    for path in first.resolved_paths.values():
        (path / "marker.txt").write_text("preserved")

    second = materialize_path_registry(
        config,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    for path in second.resolved_paths.values():
        marker = path / "marker.txt"
        assert marker.exists()
        assert marker.read_text() == "preserved"


def test_nested_paths_are_parented(tmp_path: Path) -> None:
    """`parents=True` is used; deeply-nested binding paths fresh-create."""
    nested = tmp_path / "a" / "b" / "c" / "d"
    config = PathBindingConfig(
        raw_entries=tuple(
            {
                "path_class": path_class,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(nested / path_class.value.lower()),
            }
            for path_class in PATH_CLASS_REGISTRY
        ),
    )
    result = materialize_path_registry(
        config,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    for path in result.resolved_paths.values():
        assert path.exists()


# ---------------------------------------------------------------------------
# Resolver surface (the HarnessContext.path_resolver field input).
# ---------------------------------------------------------------------------


def test_result_carries_path_resolver(tmp_path: Path) -> None:
    """`MaterializedPathRegistry.resolver` is a `PathResolver`."""
    result = materialize_path_registry(
        _full_binding_config(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    # The resolver round-trips a resolve call (proves it's wired against the binding).
    resolved = result.resolver.resolve_path(
        PathClass.SKILLS,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert resolved == result.resolved_paths[PathClass.SKILLS]


def test_result_is_frozen(tmp_path: Path) -> None:
    """`MaterializedPathRegistry` is a frozen dataclass."""
    result = materialize_path_registry(
        _full_binding_config(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    with pytest.raises((AttributeError, Exception)):
        result.resolver = None  # type: ignore[misc,assignment]


def test_isinstance_materialized_path_registry(tmp_path: Path) -> None:
    """Return type is the documented `MaterializedPathRegistry`."""
    result = materialize_path_registry(
        _full_binding_config(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert isinstance(result, MaterializedPathRegistry)
