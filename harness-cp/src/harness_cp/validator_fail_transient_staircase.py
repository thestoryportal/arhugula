"""Transient staircase + cause-attribution branching + palette restriction — U-CP-48.

Implements C-CP-21 §21.2 (transient staircase + cause-attribution branching)
and §21.3 (palette-restriction rule at cross-trust-boundary actions). Declares
`StaircaseStage` (5-value), `StaircaseTransition`, `TRANSIENT_STAIRCASE_
TRANSITIONS`, `CrossTrustBoundaryState` (4-value), `PaletteRestriction`,
`PALETTE_RESTRICTION_TABLE` (4-entry), and the `advance_staircase` /
`compute_restricted_palette` functions.

Per §21.2 the transient staircase runs for `validator.fail.class ∈
{TRANSIENT_RETRY, REFLEXION_RECOVERABLE}`; `PERMANENT_FAIL_EXIT`,
`TERMINAL_FAIL_EXIT`, and `HITL_RECOVERABLE` **skip the staircase** and route
directly to C11 HITL per §21.1.

**Palette-restriction note (Class 3).** Spec §21.3 states the cross-trust-
boundary palette is "restricted to `{approve, reject, respond}` (no `edit`)".
The U-CP-48 plan acceptance #5 narrows the three cross-trust-state entries
further to `{REJECT, RESPOND}` (the plan author preserved this at v2.4). A
subset of the spec-allowed set — still `edit`-free — so this is a within-allowed
refinement, not a verbatim divergence; landed per the plan table. Recorded as a
Class 3 observation at `.harness/phase-7-progress.md`.

Authority: Implementation_Plan_Control_Plane_v2_4.md §2.8 U-CP-48 (v2.4
amendment — `TRANSIENT_STAIRCASE_TRANSITIONS` `on_cause` conformed to the
U-CP-47 v2.4-conformed `ValidatorRetryExitClass`); Spec_Control_Plane_v1_2.md §21
C-CP-21 §21.2 + §21.3 (preserved verbatim into v1.3); ADR-D5 v1.3 §1.10.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass


class StaircaseStage(StrEnum):
    """The 5 transient-staircase stages (C-CP-21 §21.2 verbatim)."""

    STAGE_1_REFLEXION = "stage-1-reflexion"
    STAGE_2_RETRY_WITH_BACKOFF = "stage-2-retry-with-backoff"
    STAGE_3_CROSS_FAMILY_FALLBACK = "stage-3-cross-family-fallback"
    STAGE_4_LOCAL_TERMINAL = "stage-4-local-terminal"
    STAGE_5_HITL_ESCALATION = "stage-5-hitl-escalation"


class CrossTrustBoundaryState(StrEnum):
    """The 4 cross-trust-boundary states (C-CP-21 §21.4 / §21.3 verbatim)."""

    NONE = "none"
    CROSS_FAMILY_ACTIVE = "cross-family-active"
    LOCAL_TERMINAL_ACTIVE = "local-terminal-active"
    UNTRUSTED_MCP_ACTIVE = "untrusted-mcp-active"


class StaircaseTransition(BaseModel):
    """One transient-staircase transition (C-CP-21 §21.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_stage: StaircaseStage
    on_cause: ValidatorRetryExitClass
    """The retry-exit class keying the transition — the U-CP-47 v2.4-conformed
    `ValidatorRetryExitClass` (not a fail-cause token)."""

    to_stage: StaircaseStage
    preserves_cache_state: bool
    emits_fallback_event: bool
    """`true` for stage-3 transitions — they emit `fallback.cross_family_
    triggered` + `fallback.cache_state_lost` per U-CP-09 + C-CP-04 §4.3."""


class PaletteRestriction(BaseModel):
    """One cross-trust-state palette-restriction entry (C-CP-21 §21.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cross_trust_state: CrossTrustBoundaryState
    restricted_palette: frozenset[HITLResponse]
    rationale: str


# --- §21.2 transient staircase transitions ---------------------------------
#
# The staircase runs for {TRANSIENT_RETRY, REFLEXION_RECOVERABLE}. Per §21.2:
# 1st fail -> stage 1/2 (retry with backoff); 2nd fail -> cause-attribution
# branch; 3rd fail -> permanent-fail-exit -> stage 5. Stage 3 (cross-family
# fallback) transitions emit the fallback events and lose cache state.
_STAIRCASE_CLASSES: frozenset[ValidatorRetryExitClass] = frozenset(
    {ValidatorRetryExitClass.TRANSIENT_RETRY, ValidatorRetryExitClass.REFLEXION_RECOVERABLE}
)
"""The §21.2 classes that run the transient staircase."""

_SKIP_STAIRCASE_CLASSES: frozenset[ValidatorRetryExitClass] = frozenset(
    {
        ValidatorRetryExitClass.PERMANENT_FAIL_EXIT,
        ValidatorRetryExitClass.TERMINAL_FAIL_EXIT,
        ValidatorRetryExitClass.HITL_RECOVERABLE,
    }
)
"""Classes that skip the staircase and route directly to C11 HITL (§21.1)."""

TRANSIENT_STAIRCASE_TRANSITIONS: tuple[StaircaseTransition, ...] = (
    # 1st validator fail -> stage 1 reflexion -> stage 2 retry (cache kept).
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_1_REFLEXION,
        on_cause=ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        to_stage=StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        preserves_cache_state=True,
        emits_fallback_event=False,
    ),
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_1_REFLEXION,
        on_cause=ValidatorRetryExitClass.TRANSIENT_RETRY,
        to_stage=StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        preserves_cache_state=True,
        emits_fallback_event=False,
    ),
    # 2nd validator fail -> stage 3 cross-family fallback (cache state lost).
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        on_cause=ValidatorRetryExitClass.TRANSIENT_RETRY,
        to_stage=StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK,
        preserves_cache_state=False,
        emits_fallback_event=True,
    ),
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        on_cause=ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        to_stage=StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK,
        preserves_cache_state=False,
        emits_fallback_event=True,
    ),
    # Stage 3 -> stage 4 local-terminal (family exhausted).
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK,
        on_cause=ValidatorRetryExitClass.TRANSIENT_RETRY,
        to_stage=StaircaseStage.STAGE_4_LOCAL_TERMINAL,
        preserves_cache_state=False,
        emits_fallback_event=True,
    ),
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK,
        on_cause=ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        to_stage=StaircaseStage.STAGE_4_LOCAL_TERMINAL,
        preserves_cache_state=False,
        emits_fallback_event=True,
    ),
    # Stage 4 -> stage 5 HITL escalation (3rd fail -> permanent-fail-exit).
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_4_LOCAL_TERMINAL,
        on_cause=ValidatorRetryExitClass.TRANSIENT_RETRY,
        to_stage=StaircaseStage.STAGE_5_HITL_ESCALATION,
        preserves_cache_state=False,
        emits_fallback_event=False,
    ),
    StaircaseTransition(
        from_stage=StaircaseStage.STAGE_4_LOCAL_TERMINAL,
        on_cause=ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        to_stage=StaircaseStage.STAGE_5_HITL_ESCALATION,
        preserves_cache_state=False,
        emits_fallback_event=False,
    ),
)
"""The §21.2 transient-staircase transition table, keyed on the §21.1
retry-exit `ValidatorRetryExitClass`."""

_TRANSITION_INDEX: dict[tuple[StaircaseStage, ValidatorRetryExitClass], StaircaseTransition] = {
    (t.from_stage, t.on_cause): t for t in TRANSIENT_STAIRCASE_TRANSITIONS
}


# --- §21.3 palette-restriction table ---------------------------------------

_FULL_PALETTE: frozenset[HITLResponse] = frozenset(
    {
        HITLResponse.APPROVE,
        HITLResponse.EDIT,
        HITLResponse.REJECT,
        HITLResponse.RESPOND,
    }
)
# Plan acceptance #5 — cross-trust states restrict to {REJECT, RESPOND}
# (a subset of the spec §21.3 {approve, reject, respond} edit-free set).
_RESTRICTED_PALETTE: frozenset[HITLResponse] = frozenset(
    {HITLResponse.REJECT, HITLResponse.RESPOND}
)

PALETTE_RESTRICTION_TABLE: tuple[PaletteRestriction, ...] = (
    PaletteRestriction(
        cross_trust_state=CrossTrustBoundaryState.NONE,
        restricted_palette=_FULL_PALETTE,
        rationale=(
            "No cross-trust-boundary state — the full 4-response palette is "
            "presented per C-CP-21 §21.3."
        ),
    ),
    PaletteRestriction(
        cross_trust_state=CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE,
        restricted_palette=_RESTRICTED_PALETTE,
        rationale=(
            "Cross-family fallback active — operator `edit`/`approve` would "
            "re-introduce an action unsafe to dispatch without re-evaluation; "
            "restricted to {reject, respond} per §21.3."
        ),
    ),
    PaletteRestriction(
        cross_trust_state=CrossTrustBoundaryState.LOCAL_TERMINAL_ACTIVE,
        restricted_palette=_RESTRICTED_PALETTE,
        rationale=(
            "Workflow fell through to the local/open-weight tier — restricted "
            "to {reject, respond} per §21.3."
        ),
    ),
    PaletteRestriction(
        cross_trust_state=CrossTrustBoundaryState.UNTRUSTED_MCP_ACTIVE,
        restricted_palette=_RESTRICTED_PALETTE,
        rationale=(
            "Tool dispatches against an untrusted-floor MCP server — "
            "restricted to {reject, respond} per §21.3."
        ),
    ),
)
"""The 4-entry §21.3 palette-restriction table — one entry per
`CrossTrustBoundaryState`."""

_PALETTE_INDEX: dict[CrossTrustBoundaryState, frozenset[HITLResponse]] = {
    r.cross_trust_state: r.restricted_palette for r in PALETTE_RESTRICTION_TABLE
}


class StaircaseError(ValueError):
    """An `advance_staircase` call with no defined transition for the inputs."""


def advance_staircase(
    current: StaircaseStage,
    cause: ValidatorRetryExitClass,
    attempt: int,
) -> StaircaseTransition:
    """Advance the transient staircase one step (C-CP-21 §21.2).

    Deterministic given inputs — no inference path (acceptance #7). For a
    skip-staircase class (`PERMANENT_FAIL_EXIT` / `TERMINAL_FAIL_EXIT` /
    `HITL_RECOVERABLE`) the transition routes directly to
    `STAGE_5_HITL_ESCALATION` per §21.1. For a staircase class
    (`TRANSIENT_RETRY` / `REFLEXION_RECOVERABLE`) the §21.2 transition table is
    consulted; an undefined `(stage, cause)` pair raises `StaircaseError`.

    `attempt` is the validator-fail count; it is carried for caller
    bookkeeping and does not alter the deterministic transition lookup.
    """
    _ = attempt  # carried for caller bookkeeping; transition is stage-keyed.
    if cause in _SKIP_STAIRCASE_CLASSES:
        return StaircaseTransition(
            from_stage=current,
            on_cause=cause,
            to_stage=StaircaseStage.STAGE_5_HITL_ESCALATION,
            preserves_cache_state=False,
            emits_fallback_event=False,
        )
    transition = _TRANSITION_INDEX.get((current, cause))
    if transition is None:
        raise StaircaseError(f"no §21.2 staircase transition from {current!r} on {cause!r}")
    return transition


def compute_restricted_palette(
    state: CrossTrustBoundaryState,
) -> frozenset[HITLResponse]:
    """Return the restricted HITL palette for `state` (C-CP-21 §21.3).

    `NONE` yields the full 4-response palette; the three cross-trust-boundary
    states yield `{REJECT, RESPOND}` per the U-CP-48 plan acceptance #5.
    Composes with the U-CP-37 palette-completeness invariant: the restriction
    narrows *what is presentable at a cell*, never the canonical palette
    declaration.
    """
    return _PALETTE_INDEX[state]
