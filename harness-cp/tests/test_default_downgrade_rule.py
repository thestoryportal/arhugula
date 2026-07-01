"""Tests for U-CP-26 — sub-agent default-downgrade rule (C-CP-12 §12.1).

Acceptance-criterion coverage:
  #1 Tier-3 -> Tier-1 ceiling   -> test_default_downgrade_rule_per_spec
  #2 four-row ceiling table     -> test_tier_3_parent_yields_tier_1_child,
                                   test_compute_ceiling_four_row_table
  #3 default (override at U-CP-27) -> test_default_with_override_permitted
  #4 applies at sub-agent dispatch -> structural (pre-condition for U-CP-27)

Note: C-CP-12 §12.1 commits the per-tool-class disposition rule (read-only /
local-mutation INHERIT; external-reversible DOWNGRADE_TO_ASK; external-
irreversible REMOVE). The 4-row "all parents -> READ_ONLY ceiling" table is a
U-CP-26 plan-internal characterization of that rule — not a verbatim spec
table — hence the test names below do not claim "spec verbatim" for the table.
"""

from __future__ import annotations

from harness_as import BlastRadiusTier
from harness_cp.default_downgrade_rule import (
    DEFAULT_DOWNGRADE_RULE,
    compute_child_blast_radius_ceiling,
)


def test_default_downgrade_rule_per_spec() -> None:
    """Acceptance #1 — parent Tier-3 (external-reversible) → child Tier-1."""
    assert DEFAULT_DOWNGRADE_RULE.parent_blast_radius == (BlastRadiusTier.EXTERNAL_REVERSIBLE)
    assert DEFAULT_DOWNGRADE_RULE.child_ceiling == BlastRadiusTier.READ_ONLY
    assert "§12.1" in DEFAULT_DOWNGRADE_RULE.rationale


def test_tier_3_parent_yields_tier_1_child() -> None:
    """Acceptance #2 — Tier-3 parent → Tier-1 (READ_ONLY) child ceiling."""
    assert (
        compute_child_blast_radius_ceiling(BlastRadiusTier.EXTERNAL_REVERSIBLE)
        == BlastRadiusTier.READ_ONLY
    )


def test_compute_ceiling_four_row_table() -> None:
    """Acceptance #2 — every parent tier yields a READ_ONLY child ceiling.

    Plan-internal characterization of §12.1 — all four `BlastRadiusTier`
    parents default-downgrade to a Tier-1 child ceiling.
    """
    for parent in BlastRadiusTier:
        assert compute_child_blast_radius_ceiling(parent) == (BlastRadiusTier.READ_ONLY)


def test_default_with_override_permitted() -> None:
    """Acceptance #3 — the rule is the *default*; override audited at U-CP-27.

    The rationale records the default-with-override semantics; U-CP-26 itself
    declares no override surface (that is U-CP-27's monotonic-descent unit).
    """
    assert "default" in DEFAULT_DOWNGRADE_RULE.rationale.lower()
    assert "U-CP-27" in DEFAULT_DOWNGRADE_RULE.rationale
