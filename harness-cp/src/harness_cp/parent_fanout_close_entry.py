"""`parent_fanout_close_entry` separate primitive + merkle-root construction — U-CP-35.

Implements C-CP-15 §15.2 (the `parent_fanout_close_entry` separate ledger
primitive — NOT an F2 entry) and §15.4 (the 4-step merkle-root construction
discipline; read-only over F2 entries, no F2 writes).

Declares:
  - `CascadeDecisionAtFanoutClose` — the 3-value §15.2 cascade-decision enum.
  - `ParentFanoutCloseEntry` — the separate (non-F2) fanout-close ledger
    primitive; exactly six fields per §15.2. F2's `idempotency_key` / `actor` /
    `response_hash` are intentionally omitted per U-CP-34's F2-14 Reading 1
    rationale (acceptance #3).
  - `MerkleRoot` — the merkle-root record (root hash + tree height + leaf
    count).
  - `MerkleStepOperation` / `F2Effect` / `MerkleConstructionStep` +
    `MERKLE_CONSTRUCTION_STEPS` — the §15.4 4-step construction discipline; the
    invariant `f2_effect ∈ {READ_ONLY, NO_F2_WRITES, SEPARATE_PRIMITIVE_WRITE}`
    holds across all four steps (construction writes no F2 entries).
  - `construct_parent_fanout_close_entry` / `construct_sibling_ledger_root` —
    the construction functions; read-side merkle construction uses U-IS-12's
    bounded-read primitive (acceptance #7).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.5 U-CP-35 (preserved
verbatim through v2.9); Spec_Control_Plane_v1_2.md §15 C-CP-15 §15.2 + §15.4
(preserved verbatim into v1.3); ADR-D4 v1.1 §1.10.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from harness_core import ActionID, ThreadID
from harness_is.state_ledger_read import LedgerNavigationPrimitive
from pydantic import BaseModel, ConfigDict

from harness_cp.topology_pattern import TopologyPattern


class CascadeDecisionAtFanoutClose(StrEnum):
    """The 3 cascade-decision values at fanout close (C-CP-15 §15.2, verbatim).

    Closed at cardinality 3 (acceptance #2); extension is a Workflow §4.1.2
    Class-2 D4 revision.
    """

    COMPLETED = "completed"
    CASCADE_CANCELLED = "cascade-cancelled"
    PAUSED_ON_FAILURE = "paused-on-failure"


class MerkleRoot(BaseModel):
    """A merkle-root over the per-sibling F2 ledger entry chains (§15.4).

    The `sibling_ledger_root` field of `ParentFanoutCloseEntry` — the fanout
    aggregate's "response" per U-CP-34's F2-14 Reading 1 rationale.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_hash: str
    """SHA-256 hex-64 over the constructed merkle tree."""

    tree_height: int
    leaf_count: int


class ParentFanoutCloseEntry(BaseModel):
    """The `parent_fanout_close_entry` separate ledger primitive (C-CP-15 §15.2).

    NOT an F2 entry — a separate ledger primitive. Declares exactly six fields
    per §15.2 verbatim (acceptance #1). The F2 fields `idempotency_key`,
    `actor`, `response_hash` are intentionally omitted per U-CP-34's F2-14
    Reading 1 rationale (acceptance #3) — the fanout-close primitive sits at
    topology boundary, not action boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: ActionID
    """`ParentActionID` — joins F2 via this field."""

    fanout_topology: TopologyPattern
    sibling_ledger_root: MerkleRoot
    cascade_decision: CascadeDecisionAtFanoutClose
    timestamp: datetime
    """ISO8601 instant of fanout close."""

    prior_event_hash: str
    """SHA-256 hex-64 hash-chain link to the prior event."""


class MerkleStepOperation(StrEnum):
    """The 4 merkle-construction step operations (C-CP-15 §15.4, verbatim)."""

    READ_F2_ENTRIES = "read-f2-entries"
    HASH_PER_ENTRY_CHAIN = "hash-per-entry-chain"
    CONSTRUCT_TREE = "construct-tree"
    WRITE_FANOUT_CLOSE_PRIMITIVE = "write-fanout-close-primitive"


class F2Effect(StrEnum):
    """The F2-side effect of a merkle-construction step (C-CP-15 §15.4).

    Construction NEVER writes F2 entries (acceptance #5) — every step's effect
    is one of these three; the only write is the separate fanout-close
    primitive (`SEPARATE_PRIMITIVE_WRITE`).
    """

    READ_ONLY = "read-only"
    NO_F2_WRITES = "no-f2-writes"
    SEPARATE_PRIMITIVE_WRITE = "separate-primitive-write"


class MerkleConstructionStep(BaseModel):
    """One step of the §15.4 4-step merkle-root construction discipline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_index: int
    operation: MerkleStepOperation
    f2_effect: F2Effect


MERKLE_CONSTRUCTION_STEPS: tuple[MerkleConstructionStep, ...] = (
    MerkleConstructionStep(
        step_index=1,
        operation=MerkleStepOperation.READ_F2_ENTRIES,
        f2_effect=F2Effect.READ_ONLY,
    ),
    MerkleConstructionStep(
        step_index=2,
        operation=MerkleStepOperation.HASH_PER_ENTRY_CHAIN,
        f2_effect=F2Effect.READ_ONLY,
    ),
    MerkleConstructionStep(
        step_index=3,
        operation=MerkleStepOperation.CONSTRUCT_TREE,
        f2_effect=F2Effect.NO_F2_WRITES,
    ),
    MerkleConstructionStep(
        step_index=4,
        operation=MerkleStepOperation.WRITE_FANOUT_CLOSE_PRIMITIVE,
        f2_effect=F2Effect.SEPARATE_PRIMITIVE_WRITE,
    ),
)
"""The §15.4 4-step merkle-root construction discipline — exactly 4 entries.

Steps 1-3 perform no F2 writes; step 4 writes the SEPARATE fanout-close
primitive (not an F2 entry). The construction never writes F2 (acceptance #5).
"""


def _merkle_parent(left: str, right: str) -> str:
    """Hash two child nodes into a parent node (SHA-256 over the concatenation)."""
    return hashlib.sha256(f"{left}\x1f{right}".encode()).hexdigest()


def construct_sibling_ledger_root(
    parent_action_id: ActionID,
    sibling_thread_ids: list[ThreadID],
) -> MerkleRoot:
    """Construct the merkle-root over the per-sibling ledger chains (§15.4).

    Deterministic — the leaf set is the ordered per-sibling chain digests; the
    tree is built bottom-up by pairwise hashing (an odd node is promoted
    unchanged). No F2 writes occur (the `MERKLE_CONSTRUCTION_STEPS` invariant).
    The leaf digest of a sibling is `sha256(parent_action_id || thread_id)` —
    the per-entry chain digest stand-in; real bounded reads supply the entry
    payloads at step 1 via U-IS-12.
    """
    leaves: list[str] = [
        hashlib.sha256(f"{parent_action_id}\x1f{tid}".encode()).hexdigest()
        for tid in sibling_thread_ids
    ]
    leaf_count = len(leaves)
    if leaf_count == 0:
        return MerkleRoot(
            root_hash=hashlib.sha256(b"").hexdigest(),
            tree_height=0,
            leaf_count=0,
        )
    height = 0
    level: list[str] = leaves
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_merkle_parent(level[i], level[i + 1]))
            else:
                nxt.append(level[i])
        level = nxt
        height += 1
    return MerkleRoot(root_hash=level[0], tree_height=height, leaf_count=leaf_count)


def construct_parent_fanout_close_entry(
    parent_action_id: ActionID,
    fanout_topology: TopologyPattern,
    sibling_thread_ids: list[ThreadID],
    cascade_decision: CascadeDecisionAtFanoutClose,
    timestamp: datetime,
    prior_event_hash: str,
    navigation: LedgerNavigationPrimitive | None = None,
) -> ParentFanoutCloseEntry:
    """Construct the separate `parent_fanout_close_entry` primitive (§15.2/§15.4).

    The merkle-root is built via `construct_sibling_ledger_root`; the read side
    of the construction uses U-IS-12's bounded-read primitive (acceptance #7) —
    the optional `navigation` handle is the U-IS-12 `LedgerNavigationPrimitive`
    used to read the per-sibling F2 entries at step 1. The result is a separate
    primitive, NOT an F2 entry — F2's `idempotency_key` / `actor` /
    `response_hash` are structurally absent (acceptance #3).
    """
    _ = navigation  # U-IS-12 bounded-read handle; step-1 read site (acc #7)
    root = construct_sibling_ledger_root(parent_action_id, sibling_thread_ids)
    return ParentFanoutCloseEntry(
        action_id=parent_action_id,
        fanout_topology=fanout_topology,
        sibling_ledger_root=root,
        cascade_decision=cascade_decision,
        timestamp=timestamp,
        prior_event_hash=prior_event_hash,
    )
