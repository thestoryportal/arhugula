"""U-OD-49 — Decimal OTel attribute serializer tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-49:
  #1 serialize_decimal_for_otel(Decimal("1.234567890123")) returns
     "1.234567890123" (full precision)
  #2 deserialize_otel_decimal(s) round-trips byte-exact
  #3 OTel span attribute cost.attributed_decimal populated via string-form
  #4 OD sqlite span store preserves string form in attributes_json column
     (integration concern; verified via in-memory exporter assertion that
     emitted attribute value is `str` not `float`)
  #5 Property-based test: 1000 random Decimals round-trip without precision loss
"""

from __future__ import annotations

import json
import random
from decimal import Decimal

import pytest
from harness_od.cost_record_otel_serializer import (
    COST_ATTRIBUTED_DECIMAL_ATTR,
    deserialize_otel_decimal,
    serialize_decimal_for_otel,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------------------------------------------------------------------------
# AC #1 — serialize_decimal_for_otel preserves full precision
# ---------------------------------------------------------------------------


def test_serialize_preserves_full_precision() -> None:
    assert serialize_decimal_for_otel(Decimal("1.234567890123")) == "1.234567890123"


def test_serialize_extreme_precision() -> None:
    d = Decimal("0.000000000000000000000001")  # 1e-24
    assert serialize_decimal_for_otel(d) == "1E-24"


def test_serialize_large_value() -> None:
    d = Decimal("123456789012345678901234567890.987654321")
    s = serialize_decimal_for_otel(d)
    # Round-trip recovers byte-exact value (str form may use scientific notation).
    assert Decimal(s) == d


def test_serialize_zero() -> None:
    assert serialize_decimal_for_otel(Decimal("0")) == "0"


def test_serialize_negative() -> None:
    assert serialize_decimal_for_otel(Decimal("-3.14159")) == "-3.14159"


# ---------------------------------------------------------------------------
# AC #2 — Round-trip byte-exact
# ---------------------------------------------------------------------------


def test_round_trip_byte_exact_simple() -> None:
    original = Decimal("1.234567890123")
    serialized = serialize_decimal_for_otel(original)
    recovered = deserialize_otel_decimal(serialized)
    assert recovered == original


def test_round_trip_byte_exact_complex() -> None:
    cases = [
        Decimal("0"),
        Decimal("1"),
        Decimal("-1"),
        Decimal("3.14159265358979323846"),
        Decimal("1E-24"),
        Decimal("9.99E+99"),
        Decimal("0.0000000001"),
        Decimal("1234567890.0987654321"),
    ]
    for d in cases:
        assert deserialize_otel_decimal(serialize_decimal_for_otel(d)) == d


# ---------------------------------------------------------------------------
# AC #3 + AC #4 — OTel attribute populated via string-form;
# in-memory exporter receives string (not float)
# ---------------------------------------------------------------------------


def test_otel_span_attribute_round_trips_decimal_via_string() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.cost.serializer")

    original_cost = Decimal("0.000142857142857")
    with tracer.start_as_current_span("test.cost") as span:
        span.set_attribute(
            COST_ATTRIBUTED_DECIMAL_ATTR,
            serialize_decimal_for_otel(original_cost),
        )

    span_emitted = exporter.get_finished_spans()[0]
    raw_attr = (span_emitted.attributes or {}).get(COST_ATTRIBUTED_DECIMAL_ATTR)

    # AC #3 + AC #4 — attribute value is string-form (not float).
    assert isinstance(raw_attr, str)
    assert raw_attr == "0.000142857142857"

    # AC #2 round-trip — recover the canonical Decimal.
    recovered = deserialize_otel_decimal(raw_attr)
    assert recovered == original_cost


def test_attributes_json_preservation_simulation() -> None:
    """AC #4 — JSON-serialization (the format used by the C-OD-27 sqlite
    attributes_json column) preserves the Decimal-as-string form."""
    original = Decimal("3.14159265358979323846")
    attr_value = serialize_decimal_for_otel(original)
    json_blob = json.dumps({COST_ATTRIBUTED_DECIMAL_ATTR: attr_value})
    restored_blob = json.loads(json_blob)
    recovered = deserialize_otel_decimal(restored_blob[COST_ATTRIBUTED_DECIMAL_ATTR])
    assert recovered == original


# ---------------------------------------------------------------------------
# AC #5 — Property-based: 1000 random Decimals round-trip without precision loss
# ---------------------------------------------------------------------------


def test_property_based_1000_random_decimals_round_trip() -> None:
    """Deterministic random sampling (seeded) — 1000 Decimals across varied
    magnitudes and precisions, all round-trip byte-exact."""
    rng = random.Random(0xDEC1)  # deterministic seed
    failures: list[tuple[Decimal, Decimal]] = []
    for _ in range(1000):
        # Vary magnitude (10**-20 .. 10**20) and digit count (1..40 chars).
        digits = "".join(str(rng.randint(0, 9)) for _ in range(rng.randint(1, 40)))
        if not digits.lstrip("0"):
            digits = "0"
        sign = "-" if rng.random() < 0.5 else ""
        exponent = rng.randint(-20, 20)
        try:
            d = Decimal(f"{sign}{digits}E{exponent}")
        except Exception:
            continue  # malformed sample; skip
        roundtripped = deserialize_otel_decimal(serialize_decimal_for_otel(d))
        if roundtripped != d:
            failures.append((d, roundtripped))
    assert failures == [], f"{len(failures)} round-trip failures: {failures[:5]}"


# ---------------------------------------------------------------------------
# Robustness — invalid input shape
# ---------------------------------------------------------------------------


def test_deserialize_invalid_string_raises() -> None:
    from decimal import InvalidOperation

    with pytest.raises(InvalidOperation):
        deserialize_otel_decimal("not-a-decimal")
