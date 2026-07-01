"""U-OD-49 — Decimal string-serialization at OTel span attribute boundary.

Materializes OD spec v1.8 §C-OD-28.4 invariant 3 (operator-ratified at Phase
B iteration-1 F2-06). Cost values emitted as OTel span attributes are
string-serialized (NOT float-serialized) to preserve Decimal precision through
the OTel exporter pipeline.

Pattern per §C-OD-28.4 invariant 3:
    span.set_attribute("cost.attributed_decimal", str(decimal_value))
    # Consumer side:
    Decimal(span_attr_string)

Non-standard OTel pattern but audit-correct; OD sqlite span store (per
C-OD-27 sqlite write-path) preserves the string form in the `attributes_json`
column. Float-serialization would defeat §C-OD-28.4 invariant 2 (Decimal
arithmetic) at the observability boundary.

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-28.4 invariant 3 + F2-06
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-49
"""

from __future__ import annotations

from decimal import Decimal

# Canonical OTel attribute key for Decimal-encoded cost values per
# §C-OD-28.4 invariant 3. Consumers MUST parse via `deserialize_otel_decimal`
# to recover the canonical Decimal (do NOT pass through float()).
COST_ATTRIBUTED_DECIMAL_ATTR = "cost.attributed_decimal"


def serialize_decimal_for_otel(value: Decimal) -> str:
    """Serialize a Decimal for OTel span attribute emission (§C-OD-28.4 inv 3).

    Returns the canonical string form of the Decimal via `str()`. Preserves
    full precision (no float coercion). Designed for symmetric round-trip
    with `deserialize_otel_decimal` — `Decimal(str(d)) == d` for any Decimal
    `d`.

    Parameters
    ----------
    value
        A `Decimal` cost value (per the OD §28.4 invariant 2 invariant —
        all cost arithmetic uses Python Decimal, not float).

    Returns
    -------
    str
        The canonical string form preserving full precision. Pass directly
        to `span.set_attribute(COST_ATTRIBUTED_DECIMAL_ATTR, ...)`.
    """
    return str(value)


def deserialize_otel_decimal(s: str) -> Decimal:
    """Deserialize an OTel span attribute string back into a Decimal.

    Round-trip inverse of `serialize_decimal_for_otel`. Use at OTel
    attribute consumption sites (OD sqlite store reader per C-OD-27 §27.3;
    downstream analytics) to recover the canonical Decimal without float
    coercion.

    Parameters
    ----------
    s
        The serialized Decimal string (produced by `serialize_decimal_for_otel`
        or any other string-form Decimal serialization).

    Returns
    -------
    Decimal
        The recovered Decimal value, byte-exact equal to the input prior to
        serialization.

    Raises
    ------
    decimal.InvalidOperation
        `s` is not a valid Decimal string (Python's Decimal constructor
        behavior). Callers responsible for ensuring inputs come from the
        canonical serialization site.
    """
    return Decimal(s)
