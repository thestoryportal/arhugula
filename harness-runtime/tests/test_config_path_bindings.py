"""U-RT-05 — `PathBindingConfig` → `PathBinding` materialization tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L1:
- `PathBinding` accepted by `PathResolver`.
- Opt-ins validated (`shadow_git_enabled` requires `shadow_git_cadence`).
"""

from __future__ import annotations

import pytest
from harness_core import DeploymentSurface, WorkloadClass
from harness_is.path_binding import PathBinding, PathBindingDuplicateError
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.workload_manifest_opt_in_schema import (
    CheckpointCadence,
    WorkloadManifestOptIns,
)
from harness_runtime.config.path_bindings import build_path_binding, resolve_opt_ins
from harness_runtime.types import PathBindingConfig
from pydantic import ValidationError


def _entry(
    path_class: PathClass,
    workflow_class: WorkloadClass,
    surface: DeploymentSurface,
    path: str,
) -> dict[str, object]:
    return {
        "path_class": path_class,
        "workflow_class": workflow_class,
        "deployment_surface": surface,
        "path": path,
    }


def test_empty_config_builds_empty_path_binding() -> None:
    """Default `PathBindingConfig()` produces an empty but valid `PathBinding`."""
    binding = build_path_binding(PathBindingConfig())
    assert isinstance(binding, PathBinding)
    assert binding.entries == ()


def test_path_binding_accepted_by_path_resolver() -> None:
    """Built `PathBinding` instantiates a `PathResolver` (plan §2 L1 AC verbatim)."""
    config = PathBindingConfig(
        raw_entries=(
            _entry(
                PathClass.SKILLS,
                WorkloadClass.SOFTWARE_ENGINEERING,
                DeploymentSurface.LOCAL_DEVELOPMENT,
                "/tmp/skills",
            ),
        ),
    )
    binding = build_path_binding(config)
    resolver = PathResolver(binding)
    assert resolver is not None


def test_path_binding_resolves_to_canonical_path() -> None:
    """End-to-end: config → binding → resolver → resolved path matches input."""
    config = PathBindingConfig(
        raw_entries=(
            _entry(
                PathClass.PROMPTS,
                WorkloadClass.SOFTWARE_ENGINEERING,
                DeploymentSurface.LOCAL_DEVELOPMENT,
                "/tmp/prompts",
            ),
        ),
    )
    binding = build_path_binding(config)
    resolver = PathResolver(binding)
    resolved = resolver.resolve_path(
        PathClass.PROMPTS,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert str(resolved) == "/tmp/prompts"


def test_duplicate_triple_rejected() -> None:
    """Two raw entries with the same triple raise `PathBindingDuplicateError`."""
    config = PathBindingConfig(
        raw_entries=(
            _entry(
                PathClass.SKILLS,
                WorkloadClass.SOFTWARE_ENGINEERING,
                DeploymentSurface.LOCAL_DEVELOPMENT,
                "/tmp/skills-a",
            ),
            _entry(
                PathClass.SKILLS,
                WorkloadClass.SOFTWARE_ENGINEERING,
                DeploymentSurface.LOCAL_DEVELOPMENT,
                "/tmp/skills-b",
            ),
        ),
    )
    with pytest.raises(PathBindingDuplicateError):
        build_path_binding(config)


def test_invalid_raw_entry_shape_rejected() -> None:
    """Missing `path` field in a raw record raises `ValidationError`."""
    config = PathBindingConfig(
        raw_entries=(
            {
                "path_class": PathClass.SKILLS,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                # 'path' omitted
            },
        ),
    )
    with pytest.raises(ValidationError):
        build_path_binding(config)


def test_opt_ins_defaults_to_all_off() -> None:
    """Default opt-ins disable shadow-Git + worktree per C-IS-08 / C-IS-09."""
    config = PathBindingConfig()
    opt_ins = resolve_opt_ins(config)
    assert opt_ins.shadow_git_enabled is False
    assert opt_ins.shadow_git_cadence is None
    assert opt_ins.worktree_isolation_enabled is False
    assert opt_ins.worktree_concurrency_cap is None


def test_opt_ins_shadow_git_enabled_requires_cadence() -> None:
    """C-IS-08 invariant: shadow_git_enabled=True without cadence is rejected."""
    with pytest.raises(ValidationError):
        WorkloadManifestOptIns(shadow_git_enabled=True)


def test_opt_ins_shadow_git_with_cadence_validates() -> None:
    """C-IS-08: shadow_git_enabled=True with a cadence value validates."""
    opt_ins = WorkloadManifestOptIns(
        shadow_git_enabled=True,
        shadow_git_cadence=CheckpointCadence.PER_STEP,
    )
    config = PathBindingConfig(opt_ins=opt_ins)
    assert resolve_opt_ins(config) is opt_ins


def test_opt_ins_invalid_config_construction_fails_fast() -> None:
    """Building a `PathBindingConfig` with invalid opt-ins fails at config ctor.

    The check happens in `WorkloadManifestOptIns.__init__` before
    `PathBindingConfig` even sees the field. Pins the fail-fast posture.
    """
    with pytest.raises(ValidationError):
        PathBindingConfig(opt_ins=WorkloadManifestOptIns(shadow_git_enabled=True))
