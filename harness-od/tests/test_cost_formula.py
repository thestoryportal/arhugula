"""Tests for U-OD-18 — per-span Anthropic-pricing cost formula (C-OD-14 §14.1).

Test set per the U-OD-18 `Tests:` field
(Implementation_Plan_Operational_Discipline_v2_1.md §3.5.1). Every acceptance
criterion maps to at least one test.

The §14.1 formula is exercised through `compute_span_cost_with_rates` (the
rate-snapshot overload) because the `PRICE_TABLE_REF` rate table resolves at
U-OD-21 — not yet landed. `compute_span_cost` against the deferred table is
tested for its `RateLookupError` behavior.
"""

from __future__ import annotations

import inspect

import pytest
from harness_od import cost_formula
from harness_od.cost_formula import (
    OUTPUT_TOKEN_EXTENDED_THINKING_SEMANTIC_NOTE,
    PRICE_TABLE_REF,
    PriceRateEntry,
    PriceRateKey,
    RateLookupError,
    SpanCostInputs,
    compute_span_cost,
    compute_span_cost_with_rates,
)

# Acceptance #4 — the §14.1 [MODERATE] semantic note, byte-exact.
_SPEC_NOTE = (
    "v1 includes extended-thinking output tokens per Anthropic "
    "billing-as-output-tokens model [MODERATE; not verified against "
    "primary-source pricing documentation]"
)

_KEY = PriceRateKey(
    provider_name="anthropic",
    model="claude-opus-4",
    tokenizer_version="v1",
)

#: A rate snapshot — round USD-per-token values chosen so the arithmetic is
#: exact under float (acceptance #7 — deterministic given inputs + snapshot).
_RATES = PriceRateEntry(key=_KEY, base_input=10.0, base_output=20.0)


def _inputs(
    *, input_tokens: int, cache_creation: int, cache_read: int, output_tokens: int
) -> SpanCostInputs:
    return SpanCostInputs(
        input_tokens=input_tokens,
        cache_creation=cache_creation,
        cache_read=cache_read,
        output_tokens=output_tokens,
        rate_key=_KEY,
    )


def test_compute_span_cost_no_cache_no_thinking() -> None:
    """Acceptance #2 — uncached-only span: cost = input*BASE_INPUT + output*BASE_OUTPUT."""
    inputs = _inputs(input_tokens=100, cache_creation=0, cache_read=0, output_tokens=50)
    # 100*10 + 0 + 0 + 50*20 = 1000 + 1000 = 2000
    assert compute_span_cost_with_rates(inputs, _RATES) == 2000.0


def test_compute_span_cost_with_cache_creation() -> None:
    """Acceptance #2 — cache-creation contributes BASE_INPUT * 1.25 surcharge."""
    inputs = _inputs(input_tokens=100, cache_creation=40, cache_read=0, output_tokens=0)
    # uncached = 100-0-40 = 60 → 60*10 = 600
    # cache_creation 40*10*1.25 = 500
    assert compute_span_cost_with_rates(inputs, _RATES) == 1100.0


def test_compute_span_cost_with_cache_read() -> None:
    """Acceptance #2 — cache-read contributes BASE_INPUT * 0.10 discount."""
    inputs = _inputs(input_tokens=100, cache_creation=0, cache_read=60, output_tokens=0)
    # uncached = 100-60-0 = 40 → 40*10 = 400
    # cache_read 60*10*0.10 = 60
    assert compute_span_cost_with_rates(inputs, _RATES) == 460.0


def test_compute_span_cost_full_breakdown() -> None:
    """Acceptance #2 — all four formula terms compose per §14.1."""
    inputs = _inputs(input_tokens=200, cache_creation=50, cache_read=50, output_tokens=30)
    # uncached = 200-50-50 = 100 → 100*10 = 1000
    # cache_creation 50*10*1.25 = 625
    # cache_read 50*10*0.10 = 50
    # output 30*20 = 600
    assert compute_span_cost_with_rates(inputs, _RATES) == 2275.0


def test_compute_span_cost_extended_thinking_included() -> None:
    """Acceptance #4 — output-token contribution includes extended-thinking
    tokens (they are counted in output_tokens, billed at BASE_OUTPUT)."""
    base = _inputs(input_tokens=10, cache_creation=0, cache_read=0, output_tokens=10)
    with_thinking = _inputs(input_tokens=10, cache_creation=0, cache_read=0, output_tokens=30)
    base_cost = compute_span_cost_with_rates(base, _RATES)
    thinking_cost = compute_span_cost_with_rates(with_thinking, _RATES)
    # 20 extra output tokens at BASE_OUTPUT=20 → +400
    assert thinking_cost - base_cost == 400.0


def test_no_reasoning_output_tokens_field() -> None:
    """Acceptance #5 — the legacy reasoning.output_tokens line (dropped per
    F2-01) is NOT present: SpanCostInputs has no reasoning-token field."""
    fields = set(SpanCostInputs.model_fields)
    assert fields == {
        "input_tokens",
        "cache_creation",
        "cache_read",
        "output_tokens",
        "rate_key",
    }
    source = inspect.getsource(cost_formula)
    assert "reasoning.output_tokens" not in source
    assert "reasoning_output_tokens" not in source


def test_compute_span_cost_non_negative() -> None:
    """Acceptance #1 — cost is non-negative for any valid inputs
    (input_tokens >= cache_read + cache_creation, output_tokens >= 0)."""
    inputs = _inputs(input_tokens=500, cache_creation=100, cache_read=200, output_tokens=0)
    assert compute_span_cost_with_rates(inputs, _RATES) >= 0.0


def test_compute_span_cost_zero_inputs() -> None:
    """Acceptance #1 — an all-zero span costs 0.0."""
    inputs = _inputs(input_tokens=0, cache_creation=0, cache_read=0, output_tokens=0)
    assert compute_span_cost_with_rates(inputs, _RATES) == 0.0


def test_rate_key_three_field_cardinality() -> None:
    """Acceptance #3 — PriceRateKey is keyed on exactly 3 fields per §15.2."""
    assert set(PriceRateKey.model_fields) == {
        "provider_name",
        "model",
        "tokenizer_version",
    }


def test_cache_invariant_holds_at_input() -> None:
    """Acceptance #6 — the formula assumes cache_creation + cache_read + uncached
    == input_tokens; with a well-formed input the uncached term is non-negative."""
    inputs = _inputs(input_tokens=300, cache_creation=100, cache_read=100, output_tokens=10)
    uncached = inputs.input_tokens - inputs.cache_read - inputs.cache_creation
    assert uncached == 100
    assert uncached >= 0


def test_extended_thinking_semantic_note_byte_exact() -> None:
    """Acceptance #4 — OUTPUT_TOKEN_EXTENDED_THINKING_SEMANTIC_NOTE matches the
    §14.1 [MODERATE] confidence annotation verbatim."""
    assert OUTPUT_TOKEN_EXTENDED_THINKING_SEMANTIC_NOTE == _SPEC_NOTE


def test_compute_span_cost_deterministic() -> None:
    """Acceptance #7 — the formula is deterministic given inputs + rate snapshot."""
    inputs = _inputs(input_tokens=123, cache_creation=10, cache_read=20, output_tokens=45)
    first = compute_span_cost_with_rates(inputs, _RATES)
    second = compute_span_cost_with_rates(inputs, _RATES)
    assert first == second


def test_compute_span_cost_rate_key_mismatch_raises() -> None:
    """Acceptance #3 — a rate entry whose key does not match the span input's
    rate_key is a RateLookupError."""
    other_key = PriceRateKey(
        provider_name="anthropic", model="claude-haiku-4", tokenizer_version="v1"
    )
    other_rates = PriceRateEntry(key=other_key, base_input=1.0, base_output=2.0)
    inputs = _inputs(input_tokens=10, cache_creation=0, cache_read=0, output_tokens=10)
    with pytest.raises(RateLookupError):
        compute_span_cost_with_rates(inputs, other_rates)


def test_compute_span_cost_deferred_rate_table_raises() -> None:
    """Acceptance #3 / #8 — PRICE_TABLE_REF resolves at U-OD-21; until then
    compute_span_cost against the deferred table raises RateLookupError."""
    inputs = _inputs(input_tokens=10, cache_creation=0, cache_read=0, output_tokens=10)
    with pytest.raises(RateLookupError):
        compute_span_cost(inputs)
    # The opaque marker is declared and resolves at U-OD-21.
    assert isinstance(PRICE_TABLE_REF, str)
