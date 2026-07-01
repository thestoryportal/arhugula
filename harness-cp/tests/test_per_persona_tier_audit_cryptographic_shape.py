"""Tests for U-CP-42 — per-persona-tier audit cryptographic shape (C-CP-20 §20.1/§20.2).

Acceptance-criterion coverage:
  #1 CryptographicShape cardinality 3 -> test_cryptographic_shape_cardinality_three
  #2 3 entries per §20.1 verbatim     -> test_persona_tier_shapes_cardinality_three,
                                         test_solo_append_only_no_chain,
                                         test_team_hash_chained_no_signing,
                                         test_multi_tenant_signed
  #3 strict-monotonic strength        -> test_monotonic_strength_ascending
  #4 chain_construction_source        -> test_chain_construction_delegates_to_u_is_09
  #5 signing_key_source cites U-CP-44 -> test_signing_key_source_cites_u_cp_44
  #6 verification_at_read             -> covered by per-tier assertions
"""

from __future__ import annotations

from harness_core import PersonaTier
from harness_cp.per_persona_tier_audit_cryptographic_shape import (
    PERSONA_TIER_CRYPTOGRAPHIC_SHAPES,
    CryptographicShape,
    cryptographic_shape_for,
    shape_strength,
)

_PERSONA_TIER_ORDER = (
    PersonaTier.SOLO_DEVELOPER,
    PersonaTier.TEAM_BINDING,
    PersonaTier.MULTI_TENANT_COMPLIANCE,
)


def test_cryptographic_shape_cardinality_three() -> None:
    """#1 — CryptographicShape declares exactly three values."""
    assert len(CryptographicShape) == 3


def test_persona_tier_shapes_cardinality_three() -> None:
    """#2 — exactly three per-persona-tier entries."""
    assert len(PERSONA_TIER_CRYPTOGRAPHIC_SHAPES) == 3
    assert {e.persona_tier for e in PERSONA_TIER_CRYPTOGRAPHIC_SHAPES} == set(PersonaTier)


def test_solo_append_only_no_chain() -> None:
    """#2 — SOLO_DEVELOPER -> APPEND_ONLY_SQLITE, no signing, no verify-at-read."""
    s = cryptographic_shape_for(PersonaTier.SOLO_DEVELOPER)
    assert s.cryptographic_shape is CryptographicShape.APPEND_ONLY_SQLITE
    assert s.signing_required is False
    assert s.verification_at_read is False
    assert s.chain_construction_source is None


def test_team_hash_chained_no_signing() -> None:
    """#2 — TEAM_BINDING -> HASH_CHAINED_SQLITE, no signing, verify-at-read."""
    s = cryptographic_shape_for(PersonaTier.TEAM_BINDING)
    assert s.cryptographic_shape is CryptographicShape.HASH_CHAINED_SQLITE
    assert s.signing_required is False
    assert s.verification_at_read is True


def test_multi_tenant_signed() -> None:
    """#2 — MULTI_TENANT_COMPLIANCE -> signed, verify-at-read."""
    s = cryptographic_shape_for(PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert s.cryptographic_shape is CryptographicShape.HASH_CHAINED_SQLITE_WITH_SIGNATURE
    assert s.signing_required is True
    assert s.verification_at_read is True


def test_monotonic_strength_ascending() -> None:
    """#3 — cryptographic shape strength ascends strict-monotonically."""
    strengths = [
        shape_strength(cryptographic_shape_for(t).cryptographic_shape) for t in _PERSONA_TIER_ORDER
    ]
    assert strengths == sorted(strengths)
    assert len(set(strengths)) == 3  # strict — no two tiers share a strength


def test_chain_construction_delegates_to_u_is_09() -> None:
    """#4 — chain_construction_source cites U-IS-09 at team-binding+."""
    assert cryptographic_shape_for(PersonaTier.SOLO_DEVELOPER).chain_construction_source is None
    for t in (PersonaTier.TEAM_BINDING, PersonaTier.MULTI_TENANT_COMPLIANCE):
        src = cryptographic_shape_for(t).chain_construction_source
        assert src is not None and "U-IS-09" in src


def test_signing_key_source_cites_u_cp_44() -> None:
    """#5 — signing_key_source cites U-CP-44 only at multi-tenant-compliance."""
    assert cryptographic_shape_for(PersonaTier.SOLO_DEVELOPER).signing_key_source is None
    assert cryptographic_shape_for(PersonaTier.TEAM_BINDING).signing_key_source is None
    src = cryptographic_shape_for(PersonaTier.MULTI_TENANT_COMPLIANCE).signing_key_source
    assert src is not None and "U-CP-44" in src
