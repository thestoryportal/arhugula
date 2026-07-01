"""Per-persona-tier audit-ledger cryptographic shape table — U-CP-42.

Implements C-CP-20 §20.1 (the per-persona-tier audit-ledger cryptographic
shape) and §20.2 (the strict-monotonic shape-strength ascent along the
persona-tier axis).

Declares the `CryptographicShape` 3-value enum, the
`PersonaTierCryptographicShape` record, the 3-entry
`PERSONA_TIER_CRYPTOGRAPHIC_SHAPES` table — one per `PersonaTier` — and the
`cryptographic_shape_for` accessor.

The cryptographic shape ascends strict-monotonically across
`SOLO_DEVELOPER -> TEAM_BINDING -> MULTI_TENANT_COMPLIANCE` (§20.2):
append-only (no chain) -> hash-chained -> hash-chained + signature. Chain
construction delegates to U-IS-09 (team-binding+); signing-key resolution
delegates to U-CP-44 (multi-tenant-compliance only — F5 signing-key
resolution).

Authority: Implementation_Plan_Control_Plane_v2_1.md §2.7 U-CP-42;
Spec_Control_Plane_v1_2.md §20 C-CP-20 §20.1 + §20.2 (preserved verbatim into
v1.3); Spec_Information_Substrate_v1.md C-IS-06 (hash-chain construction).
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict


class CryptographicShape(StrEnum):
    """The audit-ledger cryptographic shape per persona tier (C-CP-20 §20.1).

    Closed at cardinality 3. Strength ascends `APPEND_ONLY_SQLITE <
    HASH_CHAINED_SQLITE < HASH_CHAINED_SQLITE_WITH_SIGNATURE` (§20.2)."""

    APPEND_ONLY_SQLITE = "append_only_sqlite"
    """Solo-developer — append-only, no hash chain."""

    HASH_CHAINED_SQLITE = "hash_chained_sqlite"
    """Team-binding — hash-chained, no signature."""

    HASH_CHAINED_SQLITE_WITH_SIGNATURE = "hash_chained_sqlite_with_signature"
    """Multi-tenant-compliance — hash-chained + cryptographic signature."""


# Strict-monotonic strength ordering (§20.2).
_SHAPE_STRENGTH: dict[CryptographicShape, int] = {
    CryptographicShape.APPEND_ONLY_SQLITE: 0,
    CryptographicShape.HASH_CHAINED_SQLITE: 1,
    CryptographicShape.HASH_CHAINED_SQLITE_WITH_SIGNATURE: 2,
}


class PersonaTierCryptographicShape(BaseModel):
    """The audit-ledger cryptographic shape for one persona tier (§20.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_tier: PersonaTier
    cryptographic_shape: CryptographicShape
    chain_construction_source: str | None
    """Delegation citation to U-IS-09; `None` at `SOLO_DEVELOPER` (no chain)."""

    signing_required: bool
    signing_key_source: str | None
    """Delegation citation to U-CP-44; populated only at
    `MULTI_TENANT_COMPLIANCE`."""

    verification_at_read: bool


# --- Registry population (C-CP-20 §20.1/§20.2 3-row table) ------------------

PERSONA_TIER_CRYPTOGRAPHIC_SHAPES: tuple[PersonaTierCryptographicShape, ...] = (
    PersonaTierCryptographicShape(
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        cryptographic_shape=CryptographicShape.APPEND_ONLY_SQLITE,
        chain_construction_source=None,
        signing_required=False,
        signing_key_source=None,
        verification_at_read=False,
    ),
    PersonaTierCryptographicShape(
        persona_tier=PersonaTier.TEAM_BINDING,
        cryptographic_shape=CryptographicShape.HASH_CHAINED_SQLITE,
        chain_construction_source="U-IS-09 (C-IS-06 hash-chain construction)",
        signing_required=False,
        signing_key_source=None,
        verification_at_read=True,
    ),
    PersonaTierCryptographicShape(
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        cryptographic_shape=CryptographicShape.HASH_CHAINED_SQLITE_WITH_SIGNATURE,
        chain_construction_source="U-IS-09 (C-IS-06 hash-chain construction)",
        signing_required=True,
        signing_key_source="U-CP-44 (F5 signing-key resolution)",
        verification_at_read=True,
    ),
)
"""The 3 per-persona-tier cryptographic shapes per C-CP-20 §20.1/§20.2 verbatim."""

_SHAPE_BY_TIER: dict[PersonaTier, PersonaTierCryptographicShape] = {
    e.persona_tier: e for e in PERSONA_TIER_CRYPTOGRAPHIC_SHAPES
}

# Persona-tier ascent order (§20.2) — used for the monotonicity invariant.
_PERSONA_TIER_ORDER: tuple[PersonaTier, ...] = (
    PersonaTier.SOLO_DEVELOPER,
    PersonaTier.TEAM_BINDING,
    PersonaTier.MULTI_TENANT_COMPLIANCE,
)


def cryptographic_shape_for(persona_tier: PersonaTier) -> PersonaTierCryptographicShape:
    """Return the cryptographic shape for a persona tier. Total; deterministic."""
    return _SHAPE_BY_TIER[persona_tier]


def shape_strength(shape: CryptographicShape) -> int:
    """Return the §20.2 monotonic strength rank of a cryptographic shape."""
    return _SHAPE_STRENGTH[shape]
