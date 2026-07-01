"""Combined git tier sub-role taxonomy — U-IS-04.

Implements C-IS-03 §3 (combined git tier role decomposition). Declares the
5 git-tier sub-roles, their foundational-vs-opt-in posture, per-sub-role
composition contracts, and the cross-sub-role co-residence invariants.

Authority: Implementation_Plan_Information_Substrate_v2_1.md §2 U-IS-04
(preserved verbatim at v2.2); Spec_Information_Substrate_v1.md §3 C-IS-03;
ADR-F2 v1.2 §Decision (combined git tier serving five-sub-role composition).

Spec tension cleared before implementation: C-IS-03 §3 prose said "four
sub-roles" but enumerated 5 — corrected to "five" in-CLI. See
Phase_7_Class_3_Tension_001_Git_Tier_Sub_Role_Count.md.

`ContractID` is named in the U-IS-04 signature but undefined — modelled
here as an opaque str NewType (same pattern as U-IS-02 `WorkflowClass`).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NewType

from pydantic import BaseModel, ConfigDict

ContractID = NewType("ContractID", str)
"""Opaque spec-contract identifier, e.g. ``C-IS-04``."""


class GitTierSubRole(StrEnum):
    """The 5 sub-roles of the combined git tier (C-IS-03 §3)."""

    VERSIONING = "VERSIONING"
    STATE_LEDGER_VIA_COMMIT_STREAM = "STATE_LEDGER_VIA_COMMIT_STREAM"
    JSONL_EVENT_LEDGER = "JSONL_EVENT_LEDGER"
    SHADOW_GIT_CHECKPOINTING = "SHADOW_GIT_CHECKPOINTING"
    WORKTREE_ISOLATION = "WORKTREE_ISOLATION"


class SubRolePosture(StrEnum):
    """Posture commitment of a git-tier sub-role (C-IS-03 §3)."""

    FOUNDATIONAL = "FOUNDATIONAL"
    WORKLOAD_CLASS_OPT_IN = "WORKLOAD_CLASS_OPT_IN"


class GitTierSubRoleMetadata(BaseModel):
    """Registered metadata for one git-tier sub-role (C-IS-03 §3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub_role: GitTierSubRole
    posture: SubRolePosture
    composition_with: tuple[ContractID, ...]
    """Spec-contract IDs this sub-role composes with (C-IS-03 §3 column).

    Empty for STATE_LEDGER_VIA_COMMIT_STREAM — its §3 composition column
    cites a sibling sub-role, not a contract ID.
    """


# --- Cross-sub-role consistency invariant constants (C-IS-03 §3) -----------

CO_RESIDENCE_ONE_REPO_HOSTS_ONE_HARNESS_STATE_LEDGER: Final[bool] = True
"""A git repository hosts at most one harness state-ledger (C-IS-03 §3)."""

CROSS_REPOSITORY_LEDGER_COMPOSITION: Final[bool] = False
"""Cross-repository state-ledger composition is out of scope at F2 (C-IS-03 §3)."""


# --- Registry population (spec C-IS-03 §3 sub-role table) ------------------

_SUB_ROLE_SPEC: dict[GitTierSubRole, tuple[SubRolePosture, tuple[ContractID, ...]]] = {
    GitTierSubRole.VERSIONING: (
        SubRolePosture.FOUNDATIONAL,
        (ContractID("C-IS-04"),),
    ),
    GitTierSubRole.STATE_LEDGER_VIA_COMMIT_STREAM: (
        SubRolePosture.FOUNDATIONAL,
        (),
    ),
    GitTierSubRole.JSONL_EVENT_LEDGER: (
        SubRolePosture.FOUNDATIONAL,
        (ContractID("C-IS-05"), ContractID("C-IS-06")),
    ),
    GitTierSubRole.SHADOW_GIT_CHECKPOINTING: (
        SubRolePosture.WORKLOAD_CLASS_OPT_IN,
        (ContractID("C-IS-08"),),
    ),
    GitTierSubRole.WORKTREE_ISOLATION: (
        SubRolePosture.WORKLOAD_CLASS_OPT_IN,
        (ContractID("C-IS-09"),),
    ),
}

GIT_TIER_SUB_ROLE_REGISTRY: Mapping[GitTierSubRole, GitTierSubRoleMetadata] = MappingProxyType(
    {
        sub_role: GitTierSubRoleMetadata(
            sub_role=sub_role,
            posture=posture,
            composition_with=composition_with,
        )
        for sub_role, (posture, composition_with) in _SUB_ROLE_SPEC.items()
    }
)
"""Immutable registry — one `GitTierSubRoleMetadata` per `GitTierSubRole`."""
