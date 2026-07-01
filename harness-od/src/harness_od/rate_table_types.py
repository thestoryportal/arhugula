"""U-OD-46 — PRICE_TABLE_REF canonical schema (rate-table types).

Materializes OD spec v1.8 §C-OD-28.1 — RateTable + ProviderRates + ToolRate +
WebhookRate. 4 frozen Pydantic v2 models with `Decimal` rate fields. Decimal
arithmetic invariant per §C-OD-28.4 invariant 2 — all rate computations use
Python `Decimal` (not float) for cost-attribution audit precision. String
serialization at the OTel span attribute boundary is U-OD-49.

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-28.1
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-46
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class ProviderRates(BaseModel):
    """Per-provider rate set (§C-OD-28.1).

    `cache_read_rate` + `cache_write_rate` are anthropic-specific at v1; other
    providers populate with zero. `per_model_overrides` enables nested
    resolution per §C-OD-28.4 invariant 4 — model-specific overrides resolve
    before falling back to the provider-level default.
    """

    model_config = _FROZEN_CONFIG

    input_token_rate: Decimal
    output_token_rate: Decimal
    cache_read_rate: Decimal
    cache_write_rate: Decimal
    per_model_overrides: Mapping[str, ProviderRates] | None = None

    def __hash__(self) -> int:
        # `per_model_overrides: Mapping` is not natively hashable. Hash on the
        # 4 Decimal fields + frozenset of per_model_overrides items.
        overrides_hash = (
            frozenset(self.per_model_overrides.items())
            if self.per_model_overrides is not None
            else None
        )
        return hash(
            (
                self.input_token_rate,
                self.output_token_rate,
                self.cache_read_rate,
                self.cache_write_rate,
                overrides_hash,
            )
        )


class ToolRate(BaseModel):
    """Per-tool cost rate (§C-OD-28.1).

    `cost_kind` enumerates the three F2-04-ratified billable-rate kinds for
    tool dispatch per U-OD-39 AC #2:
      - `flat_per_invocation` — cost = rate (constant per invocation)
      - `per_input_byte` — cost = rate × len(canonical_json(args))
      - `per_output_byte` — cost = rate × len(canonical_json(result))
    """

    model_config = _FROZEN_CONFIG

    cost_kind: Literal["flat_per_invocation", "per_input_byte", "per_output_byte"]
    rate: Decimal

    def __hash__(self) -> int:
        return hash((self.cost_kind, self.rate))


class WebhookRate(BaseModel):
    """HITL webhook delivery rate (§C-OD-28.1).

    `flat_per_attempt` is the base per-attempt cost. `plus_egress = True`
    adds `egress_rate_per_byte × bytes_sent` to the per-attempt charge per
    `RateTable.egress_rate_per_byte`.
    """

    model_config = _FROZEN_CONFIG

    flat_per_attempt: Decimal
    plus_egress: bool

    def __hash__(self) -> int:
        return hash((self.flat_per_attempt, self.plus_egress))


class RateTable(BaseModel):
    """The full `PRICE_TABLE_REF` rate table (§C-OD-28.1).

    Resolved at workflow_driver entry per §C-OD-28.2; immutable post-resolution
    per §C-OD-28.4 invariant 1 ("Rate-table version immutable per workflow").
    Operator-overridable via bootstrap config; v1 default substrate at
    `harness_od.rate_table_v1.RATE_TABLE_V1` per §C-OD-28.3 (U-OD-47).
    """

    model_config = _FROZEN_CONFIG

    version: str
    providers: Mapping[str, ProviderRates]
    tool_rates: Mapping[str, ToolRate]
    webhook_rate: WebhookRate
    cpu_rate_per_ms: Decimal
    egress_rate_per_byte: Decimal

    def __hash__(self) -> int:
        return hash(
            (
                self.version,
                frozenset(self.providers.items()),
                frozenset(self.tool_rates.items()),
                self.webhook_rate,
                self.cpu_rate_per_ms,
                self.egress_rate_per_byte,
            )
        )


# Pydantic forward-reference resolution for the self-referential
# `ProviderRates.per_model_overrides: Mapping[str, ProviderRates]` field.
ProviderRates.model_rebuild()
