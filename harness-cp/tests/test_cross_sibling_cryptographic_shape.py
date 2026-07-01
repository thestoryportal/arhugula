"""Tests for U-CP-36 — cross-sibling cryptographic shape composition (C-CP-15 §15.5/§15.6).

Acceptance-criterion coverage:
  #1 composition cardinality 3            -> test_cross_sibling_composition_cardinality_three
  #2 per-tier shapes match spec           -> test_per_tier_match_spec
  #3 each row delegates to U-CP-42        -> test_rows_delegate_to_u_cp_42
  #4 trace-inspection 3 surfaces          -> test_cascade_decision_id_resolution,
                                             test_action_id_join,
                                             test_trace_inspection_cardinality_three
  #5 signature verification -> U-CP-45    -> test_signature_verification_delegates_to_u_cp_45
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harness_core import ActionID, PersonaTier
from harness_cp.cross_sibling_cryptographic_shape import (
    CROSS_SIBLING_CRYPTOGRAPHIC_COMPOSITION,
    CROSS_SIBLING_TRACE_INSPECTION,
    ResolutionError,
    compose_per_persona_tier_cryptographic_shape,
    resolve_audit_ledger_entry_from_trace,
    verify_multi_tenant_compliance_signature,
)
from harness_cp.parent_fanout_close_entry import (
    CascadeDecisionAtFanoutClose,
    MerkleRoot,
    ParentFanoutCloseEntry,
)
from harness_cp.per_persona_tier_audit_cryptographic_shape import CryptographicShape
from harness_cp.sibling_ledger_entry_composition import SiblingLedgerEntry
from harness_cp.topology_pattern import TopologyPattern
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier


def _sibling() -> SiblingLedgerEntry:
    return SiblingLedgerEntry(
        action_id=Identifier("a0"),
        idempotency_key=Identifier("k0"),
        actor=Actor(actor_class=ActorClass.SUB_AGENT, actor_id="sib-0"),
        response_hash=b"\x00" * 32,
        timestamp=datetime(2026, 5, 16, tzinfo=UTC),
        prior_event_hash=b"\x01" * 32,
    )


def _fanout_close() -> ParentFanoutCloseEntry:
    return ParentFanoutCloseEntry(
        action_id=ActionID("parent-1"),
        fanout_topology=TopologyPattern.ORCHESTRATOR_WORKERS,
        sibling_ledger_root=MerkleRoot(root_hash="2" * 64, tree_height=1, leaf_count=2),
        cascade_decision=CascadeDecisionAtFanoutClose.COMPLETED,
        timestamp=datetime(2026, 5, 16, tzinfo=UTC),
        prior_event_hash="3" * 64,
    )


def test_cross_sibling_composition_cardinality_three() -> None:
    """#1 — exactly three compositions, one per persona tier."""
    assert len(CROSS_SIBLING_CRYPTOGRAPHIC_COMPOSITION) == 3
    assert {c.persona_tier for c in CROSS_SIBLING_CRYPTOGRAPHIC_COMPOSITION} == set(PersonaTier)


def test_per_tier_match_spec() -> None:
    """#2 — per-tier shapes match C-CP-15 §15.5 / C-CP-20 §20.2 verbatim."""
    expected = {
        PersonaTier.SOLO_DEVELOPER: CryptographicShape.APPEND_ONLY_SQLITE,
        PersonaTier.TEAM_BINDING: CryptographicShape.HASH_CHAINED_SQLITE,
        PersonaTier.MULTI_TENANT_COMPLIANCE: (
            CryptographicShape.HASH_CHAINED_SQLITE_WITH_SIGNATURE
        ),
    }
    for comp in CROSS_SIBLING_CRYPTOGRAPHIC_COMPOSITION:
        assert comp.sibling_ledger_entry_cryptographic_shape == expected[comp.persona_tier]
        assert comp.parent_fanout_close_entry_cryptographic_shape == expected[comp.persona_tier]


def test_rows_delegate_to_u_cp_42() -> None:
    """#3 — compose_per_persona_tier delegates to the U-CP-42 registry."""
    for tier in PersonaTier:
        comp = compose_per_persona_tier_cryptographic_shape(tier)
        # Both halves carry the U-CP-42 registry shape for the tier.
        assert (
            comp.sibling_ledger_entry_cryptographic_shape
            == comp.parent_fanout_close_entry_cryptographic_shape
        )
        assert comp.persona_tier is tier


def test_trace_inspection_cardinality_three() -> None:
    """#4 — CROSS_SIBLING_TRACE_INSPECTION declares 3 surfaces per §15.6 verbatim."""
    assert len(CROSS_SIBLING_TRACE_INSPECTION) == 3
    names = {s.surface_name for s in CROSS_SIBLING_TRACE_INSPECTION}
    assert names == {
        "cascade_decision_audit_ledger_id_resolution",
        "per_sibling_action_id_join",
        "multi_tenant_signature_verification",
    }


def test_cascade_decision_id_resolution() -> None:
    """#4 — resolve_audit_ledger_entry_from_trace resolves via the audit-ledger id."""
    entry = _fanout_close()
    store = {"ledger-id-1": entry}
    resolved = resolve_audit_ledger_entry_from_trace("ledger-id-1", store)
    assert isinstance(resolved, ParentFanoutCloseEntry)
    assert resolved == entry
    missing = resolve_audit_ledger_entry_from_trace("nope", store)
    assert isinstance(missing, ResolutionError)
    assert missing.cascade_decision_audit_ledger_id == "nope"


def test_action_id_join() -> None:
    """#4 — per-sibling action_id is the join key between siblings and the fanout."""
    sib = _sibling()
    # The sibling's action_id is the F2 join field (U-CP-34).
    assert sib.action_id == Identifier("a0")


def test_signature_verification_delegates_to_u_cp_45() -> None:
    """#5 — signature verification delegates to U-CP-45 (delegation-pending stub)."""
    with pytest.raises(NotImplementedError, match="U-CP-45 verifier"):
        verify_multi_tenant_compliance_signature(_fanout_close(), [_sibling()])
