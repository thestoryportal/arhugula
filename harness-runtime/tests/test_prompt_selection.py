"""Unit tests for the runtime prompt-selection consumer (R-PM-1 cascade PR #3).

`reconcile_active_prompt_via_selection` composes the CP selection resolver
(`(role, workload) → version_sha`) with the IS `PromptManifest.versions` store
(`version_sha → member`) and reconciles `active_prompt_version` onto the selected
member — so BOTH the runtime injection reader (`.content`) and the C-IS-05 §5.2
procedural-tier hash reader (`.version_sha`) read the SAME selected version
(coherent by construction). The bootstrap-level e2e proof lives in
`test_bootstrap.py`; these isolate the reconciliation logic + the fail-loud
cross-axis membership check.
"""

from __future__ import annotations

import pytest
from harness_core import PersonaTier, WorkloadClass
from harness_cp.cp_shared_types import AgentRole
from harness_cp.prompt_selection_manifest import PromptBinding, PromptSelectionManifest
from harness_is.prompt_manifest import PromptManifest, prompt_version_sha
from harness_runtime.lifecycle.prompt_selection import (
    InvalidPromptSelectionManifestError,
    PromptSelectionUnauthoredError,
    PromptVersionUnapprovedError,
    reconcile_active_prompt_via_selection,
    resolve_per_role_system_prompts,
)

_SE = WorkloadClass.SOFTWARE_ENGINEERING
_RESEARCH = WorkloadClass.RESEARCH


def _store_manifest() -> PromptManifest:
    """A two-version authored store with 'B body' as the standing inline active."""
    return PromptManifest.from_contents(
        manifest_version=1,
        contents=["A body", "B body"],
        active="B body",
    )


def test_reconcile_none_selection_manifest_unchanged() -> None:
    """No selection configured (`None`) → the manifest is returned unchanged (the
    #496/PR-#1 standing inline active prompt; zero behavior change)."""
    pm = _store_manifest()
    result = reconcile_active_prompt_via_selection(pm, None, workload_class=_SE)
    assert result is pm


def test_reconcile_no_match_unchanged() -> None:
    """A selection manifest that binds neither the role nor the workload → unchanged."""
    pm = _store_manifest()
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={_RESEARCH: PromptBinding(version_sha=prompt_version_sha("A body"))},
    )
    # The run workload is _SE, but the override is for _RESEARCH → no match.
    result = reconcile_active_prompt_via_selection(pm, selection, workload_class=_SE)
    assert result.active_prompt_version.content == "B body"


def test_reconcile_workload_selection_drives_active_and_is_coherent() -> None:
    """A workload override selects an authored member; BOTH the content reader and
    the version_sha (hash) reader move to the selected version — coherent."""
    pm = _store_manifest()  # active = "B body"
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={_SE: PromptBinding(version_sha=prompt_version_sha("A body"))},
    )
    result = reconcile_active_prompt_via_selection(pm, selection, workload_class=_SE)
    # Injection reader (content) AND §5.2 hash reader (version_sha) BOTH = selected.
    assert result.active_prompt_version.content == "A body"
    assert result.active_prompt_version.version_sha == prompt_version_sha("A body")
    # The store is preserved intact (only the active selection changed).
    assert {v.version_sha for v in result.versions} == {
        prompt_version_sha("A body"),
        prompt_version_sha("B body"),
    }


def test_reconcile_per_role_binding_against_mvp_default_role() -> None:
    """Per-role selection resolves against the MVP-default role (the runtime has no
    per-step role at MVP; faithful to the routing precedent)."""
    pm = _store_manifest()
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("default"): PromptBinding(version_sha=prompt_version_sha("A body"))
        },
    )
    # No workload override → falls to the role binding, keyed on the default role.
    result = reconcile_active_prompt_via_selection(pm, selection, workload_class=_SE)
    assert result.active_prompt_version.content == "A body"


def test_reconcile_unauthored_sha_fails_loud() -> None:
    """A binding to a sha that is not an authored store member is fail-loud
    (cross-axis membership check, detect-then-refuse)."""
    pm = _store_manifest()
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={_SE: PromptBinding(version_sha="deadbeef" * 8)},
    )
    with pytest.raises(PromptSelectionUnauthoredError, match="RT-FAIL-PROMPT-SELECTION-UNAUTHORED"):
        reconcile_active_prompt_via_selection(pm, selection, workload_class=_SE)


def test_mvp_default_role_matches_dispatch_default() -> None:
    """One-source-of-truth seam (advisor): prompt-selection's MVP-default role
    MUST equal the dispatch MVP-default role. Today both default/discard the role
    so it is inert, but when R-300-second-provider lands real per-role dispatch a
    divergence would silently key prompt-selection on a different role than
    routing. This pins the coupling (enforced, not comment-documented)."""
    from harness_runtime.lifecycle.llm_dispatch import (
        _MVP_DEFAULT_AGENT_ROLE as _DISPATCH_DEFAULT_ROLE,
    )
    from harness_runtime.lifecycle.prompt_selection import (
        _MVP_DEFAULT_AGENT_ROLE as _SELECTION_DEFAULT_ROLE,
    )

    assert _SELECTION_DEFAULT_ROLE == _DISPATCH_DEFAULT_ROLE


def test_reconcile_invalid_manifest_fails_loud() -> None:
    """An operator-supplied manifest that fails the structural validator
    (`manifest_version < 1`) is fail-loud at the consumer site — parity with
    `build_routing_manifest`'s bootstrap validation (the validator is not left
    unwired)."""
    pm = _store_manifest()
    invalid = PromptSelectionManifest(
        manifest_version=0,
        per_workload_overrides={_SE: PromptBinding(version_sha=prompt_version_sha("A body"))},
    )
    with pytest.raises(InvalidPromptSelectionManifestError, match="manifest_version"):
        reconcile_active_prompt_via_selection(pm, invalid, workload_class=_SE)


def test_reconcile_empty_store_with_selection_fails_loud() -> None:
    """Selecting any version against an empty store is fail-loud (you cannot select
    an unauthored version)."""
    pm = PromptManifest.from_contents(manifest_version=1, contents=[], active=None)
    assert pm.versions == ()
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={_SE: PromptBinding(version_sha=prompt_version_sha("anything"))},
    )
    with pytest.raises(PromptSelectionUnauthoredError):
        reconcile_active_prompt_via_selection(pm, selection, workload_class=_SE)


# ---------------------------------------------------------------------------
# R-FS-1 arc B4 — resolve_per_role_system_prompts (the stage-0 builder of the
# per-role injection map the LLM dispatcher indexes at dispatch, §14.5.3).
# ---------------------------------------------------------------------------

_A_SHA = prompt_version_sha("A body")


def test_resolve_per_role_none_selection_returns_empty() -> None:
    """No selection manifest → empty map → every dispatch falls through to the
    default-role active_system_prompt (byte-identical to pre-B4)."""
    assert (
        resolve_per_role_system_prompts(
            _store_manifest(),
            None,
            workload_class=_SE,
            persona_tier=PersonaTier.SOLO_DEVELOPER,
            approved_prompt_version_shas=frozenset(),
        )
        == {}
    )


def test_resolve_per_role_resolves_content_and_excludes_default() -> None:
    """Each NON-default per-role binding resolves to its authored content; the
    `"default"` role is EXCLUDED (it IS active_system_prompt — keeps the linear
    path falling through unchanged)."""
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("researcher"): PromptBinding(version_sha=_A_SHA),
            AgentRole("default"): PromptBinding(version_sha=_A_SHA),
        },
    )
    result = resolve_per_role_system_prompts(
        _store_manifest(),
        selection,
        workload_class=_SE,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        approved_prompt_version_shas=frozenset(),
    )
    assert result == {AgentRole("researcher"): "A body"}
    assert AgentRole("default") not in result


def test_resolve_per_role_fail_loud_unauthored_sha() -> None:
    """A per-role binding to a sha not in the store fails loud at stage 0
    (surfaces as BootstrapFailure) — never silently dropped."""
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={AgentRole("researcher"): PromptBinding(version_sha="deadbeef" * 8)},
    )
    with pytest.raises(PromptSelectionUnauthoredError):
        resolve_per_role_system_prompts(
            _store_manifest(),
            selection,
            workload_class=_SE,
            persona_tier=PersonaTier.SOLO_DEVELOPER,
            approved_prompt_version_shas=frozenset(),
        )


def test_resolve_per_role_fail_loud_unapproved_at_binding_tier() -> None:
    """A per-role binding driving an authored-but-UNapproved version at a binding
    persona tier fails loud (per-role governance parity with the default-role gate)."""
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={AgentRole("researcher"): PromptBinding(version_sha=_A_SHA)},
    )
    with pytest.raises(PromptVersionUnapprovedError):
        resolve_per_role_system_prompts(
            _store_manifest(),
            selection,
            workload_class=_SE,
            persona_tier=PersonaTier.TEAM_BINDING,
            approved_prompt_version_shas=frozenset(),  # _A_SHA NOT approved
        )
