"""Tests for U-OD-10 — namespace collision precedence + cardinality discipline.

Test set per the U-OD-10 `Tests:` field (Implementation_Plan_Operational_
Discipline_v2_1.md §3.3.2 + v2.6 §3.3.2 addition). Every acceptance criterion
maps to >=1 test.

Acceptance criteria (C-OD-08 §8.1 / §8.2 / §8.3):
  #1 — NamespacePrecedenceRule enumerates exactly 2 values.
  #2 — NAMESPACE_COLLISIONS includes the harness.breaker. precedence example.
  #3 — substrate-anchored namespace takes precedence per §8.1.
  #4 — cache-tier cardinality invariant; violation → CanonicalValueViolation.
  #5 — invariant enforced at span emission time.
  #6 — enforce_otel_canonical_value invoked at every span emission.
  #7 (v2.6) — span_attrs resolves to the U-OD-04 carrier.
"""

from __future__ import annotations

import pytest
from harness_od.namespace_collision_discipline import (
    ATTR_CACHE_CREATION,
    ATTR_CACHE_READ,
    ATTR_INPUT_TOKENS,
    CACHE_TIER_SUBSET_INVARIANT,
    NAMESPACE_COLLISIONS,
    CacheTierSubsetInvariant,
    CanonicalValueViolation,
    NamespaceCollisionResolution,
    NamespacePrecedenceRule,
    enforce_otel_canonical_value,
)


def test_namespace_precedence_rule_cardinality_two() -> None:
    """Acceptance #1 — NamespacePrecedenceRule enumerates exactly 2 values."""
    assert len(NamespacePrecedenceRule) == 2
    assert set(NamespacePrecedenceRule) == {
        NamespacePrecedenceRule.SUBSTRATE_ANCHORED_TAKES_PRECEDENCE,
        NamespacePrecedenceRule.AUTHORITATIVE_DECLARER_RESOLVES_COLLISION,
    }


def test_namespace_collisions_includes_harness_breaker_precedence() -> None:
    """Acceptance #2 — NAMESPACE_COLLISIONS includes the §8.2 canonical example."""
    resolution = NAMESPACE_COLLISIONS[0]
    assert resolution.colliding_prefix == "breaker."
    assert resolution.authoritative_prefix == "harness.breaker."
    assert resolution.precedence_rule is NamespacePrecedenceRule.SUBSTRATE_ANCHORED_TAKES_PRECEDENCE


def test_substrate_anchored_takes_precedence_per_section_8_1() -> None:
    """Acceptance #3 — substrate-anchored namespace takes precedence per §8.1."""
    for resolution in NAMESPACE_COLLISIONS:
        assert isinstance(resolution, NamespaceCollisionResolution)
    assert (
        NAMESPACE_COLLISIONS[0].precedence_rule
        is NamespacePrecedenceRule.SUBSTRATE_ANCHORED_TAKES_PRECEDENCE
    )


def test_f_cp_01_stage_3b_rationale_byte_exact() -> None:
    """Acceptance #2 — the rationale string is byte-exact per plan acc #2."""
    assert NAMESPACE_COLLISIONS[0].rationale_ref == "F-CP-01 Stage 3b alignment"


def test_collision_resolution_resolves_to_authoritative_prefix() -> None:
    """Acceptance #3 — the OD-anchored prefix is the authoritative declarer."""
    assert NAMESPACE_COLLISIONS[0].authoritative_prefix == "harness.breaker."


def test_replaced_namespace_no_longer_declarative() -> None:
    """Acceptance #3 — the replaced CP-side prefix is the colliding (non-
    authoritative) prefix, not the authoritative declarer."""
    resolution = NAMESPACE_COLLISIONS[0]
    assert resolution.colliding_prefix != resolution.authoritative_prefix


def test_cache_tier_subset_invariant_holds_at_canonical_span() -> None:
    """Acceptance #4 — a conformant cache-tier attribute set is accepted."""
    attrs = {
        ATTR_INPUT_TOKENS: 1000,
        ATTR_CACHE_CREATION: 200,
        ATTR_CACHE_READ: 300,
    }
    # uncached = 1000 - 300 - 200 = 500 >= 0 → invariant holds.
    assert enforce_otel_canonical_value(attrs) is None


def test_cache_tier_violation_rejected() -> None:
    """Acceptance #4 — a cache-tier breakdown exceeding input_tokens is rejected."""
    attrs = {
        ATTR_INPUT_TOKENS: 100,
        ATTR_CACHE_CREATION: 80,
        ATTR_CACHE_READ: 80,
    }
    with pytest.raises(CanonicalValueViolation):
        enforce_otel_canonical_value(attrs)


def test_enforce_otel_canonical_value_passes_valid() -> None:
    """Acceptance #6 — a valid attribute set passes (boundary: full cache use)."""
    attrs = {
        ATTR_INPUT_TOKENS: 500,
        ATTR_CACHE_CREATION: 200,
        ATTR_CACHE_READ: 300,
    }
    # uncached = 0 — boundary case, still valid.
    assert enforce_otel_canonical_value(attrs) is None


def test_enforce_otel_canonical_value_rejects_invalid() -> None:
    """Acceptance #6 — a non-conformant attribute set is rejected."""
    attrs = {
        ATTR_INPUT_TOKENS: 10,
        ATTR_CACHE_CREATION: 6,
        ATTR_CACHE_READ: 6,
    }
    with pytest.raises(CanonicalValueViolation):
        enforce_otel_canonical_value(attrs)


def test_enforce_no_token_attrs_vacuous_accept() -> None:
    """Acceptance #5 — a span with no token attribution is accepted (vacuous)."""
    assert enforce_otel_canonical_value({"some.other.attr": "value"}) is None
    assert enforce_otel_canonical_value(None) is None


def test_enforce_partial_token_attrs_rejected() -> None:
    """Acceptance #6 — a partial token-attribute set is rejected as non-conformant."""
    with pytest.raises(CanonicalValueViolation):
        enforce_otel_canonical_value({ATTR_INPUT_TOKENS: 100})


def test_cache_tier_subset_invariant_form() -> None:
    """Acceptance #4 — the invariant form string is byte-exact per §8.3."""
    assert (
        CACHE_TIER_SUBSET_INVARIANT.invariant_form
        == "cache_creation + cache_read + uncached == input_tokens"
    )
    assert isinstance(CACHE_TIER_SUBSET_INVARIANT, CacheTierSubsetInvariant)


def test_span_attributes_param_resolves_to_u_od_04_carrier() -> None:
    """Acceptance #7 (v2.6) — span_attrs resolves to the U-OD-04 OTel-handle
    alias family (SpanAttributes is the otel_genai_base type alias).

    The function consumes a Mapping-shaped SpanAttributes; passing an OTel-
    attribute-shaped dict exercises the carrier alias.
    """
    from harness_od.otel_genai_base import SpanAttributes as _SpanAttributes

    attrs: _SpanAttributes = {ATTR_INPUT_TOKENS: 1, ATTR_CACHE_CREATION: 0, ATTR_CACHE_READ: 0}
    assert enforce_otel_canonical_value(attrs) is None
