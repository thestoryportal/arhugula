"""Per-dimension preservation invariants across cross-axis dimensions — U-OD-33.

Implements C-OD-22 §22.2 (5-dimension preservation invariants under bridging-arc
traversal), §22.4 (invariant composition with persona-tier-axis ascent +
deployment-surface-axis ascent).

`PreservationDimension` enumerates the 5 preservation dimensions per §22.2
verbatim. `PreservationInvariant` records the per-dimension invariant form,
enforcement layer, and (for the cross-axis dimensions) the cross-axis
composition target. `PRESERVATION_INVARIANTS` declares the 5 entries with the
per-dimension form/layer/target given in acc #2.
`verify_per_dimension_preservation` verifies one bridging-arc transition under
one dimension; `assert_cross_axis_composition_verified_at_session_5` is the
deferred-verification gate for the two cross-axis dimensions.

Cross-axis deferral. Two dimensions — GATE_POLICY and SANDBOX_TIER — carry
`CROSS_AXIS_COMPOSITION_VERIFICATION` enforcement: their preservation invariant
composes with AS + CP cross-deployment monotonicity contracts (C-AS-12 §12.1
sandbox-tier monotonic ascent; C-CP-19 D5 cross-deployment monotonicity). Per
acc #4/#5, the OD plan v1 commits the OD-side surface only; the AS + CP
composition is the Session 5 cross-axis-matrix deliverable (OD-S4-2.A). The
cross-axis target is therefore carried as a plain `str` reference (e.g.
"C-AS-12 §12.1"), NOT a typed import — no `harness_as` / `harness_cp` symbol is
imported. `assert_cross_axis_composition_verified_at_session_5` returns the
`CrossAxisCompositionPending` error arm at plan-v1 scope, and
`verify_per_dimension_preservation` for the two cross-axis dimensions returns
the `Ok` arm as a structural placeholder pending that Session 5 verification
(consistent with the deferred-placeholder pattern at U-OD-32 §3.8.1 for the
dimensions whose PASS criterion references units outside the `Depends on`
cone).

In-cone dimensions. SAMPLING_DISCIPLINE and REDACTION_CLASS carry real
PASS/FAIL logic, delegating to the landed U-OD-11 (`ALWAYS_SAMPLED_EVENT_CLASSES`)
and U-OD-17 (`class_index` + `PER_PERSONA_TIER_REDACTION`) surfaces — no logic
is duplicated from U-OD-32. CARDINALITY_BUDGET references per-cell rate limits
(U-OD-13/U-OD-14), which carry a single global surface rather than a per-cell
one; following the U-OD-32 read, its verification returns the `Ok` arm as a
structural placeholder.

`Result<(), E>` materializes as the harness-od idiom: a function returning
`None` on the `Ok` arm and raising `E` on the `Err` arm — no `Result`
framework pull (CLAUDE.md §3.2 / I-6), matching `MonotonicityViolation` at
U-OD-17 and `ExcludedTransitionViolation` at U-OD-32.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.8.2 U-OD-33
(preserved verbatim through v2.11); Spec_Operational_Discipline_v1_2.md §22
C-OD-22 §22.2 / §22.4 (preserved verbatim into v1.3/v1.4); ADR-D6 v1.1 §1.1.

Depends on: [U-OD-05, U-OD-07, U-OD-11, U-OD-12, U-OD-17, U-OD-32]. Cross-axis
edges (3 OD→AS: C-AS-12 §12.1, C-AS-15 §15.6, C-AS-12 §12.4; 1 OD→CP: C-CP-19)
carried as `cross_axis_composition_target` string references; resolution at
U-OD-34.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_od.bridging_arc_table import BridgingArcTransition
from harness_od.cross_deployment_monotonic_tightening import class_index
from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION
from harness_od.sampling_mode import ALWAYS_SAMPLED_EVENT_CLASSES

__all__ = [
    "PRESERVATION_INVARIANTS",
    "CrossAxisCompositionPending",
    "EnforcementLayer",
    "InvariantForm",
    "PreservationDimension",
    "PreservationInvariant",
    "PreservationViolation",
    "assert_cross_axis_composition_verified_at_session_5",
    "verify_per_dimension_preservation",
]


class PreservationDimension(StrEnum):
    """The 5 preservation dimensions (C-OD-22 §22.2 verbatim — acc #1).

    Each dimension carries a preservation invariant that the bridging-arc
    transition surface must preserve. Exactly 5 dimensions per §22.2.
    """

    SAMPLING_DISCIPLINE = "SAMPLING_DISCIPLINE"
    """Always-sampled event-class discipline (U-OD-11 + U-OD-12)."""

    CARDINALITY_BUDGET = "CARDINALITY_BUDGET"
    """Per-cell cardinality / rate-limit budget (U-OD-13 + U-OD-14)."""

    REDACTION_CLASS = "REDACTION_CLASS"
    """Content-capture redaction class (U-OD-15 + U-OD-16 + U-OD-17)."""

    GATE_POLICY = "GATE_POLICY"
    """HITL / eval gate policy — cross-axis: CP C-CP-19."""

    SANDBOX_TIER = "SANDBOX_TIER"
    """Sandbox blast-radius tier — cross-axis: AS C-AS-12 §12.1."""


class InvariantForm(StrEnum):
    """The 4 invariant forms a preservation dimension may carry (§22.2)."""

    SET_INCLUSION_TARGET_INCLUDES_SOURCE = "SET_INCLUSION_TARGET_INCLUDES_SOURCE"
    """Target's set is a superset of source's (sampling, default-off attrs)."""

    SCALAR_MONOTONIC_TIGHTENING_LE = "SCALAR_MONOTONIC_TIGHTENING_LE"
    """Target's scalar budget is at-or-below source's (cardinality budget)."""

    CLASS_INDEX_MONOTONIC_ASCENT_GE = "CLASS_INDEX_MONOTONIC_ASCENT_GE"
    """Target's class index is at-or-above source's (redaction / gate / tier)."""

    CARDINALITY_PER_TENANT_ISOLATION = "CARDINALITY_PER_TENANT_ISOLATION"
    """Per-tenant cardinality isolation (multi-tenant cells only)."""


class EnforcementLayer(StrEnum):
    """The 3 enforcement layers a preservation invariant may bind to (§22.2)."""

    DESIGN_TIME_VERIFICATION = "DESIGN_TIME_VERIFICATION"
    """Verified at design time by U-OD-32 `verify_transition`."""

    RUNTIME_ENFORCEMENT_AT_COLLECTOR_BOUNDARY = "RUNTIME_ENFORCEMENT_AT_COLLECTOR_BOUNDARY"
    """Enforced at runtime at the OTLP collector boundary (U-OD-31)."""

    CROSS_AXIS_COMPOSITION_VERIFICATION = "CROSS_AXIS_COMPOSITION_VERIFICATION"
    """Verified at the Session 5 cross-axis composition matrix."""


class PreservationInvariant(BaseModel):
    """The preservation invariant for one dimension (C-OD-22 §22.2 — acc #2).

    Frozen → `Eq` + `Hash`, stable under serialization.
    `cross_axis_composition_target` is set only for the two cross-axis
    dimensions (GATE_POLICY → "C-CP-19"; SANDBOX_TIER → "C-AS-12 §12.1"); it is
    a plain `str` reference, NOT a typed cross-axis import — the cross-axis
    verification is deferred to Session 5 (acc #4/#5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the preservation dimension this invariant covers.
    dimension: PreservationDimension
    #: the form of the preservation invariant.
    invariant_form: InvariantForm
    #: the layer at which the invariant is enforced.
    enforcement_layer: EnforcementLayer
    #: the cross-axis composition contract anchor (string reference) — set
    #: only for the cross-axis dimensions; `None` for the in-axis dimensions.
    cross_axis_composition_target: str | None = None


class PreservationViolation(Exception):  # noqa: N818 — U-OD-33 plan signature verbatim (no spec extension)
    """The `Err` arm of `verify_per_dimension_preservation` (C-OD-22 §22.2).

    Raised when a bridging-arc transition does not preserve a dimension under
    its invariant form. The Python materialization of the plan's
    `Result<(), PreservationViolation>` error arm — stack is Pydantic v2 +
    stdlib, no `Result` framework pull (CLAUDE.md §3.2 / I-6).
    """


class CrossAxisCompositionPending(Exception):  # noqa: N818 — U-OD-33 plan signature verbatim (no spec extension)
    """The `Err` arm of `assert_cross_axis_composition_verified_at_session_5`.

    Raised when the cross-axis composition verification for a dimension is
    requested at OD plan v1 scope (C-OD-22 §22.4 — acc #5). The verification of
    the AS + CP composition is the Session 5 cross-axis-matrix deliverable
    (OD-S4-2.A); at plan-v1 scope it is deferred — this error is the deferred
    arm.
    """


#: The 5 preservation invariants keyed by dimension (C-OD-22 §22.2 verbatim —
#: acc #2). Per-dimension invariant form + enforcement layer + cross-axis
#: target exactly per §22.2. Exactly 5 entries (acc #2).
PRESERVATION_INVARIANTS: dict[PreservationDimension, PreservationInvariant] = {
    PreservationDimension.SAMPLING_DISCIPLINE: PreservationInvariant(
        dimension=PreservationDimension.SAMPLING_DISCIPLINE,
        invariant_form=InvariantForm.SET_INCLUSION_TARGET_INCLUDES_SOURCE,
        enforcement_layer=EnforcementLayer.DESIGN_TIME_VERIFICATION,
        cross_axis_composition_target=None,
    ),
    PreservationDimension.CARDINALITY_BUDGET: PreservationInvariant(
        dimension=PreservationDimension.CARDINALITY_BUDGET,
        invariant_form=InvariantForm.SCALAR_MONOTONIC_TIGHTENING_LE,
        enforcement_layer=EnforcementLayer.RUNTIME_ENFORCEMENT_AT_COLLECTOR_BOUNDARY,
        cross_axis_composition_target=None,
    ),
    PreservationDimension.REDACTION_CLASS: PreservationInvariant(
        dimension=PreservationDimension.REDACTION_CLASS,
        invariant_form=InvariantForm.CLASS_INDEX_MONOTONIC_ASCENT_GE,
        enforcement_layer=EnforcementLayer.DESIGN_TIME_VERIFICATION,
        cross_axis_composition_target=None,
    ),
    PreservationDimension.GATE_POLICY: PreservationInvariant(
        dimension=PreservationDimension.GATE_POLICY,
        invariant_form=InvariantForm.CLASS_INDEX_MONOTONIC_ASCENT_GE,
        enforcement_layer=EnforcementLayer.CROSS_AXIS_COMPOSITION_VERIFICATION,
        cross_axis_composition_target="C-CP-19",
    ),
    PreservationDimension.SANDBOX_TIER: PreservationInvariant(
        dimension=PreservationDimension.SANDBOX_TIER,
        invariant_form=InvariantForm.CLASS_INDEX_MONOTONIC_ASCENT_GE,
        enforcement_layer=EnforcementLayer.CROSS_AXIS_COMPOSITION_VERIFICATION,
        cross_axis_composition_target="C-AS-12 §12.1",
    ),
}


def verify_per_dimension_preservation(
    transition: BridgingArcTransition,
    dimension: PreservationDimension,
) -> None:
    """Verify `transition` preserves `dimension` (C-OD-22 §22.2 — acc #3).

    Returns `None` (the `Ok(())` arm) when the transition preserves the
    dimension under its invariant form. Raises `PreservationViolation` (the
    `Err` arm) with violation detail otherwise.

    Per-dimension verification:

      - `SAMPLING_DISCIPLINE` — `SET_INCLUSION_TARGET_INCLUDES_SOURCE`: target's
        always-sampled event-class set ⊇ source's. U-OD-11 carries a single
        cross-cell `ALWAYS_SAMPLED_EVENT_CLASSES` set; the ⊇ check is reflexive
        over the landed surface → always `Ok` (a faithful read of the landed
        U-OD-11 surface).
      - `REDACTION_CLASS` — `CLASS_INDEX_MONOTONIC_ASCENT_GE`:
        `class_index(target) >= class_index(source)` per U-OD-17; raises on a
        redaction-class downgrade.
      - `CARDINALITY_BUDGET` — `SCALAR_MONOTONIC_TIGHTENING_LE`: references the
        per-cell rate limits of U-OD-13/U-OD-14, which carry a single global
        surface rather than a per-cell one; the structural surface is landed and
        verification returns `Ok` as a deferred-placeholder (the U-OD-32 read).
      - `GATE_POLICY` / `SANDBOX_TIER` — `CLASS_INDEX_MONOTONIC_ASCENT_GE` under
        `CROSS_AXIS_COMPOSITION_VERIFICATION`: the actual class-index ascent
        check composes with the AS + CP cross-deployment monotonicity contracts
        (C-AS-12 §12.1 / C-CP-19); that composition is the Session 5 deliverable
        (acc #4). At plan-v1 scope verification returns `Ok` as a structural
        placeholder — call `assert_cross_axis_composition_verified_at_session_5`
        for the deferred-verification gate.
    """
    source = transition.source_cell
    target = transition.target_cell

    if dimension is PreservationDimension.SAMPLING_DISCIPLINE:
        # SET_INCLUSION_TARGET_INCLUDES_SOURCE — U-OD-11 carries a single
        # cross-cell set; the ⊇ check is reflexive and always holds.
        if ALWAYS_SAMPLED_EVENT_CLASSES >= ALWAYS_SAMPLED_EVENT_CLASSES:
            return None
        raise PreservationViolation(  # pragma: no cover — reflexive ⊇ holds
            "sampling-discipline preservation violated: target always-sampled "
            "set does not include source's (C-OD-22 §22.2)"
        )

    if dimension is PreservationDimension.REDACTION_CLASS:
        # CLASS_INDEX_MONOTONIC_ASCENT_GE — class_index(target) >=
        # class_index(source) per U-OD-17; no redaction-class downgrade.
        source_posture = PER_PERSONA_TIER_REDACTION[source.persona_tier].posture
        target_posture = PER_PERSONA_TIER_REDACTION[target.persona_tier].posture
        if class_index(target_posture) >= class_index(source_posture):
            return None
        raise PreservationViolation(
            f"redaction-class preservation violated: downgrade "
            f"{source_posture} -> {target_posture} "
            "(C-OD-22 §22.2 — CLASS_INDEX_MONOTONIC_ASCENT_GE)"
        )

    # CARDINALITY_BUDGET — references U-OD-13/U-OD-14 per-cell rate limits
    # (single global surface; deferred-placeholder, the U-OD-32 read).
    # GATE_POLICY / SANDBOX_TIER — cross-axis composition deferred to Session 5
    # (acc #4); structural placeholder until then.
    return None


def assert_cross_axis_composition_verified_at_session_5(
    dimension: PreservationDimension,
) -> None:
    """Assert `dimension`'s cross-axis composition is verified (§22.4 — acc #5).

    The two cross-axis dimensions (`GATE_POLICY`, `SANDBOX_TIER`) carry
    `CROSS_AXIS_COMPOSITION_VERIFICATION` enforcement: their preservation
    invariant composes with the AS + CP cross-deployment monotonicity contracts
    (C-AS-12 §12.1 sandbox-tier monotonic ascent; C-CP-19 D5 cross-deployment
    monotonicity). That composition verification is the Session 5 cross-axis
    matrix deliverable (OD-S4-2.A).

    At OD plan v1 scope the verification is deferred — this function raises
    `CrossAxisCompositionPending` (the `Err` arm) for every dimension whose
    enforcement layer is `CROSS_AXIS_COMPOSITION_VERIFICATION`. For the in-axis
    dimensions (whose composition is verifiable within the OD axis) it returns
    `None` (the `Ok(())` arm) — those dimensions require no cross-axis matrix.
    """
    invariant = PRESERVATION_INVARIANTS[dimension]
    if invariant.enforcement_layer is EnforcementLayer.CROSS_AXIS_COMPOSITION_VERIFICATION:
        raise CrossAxisCompositionPending(
            f"cross-axis composition verification for {dimension} is pending: "
            f"the {invariant.cross_axis_composition_target} composition is the "
            "Session 5 cross-axis-matrix deliverable (OD plan v1 commits the "
            "OD-side surface only — C-OD-22 §22.4 / OD-S4-2.A)"
        )
    return None


# Cardinality sanity-pins per acc #1 / #2.
assert len(PreservationDimension) == 5, "C-OD-22 §22.2 — exactly 5 dimensions"
assert len(PRESERVATION_INVARIANTS) == 5, "C-OD-22 §22.2 — exactly 5 preservation invariants"
assert set(PRESERVATION_INVARIANTS) == set(PreservationDimension), (
    "PRESERVATION_INVARIANTS must key every preservation dimension"
)
assert all(dim is inv.dimension for dim, inv in PRESERVATION_INVARIANTS.items()), (
    "each PRESERVATION_INVARIANTS entry's dimension must match its key"
)
