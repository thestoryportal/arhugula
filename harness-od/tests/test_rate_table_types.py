"""U-OD-46 — rate-table type tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-46:
  #1 All 4 dataclasses instantiable with Decimal-typed rate fields
  #2 Pydantic v2 validation
  #3 Frozen + hashable
  #4 pyright strict mode passes  (enforced at workspace-wide pyright)
  #5 Unit test: serialize → deserialize round-trip preserves Decimal precision
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from harness_od.rate_table_types import (
    ProviderRates,
    RateTable,
    ToolRate,
    WebhookRate,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# AC #1 — Instantiation with Decimal-typed fields
# ---------------------------------------------------------------------------


def test_provider_rates_instantiable_with_decimal_fields() -> None:
    rates = ProviderRates(
        input_token_rate=Decimal("3.00"),
        output_token_rate=Decimal("15.00"),
        cache_read_rate=Decimal("0.30"),
        cache_write_rate=Decimal("3.75"),
    )
    assert rates.input_token_rate == Decimal("3.00")
    assert isinstance(rates.input_token_rate, Decimal)


def test_tool_rate_instantiable_with_cost_kind() -> None:
    rate = ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.001"))
    assert rate.cost_kind == "flat_per_invocation"
    assert rate.rate == Decimal("0.001")


def test_webhook_rate_instantiable() -> None:
    rate = WebhookRate(flat_per_attempt=Decimal("0.0001"), plus_egress=True)
    assert rate.plus_egress is True


def test_rate_table_instantiable_with_full_substrate() -> None:
    table = RateTable(
        version="2026-05-21",
        providers={"anthropic": _anthropic_rates()},
        tool_rates={"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0"))},
        webhook_rate=WebhookRate(flat_per_attempt=Decimal("0"), plus_egress=False),
        cpu_rate_per_ms=Decimal("0.000001"),
        egress_rate_per_byte=Decimal("0.00000001"),
    )
    assert table.version == "2026-05-21"
    assert "anthropic" in table.providers


# ---------------------------------------------------------------------------
# AC #2 — Pydantic v2 validation
# ---------------------------------------------------------------------------


def test_invalid_cost_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolRate(cost_kind="invalid_kind", rate=Decimal("1"))  # type: ignore[arg-type]


def test_extra_fields_rejected_on_provider_rates() -> None:
    with pytest.raises(ValidationError):
        ProviderRates(
            input_token_rate=Decimal("1"),
            output_token_rate=Decimal("1"),
            cache_read_rate=Decimal("0"),
            cache_write_rate=Decimal("0"),
            unexpected_field="boom",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# AC #3 — Frozen + hashable
# ---------------------------------------------------------------------------


def test_provider_rates_frozen() -> None:
    rates = _anthropic_rates()
    with pytest.raises(ValidationError):
        rates.input_token_rate = Decimal("999")  # type: ignore[misc]


def test_provider_rates_hashable() -> None:
    rates = _anthropic_rates()
    # Should not raise; usable as dict key / set member.
    assert hash(rates) == hash(_anthropic_rates())
    {rates}


def test_rate_table_frozen() -> None:
    table = _table()
    with pytest.raises(ValidationError):
        table.version = "9999-99-99"  # type: ignore[misc]


def test_rate_table_hashable() -> None:
    table = _table()
    assert hash(table) == hash(_table())


def test_tool_rate_hashable() -> None:
    a = ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.01"))
    b = ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.01"))
    assert hash(a) == hash(b)


def test_webhook_rate_hashable() -> None:
    a = WebhookRate(flat_per_attempt=Decimal("0.01"), plus_egress=True)
    b = WebhookRate(flat_per_attempt=Decimal("0.01"), plus_egress=True)
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# AC #5 — Serialize → deserialize round-trip preserves Decimal precision
# ---------------------------------------------------------------------------


def test_provider_rates_json_round_trip_preserves_decimal_precision() -> None:
    rates = ProviderRates(
        input_token_rate=Decimal("1.234567890123456789"),
        output_token_rate=Decimal("9.876543210987654321"),
        cache_read_rate=Decimal("0.000000000000000001"),
        cache_write_rate=Decimal("123456789.0"),
    )
    json_str = rates.model_dump_json()
    restored = ProviderRates.model_validate_json(json_str)
    assert restored.input_token_rate == rates.input_token_rate
    assert restored.output_token_rate == rates.output_token_rate
    assert restored.cache_read_rate == rates.cache_read_rate
    assert restored.cache_write_rate == rates.cache_write_rate


def test_rate_table_json_round_trip_preserves_decimal_precision() -> None:
    table = _table()
    json_str = table.model_dump_json()
    restored = RateTable.model_validate_json(json_str)
    assert restored == table
    assert (
        restored.providers["anthropic"].input_token_rate
        == table.providers["anthropic"].input_token_rate
    )


# ---------------------------------------------------------------------------
# Self-referential per_model_overrides nested resolution
# ---------------------------------------------------------------------------


def test_per_model_overrides_nested_resolution() -> None:
    base = ProviderRates(
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
    )
    assert base.per_model_overrides is not None
    override = base.per_model_overrides["claude-sonnet-4-6"]
    assert override.input_token_rate == Decimal("5.00")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _anthropic_rates() -> ProviderRates:
    return ProviderRates(
        input_token_rate=Decimal("3.00"),
        output_token_rate=Decimal("15.00"),
        cache_read_rate=Decimal("0.30"),
        cache_write_rate=Decimal("3.75"),
    )


def _table() -> RateTable:
    return RateTable(
        version="2026-05-21",
        providers={"anthropic": _anthropic_rates()},
        tool_rates={"echo": ToolRate(cost_kind="flat_per_invocation", rate=Decimal("0.001"))},
        webhook_rate=WebhookRate(flat_per_attempt=Decimal("0.0001"), plus_egress=True),
        cpu_rate_per_ms=Decimal("0.000001"),
        egress_rate_per_byte=Decimal("0.00000001"),
    )
