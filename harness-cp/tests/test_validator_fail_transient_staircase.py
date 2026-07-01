"""Tests for U-CP-48 — transient staircase + palette restriction (C-CP-21).

Acceptance-criterion coverage (C-CP-21 §21.2 + §21.3):
  #1 StaircaseStage cardinality 5      -> test_staircase_stage_cardinality_five
  #2 staircase runs / skip behavior    -> test_staircase_runs_for_transient_and_reflexion_recoverable,
                                          test_permanent_fail_exit_skips_staircase,
                                          test_terminal_fail_exit_halts,
                                          test_budget_exhausted_advances_to_stage_3,
                                          test_family_exhausted_advances_to_stage_4,
                                          test_local_fail_advances_to_stage_5
  #3 stage 3 emits cache-state-lost    -> test_stage_3_emits_cache_state_lost
  #4 CrossTrustBoundaryState 4 values  -> test_cross_trust_state_cardinality_four
  #5 palette restriction table §21.3   -> test_palette_restriction_match_spec_21_3,
                                          test_none_full_palette,
                                          test_cross_family_restricted_to_reject_respond,
                                          test_local_terminal_restricted_to_reject_respond,
                                          test_untrusted_mcp_restricted_to_reject_respond
  #6 composes w/ completeness invariant-> test_restriction_composes_with_completeness_invariant
  #7 advance_staircase deterministic   -> test_advance_staircase_deterministic
"""

from __future__ import annotations

import pytest
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass
from harness_cp.validator_fail_transient_staircase import (
    PALETTE_RESTRICTION_TABLE,
    TRANSIENT_STAIRCASE_TRANSITIONS,
    CrossTrustBoundaryState,
    StaircaseStage,
    advance_staircase,
    compute_restricted_palette,
)


def test_staircase_stage_cardinality_five() -> None:
    """Acceptance #1 — `StaircaseStage` declares exactly five stages."""
    assert len(StaircaseStage) == 5


def test_staircase_runs_for_transient_and_reflexion_recoverable() -> None:
    """Acceptance #2 — staircase advances for the two staircase classes."""
    for cause in (
        ValidatorRetryExitClass.TRANSIENT_RETRY,
        ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
    ):
        t = advance_staircase(StaircaseStage.STAGE_1_REFLEXION, cause, attempt=1)
        assert t.to_stage == StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF


def test_permanent_fail_exit_skips_staircase() -> None:
    """Acceptance #2 — `PERMANENT_FAIL_EXIT` skips to stage 5."""
    t = advance_staircase(
        StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        ValidatorRetryExitClass.PERMANENT_FAIL_EXIT,
        attempt=3,
    )
    assert t.to_stage == StaircaseStage.STAGE_5_HITL_ESCALATION


def test_terminal_fail_exit_halts() -> None:
    """Acceptance #2 — `TERMINAL_FAIL_EXIT` skips to stage 5 (halt + HITL)."""
    t = advance_staircase(
        StaircaseStage.STAGE_1_REFLEXION,
        ValidatorRetryExitClass.TERMINAL_FAIL_EXIT,
        attempt=1,
    )
    assert t.to_stage == StaircaseStage.STAGE_5_HITL_ESCALATION


def test_hitl_recoverable_skips_staircase() -> None:
    """Acceptance #2 — `HITL_RECOVERABLE` routes directly to C11 HITL (§21.1)."""
    t = advance_staircase(
        StaircaseStage.STAGE_1_REFLEXION,
        ValidatorRetryExitClass.HITL_RECOVERABLE,
        attempt=1,
    )
    assert t.to_stage == StaircaseStage.STAGE_5_HITL_ESCALATION


def test_budget_exhausted_advances_to_stage_3() -> None:
    """Acceptance #2 — 2nd fail at stage 2 advances to cross-family fallback."""
    t = advance_staircase(
        StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        ValidatorRetryExitClass.TRANSIENT_RETRY,
        attempt=2,
    )
    assert t.to_stage == StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK


def test_family_exhausted_advances_to_stage_4() -> None:
    """Acceptance #2 — stage 3 advances to local-terminal on family exhaust."""
    t = advance_staircase(
        StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK,
        ValidatorRetryExitClass.TRANSIENT_RETRY,
        attempt=3,
    )
    assert t.to_stage == StaircaseStage.STAGE_4_LOCAL_TERMINAL


def test_local_fail_advances_to_stage_5() -> None:
    """Acceptance #2 — stage 4 advances to HITL escalation on local fail."""
    t = advance_staircase(
        StaircaseStage.STAGE_4_LOCAL_TERMINAL,
        ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        attempt=3,
    )
    assert t.to_stage == StaircaseStage.STAGE_5_HITL_ESCALATION


def test_stage_3_emits_cache_state_lost() -> None:
    """Acceptance #3 — stage-3 transitions lose cache state + emit fallback."""
    stage_3_transitions = [
        t
        for t in TRANSIENT_STAIRCASE_TRANSITIONS
        if t.to_stage == StaircaseStage.STAGE_3_CROSS_FAMILY_FALLBACK
    ]
    assert stage_3_transitions
    for t in stage_3_transitions:
        assert t.emits_fallback_event
        assert not t.preserves_cache_state


def test_cross_trust_state_cardinality_four() -> None:
    """Acceptance #4 — `CrossTrustBoundaryState` declares exactly four values."""
    assert len(CrossTrustBoundaryState) == 4


def test_palette_restriction_match_spec_21_3() -> None:
    """Acceptance #5 — 4 entries, one per cross-trust state."""
    assert len(PALETTE_RESTRICTION_TABLE) == 4
    assert {r.cross_trust_state for r in PALETTE_RESTRICTION_TABLE} == set(CrossTrustBoundaryState)


def test_none_full_palette() -> None:
    """Acceptance #5 — `NONE` yields the full 4-response palette."""
    assert compute_restricted_palette(CrossTrustBoundaryState.NONE) == frozenset(
        {
            HITLResponse.APPROVE,
            HITLResponse.EDIT,
            HITLResponse.REJECT,
            HITLResponse.RESPOND,
        }
    )


def test_cross_family_restricted_to_reject_respond() -> None:
    """Acceptance #5 — `CROSS_FAMILY_ACTIVE` restricts to {reject, respond}."""
    assert compute_restricted_palette(CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE) == frozenset(
        {HITLResponse.REJECT, HITLResponse.RESPOND}
    )


def test_local_terminal_restricted_to_reject_respond() -> None:
    """Acceptance #5 — `LOCAL_TERMINAL_ACTIVE` restricts to {reject, respond}."""
    assert compute_restricted_palette(CrossTrustBoundaryState.LOCAL_TERMINAL_ACTIVE) == frozenset(
        {HITLResponse.REJECT, HITLResponse.RESPOND}
    )


def test_untrusted_mcp_restricted_to_reject_respond() -> None:
    """Acceptance #5 — `UNTRUSTED_MCP_ACTIVE` restricts to {reject, respond}."""
    assert compute_restricted_palette(CrossTrustBoundaryState.UNTRUSTED_MCP_ACTIVE) == frozenset(
        {HITLResponse.REJECT, HITLResponse.RESPOND}
    )


def test_restriction_composes_with_completeness_invariant() -> None:
    """Acceptance #6 — every restricted palette is a subset of the full one.

    The §21.3 restriction narrows what is presentable at a cell; it never
    introduces a response outside the U-CP-37 4-response palette.
    """
    full = {
        HITLResponse.APPROVE,
        HITLResponse.EDIT,
        HITLResponse.REJECT,
        HITLResponse.RESPOND,
    }
    for state in CrossTrustBoundaryState:
        assert compute_restricted_palette(state) <= full


def test_advance_staircase_deterministic() -> None:
    """Acceptance #7 — `advance_staircase` is deterministic given inputs."""
    first = advance_staircase(
        StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
        ValidatorRetryExitClass.TRANSIENT_RETRY,
        attempt=2,
    )
    for _ in range(8):
        again = advance_staircase(
            StaircaseStage.STAGE_2_RETRY_WITH_BACKOFF,
            ValidatorRetryExitClass.TRANSIENT_RETRY,
            attempt=2,
        )
        assert again == first


def test_attempt_does_not_alter_transition() -> None:
    """Acceptance #7 — `attempt` is bookkeeping; transition is stage-keyed."""
    base = advance_staircase(
        StaircaseStage.STAGE_1_REFLEXION,
        ValidatorRetryExitClass.TRANSIENT_RETRY,
        attempt=1,
    )
    for n in (2, 5, 99):
        assert (
            advance_staircase(
                StaircaseStage.STAGE_1_REFLEXION,
                ValidatorRetryExitClass.TRANSIENT_RETRY,
                attempt=n,
            )
            == base
        )


def test_palette_table_frozen() -> None:
    """Palette-restriction records are frozen (ConfigDict frozen=True)."""
    with pytest.raises(Exception):
        PALETTE_RESTRICTION_TABLE[0].cross_trust_state = (  # type: ignore[misc]
            CrossTrustBoundaryState.NONE
        )
