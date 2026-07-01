"""Tests for U-OD-14 — cardinality-safe + cardinality-prohibited attribute classes.

Each test maps to a U-OD-14 acceptance criterion (C-OD-11 §11.2 / §11.3).
"""

from __future__ import annotations

import pytest
from harness_od.attribute_class_enforcement import (
    CARDINALITY_PROHIBITED_ATTRIBUTES,
    CARDINALITY_SAFE_ATTRIBUTES,
    CardinalityViolation,
    assert_cardinality_prohibited_not_in_dashboard_dimension,
    assert_cardinality_safe_for_dashboard_dimension,
)

# §11.2 verbatim member set (acceptance #1).
_SAFE_MEMBERS = {
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.finish_reasons",
    "sandbox.tier",
    "sandbox.tech",
    "sandbox.provider",
    "hitl.gate.level",
    "hitl.response.class",
    "harness.breaker.scope",
    "harness.breaker.from_state",
    "harness.breaker.to_state",
    "validator.fail.class",
}

# §11.3 verbatim member set (acceptance #2).
_PROHIBITED_MEMBERS = {
    "gen_ai.conversation.id",
    "session_user_tenant_ids",
    "idempotency_key",
    "mcp.primitive.signature.sha256",
    "skill.version_sha",
    "audit.signature.sha256_or_prior_hash",
}


def test_cardinality_safe_cardinality_thirteen() -> None:
    """Acceptance #1 — `CARDINALITY_SAFE_ATTRIBUTES` has cardinality 13."""
    assert len(CARDINALITY_SAFE_ATTRIBUTES) == 13


def test_cardinality_safe_members_byte_exact_per_section_11_2() -> None:
    """Acceptance #1 — member set conforms byte-exact to the §11.2 table."""
    assert set(CARDINALITY_SAFE_ATTRIBUTES) == _SAFE_MEMBERS


def test_cardinality_prohibited_cardinality_six() -> None:
    """Acceptance #2 — `CARDINALITY_PROHIBITED_ATTRIBUTES` has cardinality 6."""
    assert len(CARDINALITY_PROHIBITED_ATTRIBUTES) == 6


def test_cardinality_prohibited_members_byte_exact_per_section_11_3() -> None:
    """Acceptance #2 — member set conforms byte-exact to the §11.3 table."""
    assert set(CARDINALITY_PROHIBITED_ATTRIBUTES) == _PROHIBITED_MEMBERS


def test_attribute_sets_disjoint() -> None:
    """Acceptance #3 — the safe and prohibited sets are disjoint."""
    assert CARDINALITY_SAFE_ATTRIBUTES & CARDINALITY_PROHIBITED_ATTRIBUTES == frozenset()


@pytest.mark.parametrize("attr", sorted(_SAFE_MEMBERS))
def test_safe_attribute_accepted_as_dashboard_dim(attr: str) -> None:
    """Acceptance #5 — a cardinality-safe attribute is accepted as a dimension."""
    assert assert_cardinality_safe_for_dashboard_dimension(attr) is None


@pytest.mark.parametrize("attr", sorted(_PROHIBITED_MEMBERS))
def test_prohibited_attribute_rejected_as_dashboard_dim(attr: str) -> None:
    """Acceptance #6 — a cardinality-prohibited attribute is rejected as a dimension."""
    with pytest.raises(CardinalityViolation):
        assert_cardinality_prohibited_not_in_dashboard_dimension(attr)


def test_prohibited_attribute_also_rejected_by_safe_gate() -> None:
    """Acceptance #5 — a prohibited attr is not in the safe set → safe gate rejects."""
    with pytest.raises(CardinalityViolation):
        assert_cardinality_safe_for_dashboard_dimension("idempotency_key")


def test_unknown_attribute_rejected_as_dashboard_dim() -> None:
    """Acceptance #5 — an attribute in neither set is rejected by the safe gate."""
    with pytest.raises(CardinalityViolation):
        assert_cardinality_safe_for_dashboard_dimension("some.unknown.attribute")


def test_safe_attribute_passes_prohibited_gate() -> None:
    """Acceptance #6 — a non-prohibited attribute passes the prohibited gate."""
    assert assert_cardinality_prohibited_not_in_dashboard_dimension("sandbox.tier") is None


def test_unknown_attribute_passes_prohibited_gate() -> None:
    """Acceptance #4/#6 — an unknown attr is not prohibited, so the prohibited gate passes.

    The prohibited gate only blocks the §11.3 set; unknown attributes are
    caught by the cardinality-safe gate (acceptance #5), not this one.
    """
    assert assert_cardinality_prohibited_not_in_dashboard_dimension("some.unknown.attr") is None
