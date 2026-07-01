"""Decimal ProviderRates → float PriceRateEntry bridge for U-OD-38.

§C-OD-28.4 invariant 2 mandates Decimal arithmetic at the rate-table layer
(per U-OD-46/47/48). The existing cost-formula chain at `cost_formula.py`
uses `float`-typed `PriceRateEntry` per the older C-OD-14 §14.1 contract.
This bridge converts the Decimal-typed ProviderRates (per-MTok rates) into
the float-typed PriceRateEntry (per-token rates) that the chain consumes.

Precision trade-off: float coercion loses precision past ~15 significant
digits. Acceptable at v1 for the compute_per_attempt_cost path; full-Decimal
chain migration is deferred to a follow-on arc (Class 3 drift candidate per
§C-OD-28.4 invariant 2 prose vs cost_formula float reality). Final cost
emission at the OTel span attribute boundary uses U-OD-49 string-form
preserving the original Decimal product when the bridge is bypassed by
audit-record-only path.

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-28.1 + §28.4
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-38
"""

from __future__ import annotations

from harness_od.cost_formula import PriceRateEntry, PriceRateKey
from harness_od.rate_table_types import ProviderRates

#: Tokens-per-MTok conversion factor. Per-MTok rates ($/MTok) divide by this
#: to obtain per-token rates ($/token).
_TOKENS_PER_MTOK = 1_000_000


def provider_rates_to_price_rate_entry(
    rates: ProviderRates,
    key: PriceRateKey,
) -> PriceRateEntry:
    """Bridge new Decimal ProviderRates → existing float PriceRateEntry.

    Converts the Decimal $/MTok rates from `ProviderRates` to the
    `PriceRateEntry` float $/token shape that `cost_formula.compute_span_cost_with_rates`
    expects. The conversion floats the Decimal at the bridge boundary —
    downstream `_formula` arithmetic is float-only.

    Parameters
    ----------
    rates
        The resolved per-(provider, model) Decimal rate set from
        `rate_table_resolver.resolve_for`.
    key
        The PriceRateKey identifying the (provider, model, tokenizer_version)
        triple this rate entry corresponds to.

    Returns
    -------
    PriceRateEntry
        Float-typed per-token rates suitable for
        `RuntimeCostAttributionChain.compute_per_attempt_cost`.
    """
    return PriceRateEntry(
        key=key,
        base_input=float(rates.input_token_rate) / _TOKENS_PER_MTOK,
        base_output=float(rates.output_token_rate) / _TOKENS_PER_MTOK,
    )
