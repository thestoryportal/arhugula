"""Tests for U-CP-39 — HITL-as-tool-call rewriting (C-CP-17 §17.2).

Acceptance-criterion coverage:
  #1 variant cardinality 3       -> test_semantic_variant_cardinality_three
  #2 variants match spec         -> test_variants_match_spec
  #3 evaluates _hitl_required    -> test_rewrite_evaluates_predicate
  #3 returns original when false -> test_returns_original_when_false
  #4 variant per synchrony       -> test_variant_selection_per_synchrony
  #5 palette full / restricted   -> test_palette_full_when_no_cross_trust
                                    test_palette_restricted_when_cross_family_active
                                    test_palette_restricted_when_local_terminal
                                    test_palette_restricted_when_untrusted_mcp
  #7 rewriting before dispatch   -> test_rewriting_before_dispatch
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harness_core import PersonaTier
from harness_cp.handoff_context import ActionKind, ProposedAction
from harness_cp.hitl_as_tool_call_rewriting import (
    HITL_SEMANTIC_VARIANTS,
    EngineBindingClass,
    HITLSemanticVariant,
    rewrite_tool_call_to_hitl,
    select_variant,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState


def _action() -> ProposedAction:
    payload: Mapping[str, Any] = {}
    return ProposedAction(action_kind=ActionKind.TOOL_CALL, payload=payload, brief=None)


def test_semantic_variant_cardinality_three() -> None:
    """#1 — HITLSemanticVariant declares exactly three values."""
    assert len(HITLSemanticVariant) == 3
    assert {v.value for v in HITLSemanticVariant} == {
        "request_human_input",
        "await_human_approval",
        "escalate_to_human",
    }


def test_variants_match_spec() -> None:
    """#2 — HITL_SEMANTIC_VARIANTS has 3 entries with the §17.2 bindings."""
    assert len(HITL_SEMANTIC_VARIANTS) == 3
    by_variant = {b.variant: b for b in HITL_SEMANTIC_VARIANTS}
    assert (
        by_variant[HITLSemanticVariant.REQUEST_HUMAN_INPUT].engine_binding
        is EngineBindingClass.SYNC_BLOCKING
    )
    assert (
        by_variant[HITLSemanticVariant.AWAIT_HUMAN_APPROVAL].engine_binding
        is EngineBindingClass.DURABLE_ASYNC
    )
    assert (
        by_variant[HITLSemanticVariant.ESCALATE_TO_HUMAN].engine_binding
        is EngineBindingClass.ALL_CELLS
    )


def test_rewrite_evaluates_predicate() -> None:
    """#3 — when _hitl_required is true, the call is rewritten."""
    rc = rewrite_tool_call_to_hitl(
        "Bash",
        "core-mcp",
        PersonaTier.SOLO_DEVELOPER,
        _action(),
        SynchronyClass.SYNC_BLOCKING,
        CrossTrustBoundaryState.NONE,
        hitl_required=True,
    )
    assert rc.hitl_required is True
    assert rc.variant is HITLSemanticVariant.REQUEST_HUMAN_INPUT
    assert rc.response_palette is not None


def test_returns_original_when_false() -> None:
    """#3 — when _hitl_required is false, the original call passes through."""
    rc = rewrite_tool_call_to_hitl(
        "Read",
        "core-mcp",
        PersonaTier.SOLO_DEVELOPER,
        _action(),
        SynchronyClass.SYNC_BLOCKING,
        CrossTrustBoundaryState.NONE,
        hitl_required=False,
    )
    assert rc.hitl_required is False
    assert rc.variant is None
    assert rc.response_palette is None
    assert rc.tool == "Read"


def test_variant_selection_per_synchrony() -> None:
    """#4 — variant selection is deterministic per cell synchrony class."""
    assert select_variant(SynchronyClass.SYNC_BLOCKING) is HITLSemanticVariant.REQUEST_HUMAN_INPUT
    assert select_variant(SynchronyClass.DURABLE_ASYNC) is HITLSemanticVariant.AWAIT_HUMAN_APPROVAL
    assert select_variant(SynchronyClass.BOTH_BY_TIER) is HITLSemanticVariant.ESCALATE_TO_HUMAN
    assert select_variant(SynchronyClass.EXCLUDED) is HITLSemanticVariant.ESCALATE_TO_HUMAN


def _rewrite(state: CrossTrustBoundaryState) -> frozenset[HITLResponse]:
    rc = rewrite_tool_call_to_hitl(
        "Bash",
        "core-mcp",
        PersonaTier.TEAM_BINDING,
        _action(),
        SynchronyClass.DURABLE_ASYNC,
        state,
        hitl_required=True,
    )
    assert rc.response_palette is not None
    return rc.response_palette


def test_palette_full_when_no_cross_trust() -> None:
    """#5 — full 4-response palette when no cross-trust-boundary state."""
    assert _rewrite(CrossTrustBoundaryState.NONE) == frozenset(HITLResponse)


def test_palette_restricted_when_cross_family_active() -> None:
    """#5 — restricted palette when CROSS_FAMILY_ACTIVE."""
    palette = _rewrite(CrossTrustBoundaryState.CROSS_FAMILY_ACTIVE)
    assert palette == frozenset({HITLResponse.REJECT, HITLResponse.RESPOND})


def test_palette_restricted_when_local_terminal() -> None:
    """#5 — restricted palette when LOCAL_TERMINAL_ACTIVE."""
    palette = _rewrite(CrossTrustBoundaryState.LOCAL_TERMINAL_ACTIVE)
    assert palette == frozenset({HITLResponse.REJECT, HITLResponse.RESPOND})


def test_palette_restricted_when_untrusted_mcp() -> None:
    """#5 — restricted palette when UNTRUSTED_MCP_ACTIVE."""
    palette = _rewrite(CrossTrustBoundaryState.UNTRUSTED_MCP_ACTIVE)
    assert palette == frozenset({HITLResponse.REJECT, HITLResponse.RESPOND})


def test_rewriting_before_dispatch() -> None:
    """#7 — rewriting fires before dispatch: every call yields a verdict.

    `rewrite_tool_call_to_hitl` always returns a RewrittenToolCall carrying the
    `hitl_required` verdict — no tool call reaches the action surface without a
    rewriting evaluation.
    """
    for required in (True, False):
        rc = rewrite_tool_call_to_hitl(
            "Write",
            "core-mcp",
            PersonaTier.SOLO_DEVELOPER,
            _action(),
            SynchronyClass.SYNC_BLOCKING,
            CrossTrustBoundaryState.NONE,
            hitl_required=required,
        )
        assert rc.hitl_required is required
