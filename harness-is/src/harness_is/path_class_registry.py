"""Path-class registry schema — U-IS-01.

Implements C-IS-01 §1 (canonical filesystem path contract). Declares the
4-class `PathClass` enum, the per-class metadata schema, and the populated
`PATH_CLASS_REGISTRY`.

Authority: Implementation_Plan_Information_Substrate_v2_1.md §2 U-IS-01
(preserved verbatim at v2.2); Spec_Information_Substrate_v1.md §1 C-IS-01;
ADR-F2 v1.2 §Decision (filesystem + git canonical state substrate).

Two declared resolutions of U-IS-01 under-specifications (operator-approved):
  ① `ResidenceContract` is referenced but not defined in the U-IS-01
     signature block — resolved minimally as a single-field record
     carrying the spec C-IS-01 §1 residence-contract prose verbatim. No
     invented structure (X-AL-3).
  ② `stability_invariant` per-class values are not differentiated by the
     spec — populated uniformly from the spec's contract-level "Stability
     invariants" section. Not constrained by any U-IS-01 acceptance
     criterion.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict


class PathClass(StrEnum):
    """The 4 canonical artifact classes (C-IS-01 §1)."""

    SKILLS = "SKILLS"
    PROMPTS = "PROMPTS"
    ROUTING_MANIFEST = "ROUTING_MANIFEST"
    STATE_LEDGER = "STATE_LEDGER"


class ResidenceContract(BaseModel):
    """Path-residence contract for a path class (C-IS-01 §1 table column).

    Minimal resolution of the U-IS-01 referenced-but-undefined
    `ResidenceContract` type (flag ①): carries the spec's residence-contract
    prose verbatim, with no invented sub-structure.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: str
    """Verbatim residence-contract prose from spec C-IS-01 §1."""


class StabilityInvariant(BaseModel):
    """Path-stability invariant for a path class (C-IS-01 §1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_canonical: bool
    """Path identifier is stable across all runs of the same workflow class."""

    workflow_class_varying: bool
    """Path identifier MAY vary across workflow classes."""

    deployment_surface_varying: bool
    """Path identifier MAY vary across deployment surfaces (ADD §3 OD-2.A)."""


class VisibilitySurface(BaseModel):
    """Visibility surface for a path class (C-IS-01 §1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_readable_during_run: bool
    """Filesystem-readable to the production-time operator during a run."""

    maintainer_readable_post_run: bool
    """Filesystem-readable to the downstream maintainer after termination."""

    in_memory_only: bool
    """Negative constraint — false for all 4 classes (C-IS-01 §1)."""


class PathClassMetadata(BaseModel):
    """Registered metadata for one path class (C-IS-01 §1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path_class: PathClass
    residence_contract: ResidenceContract
    stability_invariant: StabilityInvariant
    visibility_surface: VisibilitySurface


# --- Registry population (spec C-IS-01 §1) ---------------------------------

# Visibility surface is identical across all 4 classes per the C-IS-01 §1
# "Visibility surface contract": readable to operator during run, readable
# to maintainer post-run, never in-memory-only.
_ALL_CLASS_VISIBILITY = VisibilitySurface(
    operator_readable_during_run=True,
    maintainer_readable_post_run=True,
    in_memory_only=False,
)

# Stability invariant — flag ②: uniform population from the spec's
# contract-level "Stability invariants" section. The contract commits only
# that some stable path exists per (workflow class, deployment surface) cell.
_ALL_CLASS_STABILITY = StabilityInvariant(
    workflow_canonical=True,
    workflow_class_varying=True,
    deployment_surface_varying=True,
)

_RESIDENCE_CONTRACTS: dict[PathClass, str] = {
    PathClass.SKILLS: (
        "SKILL.md-as-directory (folder-with-SKILL.md), one folder per skill; "
        "folder name is the skill identifier; SKILL.md frontmatter carries "
        "name + description (required) and allowed-tools + "
        "disable-model-invocation + license + dependencies (optional) per "
        "agentskills.io open standard ratified 18 Dec 2025."
    ),
    PathClass.PROMPTS: (
        "Plain-text-file-in-git; one file per prompt artifact; prompts loaded "
        "as stable static-prefix content per Cluster 2 V2 §1.2 prompt-cache "
        "hierarchy."
    ),
    PathClass.ROUTING_MANIFEST: (
        "Single file in git per ADR-F1 v1.2 Consequences §(a) "
        '"manifest-layer model assignment as auditable default at every call '
        'site"; manifest declares per-agent-role + per-workflow-class + '
        "per-step model assignments."
    ),
    PathClass.STATE_LEDGER: (
        "Two-mode composite per C-IS-03 §3 (git commit stream as one mode; "
        "JSONL event ledger as second mode); JSONL ledger file at "
        "workflow-canonical path; commit stream at workflow-bound git "
        "repository."
    ),
}

PATH_CLASS_REGISTRY: Mapping[PathClass, PathClassMetadata] = MappingProxyType(
    {
        path_class: PathClassMetadata(
            path_class=path_class,
            residence_contract=ResidenceContract(contract=contract),
            stability_invariant=_ALL_CLASS_STABILITY,
            visibility_surface=_ALL_CLASS_VISIBILITY,
        )
        for path_class, contract in _RESIDENCE_CONTRACTS.items()
    }
)
"""Immutable registry — one `PathClassMetadata` per `PathClass` (C-IS-01 §1)."""
