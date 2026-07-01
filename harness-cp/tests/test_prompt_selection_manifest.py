"""Tests for the CP prompt-selection surface — `PromptSelectionManifest`.

R-PM-1 cascade PR #3 (CP spec v1.31 §29). The CP-axis prompt-selection layer:
per-role + per-workload bindings resolving `(role, workload) → version_sha`,
mirroring `RoutingManifest.per_role_bindings` / `per_workload_overrides`.

These are CP-pure unit tests — the resolver yields an sha and never consults the
IS store; the store-membership check + the e2e selection→sha→content→injection
proof live at the runtime consumer site (`harness-runtime`), the appropriate
verification shape for a CP-pure surface (`[[verification-shape-sharpened-grep-vs-e2e]]`).
"""

from __future__ import annotations

import pytest
from harness_core import WorkloadClass
from harness_cp.cp_shared_types import AgentRole
from harness_cp.prompt_selection_manifest import (
    PromptBinding,
    PromptSelectionManifest,
    PromptSelectionManifestValidationError,
    resolve_active_prompt_version_sha,
    validate_prompt_selection_manifest,
)
from pydantic import ValidationError

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def test_prompt_binding_frozen_and_extra_forbid() -> None:
    """`PromptBinding` mirrors `RoleRoutingBinding`: frozen + extra-forbid."""
    binding = PromptBinding(version_sha=_SHA_A)
    assert binding.version_sha == _SHA_A
    with pytest.raises(ValidationError):
        binding.version_sha = _SHA_B  # type: ignore[misc]  # frozen
    with pytest.raises(ValidationError):
        PromptBinding(version_sha=_SHA_A, unexpected="x")  # type: ignore[call-arg]  # extra-forbid


def test_prompt_selection_manifest_frozen_and_defaults_empty() -> None:
    """`PromptSelectionManifest` mirrors `RoutingManifest` (frozen + extra-forbid);
    both binding maps default to empty (the fall-through / zero-burden default)."""
    manifest = PromptSelectionManifest(manifest_version=1)
    assert manifest.manifest_version == 1
    assert manifest.per_role_bindings == {}
    assert manifest.per_workload_overrides == {}
    with pytest.raises(ValidationError):
        manifest.manifest_version = 2  # type: ignore[misc]  # frozen
    with pytest.raises(ValidationError):
        PromptSelectionManifest(manifest_version=1, unexpected="x")  # type: ignore[call-arg]


def test_resolve_returns_none_for_empty_manifest() -> None:
    """An empty manifest selects nothing → None (fall-through to the inline
    active prompt)."""
    manifest = PromptSelectionManifest(manifest_version=1)
    assert (
        resolve_active_prompt_version_sha(
            manifest,
            role=AgentRole("default"),
            workload=WorkloadClass.SOFTWARE_ENGINEERING,
        )
        is None
    )


def test_resolve_per_role_binding() -> None:
    """A role binding resolves to its version_sha when no workload override matches."""
    manifest = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={AgentRole("planner"): PromptBinding(version_sha=_SHA_A)},
    )
    assert (
        resolve_active_prompt_version_sha(
            manifest, role=AgentRole("planner"), workload=WorkloadClass.RESEARCH
        )
        == _SHA_A
    )
    # An unbound role → None.
    assert (
        resolve_active_prompt_version_sha(
            manifest, role=AgentRole("coder"), workload=WorkloadClass.RESEARCH
        )
        is None
    )


def test_resolve_per_workload_override() -> None:
    """A workload override resolves to its version_sha (no role binding present)."""
    manifest = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: PromptBinding(version_sha=_SHA_B)
        },
    )
    assert (
        resolve_active_prompt_version_sha(
            manifest,
            role=AgentRole("default"),
            workload=WorkloadClass.SOFTWARE_ENGINEERING,
        )
        == _SHA_B
    )
    # A different workload → None.
    assert (
        resolve_active_prompt_version_sha(
            manifest, role=AgentRole("default"), workload=WorkloadClass.CONTENT_CREATION
        )
        is None
    )


def test_resolve_workload_override_takes_precedence_over_role() -> None:
    """Precedence mirrors RoutingManifest: a workload override sits on top of the
    role binding."""
    manifest = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={AgentRole("planner"): PromptBinding(version_sha=_SHA_A)},
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: PromptBinding(version_sha=_SHA_C)
        },
    )
    # Both the role AND the workload are bound; the workload override wins.
    assert (
        resolve_active_prompt_version_sha(
            manifest,
            role=AgentRole("planner"),
            workload=WorkloadClass.SOFTWARE_ENGINEERING,
        )
        == _SHA_C
    )
    # For a workload with no override, the role binding still applies.
    assert (
        resolve_active_prompt_version_sha(
            manifest, role=AgentRole("planner"), workload=WorkloadClass.RESEARCH
        )
        == _SHA_A
    )


def test_validate_structural_only() -> None:
    """Validation is structural-only (mirrors validate_routing_manifest): a
    positive manifest_version passes; the store-membership check is runtime-deferred."""
    valid = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            # A non-existent sha still passes CP-side validation (membership is a
            # runtime-deferred cross-axis check).
            WorkloadClass.RESEARCH: PromptBinding(version_sha="not-in-any-store")
        },
    )
    assert validate_prompt_selection_manifest(valid) is None

    invalid = PromptSelectionManifest(manifest_version=0)
    err = validate_prompt_selection_manifest(invalid)
    assert isinstance(err, PromptSelectionManifestValidationError)
    assert "positive" in err.reason
