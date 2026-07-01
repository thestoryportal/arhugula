"""Schema-attribute utility enums — cross-axis shared types.

Declares `AttributeValueType` and `Cardinality` — the value-type and
cardinality discriminators used to type per-namespace `…AttributeSchema`
records across axes: the CP attribute-schema records (U-CP-01/07/11/21/31/37/
46/47) **and** the AS Anthropic-primitive namespace schemas (U-AS-31, C-AS-14).

Carrier-home: re-homed to `harness-core` per the U-AS-31 Class 1 fork
resolution (`.harness/class_1_tension_u_as_31_attribute_schema_enums.md`,
operator-ruled 2026-05-16). U-CP-00b originally landed these enums in
`harness-cp` under operator decision D3 ("no cross-axis sharing"); U-AS-31
(AS axis) consuming them would require an AS→CP package edge that cycles
against the 24 declared CP→AS edges (CXA v2.1 §2.3.4). They are genuinely
cross-axis shared types and belong on the single `harness-core` path per
`CLAUDE.md` §3.3 (the U-CORE-01 pattern). `harness-cp` re-exports them from
here for CP-side citation stability.

Member sets are byte-exact with the enums U-CP-00b landed (relocated from the
CP plan v2.4 U-CP-01 body lines 175-176): `AttributeValueType` =
`STRING | INT | FLOAT | BOOL | ENUM_REF`; `Cardinality` =
`LOW | MEDIUM | HIGH | PER_REQUEST`. The SCREAMING_SNAKE_CASE names and
lowercase string values are a Python-stack naming convention.
"""

from __future__ import annotations

from enum import StrEnum


class AttributeValueType(StrEnum):
    """The 5 attribute value-type discriminators. Closed at cardinality 5."""

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ENUM_REF = "enum_ref"


class Cardinality(StrEnum):
    """The 4 attribute-cardinality discriminators. Closed at cardinality 4."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PER_REQUEST = "per_request"
