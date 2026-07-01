"""Tests for U-CP-27 — sub-agent gate-level monotonic descent (C-CP-12).

Acceptance-criterion coverage:
  #1 child gate-level <= parent  -> test_child_gate_level_monotonic_descent
  #2 child sandbox-tier >= parent -> test_child_sandbox_tier_monotonic_ascent
  #3 default-downgrade ceiling   -> test_default_downgrade_applied
  #3 override permitted          -> test_override_applied_flag
  #4 cross-deployment monotonic  -> test_monotonic_ascent_rejects_descent
  #5 audit entry shape           -> test_audit_entry_cp_audit_ledger_entry
  #5 response_hash brief         -> test_response_hash_brief_canonicalize
  #6 parent_gate consumed        -> test_parent_gate_level_consumed_not_recomputed
"""

from __future__ import annotations

import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_core import ActionID
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry
from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
    compute_brief_summary_hash,
)
from harness_cp.sub_agent_gate_level_descent import (
    GateOverride,
    SubAgentGateLevelDescent,
    assert_monotonic_ascent,
    assert_monotonic_descent,
    dispatch_sub_agent,
    emit_sub_agent_dispatch_audit,
    sub_agent_dispatch_response_hash,
)


def _brief() -> SubAgentBrief:
    boundaries = ClearTaskBoundaries(
        in_scope=("a",), out_of_scope=("b",), termination_criteria=("c",)
    )
    of = OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT)

    def _build(h: str) -> SubAgentBrief:
        return SubAgentBrief(
            objective="o",
            output_format=of,
            guidance="g",
            task_boundaries=boundaries,
            summary_hash=h,
        )

    return _build(compute_brief_summary_hash(_build("0" * 64)))


def test_child_gate_level_monotonic_descent() -> None:
    """#1 — child_gate_level <= parent_gate_level; ascent prohibited."""
    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.ASK,
        SandboxTier.TIER_2_CONTAINER,
        _brief(),
        None,
    )
    rank = {GateLevel.AUTO: 0, GateLevel.ASK: 1, GateLevel.DENY: 2}
    assert rank[descent.child_gate_level] <= rank[descent.parent_gate_level]
    # Ascent raises.
    with pytest.raises(ValueError, match="monotonic-descent"):
        assert_monotonic_descent(GateLevel.AUTO, GateLevel.DENY)
    # Equality and descent are admitted.
    assert_monotonic_descent(GateLevel.DENY, GateLevel.AUTO)
    assert_monotonic_descent(GateLevel.ASK, GateLevel.ASK)


def test_child_sandbox_tier_monotonic_ascent() -> None:
    """#2 — child_sandbox_tier >= parent_sandbox_tier per C-AS-11."""
    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.AUTO,
        SandboxTier.TIER_3_MICROVM,
        _brief(),
        None,
    )
    rank = {
        SandboxTier.TIER_1_PROCESS: 0,
        SandboxTier.TIER_2_CONTAINER: 1,
        SandboxTier.TIER_3_MICROVM: 2,
        SandboxTier.TIER_4_FULL_VM: 3,
    }
    assert rank[descent.child_sandbox_tier] >= rank[descent.parent_sandbox_tier]


def test_monotonic_ascent_rejects_descent() -> None:
    """#4 — sandbox-tier downgrade is structurally rejected."""
    with pytest.raises(ValueError, match="monotonic-ascension"):
        assert_monotonic_ascent(SandboxTier.TIER_3_MICROVM, SandboxTier.TIER_1_PROCESS)
    assert_monotonic_ascent(SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_3_MICROVM)


def test_default_downgrade_applied() -> None:
    """#3 — child blast-radius ceiling from the U-CP-26 default-downgrade rule."""
    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.AUTO,
        SandboxTier.TIER_4_FULL_VM,
        _brief(),
        None,
    )
    # The §12.1 default-downgrade yields a READ_ONLY child ceiling.
    assert descent.child_blast_radius_ceiling is BlastRadiusTier.READ_ONLY


def test_override_applied_flag() -> None:
    """#3 — operator override sets override_applied; GateOverride is opaque."""
    override: GateOverride = {"scope": "external-reversible", "persona": "team"}
    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.ASK,
        SandboxTier.TIER_2_CONTAINER,
        _brief(),
        override,
    )
    assert descent.override_applied is True
    no_override = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.ASK,
        SandboxTier.TIER_2_CONTAINER,
        _brief(),
        None,
    )
    assert no_override.override_applied is False
    assert no_override.override_audit_ref is None


def test_audit_entry_cp_audit_ledger_entry() -> None:
    """#5 — emit_sub_agent_dispatch_audit returns a CPAuditLedgerEntry."""
    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.ASK,
        SandboxTier.TIER_2_CONTAINER,
        _brief(),
        None,
    )
    entry = emit_sub_agent_dispatch_audit(ActionID("p0"), descent, "0" * 64)
    assert isinstance(entry, CPAuditLedgerEntry)
    assert entry.action_id == ActionID("p0||sub-agent")
    # An approve-class dispatch carries no response-specific hash fields.
    assert entry.edited_proposal_hash is None
    assert entry.rejection_reason_hash is None
    assert entry.response_text_hash is None


def test_audit_entry_timestamp_is_iso_8601_per_v1_28() -> None:
    """CP spec v1.28 §16.5.6.X — `timestamp` is non-tier-conditional per
    C-CP-16 §16.2 + ADR-D5 §1.4. Pre-v1.28 `timestamp=""` placeholder closed."""
    from datetime import datetime

    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.ASK,
        SandboxTier.TIER_2_CONTAINER,
        _brief(),
        None,
    )
    entry = emit_sub_agent_dispatch_audit(ActionID("p0"), descent, "0" * 64)
    assert entry.timestamp != ""
    parsed = datetime.fromisoformat(entry.timestamp)
    assert parsed.tzinfo is not None, "timestamp MUST carry UTC tzinfo"


def test_response_hash_brief_canonicalize() -> None:
    """#5 — response_hash = sha256(canonicalize(SubAgentBrief))."""
    brief = _brief()
    assert sub_agent_dispatch_response_hash(brief) == compute_brief_summary_hash(brief)


def test_parent_gate_level_consumed_not_recomputed() -> None:
    """#6 — parent_gate_level is consumed verbatim, not recomputed."""
    descent = dispatch_sub_agent(
        ActionID("p0"),
        GateLevel.DENY,
        SandboxTier.TIER_1_PROCESS,
        _brief(),
        None,
    )
    assert descent.parent_gate_level is GateLevel.DENY
    assert set(SubAgentGateLevelDescent.model_fields) == {
        "parent_gate_level",
        "parent_sandbox_tier",
        "child_blast_radius_ceiling",
        "child_sandbox_tier",
        "child_gate_level",
        "override_applied",
        "override_audit_ref",
    }
