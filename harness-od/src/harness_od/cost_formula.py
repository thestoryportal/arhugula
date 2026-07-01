"""Per-span Anthropic-pricing canonical cost formula — U-OD-18.

Implements C-OD-14 §14.1 (per-span cost formula, Anthropic-pricing canonical).

The cost-attribution-per-span foundational primitive: `compute_span_cost`
materializes the §14.1 formula —

    cost = (input_tokens - cache_read - cache_creation) * BASE_INPUT
         + cache_creation * BASE_INPUT * 1.25      # 5-min TTL cache creation surcharge
         + cache_read     * BASE_INPUT * 0.10      # cache hit discount
         + output_tokens  * BASE_OUTPUT            # includes extended-thinking tokens

where `BASE_INPUT` / `BASE_OUTPUT` are per-`(provider, model, tokenizer_version)`
rate values keyed by the 3-field `PriceRateKey` per C-OD-15 §15.2. The rate
table itself resides at U-OD-21 / deployment-binding-time refresh — `U-OD-18`
declares the formula and the rate-table reference marker only.

Authority: Implementation_Plan_Operational_Discipline_v2_1.md §3.5.1 U-OD-18
(preserved verbatim through v2.5 §0.3 + v2.6 §3 — no delta);
Spec_Operational_Discipline_v1_2.md §14 C-OD-14 §14.1 (preserved verbatim into
v1.3 — the v1.3 §14.5 amendment does not touch §14.1); ADR-D6 v1.1 §1.5
cost-attribution-per-span dashboarding contract.

Scaffold note. The plan §3.5.1 fn body references `lookup_rates(PRICE_TABLE_REF,
rate_key)` without signaturing it; `PRICE_TABLE_REF` is declared `opaque
Reference` "resolved at U-OD-21". This module materializes `PRICE_TABLE_REF` as
an opaque marker and `lookup_rates` as a module-private rate-table resolution
function — plan-internal scaffolding, not a design extension. `compute_span_cost`
also accepts an explicit `PriceRateEntry` overload so the formula is unit-testable
without the deferred U-OD-21 rate table (acceptance #7 — deterministic given
inputs + a rate-table snapshot).
"""

from __future__ import annotations

from typing import NewType

from pydantic import BaseModel, ConfigDict

__all__ = [
    "OUTPUT_TOKEN_EXTENDED_THINKING_SEMANTIC_NOTE",
    "PRICE_TABLE_REF",
    "PriceRateEntry",
    "PriceRateKey",
    "PriceTableRef",
    "RateLookupError",
    "SpanCostInputs",
    "compute_span_cost",
    "compute_span_cost_with_rates",
]

# --- §14.1 cache-tier coefficients (verbatim) ------------------------------

#: 5-minute-TTL cache-creation surcharge multiplier on `BASE_INPUT` (§14.1).
_CACHE_CREATION_SURCHARGE: float = 1.25

#: Cache-read (cache-hit) discount multiplier on `BASE_INPUT` (§14.1).
_CACHE_READ_DISCOUNT: float = 0.10


PriceTableRef = NewType("PriceTableRef", str)
"""Opaque marker for the deferred rate-table reference.

The Python materialization of the plan's `opaque PRICE_TABLE_REF : Reference` —
the concrete rate table resolves at U-OD-21 / deployment-binding-time refresh.
"""

#: The opaque rate-table reference — resolved at U-OD-21 (acceptance #3).
PRICE_TABLE_REF: PriceTableRef = PriceTableRef("od-price-table-ref::deferred-to-U-OD-21")


class PriceRateKey(BaseModel):
    """The 3-field versioned price-table key (C-OD-15 §15.2).

    Frozen → `Eq` + `Hash`, so it keys the rate table. Per §15.2 the rate table
    is versioned-keyed on exactly these three fields (acceptance #3 / the
    3-field-cardinality test).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    """`gen_ai.provider.name`."""

    model: str
    """`gen_ai.request.model`."""

    tokenizer_version: str
    """`anthropic.tokenizer_version`."""

    def __hash__(self) -> int:
        return hash((self.provider_name, self.model, self.tokenizer_version))


class PriceRateEntry(BaseModel):
    """A per-key rate-table entry — USD-per-token rates for input and output.

    `base_output` includes extended-thinking output tokens per the Anthropic
    billing-as-output-tokens model (acceptance #4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: PriceRateKey
    base_input: float
    """USD per input token (`BASE_INPUT`)."""

    base_output: float
    """USD per output token, incl. extended-thinking tokens (`BASE_OUTPUT`)."""


class SpanCostInputs(BaseModel):
    """The per-span token-count inputs to the §14.1 cost formula.

    `cache_creation` / `cache_read` are subsets of `input_tokens` per the
    C-OD-08 §8.2 cache-tier breakdown invariant
    (`cache_creation + cache_read + uncached == input_tokens`); violation is
    rejected at U-OD-10's `enforce_otel_canonical_value` (acceptance #6).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int
    cache_creation: int
    """`anthropic.cache_creation_input_tokens`."""

    cache_read: int
    """`anthropic.cache_read_input_tokens`."""

    output_tokens: int
    """Includes extended-thinking output tokens."""

    rate_key: PriceRateKey


class RateLookupError(Exception):
    """Raised when the rate table holds no entry for a `PriceRateKey`.

    Inline error-type materialization per the OD-plan materializability audit
    M-1 error-type-tail disposition (`.harness/materializability_audit_od_plan.md`):
    thin error classes are materialized inline at the first-consuming unit.
    """


def _formula(inputs: SpanCostInputs, rates: PriceRateEntry) -> float:
    """The §14.1 per-span cost formula, verbatim.

    `uncached = input_tokens - cache_read - cache_creation`; total is the
    uncached contribution + the cache-creation 1.25x surcharge + the cache-read
    0.10x discount + the output contribution (acceptance #2).
    """
    uncached = inputs.input_tokens - inputs.cache_read - inputs.cache_creation
    return (
        uncached * rates.base_input
        + inputs.cache_creation * rates.base_input * _CACHE_CREATION_SURCHARGE
        + inputs.cache_read * rates.base_input * _CACHE_READ_DISCOUNT
        + inputs.output_tokens * rates.base_output
    )


def compute_span_cost_with_rates(inputs: SpanCostInputs, rates: PriceRateEntry) -> float:
    """Compute the per-span cost given an explicit `PriceRateEntry` snapshot.

    The rate-table-snapshot overload of `compute_span_cost` — deterministic
    given `inputs` and `rates` (acceptance #7), and testable without the
    deferred U-OD-21 rate table. `rates.key` must match `inputs.rate_key`.
    """
    if rates.key != inputs.rate_key:
        raise RateLookupError(
            f"rate entry key {rates.key!r} does not match span input rate_key {inputs.rate_key!r}"
        )
    return _formula(inputs, rates)


def _lookup_rates(table_ref: PriceTableRef, key: PriceRateKey) -> PriceRateEntry:
    """Resolve the `PriceRateEntry` for `key` from the rate table at `table_ref`.

    Module-private scaffold for the plan's `lookup_rates(PRICE_TABLE_REF,
    rate_key)` reference. The concrete rate table resides at U-OD-21 /
    deployment-binding-time refresh (acceptance #3); until U-OD-21 lands there
    is no resident table, so this raises `RateLookupError`. `compute_span_cost`
    callers that hold a rate snapshot use `compute_span_cost_with_rates`.
    """
    del table_ref, key
    raise RateLookupError(
        "no resident rate table — PRICE_TABLE_REF resolves at U-OD-21; "
        "use compute_span_cost_with_rates with an explicit PriceRateEntry"
    )


def compute_span_cost(inputs: SpanCostInputs) -> float:
    """Compute the per-span Anthropic-pricing cost (C-OD-14 §14.1).

    Resolves the rate entry for `inputs.rate_key` via the `PRICE_TABLE_REF`
    rate table, then applies the §14.1 formula. Until the U-OD-21 rate table
    lands, callers that hold a rate snapshot should use
    `compute_span_cost_with_rates`. The formula is declarative — implementations
    may pre-compute or cache rate-table lookups per the deployment-binding-time
    refresh cadence (acceptance #8).
    """
    rates = _lookup_rates(PRICE_TABLE_REF, inputs.rate_key)
    return _formula(inputs, rates)


#: The §14.1 [MODERATE]-confidence semantic note on output-token billing,
#: transcribed verbatim from the U-OD-18 plan Signatures block (acceptance #4).
OUTPUT_TOKEN_EXTENDED_THINKING_SEMANTIC_NOTE: str = (
    "v1 includes extended-thinking output tokens per Anthropic "
    "billing-as-output-tokens model [MODERATE; not verified against "
    "primary-source pricing documentation]"
)
