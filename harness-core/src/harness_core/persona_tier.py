"""Persona-tier ladder enumeration — U-CORE-01.

Implements C-AS-09 §9.4 (operator-policy override scope per persona tier) +
ADR-D5 v1.3 §1.5 (the F4 persona-tier ladder). Declares the closed 3-value
`PersonaTier` enum.

`PersonaTier` is a **cross-axis shared type** — consumed by the AS and OD axis
plans — and therefore resides in `harness-core` per `CLAUDE.md` §3.3 and the
R-series carrier map (disposition-1). A single carrier prevents `pyright`
treating the per-axis re-declarations as distinct types.

The ladder is **closed** at cardinality 3. Member string values are the
§9.4 / §11 persona-tier identifiers verbatim (lowercase-hyphen). The
SCREAMING_SNAKE_CASE member names are a Python-stack naming convention.

Authority: Implementation_Plan_Harness_Core_v1_1.md §2 U-CORE-01 (acceptance
criterion #2); Spec_Action_Surface_v1.md C-AS-09 §9.4; ADR-D5 v1.3 §1.5.
"""

from __future__ import annotations

from enum import StrEnum


class PersonaTier(StrEnum):
    """The 3 persona tiers (C-AS-09 §9.4 / ADR-D5 v1.3 §1.5, verbatim).

    Closed at cardinality 3 — the F4 persona-tier ladder. Member string values
    are the §9.4 override-scope table row labels byte-exact.
    """

    SOLO_DEVELOPER = "solo-developer"
    TEAM_BINDING = "team-binding"
    MULTI_TENANT_COMPLIANCE = "multi-tenant-compliance"
