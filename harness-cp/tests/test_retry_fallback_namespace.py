"""Tests for U-CP-07 — fallback / harness-breaker / retry namespace schemas.

PARTIAL LAND — acceptance #7 (runtime dual-emission discipline) + its 5
emission tests are struck per `.harness/class_1_tension_u_cp_07_dual_emission.md`.
Coverage below is for the landed schema declarations (acceptance #1–#6):

  #1 fallback.* 9 attrs        -> test_fallback_namespace_cardinality_nine,
                                  test_fallback_attributes_match_spec_verbatim
  #2 harness.breaker.* 7 attrs -> test_harness_breaker_namespace_cardinality_seven,
                                  test_harness_breaker_source_authority
  #3 retry.* 6 attrs           -> test_retry_namespace_cardinality_six,
                                  test_retry_attributes_match_spec_v1_3_verbatim
  #4 RetryCause 5 values       -> test_retry_cause_cardinality_five
  #5 D6 ingestion out of scope -> structural (no ingestion surface)
  #6 retry.attempt event 3 fld -> test_retry_attempt_event_schema_cardinality_three,
                                  test_retry_attempt_event_fields_match_spec_verbatim,
                                  test_retry_attempt_event_parent_next_delay_ms_optional
"""

from __future__ import annotations

from harness_cp.retry_fallback_namespace import (
    FALLBACK_NAMESPACE_SCHEMA,
    HARNESS_BREAKER_NAMESPACE_SCHEMA,
    RETRY_ATTEMPT_EVENT_SCHEMA,
    RETRY_NAMESPACE_SCHEMA,
    RetryCause,
)

_SPEC_FALLBACK = {
    "fallback.layer",
    "fallback.candidate_chosen",
    "fallback.candidates_skipped",
    "fallback.cause",
    "fallback.cross_family",
    "fallback.cross_family_triggered",
    "fallback.exhausted",
    "fallback.depth",
    "fallback.cache_state_lost",
}

_SPEC_HARNESS_BREAKER = {
    "harness.breaker.id",
    "harness.breaker.state",
    "harness.breaker.scope",
    "harness.breaker.trip_count",
    "harness.breaker.trip_window_seconds",
    "harness.breaker.fail_count_in_window",
    "harness.breaker.fail_threshold",
}

_SPEC_RETRY = {
    "retry.attempt_number",
    "retry.original_span_id",
    "retry.delay_ms",
    "retry.cause_attribution",
    "retry.fail_class",
    "engine.replay_disposition",
}

_SPEC_RETRY_EVENT = {
    "parent.attempt_count",
    "parent.attempts_remaining",
    "parent.next_delay_ms",
}


def test_fallback_namespace_cardinality_nine() -> None:
    """Acceptance #1 — exactly nine `fallback.*` attributes."""
    assert len(FALLBACK_NAMESPACE_SCHEMA) == 9


def test_fallback_attributes_match_spec_verbatim() -> None:
    """Acceptance #1 — `fallback.*` names match C-CP-03 §3.5 verbatim."""
    assert {a.attribute_name for a in FALLBACK_NAMESPACE_SCHEMA} == _SPEC_FALLBACK


def test_harness_breaker_namespace_cardinality_seven() -> None:
    """Acceptance #2 — exactly seven `harness.breaker.*` attributes."""
    assert len(HARNESS_BREAKER_NAMESPACE_SCHEMA) == 7


def test_harness_breaker_attributes_match_spec_verbatim() -> None:
    """Acceptance #2 — `harness.breaker.*` names match §3.5 + OD C-OD-07 §7.1."""
    assert {a.attribute_name for a in HARNESS_BREAKER_NAMESPACE_SCHEMA} == _SPEC_HARNESS_BREAKER


def test_harness_breaker_source_authority() -> None:
    """Acceptance #2 — every `harness.breaker.*` attribute cites c9 authority."""
    for attr in HARNESS_BREAKER_NAMESPACE_SCHEMA:
        assert attr.source_authority == "c9-reliability-recovery SKILL.md"


def test_retry_namespace_cardinality_six() -> None:
    """Acceptance #3 — exactly six `retry.*` attributes (v2.3 from 4)."""
    assert len(RETRY_NAMESPACE_SCHEMA) == 6


def test_retry_attributes_match_spec_v1_3_verbatim() -> None:
    """Acceptance #3 — `retry.*` names match C-CP-03 §3.5 + ADR-D6 v1.2 §1.2.2.1."""
    assert {a.attribute_name for a in RETRY_NAMESPACE_SCHEMA} == _SPEC_RETRY
    for attr in RETRY_NAMESPACE_SCHEMA:
        assert attr.source_authority  # every attribute carries an authority


def test_retry_cause_cardinality_five() -> None:
    """Acceptance #4 — `RetryCause` declares exactly five values."""
    assert len(RetryCause) == 5
    assert {c.name for c in RetryCause} == {
        "TRANSIENT_PROVIDER_ERROR",
        "RATE_LIMIT",
        "TIMEOUT",
        "CAPABILITY_SHORTFALL",
        "VALIDATOR_FAIL_TRANSIENT",
    }


def test_retry_attempt_event_schema_cardinality_three() -> None:
    """Acceptance #6 — the `retry.attempt` parent-event schema has 3 fields."""
    assert len(RETRY_ATTEMPT_EVENT_SCHEMA) == 3


def test_retry_attempt_event_fields_match_spec_verbatim() -> None:
    """Acceptance #6 — event field names match ADR-D6 v1.2 §1.2.2.2 verbatim."""
    assert {f.field_name for f in RETRY_ATTEMPT_EVENT_SCHEMA} == _SPEC_RETRY_EVENT


def test_retry_attempt_event_parent_next_delay_ms_optional() -> None:
    """Acceptance #6 — `parent.next_delay_ms` is optional; the other 2 are not."""
    by_name = {f.field_name: f for f in RETRY_ATTEMPT_EVENT_SCHEMA}
    assert by_name["parent.next_delay_ms"].optional is True
    assert by_name["parent.attempt_count"].optional is False
    assert by_name["parent.attempts_remaining"].optional is False
