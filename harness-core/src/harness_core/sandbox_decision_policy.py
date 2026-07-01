"""Sandbox decision policy empty-marker carrier — U-CORE-02.

Implements `Spec_Harness_Runtime_v1.md` v1.16 §3 C-RT-02 — the
`sandbox_decision_policy: SandboxDecisionPolicy | None` field carrier-home
commitment at `harness-core` per the Q1=C-i Class 1 fork resolution
2026-05-22 (`.harness/class_1_fork_sandbox_decision_policy_phantom_cite.md`).

The carrier is an **empty-marker**: a frozen Pydantic v2 BaseModel with NO
fields plus a `.default()` factory. Runtime spec v1.16 §3 C-RT-02 commits
only the field type + the `.default()` factory; no §14 contract specifies
any internal field set (§14.9.1 step 5 reads only
`sandbox.tier >= ToolContract.minimum_tier` and does NOT consume the policy
yet — dangling marker per spec v1.16 §"Adjacent defects surfaced" (i)).

Per X-AL-3 (no silent H_T design extension at Phase 7) +
`implementation-planner` SKILL.md §4 sub-discipline 4.4 (no spec extension):
no fields, no methods beyond `.default()`. Future operator-driven extension
(e.g. `tier_floor_overrides`) surfaces via spec extension + planner revision
pass.

Consumed by `harness-runtime` U-RT-71 (`RuntimeConfig.sandbox_decision_policy`
field type) + (future) U-RT-75 factory body.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SandboxDecisionPolicy(BaseModel):
    """Empty-marker carrier for operator-supplied sandbox decision policy.

    Frozen + `extra='forbid'` per workspace stack discipline. No fields
    declared at v1.2 of the harness-core plan — all instances are
    structurally identical.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def default(cls) -> SandboxDecisionPolicy:
        """Return the empty-marker instance used when no policy is configured."""
        return cls()
