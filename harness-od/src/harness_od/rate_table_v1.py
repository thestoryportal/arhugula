"""U-OD-47 — v1 default PRICE_TABLE_REF substrate.

Materializes OD spec v1.8 §C-OD-28.3 — authoring substrate. Ships with
operator-default rates for the 3 ADR-F1 v1.2 providers (anthropic + openai +
ollama). Operator overrides via bootstrap config per §C-OD-28.2.

Default rates are PLACEHOLDER per §C-OD-28.5 — operator updates per their
billing arrangements. Authoring date 2026-05-21 (matches RateTable.version
per AC #5).

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-28.3 + §28.5
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-47
- ADR-F1 v1.2 (3 providers: anthropic + openai + ollama)
"""

from __future__ import annotations

from decimal import Decimal

from harness_od.rate_table_types import (
    ProviderRates,
    RateTable,
    ToolRate,
    WebhookRate,
)

# ---------------------------------------------------------------------------
# Provider default rates
# ---------------------------------------------------------------------------
#
# Rate units: USD per million tokens ($/MTok). Default values are operator-
# updatable per §C-OD-28.5; the placeholders below reflect public list-price
# snapshots at authoring date and SHOULD be overridden by bootstrap config in
# production. Anthropic carries cache_read + cache_write columns per the
# provider's prompt-caching surface; openai + ollama populate with zero
# (cache pricing not modeled at v1).

_ANTHROPIC_BASE = ProviderRates(
    input_token_rate=Decimal("3.00"),
    output_token_rate=Decimal("15.00"),
    cache_read_rate=Decimal("0.30"),
    cache_write_rate=Decimal("3.75"),
)

_ANTHROPIC = ProviderRates(
    input_token_rate=_ANTHROPIC_BASE.input_token_rate,
    output_token_rate=_ANTHROPIC_BASE.output_token_rate,
    cache_read_rate=_ANTHROPIC_BASE.cache_read_rate,
    cache_write_rate=_ANTHROPIC_BASE.cache_write_rate,
    per_model_overrides={
        # Sonnet 4.6 / Opus 4.7 per-model override hooks. Operator may
        # populate distinct per-model rates here; default falls through to
        # the provider-level base above per §C-OD-28.4 invariant 4.
        "claude-haiku-4-5": ProviderRates(
            input_token_rate=Decimal("1.00"),
            output_token_rate=Decimal("5.00"),
            cache_read_rate=Decimal("0.10"),
            cache_write_rate=Decimal("1.25"),
        ),
    },
)

_OPENAI = ProviderRates(
    input_token_rate=Decimal("2.50"),
    output_token_rate=Decimal("10.00"),
    # OpenAI cache pricing not modeled at v1; populate with zero so Decimal
    # arithmetic is well-defined per §C-OD-28.4 invariant 2.
    cache_read_rate=Decimal("0"),
    cache_write_rate=Decimal("0"),
)

_OLLAMA = ProviderRates(
    # Local-tier provider per ADR-F1 v1.2; nominal rates are zero. Operator
    # may override for self-hosted GPU cost attribution.
    input_token_rate=Decimal("0"),
    output_token_rate=Decimal("0"),
    cache_read_rate=Decimal("0"),
    cache_write_rate=Decimal("0"),
)


# ---------------------------------------------------------------------------
# Tool / webhook / per-resource default rates
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_RATES: dict[str, ToolRate] = {
    # Default per-tool rates are populated by operator at authoring of new
    # tool contracts (per C-AS-04 tool contract surface). v1 ships an empty
    # baseline — tools without an explicit rate fall back to zero per
    # §C-OD-28.2 resolution discipline (operator may flip to fail-closed via
    # bootstrap config).
}

_DEFAULT_WEBHOOK_RATE = WebhookRate(
    flat_per_attempt=Decimal("0.0001"),
    # Egress cost adds RateTable.egress_rate_per_byte × bytes_sent when True.
    plus_egress=True,
)

# CPU rate per millisecond ($/CPU-ms) for CPU-bound spans (validator per
# U-OD-40 Decision 2.D5 CPU-meter default). Placeholder value reflects a
# rough order-of-magnitude for a commodity vCPU; operator overrides per
# their compute-cost arrangement.
_DEFAULT_CPU_RATE_PER_MS = Decimal("0.0000000278")

# Egress rate per byte ($/byte) for egress-bearing spans (webhook delivery
# per U-OD-40 + WebhookRate.plus_egress). Placeholder reflects $0.09/GB
# converted to per-byte. Operator overrides per their network-cost
# arrangement.
_DEFAULT_EGRESS_RATE_PER_BYTE = Decimal("0.00000000009")


# ---------------------------------------------------------------------------
# Module export — RATE_TABLE_V1
# ---------------------------------------------------------------------------

RATE_TABLE_V1: RateTable = RateTable(
    # AC #5 — version matches authoring date.
    version="2026-05-21",
    providers={
        "anthropic": _ANTHROPIC,
        "openai": _OPENAI,
        "ollama": _OLLAMA,
    },
    tool_rates=_DEFAULT_TOOL_RATES,
    webhook_rate=_DEFAULT_WEBHOOK_RATE,
    cpu_rate_per_ms=_DEFAULT_CPU_RATE_PER_MS,
    egress_rate_per_byte=_DEFAULT_EGRESS_RATE_PER_BYTE,
)
