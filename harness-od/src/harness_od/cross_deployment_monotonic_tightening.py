"""Cross-deployment monotonic-tightening invariant — U-OD-17.

Implements C-OD-13 §13.3 (cross-deployment monotonic-tightening invariant —
redaction class strictly tightens along the persona-tier axis at fixed
deployment surface).

`REDACTION_CLASS_ORDER` is the strict-ascending ordering of the three
`ContentCapturePosture` values (weakest → strongest); `class_index` projects a
posture onto its 0/1/2 rank. `assert_monotonic_tightening_across_transition`
admits an equal-or-tightening bridging-arc transition and rejects a relaxing
one; `reject_class_downgrade` is the alias consumed at U-OD-32 bridging-arc
transition verification (§13.3 downgrade-rejection invariant per C-OD-22).

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.4.7 U-OD-17
(body preserved verbatim through v2.5 / v2.6 — v2.6 §3 pointer table lists
U-OD-17 at v2.1 §3.4.7, no delta); Spec_Operational_Discipline_v1_2.md §13
C-OD-13 §13.3 (preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.3,
ADR-D2 v1.1 §1.6, ADR-D5 v1.3 §1.5.2.

`MonotonicityViolation` is declared in-unit — the Python materialization of the
plan's `Result<(), MonotonicityViolation>` error arm (the landed-unit
convention: `-> None` on success, `raise` on the error branch — see U-OD-14
`CardinalityViolation`, U-OD-23 `EmissionContractViolation`).

Cross-axis edges (acc #5 / #6): `Depends on` cites U-AS-NN (C-AS-12 §12.1 — D2
sandbox-tier cross-deployment monotonicity) and U-CP-NN (C-CP-19 — D5 gate-level
cross-deployment monotonicity). Those edges are declarative; placeholder
unit-IDs resolve at sub-phase 7c via the U-OD-34 aggregate manifest. U-OD-17
consumes no typed surface from either — the composition is at the bridging-arc
transition surface, not a type import. The declarations below carry the
composition anchors as module constants so the acc #5 / #6 tests verify the
edges are declared.
"""

from __future__ import annotations

from harness_od.redaction_gradient import ContentCapturePosture

__all__ = [
    "D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION",
    "D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION",
    "REDACTION_CLASS_ORDER",
    "MonotonicityViolation",
    "assert_monotonic_tightening_across_transition",
    "class_index",
    "reject_class_downgrade",
]


class MonotonicityViolation(Exception):  # noqa: N818 — name is the U-OD-17 plan signature verbatim
    """Raised when a bridging-arc transition relaxes the redaction class.

    The Python materialization of the plan's `Result<(), MonotonicityViolation>`
    error arm in `assert_monotonic_tightening_across_transition` /
    `reject_class_downgrade` — stack is Pydantic v2 + stdlib, no `Result`
    framework pull (CLAUDE.md §3.2 / I-6).
    """


# --- §13.3 strict-monotonic redaction-class ordering -----------------------

#: §13.3 verbatim — the redaction class strictly tightens along the
#: persona-tier axis: solo-developer (operator-self-redact, weakest) →
#: team-binding (redaction processor at OTLP collector boundary) →
#: multi-tenant-compliance (pre-collector eval-grade pipeline, strongest).
REDACTION_CLASS_ORDER: tuple[ContentCapturePosture, ...] = (
    ContentCapturePosture.OPERATOR_SELF_REDACT,
    ContentCapturePosture.REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY,
    ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE,
)

#: Cross-axis composition anchor — ADR-D2 v1.1 §1.6 sandbox-tier monotonic
#: ascension across deployment surfaces; composes with redaction-class
#: monotonicity at the bridging-arc transition surface (acc #5). Edge declared
#: at `Depends on: [U-AS-NN (cross-axis: AS — C-AS-12 §12.1)]`; resolves at 7c.
D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION: str = (
    "ADR-D2 v1.1 §1.6 — sandbox-tier monotonic ascension across deployment "
    "surfaces (never decrease sandbox tier on a bridging-arc transition); "
    "composes at C-AS-12 §12.1 (cross-axis: AS)"
)

#: Cross-axis composition anchor — ADR-D5 v1.3 §1.5.2 gate-level monotonic
#: ascension across deployment surfaces (acc #5). Edge declared at
#: `Depends on: [U-CP-NN (cross-axis: CP — C-CP-19)]`; resolves at 7c.
D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION: str = (
    "ADR-D5 v1.3 §1.5.2 — gate-level monotonic ascension across deployment "
    "surfaces; T-perm-1 D5-layer multiplicative gate-level rule; composes at "
    "C-CP-19 (cross-axis: CP)"
)


def class_index(c: ContentCapturePosture) -> int:
    """Return the 0/1/2 strict-ascending rank of `c` per `REDACTION_CLASS_ORDER`.

    `OPERATOR_SELF_REDACT` → 0 (weakest), `REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_
    BOUNDARY` → 1, `PRE_COLLECTOR_EVAL_GRADE_PIPELINE` → 2 (strongest) —
    C-OD-13 §13.3 (acc #2).
    """
    return REDACTION_CLASS_ORDER.index(c)


def assert_monotonic_tightening_across_transition(
    source_class: ContentCapturePosture,
    target_class: ContentCapturePosture,
) -> None:
    """Assert a bridging-arc transition does not relax the redaction class.

    Returns `None` (the `Ok(())` arm) when `class_index(target) >=
    class_index(source)` — an equal-or-tightening transition. Raises
    `MonotonicityViolation` (the `Err` arm) when `class_index(target) <
    class_index(source)` — a relaxing transition (C-OD-13 §13.3 monotonic
    tightening invariant, acc #3).
    """
    if class_index(target_class) < class_index(source_class):
        raise MonotonicityViolation(
            f"redaction-class downgrade rejected: {source_class} "
            f"(rank {class_index(source_class)}) → {target_class} "
            f"(rank {class_index(target_class)}) relaxes the redaction class "
            f"(C-OD-13 §13.3 monotonic-tightening invariant)"
        )
    return None


def reject_class_downgrade(
    source_class: ContentCapturePosture,
    target_class: ContentCapturePosture,
) -> None:
    """Structurally reject a redaction-class downgrade (C-OD-13 §13.3, acc #4).

    The alias for `assert_monotonic_tightening_across_transition` consumed at
    U-OD-32 bridging-arc transition verification — a transition that would
    relax the redaction class (e.g. multi-tenant → team-binding) is
    structurally rejected at the cell-transition surface per C-OD-22.
    """
    return assert_monotonic_tightening_across_transition(source_class, target_class)
