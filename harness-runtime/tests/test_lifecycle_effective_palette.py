"""U-RT-90 unit tests — effective-palette computation per Reading B.

Covers runtime spec v1.22 §14.8.2 step 4d + §14.15.4 invariant 4. CP-as-landed
truth-table (per CP plan acc #5 §21.3 divergence — restricted palette is
``{REJECT, RESPOND}`` not spec-narrative ``{APPROVE, REJECT, RESPOND}``).
"""

from __future__ import annotations

import pytest
from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.validator_framework_types import (
    HITLEscalationBrief,
    ValidatorFailClass,
)
from harness_runtime.lifecycle.effective_palette import (
    DENY_ROW_PALETTE,
    FULL_PALETTE,
    compute_effective_palette,
)

# Aliases for readability.
APPROVE = HITLResponse.APPROVE
EDIT = HITLResponse.EDIT
REJECT = HITLResponse.REJECT
RESPOND = HITLResponse.RESPOND


def _brief(palette: frozenset[HITLResponse] | None = None) -> HITLEscalationBrief:
    """Construct a test brief with optional palette override."""
    kwargs: dict[str, object] = {
        "parent_step_id": "step-1",
        "parent_action_id": "act-1",
        "fail_class": ValidatorFailClass.SCHEMA_VIOLATION,
        "fail_detail_hash": "deadbeef",
        "escalation_reason": "test",
    }
    if palette is not None:
        kwargs["proposed_response_palette"] = palette
    return HITLEscalationBrief(**kwargs)


class TestComputeEffectivePalette:
    """4-case CP-as-landed truth-table coverage (AC #1)."""

    def test_deny_no_cross_trust_yields_deny_row(self) -> None:
        """gate=DENY + cross-trust=NONE → {REJECT, RESPOND} per §19.4."""
        result = compute_effective_palette(GateLevel.DENY, CrossTrustBoundaryState.NONE, None)
        assert result == frozenset({REJECT, RESPOND})

    def test_deny_with_cross_trust_yields_deny_row(self) -> None:
        """gate=DENY + cross-trust active → {REJECT, RESPOND} (intersection)."""
        result = compute_effective_palette(
            GateLevel.DENY, CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE, None
        )
        assert result == frozenset({REJECT, RESPOND})

    def test_ask_no_cross_trust_yields_full_palette(self) -> None:
        """gate=ASK + cross-trust=NONE → full palette per C-CP-16 §16.1."""
        result = compute_effective_palette(GateLevel.ASK, CrossTrustBoundaryState.NONE, None)
        assert result == frozenset({APPROVE, EDIT, REJECT, RESPOND})

    def test_ask_with_cross_trust_yields_restricted_palette(self) -> None:
        """gate=ASK + cross-trust active → CP-as-landed restricted palette.

        CP-axis PALETTE_RESTRICTION_TABLE per CP plan acc #5 = {REJECT, RESPOND}
        (NOT spec-narrative {APPROVE, REJECT, RESPOND}). Documented divergence.
        """
        result = compute_effective_palette(
            GateLevel.ASK, CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE, None
        )
        assert result == frozenset({REJECT, RESPOND})

    @pytest.mark.parametrize(
        "cross_trust",
        [
            CrossTrustBoundaryState.LOCAL_TERMINAL_ACTIVE,
            CrossTrustBoundaryState.UNTRUSTED_MCP_ACTIVE,
        ],
    )
    def test_ask_with_other_cross_trust_states(self, cross_trust: CrossTrustBoundaryState) -> None:
        """All 3 non-NONE cross-trust states narrow uniformly per CP-as-landed."""
        result = compute_effective_palette(GateLevel.ASK, cross_trust, None)
        assert result == frozenset({REJECT, RESPOND})


class TestValidatorEscalationBriefNarrowing:
    """Validator-proposed palette narrowing per spec §14.15.4 invariant 4 (AC #1)."""

    def test_default_brief_palette_no_extra_narrowing(self) -> None:
        """Default brief palette = full → no additional narrowing."""
        brief = _brief()
        result = compute_effective_palette(GateLevel.ASK, CrossTrustBoundaryState.NONE, brief)
        assert result == frozenset({APPROVE, EDIT, REJECT, RESPOND})

    def test_brief_palette_intersection_narrows_further(self) -> None:
        """Brief proposing {APPROVE, REJECT} narrows full palette to {APPROVE, REJECT}."""
        brief = _brief(palette=frozenset({APPROVE, REJECT}))
        result = compute_effective_palette(GateLevel.ASK, CrossTrustBoundaryState.NONE, brief)
        assert result == frozenset({APPROVE, REJECT})

    def test_brief_palette_cannot_exceed_constraints(self) -> None:
        """Even when brief proposes APPROVE, deny-row strips it out (final ≤ both)."""
        brief = _brief(palette=frozenset({APPROVE, REJECT, RESPOND}))
        result = compute_effective_palette(GateLevel.DENY, CrossTrustBoundaryState.NONE, brief)
        # deny-row palette = {REJECT, RESPOND}; brief intersection drops APPROVE.
        assert result == frozenset({REJECT, RESPOND})
        assert APPROVE not in result


class TestModuleConstants:
    """Module-level constant sanity (AC #1 boundary cases)."""

    def test_full_palette_has_4_members(self) -> None:
        assert len(FULL_PALETTE) == 4
        assert FULL_PALETTE == frozenset({APPROVE, EDIT, REJECT, RESPOND})

    def test_deny_row_palette_has_2_members(self) -> None:
        assert len(DENY_ROW_PALETTE) == 2
        assert DENY_ROW_PALETTE == frozenset({REJECT, RESPOND})


class TestPureFunctionGuarantee:
    """AC #3 — pure function (idempotent + no input mutation)."""

    def test_idempotent_same_inputs_same_output(self) -> None:
        """Calling twice with same inputs returns equal outputs."""
        result1 = compute_effective_palette(
            GateLevel.DENY, CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE, None
        )
        result2 = compute_effective_palette(
            GateLevel.DENY, CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE, None
        )
        assert result1 == result2

    def test_no_module_state_between_calls(self) -> None:
        """Result depends only on arguments — not on call history."""
        # Different call → different result; no hidden state.
        deny_result = compute_effective_palette(GateLevel.DENY, CrossTrustBoundaryState.NONE, None)
        ask_result = compute_effective_palette(GateLevel.ASK, CrossTrustBoundaryState.NONE, None)
        assert deny_result != ask_result
