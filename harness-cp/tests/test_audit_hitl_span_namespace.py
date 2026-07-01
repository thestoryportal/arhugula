"""Tests for U-CP-46 — audit + HITL-span namespaces (C-CP-20 §20.4-§20.6).

Lands the v2.4-conformed body. Acceptance-criterion coverage:
  #1 audit namespace 7 attrs    -> test_audit_namespace_cardinality_seven
                                   test_audit_attributes_match_spec_20_4_verbatim
  #2 HITL span schema 4 spans   -> test_hitl_span_namespace_cardinality_four
                                   test_hitl_span_schema_match_spec_20_6_verbatim
  #4 per-persona emission       -> test_per_persona_emission_cardinality_three
                                   test_solo_emits_actor_id_only
                                   test_team_emits_actor_id_plus_prior_hash
                                   test_multi_tenant_emits_all_seven
  #5 monotonic emission         -> test_monotonic_emission_ascending
  #6 signature attrs @ solo     -> test_signature_attrs_absent_at_solo
  #9 byte-exact attribute names -> test_attribute_names_byte_exact
  #11 cardinality invariants    -> test_cardinality_invariants_at_startup
"""

from __future__ import annotations

from harness_core import PersonaTier
from harness_cp.audit_hitl_span_namespace import (
    AUDIT_NAMESPACE_SCHEMA,
    HITL_SPAN_NAMESPACE_SCHEMA,
    PERSONA_TIER_AUDIT_EMISSION,
)

# The 7 §20.4 audit.* attributes, byte-exact from the spec table.
_SPEC_20_4_ATTRS = (
    "audit.signature.sha256",
    "audit.signature.prior_hash",
    "audit.actor.id",
    "audit.signature.value",
    "audit.signature.algorithm",
    "audit.signature.key_id",
    "audit.signature.key_period",
)

# The 4 §20.6 HITL span names, byte-exact.
_SPEC_20_6_SPANS = {
    "hitl.gate.evaluated",
    "hitl.invocation.opened",
    "hitl.invocation.responded",
    "hitl.invocation.timed_out",
}


def _row(tier: PersonaTier):
    return next(r for r in PERSONA_TIER_AUDIT_EMISSION if r.persona_tier is tier)


def test_audit_namespace_cardinality_seven() -> None:
    """#1 — AUDIT_NAMESPACE_SCHEMA declares exactly 7 entries."""
    assert len(AUDIT_NAMESPACE_SCHEMA) == 7


def test_audit_attributes_match_spec_20_4_verbatim() -> None:
    """#1 — the 7 audit.* attribute names match C-CP-20 §20.4 verbatim."""
    assert tuple(a.attribute_name for a in AUDIT_NAMESPACE_SCHEMA) == _SPEC_20_4_ATTRS


def test_hitl_span_namespace_cardinality_four() -> None:
    """#2 — HITL_SPAN_NAMESPACE_SCHEMA declares exactly 4 entries."""
    assert len(HITL_SPAN_NAMESPACE_SCHEMA) == 4


def test_hitl_span_schema_match_spec_20_6_verbatim() -> None:
    """#2 — the 4 HITL span names + per-span attributes match §20.6 verbatim."""
    assert {s.span_name for s in HITL_SPAN_NAMESPACE_SCHEMA} == _SPEC_20_6_SPANS
    by_span = {s.span_name: s for s in HITL_SPAN_NAMESPACE_SCHEMA}
    assert by_span["hitl.gate.evaluated"].span_attributes == (
        "hitl.gate.level",
        "hitl.gate.persona_tier",
        "hitl.gate.required",
    )
    assert "hitl.invocation.audit_ledger_entry_id" in (
        by_span["hitl.invocation.opened"].span_attributes
    )
    assert by_span["hitl.invocation.responded"].span_attributes == (
        "hitl.response.class",
        "hitl.response.latency_ms",
        "hitl.response.summary_hash",
    )


def test_per_persona_emission_cardinality_three() -> None:
    """#4 — PERSONA_TIER_AUDIT_EMISSION declares exactly 3 entries."""
    assert len(PERSONA_TIER_AUDIT_EMISSION) == 3


def test_solo_emits_actor_id_only() -> None:
    """#4 — solo-developer emits audit.actor.id only."""
    row = _row(PersonaTier.SOLO_DEVELOPER)
    assert row.emitted_audit_attributes == frozenset({"audit.actor.id"})
    assert row.optional_audit_attributes == frozenset()


def test_team_emits_actor_id_plus_prior_hash() -> None:
    """#4 — team-binding emits actor.id + prior_hash; signature attrs optional."""
    row = _row(PersonaTier.TEAM_BINDING)
    assert row.emitted_audit_attributes == frozenset(
        {"audit.actor.id", "audit.signature.prior_hash"}
    )
    assert len(row.optional_audit_attributes) == 5


def test_multi_tenant_emits_all_seven() -> None:
    """#4 — multi-tenant-compliance emits all seven audit.* attributes."""
    row = _row(PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert row.emitted_audit_attributes == frozenset(_SPEC_20_4_ATTRS)


def test_monotonic_emission_ascending() -> None:
    """#5 — emission set ascends across solo -> team -> multi-tenant."""
    solo = _row(PersonaTier.SOLO_DEVELOPER).emitted_audit_attributes
    team = _row(PersonaTier.TEAM_BINDING).emitted_audit_attributes
    mtc = _row(PersonaTier.MULTI_TENANT_COMPLIANCE).emitted_audit_attributes
    assert solo < team < mtc


def test_signature_attrs_absent_at_solo() -> None:
    """#6 — audit.signature.* attributes are absent at solo-developer."""
    solo = _row(PersonaTier.SOLO_DEVELOPER).emitted_audit_attributes
    assert not any(a.startswith("audit.signature.") for a in solo)


def test_attribute_names_byte_exact() -> None:
    """#9 — every audit.* attribute name is byte-exact per §20.4."""
    assert set(a.attribute_name for a in AUDIT_NAMESPACE_SCHEMA) == set(_SPEC_20_4_ATTRS)


def test_cardinality_invariants_at_startup() -> None:
    """#11 — the 7 / 4 cardinality invariants hold."""
    assert len(AUDIT_NAMESPACE_SCHEMA) == 7
    assert len(HITL_SPAN_NAMESPACE_SCHEMA) == 4
