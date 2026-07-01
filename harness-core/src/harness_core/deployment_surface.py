"""Deployment-surface enumeration — U-CORE-01.

Implements C-AS-09 §9.1 (the 12-cell sandbox provider matrix over the
deployment-surface and blast-radius-tier axes — the deployment-surface axis).
Declares the closed 3-value `DeploymentSurface` enum.

`DeploymentSurface` is a **cross-axis shared type** — consumed by the IS, AS,
and OD axis plans — and therefore resides in `harness-core` per `CLAUDE.md`
§3.3 and the R-series carrier map (disposition-1). A single carrier prevents
`pyright` treating the per-axis re-declarations as distinct types.

The taxonomy is **closed** at cardinality 3 — the deployment-surface axis of
the C-AS-09 §9.1 matrix. Member string values are the §9.1 matrix row labels
verbatim (lowercase-hyphen). The SCREAMING_SNAKE_CASE member names are a
Python-stack naming convention.

Authority: Implementation_Plan_Harness_Core_v1_1.md §2 U-CORE-01 (acceptance
criterion #1); Spec_Action_Surface_v1.md C-AS-09 §9.1; ADR-D2 v1.2 §1.1.
"""

from __future__ import annotations

from enum import StrEnum


class DeploymentSurface(StrEnum):
    """The 3 deployment surfaces (C-AS-09 §9.1, verbatim).

    Closed at cardinality 3 — the deployment-surface axis of the §9.1 12-cell
    matrix. Member string values are the §9.1 row labels byte-exact.
    """

    LOCAL_DEVELOPMENT = "local-development"
    SELF_HOSTED_SERVER = "self-hosted-server"
    MANAGED_CLOUD = "managed-cloud"
