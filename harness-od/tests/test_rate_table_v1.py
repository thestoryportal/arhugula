"""U-OD-47 — v1 default rate-table substrate tests.

ACs per `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-47:
  #1 anthropic provider has input + output + cache_read + cache_write rates
  #2 openai provider has input + output rates
  #3 ollama provider has nominal rates (or zero for local-only deployment)
  #4 cpu_rate_per_ms + egress_rate_per_byte populated with operator-
     configurable defaults
  #5 RateTable.version = "2026-05-21" (matches authoring date)
"""

from __future__ import annotations

from decimal import Decimal

from harness_od.rate_table_types import ProviderRates, RateTable
from harness_od.rate_table_v1 import RATE_TABLE_V1


def test_rate_table_v1_is_rate_table_instance() -> None:
    assert isinstance(RATE_TABLE_V1, RateTable)


def test_anthropic_provider_has_4_rate_fields() -> None:
    """AC #1 — anthropic has input + output + cache_read + cache_write."""
    anthropic = RATE_TABLE_V1.providers["anthropic"]
    assert isinstance(anthropic, ProviderRates)
    assert anthropic.input_token_rate > Decimal("0")
    assert anthropic.output_token_rate > Decimal("0")
    assert anthropic.cache_read_rate > Decimal("0")
    assert anthropic.cache_write_rate > Decimal("0")


def test_anthropic_per_model_override_has_haiku() -> None:
    """Per-model override populated for at least one model (claude-haiku-4-5)
    to exercise §C-OD-28.4 invariant 4 resolution discipline."""
    anthropic = RATE_TABLE_V1.providers["anthropic"]
    assert anthropic.per_model_overrides is not None
    assert "claude-haiku-4-5" in anthropic.per_model_overrides


def test_openai_provider_has_input_output_rates() -> None:
    """AC #2 — openai has input + output rates."""
    openai = RATE_TABLE_V1.providers["openai"]
    assert openai.input_token_rate > Decimal("0")
    assert openai.output_token_rate > Decimal("0")


def test_ollama_provider_has_nominal_rates() -> None:
    """AC #3 — ollama nominal rates (zero is acceptable for local-only)."""
    ollama = RATE_TABLE_V1.providers["ollama"]
    assert ollama.input_token_rate == Decimal("0")
    assert ollama.output_token_rate == Decimal("0")


def test_cpu_rate_per_ms_populated() -> None:
    """AC #4 — cpu_rate_per_ms populated with operator-configurable default."""
    assert RATE_TABLE_V1.cpu_rate_per_ms > Decimal("0")
    assert isinstance(RATE_TABLE_V1.cpu_rate_per_ms, Decimal)


def test_egress_rate_per_byte_populated() -> None:
    """AC #4 — egress_rate_per_byte populated with operator-configurable default."""
    assert RATE_TABLE_V1.egress_rate_per_byte > Decimal("0")
    assert isinstance(RATE_TABLE_V1.egress_rate_per_byte, Decimal)


def test_rate_table_version_matches_authoring_date() -> None:
    """AC #5 — version = "2026-05-21" matches authoring date."""
    assert RATE_TABLE_V1.version == "2026-05-21"


def test_webhook_rate_populated() -> None:
    assert RATE_TABLE_V1.webhook_rate.flat_per_attempt > Decimal("0")
    # Default ships with egress accounting on (operator may override).
    assert RATE_TABLE_V1.webhook_rate.plus_egress is True


def test_all_3_adr_f1_providers_present() -> None:
    """ADR-F1 v1.2 commits to anthropic + openai + ollama. All 3 present."""
    assert set(RATE_TABLE_V1.providers.keys()) == {"anthropic", "openai", "ollama"}


def test_rate_table_v1_is_immutable() -> None:
    """§C-OD-28.4 invariant 1 — rate-table version immutable per workflow.
    Verified at the type-system level via frozen=True ConfigDict."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RATE_TABLE_V1.version = "9999-99-99"  # type: ignore[misc]
