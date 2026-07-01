"""Unit tests for the runtime prompt-version approval gate (R-PM-1 cascade PR #4).

`enforce_prompt_version_approval` consumes the OD per-persona-tier prompt-governance
posture (C-OD-34, `harness_od.prompt_governance_gradient`) at the bootstrap stage-0
selection-reconciliation site: at a binding tier (team-binding / multi-tenant-
compliance) a selection-DRIVEN active prompt version must be operator-approved
(`approved_prompt_version_shas`), else fail-loud. The gate is inert at solo-developer
and for inline-only / no-match deployments.

The bootstrap-level e2e proof (the gate firing through `run_bootstrap`) lives in
`test_bootstrap.py`; these isolate the gate logic + non-vacuous tier-distinctness.
"""

from __future__ import annotations

import pytest
from harness_core import PersonaTier, WorkloadClass
from harness_cp.cp_shared_types import AgentRole
from harness_cp.prompt_selection_manifest import PromptBinding, PromptSelectionManifest
from harness_is.prompt_manifest import prompt_version_sha
from harness_runtime.lifecycle.prompt_selection import (
    PromptVersionUnapprovedError,
    enforce_prompt_version_approval,
)

_SE = WorkloadClass.SOFTWARE_ENGINEERING
_RESEARCH = WorkloadClass.RESEARCH
_SHA_A = prompt_version_sha("A body")


def _workload_selection(
    sha: str = _SHA_A, *, workload: WorkloadClass = _SE
) -> PromptSelectionManifest:
    """A selection manifest whose workload override drives `sha` for `workload`."""
    return PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={workload: PromptBinding(version_sha=sha)},
    )


def test_solo_tier_is_inert_even_when_unapproved() -> None:
    """Solo-developer (local-first) → the gate never fires, even with a selection
    driving a version absent from the (empty) approved set."""
    enforce_prompt_version_approval(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        selection_manifest=_workload_selection(),
        approved_prompt_version_shas=frozenset(),
        workload_class=_SE,
    )  # no raise


@pytest.mark.parametrize("tier", [PersonaTier.TEAM_BINDING, PersonaTier.MULTI_TENANT_COMPLIANCE])
def test_binding_tier_unapproved_selection_fails_loud(tier: PersonaTier) -> None:
    """A binding tier activating a selection-driven version NOT in the approved set
    is fail-loud (RT-FAIL-PROMPT-VERSION-UNAPPROVED)."""
    with pytest.raises(
        PromptVersionUnapprovedError, match="RT-FAIL-PROMPT-VERSION-UNAPPROVED"
    ) as exc:
        enforce_prompt_version_approval(
            persona_tier=tier,
            selection_manifest=_workload_selection(),
            approved_prompt_version_shas=frozenset(),
            workload_class=_SE,
        )
    assert exc.value.persona_tier is tier
    assert exc.value.version_sha == _SHA_A


@pytest.mark.parametrize("tier", [PersonaTier.TEAM_BINDING, PersonaTier.MULTI_TENANT_COMPLIANCE])
def test_binding_tier_approved_selection_passes(tier: PersonaTier) -> None:
    """A binding tier activating a selection-driven version that IS approved → no raise."""
    enforce_prompt_version_approval(
        persona_tier=tier,
        selection_manifest=_workload_selection(),
        approved_prompt_version_shas=frozenset({_SHA_A}),
        workload_class=_SE,
    )  # no raise


def test_binding_tier_no_selection_manifest_is_inert() -> None:
    """A binding tier WITHOUT a selection manifest (inline-only deployment) → inert;
    nothing selection-driven to govern."""
    enforce_prompt_version_approval(
        persona_tier=PersonaTier.TEAM_BINDING,
        selection_manifest=None,
        approved_prompt_version_shas=frozenset(),
        workload_class=_SE,
    )  # no raise


def test_binding_tier_selection_fall_through_is_inert() -> None:
    """A binding tier whose selection manifest does NOT match the run's
    (role, workload) → inert; the active prompt is the inline default, not
    selection-driven."""
    # Override is keyed on _RESEARCH; the run workload is _SE → no match.
    enforce_prompt_version_approval(
        persona_tier=PersonaTier.TEAM_BINDING,
        selection_manifest=_workload_selection(workload=_RESEARCH),
        approved_prompt_version_shas=frozenset(),
        workload_class=_SE,
    )  # no raise


def test_gate_is_non_vacuously_tier_distinct() -> None:
    """Non-vacuity: the SAME unapproved selection that is inert at solo FAILS at the
    binding tiers — the enforcement genuinely differs by tier, not just a table."""
    selection = _workload_selection()
    # Inert at solo.
    enforce_prompt_version_approval(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        selection_manifest=selection,
        approved_prompt_version_shas=frozenset(),
        workload_class=_SE,
    )
    # ...but fails at team + multi.
    for tier in (PersonaTier.TEAM_BINDING, PersonaTier.MULTI_TENANT_COMPLIANCE):
        with pytest.raises(PromptVersionUnapprovedError):
            enforce_prompt_version_approval(
                persona_tier=tier,
                selection_manifest=selection,
                approved_prompt_version_shas=frozenset(),
                workload_class=_SE,
            )


def test_per_role_binding_against_mvp_default_role_is_governed() -> None:
    """A per-role binding (resolved against the MVP-default role) is selection-driven
    and therefore governed at a binding tier — parity with the reconciler's role path."""
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={AgentRole("default"): PromptBinding(version_sha=_SHA_A)},
    )
    with pytest.raises(PromptVersionUnapprovedError):
        enforce_prompt_version_approval(
            persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
            selection_manifest=selection,
            approved_prompt_version_shas=frozenset(),
            workload_class=_SE,
        )
