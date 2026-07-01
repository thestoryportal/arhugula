"""Tests for U-CP-35 — parent_fanout_close_entry primitive + merkle construction.

Acceptance-criterion coverage (C-CP-15 §15.2/§15.4):
  #1 ParentFanoutCloseEntry six fields    -> test_parent_fanout_close_entry_six_fields
  #2 CascadeDecision cardinality 3        -> test_cascade_decision_cardinality_three
  #3 F2 fields omitted                    -> test_omits_idempotency_key,
                                             test_omits_actor, test_omits_response_hash
  #4 MERKLE_CONSTRUCTION_STEPS cardinality 4 -> test_merkle_steps_cardinality_four
  #5 no F2 writes invariant               -> test_merkle_construction_no_f2_writes
  #6 T-perm-2 stands                      -> test_t_perm_2_stands
  #7 merkle read delegates to U-IS-12     -> test_merkle_read_delegates_to_u_is_12
"""

from __future__ import annotations

from datetime import UTC, datetime

from harness_core import ActionID, ThreadID
from harness_cp.parent_fanout_close_entry import (
    MERKLE_CONSTRUCTION_STEPS,
    CascadeDecisionAtFanoutClose,
    F2Effect,
    MerkleConstructionStep,
    MerkleRoot,
    MerkleStepOperation,
    ParentFanoutCloseEntry,
    construct_parent_fanout_close_entry,
    construct_sibling_ledger_root,
)
from harness_cp.topology_pattern import TopologyPattern


def _entry() -> ParentFanoutCloseEntry:
    return construct_parent_fanout_close_entry(
        parent_action_id=ActionID("parent-1"),
        fanout_topology=TopologyPattern.ORCHESTRATOR_WORKERS,
        sibling_thread_ids=[ThreadID("t0"), ThreadID("t1"), ThreadID("t2")],
        cascade_decision=CascadeDecisionAtFanoutClose.COMPLETED,
        timestamp=datetime(2026, 5, 16, tzinfo=UTC),
        prior_event_hash="0" * 64,
    )


def test_parent_fanout_close_entry_six_fields() -> None:
    """#1 — ParentFanoutCloseEntry declares exactly six fields per §15.2."""
    fields = set(ParentFanoutCloseEntry.model_fields)
    assert fields == {
        "action_id",
        "fanout_topology",
        "sibling_ledger_root",
        "cascade_decision",
        "timestamp",
        "prior_event_hash",
    }
    assert len(fields) == 6


def test_cascade_decision_cardinality_three() -> None:
    """#2 — CascadeDecisionAtFanoutClose declares exactly three values."""
    assert len(CascadeDecisionAtFanoutClose) == 3
    assert {m.value for m in CascadeDecisionAtFanoutClose} == {
        "completed",
        "cascade-cancelled",
        "paused-on-failure",
    }


def test_omits_idempotency_key() -> None:
    """#3 — F2 `idempotency_key` intentionally absent from the primitive."""
    assert "idempotency_key" not in ParentFanoutCloseEntry.model_fields


def test_omits_actor() -> None:
    """#3 — F2 `actor` intentionally absent from the primitive."""
    assert "actor" not in ParentFanoutCloseEntry.model_fields


def test_omits_response_hash() -> None:
    """#3 — F2 `response_hash` intentionally absent (sibling_ledger_root is the response)."""
    assert "response_hash" not in ParentFanoutCloseEntry.model_fields
    assert "sibling_ledger_root" in ParentFanoutCloseEntry.model_fields


def test_merkle_steps_cardinality_four() -> None:
    """#4 — MERKLE_CONSTRUCTION_STEPS declares exactly 4 steps per §15.4 verbatim."""
    assert len(MERKLE_CONSTRUCTION_STEPS) == 4
    assert [s.operation for s in MERKLE_CONSTRUCTION_STEPS] == [
        MerkleStepOperation.READ_F2_ENTRIES,
        MerkleStepOperation.HASH_PER_ENTRY_CHAIN,
        MerkleStepOperation.CONSTRUCT_TREE,
        MerkleStepOperation.WRITE_FANOUT_CLOSE_PRIMITIVE,
    ]
    assert [s.step_index for s in MERKLE_CONSTRUCTION_STEPS] == [1, 2, 3, 4]


def test_merkle_construction_no_f2_writes() -> None:
    """#5 — every step's f2_effect ∈ {READ_ONLY, NO_F2_WRITES, SEPARATE_PRIMITIVE_WRITE}."""
    allowed = {
        F2Effect.READ_ONLY,
        F2Effect.NO_F2_WRITES,
        F2Effect.SEPARATE_PRIMITIVE_WRITE,
    }
    for step in MERKLE_CONSTRUCTION_STEPS:
        assert step.f2_effect in allowed
    # Steps 1-2 read-only; step 3 no writes; step 4 the SEPARATE primitive write.
    assert MERKLE_CONSTRUCTION_STEPS[0].f2_effect is F2Effect.READ_ONLY
    assert MERKLE_CONSTRUCTION_STEPS[1].f2_effect is F2Effect.READ_ONLY
    assert MERKLE_CONSTRUCTION_STEPS[2].f2_effect is F2Effect.NO_F2_WRITES
    assert MERKLE_CONSTRUCTION_STEPS[3].f2_effect is F2Effect.SEPARATE_PRIMITIVE_WRITE
    # No step has an F2-mutating effect — only the read-only / no-write /
    # separate-primitive effects are present (none writes an F2 entry).
    assert all(
        s.f2_effect
        in {F2Effect.READ_ONLY, F2Effect.NO_F2_WRITES, F2Effect.SEPARATE_PRIMITIVE_WRITE}
        for s in MERKLE_CONSTRUCTION_STEPS
    )


def test_t_perm_2_stands() -> None:
    """#6 — T-perm-2 F2-layer resolution stands: the primitive is not an F2 entry.

    The fanout-close primitive is a separate ledger primitive; its construction
    requires no F2 schema revision (ADR-D4 v1.1 §1.10).
    """
    entry = _entry()
    assert isinstance(entry, ParentFanoutCloseEntry)
    # It is NOT a StateLedgerEntry subclass — a separate primitive.
    from harness_is.state_ledger_entry_schema import StateLedgerEntry

    assert not issubclass(ParentFanoutCloseEntry, StateLedgerEntry)


def test_merkle_read_delegates_to_u_is_12() -> None:
    """#7 — construct accepts a U-IS-12 LedgerNavigationPrimitive for the read side."""
    from harness_is.state_ledger_read import LedgerNavigationPrimitive

    sig = construct_parent_fanout_close_entry.__annotations__
    assert "navigation" in sig
    # The annotation references the U-IS-12 navigation primitive.
    assert LedgerNavigationPrimitive.__name__ in str(sig["navigation"])


def test_merkle_root_deterministic() -> None:
    """Construction is deterministic given inputs."""
    a = construct_sibling_ledger_root(
        ActionID("p"), [ThreadID("t0"), ThreadID("t1"), ThreadID("t2")]
    )
    b = construct_sibling_ledger_root(
        ActionID("p"), [ThreadID("t0"), ThreadID("t1"), ThreadID("t2")]
    )
    assert a == b
    assert isinstance(a, MerkleRoot)
    assert a.leaf_count == 3
    assert a.tree_height == 2  # 3 leaves -> 2 -> 1 (height 2)


def test_merkle_root_single_and_empty() -> None:
    """Merkle root handles single-leaf and empty leaf sets."""
    single = construct_sibling_ledger_root(ActionID("p"), [ThreadID("t0")])
    assert single.leaf_count == 1
    assert single.tree_height == 0
    empty = construct_sibling_ledger_root(ActionID("p"), [])
    assert empty.leaf_count == 0
    assert empty.tree_height == 0


def test_merkle_construction_step_model_frozen() -> None:
    """MerkleConstructionStep is a frozen extra-forbid model."""
    step = MerkleConstructionStep(
        step_index=1,
        operation=MerkleStepOperation.READ_F2_ENTRIES,
        f2_effect=F2Effect.READ_ONLY,
    )
    assert step.model_config.get("frozen") is True
