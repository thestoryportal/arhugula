"""Per-resumption observable behavior catalog — U-CP-20.

Implements C-CP-08 §8.3 (per-resumption observable behavior). Declares the
5-entry `PER_RESUMPTION_OBSERVABLE_BEHAVIOR` catalog — one entry per
`ResumptionKind` — the `ContinuityGuarantee` 5-value enum, and the F2-12
carry-forward closure declaration.

Each catalog entry records, for one resumption kind:
  - the `workflow.resumption` span it emits (per U-CP-10 lifecycle event class);
  - the required span attributes — `engine.class` + `engine.replay_disposition`
    per the U-CP-21 4-attribute `engine.*` namespace (v2.2 amendment: was
    `engine.resumption_kind` at v2.1; CP spec v1.3 §9.1 4-attribute canonical +
    ADR-D1 v1.2 §1.1.1/§1.1.2);
  - the F2 join path (`F2JoinKind` from U-CP-18), per-engine-class;
  - the observable continuity guarantee.

**F2-12 carry-forward — CLOSED (v2.2).** Per CP spec v1.3 §8.4 + §3.5 + §9.1
the F2-12 carry-forward is CLOSED at this unit: D1 v1.2 + D6 v1.2 closed the
three sub-scopes (span re-emission semantics; retry.attempt child-per-attempt;
trace-ingestion dedup composition). The cascade D1 v1.2 / D6 v1.2 -> ADD v1.3
-> PRD v1.1 -> CP spec v1.3 is complete.

Authority: Implementation_Plan_Control_Plane_v2_2.md §2.3 U-CP-20 (v2.2
amendment to acceptance #5 — F2-12 CLOSED; engine.replay_disposition required
attribute at acceptance #2); Spec_Control_Plane_v1_2.md §8 C-CP-08 §8.3
(preserved verbatim into v1.3); ADR-D1 v1.2 §1.1.1 + §1.1.2; ADR-D6 v1.2 §1.2.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.f2_substrate_join_discipline import F2JoinKind, f2_join_contract
from harness_cp.resumption_kind import RESUMPTION_KIND_BINDINGS, ResumptionKind


class ContinuityGuarantee(StrEnum):
    """The observable continuity guarantee per resumption kind (C-CP-08 §8.3).

    Closed at cardinality 5 — discriminates across the five resumption kinds."""

    EXACT_REPLAY = "exact_replay"
    CHECKPOINT_RESTORE = "checkpoint_restore"
    REPLAY_FROM_LEDGER = "replay_from_ledger"
    RECONVERGE = "reconverge"
    WAL_REPLAY = "wal_replay"


class PerResumptionObservableBehavior(BaseModel):
    """The observable behavior for one resumption kind (C-CP-08 §8.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resumption_kind: ResumptionKind
    emits_span: str
    """`workflow.resumption` — the lifecycle span emitted at resumption."""

    required_attributes: frozenset[str]
    """Includes `engine.class` + `engine.replay_disposition` per U-CP-21."""

    f2_join_path: F2JoinKind
    """Carried from U-CP-18 `EngineF2JoinContract`, per-engine-class."""

    observable_continuity: ContinuityGuarantee


# --- Resumption-kind -> continuity-guarantee mapping (§8.3) ------------------

_CONTINUITY_BY_KIND: dict[ResumptionKind, ContinuityGuarantee] = {
    ResumptionKind.ENGINE_REPLAY: ContinuityGuarantee.EXACT_REPLAY,
    ResumptionKind.SAVE_POINT_RESUME: ContinuityGuarantee.CHECKPOINT_RESTORE,
    ResumptionKind.JOURNAL_RESUME: ContinuityGuarantee.REPLAY_FROM_LEDGER,
    ResumptionKind.RECONCILER_CONVERGE: ContinuityGuarantee.RECONVERGE,
    ResumptionKind.SEGMENT_REPLAY: ContinuityGuarantee.WAL_REPLAY,
}

# Resumption kind -> the engine class it binds to (inverse of the U-CP-19 1:1
# RESUMPTION_KIND_BINDINGS), used to resolve the per-engine-class F2 join path.
_ENGINE_BY_KIND = {b.resumption_kind: b.engine_class for b in RESUMPTION_KIND_BINDINGS}

_REQUIRED_RESUMPTION_ATTRIBUTES: frozenset[str] = frozenset(
    {"workflow.id", "engine.class", "engine.replay_disposition", "idempotency_key"}
)

PER_RESUMPTION_OBSERVABLE_BEHAVIOR: tuple[PerResumptionObservableBehavior, ...] = tuple(
    PerResumptionObservableBehavior(
        resumption_kind=rk,
        emits_span="workflow.resumption",
        required_attributes=_REQUIRED_RESUMPTION_ATTRIBUTES,
        f2_join_path=f2_join_contract(_ENGINE_BY_KIND[rk]).join_kind,
        observable_continuity=_CONTINUITY_BY_KIND[rk],
    )
    for rk in ResumptionKind
)
"""The 5 per-resumption observable behaviors — one per `ResumptionKind`
(C-CP-08 §8.3 verbatim)."""


F2_12_CARRY_FORWARD_CLOSED: bool = True
"""F2-12 carry-forward is CLOSED at this unit (v2.2 amendment) — the
D1 v1.2 / D6 v1.2 -> ADD v1.3 -> PRD v1.1 -> CP spec v1.3 cascade is complete."""
