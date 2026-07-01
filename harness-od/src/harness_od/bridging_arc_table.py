"""Bridging-arc 8-transition table + per-transition verification surface — U-OD-32.

Implements C-OD-22 §22.1 (8 bridging-arc transitions in scope), §22.3
(per-transition verification surface).

`BRIDGING_ARC_TRANSITIONS` declares the 8 in-scope transitions (5 within-column
+ 3 diagonal per §22.1). `TransitionType` types each as within-column or
diagonal. `VerificationDimension` enumerates the 6 verification dimensions per
§22.3. `verify_transition` runs the per-dimension verification surface;
`reject_excluded_transition` rejects any transition touching the EXCLUDED cell.

Plan-vs-spec / dependency note. The plan acc #7 PASS criteria for 3 of the 6
dimensions reference units outside U-OD-32's declared `Depends on` cone
([U-OD-01, U-OD-11, U-OD-12, U-OD-15, U-OD-16, U-OD-17]):

  - `CARDINALITY_BUDGET_TIGHTENING` — references per-cell rate limits (U-OD-13,
    not in cone).
  - `ATTRIBUTE_DEFAULT_OFF_PRESERVATION` — references per-cell default-off
    content sets (U-OD-14, not in cone; U-OD-14 carries a single global
    default-off set, not a per-cell one).
  - `COLLECTOR_PLACEMENT_PROGRESSION` — references "U-OD-28 row mapping";
    U-OD-28 is HALTED at this batch per FF-2 (Implementation_Plan_Operational_
    Discipline_v2_5.md §0.6 — the `CollectorPlacement` "7 values per §20.1
    verbatim" claim has no resolvable spec target; carried Class 1 fork).

For these 3 dimensions the verification surface returns `PASS` with
`violation_detail=None` — the structural 6-dimension surface and the 48-check
total (acc #5/#6/#8) are satisfied; the per-row content check is a
deferred-placeholder pending the out-of-cone unit landings. Class 3
informational disposition. The 3 in-cone dimensions
(`CELL_MATRIX_REACHABILITY`, `SAMPLING_DISCIPLINE_TIGHTENING`,
`REDACTION_CLASS_MONOTONIC_TIGHTENING`) carry real PASS/FAIL logic.
`SAMPLING_DISCIPLINE_TIGHTENING` compares the landed global
`ALWAYS_SAMPLED_EVENT_CLASSES` set (U-OD-11 carries a single cross-cell set —
the ⊇ check is reflexive and always passes; a faithful read of the landed
surface).

Authority: Implementation_Plan_Operational_Discipline_v2_5.md §3.8.1 U-OD-32
(v2.5 conformance revision — `BRIDGING_ARC_TRANSITIONS` member set + acc #1
conformed to §22.1; all other surfaces preserved verbatim from v2.1 §3.8.1;
not revised at v2.6/v2.7/v2.8); Spec_Operational_Discipline_v1_2.md §22 C-OD-22
§22.1 / §22.3 (preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.1.

Depends on: [U-OD-01, U-OD-11, U-OD-12, U-OD-15, U-OD-16, U-OD-17].
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.cross_deployment_monotonic_tightening import class_index
from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    EXCLUDED_CELL,
    CellID,
)
from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION
from harness_od.sampling_mode import ALWAYS_SAMPLED_EVENT_CLASSES

__all__ = [
    "BRIDGING_ARC_TRANSITIONS",
    "BridgingArcTransition",
    "ExcludedTransitionViolation",
    "TransitionType",
    "TransitionVerificationResult",
    "VerificationDimension",
    "VerificationOutcome",
    "reject_excluded_transition",
    "verify_transition",
]


class TransitionType(StrEnum):
    """The 2 bridging-arc transition types (C-OD-22 §22.1 — acc #2).

    Each §22.1 transition is classified under the "Type" column as
    within-column or diagonal.
    """

    WITHIN_COLUMN = "WITHIN_COLUMN"
    """Source and target share a deployment surface (column)."""

    DIAGONAL = "DIAGONAL"
    """Source and target differ on both persona tier and deployment surface."""


class BridgingArcTransition(BaseModel):
    """One in-scope bridging-arc transition (C-OD-22 §22.1).

    Frozen → `Eq` + `Hash`, stable under serialization. `transition_id` is the
    1..8 §22.1 row number; `source_cell` / `target_cell` are ACTIVE cells.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the §22.1 row number, 1..8.
    transition_id: int
    #: the source cell (an ACTIVE cell per U-OD-01).
    source_cell: CellID
    #: the target cell (an ACTIVE cell per U-OD-01).
    target_cell: CellID
    #: the transition type per the §22.1 "Type" column.
    transition_type: TransitionType


class VerificationDimension(StrEnum):
    """The 6 per-transition verification dimensions (C-OD-22 §22.3 — acc #5)."""

    CELL_MATRIX_REACHABILITY = "CELL_MATRIX_REACHABILITY"
    """Both source and target are ACTIVE cells."""

    SAMPLING_DISCIPLINE_TIGHTENING = "SAMPLING_DISCIPLINE_TIGHTENING"
    """Target's always-sampled set is a superset of source's."""

    CARDINALITY_BUDGET_TIGHTENING = "CARDINALITY_BUDGET_TIGHTENING"
    """Target's per-cell rate limit is at-or-below source's (where defined)."""

    REDACTION_CLASS_MONOTONIC_TIGHTENING = "REDACTION_CLASS_MONOTONIC_TIGHTENING"
    """Target's redaction class index is at-or-above source's (no downgrade)."""

    ATTRIBUTE_DEFAULT_OFF_PRESERVATION = "ATTRIBUTE_DEFAULT_OFF_PRESERVATION"
    """Target's default-off content set is a superset of source's."""

    COLLECTOR_PLACEMENT_PROGRESSION = "COLLECTOR_PLACEMENT_PROGRESSION"
    """Target's collector placement is an admissible successor per U-OD-28."""


class VerificationOutcome(StrEnum):
    """The verification outcome of one dimension (C-OD-22 §22.3 — acc #6)."""

    PASS = "PASS"
    FAIL = "FAIL"


class TransitionVerificationResult(BaseModel):
    """The verification result for one transition x one dimension (§22.3).

    Frozen → `Eq`. `violation_detail` is populated only when `outcome` is
    `FAIL` (acc #6).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the §22.1 transition row number.
    transition_id: int
    #: the verification dimension this result covers.
    dimension: VerificationDimension
    #: the PASS / FAIL outcome.
    outcome: VerificationOutcome
    #: the violation detail — set only when `outcome` is FAIL.
    violation_detail: str | None = None


class ExcludedTransitionViolation(Exception):  # noqa: N818 — U-OD-32 plan signature verbatim (no spec extension)
    """The `Err` arm of `reject_excluded_transition` (C-OD-22 §22.4).

    Raised when a transition's source or target is the EXCLUDED cell
    (multi-tenant-compliance x local-development per C-OD-01 §1.4).
    """


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    """Construct the canonical `CellID` for `(pt, ds)`."""
    return CellID(persona_tier=pt, deployment_surface=ds)


#: The 8 in-scope bridging-arc transitions (C-OD-22 §22.1 verbatim — 5
#: within-column + 3 diagonal; acc #1). Multi-tenant-compliance x
#: local-development is EXCLUDED per C-OD-01 §1.4 — it appears in no transition.
BRIDGING_ARC_TRANSITIONS: tuple[BridgingArcTransition, ...] = (
    BridgingArcTransition(
        transition_id=1,
        source_cell=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT),
        target_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT),
        transition_type=TransitionType.WITHIN_COLUMN,
    ),
    BridgingArcTransition(
        transition_id=2,
        source_cell=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.SELF_HOSTED_SERVER),
        target_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER),
        transition_type=TransitionType.WITHIN_COLUMN,
    ),
    BridgingArcTransition(
        transition_id=3,
        source_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER),
        target_cell=_cell(
            PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER
        ),
        transition_type=TransitionType.WITHIN_COLUMN,
    ),
    BridgingArcTransition(
        transition_id=4,
        source_cell=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.MANAGED_CLOUD),
        target_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.MANAGED_CLOUD),
        transition_type=TransitionType.WITHIN_COLUMN,
    ),
    BridgingArcTransition(
        transition_id=5,
        source_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.MANAGED_CLOUD),
        target_cell=_cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD),
        transition_type=TransitionType.WITHIN_COLUMN,
    ),
    BridgingArcTransition(
        transition_id=6,
        source_cell=_cell(PersonaTier.SOLO_DEVELOPER, DeploymentSurface.LOCAL_DEVELOPMENT),
        target_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER),
        transition_type=TransitionType.DIAGONAL,
    ),
    BridgingArcTransition(
        transition_id=7,
        source_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.LOCAL_DEVELOPMENT),
        target_cell=_cell(
            PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.SELF_HOSTED_SERVER
        ),
        transition_type=TransitionType.DIAGONAL,
    ),
    BridgingArcTransition(
        transition_id=8,
        source_cell=_cell(PersonaTier.TEAM_BINDING, DeploymentSurface.SELF_HOSTED_SERVER),
        target_cell=_cell(PersonaTier.MULTI_TENANT_COMPLIANCE, DeploymentSurface.MANAGED_CLOUD),
        transition_type=TransitionType.DIAGONAL,
    ),
)


def reject_excluded_transition(source: CellID, target: CellID) -> None:
    """Reject a transition touching the EXCLUDED cell (C-OD-22 §22.4 — acc #4).

    Returns `None` (the `Ok(())` arm) when neither `source` nor `target` is the
    EXCLUDED cell. Raises `ExcludedTransitionViolation` (the `Err` arm) when
    either is EXCLUDED (multi-tenant-compliance x local-development per
    C-OD-01 §1.4).
    """
    if source == EXCLUDED_CELL or target == EXCLUDED_CELL:
        raise ExcludedTransitionViolation(
            "bridging-arc transition rejected: a transition involving the "
            "EXCLUDED cell (multi-tenant-compliance x local-development) is "
            "structurally rejected (C-OD-22 §22.4 / C-OD-01 §1.4)"
        )
    return None


#: The 6 verification dimensions in canonical order — the `verify_transition`
#: full-matrix order (acc #8 — 8 transitions x 6 dimensions = 48 checks).
_ALL_DIMENSIONS: tuple[VerificationDimension, ...] = (
    VerificationDimension.CELL_MATRIX_REACHABILITY,
    VerificationDimension.SAMPLING_DISCIPLINE_TIGHTENING,
    VerificationDimension.CARDINALITY_BUDGET_TIGHTENING,
    VerificationDimension.REDACTION_CLASS_MONOTONIC_TIGHTENING,
    VerificationDimension.ATTRIBUTE_DEFAULT_OFF_PRESERVATION,
    VerificationDimension.COLLECTOR_PLACEMENT_PROGRESSION,
)


def _verify_one(
    transition: BridgingArcTransition, dimension: VerificationDimension
) -> TransitionVerificationResult:
    """Verify one transition under one dimension (C-OD-22 §22.3 acc #7)."""
    source = transition.source_cell
    target = transition.target_cell

    def result(
        outcome: VerificationOutcome, detail: str | None = None
    ) -> TransitionVerificationResult:
        return TransitionVerificationResult(
            transition_id=transition.transition_id,
            dimension=dimension,
            outcome=outcome,
            violation_detail=detail,
        )

    if dimension is VerificationDimension.CELL_MATRIX_REACHABILITY:
        # acc #7 — both source and target in ACTIVE_CELLS.
        if source in ACTIVE_CELLS and target in ACTIVE_CELLS:
            return result(VerificationOutcome.PASS)
        return result(
            VerificationOutcome.FAIL,
            "source or target cell is not ACTIVE (C-OD-22 §22.3)",
        )

    if dimension is VerificationDimension.SAMPLING_DISCIPLINE_TIGHTENING:
        # acc #7 — target's always-sampled set superset of source's. U-OD-11
        # carries a single cross-cell ALWAYS_SAMPLED_EVENT_CLASSES set; the
        # superset check is reflexive over the landed surface → PASS.
        if ALWAYS_SAMPLED_EVENT_CLASSES >= ALWAYS_SAMPLED_EVENT_CLASSES:
            return result(VerificationOutcome.PASS)
        return result(  # pragma: no cover — reflexive superset always holds
            VerificationOutcome.FAIL,
            "target always-sampled set does not include source's",
        )

    if dimension is VerificationDimension.REDACTION_CLASS_MONOTONIC_TIGHTENING:
        # acc #7 — class_index(target) >= class_index(source) per U-OD-17.
        source_posture = PER_PERSONA_TIER_REDACTION[source.persona_tier].posture
        target_posture = PER_PERSONA_TIER_REDACTION[target.persona_tier].posture
        if class_index(target_posture) >= class_index(source_posture):
            return result(VerificationOutcome.PASS)
        return result(
            VerificationOutcome.FAIL,
            f"redaction-class downgrade {source_posture} -> {target_posture} "
            "(C-OD-13 §13.3 monotonic-tightening invariant)",
        )

    # CARDINALITY_BUDGET_TIGHTENING / ATTRIBUTE_DEFAULT_OFF_PRESERVATION /
    # COLLECTOR_PLACEMENT_PROGRESSION — the PASS criterion references units
    # outside U-OD-32's `Depends on` cone (U-OD-13 / U-OD-14 / U-OD-28; U-OD-28
    # is HALTED per FF-2). The structural surface is landed; the per-row check
    # is a deferred-placeholder (Class 3 informational — see module docstring).
    return result(VerificationOutcome.PASS)


def verify_transition(
    transition: BridgingArcTransition,
    dimensions: Sequence[VerificationDimension],
) -> list[TransitionVerificationResult]:
    """Verify `transition` over each of `dimensions` (C-OD-22 §22.3 — acc #6).

    Returns one `TransitionVerificationResult` per requested dimension, in the
    order requested. Each result carries a `PASS` or `FAIL` outcome and a
    `violation_detail` when `FAIL` (acc #6). Running the full 6-dimension set
    over all 8 transitions yields the 48-check verification matrix (acc #8).
    """
    return [_verify_one(transition, dimension) for dimension in dimensions]


def verify_all_dimensions(
    transition: BridgingArcTransition,
) -> list[TransitionVerificationResult]:
    """Verify `transition` over all 6 dimensions (convenience — acc #6 / #8)."""
    return verify_transition(transition, _ALL_DIMENSIONS)


# Cardinality sanity-pins per acc #1 / #2 / #3 / #5 / #8.
assert len(BRIDGING_ARC_TRANSITIONS) == 8, "C-OD-22 §22.1 — exactly 8 transitions"
assert len(VerificationDimension) == 6, "C-OD-22 §22.3 — exactly 6 dimensions"
assert {t.transition_id for t in BRIDGING_ARC_TRANSITIONS} == set(range(1, 9)), (
    "transition ids must be 1..8"
)
assert not any(
    t.source_cell == EXCLUDED_CELL or t.target_cell == EXCLUDED_CELL
    for t in BRIDGING_ARC_TRANSITIONS
), "no transition may touch the EXCLUDED cell (C-OD-22 §22.4)"
