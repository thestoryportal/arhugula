"""U-OD-48 — rate-table resolver tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-48:
  #1 Per-model override resolves before falling back to provider-level
  #2 Unknown provider raises CP-FAIL-RATE-TABLE-MISSING
  #3 Cached at workflow scope (immutable post-resolution) — caller's concern;
     this module exposes a pure resolution surface (test_resolver_is_pure)
  #4 Decimal arithmetic throughout (no float coercion)
  #5 Unit test: anthropic + claude-sonnet-4-6 resolves with model override
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from harness_od.rate_table_resolver import RateTableMissingError, resolve_for
from harness_od.rate_table_types import (
    ProviderRates,
    RateTable,
    WebhookRate,
)
from harness_od.rate_table_v1 import RATE_TABLE_V1

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _custom_table_with_override() -> RateTable:
    """Build a 1-provider rate table with claude-sonnet-4-6 per-model override
    so we can exercise AC #1 + AC #5 against a known shape.
    """
    return RateTable(
        version="2026-05-21",
        providers={
            "anthropic": ProviderRates(
                input_token_rate=Decimal("3.00"),
                output_token_rate=Decimal("15.00"),
                cache_read_rate=Decimal("0.30"),
                cache_write_rate=Decimal("3.75"),
                per_model_overrides={
                    "claude-sonnet-4-6": ProviderRates(
                        input_token_rate=Decimal("5.00"),
                        output_token_rate=Decimal("25.00"),
                        cache_read_rate=Decimal("0.50"),
                        cache_write_rate=Decimal("6.25"),
                    ),
                },
            ),
        },
        tool_rates={},
        webhook_rate=WebhookRate(flat_per_attempt=Decimal("0"), plus_egress=False),
        cpu_rate_per_ms=Decimal("0"),
        egress_rate_per_byte=Decimal("0"),
    )


# ---------------------------------------------------------------------------
# AC #1 + AC #5 — per-model override resolution
# ---------------------------------------------------------------------------


def test_per_model_override_resolves_before_provider_level() -> None:
    """AC #1 — model-level override wins when present.
    AC #5 — anthropic + claude-sonnet-4-6 resolves with model override."""
    table = _custom_table_with_override()
    resolved = resolve_for(table, provider="anthropic", model="claude-sonnet-4-6")
    assert resolved.input_token_rate == Decimal("5.00")
    assert resolved.output_token_rate == Decimal("25.00")


def test_resolution_falls_back_to_provider_level_when_no_override() -> None:
    """AC #1 — when model is not in per_model_overrides, return provider-level."""
    table = _custom_table_with_override()
    resolved = resolve_for(table, provider="anthropic", model="claude-opus-4-7")
    # opus-4-7 not in overrides → falls back to anthropic-level base rates.
    assert resolved.input_token_rate == Decimal("3.00")
    assert resolved.output_token_rate == Decimal("15.00")


def test_resolution_falls_back_to_provider_level_when_model_none() -> None:
    """AC #1 — when model=None, return provider-level (no override resolution)."""
    table = _custom_table_with_override()
    resolved = resolve_for(table, provider="anthropic", model=None)
    assert resolved.input_token_rate == Decimal("3.00")


def test_per_model_override_resolves_against_rate_table_v1_haiku() -> None:
    """RATE_TABLE_V1 ships with claude-haiku-4-5 override; resolver finds it."""
    resolved = resolve_for(RATE_TABLE_V1, provider="anthropic", model="claude-haiku-4-5")
    # Haiku rates ($1/MTok input) distinct from provider-level ($3/MTok input).
    assert resolved.input_token_rate == Decimal("1.00")


# ---------------------------------------------------------------------------
# AC #2 — Unknown provider raises CP-FAIL-RATE-TABLE-MISSING
# ---------------------------------------------------------------------------


def test_unknown_provider_raises_rate_table_missing() -> None:
    table = _custom_table_with_override()
    with pytest.raises(RateTableMissingError) as exc_info:
        resolve_for(table, provider="cohere", model=None)
    assert "CP-FAIL-RATE-TABLE-MISSING" in str(exc_info.value)
    assert "cohere" in str(exc_info.value)


def test_rate_table_missing_is_lookup_error_subclass() -> None:
    """RateTableMissingError extends LookupError per pythonic taxonomy."""
    assert issubclass(RateTableMissingError, LookupError)


# ---------------------------------------------------------------------------
# AC #3 — Resolver is pure (caching is caller's concern)
# ---------------------------------------------------------------------------


def test_resolver_is_pure_repeated_calls_return_equal_result() -> None:
    """Pure-function discipline: repeated resolution returns equal output."""
    table = _custom_table_with_override()
    a = resolve_for(table, provider="anthropic", model="claude-sonnet-4-6")
    b = resolve_for(table, provider="anthropic", model="claude-sonnet-4-6")
    assert a == b


# ---------------------------------------------------------------------------
# AC #4 — Decimal arithmetic preserved
# ---------------------------------------------------------------------------


def test_resolved_rates_are_decimal_typed() -> None:
    table = _custom_table_with_override()
    resolved = resolve_for(table, provider="anthropic", model="claude-sonnet-4-6")
    assert isinstance(resolved.input_token_rate, Decimal)
    assert isinstance(resolved.output_token_rate, Decimal)
    assert isinstance(resolved.cache_read_rate, Decimal)
    assert isinstance(resolved.cache_write_rate, Decimal)


def test_resolution_against_all_3_rate_table_v1_providers() -> None:
    """RATE_TABLE_V1 carries anthropic + openai + ollama per ADR-F1 v1.2;
    all 3 resolve without raising."""
    for provider in ("anthropic", "openai", "ollama"):
        resolved = resolve_for(RATE_TABLE_V1, provider=provider, model=None)
        assert isinstance(resolved, ProviderRates)
