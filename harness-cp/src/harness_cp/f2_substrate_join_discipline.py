"""F2 substrate join discipline — U-CP-18.

Implements C-CP-08 §8.2 — the R-CP-07-satisfying F2 substrate join contract.
Declares `F2JoinKind` (3-value enum), `EngineF2JoinContract`, the 5-entry
`ENGINE_F2_JOIN_CONTRACTS` table (one per `EngineClass`), and the
`f2_join_contract` lookup.

Per §8.2, the join discipline preserves the F2 six-field state-ledger entry
shape regardless of join kind: `event-sourced-replay` and `reconciler-loop`
engines own their internal substrate and adapt it to expose the F2 shape at
their read surface; `save-point-checkpoint`, `pure-pattern-no-engine`, and
`WAL-segment` engines compose above the harness overlay ledger directly.

The `read_contract` / `write_contract` / `chain_construction` /
`idempotency_key_path` fields are **string delegation pointers** to the
cross-axis IS export surfaces — the IS axis owns the F2 read/write primitive,
the hash-chain construction discipline, and the idempotency-key join. This
unit declares the per-engine-class binding to those primitives; it does not
re-implement them (cross-axis edges U-IS-07 / U-IS-09 / U-IS-12).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-18 (preserved
verbatim through v2.6 — no body rewrite); Spec_Control_Plane_v1_2.md §8
C-CP-08 §8.2 (preserved verbatim into v1.3); ADR-F2 v1.2; ADR-F3 v1.1.
Cross-axis IS substrate consumed: C-IS-10 §10.1 (U-IS-07 state-ledger entry
shape), §10.3 (U-IS-09 hash-chain construction), §10.2 (U-IS-12 idempotency-
key join).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.engine_class import EngineClass


class F2JoinKind(StrEnum):
    """The 3 F2 substrate join kinds (C-CP-08 §8.2 verbatim discrimination).

    Closed at cardinality 3 — the §8.2 verbatim discrimination of how an
    engine class joins the F2 state-ledger substrate.
    """

    ENGINE_NATIVE_LEDGER = "engine-native-ledger"
    """Event-sourced-replay engines own their ledger; the F2 shape is exposed
    at the read surface by adapting the engine's native substrate."""

    HARNESS_OVERLAY_LEDGER = "harness-overlay-ledger"
    """Save-point/pure-pattern/WAL engines compose above the harness ledger."""

    CRD_RECONCILER_LEDGER = "crd-reconciler-ledger"
    """Reconciler-loop engines own a CRD-resident ledger; the F2 shape is
    exposed at the read surface by adapting the CRD substrate."""


class EngineF2JoinContract(BaseModel):
    """The F2 substrate join contract for one engine class (C-CP-08 §8.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: EngineClass
    join_kind: F2JoinKind

    read_contract: str
    """Delegation pointer — the U-IS-07 F2 read primitive (C-IS-10 §10.1)."""

    write_contract: str
    """Delegation pointer — the U-IS-07 + U-IS-09 write composition."""

    chain_construction: str
    """Delegation pointer — the U-IS-09 `prior_event_hash` hash-chain
    construction discipline (C-IS-10 §10.3)."""

    idempotency_key_path: str
    """Delegation pointer — the U-IS-12 idempotency-key join (C-IS-10 §10.2)."""


# --- Per-engine-class join-kind mapping (C-CP-08 §8.2 acceptance #2) --------

_JOIN_KIND_BY_CLASS: dict[EngineClass, F2JoinKind] = {
    EngineClass.EVENT_SOURCED_REPLAY: F2JoinKind.ENGINE_NATIVE_LEDGER,
    EngineClass.SAVE_POINT_CHECKPOINT: F2JoinKind.HARNESS_OVERLAY_LEDGER,
    EngineClass.PURE_PATTERN_NO_ENGINE: F2JoinKind.HARNESS_OVERLAY_LEDGER,
    EngineClass.RECONCILER_LOOP: F2JoinKind.CRD_RECONCILER_LEDGER,
    EngineClass.WAL_SEGMENT: F2JoinKind.HARNESS_OVERLAY_LEDGER,
}
"""The §8.2 per-engine-class join-kind discrimination — all 5 classes."""

# Delegation-pointer strings — the IS-export surfaces this contract binds to.
_READ_DELEGATE = "U-IS-07 STATE_LEDGER_ENTRY_SHAPE_EXPORT read primitive (C-IS-10 §10.1)"
_WRITE_DELEGATE = (
    "U-IS-07 STATE_LEDGER_ENTRY_SHAPE_EXPORT write primitive composed with "
    "U-IS-09 HASH_CHAIN_CONSTRUCTION_DISCIPLINE_EXPORT (C-IS-10 §10.1 + §10.3)"
)
_CHAIN_DELEGATE = (
    "U-IS-09 HASH_CHAIN_CONSTRUCTION_DISCIPLINE_EXPORT prior_event_hash "
    "construction (C-IS-10 §10.3)"
)
_IDEMPOTENCY_DELEGATE = "U-IS-12 IDEMPOTENCY_KEY_JOIN_EXPORT join key (C-IS-10 §10.2)"


ENGINE_F2_JOIN_CONTRACTS: tuple[EngineF2JoinContract, ...] = tuple(
    EngineF2JoinContract(
        engine_class=ec,
        join_kind=_JOIN_KIND_BY_CLASS[ec],
        read_contract=_READ_DELEGATE,
        write_contract=_WRITE_DELEGATE,
        chain_construction=_CHAIN_DELEGATE,
        idempotency_key_path=_IDEMPOTENCY_DELEGATE,
    )
    for ec in EngineClass
)
"""The 5 per-engine-class F2 join contracts (C-CP-08 §8.2) — one per
`EngineClass`. This is the R-CP-07-satisfying contract per §8.2."""

_CONTRACT_BY_CLASS: dict[EngineClass, EngineF2JoinContract] = {
    c.engine_class: c for c in ENGINE_F2_JOIN_CONTRACTS
}


def f2_join_contract(engine: EngineClass) -> EngineF2JoinContract:
    """Return the F2 substrate join contract for `engine` (C-CP-08 §8.2).

    Total over `EngineClass` — `ENGINE_F2_JOIN_CONTRACTS` carries one entry per
    class, so every engine class resolves a contract.
    """
    return _CONTRACT_BY_CLASS[engine]
