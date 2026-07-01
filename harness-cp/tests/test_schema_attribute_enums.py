"""Tests for U-CP-00b — `AttributeValueType` + `Cardinality` utility enums.

Test set per the U-CP-00b `Tests:` field (CP plan v2.7 §2.0b). The CP plan
commits the enum member *sets* (byte-exact relocations of the U-CP-01 inline
enums); the "byte-exact" assertions check the member-name sets, since the plan
assigns no string values.
"""

from __future__ import annotations

from harness_cp.schema_attribute_enums import AttributeValueType, Cardinality

# Member-name sets — byte-exact with U-CP-01 (CP plan v2.4 body lines 175-176).
_RELOCATED_ATTRIBUTE_VALUE_TYPES = {"STRING", "INT", "FLOAT", "BOOL", "ENUM_REF"}
_RELOCATED_CARDINALITIES = {"LOW", "MEDIUM", "HIGH", "PER_REQUEST"}


def test_attribute_value_type_cardinality_five() -> None:
    """Acceptance #1 — AttributeValueType is closed at cardinality 5."""
    assert len(AttributeValueType) == 5


def test_attribute_value_type_values_byte_exact_with_relocated_enum() -> None:
    """Acceptance #1 — member set byte-exact with the U-CP-01 inline enum."""
    assert {m.name for m in AttributeValueType} == _RELOCATED_ATTRIBUTE_VALUE_TYPES


def test_cardinality_cardinality_four() -> None:
    """Acceptance #2 — Cardinality is closed at cardinality 4."""
    assert len(Cardinality) == 4


def test_cardinality_values_byte_exact_with_relocated_enum() -> None:
    """Acceptance #2 — member set byte-exact with the U-CP-01 inline enum."""
    assert {m.name for m in Cardinality} == _RELOCATED_CARDINALITIES


def test_both_enums_re_homed_to_harness_core() -> None:
    """The enums are re-homed to `harness-core` per the U-AS-31 Class 1 fork
    resolution (`.harness/class_1_tension_u_as_31_attribute_schema_enums.md`).
    They are cross-axis shared types (CP + AS); `harness_cp.schema_attribute_enums`
    re-exports them so CP-side citations stay stable."""
    assert AttributeValueType.__module__ == "harness_core.schema_attribute_enums"
    assert Cardinality.__module__ == "harness_core.schema_attribute_enums"


def test_attribute_schema_units_resolve_single_nominal_type() -> None:
    """Acceptance #3 — both enums are exposed from one CP-axis carrier path so
    every `…AttributeSchema` consumer (U-CP-01/07/11/21/31/37/46/47) resolves a
    single nominal type. The carrier precondition: each enum is one object,
    importable from the single canonical module. (The full cross-unit
    `pyright`-strict composition check activates when those 7 units land.)"""
    from harness_cp import schema_attribute_enums

    assert schema_attribute_enums.AttributeValueType is AttributeValueType
    assert schema_attribute_enums.Cardinality is Cardinality
