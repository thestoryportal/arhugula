"""Tests for U-IS-04 — combined git tier sub-role taxonomy (C-IS-03 §3).

Test set per the U-IS-04 `Tests:` field. Acceptance-criterion coverage:
  #1 GitTierSubRole — exactly 5 verbatim -> test_git_tier_sub_role_taxonomy_completeness
  #2 SubRolePosture — exactly 2          -> test_sub_role_posture_per_role
  #3 posture assignment per sub-role     -> test_sub_role_posture_per_role
  #4 composition_with per spec §3 column -> test_git_tier_sub_role_taxonomy_completeness
  #5 co-residence invariant constants    -> test_one_repo_hosts_one_ledger_invariant
                                            test_cross_repository_ledger_composition_out_of_scope
"""

from __future__ import annotations

from harness_is.git_tier_sub_role_taxonomy import (
    CO_RESIDENCE_ONE_REPO_HOSTS_ONE_HARNESS_STATE_LEDGER,
    CROSS_REPOSITORY_LEDGER_COMPOSITION,
    GIT_TIER_SUB_ROLE_REGISTRY,
    ContractID,
    GitTierSubRole,
    SubRolePosture,
)

# The 5 sub-role values, verbatim from Spec_Information_Substrate_v1.md §3.
_SPEC_SUB_ROLES = {
    "VERSIONING",
    "STATE_LEDGER_VIA_COMMIT_STREAM",
    "JSONL_EVENT_LEDGER",
    "SHADOW_GIT_CHECKPOINTING",
    "WORKTREE_ISOLATION",
}

# Per-sub-role posture from the spec §3 "Posture" column (acceptance #3).
_SPEC_POSTURE: dict[GitTierSubRole, SubRolePosture] = {
    GitTierSubRole.VERSIONING: SubRolePosture.FOUNDATIONAL,
    GitTierSubRole.STATE_LEDGER_VIA_COMMIT_STREAM: SubRolePosture.FOUNDATIONAL,
    GitTierSubRole.JSONL_EVENT_LEDGER: SubRolePosture.FOUNDATIONAL,
    GitTierSubRole.SHADOW_GIT_CHECKPOINTING: SubRolePosture.WORKLOAD_CLASS_OPT_IN,
    GitTierSubRole.WORKTREE_ISOLATION: SubRolePosture.WORKLOAD_CLASS_OPT_IN,
}

# Per-sub-role composition contracts from the spec §3 column (acceptance #4).
_SPEC_COMPOSITION: dict[GitTierSubRole, tuple[ContractID, ...]] = {
    GitTierSubRole.VERSIONING: (ContractID("C-IS-04"),),
    GitTierSubRole.STATE_LEDGER_VIA_COMMIT_STREAM: (),
    GitTierSubRole.JSONL_EVENT_LEDGER: (ContractID("C-IS-05"), ContractID("C-IS-06")),
    GitTierSubRole.SHADOW_GIT_CHECKPOINTING: (ContractID("C-IS-08"),),
    GitTierSubRole.WORKTREE_ISOLATION: (ContractID("C-IS-09"),),
}


def test_git_tier_sub_role_taxonomy_completeness() -> None:
    """Acceptance #1 + #4 — 5 sub-roles verbatim; composition per spec §3."""
    assert len(GitTierSubRole) == 5
    assert {sr.value for sr in GitTierSubRole} == _SPEC_SUB_ROLES
    assert set(GIT_TIER_SUB_ROLE_REGISTRY.keys()) == set(GitTierSubRole)
    for sub_role, composition in _SPEC_COMPOSITION.items():
        assert GIT_TIER_SUB_ROLE_REGISTRY[sub_role].composition_with == composition


def test_sub_role_posture_per_role() -> None:
    """Acceptance #2 + #3 — 2 postures; assignment matches spec §3 per role."""
    assert len(SubRolePosture) == 2
    for sub_role, posture in _SPEC_POSTURE.items():
        assert GIT_TIER_SUB_ROLE_REGISTRY[sub_role].posture is posture


def test_one_repo_hosts_one_ledger_invariant() -> None:
    """Acceptance #5 — one-repo-one-ledger co-residence constant is true."""
    assert CO_RESIDENCE_ONE_REPO_HOSTS_ONE_HARNESS_STATE_LEDGER is True


def test_cross_repository_ledger_composition_out_of_scope() -> None:
    """Acceptance #5 — cross-repository ledger composition constant is false."""
    assert CROSS_REPOSITORY_LEDGER_COMPOSITION is False
