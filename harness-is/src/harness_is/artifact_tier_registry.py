"""Artifact-tier registry schema — U-IS-03.

Implements C-IS-02 §2 (artifact-tier layering schema). Declares the 5-tier
`ArtifactTier` enum, the per-tier metadata schema, and the populated
`ARTIFACT_TIER_REGISTRY`.

Authority: Implementation_Plan_Information_Substrate_v2_1.md §2 U-IS-03
(preserved verbatim at v2.2); Spec_Information_Substrate_v1.md §2 C-IS-02;
ADR-F2 v1.2 §Decision (artifact-tier layering: working / episodic /
semantic / procedural / durable).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict


class ArtifactTier(StrEnum):
    """The 5 artifact tiers (C-IS-02 §2)."""

    WORKING = "WORKING"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"
    DURABLE = "DURABLE"


class SurvivalScope(StrEnum):
    """Survival scope of a tier's artifacts (C-IS-02 §2 "Survives across")."""

    WITHIN_SINGLE_INFERENCE_CALL = "WITHIN_SINGLE_INFERENCE_CALL"
    WITHIN_RUN_RESTART_VIA_REPLAY_ONLY = "WITHIN_RUN_RESTART_VIA_REPLAY_ONLY"
    ACROSS_RUNS = "ACROSS_RUNS"
    ACROSS_RUNS_AND_WORKFLOW_VERSIONS = "ACROSS_RUNS_AND_WORKFLOW_VERSIONS"
    ACROSS_RUNS_AND_CRASH_RECOVERY = "ACROSS_RUNS_AND_CRASH_RECOVERY"


class SubstrateResidence(BaseModel):
    """Substrate residence for a tier (C-IS-02 §2 "Substrate residence")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filesystem: bool
    """True for all 5 tiers."""

    git: bool
    """False for WORKING and EPISODIC; true for the 3 durable-survival tiers."""


class ArtifactTierMetadata(BaseModel):
    """Registered metadata for one artifact tier (C-IS-02 §2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: ArtifactTier
    semantic_role: str
    substrate_residence: SubstrateResidence
    survival_scope: SurvivalScope


# --- Registry population (spec C-IS-02 §2 five-tier table) -----------------

_FILESYSTEM_ONLY = SubstrateResidence(filesystem=True, git=False)
_FILESYSTEM_AND_GIT = SubstrateResidence(filesystem=True, git=True)

_TIER_SPEC: dict[ArtifactTier, tuple[str, SubstrateResidence, SurvivalScope]] = {
    ArtifactTier.WORKING: (
        "Per-run scratch state",
        _FILESYSTEM_ONLY,
        SurvivalScope.WITHIN_SINGLE_INFERENCE_CALL,
    ),
    ArtifactTier.EPISODIC: (
        "Per-run history; in-flight conversational state",
        _FILESYSTEM_ONLY,
        SurvivalScope.WITHIN_RUN_RESTART_VIA_REPLAY_ONLY,
    ),
    ArtifactTier.SEMANTIC: (
        "Cross-run knowledge artifacts; learned content",
        _FILESYSTEM_AND_GIT,
        SurvivalScope.ACROSS_RUNS,
    ),
    ArtifactTier.PROCEDURAL: (
        "Workflow-class procedural artifacts (Skills, prompts, routing manifest)",
        _FILESYSTEM_AND_GIT,
        SurvivalScope.ACROSS_RUNS_AND_WORKFLOW_VERSIONS,
    ),
    ArtifactTier.DURABLE: (
        "Append-only state-ledger + JSONL event ledger + audit ledger",
        _FILESYSTEM_AND_GIT,
        SurvivalScope.ACROSS_RUNS_AND_CRASH_RECOVERY,
    ),
}

ARTIFACT_TIER_REGISTRY: Mapping[ArtifactTier, ArtifactTierMetadata] = MappingProxyType(
    {
        tier: ArtifactTierMetadata(
            tier=tier,
            semantic_role=semantic_role,
            substrate_residence=substrate_residence,
            survival_scope=survival_scope,
        )
        for tier, (
            semantic_role,
            substrate_residence,
            survival_scope,
        ) in _TIER_SPEC.items()
    }
)
"""Immutable registry — one `ArtifactTierMetadata` per `ArtifactTier`."""
