"""Workload manifest opt-in declaration schema — U-IS-13.

Implements C-IS-08 §8.1 + §8.2 (shadow-Git checkpoint opt-in + cadence) and
C-IS-09 §9.1 (worktree-isolation opt-in). Declares `WorkloadManifestOptIns` —
the per-workflow-manifest declaration of the two workload-class-opt-in F2
substrate features (shadow-Git checkpointing, worktree isolation) — and the
`CheckpointCadence` enum.

Both features default OFF (acceptance #2). The manifest authoring format
(YAML / JSON / TOML) is configuration-supplied per the §8 deferral; this unit
declares the validated in-memory schema only.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.1 U-IS-13
(preserved verbatim from v2.1 §2); Spec_Information_Substrate_v1.md C-IS-08
§8.1 / §8.2 + C-IS-09 §9.1; ADR-F2 v1.2 §Rationale (a).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class CheckpointCadence(StrEnum):
    """Shadow-Git checkpoint cadence (C-IS-08 §8.2, verbatim).

    Closed at cardinality 4 — member string values byte-exact with the §8.2
    cadence enumeration.
    """

    PER_STEP = "per_step"
    PER_TOOL_CALL = "per_tool_call"
    PER_SIGNIFICANT_CHANGE = "per_significant_change"
    PER_EXPLICIT_MARKER = "per_explicit_marker"


class WorkloadManifestOptIns(BaseModel):
    """The 4-field workload-manifest opt-in declaration (C-IS-08 §8.1 / C-IS-09 §9.1).

    Validation invariant (acceptance #5): `shadow_git_enabled == True` requires
    `shadow_git_cadence` to be set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    shadow_git_enabled: bool = False
    shadow_git_cadence: CheckpointCadence | None = None
    worktree_isolation_enabled: bool = False
    worktree_concurrency_cap: int | None = None
    """Absent (`None`) = unbounded worktree concurrency (C-IS-09 §9.1)."""

    @model_validator(mode="after")
    def _shadow_git_enabled_requires_cadence(self) -> WorkloadManifestOptIns:
        """C-IS-08 §8.1 / §8.2 — a checkpoint cadence is required when
        shadow-Git checkpointing is enabled."""
        if self.shadow_git_enabled and self.shadow_git_cadence is None:
            raise ValueError("shadow_git_cadence is required when shadow_git_enabled is true")
        return self
