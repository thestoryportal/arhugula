"""Tests for U-OD-17 — cross-deployment monotonic-tightening invariant (C-OD-13).

Test set per the U-OD-17 §3.4.7 `Tests:` field — covers acceptance #1-#6
against C-OD-13 §13.3.
"""

from __future__ import annotations

import pytest
from harness_od.cross_deployment_monotonic_tightening import (
    D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION,
    D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION,
    REDACTION_CLASS_ORDER,
    MonotonicityViolation,
    assert_monotonic_tightening_across_transition,
    class_index,
    reject_class_downgrade,
)
from harness_od.redaction_gradient import ContentCapturePosture

_WEAK = ContentCapturePosture.OPERATOR_SELF_REDACT
_MID = ContentCapturePosture.REDACTION_PROCESSOR_AT_OTLP_COLLECTOR_BOUNDARY
_STRONG = ContentCapturePosture.PRE_COLLECTOR_EVAL_GRADE_PIPELINE


# --- acc #1 ----------------------------------------------------------------
def test_redaction_class_order_strict_ascending() -> None:
    """`REDACTION_CLASS_ORDER` is strict-ascending per §13.3 verbatim."""
    assert REDACTION_CLASS_ORDER == (_WEAK, _MID, _STRONG)
    # Strict — no duplicates, exactly the 3 postures.
    assert len(REDACTION_CLASS_ORDER) == 3
    assert len(set(REDACTION_CLASS_ORDER)) == 3
    assert set(REDACTION_CLASS_ORDER) == set(ContentCapturePosture)


# --- acc #2 ----------------------------------------------------------------
def test_class_index_returns_0_1_2() -> None:
    """`class_index` returns 0/1/2 for the three postures in strict order."""
    assert class_index(_WEAK) == 0
    assert class_index(_MID) == 1
    assert class_index(_STRONG) == 2


# --- acc #3 ----------------------------------------------------------------
def test_assert_monotonic_tightening_accept_equal() -> None:
    """Equal source/target classes — accepted (`Ok`)."""
    for posture in REDACTION_CLASS_ORDER:
        assert assert_monotonic_tightening_across_transition(posture, posture) is None


def test_assert_monotonic_tightening_accept_ascend() -> None:
    """Tightening transition (target rank > source rank) — accepted (`Ok`)."""
    assert assert_monotonic_tightening_across_transition(_WEAK, _MID) is None
    assert assert_monotonic_tightening_across_transition(_WEAK, _STRONG) is None
    assert assert_monotonic_tightening_across_transition(_MID, _STRONG) is None


def test_assert_monotonic_tightening_reject_descend() -> None:
    """Relaxing transition (target rank < source rank) — `Err`."""
    with pytest.raises(MonotonicityViolation):
        assert_monotonic_tightening_across_transition(_STRONG, _MID)
    with pytest.raises(MonotonicityViolation):
        assert_monotonic_tightening_across_transition(_STRONG, _WEAK)
    with pytest.raises(MonotonicityViolation):
        assert_monotonic_tightening_across_transition(_MID, _WEAK)


# --- acc #4 ----------------------------------------------------------------
def test_reject_class_downgrade_per_22_2() -> None:
    """`reject_class_downgrade` structurally rejects a downgrade (U-OD-32 alias)."""
    # Downgrade rejected.
    with pytest.raises(MonotonicityViolation):
        reject_class_downgrade(_STRONG, _MID)
    with pytest.raises(MonotonicityViolation):
        reject_class_downgrade(_MID, _WEAK)
    # Equal + tightening accepted — it is the strict-monotonic alias.
    assert reject_class_downgrade(_WEAK, _WEAK) is None
    assert reject_class_downgrade(_WEAK, _STRONG) is None


# --- acc #5 ----------------------------------------------------------------
def test_d2_cross_deployment_monotonicity_composition_declared() -> None:
    """D2 v1.1 §1.6 sandbox-tier monotonicity composition anchor is declared."""
    assert "D2" in D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION
    assert "§1.6" in D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION
    assert "C-AS-12 §12.1" in D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION


def test_d5_cross_deployment_monotonicity_composition_declared() -> None:
    """D5 v1.3 §1.5.2 gate-level monotonicity composition anchor is declared."""
    assert "D5" in D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION
    assert "§1.5.2" in D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION
    assert "C-CP-19" in D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION


# --- acc #6 ----------------------------------------------------------------
def test_cross_axis_edge_to_u_as_nn_c_as_12_declared() -> None:
    """Cross-axis edge to U-AS-NN (C-AS-12 §12.1) is declared in the AS anchor."""
    assert "cross-axis: AS" in D2_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION


def test_cross_axis_edge_to_u_cp_nn_c_cp_19_declared() -> None:
    """Cross-axis edge to U-CP-NN (C-CP-19) is declared in the CP anchor."""
    assert "cross-axis: CP" in D5_CROSS_DEPLOYMENT_MONOTONICITY_COMPOSITION
