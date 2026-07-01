"""Tests for U-CP-08 — deterministic fall-through procedure (C-CP-03 §3.5).

Acceptance-criterion coverage:
  #1 FallThroughCause 4 values    -> test_fall_through_cause_cardinality_four
  #1 byte-exact with §3.5         -> test_fall_through_cause_values_byte_exact_with_spec_3_5
  #2 "no decision" = cause=None   -> test_no_decision_cause_none_silent
  #3 honors layer ordering        -> test_fall_through_honors_layer_ordering
  #4 cause emits event            -> test_time_budget_exceeded_emits_event,
                                       test_capability_shortfall_emits_event,
                                       test_breaker_open_emits_event,
                                       test_rate_limit_storm_emits_event
  #5 final-layer exhausted        -> test_final_layer_exhausted
  #6 deterministic                -> test_procedure_deterministic
"""

from __future__ import annotations

from harness_cp.cp_shared_types import ProviderAgnosticPayload
from harness_cp.fall_through_procedure import (
    FallThroughCause,
    FallThroughResult,
    fall_through,
)
from harness_cp.routing_layer import RoutingLayer

_REQUEST = ProviderAgnosticPayload(messages=(), tools=None, params={})


def test_fall_through_cause_cardinality_four() -> None:
    assert len(list(FallThroughCause)) == 4


def test_fall_through_cause_values_byte_exact_with_spec_3_5() -> None:
    assert {c.value for c in FallThroughCause} == {
        "time_budget_exceeded",
        "capability_shortfall",
        "breaker_open",
        "rate_limit_storm",
    }


def test_no_decision_cause_none_silent() -> None:
    result = fall_through(RoutingLayer.DECLARATIVE, None, _REQUEST)
    assert result.cause is None
    assert result.emit_fallback_event is False
    assert result.next_layer is RoutingLayer.EMBEDDING


def test_fall_through_honors_layer_ordering() -> None:
    assert (
        fall_through(RoutingLayer.DECLARATIVE, None, _REQUEST).next_layer is RoutingLayer.EMBEDDING
    )
    assert (
        fall_through(RoutingLayer.EMBEDDING, None, _REQUEST).next_layer
        is RoutingLayer.LLM_AS_ROUTER
    )


def test_time_budget_exceeded_emits_event() -> None:
    result = fall_through(RoutingLayer.DECLARATIVE, FallThroughCause.TIME_BUDGET_EXCEEDED, _REQUEST)
    assert result.emit_fallback_event is True
    assert result.cause is FallThroughCause.TIME_BUDGET_EXCEEDED


def test_capability_shortfall_emits_event() -> None:
    result = fall_through(RoutingLayer.EMBEDDING, FallThroughCause.CAPABILITY_SHORTFALL, _REQUEST)
    assert result.emit_fallback_event is True


def test_breaker_open_emits_event() -> None:
    result = fall_through(RoutingLayer.DECLARATIVE, FallThroughCause.BREAKER_OPEN, _REQUEST)
    assert result.emit_fallback_event is True


def test_rate_limit_storm_emits_event() -> None:
    result = fall_through(RoutingLayer.EMBEDDING, FallThroughCause.RATE_LIMIT_STORM, _REQUEST)
    assert result.emit_fallback_event is True


def test_final_layer_exhausted() -> None:
    result = fall_through(RoutingLayer.LLM_AS_ROUTER, None, _REQUEST)
    assert result.next_layer is None


def test_procedure_deterministic() -> None:
    a = fall_through(RoutingLayer.DECLARATIVE, FallThroughCause.BREAKER_OPEN, _REQUEST)
    b = fall_through(RoutingLayer.DECLARATIVE, FallThroughCause.BREAKER_OPEN, _REQUEST)
    assert a == b
    assert isinstance(a, FallThroughResult)
