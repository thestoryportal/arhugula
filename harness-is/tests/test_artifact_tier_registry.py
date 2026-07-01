"""Tests for U-IS-03 — artifact-tier registry schema (C-IS-02 §2).

Test set per the U-IS-03 `Tests:` field. Acceptance-criterion coverage:
  #1 enum cardinality 5 + verbatim values -> test_artifact_tier_registry_completeness
  #2 WORKING/EPISODIC residence            -> test_substrate_residence_per_tier
  #3 SEMANTIC/PROCEDURAL/DURABLE residence -> test_substrate_residence_per_tier
  #4 survival_scope per tier               -> test_survival_scope_per_tier
  #5 statically validatable                -> Pydantic v2 + pyright strict
"""

from __future__ import annotations

from harness_is.artifact_tier_registry import (
    ARTIFACT_TIER_REGISTRY,
    ArtifactTier,
    SurvivalScope,
)

# 5 tier values, verbatim from Spec_Information_Substrate_v1.md §2 C-IS-02.
_SPEC_TIERS = {"WORKING", "EPISODIC", "SEMANTIC", "PROCEDURAL", "DURABLE"}

# Per-tier (filesystem, git) from the spec §2 "Substrate residence" column.
_SPEC_RESIDENCE: dict[ArtifactTier, tuple[bool, bool]] = {
    ArtifactTier.WORKING: (True, False),
    ArtifactTier.EPISODIC: (True, False),
    ArtifactTier.SEMANTIC: (True, True),
    ArtifactTier.PROCEDURAL: (True, True),
    ArtifactTier.DURABLE: (True, True),
}

# Per-tier survival scope from the spec §2 "Survives across" column.
_SPEC_SURVIVAL: dict[ArtifactTier, SurvivalScope] = {
    ArtifactTier.WORKING: SurvivalScope.WITHIN_SINGLE_INFERENCE_CALL,
    ArtifactTier.EPISODIC: SurvivalScope.WITHIN_RUN_RESTART_VIA_REPLAY_ONLY,
    ArtifactTier.SEMANTIC: SurvivalScope.ACROSS_RUNS,
    ArtifactTier.PROCEDURAL: SurvivalScope.ACROSS_RUNS_AND_WORKFLOW_VERSIONS,
    ArtifactTier.DURABLE: SurvivalScope.ACROSS_RUNS_AND_CRASH_RECOVERY,
}


def test_artifact_tier_registry_completeness() -> None:
    """Acceptance #1 — 5 enum values match spec §2 verbatim; all registered."""
    assert len(ArtifactTier) == 5
    assert {t.value for t in ArtifactTier} == _SPEC_TIERS
    assert set(ARTIFACT_TIER_REGISTRY.keys()) == set(ArtifactTier)


def test_substrate_residence_per_tier() -> None:
    """Acceptance #2 + #3 — substrate residence matches spec §2 per tier."""
    for tier, (filesystem, git) in _SPEC_RESIDENCE.items():
        residence = ARTIFACT_TIER_REGISTRY[tier].substrate_residence
        assert residence.filesystem is filesystem
        assert residence.git is git


def test_survival_scope_per_tier() -> None:
    """Acceptance #4 — survival_scope matches spec §2 per tier exhaustively."""
    for tier, scope in _SPEC_SURVIVAL.items():
        assert ARTIFACT_TIER_REGISTRY[tier].survival_scope is scope
