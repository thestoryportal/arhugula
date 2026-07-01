"""Sub-agent default-downgrade rule (Tier-3 → Tier-1 ceiling) — U-CP-26.

Implements C-CP-12 §12.1 (default-downgrade rule per blast-radius tier).
Declares `SubAgentDefaultDowngrade`, the `DEFAULT_DOWNGRADE_RULE` constant, and
`compute_child_blast_radius_ceiling`.

C-CP-12 §12.1 commits the per-tool-class disposition rule per ADR-D4 v1.1 §1.5:
read-only `INHERIT`, local-mutation `INHERIT`, external-reversible
`DOWNGRADE_TO_ASK`, external-irreversible `REMOVE`. The plan re-expresses the
**default-downgrade** half as a child blast-radius *ceiling*: a sub-agent's
blast radius is capped at `READ_ONLY` (the §12.1 four-tier taxonomy's Tier-1)
regardless of the parent's tier — the conservative default that
`external-reversible`/`external-irreversible` parents drop their dispatched
sub-agents to. The all-parents → `READ_ONLY` ceiling table is a **plan-internal
characterization** of §12.1's disposition rule (the same sanctioned-field
pattern as U-CP-01's `cardinality`, operator decision Q-R4-2); it is the
*default* — operator override with audit is permitted at U-CP-27.

`BlastRadiusTier` is the AS-axis foundational substrate consumed cross-axis via
U-AS-01 (`harness_as`); its 4 members are the §12.1 four-tier taxonomy —
`READ_ONLY` (Tier-1), `LOCAL_MUTATION` (Tier-2), `EXTERNAL_REVERSIBLE`
(Tier-3), `EXTERNAL_IRREVERSIBLE` (Tier-4).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.4 U-CP-26 (preserved
verbatim through v2.6 — no body rewrite); Spec_Control_Plane_v1_2.md §12
C-CP-12 §12.1 (preserved verbatim into v1.3); ADR-D4 v1.1 §1.5.
"""

from __future__ import annotations

from harness_as import BlastRadiusTier
from pydantic import BaseModel, ConfigDict


class SubAgentDefaultDowngrade(BaseModel):
    """The default sub-agent blast-radius downgrade rule (C-CP-12 §12.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_blast_radius: BlastRadiusTier
    """The representative cross-trust-boundary parent tier the §12.1
    default-downgrade rule is anchored to (`EXTERNAL_REVERSIBLE` — Tier-3)."""

    child_ceiling: BlastRadiusTier
    """The default child blast-radius ceiling — `READ_ONLY` (Tier-1)."""

    rationale: str
    """C-CP-12 §12.1 rule narrative."""


DEFAULT_DOWNGRADE_RULE: SubAgentDefaultDowngrade = SubAgentDefaultDowngrade(
    parent_blast_radius=BlastRadiusTier.EXTERNAL_REVERSIBLE,
    child_ceiling=BlastRadiusTier.READ_ONLY,
    rationale=(
        "C-CP-12 §12.1 (ADR-D4 v1.1 §1.5) — a Tier-3 (external-reversible) "
        "parent dispatches sub-agents at a Tier-1 (read-only) ceiling by "
        "default: external-reversible tools become DOWNGRADE_TO_ASK and "
        "external-irreversible tools are REMOVE'd from the sub-agent registry. "
        "Default-downgrade; operator override with audit permitted at U-CP-27."
    ),
)
"""The default-downgrade rule: parent Tier-3 → child Tier-1 ceiling (§12.1)."""


def compute_child_blast_radius_ceiling(
    parent: BlastRadiusTier,
) -> BlastRadiusTier:
    """Compute the default child blast-radius ceiling for `parent`.

    Per the U-CP-26 plan characterization of C-CP-12 §12.1: every parent tier
    yields a `READ_ONLY` (Tier-1) child ceiling by default — Tier-1/Tier-2
    parents pass the floor through unchanged, Tier-3/Tier-4 parents are
    downgraded. The downgrade is the **default**; operator override with audit
    is applied at U-CP-27 monotonic-descent composition.
    """
    _ = parent  # the default ceiling is parent-independent (all → Tier-1).
    return BlastRadiusTier.READ_ONLY
